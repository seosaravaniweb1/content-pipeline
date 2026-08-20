"""فیلدهای شیت — «چه ستونی داریم» را **خودِ شیت** تعیین می‌کند، نه کد.

مسئله‌ای که این ماژول حل می‌کند: شیت رمان ستون «خلاصه» و «مترجم» دارد، شیت
نمونه‌سوال ستون «تعداد سوالات» و «کد رایانه» و «جزوه همراه»، و شیت جزوه ستون
«نام کتاب» و «مناسب رشته». اگر فهرست ستون‌ها در کد ثابت باشد، برای هر موضوع
تازه باید کد عوض شود. پس برعکسش می‌کنیم:

    عنوان ستونِ شیت  →  یک :class:`FieldSpec`  →  روش استخراج و ادغام

عنوان ستون سه کار می‌کند:

1. **برچسبی که در صفحه‌ی منبع دنبالش می‌گردیم.** ستون «تعداد سوالات» یعنی در
   جدول مشخصات منابع دنبال «تعداد سوالات» بگرد. برای ستون‌های پرکاربرد،
   هم‌معنی‌هایشان هم از :data:`BUILTIN` می‌آید («نویسنده» = «مولف» = «به قلم»).
2. **نوع فیلد را می‌گوید.** «تعداد ...» عدد است، «کد ...» متن، «آیا/دارای ...»
   بله‌وخیر، «خلاصه/توضیحات» متن چندمنبعی، «تصویر» کاور، و ستونی که در تبِ
   «لیست‌ها» ستون هم‌نام دارد، کرکره‌ای است.
3. **نام ستون خروجی** — همان‌جا که مقدار نوشته می‌شود.

نتیجه: برای موضوع تازه فقط ستون‌های شیت را می‌سازید (و اگر کرکره‌ای است، یک
ستون هم‌نام در تبِ «لیست‌ها»). نه کد عوض می‌شود نه config — مگر بخواهید
هم‌معنی‌های خاص خودتان را اضافه کنید.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .details import label_key

# ---------------------------------------------------------------------------
# نوع فیلدها
# ---------------------------------------------------------------------------

#: از خودِ عنوان ساخته می‌شود (کلمه‌ی کلیدی اصلی)
KIND_KEYWORD = "keyword"
#: نام شخص: نویسنده، مترجم، مدرس، گردآورنده
KIND_PERSON = "person"
#: عدد با رأی‌گیری بین منابع: تعداد صفحات، تعداد سوالات، سال
KIND_NUMBER = "number"
#: متن کوتاه: کد رایانه، ناشر، نام کتاب
KIND_TEXT = "text"
#: متن بلند چندمنبعی: خلاصه، معرفی، توضیحات
KIND_SUMMARY = "summary"
#: یکی از گزینه‌های کرکره: ملیت، مقطع
KIND_CHOICE = "choice"
#: چند گزینه از کرکره: دسته، تگ، مناسب رشته
KIND_MULTI = "multi_choice"
#: دارد/ندارد: جزوه همراه، پاسخ تشریحی
KIND_BOOL = "boolean"
#: لینک کاور مربع
KIND_IMAGE = "image"
#: ستون عنوان (ورودی، نه خروجی)
KIND_TITLE = "title"
#: ستونی که هرگز نوشته نمی‌شود: وضعیت، شناسه محصول
KIND_SKIP = "skip"

WRITABLE_KINDS = frozenset(
    {
        KIND_KEYWORD,
        KIND_PERSON,
        KIND_NUMBER,
        KIND_TEXT,
        KIND_SUMMARY,
        KIND_CHOICE,
        KIND_MULTI,
        KIND_BOOL,
        KIND_IMAGE,
    }
)


@dataclass
class FieldSpec:
    """یک ستون شیت و روش پر کردنش."""

    key: str
    kind: str
    #: عنوان ستون همان‌طور که در شیت نوشته شده
    column: str = ""
    #: برچسب‌هایی که در صفحه‌ی منبع دنبالشان می‌گردیم (عنوان ستون همیشه جزوشان است)
    labels: tuple[str, ...] = ()
    #: گزینه‌های مجاز (برای choice/multi_choice)
    options: tuple[str, ...] = ()
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    #: خالی ماندنش یعنی ردیف «ناقص» است
    required: bool = True
    max_values: int = 3
    #: بازه‌ی معقول برای عدد — بیرونش نویز است، نه مقدار
    min_value: int = 1
    max_value: int = 100000
    #: تلورانس خوشه‌بندی عددی (اختلاف قابل‌قبول بین منابع)
    tolerance: int = 0
    true_label: str = "دارد"
    false_label: str = "ندارد"
    #: عبارت‌هایی که وجودشان یعنی «دارد» (برای boolean)
    true_hints: tuple[str, ...] = ()

    @property
    def writable(self) -> bool:
        return self.kind in WRITABLE_KINDS

    def search_labels(self) -> tuple[str, ...]:
        """برچسب‌های جستجو: عنوان ستون + هم‌معنی‌ها، بدون تکرار."""
        seen: dict[str, str] = {}
        for label in (self.column, *self.labels):
            key = label_key(label)
            if key and key not in seen:
                seen[key] = label
        return tuple(seen.values())


# ---------------------------------------------------------------------------
# کتابخانه‌ی فیلدهای شناخته‌شده
# ---------------------------------------------------------------------------

#: ستون‌های پرکاربرد با هم‌معنی‌هایشان. این فهرست فقط **کمک** است: ستونی که
#: اینجا نباشد هم کار می‌کند (عنوان خودش برچسب می‌شود)، ولی ستونی که اینجا
#: باشد هم‌معنی‌های بیشتری برای گشتن دارد و نوعش دقیق‌تر است.
BUILTIN: tuple[FieldSpec, ...] = (
    FieldSpec(
        key="title",
        kind=KIND_TITLE,
        column="عنوان",
        labels=("عنوان محتوا", "عنوان محصول", "نام محصول", "title"),
    ),
    FieldSpec(
        key="keyword",
        kind=KIND_KEYWORD,
        column="keyword",
        labels=("کلمه کلیدی", "کلمه کلیدی اصلی", "کیورد", "keyword"),
    ),
    FieldSpec(
        key="author",
        kind=KIND_PERSON,
        column="Author",
        labels=(
            "نویسنده",
            "نویسنده کتاب",
            "نویسنده رمان",
            "نام نویسنده",
            "مولف",
            "مؤلف",
            "پدیدآور",
            "به قلم",
            "author",
        ),
    ),
    FieldSpec(
        key="translator",
        kind=KIND_PERSON,
        column="Translator",
        labels=("مترجم", "مترجمان", "نام مترجم", "ترجمه", "برگردان", "translator"),
        required=False,
    ),
    FieldSpec(
        key="teacher",
        kind=KIND_PERSON,
        column="مدرس",
        labels=("استاد", "مدرس", "گردآورنده", "تهیه کننده", "نویسنده جزوه"),
        required=False,
    ),
    FieldSpec(
        key="summary",
        kind=KIND_SUMMARY,
        column="Summary",
        labels=("خلاصه", "خلاصه رمان", "خلاصه کتاب", "معرفی", "توضیحات", "شرح", "summary"),
    ),
    FieldSpec(
        key="categories",
        kind=KIND_MULTI,
        column="Categories",
        labels=("دسته", "دسته بندی", "کتگوری", "ژانر", "موضوع", "category", "categories"),
        max_values=3,
    ),
    FieldSpec(
        key="tags",
        kind=KIND_MULTI,
        column="Tags",
        labels=("تگ", "برچسب", "tag", "tags"),
        max_values=5,
    ),
    FieldSpec(
        key="nationality",
        kind=KIND_CHOICE,
        column="Nationality",
        labels=("ملیت", "ملیت نویسنده", "کشور", "nationality"),
    ),
    FieldSpec(
        key="book_format",
        kind=KIND_MULTI,
        column="Format",
        labels=("فرمت", "فرمت فایل", "نوع فایل", "قالب", "format"),
        max_values=3,
    ),
    FieldSpec(
        key="pages",
        kind=KIND_NUMBER,
        column="Pages",
        labels=("تعداد صفحات", "تعداد صفحه", "شمار صفحات", "صفحات", "pages"),
        min_value=5,
        max_value=5000,
        tolerance=10,
    ),
    FieldSpec(
        key="questions",
        kind=KIND_NUMBER,
        column="تعداد سوالات",
        labels=("تعداد سوال", "تعداد نمونه سوال", "تعداد پرسش", "سوالات"),
        min_value=1,
        max_value=10000,
        tolerance=0,
    ),
    FieldSpec(
        key="computer_code",
        kind=KIND_TEXT,
        column="کد رایانه",
        labels=("کد رایانه ای", "کد استاندارد", "کد شغل", "کد ملی آموزش", "کد دوره"),
    ),
    FieldSpec(
        key="book_name",
        kind=KIND_TEXT,
        column="نام کتاب",
        labels=("اسم کتاب", "کتاب مرجع", "منبع", "نام منبع"),
    ),
    FieldSpec(
        key="publisher",
        kind=KIND_TEXT,
        column="ناشر",
        labels=("انتشارات", "publisher"),
        required=False,
    ),
    FieldSpec(
        key="year",
        kind=KIND_NUMBER,
        column="سال انتشار",
        labels=("سال", "سال چاپ", "تاریخ انتشار", "year"),
        min_value=1300,
        max_value=2100,
        required=False,
    ),
    FieldSpec(
        key="level",
        kind=KIND_CHOICE,
        column="مقطع",
        labels=("مقطع تحصیلی", "رشته", "پایه", "سطح"),
        required=False,
    ),
    FieldSpec(
        key="fields_of_study",
        kind=KIND_MULTI,
        column="مناسب رشته",
        labels=("رشته های مرتبط", "مناسب برای", "رشته تحصیلی", "کاربرد"),
        max_values=5,
        required=False,
    ),
    FieldSpec(
        key="image",
        kind=KIND_IMAGE,
        column="Image",
        labels=("تصویر", "عکس", "کاور", "لینک تصویر", "image"),
    ),
    FieldSpec(
        key="status",
        kind=KIND_SKIP,
        column="Status",
        labels=("وضعیت", "استاتوس", "status"),
        required=False,
    ),
    FieldSpec(
        key="product_id",
        kind=KIND_SKIP,
        column="Product ID",
        labels=("شناسه محصول", "آیدی محصول", "product_id", "product id"),
        required=False,
    ),
)

_BY_LABEL: dict[str, FieldSpec] = {}
for _spec in BUILTIN:
    for _label in (_spec.column, *_spec.labels):
        _BY_LABEL.setdefault(label_key(_label), _spec)


# ---------------------------------------------------------------------------
# حدس نوع از روی عنوان ستون
# ---------------------------------------------------------------------------

#: الگوهای عنوان ستون → نوع فیلد. ترتیب مهم است؛ اولین تطبیق برنده است.
KIND_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (KIND_IMAGE, ("تصویر", "عکس", "کاور", "image", "picture", "cover")),
    (KIND_SUMMARY, ("خلاصه", "توضیح", "معرفی", "شرح", "چکیده", "summary", "description")),
    (KIND_BOOL, ("ایا", "دارای", "داشتن", "همراه", "دارد", "has")),
    (KIND_NUMBER, ("تعداد", "شمار", "سال", "قیمت", "حجم", "مدت", "count", "number", "pages")),
    (KIND_TEXT, ("کد", "شناسه", "شماره", "code", "id")),
    (KIND_PERSON, ("نویسنده", "مولف", "مترجم", "مدرس", "استاد", "گردآورنده", "author")),
    (KIND_MULTI, ("دسته", "تگ", "برچسب", "رشته", "مناسب", "ژانر", "موضوع", "category", "tag")),
    (KIND_KEYWORD, ("کلمه کلیدی", "کیورد", "keyword")),
)

_DIGIT_RE = re.compile(r"\d")


def infer_kind(column: str) -> str:
    """نوع یک ستون ناشناخته را از روی نامش حدس می‌زند.

    حدس‌ها محافظه‌کارانه‌اند و پیش‌فرضْ «متن» است: نوشتن یک مقدار متنی در
    ستونی که عدد می‌خواست، بدتر از خالی گذاشتنش نیست، ولی حدسِ عجیب می‌تواند
    مقدار بی‌ربط بنویسد.
    """
    key = label_key(column)
    for kind, hints in KIND_HINTS:
        if any(label_key(hint) in key for hint in hints):
            return kind
    return KIND_TEXT


def spec_for(
    column: str,
    options: Sequence[str] = (),
    overrides: dict[str, Any] | None = None,
) -> FieldSpec:
    """عنوان ستون شیت → :class:`FieldSpec`.

    ``options`` گزینه‌های همان ستون در تبِ «لیست‌ها»ست؛ اگر چیزی داشته باشد،
    ستون خودبه‌خود کرکره‌ای می‌شود (چون کاربر برایش فهرست تعریف کرده) و
    مقدارهای نوشته‌شده حتماً از همان فهرست خواهند بود.
    """
    column = str(column).strip()
    base = _BY_LABEL.get(label_key(column))
    if base is not None:
        spec = FieldSpec(
            key=base.key,
            kind=base.kind,
            column=column,
            labels=base.labels,
            options=tuple(options) or base.options,
            synonyms=dict(base.synonyms),
            required=base.required,
            max_values=base.max_values,
            min_value=base.min_value,
            max_value=base.max_value,
            tolerance=base.tolerance,
            true_label=base.true_label,
            false_label=base.false_label,
            true_hints=base.true_hints,
        )
    else:
        spec = FieldSpec(
            key=label_key(column) or column,
            kind=infer_kind(column),
            column=column,
            labels=(),
            options=tuple(options),
            # ستون ناشناخته نباید ردیف را «ناقص» کند: هنوز نمی‌دانیم منابع
            # اصلاً چنین چیزی می‌نویسند یا نه.
            required=False,
        )

    if options and spec.kind in {KIND_TEXT, KIND_NUMBER, KIND_CHOICE, KIND_MULTI}:
        # فهرست دو گزینه‌ای معمولاً «دارد/ندارد» است، نه کرکره‌ی چندتایی
        values = [str(item).strip() for item in options if str(item).strip()]
        if spec.kind not in {KIND_MULTI, KIND_CHOICE}:
            spec.kind = KIND_CHOICE if len(values) <= 2 else KIND_MULTI
        spec.options = tuple(values)
    if spec.kind == KIND_BOOL and options:
        values = [str(item).strip() for item in options if str(item).strip()]
        if len(values) >= 2:
            spec.true_label, spec.false_label = values[0], values[1]
    return spec


def apply_overrides(spec: FieldSpec, data: dict[str, Any] | None) -> FieldSpec:
    """اعمال تنظیمات دستی یک ستون از ``config.yaml``.

    برای وقتی حدس خودکار کافی نیست: «این ستون عدد است»، «این برچسب‌ها را هم
    بگرد»، «خالی ماندنش نقص است».
    """
    if not data:
        return spec
    if data.get("kind"):
        spec.kind = str(data["kind"])
    if data.get("labels"):
        spec.labels = tuple(dict.fromkeys([*spec.labels, *[str(x) for x in data["labels"]]]))
    if data.get("options"):
        spec.options = tuple(str(x) for x in data["options"])
    if data.get("synonyms"):
        spec.synonyms = {str(k): list(v) for k, v in dict(data["synonyms"]).items()}
    for name in ("max_values", "min_value", "max_value", "tolerance"):
        if data.get(name) is not None:
            setattr(spec, name, int(data[name]))
    if data.get("required") is not None:
        spec.required = bool(data["required"])
    for name in ("true_label", "false_label"):
        if data.get(name):
            setattr(spec, name, str(data[name]))
    if data.get("true_hints"):
        spec.true_hints = tuple(str(x) for x in data["true_hints"])
    return spec


# ---------------------------------------------------------------------------
# نقشه‌ی ستون‌های یک شیت
# ---------------------------------------------------------------------------


@dataclass
class FieldPlan:
    """همه‌ی ستون‌های یک شیت، به ترتیب خودش."""

    specs: list[FieldSpec] = field(default_factory=list)
    #: ``کلید فیلد → شماره‌ی ستون (۰-پایه)``
    columns: dict[str, int] = field(default_factory=dict)

    @property
    def title_key(self) -> str:
        for spec in self.specs:
            if spec.kind == KIND_TITLE:
                return spec.key
        return ""

    def writable(self, never_write: Sequence[str] = ()) -> list[FieldSpec]:
        blocked = {label_key(name) for name in never_write}
        return [
            spec
            for spec in self.specs
            if spec.writable
            and spec.key not in never_write
            and label_key(spec.column) not in blocked
            and label_key(spec.key) not in blocked
        ]

    def by_key(self, key: str) -> FieldSpec | None:
        for spec in self.specs:
            if spec.key == key:
                return spec
        return None

    def describe(self) -> dict[str, str]:
        """برای لاگ و پنل: ``{ستون: نوع}``."""
        return {spec.column: spec.kind for spec in self.specs}


#: نام‌های نوع که کاربر ممکن است در config بنویسد
KIND_ALIASES = {
    "عدد": KIND_NUMBER,
    "متن": KIND_TEXT,
    "شخص": KIND_PERSON,
    "خلاصه": KIND_SUMMARY,
    "کرکره": KIND_CHOICE,
    "چندانتخابی": KIND_MULTI,
    "بله_خیر": KIND_BOOL,
    "تصویر": KIND_IMAGE,
    "کلمه_کلیدی": KIND_KEYWORD,
    "عنوان": KIND_TITLE,
    "دست_نزن": KIND_SKIP,
}


def build_plan(
    header: Sequence[str],
    lists: dict[str, list[str]] | None = None,
    overrides: dict[str, Any] | None = None,
    title_column: str = "",
) -> FieldPlan:
    """سطر عنوانِ شیت → نقشه‌ی کامل ستون‌ها.

    ``lists`` خروجی تبِ «لیست‌ها»ست (``{عنوان ستون: [گزینه‌ها]}``) و
    ``overrides`` کلید ``details.columns`` در config برای ستون‌هایی که حدس
    خودکار برایشان کافی نبوده.
    """
    lists = lists or {}
    overrides = overrides or {}
    by_key = {label_key(name): value for name, value in overrides.items()}
    option_map = {label_key(name): values for name, values in lists.items()}

    plan = FieldPlan()
    seen: set[str] = set()
    for index, raw in enumerate(header):
        column = str(raw or "").strip()
        if not column:
            continue
        spec = spec_for(column, option_map.get(label_key(column), ()))
        override = by_key.get(label_key(column)) or by_key.get(label_key(spec.key))
        if isinstance(override, str):
            override = {"kind": KIND_ALIASES.get(override, override)}
        if isinstance(override, dict):
            data = dict(override)
            if data.get("kind"):
                data["kind"] = KIND_ALIASES.get(str(data["kind"]), data["kind"])
            spec = apply_overrides(spec, data)
        if title_column and label_key(title_column) == label_key(column):
            spec.kind = KIND_TITLE
        if spec.key in seen:  # دو ستون هم‌نام: دومی کلید یکتای خودش را می‌گیرد
            spec.key = f"{spec.key}_{index}"
        seen.add(spec.key)
        plan.specs.append(spec)
        plan.columns[spec.key] = index

    if not plan.title_key and plan.specs:
        # ستون عنوان پیدا نشد: ستون اول عنوان است (قرارداد همه‌ی شیت‌های شما)
        plan.specs[0].kind = KIND_TITLE
    return plan
