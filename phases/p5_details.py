"""فاز ۵ — تکمیل ستون‌های گوگل‌شیت محصولات.

ورودی: فهرست عنوان‌های شما (ستون اول شیت، یا خروجی فازهای قبلی).
خروجی: همان شیت، با ستون‌های کلمه‌ی کلیدی، نویسنده، خلاصه، دسته، تگ، ملیت،
فرمت، مترجم، تعداد صفحات و تصویر — پرشده و قابل تحویل به اسکریپت درج محصول.

مسیر کار برای هر عنوان::

    عنوان → پیدا کردن صفحه‌های همان محصول در منابع
          → استخراج دیتیل از هر صفحه            (core.details)
          → رأی‌گیری بین منابع                   (core.consensus)
          → نگاشت برچسب‌ها به کرکره‌های شیت        (core.taxonomy)
          → انتخاب کاور مربعِ بی‌واترمارک          (core.images)
          → نوشتن فقط در سلول‌های خالیِ نگاشت‌شده  (core.gsheet)

سه ضمانت که روی ۱۵ هزار ردیف اهمیت دارند:

* **از سر گرفتنی است.** هر ردیف بلافاصله در ``detail_rows`` نوشته می‌شود؛
  اجرای بعدی از همان‌جا ادامه می‌دهد و صفحه‌های دانلودشده هم کش شده‌اند.
* **سقف نشست دارد.** ``max_titles_per_session`` و ``max_pages_per_session``
  جلوی یک اجرای ۲۰ ساعته‌ی بی‌نظارت را می‌گیرند.
* **ستون وضعیت و شناسه‌ی محصول دست نمی‌خورد.** آن دو مالِ اسکریپت درج
  محصول‌اند و در ``never_write`` هستند.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..core import (
    consensus,
    db,
    details,
    extract,
    gsheet,
    images,
    normalizer,
    similarity,
    taxonomy,
)
from ..core.config import Config, SiteConfig
from ..core.details import PageDetails
from ..core.http import Fetcher

LogFn = Callable[[str], None]

#: ستون‌هایی که این فاز پر می‌کند (به ترتیب خودِ شیت)
FILLABLE = (
    "keyword",
    "author",
    "summary",
    "categories",
    "tags",
    "nationality",
    "book_format",
    "translator",
    "pages",
    "image",
)


# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------


@dataclass
class DetailOptions:
    """بخش ``details`` در config، به‌علاوه‌ی چیزی که در پنل تنظیم شده."""

    sheet: gsheet.SheetSettings = field(default_factory=gsheet.SheetSettings)
    image: images.ImageRules = field(default_factory=images.ImageRules)
    #: ``empty`` = فقط سلول خالی پر شود، ``always`` = مقدار قبلی هم بازنویسی شود
    overwrite: str = "empty"
    #: ردیف‌هایی که قبلاً پردازش شده‌اند ولی همه‌ی ستون‌هایشان پر نشد، دوباره
    #: امتحان شوند یا نه. پیش‌فرض «نه»: بعضی ستون‌ها واقعاً مقدار ندارند
    #: (مترجمِ رمان ایرانی) و بدون این، هر اجرا همان ردیف‌ها را دوباره می‌گردد.
    retry_partial: bool = False
    limit: int = 0
    max_titles_per_session: int = 0
    max_pages_per_session: int = 0
    max_sources_per_title: int = 5
    min_title_similarity: float = 0.72
    search_enabled: bool = True
    search_url_template: str = "{base}/?s={query}"
    max_results_per_site: int = 8
    batch_rows: int = 100
    cache_ttl_days: int = 30
    separator: str = "، "
    summary: dict = field(default_factory=dict)
    pages: dict = field(default_factory=dict)
    max_categories: int = 3
    max_tags: int = 5
    use_suggest_keyword: bool = True
    labels: dict = field(default_factory=dict)
    taxonomy: dict = field(default_factory=dict)
    combine_author_scripts: bool = True

    @property
    def writable(self) -> tuple[str, ...]:
        never = set(self.sheet.never_write)
        return tuple(name for name in FILLABLE if name not in never)


DEFAULT_LABELS = {
    "iranian": "ایرانی",
    "foreign": "خارجی",
    "pdf": "پی دی اف",
    "audio": "صوتی",
}


def options_from_config(config: Config, overrides: dict | None = None) -> DetailOptions:
    """``config.yaml`` + تنظیمات پنل → :class:`DetailOptions`.

    تنظیمات پنل روی config را می‌پوشانند (نه برعکس)، چون کاربر همین حالا در
    پنل نشسته و همان چیزی که آنجا می‌بیند باید اجرا شود.
    """
    raw = dict(config.get("details", {}) or {})
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            merged = dict(raw[key])
            merged.update(value)
            raw[key] = merged
        elif value not in (None, ""):
            raw[key] = value

    labels = dict(DEFAULT_LABELS)
    labels.update(raw.get("labels", {}) or {})
    return DetailOptions(
        sheet=gsheet.SheetSettings.from_mapping(raw),
        image=images.ImageRules.from_mapping(raw.get("image")),
        overwrite=str(raw.get("overwrite", "empty") or "empty"),
        retry_partial=bool(raw.get("retry_partial", False)),
        limit=int(raw.get("limit", 0) or 0),
        max_titles_per_session=int(raw.get("max_titles_per_session", 0) or 0),
        max_pages_per_session=int(raw.get("max_pages_per_session", 0) or 0),
        max_sources_per_title=max(1, int(raw.get("max_sources_per_title", 5) or 5)),
        min_title_similarity=float(raw.get("min_title_similarity", 0.72) or 0.72),
        search_enabled=bool((raw.get("search", {}) or {}).get("enabled", True)),
        search_url_template=str(
            (raw.get("search", {}) or {}).get("url_template", "{base}/?s={query}")
        ),
        max_results_per_site=int((raw.get("search", {}) or {}).get("max_results_per_site", 8)),
        batch_rows=max(1, int(raw.get("batch_rows", 100) or 100)),
        cache_ttl_days=int(raw.get("cache_ttl_days", 30) or 30),
        separator=str(raw.get("multi_select_separator", "، ")),
        summary=dict(raw.get("summary", {}) or {}),
        pages=dict(raw.get("pages", {}) or {}),
        max_categories=int(raw.get("max_categories", 3) or 3),
        max_tags=int(raw.get("max_tags", 5) or 5),
        use_suggest_keyword=bool(raw.get("use_suggest_keyword", True)),
        labels=labels,
        taxonomy=dict(raw.get("taxonomy", {}) or {}),
        combine_author_scripts=bool(raw.get("combine_author_scripts", True)),
    )


# ---------------------------------------------------------------------------
# آمار
# ---------------------------------------------------------------------------


@dataclass
class DetailStats:
    rows: int = 0
    queued: int = 0
    processed: int = 0
    completed: int = 0
    partial: int = 0
    no_source: int = 0
    pages_fetched: int = 0
    from_cache: int = 0
    cells_written: int = 0
    sheet_url: str = ""
    output_path: str = ""
    report: str = ""
    stopped_early: str = ""
    filled: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        parts = [
            f"فاز ۵ — ردیف‌های شیت: {self.rows}، در صف: {self.queued}، "
            f"پردازش‌شده: {self.processed}، کامل: {self.completed}، "
            f"ناقص: {self.partial}، بدون منبع: {self.no_source}"
        ]
        if self.filled:
            names = {
                "keyword": "کلمه کلیدی",
                "author": "نویسنده",
                "summary": "خلاصه",
                "categories": "دسته",
                "tags": "تگ",
                "nationality": "ملیت",
                "book_format": "فرمت",
                "translator": "مترجم",
                "pages": "صفحات",
                "image": "تصویر",
            }
            filled = "، ".join(
                f"{names.get(key, key)}: {value}" for key, value in self.filled.items() if value
            )
            parts.append(f"  ستون‌های پرشده — {filled}")
        parts.append(
            f"  صفحات منبع: {self.pages_fetched} دانلود / {self.from_cache} از کش، "
            f"سلول نوشته‌شده: {self.cells_written}"
        )
        if self.sheet_url:
            parts.append(f"  گوگل‌شیت: {self.sheet_url}")
        if self.output_path:
            parts.append(f"  فایل خروجی: {self.output_path}")
        if self.report:
            parts.append(f"  گزارش: {self.report}")
        if self.stopped_early:
            parts.append(f"  ⏹ {self.stopped_early}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# پیدا کردن صفحه‌های یک عنوان
# ---------------------------------------------------------------------------


def known_sources(conn: sqlite3.Connection, run_id: str, title_key: str) -> list[str]:
    """آدرس‌هایی که فازهای قبلی برای همین عنوان ذخیره کرده‌اند.

    عنوان‌های شیت معمولاً از همین ابزار درآمده‌اند، پس اغلبِ ردیف‌ها بدون
    حتی یک جستجو منبع دارند — سریع‌ترین و مؤدبانه‌ترین مسیر ممکن.
    """
    if not title_key:
        return []
    rows = conn.execute(
        """SELECT source_url, run_id FROM raw_products
           WHERE normalized_title=? ORDER BY (run_id=?) DESC, id""",
        (title_key, run_id),
    ).fetchall()
    out: list[str] = []
    for row in rows:
        url = row["source_url"]
        if url and url not in out:
            out.append(url)
    return out


def search_sources(
    site: SiteConfig,
    query: str,
    title_key: str,
    fetcher: Fetcher,
    options: DetailOptions,
    norm_config: normalizer.NormalizerConfig,
) -> list[str]:
    """جستجو در خودِ سایت منبع و برداشتن لینک‌هایی که عنوانشان می‌خورد.

    از موتور جستجوی خارجی استفاده نمی‌کنیم: هم هزینه/بلاک دارد و هم لازم
    نیست. تقریباً همه‌ی فروشگاه‌های فارسی وردپرس‌اند و ``/?s=`` را دارند.
    متن لینک نتایج برای تطبیق کافی است، پس صفحه‌های بی‌ربط دانلود نمی‌شوند.
    """
    if not query:
        return []
    url = options.search_url_template.format(
        base=site.base_url.rstrip("/"), query=urllib.parse.quote(query)
    )
    result = fetcher.fetch(url, force_browser=site.js)
    if not result.ok:
        return []
    include = [re.compile(pattern) for pattern in site.product_url_include]
    exclude = [re.compile(pattern) for pattern in site.product_url_exclude]
    scored: list[tuple[float, str]] = []
    for link, text in extract.extract_anchors(result.html, result.final_url or url):
        if not extract.same_domain(link, site.domain):
            continue
        if any(pattern in link for pattern in ("/tag/", "/category/", "?add-to-cart", "/page/")):
            continue
        if any(pattern.search(link) for pattern in exclude):
            continue
        if include and not any(pattern.search(link) for pattern in include):
            continue
        score = similarity.title_similarity(
            normalizer.normalize(text, norm_config), title_key
        )
        if score >= options.min_title_similarity:
            scored.append((score, link))
    scored.sort(key=lambda item: -item[0])
    return [link for _, link in scored[: options.max_results_per_site]]


def page_details(
    conn: sqlite3.Connection,
    url: str,
    fetcher: Fetcher,
    site_name: str = "",
    ttl_days: int = 30,
) -> tuple[PageDetails | None, bool]:
    """دیتیل یک صفحه؛ ``(نتیجه، از_کش)``.

    خروجی استخراج کش می‌شود نه خودِ HTML: هم جا کمتر می‌گیرد و هم اجرای
    دوباره‌ی فاز روی ۱۵ هزار عنوان یک درخواست تازه به منابع نمی‌زند.
    """
    cached = db.get_page_payload(conn, url, ttl_days)
    if cached is not None:
        return PageDetails.from_dict(cached), True
    result = fetcher.fetch(url)
    if not result.ok:
        return None, False
    extracted = details.extract_details(result.html, result.final_url or url, site_name)
    db.put_page_payload(conn, url, extracted.domain, extracted.as_dict())
    return extracted, False


# ---------------------------------------------------------------------------
# ساخت مقدارهای یک ردیف
# ---------------------------------------------------------------------------


def _snap(value: str, allowed: Sequence[str]) -> str:
    """نزدیک‌ترین گزینه‌ی مجاز به یک برچسب («خارجی» → «رمان خارجی»).

    اگر کرکره‌ی شیت مقدار دیگری می‌خواهد، همان نوشته می‌شود؛ وگرنه مقدار
    خودمان. هدف این است که هیچ‌وقت مقداری خارج از کرکره در سلول ننشیند.
    """
    if not allowed:
        return value
    key = details.label_key(value)
    for option in allowed:
        if details.label_key(option) == key:
            return option
    for option in allowed:
        if key and key in details.label_key(option):
            return option
    return value


@dataclass
class Vocabularies:
    categories: taxonomy.Vocabulary
    tags: taxonomy.Vocabulary
    nationality: list[str] = field(default_factory=list)
    book_format: list[str] = field(default_factory=list)


def build_vocabularies(options: DetailOptions, lists: dict[str, list[str]]) -> Vocabularies:
    configured = options.taxonomy or {}
    return Vocabularies(
        categories=taxonomy.build(
            "categories",
            options=lists.get("categories") or configured.get("categories"),
            synonyms=configured.get("category_synonyms"),
            defaults=taxonomy.DEFAULT_CATEGORIES,
            max_values=options.max_categories,
        ),
        tags=taxonomy.build(
            "tags",
            options=lists.get("tags") or configured.get("tags"),
            synonyms=configured.get("tag_synonyms"),
            defaults=taxonomy.DEFAULT_TAGS,
            max_values=options.max_tags,
        ),
        nationality=lists.get("nationality") or configured.get("nationality") or [],
        book_format=lists.get("book_format") or configured.get("format") or [],
    )


def build_values(
    title: str,
    pages_of_sources: Sequence[PageDetails],
    options: DetailOptions,
    vocab: Vocabularies,
    norm_config: normalizer.NormalizerConfig,
    suggest_keywords: Sequence[str] = (),
    image_pick: images.ImagePick | None = None,
) -> tuple[dict[str, str], consensus.MergedDetails]:
    """همه‌ی صفحه‌های یک عنوان → مقدار هر ستون شیت."""
    merged = consensus.merge(
        pages_of_sources,
        summary_options=options.summary,
        pages_options=options.pages,
        combine_scripts=options.combine_author_scripts,
    )

    names = [name for name in (merged.author.value, merged.translator.value) if name]
    keyword = normalizer.main_keyword(title, norm_config, names=names)
    if options.use_suggest_keyword and suggest_keywords:
        keyword = _keyword_from_suggest(keyword, suggest_keywords, norm_config) or keyword

    summary_text = merged.summary.value
    matched_categories = vocab.categories.match(merged.terms, title=title, text=summary_text)
    matched_tags = vocab.tags.match(merged.terms, title=title, text=summary_text)

    nationality = ""
    if merged.nationality.filled:
        nationality = _snap(
            options.labels.get(merged.nationality.value, merged.nationality.value),
            vocab.nationality,
        )

    formats = [
        _snap(options.labels.get(code, code), vocab.book_format)
        for code in merged.book_format.value.split(" | ")
        if code
    ]

    values = {
        "keyword": keyword,
        "author": merged.author.value,
        "summary": summary_text,
        "categories": taxonomy.join_values(matched_categories, options.separator),
        "tags": taxonomy.join_values(matched_tags, options.separator),
        "nationality": nationality,
        "book_format": taxonomy.join_values(formats, options.separator),
        "translator": merged.translator.value,
        "pages": merged.pages.value,
        "image": image_pick.url if image_pick else "",
    }
    return values, merged


def _keyword_from_suggest(
    keyword: str, suggestions: Sequence[str], norm_config: normalizer.NormalizerConfig
) -> str:
    """اگر گوگل ساجست عبارت کوتاه‌تری از همین عنوان داشت، همان بهتر است.

    ساجست یعنی «مردم واقعاً این را سرچ کرده‌اند» — قوی‌ترین دلیلی که می‌شود
    برای انتخاب کلمه‌ی کلیدی داشت، و هزینه‌اش صفر است چون فاز ۳ قبلاً گرفته.
    """
    target = set(normalizer.meaningful_tokens(keyword, norm_config))
    if not target:
        return ""
    best = ""
    for suggestion in suggestions:
        tokens = set(normalizer.meaningful_tokens(suggestion, norm_config))
        if not tokens or not tokens <= target:
            continue  # چیزی بیرون از عنوان دارد → کلمه‌ی کلیدی همین محصول نیست
        if len(tokens) < 2:
            continue
        if not best or len(suggestion) > len(best):
            best = suggestion
    return best


# ---------------------------------------------------------------------------
# اجرا
# ---------------------------------------------------------------------------

REPORT_HEADER = (
    "ردیف",
    "عنوان",
    "وضعیت",
    "منابع",
    "نویسنده (رأی)",
    "صفحات (رأی)",
    "خلاصه (منبع)",
    "تصویر",
    "یادداشت",
)


def run(
    conn: sqlite3.Connection,
    run_id: str,
    config: Config,
    fetcher: Fetcher,
    options: DetailOptions | None = None,
    document: gsheet.Document | None = None,
    verbose: bool = True,
    should_stop: Callable[[], bool] | None = None,
    log: LogFn | None = None,
) -> DetailStats:
    options = options or options_from_config(config, db.get_setting(conn, "details", {}))
    say: LogFn = log or (print if verbose else (lambda _line: None))
    stop = should_stop or (lambda: False)
    norm_config = normalizer.config_from_mapping(config.normalizer)
    stats = DetailStats()

    document = document or gsheet.open_document(options.sheet)
    columns, sheet_rows = document.read()
    stats.rows = len(sheet_rows)
    say(f"شیت خوانده شد: {len(sheet_rows)} ردیف — ستون‌ها: {columns.describe()}")
    if columns.missing:
        say(f"⚠ این ستون‌ها در شیت پیدا نشدند و پر نمی‌شوند: {'، '.join(columns.missing)}")

    lists = taxonomy.options_from_lists_tab(document.read_tab(options.sheet.lists_tab))
    if lists:
        say(
            "گزینه‌های مجاز از تبِ لیست‌ها خوانده شد — "
            + "، ".join(f"{key}: {len(value)}" for key, value in lists.items())
        )
    vocab = build_vocabularies(options, lists)

    targets = [name for name in options.writable if columns.index(name) is not None]
    if not targets:
        say("⚠ هیچ ستون قابل نوشتنی پیدا نشد؛ فقط دیتابیس پر می‌شود.")

    recovered = _push_unwritten(conn, run_id, document, columns, targets, sheet_rows, options)
    if recovered:
        stats.cells_written += recovered
        say(f"{recovered} سلول از اجرای قبلی که در شیت نوشته نشده بود، نوشته شد.")

    queue: list[tuple[gsheet.SheetRow, int, list[str]]] = []
    with db.transaction(conn):
        for row in sheet_rows:
            title_key = normalizer.normalize(row.title, norm_config)
            detail_id = db.upsert_detail_row(conn, run_id, row.title, title_key, row.number)
            wanted = targets if options.overwrite == "always" else row.empty_fields(targets)
            if wanted and _needs_work(conn, run_id, title_key, options):
                queue.append((row, detail_id, wanted))
    stats.queued = len(queue)

    caps = [value for value in (options.limit, options.max_titles_per_session) if value > 0]
    cap = min(caps) if caps else 0
    if cap and cap < len(queue):
        say(f"سقف این نشست: {cap} عنوان از {len(queue)} عنوانِ در صف")
        queue = queue[:cap]

    sites = _sites_for(conn, run_id, config)
    pending_updates: list[gsheet.CellUpdate] = []
    pending_ids: list[int] = []
    report_rows: list[list[object]] = []

    for index, (row, detail_id, wanted) in enumerate(queue, start=1):
        if stop():
            stats.stopped_early = "به درخواست شما متوقف شد؛ بقیه‌ی ردیف‌ها در صف ماندند."
            break
        if options.max_pages_per_session and stats.pages_fetched >= options.max_pages_per_session:
            stats.stopped_early = (
                f"به سقف {options.max_pages_per_session} صفحه در این نشست رسیدیم؛"
                " دوباره اجرا کنید تا از همین‌جا ادامه دهد."
            )
            break

        title_key = normalizer.normalize(row.title, norm_config)
        sources, fetched, cached = _collect_pages(
            conn, run_id, row.title, title_key, sites, fetcher, options, norm_config
        )
        stats.pages_fetched += fetched
        stats.from_cache += cached
        stats.processed += 1

        if not sources:
            stats.no_source += 1
            with db.transaction(conn):
                db.save_detail_values(
                    conn, detail_id, {}, status=db.PARTIAL, note="هیچ صفحه‌ی منبعی پیدا نشد"
                )
            report_rows.append(
                [row.number, row.title, "بدون منبع", 0, "", "", "", "", "منبعی پیدا نشد"]
            )
            continue

        pick = _pick_image(sources, options, fetcher)
        values, merged = build_values(
            row.title,
            sources,
            options,
            vocab,
            norm_config,
            suggest_keywords=_suggest_keywords(conn, run_id, title_key),
            image_pick=pick,
        )

        evidence = merged.evidence()
        evidence["image"] = pick.as_dict()
        required = _required_fields(wanted, values, options)
        missing = [name for name in required if not values.get(name)]
        status = db.DONE if not missing else db.PARTIAL
        note = "" if not missing else "پر نشد: " + "، ".join(missing)
        with db.transaction(conn):
            db.save_detail_values(
                conn,
                detail_id,
                values,
                sources=[page.url for page in sources],
                evidence=evidence,
                status=status,
                note=note,
            )
        stats.completed += int(status == db.DONE)
        stats.partial += int(status == db.PARTIAL)

        for name in wanted:
            value = values.get(name, "")
            column = columns.index(name)
            if not value or column is None:
                continue
            pending_updates.append(gsheet.CellUpdate(row.number, column, value))
            stats.filled[name] = stats.filled.get(name, 0) + 1
        pending_ids.append(detail_id)

        report_rows.append(
            [
                row.number,
                row.title,
                "کامل" if status == db.DONE else "ناقص",
                len(sources),
                f"{merged.author.value} ({merged.author.votes})" if merged.author.filled else "",
                f"{merged.pages.value} ({merged.pages.votes})" if merged.pages.filled else "",
                merged.summary.votes,
                pick.note if pick.filled else "",
                "؛ ".join(
                    part
                    for part in (note, merged.pages.note, "" if pick.filled else pick.note)
                    if part
                ),
            ]
        )

        if len(pending_ids) >= options.batch_rows:
            stats.cells_written += _flush(conn, document, pending_updates, pending_ids)
            pending_updates, pending_ids = [], []
        if verbose and index % 10 == 0:
            say(f"  {index}/{len(queue)} — نوشته‌شده: {stats.cells_written} سلول")

    stats.cells_written += _flush(conn, document, pending_updates, pending_ids)

    if options.sheet.report_tab and report_rows:
        try:
            stats.report = document.write_report(
                options.sheet.report_tab, REPORT_HEADER, report_rows
            )
        except Exception as exc:  # noqa: BLE001 — گزارش نباید کل فاز را بیندازد
            say(f"⚠ نوشتن تبِ گزارش نشد: {exc}")

    closed = document.close()
    if isinstance(document, gsheet.GoogleSheetDocument):
        stats.sheet_url = closed or ""
    else:
        stats.output_path = closed or ""
    return stats


def _push_unwritten(
    conn: sqlite3.Connection,
    run_id: str,
    document: gsheet.Document,
    columns: gsheet.ColumnMap,
    targets: Sequence[str],
    sheet_rows: Sequence[gsheet.SheetRow],
    options: DetailOptions,
) -> int:
    """ردیف‌هایی که در دیتابیس کامل شده‌اند ولی در شیت ننشسته‌اند.

    اگر اجرای قبلی وسط نوشتن قطع شده باشد (اینترنت، بسته شدن پنل، سهمیه‌ی
    گوگل)، مقدارها در دیتابیس هستند و ردیف «انجام‌شده» علامت خورده — بدون این
    مرحله دیگر هیچ‌وقت به شیت نمی‌رسیدند.
    """
    pending = db.unpushed_detail_rows(conn, run_id)
    if not pending or not targets:
        return 0
    by_number = {row.number: row for row in sheet_rows}
    updates: list[gsheet.CellUpdate] = []
    ids: list[int] = []
    for record in pending:
        sheet_row = by_number.get(int(record["row_number"] or 0))
        if sheet_row is None:
            continue
        allowed = targets if options.overwrite == "always" else sheet_row.empty_fields(targets)
        for name in allowed:
            column = columns.index(name)
            value = record[name] or ""
            if column is not None and value:
                updates.append(gsheet.CellUpdate(sheet_row.number, column, str(value)))
        ids.append(int(record["id"]))
    return _flush(conn, document, updates, ids)


def _needs_work(
    conn: sqlite3.Connection, run_id: str, title_key: str, options: DetailOptions
) -> bool:
    """آیا این ردیف باید (دوباره) پردازش شود؟

    ستون خالی به‌تنهایی دلیل کافی نیست: «مترجم» یک رمان ایرانی همیشه خالی
    می‌ماند و اگر ملاک فقط خالی بودن سلول باشد، هر اجرا همان ردیف‌ها را از نو
    می‌گردد. پس ردیفی که یک‌بار پردازش شده فقط با ``overwrite: always``،
    ``retry_partial`` یا دکمه‌ی «صف را از نو بساز» دوباره می‌آید.
    """
    if options.overwrite == "always":
        return True
    row = db.detail_row_by_key(conn, run_id, title_key)
    if row is None:
        return True
    status = row["status"] or db.PENDING
    if status == db.PENDING:
        return True
    return status == db.PARTIAL and options.retry_partial


def _required_fields(
    wanted: Sequence[str], values: dict[str, str], options: DetailOptions
) -> list[str]:
    """ستون‌هایی که خالی ماندنشان واقعاً «ناقص» است.

    مترجم فقط برای رمان خارجی انتظار می‌رود؛ خالی بودنش برای رمان ایرانی
    نقص نیست و نباید ردیف را «ناقص» علامت بزند.
    """
    foreign = options.labels.get("foreign", "خارجی")
    return [
        name
        for name in wanted
        if not (name == "translator" and values.get("nationality") != foreign)
    ]


def _flush(
    conn: sqlite3.Connection,
    document: gsheet.Document,
    updates: Sequence[gsheet.CellUpdate],
    ids: Sequence[int],
) -> int:
    if not updates and not ids:
        return 0
    written = document.apply(list(updates))
    db.mark_detail_pushed(conn, list(ids))
    return written


def _sites_for(conn: sqlite3.Connection, run_id: str, config: Config) -> list[SiteConfig]:
    from . import p1_crawl

    try:
        return list(p1_crawl.sites_for_run(conn, run_id, config))
    except Exception:  # pragma: no cover - config ناقص
        return list(config.sites)


def _collect_pages(
    conn: sqlite3.Connection,
    run_id: str,
    title: str,
    title_key: str,
    sites: Sequence[SiteConfig],
    fetcher: Fetcher,
    options: DetailOptions,
    norm_config: normalizer.NormalizerConfig,
) -> tuple[list[PageDetails], int, int]:
    """صفحه‌های همان محصول در منابع → ``(دیتیل‌ها، دانلود، از کش)``."""
    urls = known_sources(conn, run_id, title_key)[: options.max_sources_per_title]
    collected: list[PageDetails] = []
    fetched = cached = 0
    site_by_domain = {site.domain: site for site in sites}

    def take(url: str) -> bool:
        nonlocal fetched, cached
        domain = urllib.parse.urlsplit(url).netloc.lower()
        site = site_by_domain.get(domain)
        page, from_cache = page_details(
            conn, url, fetcher, site.site_name if site else "", options.cache_ttl_days
        )
        if from_cache:
            cached += 1
        else:
            fetched += 1
        if page is None or page.is_empty:
            return False
        if page.title and title_key:
            score = similarity.title_similarity(
                normalizer.normalize(page.title, norm_config), title_key
            )
            if score < options.min_title_similarity:
                return False  # صفحه‌ی محصول دیگری است؛ دیتیلش به این ردیف نمی‌خورد
        collected.append(page)
        return True

    for url in urls:
        take(url)
        if len(collected) >= options.max_sources_per_title:
            break

    if len(collected) < options.max_sources_per_title and options.search_enabled:
        query = normalizer.main_keyword(title, norm_config)
        for site in sites:
            if len(collected) >= options.max_sources_per_title:
                break
            for url in search_sources(site, query, title_key, fetcher, options, norm_config):
                if url in urls:
                    continue
                take(url)
                if len(collected) >= options.max_sources_per_title:
                    break
    return collected, fetched, cached


def _pick_image(
    sources: Sequence[PageDetails], options: DetailOptions, fetcher: Fetcher
) -> images.ImagePick:
    candidates = [
        (candidate, page.domain) for page in sources for candidate in page.images
    ]
    if not candidates:
        return images.ImagePick(note="هیچ تصویری در صفحه‌های منبع نبود")
    return images.pick_image(candidates, options.image, fetch=fetcher.fetch_bytes)


def _suggest_keywords(conn: sqlite3.Connection, run_id: str, title_key: str) -> list[str]:
    """کلمات ساجستِ فاز ۳ برای همین عنوان (اگر گرفته شده باشد)."""
    if not title_key:
        return []
    rows = conn.execute(
        """SELECT DISTINCT k.keyword, k.position FROM lsi_keywords k
           JOIN product_mapping m ON m.canonical_id = k.canonical_id
           JOIN raw_products r     ON r.id = m.raw_id
           WHERE r.run_id=? AND r.normalized_title=?
           ORDER BY k.position LIMIT 20""",
        (run_id, title_key),
    ).fetchall()
    return [row["keyword"] for row in rows]
