"""نگاشت برچسب‌های منابع به کرکره‌های گوگل‌شیت (ستون‌های «دسته» و «تگ»).

مسئله: هر سایت برچسب خودش را دارد («عاشقانه»، «رمان عشقی»، «romance») ولی
ستون‌های شیت کرکره‌ی محدود دارند و اسکریپت درج محصول فقط همان مقدارهای مجاز
را می‌پذیرد. پس هیچ‌وقت متن خام منبع در شیت نوشته نمی‌شود؛ اول به یکی از
گزینه‌های مجاز نگاشت می‌شود و اگر نگاشتی پیدا نشد، سلول خالی می‌ماند.

فهرست گزینه‌های مجاز از سه جا می‌آید، به همین ترتیب:

1. تبِ «لیست‌ها»ی خودِ گوگل‌شیت (دقیق‌ترین — همان چیزی که کرکره از آن ساخته شده)
2. کلید ``details.taxonomy`` در ``config.yaml``
3. فهرست پیش‌فرض همین ماژول

چون ستون‌ها چندانتخابی‌اند (اسکریپت اپ‌اسکریپت کاربر)، خروجی چند مقدار است
که با جداکننده‌ی قابل تنظیم به هم می‌چسبند.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .details import label_key

#: وزن هر منبعِ نشانه در امتیازدهی
WEIGHT_TERM = 3.0  # برچسب خودِ سایت منبع (قوی‌ترین نشانه)
WEIGHT_TITLE = 2.0  # کلمه در عنوان محصول
WEIGHT_TEXT = 1.0  # کلمه در خلاصه

#: کمینه‌ی امتیاز برای اینکه یک گزینه در شیت نوشته شود
MIN_SCORE = 2.0

#: کلمه‌های عمومی که خودشان تفکیک‌کننده نیستند و از کلید مقایسه حذف می‌شوند
GENERIC_PREFIXES: tuple[str, ...] = ("رمان", "کتاب", "داستان", "ژانر")


def _variants(label: str) -> set[str]:
    """کلیدهای مقایسه‌ی یک گزینه: خودش و شکل بدون پیشوند عمومی.

    «رمان عاشقانه» باید هم با برچسب «رمان عاشقانه» بخورد و هم با «عاشقانه».
    """
    key = label_key(label)
    out = {key}
    for prefix in (label_key(word) for word in GENERIC_PREFIXES):
        if key.startswith(prefix) and len(key) > len(prefix) + 2:
            out.add(key[len(prefix) :])
    return {item for item in out if len(item) >= 3}


@dataclass
class Vocabulary:
    """گزینه‌های مجاز یک ستون کرکره‌ای، با مترادف‌هایشان."""

    name: str
    options: list[str] = field(default_factory=list)
    #: ``گزینه‌ی مجاز → عبارت‌هایی که همان را می‌رسانند``
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    max_values: int = 3
    min_score: float = MIN_SCORE

    def keys_of(self, option: str) -> set[str]:
        keys = _variants(option)
        for synonym in self.synonyms.get(option, []):
            keys |= _variants(synonym)
        return keys

    def score(
        self, terms: Sequence[str] = (), title: str = "", text: str = ""
    ) -> list[tuple[str, float]]:
        """امتیاز هر گزینه بر اساس برچسب منابع، عنوان و خلاصه."""
        title_key = label_key(title)
        text_key = label_key(text)
        term_keys = [label_key(term) for term in terms if term]
        scored: list[tuple[str, float]] = []
        for option in self.options:
            keys = self.keys_of(option)
            if not keys:
                continue
            points = 0.0
            for index, term in enumerate(term_keys):
                if any(key == term or key in term for key in keys):
                    # برچسب‌های پررأی‌تر اول آمده‌اند؛ اثرشان کمی بیشتر است
                    points += WEIGHT_TERM if index < 5 else WEIGHT_TERM / 2
            if any(key in title_key for key in keys):
                points += WEIGHT_TITLE
            if text_key and any(key in text_key for key in keys):
                points += WEIGHT_TEXT
            if points:
                scored.append((option, points))
        scored.sort(key=lambda item: (-item[1], self.options.index(item[0])))
        return scored

    def match(
        self, terms: Sequence[str] = (), title: str = "", text: str = ""
    ) -> list[str]:
        """گزینه‌های مجازی که این محصول واقعاً به آن‌ها می‌خورد."""
        return [
            option
            for option, points in self.score(terms, title, text)
            if points >= self.min_score
        ][: self.max_values]


# ---------------------------------------------------------------------------
# فهرست‌های پیش‌فرض
# ---------------------------------------------------------------------------

#: دسته‌های رایج رمان فارسی. اگر تبِ «لیست‌ها»ی شیت پر باشد، این‌ها کنار
#: می‌روند — این فقط برای اجرای اول و بدون تنظیمات است.
DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "رمان عاشقانه": ["عاشقانه", "عشقی", "romance", "رمان عشقی"],
    "رمان اجتماعی": ["اجتماعی", "خانوادگی", "درام اجتماعی"],
    "رمان معمایی": ["معمایی", "رازآلود", "معما", "mystery"],
    "رمان جنایی و پلیسی": ["جنایی", "پلیسی", "کارآگاهی", "crime", "police"],
    "رمان هیجان‌انگیز": ["هیجان انگیز", "هیجانی", "پرکشش", "thriller", "تعلیق"],
    "رمان تاریخی": ["تاریخی", "historical"],
    "رمان فانتزی": ["فانتزی", "تخیلی", "fantasy", "علمی تخیلی"],
    "رمان ترسناک": ["ترسناک", "وحشت", "horror"],
    "رمان طنز": ["طنز", "کمدی", "خنده دار", "comedy"],
    "رمان درام": ["درام", "غمگین", "تراژدی", "drama"],
    "رمان خارجی": ["خارجی", "ترجمه", "ترجمه شده"],
    "رمان ایرانی": ["ایرانی", "فارسی", "نویسنده ایرانی"],
}

#: تگ‌های رایج. تگ‌ها معمولاً حال‌وهوای رمان را می‌گویند، نه ژانرش را.
DEFAULT_TAGS: dict[str, list[str]] = {
    "رمان بدون سانسور": ["بدون سانسور", "بی سانسور", "سانسور نشده"],
    "رمان بزرگسال": ["بزرگسال", "+۱۸", "+18", "مخصوص بزرگسالان"],
    "رمان کوتاه": ["کوتاه", "داستان کوتاه"],
    "رمان دنباله‌دار": ["دنباله دار", "چند جلدی", "مجموعه", "جلد دوم", "سری"],
    "رمان پرفروش": ["پرفروش", "محبوب", "پرطرفدار", "best seller"],
    "رمان جدید": ["جدید", "تازه منتشر شده"],
    "رمان کامل": ["کامل", "تمام شده", "پایان یافته"],
    "رمان دانشجویی": ["دانشجویی", "استاد دانشجو", "دانشگاه"],
    "رمان ارباب رعیتی": ["ارباب", "رعیت", "ارباب رعیتی"],
    "رمان انتقامی": ["انتقام", "انتقامی", "کینه"],
}


def build(
    name: str,
    options: Iterable[str] | None = None,
    synonyms: dict[str, list[str]] | None = None,
    defaults: dict[str, list[str]] | None = None,
    max_values: int = 3,
    min_score: float = MIN_SCORE,
) -> Vocabulary:
    """ساخت واژگان یک ستون از گزینه‌های شیت + مترادف‌های config + پیش‌فرض‌ها.

    گزینه‌های مجاز **همیشه** از فهرست کاربر می‌آیند؛ مترادف‌های پیش‌فرض فقط
    وقتی به کار می‌آیند که همان گزینه در فهرست کاربر هم باشد. این‌طوری
    اضافه‌کردن مترادف هیچ‌وقت مقداری خارج از کرکره تولید نمی‌کند.
    """
    defaults = defaults or {}
    chosen = [str(option).strip() for option in (options or []) if str(option).strip()]
    if not chosen:
        chosen = list(defaults)
    merged: dict[str, list[str]] = {}
    for option in chosen:
        words = list(defaults.get(option, []))
        for extra in (synonyms or {}).get(option, []):
            if extra not in words:
                words.append(extra)
        merged[option] = words
    return Vocabulary(
        name=name,
        options=chosen,
        synonyms=merged,
        max_values=max_values,
        min_score=min_score,
    )


# ---------------------------------------------------------------------------
# تبِ «لیست‌ها»
# ---------------------------------------------------------------------------

#: عنوان ستون‌های تبِ لیست‌ها → نام فیلد
LIST_HEADERS: dict[str, tuple[str, ...]] = {
    "categories": ("دسته", "دسته بندی", "دسته بندی ها", "کتگوری", "categories", "category"),
    "tags": ("تگ", "تگ ها", "برچسب", "برچسب ها", "tags", "tag"),
    "nationality": ("ملیت", "nationality"),
    "book_format": ("فرمت", "قالب", "format"),
}

_HEADER_LOOKUP = {
    label_key(alias): field_name
    for field_name, aliases in LIST_HEADERS.items()
    for alias in aliases
}


def options_from_lists_tab(rows: Sequence[Sequence[str]]) -> dict[str, list[str]]:
    """جدول تبِ «لیست‌ها» → ``{عنوان ستون: [گزینه‌های مجاز]}``.

    هر ستون یک کرکره است: سطر اول عنوان و بقیه گزینه‌ها. **هیچ ستونی نادیده
    گرفته نمی‌شود**؛ حتی ستونی که کد اسمش را نشنیده («مناسب رشته»، «مقطع»)
    هم گزینه‌هایش برداشته می‌شود، چون همان ستون در شیت محصولات کرکره دارد.

    برای ستون‌های شناخته‌شده، کلید داخلی هم اضافه می‌شود (``categories``،
    ``tags``، ...) تا کدی که با نام داخلی کار می‌کند هم جواب بگیرد.
    """
    if not rows:
        return {}
    header = list(rows[0])
    out: dict[str, list[str]] = {}
    for index, cell in enumerate(header):
        column = str(cell or "").strip()
        if not column:
            continue
        values: list[str] = []
        for row in rows[1:]:
            value = str(row[index]).strip() if index < len(row) else ""
            if value and value not in values:
                values.append(value)
        if not values:
            continue
        out[column] = values
        alias = _HEADER_LOOKUP.get(label_key(column))
        if alias and alias not in out:
            out[alias] = values
    return out


_SPLIT = re.compile(r"[،,;|/]+")


def split_values(text: str) -> list[str]:
    """محتوای یک سلول چندانتخابی → فهرست مقدارها."""
    return [piece.strip() for piece in _SPLIT.split(text or "") if piece.strip()]


def join_values(values: Sequence[str], separator: str = "، ") -> str:
    """فهرست مقدارها → همان شکلی که اسکریپت چندانتخابی شما در سلول می‌گذارد."""
    return separator.join(dict.fromkeys(value for value in values if value))
