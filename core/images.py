"""انتخاب تصویر کاور: مربع، بی‌واترمارک، بدون هزینه‌ی API.

هیچ سرویس تشخیص تصویر پولی در کار نیست. چیزی که داریم، همان چیزی است که
مرورگر هم دارد: آدرس تصویر، متن جایگزینش، اندازه‌ی اعلام‌شده در HTML، و چند
کیلوبایت اول خود فایل. با همین‌ها سه تصمیم گرفته می‌شود:

**مربع بودن** — قطعی است. اندازه یا در HTML اعلام شده، یا در نام فایل
وردپرسی (``cover-600x600.jpg``) و یا از هدر خود فایل خوانده می‌شود. برای
خواندن هدر، فقط چند کیلوبایت اول با ``Range`` دانلود می‌شود نه کل تصویر.

**واترمارک‌نداشتن** — قطعی نیست، پس سه شاهد کنار هم گذاشته می‌شود:

1. آدرس/متن جایگزین: ``watermark``، ``logo``، ``site``، نام دامنه در نام فایل
2. تکرار بین دامنه‌ها: واترمارکِ هر سایت مال خودش است، پس تصویری که **عین
   همان** در دو دامنه‌ی مختلف دیده شود تقریباً همیشه کاور اصلی ناشر است
3. فهرست دامنه‌های مطمئن/ممنوع که خودتان در config می‌دهید

**کیفیت** — حداقل ضلع، و (اگر ``Pillow`` نصب باشد) رد کردن تصویرهایی که
با حاشیه‌ی یک‌دست مربع شده‌اند؛ آن‌ها کاور مربع نیستند، بندانگشتیِ کش‌آمده‌اند.
"""

from __future__ import annotations

import hashlib
import re
import struct
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .details import ImageCandidate

#: چند کیلوبایت اول فایل برای خواندن اندازه کافی است
HEADER_BYTES = 65536

#: کلمه‌هایی که در آدرس/متن تصویر یعنی «این کاور محصول نیست»
DEFAULT_REJECT_PATTERNS: tuple[str, ...] = (
    "watermark",
    "water-mark",
    "logo",
    "icon",
    "avatar",
    "placeholder",
    "sprite",
    "banner",
    "ads",
    "advert",
    "telegram",
    "instagram",
    "whatsapp",
    "footer",
    "header",
    "loading",
    "lazy",
    "blank",
    "noimage",
    "no-image",
    "default",
    "واترمارک",
    "لوگو",
)

#: نام فایل با پسوند اندازه‌ی وردپرس: ``cover-150x150.jpg`` → ``cover``
_WP_SIZE = re.compile(r"[-_]\d{2,4}x\d{2,4}(?=\.[a-z]{3,4}$)", re.I)


@dataclass
class ImagePick:
    """تصویر انتخاب‌شده و دلیل انتخابش."""

    url: str = ""
    width: int = 0
    height: int = 0
    score: float = 0.0
    domains: int = 0
    note: str = ""

    @property
    def filled(self) -> bool:
        return bool(self.url)

    def as_dict(self) -> dict:
        return {
            "value": self.url,
            "votes": self.domains,
            "sources": 0,
            "note": self.note or f"{self.width}×{self.height}",
        }


@dataclass
class ImageRules:
    """تنظیمات انتخاب تصویر (بخش ``details.image`` در config)."""

    enabled: bool = True
    min_side: int = 400
    #: چقدر انحراف از مربع بودن قابل قبول است (۰.۰۴ = ۴٪)
    square_tolerance: float = 0.04
    max_downloads: int = 6
    reject_patterns: tuple[str, ...] = DEFAULT_REJECT_PATTERNS
    prefer_domains: tuple[str, ...] = ()
    block_domains: tuple[str, ...] = ()
    #: تصویری که اندازه‌اش هیچ‌جا معلوم نشد، نوشته شود یا نه
    allow_unknown_size: bool = False
    require_square: bool = True

    @classmethod
    def from_mapping(cls, data: dict | None) -> "ImageRules":
        data = data or {}
        extra = tuple(str(p).lower() for p in data.get("reject_url_patterns", []) or [])
        return cls(
            enabled=bool(data.get("enabled", True)),
            min_side=int(data.get("min_side", 400)),
            square_tolerance=float(data.get("square_tolerance", 0.04)),
            max_downloads=int(data.get("max_downloads", 6)),
            reject_patterns=DEFAULT_REJECT_PATTERNS + extra,
            prefer_domains=tuple(str(d).lower() for d in data.get("prefer_domains", []) or ()),
            block_domains=tuple(str(d).lower() for d in data.get("block_domains", []) or ()),
            allow_unknown_size=bool(data.get("allow_unknown_size", False)),
            require_square=bool(data.get("require_square", True)),
        )


# ---------------------------------------------------------------------------
# اندازه از روی بایت‌های اول
# ---------------------------------------------------------------------------


def sniff_size(data: bytes) -> tuple[int, int]:
    """``(عرض، ارتفاع)`` از هدر فایل — PNG، JPEG، GIF و WebP.

    ``(0, 0)`` یعنی نشد؛ یا فرمت ناشناخته بود یا هدر در همین چند کیلوبایت
    اول نیامده. هیچ وابستگی‌ای لازم نیست؛ همه با ``struct`` خوانده می‌شود.
    """
    if not data or len(data) < 10:
        return 0, 0
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return int(width), int(height)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_size(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    return 0, 0


def _webp_size(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    try:
        if chunk == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        if chunk == b"VP8 ":
            width, height = struct.unpack("<HH", data[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    except (struct.error, IndexError):  # pragma: no cover - فایل ناقص
        return 0, 0
    return 0, 0


#: نشانگرهای SOF که ابعاد را دارند (SOF4/8/12 وجود ندارند یا رزروند)
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_size(data: bytes) -> tuple[int, int]:
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in _SOF_MARKERS:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return int(width), int(height)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        try:
            segment = struct.unpack(">H", data[index + 2 : index + 4])[0]
        except struct.error:  # pragma: no cover - فایل ناقص
            return 0, 0
        if segment < 2:
            return 0, 0
        index += 2 + segment
    return 0, 0


def is_square(width: int, height: int, tolerance: float = 0.04) -> bool:
    if width <= 0 or height <= 0:
        return False
    return abs(width - height) / max(width, height) <= tolerance


def fingerprint(data: bytes) -> str:
    """اثرانگشت تصویر برای تشخیص «همین کاور در سایت دیگر».

    با ``Pillow`` یک dHash ۶۴ بیتی محاسبه می‌شود که به فشرده‌سازی دوباره و
    تغییر اندازه مقاوم است. بدون آن، هش بایت‌های خام — دقیق‌تر ولی سخت‌گیرتر
    (فقط فایل کاملاً یکسان را یکی می‌شمارد).
    """
    if not data:
        return ""
    try:  # pragma: no cover - نیازمند Pillow
        import io

        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(data)) as image:
            small = image.convert("L").resize((9, 8))
            pixels = list(small.getdata())
        bits = 0
        for row in range(8):
            for column in range(8):
                left = pixels[row * 9 + column]
                right = pixels[row * 9 + column + 1]
                bits = (bits << 1) | int(left > right)
        return f"d{bits:016x}"
    except Exception:
        return "b" + hashlib.sha1(data).hexdigest()[:16]


def has_uniform_border(data: bytes, band: int = 4) -> bool:  # pragma: no cover - Pillow
    """آیا تصویر با حاشیه‌ی یک‌دست مربع شده؟

    بعضی فروشگاه‌ها کاور مستطیل را داخل یک مربع سفید می‌گذارند. از نظر ابعاد
    مربع است ولی کاور مربع نیست و در سایت بد می‌نشیند. بدون ``Pillow`` این
    تست انجام نمی‌شود و تصویر رد نمی‌شود.
    """
    try:
        import io

        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(data)) as image:
            small = image.convert("RGB").resize((32, 32))
            pixels = small.load()
    except Exception:
        return False
    edges = [pixels[x, y] for y in range(band) for x in range(32)]
    edges += [pixels[x, 31 - y] for y in range(band) for x in range(32)]
    if not edges:
        return False
    first = edges[0]
    return all(
        abs(pixel[0] - first[0]) + abs(pixel[1] - first[1]) + abs(pixel[2] - first[2]) < 18
        for pixel in edges
    ) and (sum(first) > 690 or sum(first) < 45)


# ---------------------------------------------------------------------------
# امتیازدهی
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    candidate: ImageCandidate
    domain: str
    group: str
    domains: set[str] = field(default_factory=set)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def group_key(url: str) -> str:
    """کلید «یک کاور»: نام فایل بدون پسوند اندازه‌ی وردپرس.

    ``cover-600x600.jpg`` و ``cover.jpg`` و ``cover-150x150.jpg`` یک تصویرند
    و نباید سه کاندیدای جدا شمرده شوند.
    """
    path = urllib.parse.urlsplit(url).path
    name = path.rsplit("/", 1)[-1].lower()
    return _WP_SIZE.sub("", name)


def _rejected(candidate: ImageCandidate, domain: str, rules: ImageRules) -> str:
    haystack = f"{candidate.url} {candidate.alt}".lower()
    for pattern in rules.reject_patterns:
        if pattern and pattern in haystack:
            return f"الگوی «{pattern}» در آدرس/متن تصویر"
    for blocked in rules.block_domains:
        if blocked and blocked in domain:
            return f"دامنه‌ی ممنوع: {blocked}"
    # نام دامنه در نام فایل، نشانه‌ی نسخه‌ی واترمارک‌خورده‌ی همان سایت است
    label = domain.replace("www.", "").split(".")[0]
    if label and len(label) > 3 and label in group_key(candidate.url):
        return "نام سایت در نام فایل تصویر"
    if candidate.known_size:
        if min(candidate.width, candidate.height) < rules.min_side:
            return f"کوچک‌تر از {rules.min_side} پیکسل"
        if rules.require_square and not is_square(
            candidate.width, candidate.height, rules.square_tolerance
        ):
            return "مربع نیست"
    return ""


def rank_candidates(
    candidates: Sequence[tuple[ImageCandidate, str]], rules: ImageRules
) -> list[_Entry]:
    """کاندیداها را بدون دانلود مرتب می‌کند؛ بهترین شانس اول."""
    entries: dict[str, _Entry] = {}
    for candidate, domain in candidates:
        if not candidate.url:
            continue
        if _rejected(candidate, domain, rules):
            continue
        key = group_key(candidate.url)
        entry = entries.get(key)
        if entry is None:
            entry = _Entry(candidate=candidate, domain=domain, group=key)
            entries[key] = entry
        entry.domains.add(domain)
        # در هر گروه، نسخه‌ی بزرگ‌تر/مطمئن‌تر نماینده می‌شود
        best = entry.candidate
        if (candidate.width * candidate.height, candidate.priority) > (
            best.width * best.height,
            best.priority,
        ):
            entry.candidate = candidate

    for entry in entries.values():
        candidate = entry.candidate
        score = float(candidate.priority)
        if len(entry.domains) > 1:
            score += 3.0 * (len(entry.domains) - 1)
            entry.reasons.append(f"در {len(entry.domains)} دامنه تکرار شده")
        if candidate.known_size:
            score += 2.0
            if is_square(candidate.width, candidate.height, rules.square_tolerance):
                score += 3.0
                entry.reasons.append("اندازه‌ی مربع در خود صفحه اعلام شده")
            score += min(candidate.width, candidate.height) / 1000.0
        if any(preferred in entry.domain for preferred in rules.prefer_domains):
            score += 4.0
            entry.reasons.append("دامنه‌ی مطمئن")
        entry.score = round(score, 3)
    return sorted(entries.values(), key=lambda item: -item.score)


# ---------------------------------------------------------------------------
# انتخاب نهایی
# ---------------------------------------------------------------------------

FetchBytes = Callable[[str, int], tuple[int, bytes]]


def pick_image(
    candidates: Iterable[tuple[ImageCandidate, str]],
    rules: ImageRules,
    fetch: FetchBytes | None = None,
) -> ImagePick:
    """بهترین کاور از میان کاندیداهای همه‌ی منابع.

    ``fetch`` همان ``Fetcher.fetch_bytes`` است. اگر داده نشود، فقط به
    اطلاعات خودِ HTML اکتفا می‌کنیم (سریع ولی محتاط‌تر: تصویری که اندازه‌اش
    معلوم نیست انتخاب نمی‌شود مگر ``allow_unknown_size``).
    """
    if not rules.enabled:
        return ImagePick(note="انتخاب تصویر خاموش است")
    ranked = rank_candidates(list(candidates), rules)
    if not ranked:
        return ImagePick(note="هیچ کاندیدای تصویری نماند")

    fallback: ImagePick | None = None
    seen_prints: dict[str, set[str]] = {}
    downloads = 0

    for entry in ranked:
        candidate = entry.candidate
        width, height = candidate.width, candidate.height
        note = "، ".join(entry.reasons)

        if fetch is not None and downloads < rules.max_downloads:
            status, data = fetch(candidate.url, HEADER_BYTES)
            downloads += 1
            if status not in (200, 206) or not data:
                continue
            sniffed = sniff_size(data)
            if sniffed != (0, 0):
                width, height = sniffed
            mark = fingerprint(data)
            if mark:
                seen_prints.setdefault(mark, set()).add(entry.domain)
                entry.domains |= seen_prints[mark]
            if has_uniform_border(data):
                note = (note + "، حاشیه‌ی یک‌دست").strip("، ")
                continue

        if width and height:
            if min(width, height) < rules.min_side:
                continue
            if rules.require_square and not is_square(width, height, rules.square_tolerance):
                continue
            return ImagePick(
                url=candidate.url,
                width=width,
                height=height,
                score=entry.score,
                domains=len(entry.domains),
                note=note,
            )
        if fallback is None and rules.allow_unknown_size:
            fallback = ImagePick(
                url=candidate.url,
                score=entry.score,
                domains=len(entry.domains),
                note=(note + "، اندازه تأیید نشد").strip("، "),
            )

    if fallback is not None:
        return fallback
    return ImagePick(note="تصویر مربعِ بی‌واترمارک پیدا نشد")
