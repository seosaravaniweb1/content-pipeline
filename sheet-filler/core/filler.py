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
* **حالت خودکار دارد**: روشنش کنید و دیگر کاری ندارید — هر چند دقیقه یک‌بار
  شیت را می‌خواند و ردیف‌های تازه را پر می‌کند.
* **ستون‌های «وضعیت» و «شناسه محصول» دست نمی‌خورند**؛ آن‌ها مالِ اسکریپت درج
  محصول‌اند.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import (
    consensus,
    db,
    details,
    fields,
    gsheet,
    images,
    normalizer,
    settings,
    sources,
    taxonomy,
)
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

#: برچسب فارسی مقدارهای استاندارد (ملیت و فرمت)
DEFAULT_LABELS = {
    "iranian": "ایرانی",
    "foreign": "خارجی",
    "pdf": "پی دی اف",
    "audio": "صوتی",
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
    """تنظیمات یک اجرا.

    **این کلاس تنها جای تعریف مقدارهای پیش‌فرض است.** ``config.yaml`` و پنل
    فقط چیزهایی را می‌فرستند که کاربر عوض کرده؛ بقیه همین‌جا می‌مانند. پس یک
    تنظیم هیچ‌وقت دو مقدار پیش‌فرضِ ناهماهنگ ندارد.
    """

    sheet: gsheet.SheetSettings = field(default_factory=gsheet.SheetSettings)
    image: images.ImageRules = field(default_factory=images.ImageRules)
    sites: list[SourceSite] = field(default_factory=list)

    # -- چیزهایی که کاربر تصمیم می‌گیرد ------------------------------------
    overwrite: str = "empty"
    limit: int = 0
    max_sources_per_title: int = 5
    retry_partial: bool = False
    #: حالت خودکار: خودش هر چند دقیقه شیت را می‌خواند و ردیف‌های تازه را پر می‌کند
    auto: bool = False
    auto_every_minutes: int = 30

    # -- چیزهایی که برنامه خودش می‌داند (قابل تغییر از config، نه از پنل) ----
    min_title_similarity: float = 0.72
    search_enabled: bool = True
    max_results_per_site: int = 8
    batch_rows: int = 50
    cache_ttl_days: int = 30
    #: ردیف بی‌منبع، بعد از این مدت دوباره امتحان می‌شود (منابع تازه اضافه می‌شوند)
    retry_after_days: int = 7
    separator: str = "، "
    max_categories: int = 3
    max_tags: int = 5
    combine_author_scripts: bool = True
    labels: dict = field(default_factory=lambda: dict(DEFAULT_LABELS))
    summary: dict = field(default_factory=dict)
    numbers: dict = field(default_factory=dict)
    taxonomy: dict = field(default_factory=dict)

    def writable(self, plan: fields.FieldPlan) -> list[fields.FieldSpec]:
        """ستون‌های قابل نوشتنِ همین شیت."""
        return plan.writable(self.sheet.never_write)

    @property
    def key(self) -> str:
        return db.sheet_key(self.sheet.sheet_id, self.sheet.tab, self.sheet.file)

    @property
    def ready(self) -> bool:
        return bool(self.sheet.sheet_id or self.sheet.file)


#: کلیدهایی که مستقیم روی ``FillOptions`` می‌نشینند
_SIMPLE_KEYS = {
    name
    for name in FillOptions.__dataclass_fields__
    if name not in {"sheet", "image", "sites", "labels", "summary", "numbers", "taxonomy"}
}
def options_from_config(config: Config, overrides: dict | None = None) -> FillOptions:
    """``config.yaml`` + تنظیمات پنل → :class:`FillOptions`.

    یک مسیر ادغام، نه چهار تا: هر چه در config است با هر چه کاربر در پنل عوض
    کرده یکی می‌شود، تایپ‌ها تمیز می‌شوند و بعد روی پیش‌فرض‌های همین ماژول
    می‌نشیند.
    """
    merged: dict[str, Any] = {}
    for block in ("sheet", "fill", "image"):
        merged.update(config.get(block, {}) or {})
    merged["search_enabled"] = bool(config.get("search.enabled", True))
    merged["max_results_per_site"] = int(config.get("search.max_results_per_site", 8))
    merged["sites"] = list(config.get("sites", []) or [])
    merged.update(settings.normalize(overrides or {}))
    # بلوک ``search`` هم به‌شکل دیکشنری پذیرفته می‌شود (همان شکلی که در
    # config.yaml نوشته می‌شود)، تا یک تنظیم دو اسم نداشته باشد.
    if isinstance(merged.get("search"), dict):
        block = merged.pop("search")
        if "enabled" in block:
            merged["search_enabled"] = bool(block["enabled"])
        if "max_results_per_site" in block:
            merged["max_results_per_site"] = int(block["max_results_per_site"])

    # --- چیزهایی که خودکار فهمیده می‌شوند --------------------------------
    sheet_id = settings.sheet_id_from(merged.get("sheet_url") or merged.get("sheet_id") or "")
    if sheet_id:
        merged["sheet_id"] = sheet_id
    if merged.get("sheet_id"):
        merged["service_account_json"] = settings.find_service_account(
            merged.get("service_account_json", ""), config.path
        )

    options = FillOptions(
        sheet=gsheet.SheetSettings.from_mapping(merged),
        image=images.ImageRules.from_mapping(
            {**(config.get("image", {}) or {}), "enabled": merged.get("image", True)}
        ),
        sites=_sites_from(merged, config),
    )
    for key, value in merged.items():
        if key in _SIMPLE_KEYS and value is not None:
            setattr(options, key, value)
    labels = dict(DEFAULT_LABELS)
    labels.update(config.get("fill.labels", {}) or {})
    options.labels = labels
    options.summary = dict(config.get("fill.summary", {}) or {})
    options.numbers = dict(config.get("fill.numbers", {}) or {})
    options.taxonomy = dict(config.get("fill.taxonomy", {}) or {})
    return options


def _sites_from(merged: dict, config: Config) -> list[SourceSite]:
    default_search = config.get("search.url_template", "{base}/?s={query}")
    entries = merged.get("sites") or []
    return [SourceSite.from_entry(entry, default_search) for entry in entries if entry]


# ---------------------------------------------------------------------------
# آمار
# ---------------------------------------------------------------------------


@dataclass
class FillStats:
    rows: int = 0
    queued: int = 0
    already_filled: int = 0
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
            f"از قبل پر: {self.already_filled}، "
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


@dataclass
class _Job:
    """چیزهایی که یک اجرا از اول تا آخر با خودش می‌برد."""

    conn: sqlite3.Connection
    options: FillOptions
    document: gsheet.Document
    plan: fields.FieldPlan
    specs: list[fields.FieldSpec]
    rows: list[gsheet.SheetRow]
    norm_config: normalizer.NormalizerConfig
    fetcher: Fetcher
    say: LogFn
    stats: FillStats = field(default_factory=FillStats)
    updates: list[gsheet.CellUpdate] = field(default_factory=list)
    ids: list[int] = field(default_factory=list)
    report_rows: list[list[object]] = field(default_factory=list)

    @property
    def targets(self) -> list[str]:
        return [spec.key for spec in self.specs]

    @property
    def titles(self) -> dict[str, str]:
        return {spec.key: spec.column for spec in self.specs}


def prepare(
    conn: sqlite3.Connection,
    config: Config,
    options: FillOptions,
    fetcher: Fetcher,
    document: gsheet.Document | None,
    say: LogFn,
) -> _Job:
    """خواندن شیت، ساخت نقشه‌ی ستون‌ها و گزارش آنچه دیده شد."""
    document = document or gsheet.open_document(options.sheet)
    plan, rows, lists = inspect(options, document)
    specs = options.writable(plan)
    dropped = []
    if not options.image.enabled:
        # «جمع‌آوری تصویر» خاموش است: ستون تصویر اصلاً هدف این اجرا نیست —
        # نه دنبالش می‌گردیم، نه ردیفی را فقط به‌خاطرش در صف می‌گذاریم.
        dropped = [spec.column for spec in specs if spec.kind == fields.KIND_IMAGE]
        specs = [spec for spec in specs if spec.kind != fields.KIND_IMAGE]
    job = _Job(
        conn=conn,
        options=options,
        document=document,
        plan=plan,
        specs=specs,
        rows=rows,
        norm_config=normalizer.config_from_mapping(config.get("normalizer", {})),
        fetcher=fetcher,
        say=say,
    )
    job.stats.rows = len(rows)
    say(f"شیت خوانده شد: {len(rows)} ردیف")
    say(
        "ستون‌ها — "
        + "، ".join(f"{col}: {KIND_LABELS.get(kind, kind)}" for col, kind in plan.describe().items())
    )
    if lists:
        say("لیست‌ها — " + "، ".join(f"{name}: {len(values)}" for name, values in lists.items()))
    if dropped:
        say("جمع‌آوری تصویر خاموش است — " + "، ".join(dropped) + " دست‌نخورده می‌ماند.")
    if not job.specs:
        say("⚠ هیچ ستون قابل نوشتنی پیدا نشد؛ فقط دیتابیس پر می‌شود.")

    db.remember_sheet(
        conn,
        options.key,
        options.sheet.tab or options.sheet.file or options.sheet.sheet_id,
        [{"column": s.column, "key": s.key, "kind": s.kind} for s in plan.specs],
    )
    return job


def build_queue(job: _Job) -> list[tuple[gsheet.SheetRow, int, list[str]]]:
    """ردیف‌هایی که باید کار شوند، به‌ترتیب خودِ شیت."""
    options, conn = job.options, job.conn
    queue: list[tuple[gsheet.SheetRow, int, list[str]]] = []
    with db.transaction(conn):
        for row in job.rows:
            title_key = normalizer.normalize(row.title, job.norm_config)
            row_id = db.upsert_row(conn, options.key, row.title, title_key, row.number)
            wanted = job.targets if options.overwrite == "always" else row.empty_fields(job.targets)
            if not wanted:
                continue
            if _already_filled(job, row, wanted):
                job.stats.already_filled += 1
                continue
            if _needs_work(conn, options.key, title_key, options):
                queue.append((row, row_id, wanted))
    job.stats.queued = len(queue)
    if job.stats.already_filled:
        job.say(f"{job.stats.already_filled} ردیف از قبل پر بود و رد شد.")
    if options.limit and options.limit < len(queue):
        job.say(f"سقف این اجرا: {options.limit} ردیف از {len(queue)} ردیفِ در صف")
        queue = queue[: options.limit]
    return queue


def process_row(job: _Job, row: gsheet.SheetRow, row_id: int, wanted: list[str]) -> None:
    """یک ردیف: منابعش را پیدا کن، مقدارها را بساز، ذخیره کن."""
    options, conn, stats = job.options, job.conn, job.stats
    title_key = normalizer.normalize(row.title, job.norm_config)
    pages, source_stats = sources.collect(
        conn,
        row.title,
        title_key,
        _ordered_sites(conn, options),
        job.fetcher,
        known_urls=_known_urls(row, _sources_column(job.plan, options)),
        max_sources=options.max_sources_per_title,
        min_similarity=options.min_title_similarity,
        search_enabled=options.search_enabled,
        max_results_per_site=options.max_results_per_site,
        cache_ttl_days=options.cache_ttl_days,
        norm_config=job.norm_config,
    )
    stats.pages_fetched += source_stats.fetched
    stats.from_cache += source_stats.from_cache
    stats.processed += 1

    if not pages:
        stats.no_source += 1
        with db.transaction(conn):
            db.save_values(conn, row_id, {}, status=db.PARTIAL, note="هیچ صفحه‌ی منبعی پیدا نشد")
        job.report_rows.append(
            report_row(row.number, row.title, "بدون منبع", 0, job.specs, {}, {}, "منبعی پیدا نشد")
        )
        return

    # وقتی ستون تصویری در کار نیست، دنبال کاور نمی‌گردیم: هر کاندید یعنی چند
    # درخواست اضافه برای چیزی که قرار نیست نوشته شود.
    wants_image = any(spec.kind == fields.KIND_IMAGE for spec in job.specs)
    pick = _pick_image(pages, options, job.fetcher) if wants_image else images.ImagePick()
    values, evidence, _ = build_values(
        row.title, pages, options, job.specs, job.norm_config, image_pick=pick
    )

    missing = [
        spec.key
        for spec in job.specs
        if spec.required and spec.key in wanted and not values.get(spec.key)
    ]
    status = db.DONE if not missing else db.PARTIAL
    note = "" if not missing else "پر نشد: " + "، ".join(job.titles.get(n, n) for n in missing)
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
        # دامنه‌هایی که جواب دادند یاد گرفته می‌شوند: دفعه‌ی بعد اول از همان‌ها
        for page in pages:
            db.note_domain(conn, page.domain)
    stats.completed += int(status == db.DONE)
    stats.partial += int(status == db.PARTIAL)

    for name in wanted:
        value = values.get(name, "")
        column = job.plan.columns.get(name)
        if not value or column is None:
            continue
        job.updates.append(gsheet.CellUpdate(row.number, column, value))
        label = job.titles.get(name, name)
        stats.filled[label] = stats.filled.get(label, 0) + 1
    job.ids.append(row_id)

    job.report_rows.append(
        report_row(
            row.number,
            row.title,
            "کامل" if status == db.DONE else "ناقص",
            len(pages),
            job.specs,
            values,
            evidence,
            note,
        )
    )


def finish(job: _Job) -> FillStats:
    """نوشتن باقی‌مانده‌ها، گزارش و بستن سند."""
    options, stats = job.options, job.stats
    stats.cells_written += _flush(job.conn, job.document, job.updates, job.ids)
    job.updates, job.ids = [], []

    if options.sheet.report_tab and job.report_rows:
        try:
            stats.report = job.document.write_report(
                options.sheet.report_tab, report_header(job.specs), job.report_rows
            )
        except Exception as exc:  # noqa: BLE001 — گزارش نباید کل اجرا را بیندازد
            job.say(f"⚠ نوشتن تبِ گزارش نشد: {exc}")

    closed = job.document.close()
    if isinstance(job.document, gsheet.GoogleSheetDocument):
        stats.sheet_url = closed or ""
    else:
        stats.output_path = closed or ""
    return stats


def run(
    conn: sqlite3.Connection,
    config: Config,
    options: FillOptions,
    fetcher: Fetcher,
    document: gsheet.Document | None = None,
    log: LogFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> FillStats:
    """پر کردن شیت — یک بار، از اول تا آخر."""
    say: LogFn = log or print
    stop = should_stop or (lambda: False)

    job = prepare(conn, config, options, fetcher, document, say)
    recovered = _push_unwritten(job)
    if recovered:
        job.stats.cells_written += recovered
        say(f"{recovered} سلول از اجرای قبلی که به شیت نرسیده بود، نوشته شد.")

    queue = build_queue(job)
    for index, (row, row_id, wanted) in enumerate(queue, start=1):
        if stop():
            job.stats.stopped_early = "به درخواست شما متوقف شد؛ بقیه‌ی ردیف‌ها در صف ماندند."
            break
        process_row(job, row, row_id, wanted)
        if len(job.ids) >= options.batch_rows:
            job.stats.cells_written += _flush(conn, job.document, job.updates, job.ids)
            job.updates, job.ids = [], []
        if index % 10 == 0:
            say(f"  {index}/{len(queue)} — نوشته‌شده: {job.stats.cells_written} سلول")
    return finish(job)


def run_auto(
    conn: sqlite3.Connection,
    config: Config,
    options: FillOptions,
    fetcher_factory: Callable[[], Fetcher],
    log: LogFn | None = None,
    should_stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    rounds: int = 0,
) -> FillStats:
    """حالت خودکار: پر کن، بخواب، دوباره شیت را بخوان.

    این همان چیزی است که در عمل لازم است: عنوان تازه را در شیت می‌گذارید و
    بدون اینکه دکمه‌ای بزنید پر می‌شود. بین دورها می‌خوابیم چون شیت مدام عوض
    نمی‌شود و درخواست بی‌دلیل به سایت‌های منبع هم کند است هم بی‌ادبانه.

    ``rounds`` صفر یعنی بی‌نهایت (تا وقتی لغو شود)؛ عدد مثبت برای تست.
    """
    import time

    say: LogFn = log or print
    stop = should_stop or (lambda: False)
    nap = sleep or time.sleep
    total = FillStats()
    round_number = 0

    while not stop():
        round_number += 1
        fetcher = fetcher_factory()
        try:
            stats = run(conn, config, options, fetcher, log=say, should_stop=stop)
        finally:
            close = getattr(fetcher, "close", None)
            if close:
                close()
        _accumulate(total, stats)

        if (rounds and round_number >= rounds) or stop():
            break
        minutes = max(1, int(options.auto_every_minutes or 30))
        say(f"⏳ دور {round_number} تمام شد؛ {minutes} دقیقه‌ی دیگر دوباره سراغ شیت می‌روم.")
        # خواب تکه‌تکه تا «لغو» زودتر از چند دقیقه جواب بدهد
        for _ in range(minutes * 6):
            if stop():
                break
            nap(10)
    total.stopped_early = total.stopped_early or f"{round_number} دور اجرا شد."
    return total


def _accumulate(total: FillStats, stats: FillStats) -> None:
    total.rows = stats.rows
    for name in (
        "queued",
        "processed",
        "completed",
        "partial",
        "no_source",
        "pages_fetched",
        "from_cache",
        "cells_written",
    ):
        setattr(total, name, getattr(total, name) + getattr(stats, name))
    for key, value in stats.filled.items():
        total.filled[key] = total.filled.get(key, 0) + value
    total.sheet_url = stats.sheet_url or total.sheet_url
    total.output_path = stats.output_path or total.output_path
    total.report = stats.report or total.report


# ---------------------------------------------------------------------------
# کمکی‌ها
# ---------------------------------------------------------------------------


def _ordered_sites(conn: sqlite3.Connection, options: FillOptions) -> list[SourceSite]:
    """سایت‌ها به ترتیب «کدام تا حالا بیشتر جواب داده».

    اگر هیچ سایتی تنظیم نشده باشد، از دامنه‌ی لینک‌های ایمپورت‌شده ساخته
    می‌شود: کاربری که آدرس‌ها را داده، عملاً سایت‌ها را هم داده.
    """
    sites = list(options.sites)
    if not sites:
        sites = [
            SourceSite.from_entry(f"https://{domain}", "{base}/?s={query}")
            for domain in db.known_domains(conn)
        ]
    scores = db.domain_scores(conn)
    sites.sort(key=lambda site: -scores.get(site.domain, 0))
    return sites


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


def _already_filled(job: _Job, row: gsheet.SheetRow, wanted: Sequence[str]) -> bool:
    """ردیفی که کاربر خودش پرش کرده و فقط ستون‌های اختیاری‌اش خالی مانده.

    چنین ردیفی نباید دوباره کار شود: «Image» یا «مترجمِ» یک اثر ایرانی همیشه
    خالی می‌ماند و اگر ملاک فقط خالی بودن سلول باشد، هر اجرا بودجه‌اش را صرف
    ردیف‌های تمام‌شده می‌کند — دقیقاً همان چیزی که نباید بشود.

    هیچ‌چیز در دیتابیس ثبت نمی‌شود؛ اگر بعداً همان ستون را لازم داشتید (مثلاً
    جمع‌آوری تصویر را روشن کردید)، ردیف دوباره به صف برمی‌گردد.
    """
    if job.options.overwrite == "always":
        return False
    if len(wanted) >= len(job.targets):
        # هیچ ستونی پر نشده: ردیف دست‌نخورده است، نه تمام‌شده
        return False
    optional = {spec.key for spec in job.specs if not spec.required}
    return all(key in optional for key in wanted)


def _needs_work(
    conn: sqlite3.Connection, key: str, title_key: str, options: FillOptions
) -> bool:
    """آیا این ردیف باید (دوباره) پردازش شود؟

    ستون خالی به‌تنهایی دلیل کافی نیست: «مترجم» یک اثر ایرانی همیشه خالی
    می‌ماند و اگر ملاک فقط خالی بودن سلول باشد، هر اجرا همان ردیف‌ها را از نو
    می‌گردد.

    ولی ردیفی که **منبعی برایش پیدا نشد** فرق دارد: شاید سایت تازه‌ای اضافه
    کرده‌اید یا منبعی محصول را دیرتر گذاشته. چنین ردیفی بعد از
    ``retry_after_days`` خودبه‌خود دوباره امتحان می‌شود، بدون اینکه کاربر
    دکمه‌ای بزند.
    """
    if options.overwrite == "always":
        return True
    row = db.row_by_key(conn, key, title_key)
    if row is None:
        return True
    status = row["status"] or db.PENDING
    if status == db.PENDING:
        return True
    if status != db.PARTIAL:
        return False
    if options.retry_partial:
        return True
    note = row["note"] or ""
    return "منبع" in note and db.older_than(row["updated_at"], options.retry_after_days)


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


def _push_unwritten(job: _Job) -> int:
    """ردیف‌هایی که در دیتابیس کامل شده‌اند ولی به شیت نرسیده‌اند.

    اگر اجرای قبلی وسط نوشتن قطع شده باشد (اینترنت، بسته شدن پنل، سهمیه‌ی
    گوگل)، مقدارها هست ولی ردیف «انجام‌شده» علامت خورده — بدون این مرحله
    دیگر هیچ‌وقت نوشته نمی‌شدند.
    """
    options = job.options
    pending = db.unpushed_rows(job.conn, options.key)
    if not pending or not job.specs:
        return 0
    by_number = {row.number: row for row in job.rows}
    updates: list[gsheet.CellUpdate] = []
    ids: list[int] = []
    for record in pending:
        sheet_row = by_number.get(int(record["row_number"] or 0))
        if sheet_row is None:
            continue
        allowed = (
            job.targets if options.overwrite == "always" else sheet_row.empty_fields(job.targets)
        )
        stored = db.row_values(record)
        for name in allowed:
            column = job.plan.columns.get(name)
            value = stored.get(name, "")
            if column is not None and value:
                updates.append(gsheet.CellUpdate(sheet_row.number, column, str(value)))
        ids.append(int(record["id"]))
    return _flush(job.conn, job.document, updates, ids)


def _pick_image(
    pages: Sequence[PageDetails], options: FillOptions, fetcher: Fetcher
) -> images.ImagePick:
    candidates = [(candidate, page.domain) for page in pages for candidate in page.images]
    if not candidates:
        return images.ImagePick(note="هیچ تصویری در صفحه‌های منبع نبود")
    return images.pick_image(candidates, options.image, fetch=fetcher.fetch_bytes)
