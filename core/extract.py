"""استخراج از HTML: عنوان، لینک‌ها، متن اصلی و معیارهای کمّی.

اگر ``selectolax`` و ``trafilatura`` نصب باشند از آن‌ها استفاده می‌شود
(سریع‌تر و دقیق‌تر)، وگرنه یک پارسر جایگزین با کتابخانه‌ی استاندارد کار می‌کند
تا ابزار بدون وابستگی سنگین هم قابل اجرا باشد.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .normalizer import collapse_repeats

try:  # pragma: no cover
    from selectolax.parser import HTMLParser as SelectolaxParser  # type: ignore
except ImportError:  # pragma: no cover
    SelectolaxParser = None  # type: ignore[assignment]

try:  # pragma: no cover
    import trafilatura  # type: ignore
except ImportError:  # pragma: no cover
    trafilatura = None  # type: ignore[assignment]

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_WS_RE = re.compile(r"\s+")


def strip_tags(fragment: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", fragment))).strip()


# ---------------------------------------------------------------------------
# رمزگشایی صفحه — بخش حساس برای متن فارسی
# ---------------------------------------------------------------------------

_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w\-]+)""", re.I)
# سایت‌های قدیمی فارسی که هدر charset ندارند معمولاً windows-1256 هستند
_FALLBACK_ENCODINGS = ("utf-8", "windows-1256", "cp1252")


def _usable(name: str | None) -> str | None:
    if not name:
        return None
    try:
        "".encode(name)
    except LookupError:
        return None
    return name


def charset_from_content_type(content_type: str | None) -> str | None:
    """``text/html; charset=utf-8`` → ``utf-8``."""
    if not content_type or "charset=" not in content_type.lower():
        return None
    value = content_type.lower().split("charset=", 1)[1]
    return _usable(value.split(";")[0].strip().strip("\"'")) or None


def decode_html(body: bytes, content_type: str | None = None) -> str:
    """بایت‌های صفحه → متن.

    ترتیب: BOM، بعد charset هدر HTTP، بعد ``<meta charset>`` داخل خود صفحه،
    و در آخر حدس. حدس با ``utf-8`` شروع می‌شود ولی **سخت‌گیرانه**؛ اگر نشد
    ``windows-1256`` امتحان می‌شود، چون صفحات قدیمی فارسی با همان کدگذاری
    ساخته شده‌اند و خواندنشان با utf-8 عنوان را به علامت سؤال تبدیل می‌کند.
    """
    if not body:
        return ""
    for bom, name in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if body.startswith(bom):
            return body.decode(name, "replace")

    header = charset_from_content_type(content_type)
    if header:
        return body.decode(header, "replace")

    match = _META_CHARSET_RE.search(body[:4096])
    meta = _usable(match.group(1).decode("ascii", "ignore")) if match else None
    if meta:
        return body.decode(meta, "replace")

    for candidate in _FALLBACK_ENCODINGS:
        try:
            return body.decode(candidate)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# عنوان
# ---------------------------------------------------------------------------


#: جداکننده‌های رایج بین عنوان صفحه و نام سایت
_TITLE_SEPARATORS = re.compile(r"\s*[|–—»«\-–—:：]\s*|\s+[-–—]\s+")


def extract_title(
    page_html: str,
    selector: str | None = None,
    domain: str = "",
    site_name: str = "",
) -> str:
    """عنوان محصول: selector سفارشی → ``<h1>`` → ``og:title`` → ``<title>``.

    در حالت ``<title>`` نام سایت از انتهای/ابتدای عنوان حذف می‌شود، و در همه‌ی
    حالت‌ها پیشوند تکراری («دانلود رمان دانلود رمان ...») جمع می‌شود.
    """
    return collapse_repeats(_title_candidate(page_html, selector, domain, site_name))


def _title_candidate(
    page_html: str,
    selector: str | None = None,
    domain: str = "",
    site_name: str = "",
) -> str:
    if not page_html:
        return ""
    if selector and SelectolaxParser is not None:  # pragma: no cover - نیازمند selectolax
        node = SelectolaxParser(page_html).css_first(selector)
        if node is not None:
            text = node.text(strip=True)
            if text:
                return text
    for pattern in (
        r"<h1[^>]*>(.*?)</h1>",
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:title["\']',
    ):
        match = re.search(pattern, page_html, re.S | re.I)
        if match:
            text = strip_tags(match.group(1))
            if text:
                return text

    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.S | re.I)
    if match:
        return strip_site_name(strip_tags(match.group(1)), page_html, domain, site_name)
    return ""


def strip_site_name(
    title: str, page_html: str = "", domain: str = "", site_name: str = ""
) -> str:
    """«رمان تاوان خیانت | فروشگاه الف» → «رمان تاوان خیانت».

    فقط بخشی حذف می‌شود که **قابل اثبات** نام سایت باشد: ``og:site_name``،
    نام دامنه، یا ``site_name`` که در config اعلام شده. اگر نام سایت شناسایی
    نشد، عنوان دست‌نخورده برمی‌گردد — چون حذف کورکورانه‌ی یک بخش می‌تواند
    توکن تمایزدهنده («جلد دوم») را نابود کند و فاز ۲ را خراب کند.
    """
    if not title:
        return ""
    names: list[str] = []
    if site_name:
        names.append(site_name.strip().lower())
    match = re.search(
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\'](.*?)["\']',
        page_html,
        re.I,
    )
    if match:
        names.append(strip_tags(match.group(1)).lower())
    if domain:
        label = domain.lower().replace("www.", "").split(".")[0]
        names.extend([domain.lower(), label])
    names = [n for n in names if n]

    parts = [p.strip() for p in _TITLE_SEPARATORS.split(title) if p and p.strip()]
    if len(parts) < 2 or not names:
        return title.strip()

    kept = [part for part in parts if not any(name in part.lower() for name in names)]
    if len(kept) == len(parts):
        return title.strip()  # چیزی شناسایی نشد → دست نمی‌زنیم
    if not kept:
        return max(parts, key=len)
    return " ".join(kept)




# ---------------------------------------------------------------------------
# تشخیص صفحه‌ی محصول
# ---------------------------------------------------------------------------

#: نشانه‌های قطعی — یکی از این‌ها کافی است
_STRONG_PRODUCT = (
    re.compile(r'"@type"\s*:\s*"(?:Product|Offer|AggregateOffer)"', re.I),
    re.compile(r'itemtype=["\']https?://schema\.org/Product', re.I),
    re.compile(r'property=["\']og:type["\'][^>]+content=["\']product', re.I),
    re.compile(r'content=["\']product["\'][^>]+property=["\']og:type', re.I),
    re.compile(r'name=["\']product:price:amount', re.I),
    re.compile(r'class=["\'][^"\']*\b(?:single-product|product-page|woocommerce-product)\b', re.I),
    re.compile(r'(?:add[-_]to[-_]cart|add-to-basket|data-product[-_]id|product_id=)', re.I),
)

#: نشانه‌های ضعیف — دوتا با هم لازم است
_WEAK_PRODUCT = (
    ("افزودن به سبد", re.compile(r"افزودن\s+به\s+سبد|به\s+سبد\s+خرید|اضافه\s+به\s+سبد")),
    ("قیمت", re.compile(r"قیمت\s*[:：]|\bقیمت\b")),
    ("واحد پول", re.compile(r"[\d۰-۹][\d۰-۹,،٬\s]*\s*(?:تومان|ريال|ریال)")),
    ("خرید", re.compile(r"خرید\s+(?:محصول|فایل|اشتراک)|همین\s+حالا\s+بخرید|ثبت\s+سفارش")),
    ("دانلود فایل", re.compile(r"دانلود\s+(?:فایل|محصول)|لینک\s+دانلود")),
    ("موجودی", re.compile(r"موجود\s+در\s+انبار|ناموجود|اتمام\s+موجودی")),
)

#: نشانه‌های «این صفحه محصول نیست»
_NOT_PRODUCT = (
    ("نوع محتوا مقاله است", re.compile(r'"@type"\s*:\s*"(?:Article|BlogPosting|NewsArticle|WebPage|AboutPage|ContactPage|CollectionPage|SearchResultsPage)"', re.I)),
    ("صفحه‌ی اطلاعات سایت", re.compile(r"درباره\s*(?:ی)?\s*ما|تماس\s+با\s+ما|قوانین\s+و\s+مقررات|حریم\s+خصوصی|سوالات\s+متداول")),
)

#: مسیرهایی که تقریباً هیچ‌وقت صفحه‌ی محصول نیستند
_NOT_PRODUCT_PATH = re.compile(
    r"/(?:blog|news|article|articles|post|posts|tag|tags|category|categories|"
    r"about|about-us|contact|contact-us|privacy|terms|faq|cart|checkout|"
    r"my-account|login|register|search|author|page)(?:/|$|\?)",
    re.I,
)


@dataclass
class ProductSignals:
    """چرا این صفحه محصول شمرده شد یا نشد — برای اینکه تصمیم قابل بازبینی باشد."""

    is_product: bool
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def why(self) -> str:
        if self.is_product:
            return "، ".join(self.reasons) or "نشانه‌ی محصول"
        return "، ".join(self.blockers or ["هیچ نشانه‌ی محصولی پیدا نشد"])


def is_product_page(page_html: str, url: str = "") -> ProductSignals:
    """آیا این صفحه یک محصول قابل خرید است یا یک برگه/پست معمولی؟

    بدون این فیلتر، «درباره ما» و «راهنمای خرید» و پست‌های بلاگ هم به‌عنوان
    محصول وارد خروجی می‌شوند. ملاک، چیزی است که یک صفحه‌ی فروش دارد و یک
    برگه ندارد: داده‌ی ساخت‌یافته‌ی Product، دکمه‌ی افزودن به سبد، یا قیمت.
    """
    if not page_html:
        return ProductSignals(False, blockers=["صفحه خالی است"])

    path_blocked = bool(url) and bool(_NOT_PRODUCT_PATH.search(urllib.parse.urlsplit(url).path))
    text = _SCRIPT_RE.sub(" ", page_html)

    reasons: list[str] = []
    for pattern in _STRONG_PRODUCT:
        if pattern.search(page_html):
            reasons.append("داده‌ی ساخت‌یافته‌ی محصول")
            break

    weak = [label for label, pattern in _WEAK_PRODUCT if pattern.search(text)]
    blockers = [label for label, pattern in _NOT_PRODUCT if pattern.search(page_html)]

    strong = bool(reasons)
    if strong:
        # نشانه‌ی قطعی بر مسیر و بر نشانه‌های منفی می‌چربد
        return ProductSignals(True, reasons=reasons + weak)
    if path_blocked:
        blockers.append("مسیر URL صفحه‌ی محصول نیست")
    if len(weak) >= 2 and not blockers:
        return ProductSignals(True, reasons=weak)
    return ProductSignals(False, reasons=weak, blockers=blockers)


# ---------------------------------------------------------------------------
# لینک‌ها
# ---------------------------------------------------------------------------


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value)


def extract_links(page_html: str, base_url: str) -> list[str]:
    """لینک‌های داخل صفحه، مطلق‌شده و بدون fragment."""
    parser = _LinkParser()
    try:
        parser.feed(page_html)
    except Exception:  # pragma: no cover - HTML خراب
        pass
    out: list[str] = []
    seen: set[str] = set()
    for href in parser.links:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        absolute, _ = urllib.parse.urldefrag(absolute)
        absolute = absolute.rstrip("/") or absolute
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def same_domain(url: str, domain: str) -> bool:
    netloc = urllib.parse.urlsplit(url).netloc.lower()
    domain = domain.lower()
    return netloc == domain or netloc.endswith("." + domain)


# ---------------------------------------------------------------------------
# sitemap
# ---------------------------------------------------------------------------


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """خروجی: ``(urls, nested_sitemaps)`` — از sitemap index هم پشتیبانی می‌کند."""
    urls: list[str] = []
    nested: list[str] = []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        # بعضی سایت‌ها sitemap را به‌صورت متن ساده می‌دهند
        return [line.strip() for line in xml_text.splitlines() if line.startswith("http")], []
    tag = root.tag.split("}")[-1]
    for child in root:
        loc = child.find("{*}loc")
        if loc is None or not (loc.text or "").strip():
            continue
        value = loc.text.strip()
        if tag == "sitemapindex":
            nested.append(value)
        else:
            urls.append(value)
    return urls, nested


# ---------------------------------------------------------------------------
# متن اصلی و معیارهای کمّی (فاز ۴)
# ---------------------------------------------------------------------------


@dataclass
class ContentMetrics:
    word_count: int = 0
    h2_count: int = 0
    h3_count: int = 0
    has_table: bool = False
    has_list: bool = False
    image_count: int = 0
    published: str = ""
    text: str = field(default="", repr=False)

    def score(self) -> float:
        """امتیاز کمّی ۰..۱۰ برای انتخاب سه کاندیدای برتر."""
        words = min(self.word_count / 1500.0, 1.0) * 4.0
        headings = min((self.h2_count + self.h3_count) / 8.0, 1.0) * 2.5
        structure = (1.0 if self.has_table else 0.0) + (1.0 if self.has_list else 0.0)
        images = min(self.image_count / 4.0, 1.0) * 1.0
        freshness = 0.5 if self.published else 0.0
        return round(words + headings + structure + images + freshness, 2)


_DATE_META = (
    r'article:published_time["\'][^>]+content=["\']([^"\']+)',
    r'["\']datePublished["\']\s*:\s*["\']([^"\']+)',
    r'article:modified_time["\'][^>]+content=["\']([^"\']+)',
)


def extract_content(page_html: str, url: str = "") -> ContentMetrics:
    """متن اصلی + معیارهای ساختاری. هدر/فوتر/سایدبار حذف می‌شود."""
    if not page_html:
        return ContentMetrics()

    text = ""
    if trafilatura is not None:  # pragma: no cover - نیازمند trafilatura
        try:
            text = trafilatura.extract(page_html, url=url or None, include_comments=False) or ""
        except Exception:
            text = ""
    if not text:
        text = _fallback_main_text(page_html)

    body = _SCRIPT_RE.sub(" ", page_html)
    metrics = ContentMetrics(
        word_count=len(text.split()),
        h2_count=len(re.findall(r"<h2[\s>]", body, re.I)),
        h3_count=len(re.findall(r"<h3[\s>]", body, re.I)),
        has_table=bool(re.search(r"<table[\s>]", body, re.I)),
        has_list=bool(re.search(r"<(ul|ol)[\s>]", body, re.I)),
        image_count=len(re.findall(r"<img[\s>]", body, re.I)),
        text=text,
    )
    for pattern in _DATE_META:
        match = re.search(pattern, page_html, re.I)
        if match:
            metrics.published = match.group(1)[:32]
            break
    return metrics


def _fallback_main_text(page_html: str) -> str:
    """استخراج ساده: بزرگ‌ترین بلوکِ ``<article>``/``<main>`` یا کل ``<body>``."""
    body = _SCRIPT_RE.sub(" ", page_html)
    for pattern in (r"<article[^>]*>(.*?)</article>", r"<main[^>]*>(.*?)</main>"):
        blocks = re.findall(pattern, body, re.S | re.I)
        if blocks:
            return strip_tags(max(blocks, key=len))
    match = re.search(r"<body[^>]*>(.*?)</body>", body, re.S | re.I)
    return strip_tags(match.group(1) if match else body)


def extract_images(page_html: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"<img[^>]+src=[\"']([^\"']+)[\"']", page_html, re.I):
        urls.append(urllib.parse.urljoin(base_url, match.group(1)))
    return urls
