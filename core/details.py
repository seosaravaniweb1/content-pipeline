"""استخراج «دیتیل»های یک محصول از **یک** صفحه‌ی منبع (ورودی فاز ۵).

فاز ۱ فقط عنوان را می‌گرفت. اینجا از همان صفحه‌ها هر چیزی که برای پر کردن
گوگل‌شیت محصولات لازم است بیرون کشیده می‌شود: نویسنده، مترجم، خلاصه، تعداد
صفحات، تگ و دسته، فرمت، ملیت و کاندیداهای تصویر.

ترتیب اعتماد به منابع داده‌ی یک صفحه:

1. ``JSON-LD`` (``Book`` / ``Product``) — ساخت‌یافته و کم‌غلط
2. جدول/فهرست مشخصات («نویسنده: آوا»، «تعداد صفحات | ۳۹۸»)
3. بخش «خلاصه»ی خودِ صفحه
4. ``og:description`` و ``meta description`` — آخرین چاره

هیچ سرویس پولی و هیچ کلید API در کار نیست؛ همه‌چیز از HTML همان صفحه‌ای
می‌آید که فاز ۱ هم می‌گرفت. تصمیم‌گیری بین چند منبع کارِ
:mod:`core.consensus` است، نه این ماژول: اینجا فقط «این صفحه چه می‌گوید»
ثبت می‌شود.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from . import extract
from .normalizer import ZWNJ, latin_digits, normalize_display

# ---------------------------------------------------------------------------
# کلید مقایسه‌ی برچسب‌ها
# ---------------------------------------------------------------------------


def label_key(text: str) -> str:
    """برچسب → کلید مقایسه: «تعداد صفحات» و «تعدادصفحات» و «تعداد صفحه‌ها» یکی شوند."""
    return normalize_display(text or "").replace(ZWNJ, "").replace(" ", "")


def _keys(*labels: str) -> tuple[str, ...]:
    return tuple(label_key(label) for label in labels)


#: برچسب‌های جدول مشخصات در فروشگاه‌های فارسی. هر فیلد چند نام دارد.
LABELS: dict[str, tuple[str, ...]] = {
    "author": _keys(
        "نویسنده",
        "نویسنده رمان",
        "نویسنده کتاب",
        "نام نویسنده",
        "نویسندگان",
        "مولف",
        "مؤلف",
        "پدیدآور",
        "پدید آورنده",
        "به قلم",
        "نویسنده اثر",
        "author",
        "writer",
    ),
    "translator": _keys(
        "مترجم",
        "مترجمان",
        "نام مترجم",
        "ترجمه",
        "ترجمه از",
        "برگردان",
        "translator",
    ),
    "pages": _keys(
        "تعداد صفحات",
        "تعداد صفحه",
        "تعداد صفحه ها",
        "شمار صفحات",
        "صفحات",
        "صفحه",
        "حجم کتاب",
        "تعداد برگ",
        "pages",
        "pagecount",
    ),
    "format": _keys(
        "فرمت",
        "فرمت فایل",
        "فرمت کتاب",
        "نوع فایل",
        "قالب فایل",
        "نوع کتاب",
        "format",
    ),
    "nationality": _keys("ملیت", "ملیت رمان", "ملیت نویسنده", "کشور", "nationality"),
    "genre": _keys("ژانر", "سبک", "موضوع", "دسته", "دسته بندی", "ژانر رمان", "genre"),
    "publisher": _keys("ناشر", "انتشارات", "publisher"),
}

#: عنوان‌هایی که زیرشان خلاصه‌ی رمان نوشته می‌شود
SUMMARY_HEADINGS: tuple[str, ...] = _keys(
    "خلاصه",
    "خلاصه رمان",
    "خلاصه کتاب",
    "خلاصه داستان",
    "خلاصه ای از رمان",
    "معرفی",
    "معرفی رمان",
    "معرفی کتاب",
    "درباره رمان",
    "درباره کتاب",
    "درباره این کتاب",
    "توضیحات",
    "توضیحات محصول",
    "بخشی از رمان",
    "بخشی از کتاب",
    "قسمتی از رمان",
    "چکیده",
    "description",
    "summary",
)

#: جمله‌هایی که خلاصه نیستند — تبلیغ، راهنمای دانلود و فوتر
SUMMARY_NOISE: tuple[str, ...] = (
    "دانلود",
    "لینک",
    "تومان",
    "ریال",
    "قیمت",
    "خرید",
    "سبد",
    "پرداخت",
    "درگاه",
    "عضویت",
    "ثبت نام",
    "ورود",
    "تلگرام",
    "اینستاگرام",
    "واتساپ",
    "کانال",
    "حجم فایل",
    "رمز فایل",
    "پشتیبانی",
    "کلیک",
    "کلیه حقوق",
    "تمامی حقوق",
    "امتیاز",
    "دیدگاه",
    "نظرات کاربران",
    "اشتراک",
    "سفارش",
    "http",
    "www.",
)

#: نشانه‌های «این رمان خارجی است» و «این رمان ایرانی است»
FOREIGN_HINTS: tuple[str, ...] = _keys(
    "خارجی", "ترجمه", "ترجمه شده", "رمان خارجی", "رمان ترجمه", "foreign"
)
IRANIAN_HINTS: tuple[str, ...] = _keys("ایرانی", "رمان ایرانی", "فارسی", "iranian")

AUDIO_HINTS: tuple[str, ...] = _keys("صوتی", "کتاب صوتی", "نسخه صوتی", "فایل صوتی", "audio")
PDF_HINTS: tuple[str, ...] = _keys("pdf", "پی دی اف", "پیدیاف")

#: نویسنده‌ی نامشخص — نه نام است و نه باید در شیت بنشیند
UNKNOWN_PERSON: tuple[str, ...] = _keys(
    "ناشناس", "نامشخص", "بی نام", "بدون نام", "گمنام", "نامعلوم", "anonymous", "unknown", "-"
)

MIN_PAGES = 5
MAX_PAGES = 5000


# ---------------------------------------------------------------------------
# داده‌ی خروجی
# ---------------------------------------------------------------------------


@dataclass
class ImageCandidate:
    """یک تصویر کاندیدا با هر چه از خودِ صفحه درباره‌اش می‌دانیم."""

    url: str
    width: int = 0
    height: int = 0
    alt: str = ""
    #: ۳ = تصویر اصلی محصول، ۲ = گالری، ۱ = داخل متن
    priority: int = 1

    @property
    def known_size(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass
class SummaryBlock:
    """یک پاراگراف خلاصه، با درجه‌ی اعتماد به جای پیدا شدنش."""

    text: str
    #: ۳ = بخش «خلاصه»ی صفحه، ۲ = JSON-LD، ۱ = متا دیسکریپشن
    rank: int = 1


@dataclass
class PageDetails:
    """آنچه **یک** صفحه‌ی منبع درباره‌ی یک محصول می‌گوید."""

    url: str = ""
    domain: str = ""
    title: str = ""
    #: **همه‌ی** برچسب/مقدارهای این صفحه: ``کلید برچسب → [مقدارها]``.
    #: این دیکشنری عمومی است و به موضوع خاصی گره نخورده — ستون «تعداد سوالات»
    #: یا «کد رایانه» هم از همین‌جا درمی‌آید، بدون اینکه در کد اسمی از آن‌ها
    #: برده شده باشد. فیلدهای زیر فقط شکل آماده‌ی پرکاربردترین برچسب‌هایند.
    labeled: dict[str, list[str]] = field(default_factory=dict)
    authors: list[str] = field(default_factory=list)
    translators: list[str] = field(default_factory=list)
    #: همه‌ی عددهایی که این صفحه به‌عنوان تعداد صفحات داده (معمولاً یکی)
    page_counts: list[int] = field(default_factory=list)
    summaries: list[SummaryBlock] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    #: ``pdf`` / ``audio``
    formats: list[str] = field(default_factory=list)
    #: ``foreign`` / ``iranian``
    nationality: str = ""
    publisher: str = ""
    images: list[ImageCandidate] = field(default_factory=list)

    def values_for(self, labels: Sequence[str]) -> list[str]:
        """مقدارهای این صفحه برای هر مجموعه برچسب («تعداد سوالات»، «تعداد سوال»، ...).

        ورودی، برچسب‌های خام است نه کلید؛ نرمال‌سازی همین‌جا انجام می‌شود تا
        صداکننده لازم نباشد چیزی درباره‌ی شکل کلیدها بداند.
        """
        out: list[str] = []
        for label in labels:
            for value in self.labeled.get(label_key(label), []):
                if value not in out:
                    out.append(value)
        return out

    @property
    def pages(self) -> int | None:
        return self.page_counts[0] if self.page_counts else None

    @property
    def is_empty(self) -> bool:
        """صفحه‌ای که هیچ چیز قابل استفاده‌ای ندارد.

        ملاک، **برچسب‌های عمومی** است نه فیلدهای رمان: صفحه‌ی یک طرح توجیهی
        نه نویسنده دارد نه خلاصه، ولی «ظرفیت تولید» و «سرمایه‌گذاری»اش همان
        چیزی است که ستون‌های شیتِ آن موضوع می‌خواهند.
        """
        return not (
            self.labeled
            or self.summaries
            or self.tags
            or self.categories
            or self.images
            or self.page_counts
        )

    # -- سریال‌سازی برای کش دیتابیس ----------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageDetails":
        data = dict(data or {})
        images = [ImageCandidate(**item) for item in data.pop("images", []) or []]
        summaries = [SummaryBlock(**item) for item in data.pop("summaries", []) or []]
        known = {f for f in cls.__dataclass_fields__ if f not in {"images", "summaries"}}
        clean = {key: value for key, value in data.items() if key in known}
        return cls(**clean, images=images, summaries=summaries)


# ---------------------------------------------------------------------------
# متن بلوکی
# ---------------------------------------------------------------------------

_BLOCK_END = re.compile(
    r"</(?:p|div|section|article|li|ul|ol|tr|td|th|dt|dd|h[1-6]|br|table|span|strong|b)\s*>"
    r"|<br\s*/?>",
    re.I,
)
_HEADING_RE = re.compile(r"<(h[1-6])[^>]*>(.*?)</\1>", re.S | re.I)


def block_lines(page_html: str, limit: int = 4000) -> list[str]:
    """HTML → خطوط متنی، با حفظ مرز بلوک‌ها.

    مرزها مهم‌اند: «نویسنده» و «آوا» در جدول مشخصات دو سلول‌اند و اگر همه‌ی
    صفحه یک خط شود، دیگر نمی‌فهمیم کدام مقدارِ کدام برچسب است.
    """
    if not page_html:
        return []
    body = extract._SCRIPT_RE.sub(" ", page_html)
    # جداکننده باید از ``strip_tags`` جان سالم ببرد؛ آن تابع هر فضای سفیدی
    # (از جمله خط جدید) را به یک فاصله تبدیل می‌کند، پس نویسه‌ی صفر می‌گذاریم.
    body = _BLOCK_END.sub("\x00", body)
    out: list[str] = []
    for chunk in extract.strip_tags(body).split("\x00"):
        # دونقطه عمداً حذف نمی‌شود: در قالب‌هایی که برچسب و مقدار در دو عنصر
        # جدا هستند («<span>نویسنده:</span><span>آوا</span>») تنها نشانه‌ای
        # است که می‌گوید این خط برچسب است نه مقدار.
        text = chunk.strip(" \t،-–—|").lstrip(":")
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


_SEPARATOR = re.compile(r"\s*[:：]\s*|\s+[-–—]\s+|\s*\|\s*")

#: بلندترین چیزی که هنوز «برچسب» است، نه جمله
MAX_LABEL_CHARS = 40
MAX_LABEL_WORDS = 6
#: بلندترین چیزی که هنوز «مقدار یک برچسب» است، نه پاراگراف
MAX_VALUE_CHARS = 160
#: سقف تعداد برچسب‌های یک صفحه (جلوی باد کردن کش را می‌گیرد)
MAX_LABELS = 80

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
_DL_RE = re.compile(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", re.S | re.I)


def collect_labels(page_html: str, lines: Sequence[str] | None = None) -> dict[str, list[str]]:
    """**همه‌ی** برچسب/مقدارهای یک صفحه: ``{کلید برچسب: [مقدارها]}``.

    اینجا هیچ فهرست ثابتی از فیلدها در کار نیست و همین نکته‌ی اصلی است: شیت
    یک موضوع ستون «تعداد سوالات» دارد و شیت موضوع دیگر «مناسب رشته»؛ کد نباید
    اسم هیچ‌کدام را بداند. هر چیزی که در صفحه شکلِ «برچسب → مقدار» داشته باشد
    برداشته می‌شود و بعد هر ستون، برچسب‌های خودش را از این دیکشنری برمی‌دارد.

    سه شکل رایج پوشش داده می‌شود:

    * ردیف جدول مشخصات: ``<tr><th>تعداد سوالات</th><td>۶۰</td></tr>``
    * فهرست تعریفی: ``<dt>کد رایانه</dt><dd>۱۲۳۴</dd>``
    * خط متنی: ``کد رایانه: ۱۲۳۴`` و شکل دوتکه‌اش (``کد رایانه:`` و مقدار در
      عنصر بعدی، که در قالب‌های وردپرسی خیلی دیده می‌شود)
    """
    found: dict[str, list[str]] = {}

    def remember(label: str, value: str) -> None:
        label = (label or "").strip(" \t:،-–—|")
        value = (value or "").strip(" \t:،-–—|")
        key = label_key(label)
        if not key or not value or len(found) >= MAX_LABELS:
            return
        if len(label) > MAX_LABEL_CHARS or len(label.split()) > MAX_LABEL_WORDS:
            return
        if len(value) > MAX_VALUE_CHARS:
            value = value[:MAX_VALUE_CHARS].rstrip()
        bucket = found.setdefault(key, [])
        if value not in bucket and len(bucket) < 5:
            bucket.append(value)

    for row in _ROW_RE.findall(page_html or ""):
        cells = [extract.strip_tags(cell) for cell in _CELL_RE.findall(row)]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            remember(cells[0], " ".join(cells[1:]))

    for label, value in _DL_RE.findall(page_html or ""):
        remember(extract.strip_tags(label), extract.strip_tags(value))

    rows = list(lines if lines is not None else block_lines(page_html))
    for index, line in enumerate(rows):
        if len(line) > MAX_LABEL_CHARS + MAX_VALUE_CHARS:
            continue  # جمله‌ی متن، نه سلول مشخصات
        head, separator, tail = _partition(line)
        if not separator:
            continue
        if tail.strip():
            remember(head, tail)
            continue
        # «نویسنده:» در یک عنصر و مقدارش در عنصر بعدی
        for candidate in rows[index + 1 : index + 2]:
            if candidate.strip() and not _partition(candidate)[1]:
                remember(head, candidate)
    return found


def values_for(labeled: dict[str, list[str]], labels: Iterable[str]) -> list[str]:
    """مقدارهای همه‌ی هم‌معنی‌های یک برچسب، بدون تکرار."""
    out: list[str] = []
    for label in labels:
        for value in labeled.get(label_key(label), []):
            if value not in out:
                out.append(value)
    return out


def labeled_values(lines: Iterable[str]) -> dict[str, list[str]]:
    """شکل قدیمی: ``{نام فیلد: [مقدارها]}`` بر اساس :data:`LABELS`.

    روی همان استخراج عمومی سوار است و فقط برچسب‌های شناخته‌شده‌ی رمان/کتاب را
    جدا می‌کند؛ برای کدی که فیلدهای آماده می‌خواهد.
    """
    labeled = collect_labels("", list(lines))
    out: dict[str, list[str]] = {}
    for field_name, aliases in LABELS.items():
        values = [value for key in aliases for value in labeled.get(key, [])]
        if values:
            out[field_name] = list(dict.fromkeys(values))
    return out


def _partition(line: str) -> tuple[str, str, str]:
    match = _SEPARATOR.search(line)
    if match is None:
        return line, "", ""
    return line[: match.start()], match.group(0), line[match.end() :]


# ---------------------------------------------------------------------------
# اشخاص (نویسنده/مترجم)
# ---------------------------------------------------------------------------

_PERSON_TRIM = re.compile(r"^(?:اثر|نوشته(?:ی)?|به قلم|قلم|از|by)\s+", re.I)
_PERSON_SPLIT = re.compile(r"[،,;؛/\\|]|\s+و\s+(?=[^\s]{2,})")
_PERSON_BAD = re.compile(r"\d|[«»\"'()\[\]{}]|@|https?:")


def clean_persons(raw: str, max_names: int = 3) -> list[str]:
    """«نوشته‌ی آوا محمدی، مهسا ک.» → ``['آوا محمدی', 'مهسا ک.']``.

    نام باید نام باشد: بدون رقم، بدون آدرس، حداکثر پنج کلمه. «ناشناس» و
    هم‌خانواده‌هایش حذف می‌شوند تا در شیت به‌جای نام، «نامشخص» ننشیند.
    """
    if not raw:
        return []
    out: list[str] = []
    for piece in _PERSON_SPLIT.split(normalize_display(raw)):
        name = _PERSON_TRIM.sub("", piece.strip(" \t.،-–—")).strip()
        if not name or _PERSON_BAD.search(name):
            continue
        if label_key(name) in UNKNOWN_PERSON:
            continue
        words = name.split()
        if not 1 <= len(words) <= 5 or len(name) < 2 or len(name) > 60:
            continue
        if name not in out:
            out.append(name)
        if len(out) >= max_names:
            break
    return out


_LATIN_LETTERS = re.compile(r"[A-Za-z]")


def is_latin_name(name: str) -> bool:
    """نام لاتین («Rina Kent») — قوی‌ترین نشانه‌ی رمان خارجی."""
    letters = [ch for ch in name if ch.isalpha()]
    if not letters:
        return False
    latin = sum(1 for ch in letters if _LATIN_LETTERS.match(ch))
    return latin / len(letters) > 0.6


# ---------------------------------------------------------------------------
# تعداد صفحات
# ---------------------------------------------------------------------------

_PAGES_PATTERNS = (
    re.compile(r"(?:تعداد\s*صفح\S*|شمار\s*صفح\S*)\D{0,12}?(\d{1,4})"),
    re.compile(r"(\d{2,4})\s*صفحه"),
    re.compile(r"صفحه\D{0,4}(\d{2,4})"),
)


def page_numbers(text: str) -> list[int]:
    """همه‌ی عددهایی که در این متن معنای «تعداد صفحات» دارند."""
    latin = latin_digits(text or "")
    out: list[int] = []
    for pattern in _PAGES_PATTERNS:
        for match in pattern.finditer(latin):
            try:
                value = int(match.group(1))
            except ValueError:  # pragma: no cover - گروه همیشه رقم است
                continue
            if MIN_PAGES <= value <= MAX_PAGES and value not in out:
                out.append(value)
    return out


# ---------------------------------------------------------------------------
# خلاصه
# ---------------------------------------------------------------------------

_META_DESCRIPTION = (
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
    r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:description["\']',
)


def summary_blocks(page_html: str, min_chars: int = 60) -> list[SummaryBlock]:
    """پاراگراف‌های خلاصه‌ی این صفحه، از معتبرترین جای ممکن.

    اول زیر عنوانِ «خلاصه»/«معرفی» را می‌گردیم (این‌ها متن واقعی رمان‌اند)،
    بعد ``description`` در JSON-LD، و در آخر متا دیسکریپشن که معمولاً کوتاه و
    بریده است.
    """
    blocks: list[SummaryBlock] = []
    seen: set[str] = set()

    def add(text: str, rank: int) -> None:
        text = _clean_sentence(text)
        if len(text) < min_chars or _is_noise(text):
            return
        finger = label_key(text)[:120]
        if finger in seen:
            return
        seen.add(finger)
        blocks.append(SummaryBlock(text=text, rank=rank))

    for section in _summary_sections(page_html):
        for line in block_lines(section, limit=60):
            add(line, 3)

    for node in extract.json_ld_blocks(page_html):
        for key in ("description", "abstract"):
            value = node.get(key)
            if isinstance(value, str):
                add(extract.strip_tags(value), 2)

    for pattern in _META_DESCRIPTION:
        match = re.search(pattern, page_html or "", re.I | re.S)
        if match:
            add(extract.strip_tags(match.group(1)), 1)
    return blocks


def _summary_sections(page_html: str, window: int = 9000) -> list[str]:
    """قطعه‌های HTMLِ بعد از هر عنوانِ «خلاصه» تا عنوان بعدی."""
    if not page_html:
        return []
    sections: list[str] = []
    for match in _HEADING_RE.finditer(page_html):
        if label_key(extract.strip_tags(match.group(2))) not in SUMMARY_HEADINGS:
            continue
        start = match.end()
        chunk = page_html[start : start + window]
        following = re.search(r"<h[1-6][\s>]", chunk, re.I)
        sections.append(chunk[: following.start()] if following else chunk)
    if not sections:
        # تب «توضیحات» وردپرس عنوان ندارد، فقط یک id
        marker = re.search(
            r'id=["\'](?:tab-description|description|tab-additional_information)["\']',
            page_html,
            re.I,
        )
        if marker:
            chunk = page_html[marker.end() : marker.end() + window]
            following = re.search(r"<h[1-6][\s>]", chunk, re.I)
            sections.append(chunk[: following.start()] if following else chunk)
    return sections


_SENTENCE_SPLIT = re.compile(r"(?<=[.!؟?…])\s+|\n+")


def _clean_sentence(text: str) -> str:
    text = extract.strip_tags(text or "").strip()
    return re.sub(r"\s{2,}", " ", text).strip(" \t«»\"'-–—")


def _is_noise(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in SUMMARY_NOISE)


def sentences(text: str) -> list[str]:
    """جمله‌های یک پاراگراف — ورودی خلاصه‌سازی چندمنبعی."""
    return [part.strip() for part in _SENTENCE_SPLIT.split(text or "") if part.strip()]


# ---------------------------------------------------------------------------
# تگ و دسته
# ---------------------------------------------------------------------------

_TAG_HREF = re.compile(r"/(?:product[-_]tag|product[-_]tags|tag|tags|brand)/", re.I)
_CAT_HREF = re.compile(r"/(?:product[-_]cat(?:egory)?|category|categories|daste)/", re.I)
_REL_TAG = re.compile(r'<a[^>]+rel=["\'][^"\']*\btag\b[^"\']*["\'][^>]*>(.*?)</a>', re.S | re.I)


def taxonomy_terms(page_html: str, base_url: str) -> tuple[list[str], list[str]]:
    """``(تگ‌ها، دسته‌ها)`` این صفحه — از لینک‌های تگ/دسته و JSON-LD.

    اینجا برچسب‌های خودِ سایت منبع درمی‌آید، نه دسته‌بندی ما؛ نگاشت به
    کرکره‌های گوگل‌شیت کارِ :mod:`core.taxonomy` است.
    """
    tags: list[str] = []
    categories: list[str] = []

    def add(bucket: list[str], text: str) -> None:
        text = _clean_term(text)
        if text and text not in bucket:
            bucket.append(text)

    for url, text in extract.extract_anchors(page_html, base_url):
        path = urllib.parse.urlsplit(url).path
        if _TAG_HREF.search(path):
            add(tags, text)
        elif _CAT_HREF.search(path):
            add(categories, text)

    for match in _REL_TAG.finditer(page_html or ""):
        add(tags, extract.strip_tags(match.group(1)))

    for node in extract.json_ld_blocks(page_html):
        for key, bucket in (("keywords", tags), ("genre", categories)):
            value = node.get(key)
            if isinstance(value, str):
                for piece in value.split(","):
                    add(bucket, piece)
            elif isinstance(value, list):
                for piece in value:
                    if isinstance(piece, str):
                        add(bucket, piece)
    return tags, categories


_TERM_BAD = re.compile(r"https?:|\d{4,}")


def _clean_term(text: str) -> str:
    text = _clean_sentence(text).strip("#")
    if not 2 <= len(text) <= 60 or _TERM_BAD.search(text):
        return ""
    if len(text.split()) > 6:
        return ""
    return text


# ---------------------------------------------------------------------------
# تصویر
# ---------------------------------------------------------------------------

_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_ATTR = re.compile(r'([a-zA-Z_:.-]+)\s*=\s*"([^"]*)"|([a-zA-Z_:.-]+)\s*=\s*\'([^\']*)\'')
_SIZE_IN_NAME = re.compile(r"[-_](\d{2,4})x(\d{2,4})\.(?:jpe?g|png|webp)(?:$|\?)", re.I)
_OG_IMAGE = (
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](.*?)["\']',
    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\'](.*?)["\']',
)
_IMAGE_EXT = re.compile(r"\.(?:jpe?g|png|webp)(?:$|\?)", re.I)


def image_candidates(page_html: str, base_url: str, limit: int = 24) -> list[ImageCandidate]:
    """کاندیداهای تصویر این صفحه، با هر اندازه‌ای که در HTML اعلام شده.

    اندازه از سه جا خوانده می‌شود: صفت‌های ``width``/``height``، صفت‌های
    ``data-large_image_*`` ووکامرس، و پسوند نام فایل وردپرس
    (``cover-600x600.jpg``). دانستن اندازه بدون دانلود یعنی می‌شود پیش از
    گرفتن فایل، تصویرهای غیرمربع را کنار گذاشت.
    """
    found: list[ImageCandidate] = []
    seen: set[str] = set()

    def add(url: str, width: int = 0, height: int = 0, alt: str = "", priority: int = 1) -> None:
        if not url or url.startswith("data:"):
            return
        absolute, _ = urllib.parse.urldefrag(urllib.parse.urljoin(base_url, url.strip()))
        if not _IMAGE_EXT.search(absolute) or absolute in seen:
            return
        if not width or not height:
            match = _SIZE_IN_NAME.search(absolute)
            if match:
                width, height = int(match.group(1)), int(match.group(2))
        seen.add(absolute)
        found.append(ImageCandidate(absolute, width, height, alt[:120], priority))

    for pattern in _OG_IMAGE:
        match = re.search(pattern, page_html or "", re.I)
        if match:
            add(match.group(1), priority=3)

    for node in extract.json_ld_blocks(page_html):
        value = node.get("image")
        if isinstance(value, str):
            add(value, priority=3)
        elif isinstance(value, dict):
            add(
                str(value.get("url") or ""),
                _int(value.get("width")),
                _int(value.get("height")),
                priority=3,
            )
        elif isinstance(value, list):
            for item in value[:4]:
                if isinstance(item, str):
                    add(item, priority=3)
                elif isinstance(item, dict):
                    add(str(item.get("url") or ""), priority=3)

    for match in _IMG_TAG.finditer(page_html or ""):
        attrs = _attrs(match.group(0))
        classes = attrs.get("class", "").lower()
        priority = 2 if ("wp-post-image" in classes or "product" in classes) else 1
        url = (
            attrs.get("data-large_image")
            or attrs.get("data-src")
            or attrs.get("data-lazy-src")
            or attrs.get("src")
            or ""
        )
        add(
            url,
            _int(attrs.get("data-large_image_width") or attrs.get("width")),
            _int(attrs.get("data-large_image_height") or attrs.get("height")),
            attrs.get("alt", ""),
            priority,
        )
        for entry in attrs.get("srcset", "").split(","):
            add(entry.strip().split(" ")[0], alt=attrs.get("alt", ""), priority=priority)
        if len(found) >= limit:
            break
    return found[:limit]


def _attrs(tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _ATTR.finditer(tag):
        name = (match.group(1) or match.group(3) or "").lower()
        value = match.group(2) if match.group(2) is not None else (match.group(4) or "")
        if name:
            out[name] = value
    return out


def _int(value: object) -> int:
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------

_PERSON_KEYS = {"author": "authors", "translator": "translators", "creator": "authors"}


def _json_ld_people(node: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, target in _PERSON_KEYS.items():
        value = node.get(key)
        names: list[str] = []
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                names.extend(clean_persons(item))
            elif isinstance(item, dict):
                names.extend(clean_persons(str(item.get("name") or "")))
        if names:
            out.setdefault(target, []).extend(names)
    return out


# ---------------------------------------------------------------------------
# استخراج کامل یک صفحه
# ---------------------------------------------------------------------------


def extract_details(
    page_html: str,
    url: str,
    site_name: str = "",
    title_selector: str | None = None,
) -> PageDetails:
    """همه‌ی دیتیل‌های قابل استخراج از یک صفحه‌ی محصول."""
    domain = urllib.parse.urlsplit(url).netloc.lower()
    details = PageDetails(
        url=url,
        domain=domain,
        title=extract.extract_title(page_html, title_selector, domain, site_name),
    )
    if not page_html:
        return details

    lines = block_lines(page_html)
    # همه‌ی برچسب‌های صفحه ذخیره می‌شوند (هر ستونی از هر شیتی بعداً از همین
    # دیکشنری مقدارش را برمی‌دارد)، و بعد فیلدهای آماده از رویشان ساخته می‌شوند.
    details.labeled = collect_labels(page_html, lines)
    labeled = {
        name: values_for(details.labeled, aliases) for name, aliases in LABELS.items()
    }
    blocks = extract.json_ld_blocks(page_html)

    # -- اشخاص --------------------------------------------------------------
    for node in blocks:
        for target, names in _json_ld_people(node).items():
            _extend(getattr(details, target), names)
    for value in labeled.get("author", []):
        _extend(details.authors, clean_persons(value))
    for value in labeled.get("translator", []):
        _extend(details.translators, clean_persons(value))

    # -- تعداد صفحات ---------------------------------------------------------
    for node in blocks:
        for key in ("numberOfPages", "numberofpages", "pageCount"):
            value = _int(node.get(key))
            if MIN_PAGES <= value <= MAX_PAGES:
                _extend(details.page_counts, [value])
    for value in labeled.get("pages", []):
        _extend(details.page_counts, page_numbers(value) or page_numbers(f"صفحه {value}"))
    if not details.page_counts:
        # جدول مشخصات نداشت؛ در متن صفحه بگرد («کتابی ۳۹۸ صفحه‌ای»)
        for line in lines:
            if len(line) <= 160:
                _extend(details.page_counts, page_numbers(line))
            if details.page_counts:
                break

    # -- خلاصه ---------------------------------------------------------------
    details.summaries = summary_blocks(page_html)

    # -- تگ و دسته -----------------------------------------------------------
    details.tags, details.categories = taxonomy_terms(page_html, url)
    for value in labeled.get("genre", []):
        _extend(details.categories, [term for term in (_clean_term(value),) if term])

    # -- فرمت ----------------------------------------------------------------
    details.formats = _formats(details, labeled)

    # -- ملیت ----------------------------------------------------------------
    details.nationality = _nationality(details, labeled)

    if labeled.get("publisher"):
        details.publisher = _clean_term(labeled["publisher"][0])

    details.images = image_candidates(page_html, url)
    return details


def _extend(target: list, values: Iterable) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _formats(details: PageDetails, labeled: dict[str, list[str]]) -> list[str]:
    """فرمت‌های این محصول.

    عمداً کل متن صفحه گشته نمی‌شود: «کتاب صوتی» در منوی سایت هست و اگر متن را
    بگردیم همه‌ی محصولات صوتی می‌شوند. فقط مقدار برچسب «فرمت»، عنوان محصول و
    تگ/دسته‌های خودش شمرده می‌شوند.
    """
    signals = [
        *labeled.get("format", []),
        details.title,
        *details.tags,
        *details.categories,
    ]
    keys = [label_key(signal) for signal in signals if signal]
    formats: list[str] = []
    if any(any(hint in key for hint in PDF_HINTS) for key in keys):
        formats.append("pdf")
    if any(any(hint in key for hint in AUDIO_HINTS) for key in keys):
        formats.append("audio")
    return formats


def _nationality(details: PageDetails, labeled: dict[str, list[str]]) -> str:
    """``foreign`` / ``iranian`` / خالی.

    ترتیب شواهد: برچسب صریح «ملیت» → وجود مترجم → لاتین بودن نام نویسنده →
    تگ/دسته‌ی «ایرانی»/«ترجمه». اگر هیچ‌کدام نبود خالی می‌ماند؛ حدس زدن ملیت
    یعنی نوشتن مقدار غلط در شیت.
    """
    for value in labeled.get("nationality", []):
        key = label_key(value)
        if any(hint in key for hint in FOREIGN_HINTS):
            return "foreign"
        if any(hint in key for hint in IRANIAN_HINTS):
            return "iranian"
    if details.translators:
        return "foreign"
    if any(is_latin_name(name) for name in details.authors):
        return "foreign"
    terms = [label_key(term) for term in (*details.tags, *details.categories)]
    if any(any(hint in term for hint in FOREIGN_HINTS) for term in terms):
        return "foreign"
    if any(any(hint in term for hint in IRANIAN_HINTS) for term in terms):
        return "iranian"
    return ""
