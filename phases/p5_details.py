"""فاز ۵ — تکمیل ستون‌های گوگل‌شیت محصولات.

ورودی: فهرست عنوان‌های شما (ستون اول شیت، یا خروجی فازهای قبلی).
خروجی: همان شیت، با ستون‌هایش پرشده و آماده‌ی تحویل به اسکریپت درج محصول.

**کدام ستون‌ها؟ هرکدام که در شیت باشند.** این فاز فهرست ثابتی از فیلدها ندارد:
شیت رمان «خلاصه» و «مترجم» دارد، شیت نمونه‌سوال «تعداد سوالات» و «کد رایانه»،
شیت طرح توجیهی چیز دیگر. عنوان هر ستون هم برچسبِ جستجو در منابع است و هم
نوع فیلد را تعیین می‌کند (:mod:`core.fields`).

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
    fields,
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

    def writable(self, plan: fields.FieldPlan) -> list[fields.FieldSpec]:
        """ستون‌های قابل نوشتنِ همین شیت (نه یک فهرست ثابت در کد)."""
        return plan.writable(self.sheet.never_write)


#: نام فارسی نوع ستون‌ها — فقط برای لاگ و پنل
KIND_LABELS = {
    fields.KIND_TITLE: "عنوان",
    fields.KIND_KEYWORD: "کلمه کلیدی",
    fields.KIND_PERSON: "شخص",
    fields.KIND_NUMBER: "عدد",
    fields.KIND_TEXT: "متن",
    fields.KIND_SUMMARY: "خلاصه",
    fields.KIND_CHOICE: "کرکره",
    fields.KIND_MULTI: "کرکره چندتایی",
    fields.KIND_BOOL: "دارد/ندارد",
    fields.KIND_IMAGE: "تصویر",
    fields.KIND_SKIP: "دست نمی‌خورد",
}

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
            # کلیدها همان عنوان ستون‌های شیت خودتان‌اند
            filled = "، ".join(
                f"{key}: {value}" for key, value in self.filled.items() if value
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


#: واژگان پیش‌فرض برای ستون‌های شناخته‌شده (اگر تبِ «لیست‌ها» چیزی نداشت)
DEFAULT_VOCABULARIES = {
    "categories": taxonomy.DEFAULT_CATEGORIES,
    "tags": taxonomy.DEFAULT_TAGS,
}


def vocabulary_for(spec: fields.FieldSpec, options: DetailOptions) -> taxonomy.Vocabulary:
    """واژگان یک ستون کرکره‌ای.

    گزینه‌ها از تبِ «لیست‌ها»ی خود شیت می‌آیند (در ``spec.options`` نشسته‌اند).
    فهرست پیش‌فرض فقط برای ستون‌های شناخته‌شده و وقتی کاربر لیستی نداده است.
    """
    configured = options.taxonomy or {}
    return taxonomy.build(
        spec.column or spec.key,
        options=spec.options or configured.get(spec.key),
        synonyms=spec.synonyms or configured.get(f"{spec.key}_synonyms"),
        defaults=DEFAULT_VOCABULARIES.get(spec.key, {}),
        max_values=spec.max_values,
    )


def page_values(page: PageDetails, spec: fields.FieldSpec) -> list[str]:
    """مقدارهای یک صفحه برای یک ستون — از روی برچسب‌های همان ستون."""
    return page.values_for(spec.search_labels())


def decide(
    spec: fields.FieldSpec,
    title: str,
    sources: Sequence[PageDetails],
    common: consensus.MergedDetails,
    options: DetailOptions,
    norm_config: normalizer.NormalizerConfig,
    suggest_keywords: Sequence[str] = (),
    image_pick: images.ImagePick | None = None,
) -> consensus.Decision:
    """مقدار یک ستون از روی همه‌ی منابع — بر اساس **نوع** ستون، نه اسمش.

    ستون‌های شناخته‌شده (نویسنده، خلاصه، ملیت، فرمت) شاهدهای اضافه‌ای دارند
    که در :mod:`core.consensus` جمع شده‌اند؛ بقیه‌ی ستون‌ها — هر چه باشند —
    از همین مسیر عمومی پر می‌شوند: برچسبِ هم‌نامِ ستون در منابع، بعد رأی‌گیری.
    """
    kind = spec.kind

    if kind == fields.KIND_KEYWORD:
        names = [name for name in (common.author.value, common.translator.value) if name]
        keyword = normalizer.main_keyword(title, norm_config, names=names)
        if options.use_suggest_keyword and suggest_keywords:
            keyword = _keyword_from_suggest(keyword, suggest_keywords, norm_config) or keyword
        return consensus.Decision(value=keyword, votes=1, sources=len(sources))

    if kind == fields.KIND_IMAGE:
        pick = image_pick or images.ImagePick()
        return consensus.Decision(
            value=pick.url, votes=pick.domains, sources=len(sources), note=pick.note
        )

    if kind == fields.KIND_PERSON:
        if spec.key == "author" and common.author.filled:
            return common.author
        if spec.key == "translator" and common.translator.filled:
            return common.translator
        return consensus.merge_persons(
            [
                [name for value in page_values(page, spec) for name in details.clean_persons(value)]
                for page in sources
            ],
            combine_scripts=options.combine_author_scripts,
        )

    if kind == fields.KIND_NUMBER:
        if spec.key == "pages":
            return common.pages
        per_source = [_numbers_of(page_values(page, spec), spec) for page in sources]
        return consensus.merge_numbers(
            per_source,
            tolerance=spec.tolerance,
            min_agreement=int((options.pages or {}).get("min_agreement", 2)),
            accept_single=bool((options.pages or {}).get("accept_single", True)),
        )

    if kind == fields.KIND_SUMMARY:
        if spec.key == "summary" and common.summary.filled:
            return common.summary
        blocks = [
            [
                *page.summaries,
                *[details.SummaryBlock(value, 1) for value in page_values(page, spec)],
            ]
            for page in sources
        ]
        return consensus.merge_summary(
            blocks,
            max_chars=int(options.summary.get("max_chars", 1200)),
            min_chars=int(options.summary.get("min_chars", 200)),
            max_sentences=int(options.summary.get("max_sentences", 14)),
        )

    if kind == fields.KIND_BOOL:
        per_source = [page_values(page, spec) or _bool_probe(page, spec) for page in sources]
        return consensus.merge_flag(
            per_source,
            true_label=_snap(spec.true_label, spec.options),
            false_label=_snap(spec.false_label, spec.options),
            extra_true=spec.true_hints,
        )

    if kind in (fields.KIND_CHOICE, fields.KIND_MULTI):
        return _decide_choice(spec, title, sources, common, options)

    # KIND_TEXT و هر چیز دیگر: مقدار متنی کوتاه با رأی‌گیری
    decision = consensus.merge_text([page_values(page, spec) for page in sources])
    if decision.filled and _is_code(decision.value):
        # کد رایانه و شناسه در سیستم‌های دیگر کپی می‌شوند؛ رقم فارسی آنجا
        # مقدار دیگری است، پس همیشه شکل لاتین نوشته می‌شود.
        decision.value = normalizer.latin_digits(decision.value)
    return decision


def _decide_choice(
    spec: fields.FieldSpec,
    title: str,
    sources: Sequence[PageDetails],
    common: consensus.MergedDetails,
    options: DetailOptions,
) -> consensus.Decision:
    """ستون‌های کرکره‌ای: فقط گزینه‌های مجاز، چندتایی یا تکی."""
    vocabulary = vocabulary_for(spec, options)

    if spec.key == "nationality":
        decision = common.nationality
        if not decision.filled:
            return decision
        label = options.labels.get(decision.value, decision.value)
        return consensus.Decision(
            value=_snap(label, vocabulary.options or spec.options),
            votes=decision.votes,
            sources=decision.sources,
            note=decision.note,
        )

    if spec.key == "book_format":
        labels = [
            _snap(options.labels.get(code, code), vocabulary.options or spec.options)
            for code in common.book_format.value.split(" | ")
            if code
        ]
        return consensus.Decision(
            value=taxonomy.join_values(labels, options.separator),
            votes=common.book_format.votes,
            sources=common.book_format.sources,
        )

    # برچسب‌های خام: تگ/دسته‌ی منابع + مقدار همان ستون در جدول مشخصات منابع
    terms = list(common.terms)
    for page in sources:
        for value in page_values(page, spec):
            for piece in taxonomy.split_values(value):
                if piece not in terms:
                    terms.append(piece)

    matched = vocabulary.match(terms, title=title, text=common.summary.value)
    if not matched and not spec.options:
        # این ستون کرکره‌ای است ولی کاربر فهرستی برایش نداده و فهرست پیش‌فرض
        # هم (که مالِ رمان است) چیزی نگرفت. تنها حقیقتِ در دسترس، برچسب خودِ
        # منابع است: همان را می‌نویسیم تا ستون خالی نماند.
        matched = [term for term in terms if term][: spec.max_values]
    if spec.kind == fields.KIND_CHOICE:
        matched = matched[:1]
    return consensus.Decision(
        value=taxonomy.join_values(matched, options.separator),
        votes=len(matched),
        sources=len(sources),
    )


def _is_code(value: str) -> bool:
    """مقداری که فقط رقم (و جداکننده) است — شناسه، نه متن."""
    stripped = re.sub(r"[\s\-_/]", "", normalizer.latin_digits(value))
    return bool(stripped) and stripped.isdigit()


def _numbers_of(values: Sequence[str], spec: fields.FieldSpec) -> list[int]:
    """مقدارهای متنی یک ستون عددی → عددهای معقول همان ستون."""
    out: list[int] = []
    for value in values:
        for number in re.findall(r"\d{1,6}", normalizer.latin_digits(str(value))):
            candidate = int(number)
            if spec.min_value <= candidate <= spec.max_value and candidate not in out:
                out.append(candidate)
    return out


def _bool_probe(page: PageDetails, spec: fields.FieldSpec) -> list[str]:
    """وقتی جدول مشخصات چیزی نگفته، عنوان و برچسب‌های خودِ محصول را نگاه کن.

    «همراه با جزوه» در عنوان محصول یا تگ‌هایش، همان‌قدر شاهد است که یک ردیف
    جدول. متن کامل صفحه عمداً گشته نمی‌شود؛ آنجا منوی سایت هم هست.
    """
    haystack = details.label_key(" ".join([page.title, *page.tags, *page.categories]))
    words = [spec.column, *spec.labels, *spec.true_hints]
    return [spec.true_label] if any(details.label_key(w) in haystack for w in words if w) else []


def build_values(
    title: str,
    pages_of_sources: Sequence[PageDetails],
    options: DetailOptions,
    specs: Sequence[fields.FieldSpec],
    norm_config: normalizer.NormalizerConfig,
    suggest_keywords: Sequence[str] = (),
    image_pick: images.ImagePick | None = None,
) -> tuple[dict[str, str], dict[str, dict], consensus.MergedDetails]:
    """همه‌ی صفحه‌های یک عنوان → مقدار هر ستونِ شیت.

    خروجی سوم (:class:`~core.consensus.MergedDetails`) برای گزارش است: همان
    شاهدهایی که تصمیم‌ها را ساختند.
    """
    common = consensus.merge(
        pages_of_sources,
        summary_options=options.summary,
        pages_options=options.pages,
        combine_scripts=options.combine_author_scripts,
    )
    values: dict[str, str] = {}
    evidence: dict[str, dict] = {}
    for spec in specs:
        decision = decide(
            spec,
            title,
            pages_of_sources,
            common,
            options,
            norm_config,
            suggest_keywords=suggest_keywords,
            image_pick=image_pick,
        )
        values[spec.key] = decision.value
        evidence[spec.key] = {"column": spec.column, "kind": spec.kind, **decision.as_dict()}
    return values, evidence, common


# ---------------------------------------------------------------------------
# اجرا
# ---------------------------------------------------------------------------

#: ستون‌های ثابت گزارش؛ بقیه‌ی ستون‌ها از روی نقشه‌ی همان شیت ساخته می‌شوند
REPORT_LEAD = ("ردیف", "عنوان", "وضعیت", "منابع")
REPORT_TAIL = ("یادداشت",)


def report_header(specs: Sequence[fields.FieldSpec]) -> list[str]:
    """سرستون‌های گزارش: برای هر ستون شیت، مقدار و تعداد رأیش."""
    return [*REPORT_LEAD, *[f"{spec.column} (رأی)" for spec in specs], *REPORT_TAIL]


def report_row(
    row_number: int,
    title: str,
    status: str,
    sources: int,
    specs: Sequence[fields.FieldSpec],
    values: dict[str, str],
    evidence: dict[str, dict],
    note: str,
) -> list[object]:
    cells: list[object] = [row_number, title, status, sources]
    for spec in specs:
        value = values.get(spec.key, "")
        votes = (evidence.get(spec.key) or {}).get("votes", 0)
        short = value if len(str(value)) <= 60 else f"{str(value)[:60]}…"
        cells.append(f"{short} ({votes})" if value else "")
    notes = [note]
    for spec in specs:
        extra = (evidence.get(spec.key) or {}).get("note", "")
        if extra:
            notes.append(f"{spec.column}: {extra}")
    cells.append("؛ ".join(part for part in notes if part))
    return cells


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

    # اول لیست‌ها، بعد نقشه‌ی ستون‌ها: ستونی که در تبِ «لیست‌ها» فهرست دارد،
    # کرکره‌ای شمرده می‌شود و فقط از همان فهرست پر خواهد شد.
    lists = taxonomy.options_from_lists_tab(document.read_tab(options.sheet.lists_tab))
    if lists:
        say(
            "گزینه‌های مجاز از تبِ لیست‌ها خوانده شد — "
            + "، ".join(f"{key}: {len(value)}" for key, value in lists.items() if key)
        )

    grid = document.read_grid()
    plan = gsheet.plan_from_grid(grid, options.sheet, lists)
    sheet_rows = gsheet.rows_from_grid(grid, plan, options.sheet.header_row)
    stats.rows = len(sheet_rows)
    say(f"شیت خوانده شد: {len(sheet_rows)} ردیف")
    say("ستون‌ها — " + "، ".join(f"{col}: {KIND_LABELS.get(kind, kind)}" for col, kind in plan.describe().items()))

    targets_specs = options.writable(plan)
    targets = [spec.key for spec in targets_specs]
    columns = plan.columns
    # نقشه‌ی همین شیت ذخیره می‌شود تا خروجی محلی و پنل، ستون‌های واقعی همین
    # موضوع را نشان بدهند نه فهرست ثابتِ رمان.
    db.set_setting(
        conn,
        f"detail_plan:{run_id}",
        [
            {"column": spec.column, "key": spec.key, "kind": spec.kind}
            for spec in plan.specs
        ],
    )
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
                report_row(
                    row.number, row.title, "بدون منبع", 0, targets_specs, {}, {}, "منبعی پیدا نشد"
                )
            )
            continue

        pick = _pick_image(sources, options, fetcher)
        values, evidence, merged = build_values(
            row.title,
            sources,
            options,
            targets_specs,
            norm_config,
            suggest_keywords=_suggest_keywords(conn, run_id, title_key),
            image_pick=pick,
        )
        evidence["_منابع"] = {"value": len(sources), "votes": 0, "sources": len(sources), "note": ""}
        required = _required_fields(targets_specs, wanted, values, options)
        missing = [name for name in required if not values.get(name)]
        status = db.DONE if not missing else db.PARTIAL
        titles = {spec.key: spec.column for spec in targets_specs}
        note = "" if not missing else "پر نشد: " + "، ".join(titles.get(n, n) for n in missing)
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
            column = columns.get(name)
            if not value or column is None:
                continue
            pending_updates.append(gsheet.CellUpdate(row.number, column, value))
            label = titles.get(name, name)
            stats.filled[label] = stats.filled.get(label, 0) + 1
        pending_ids.append(detail_id)

        report_rows.append(
            report_row(
                row.number,
                row.title,
                "کامل" if status == db.DONE else "ناقص",
                len(sources),
                targets_specs,
                values,
                evidence,
                note,
            )
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
                options.sheet.report_tab, report_header(targets_specs), report_rows
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
    columns: dict[str, int],
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
        stored = db.detail_values(record)
        for name in allowed:
            column = columns.get(name)
            value = stored.get(name, "")
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
    specs: Sequence[fields.FieldSpec],
    wanted: Sequence[str],
    values: dict[str, str],
    options: DetailOptions,
) -> list[str]:
    """ستون‌هایی که خالی ماندنشان واقعاً «ناقص» است.

    دو چیز تعیینش می‌کند: ``required`` خودِ ستون (ستون ناشناخته پیش‌فرض
    اختیاری است، چون نمی‌دانیم منابع اصلاً چنین چیزی می‌نویسند یا نه)، و یک
    قاعده‌ی وابسته: مترجم فقط برای اثر خارجی انتظار می‌رود.
    """
    foreign = options.labels.get("foreign", "خارجی")
    required = {spec.key for spec in specs if spec.required}
    return [
        name
        for name in wanted
        if name in required
        and not (name == "translator" and values.get("nationality") != foreign)
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
