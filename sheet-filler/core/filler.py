"""موتور تکمیل شیت — قلب این برنامه.

برای هر عنوانِ شیت::

    عنوان → پیدا کردن صفحه‌های همان محصول      (core.sources)
          → استخراج برچسب‌های هر صفحه           (core.details)
          → رأی‌گیری بین منابع                   (core.consensus)
          → نگاشت به کرکره‌های شیت               (core.taxonomy)
          → انتخاب کاور مربعِ بی‌واترمارک          (core.images)
          → نوشتن فقط در سلول‌های خالیِ هدف       (core.gsheet)

**کدام ستون‌ها؟ هرکدام که در شیت باشند.** فهرست ثابتی از فیلدها در کد نیست:
عنوان هر ستون هم برچسبِ جستجو در منابع است و هم نوع فیلد را تعیین می‌کند
(:mod:`core.fields`). پس یک نصب، هم شیت رمان را پر می‌کند هم شیت نمونه‌سوال
و جزوه و طرح توجیهی را.

سه ضمانتی که روی ۱۵ هزار ردیف اهمیت دارند:

* **از سر گرفتنی است.** هر ردیف بلافاصله در دیتابیس می‌نشیند و صفحه‌های
  دانلودشده کش می‌شوند؛ اجرای بعدی از همان‌جا ادامه می‌دهد.
* **سقف نشست دارد** تا یک اجرای بی‌نظارتِ چندساعته راه نیفتد.
* **ستون‌های «وضعیت» و «شناسه محصول» دست نمی‌خورند**؛ آن‌ها مالِ اسکریپت درج
  محصول‌اند.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Sequence

from . import consensus, db, details, fields, gsheet, images, normalizer, sources, taxonomy
from .config import Config, SourceSite
from .details import PageDetails
from .http import Fetcher

LogFn = Callable[[str], None]

#: نام فارسی نوع ستون‌ها — برای لاگ و پنل
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

#: واژگان پیش‌فرض برای ستون‌های شناخته‌شده (اگر تبِ «لیست‌ها» چیزی نداشت)
DEFAULT_VOCABULARIES = {
    "categories": taxonomy.DEFAULT_CATEGORIES,
    "tags": taxonomy.DEFAULT_TAGS,
}


# ---------------------------------------------------------------------------
# تنظیمات اجرا
# ---------------------------------------------------------------------------


@dataclass
class FillOptions:
    """تنظیمات یک اجرا: config به‌علاوه‌ی چیزی که در پنل عوض شده."""

    sheet: gsheet.SheetSettings = field(default_factory=gsheet.SheetSettings)
    image: images.ImageRules = field(default_factory=images.ImageRules)
    sites: list[SourceSite] = field(default_factory=list)
    overwrite: str = "empty"
    retry_partial: bool = False
    limit: int = 0
    max_titles_per_session: int = 0
    max_pages_per_session: int = 0
    max_sources_per_title: int = 5
    min_title_similarity: float = 0.72
    search_enabled: bool = True
    max_results_per_site: int = 8
    batch_rows: int = 100
    cache_ttl_days: int = 30
    separator: str = "، "
    max_categories: int = 3
    max_tags: int = 5
    combine_author_scripts: bool = True
    labels: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    numbers: dict = field(default_factory=dict)
    taxonomy: dict = field(default_factory=dict)

    def writable(self, plan: fields.FieldPlan) -> list[fields.FieldSpec]:
        """ستون‌های قابل نوشتنِ همین شیت."""
        return plan.writable(self.sheet.never_write)

    @property
    def key(self) -> str:
        return db.sheet_key(self.sheet.sheet_id, self.sheet.tab, self.sheet.file)


def options_from_config(config: Config, overrides: dict | None = None) -> FillOptions:
    """``config.yaml`` + تنظیمات پنل → :class:`FillOptions`.

    تنظیمات پنل روی config سوار می‌شوند: کاربر همین حالا آنجا نشسته و همان
    چیزی که می‌بیند باید اجرا شود.
    """
    sheet_raw = dict(config.get("sheet", {}) or {})
    fill_raw = dict(config.get("fill", {}) or {})
    search_raw = dict(config.get("search", {}) or {})
    image_raw = dict(config.get("image", {}) or {})
    sites = list(config.sites)

    for key, value in (overrides or {}).items():
        if key in {"sheet", "fill", "search", "image"} and isinstance(value, dict):
            target = {"sheet": sheet_raw, "fill": fill_raw, "search": search_raw, "image": image_raw}[key]
            target.update({k: v for k, v in value.items() if v is not None})
        elif key == "sites" and isinstance(value, list):
            default_search = search_raw.get("url_template", "{base}/?s={query}")
            sites = [SourceSite.from_entry(entry, default_search) for entry in value if entry]
        elif key in sheet_raw or key in {"sheet_id", "service_account_json", "tab", "file"}:
            sheet_raw[key] = value
        elif value not in (None, ""):
            fill_raw[key] = value

    labels = dict(config.get("fill.labels", {}) or {})
    labels.update(fill_raw.get("labels", {}) or {})
    return FillOptions(
        sheet=gsheet.SheetSettings.from_mapping(sheet_raw),
        image=images.ImageRules.from_mapping(image_raw),
        sites=sites,
        overwrite=str(fill_raw.get("overwrite", "empty") or "empty"),
        retry_partial=bool(fill_raw.get("retry_partial", False)),
        limit=int(fill_raw.get("limit", 0) or 0),
        max_titles_per_session=int(fill_raw.get("max_titles_per_session", 0) or 0),
        max_pages_per_session=int(fill_raw.get("max_pages_per_session", 0) or 0),
        max_sources_per_title=max(1, int(fill_raw.get("max_sources_per_title", 5) or 5)),
        min_title_similarity=float(fill_raw.get("min_title_similarity", 0.72) or 0.72),
        search_enabled=bool(search_raw.get("enabled", True)),
        max_results_per_site=int(search_raw.get("max_results_per_site", 8) or 8),
        batch_rows=max(1, int(fill_raw.get("batch_rows", 100) or 100)),
        cache_ttl_days=int(fill_raw.get("cache_ttl_days", 30) or 30),
        separator=str(fill_raw.get("multi_select_separator", "، ")),
        max_categories=int(fill_raw.get("max_categories", 3) or 3),
        max_tags=int(fill_raw.get("max_tags", 5) or 5),
        combine_author_scripts=bool(fill_raw.get("combine_author_scripts", True)),
        labels=labels,
        summary=dict(fill_raw.get("summary", {}) or {}),
        numbers=dict(fill_raw.get("numbers", {}) or {}),
        taxonomy=dict(fill_raw.get("taxonomy", {}) or {}),
    )


# ---------------------------------------------------------------------------
# آمار
# ---------------------------------------------------------------------------


@dataclass
class FillStats:
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
            f"ردیف‌های شیت: {self.rows}، در صف: {self.queued}، "
            f"پردازش‌شده: {self.processed}، کامل: {self.completed}، "
            f"ناقص: {self.partial}، بدون منبع: {self.no_source}"
        ]
        if self.filled:
            parts.append(
                "  ستون‌های پرشده — "
                + "، ".join(f"{key}: {value}" for key, value in self.filled.items() if value)
            )
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
# تصمیم‌گیری برای هر ستون
# ---------------------------------------------------------------------------


def snap(value: str, allowed: Sequence[str]) -> str:
    """نزدیک‌ترین گزینه‌ی مجاز به یک برچسب («خارجی» → «رمان خارجی»).

    هدف: هیچ‌وقت مقداری خارج از کرکره‌ی شما در سلول ننشیند.
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


def vocabulary_for(spec: fields.FieldSpec, options: FillOptions) -> taxonomy.Vocabulary:
    """واژگان یک ستون کرکره‌ای — گزینه‌ها از تبِ «لیست‌ها»ی خود شیت."""
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
    pages: Sequence[PageDetails],
    common: consensus.MergedDetails,
    options: FillOptions,
    norm_config: normalizer.NormalizerConfig,
    image_pick: images.ImagePick | None = None,
) -> consensus.Decision:
    """مقدار یک ستون از روی همه‌ی منابع — بر اساس **نوع** ستون، نه اسمش."""
    kind = spec.kind

    if kind == fields.KIND_KEYWORD:
        names = [name for name in (common.author.value, common.translator.value) if name]
        return consensus.Decision(
            value=normalizer.main_keyword(title, norm_config, names=names),
            votes=1,
            sources=len(pages),
        )

    if kind == fields.KIND_IMAGE:
        pick = image_pick or images.ImagePick()
        return consensus.Decision(
            value=pick.url, votes=pick.domains, sources=len(pages), note=pick.note
        )

    if kind == fields.KIND_PERSON:
        if spec.key == "author" and common.author.filled:
            return common.author
        if spec.key == "translator" and common.translator.filled:
            return common.translator
        return consensus.merge_persons(
            [
                [name for value in page_values(page, spec) for name in details.clean_persons(value)]
                for page in pages
            ],
            combine_scripts=options.combine_author_scripts,
        )

    if kind == fields.KIND_NUMBER:
        if spec.key == "pages":
            return common.pages
        return consensus.merge_numbers(
            [_numbers_of(page_values(page, spec), spec) for page in pages],
            tolerance=spec.tolerance,
            min_agreement=int(options.numbers.get("min_agreement", 2)),
            accept_single=bool(options.numbers.get("accept_single", True)),
        )

    if kind == fields.KIND_SUMMARY:
        if spec.key == "summary" and common.summary.filled:
            return common.summary
        blocks = [
            [*page.summaries, *[details.SummaryBlock(v, 1) for v in page_values(page, spec)]]
            for page in pages
        ]
        return consensus.merge_summary(
            blocks,
            max_chars=int(options.summary.get("max_chars", 1200)),
            min_chars=int(options.summary.get("min_chars", 200)),
            max_sentences=int(options.summary.get("max_sentences", 14)),
        )

    if kind == fields.KIND_BOOL:
        return consensus.merge_flag(
            [page_values(page, spec) or _bool_probe(page, spec) for page in pages],
            true_label=snap(spec.true_label, spec.options),
            false_label=snap(spec.false_label, spec.options),
            extra_true=spec.true_hints,
        )

    if kind in (fields.KIND_CHOICE, fields.KIND_MULTI):
        return _decide_choice(spec, title, pages, common, options)

    decision = consensus.merge_text([page_values(page, spec) for page in pages])
    if decision.filled and _is_code(decision.value):
        # کد و شناسه در سیستم‌های دیگر کپی می‌شوند؛ رقم فارسی آنجا مقدار
        # دیگری است، پس همیشه شکل لاتین نوشته می‌شود.
        decision.value = normalizer.latin_digits(decision.value)
    return decision


def _decide_choice(
    spec: fields.FieldSpec,
    title: str,
    pages: Sequence[PageDetails],
    common: consensus.MergedDetails,
    options: FillOptions,
) -> consensus.Decision:
    vocabulary = vocabulary_for(spec, options)

    if spec.key == "nationality":
        decision = common.nationality
        if not decision.filled:
            return decision
        label = options.labels.get(decision.value, decision.value)
        return consensus.Decision(
            value=snap(label, vocabulary.options or spec.options),
            votes=decision.votes,
            sources=decision.sources,
            note=decision.note,
        )

    if spec.key == "book_format":
        labels = [
            snap(options.labels.get(code, code), vocabulary.options or spec.options)
            for code in common.book_format.value.split(" | ")
            if code
        ]
        return consensus.Decision(
            value=taxonomy.join_values(labels, options.separator),
            votes=common.book_format.votes,
            sources=common.book_format.sources,
        )

    terms = list(common.terms)
    for page in pages:
        for value in page_values(page, spec):
            for piece in taxonomy.split_values(value):
                if piece not in terms:
                    terms.append(piece)

    matched = vocabulary.match(terms, title=title, text=common.summary.value)
    if not matched and not spec.options:
        # کرکره‌ای بدون فهرست: تنها حقیقتِ در دسترس، برچسب خودِ منابع است
        matched = [term for term in terms if term][: spec.max_values]
    if spec.kind == fields.KIND_CHOICE:
        matched = matched[:1]
    return consensus.Decision(
        value=taxonomy.join_values(matched, options.separator),
        votes=len(matched),
        sources=len(pages),
    )


def _numbers_of(values: Sequence[str], spec: fields.FieldSpec) -> list[int]:
    out: list[int] = []
    for value in values:
        for number in re.findall(r"\d{1,6}", normalizer.latin_digits(str(value))):
            candidate = int(number)
            if spec.min_value <= candidate <= spec.max_value and candidate not in out:
                out.append(candidate)
    return out


def _is_code(value: str) -> bool:
    stripped = re.sub(r"[\s\-_/]", "", normalizer.latin_digits(value))
    return bool(stripped) and stripped.isdigit()


def _bool_probe(page: PageDetails, spec: fields.FieldSpec) -> list[str]:
    """وقتی جدول مشخصات چیزی نگفته، عنوان و برچسب‌های خودِ محصول را نگاه کن.

    متن کامل صفحه عمداً گشته نمی‌شود؛ آنجا منوی سایت هم هست و «دارد» را از
    جای اشتباه برمی‌دارد.
    """
    haystack = details.label_key(" ".join([page.title, *page.tags, *page.categories]))
    words = [spec.column, *spec.labels, *spec.true_hints]
    return [spec.true_label] if any(details.label_key(w) in haystack for w in words if w) else []


def build_values(
    title: str,
    pages: Sequence[PageDetails],
    options: FillOptions,
    specs: Sequence[fields.FieldSpec],
    norm_config: normalizer.NormalizerConfig,
    image_pick: images.ImagePick | None = None,
) -> tuple[dict[str, str], dict[str, dict], consensus.MergedDetails]:
    """همه‌ی صفحه‌های یک عنوان → مقدار هر ستونِ شیت."""
    common = consensus.merge(
        pages,
        summary_options=options.summary,
        pages_options={"tolerance": 10, **options.numbers},
        combine_scripts=options.combine_author_scripts,
    )
    values: dict[str, str] = {}
    evidence: dict[str, dict] = {}
    for spec in specs:
        decision = decide(
            spec, title, pages, common, options, norm_config, image_pick=image_pick
        )
        values[spec.key] = decision.value
        evidence[spec.key] = {"column": spec.column, "kind": spec.kind, **decision.as_dict()}
    return values, evidence, common


# ---------------------------------------------------------------------------
# گزارش
# ---------------------------------------------------------------------------

REPORT_LEAD = ("ردیف", "عنوان", "وضعیت", "منابع")
REPORT_TAIL = ("یادداشت",)


def report_header(specs: Sequence[fields.FieldSpec]) -> list[str]:
    return [*REPORT_LEAD, *[f"{spec.column} (رأی)" for spec in specs], *REPORT_TAIL]


def report_row(
    row_number: int,
    title: str,
    status: str,
    source_count: int,
    specs: Sequence[fields.FieldSpec],
    values: dict[str, str],
    evidence: dict[str, dict],
    note: str,
) -> list[object]:
    cells: list[object] = [row_number, title, status, source_count]
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


# ---------------------------------------------------------------------------
# اجرا
# ---------------------------------------------------------------------------


def inspect(
    options: FillOptions, document: gsheet.Document | None = None
) -> tuple[fields.FieldPlan, list[gsheet.SheetRow], dict[str, list[str]]]:
    """شیت را می‌خواند و می‌گوید چه ستون‌هایی دارد — بدون نوشتن هیچ چیزی."""
    document = document or gsheet.open_document(options.sheet)
    lists = taxonomy.options_from_lists_tab(document.read_tab(options.sheet.lists_tab))
    grid = document.read_grid()
    plan = gsheet.plan_from_grid(grid, options.sheet, lists)
    rows = gsheet.rows_from_grid(grid, plan, options.sheet.header_row)
    return plan, rows, lists


def run(
    conn: sqlite3.Connection,
    config: Config,
    options: FillOptions,
    fetcher: Fetcher,
    document: gsheet.Document | None = None,
    log: LogFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> FillStats:
    """پر کردن شیت. خروجی، خلاصه‌ی همین اجراست."""
    say: LogFn = log or print
    stop = should_stop or (lambda: False)
    norm_config = normalizer.config_from_mapping(config.get("normalizer", {}))
    stats = FillStats()
    key = options.key

    document = document or gsheet.open_document(options.sheet)
    plan, sheet_rows, lists = inspect(options, document)
    stats.rows = len(sheet_rows)
    say(f"شیت خوانده شد: {len(sheet_rows)} ردیف")
    say(
        "ستون‌ها — "
        + "، ".join(f"{col}: {KIND_LABELS.get(kind, kind)}" for col, kind in plan.describe().items())
    )
    if lists:
        say("لیست‌ها — " + "، ".join(f"{name}: {len(values)}" for name, values in lists.items()))

    specs = options.writable(plan)
    targets = [spec.key for spec in specs]
    titles = {spec.key: spec.column for spec in specs}
    db.remember_sheet(
        conn,
        key,
        options.sheet.tab or options.sheet.file or options.sheet.sheet_id,
        [{"column": s.column, "key": s.key, "kind": s.kind} for s in plan.specs],
    )
    if not targets:
        say("⚠ هیچ ستون قابل نوشتنی پیدا نشد؛ فقط دیتابیس پر می‌شود.")

    recovered = _push_unwritten(conn, key, document, plan.columns, targets, sheet_rows, options)
    if recovered:
        stats.cells_written += recovered
        say(f"{recovered} سلول از اجرای قبلی که به شیت نرسیده بود، نوشته شد.")

    queue: list[tuple[gsheet.SheetRow, int, list[str]]] = []
    with db.transaction(conn):
        for row in sheet_rows:
            title_key = normalizer.normalize(row.title, norm_config)
            row_id = db.upsert_row(conn, key, row.title, title_key, row.number)
            wanted = targets if options.overwrite == "always" else row.empty_fields(targets)
            if wanted and _needs_work(conn, key, title_key, options):
                queue.append((row, row_id, wanted))
    stats.queued = len(queue)

    caps = [value for value in (options.limit, options.max_titles_per_session) if value > 0]
    cap = min(caps) if caps else 0
    if cap and cap < len(queue):
        say(f"سقف این نشست: {cap} عنوان از {len(queue)} عنوانِ در صف")
        queue = queue[:cap]

    pending_updates: list[gsheet.CellUpdate] = []
    pending_ids: list[int] = []
    report_rows: list[list[object]] = []
    sources_column = _sources_column(plan, options)

    for index, (row, row_id, wanted) in enumerate(queue, start=1):
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
        known = _known_urls(row, sources_column)
        pages, source_stats = sources.collect(
            conn,
            row.title,
            title_key,
            options.sites,
            fetcher,
            known_urls=known,
            max_sources=options.max_sources_per_title,
            min_similarity=options.min_title_similarity,
            search_enabled=options.search_enabled,
            max_results_per_site=options.max_results_per_site,
            cache_ttl_days=options.cache_ttl_days,
            norm_config=norm_config,
        )
        stats.pages_fetched += source_stats.fetched
        stats.from_cache += source_stats.from_cache
        stats.processed += 1

        if not pages:
            stats.no_source += 1
            with db.transaction(conn):
                db.save_values(
                    conn, row_id, {}, status=db.PARTIAL, note="هیچ صفحه‌ی منبعی پیدا نشد"
                )
            report_rows.append(
                report_row(row.number, row.title, "بدون منبع", 0, specs, {}, {}, "منبعی پیدا نشد")
            )
            continue

        pick = _pick_image(pages, options, fetcher)
        values, evidence, _ = build_values(
            row.title, pages, options, specs, norm_config, image_pick=pick
        )

        required = [spec.key for spec in specs if spec.required and _expected(spec, values, options)]
        missing = [name for name in required if name in wanted and not values.get(name)]
        status = db.DONE if not missing else db.PARTIAL
        note = "" if not missing else "پر نشد: " + "، ".join(titles.get(n, n) for n in missing)
        with db.transaction(conn):
            db.save_values(
                conn,
                row_id,
                values,
                sources=[page.url for page in pages],
                evidence=evidence,
                status=status,
                note=note,
            )
        stats.completed += int(status == db.DONE)
        stats.partial += int(status == db.PARTIAL)

        for name in wanted:
            value = values.get(name, "")
            column = plan.columns.get(name)
            if not value or column is None:
                continue
            pending_updates.append(gsheet.CellUpdate(row.number, column, value))
            label = titles.get(name, name)
            stats.filled[label] = stats.filled.get(label, 0) + 1
        pending_ids.append(row_id)

        report_rows.append(
            report_row(
                row.number,
                row.title,
                "کامل" if status == db.DONE else "ناقص",
                len(pages),
                specs,
                values,
                evidence,
                note,
            )
        )

        if len(pending_ids) >= options.batch_rows:
            stats.cells_written += _flush(conn, document, pending_updates, pending_ids)
            pending_updates, pending_ids = [], []
        if index % 10 == 0:
            say(f"  {index}/{len(queue)} — نوشته‌شده: {stats.cells_written} سلول")

    stats.cells_written += _flush(conn, document, pending_updates, pending_ids)

    if options.sheet.report_tab and report_rows:
        try:
            stats.report = document.write_report(
                options.sheet.report_tab, report_header(specs), report_rows
            )
        except Exception as exc:  # noqa: BLE001 — گزارش نباید کل اجرا را بیندازد
            say(f"⚠ نوشتن تبِ گزارش نشد: {exc}")

    closed = document.close()
    if isinstance(document, gsheet.GoogleSheetDocument):
        stats.sheet_url = closed or ""
    else:
        stats.output_path = closed or ""
    return stats


# ---------------------------------------------------------------------------
# کمکی‌ها
# ---------------------------------------------------------------------------


def _sources_column(plan: fields.FieldPlan, options: FillOptions) -> str:
    """کلید ستونی که آدرس منابع در آن است (اگر شیت چنین ستونی دارد)."""
    wanted = details.label_key(options.sheet.sources_column)
    if not wanted:
        return ""
    for spec in plan.specs:
        if details.label_key(spec.column) == wanted:
            return spec.key
    return ""


def _known_urls(row: gsheet.SheetRow, column_key: str) -> list[str]:
    if not column_key:
        return []
    return re.findall(r"https?://\S+", row.values.get(column_key, ""))


def _expected(spec: fields.FieldSpec, values: dict[str, str], options: FillOptions) -> bool:
    """آیا خالی ماندن این ستون واقعاً نقص است؟

    مترجم فقط برای اثر خارجی انتظار می‌رود؛ خالی بودنش برای اثر ایرانی نقص
    نیست و نباید ردیف را «ناقص» علامت بزند.
    """
    if spec.key != "translator":
        return True
    return values.get("nationality") == options.labels.get("foreign", "خارجی")


def _needs_work(
    conn: sqlite3.Connection, key: str, title_key: str, options: FillOptions
) -> bool:
    """آیا این ردیف باید (دوباره) پردازش شود؟

    ستون خالی به‌تنهایی دلیل کافی نیست: «مترجم» یک اثر ایرانی همیشه خالی
    می‌ماند و اگر ملاک فقط خالی بودن سلول باشد، هر اجرا همان ردیف‌ها را از نو
    می‌گردد.
    """
    if options.overwrite == "always":
        return True
    row = db.row_by_key(conn, key, title_key)
    if row is None:
        return True
    status = row["status"] or db.PENDING
    if status == db.PENDING:
        return True
    return status == db.PARTIAL and options.retry_partial


def _flush(
    conn: sqlite3.Connection,
    document: gsheet.Document,
    updates: Sequence[gsheet.CellUpdate],
    ids: Sequence[int],
) -> int:
    if not updates and not ids:
        return 0
    written = document.apply(list(updates))
    db.mark_pushed(conn, list(ids))
    return written


def _push_unwritten(
    conn: sqlite3.Connection,
    key: str,
    document: gsheet.Document,
    columns: dict[str, int],
    targets: Sequence[str],
    sheet_rows: Sequence[gsheet.SheetRow],
    options: FillOptions,
) -> int:
    """ردیف‌هایی که در دیتابیس کامل شده‌اند ولی به شیت نرسیده‌اند.

    اگر اجرای قبلی وسط نوشتن قطع شده باشد (اینترنت، بسته شدن پنل، سهمیه‌ی
    گوگل)، مقدارها هست ولی ردیف «انجام‌شده» علامت خورده — بدون این مرحله
    دیگر هیچ‌وقت نوشته نمی‌شدند.
    """
    pending = db.unpushed_rows(conn, key)
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
        stored = db.row_values(record)
        for name in allowed:
            column = columns.get(name)
            value = stored.get(name, "")
            if column is not None and value:
                updates.append(gsheet.CellUpdate(sheet_row.number, column, str(value)))
        ids.append(int(record["id"]))
    return _flush(conn, document, updates, ids)


def _pick_image(
    pages: Sequence[PageDetails], options: FillOptions, fetcher: Fetcher
) -> images.ImagePick:
    candidates = [(candidate, page.domain) for page in pages for candidate in page.images]
    if not candidates:
        return images.ImagePick(note="هیچ تصویری در صفحه‌های منبع نبود")
    return images.pick_image(candidates, options.image, fetch=fetcher.fetch_bytes)
