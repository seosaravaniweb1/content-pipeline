"""پیدا کردن صفحه‌های منبعِ هر عنوان.

این برنامه دیتابیس کراول ندارد و نمی‌خواهد داشته باشد؛ پس برای هر عنوان باید
خودش بفهمد کدام صفحه‌ها همان محصول‌اند. سه راه، به همین ترتیب:

1. **ستون منابع در خودِ شیت** — اگر ستونی دارید که آدرس صفحه(ها) در آن است
   (``sheet.sources_column``)، مستقیم از همان خوانده می‌شود. سریع‌ترین و
   دقیق‌ترین حالت.
2. **نگاشت ذخیره‌شده** — یک بار از فایل (خروجی هر ابزار دیگری که آدرس‌ها را
   دارد) ایمپورت می‌کنید و در ``source_links`` می‌ماند.
3. **جستجو در خودِ سایت‌های منبع** — ``/?s=`` وردپرس یا هر قالب دیگری که در
   config بدهید. از موتور جستجوی خارجی استفاده نمی‌شود: هم بلاک می‌شویم هم
   لازم نیست.

در هر سه حالت، صفحه‌ای که عنوانش با عنوان ما نخواند کنار گذاشته می‌شود؛ دیتیل
رمانِ دیگری نباید در ردیف شما بنشیند.
"""

from __future__ import annotations

import csv
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import db, details, extract, normalizer, similarity
from .config import SourceSite
from .details import PageDetails
from .http import Fetcher

LogFn = Callable[[str], None]

#: آدرس‌هایی که هیچ‌وقت صفحه‌ی محصول نیستند
_SKIP_PATTERNS = ("/tag/", "/category/", "/product-tag/", "?add-to-cart", "/page/", "/author/")


@dataclass
class SourceStats:
    fetched: int = 0
    from_cache: int = 0
    searched: int = 0


def page_details(
    conn: sqlite3.Connection,
    url: str,
    fetcher: Fetcher,
    site_name: str = "",
    ttl_days: int = 30,
) -> tuple[PageDetails | None, bool]:
    """دیتیل یک صفحه؛ ``(نتیجه، از_کش)``.

    خروجی استخراج کش می‌شود نه HTML خام: هم جای کمتری می‌گیرد و هم اجرای
    دوباره روی ۱۵ هزار عنوان یک درخواست تازه به سایت‌ها نمی‌زند.
    """
    cached = db.get_page(conn, url, ttl_days, version=details.EXTRACT_VERSION)
    if cached is not None:
        return PageDetails.from_dict(cached), True
    result = fetcher.fetch(url)
    if not result.ok:
        return None, False
    try:
        extracted = details.extract_details(result.html, result.final_url or url, site_name)
    except Exception:  # noqa: BLE001 — یک صفحه‌ی خراب یعنی همان یک صفحه رد شود
        return None, False
    db.put_page(conn, url, extracted.domain, extracted.as_dict(), version=details.EXTRACT_VERSION)
    return extracted, False


def search_site(
    site: SourceSite,
    query: str,
    title_key: str,
    fetcher: Fetcher,
    min_similarity: float,
    max_results: int,
    norm_config: normalizer.NormalizerConfig,
) -> list[str]:
    """جستجو در یک سایت و برداشتن لینک‌هایی که عنوانشان می‌خورد.

    متن خودِ لینک برای تطبیق کافی است، پس صفحه‌های بی‌ربط اصلاً دانلود
    نمی‌شوند — روی ۱۵ هزار عنوان تفاوتش ساعت‌هاست.
    """
    if not query:
        return []
    url = (site.search_url or "{base}/?s={query}").format(
        base=site.base_url.rstrip("/"), query=urllib.parse.quote(query)
    )
    result = fetcher.fetch(url, force_browser=site.js)
    if not result.ok:
        return []

    include = [re.compile(pattern) for pattern in site.include]
    exclude = [re.compile(pattern) for pattern in site.exclude]
    scored: list[tuple[float, str]] = []
    for link, text in extract.extract_anchors(result.html, result.final_url or url):
        if not extract.same_domain(link, site.domain):
            continue
        if any(part in link for part in _SKIP_PATTERNS):
            continue
        if any(pattern.search(link) for pattern in exclude):
            continue
        if include and not any(pattern.search(link) for pattern in include):
            continue
        score = similarity.title_similarity(normalizer.normalize(text, norm_config), title_key)
        if score >= min_similarity:
            scored.append((score, link))
    scored.sort(key=lambda item: -item[0])
    return [link for _, link in scored[:max_results]]


def collect(
    conn: sqlite3.Connection,
    title: str,
    title_key: str,
    sites: Sequence[SourceSite],
    fetcher: Fetcher,
    *,
    known_urls: Sequence[str] = (),
    max_sources: int = 5,
    min_similarity: float = 0.72,
    search_enabled: bool = True,
    max_results_per_site: int = 8,
    cache_ttl_days: int = 30,
    norm_config: normalizer.NormalizerConfig | None = None,
) -> tuple[list[PageDetails], SourceStats]:
    """صفحه‌های همان محصول در منابع → دیتیل‌هایشان."""
    norm_config = norm_config or normalizer.DEFAULT_CONFIG
    stats = SourceStats()
    collected: list[PageDetails] = []
    seen: set[str] = set()
    by_domain = {site.domain: site for site in sites}

    def take(url: str) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        domain = urllib.parse.urlsplit(url).netloc.lower()
        site = by_domain.get(domain)
        page, from_cache = page_details(
            conn, url, fetcher, site.site_name if site else "", cache_ttl_days
        )
        if from_cache:
            stats.from_cache += 1
        else:
            stats.fetched += 1
        if page is None or page.is_empty:
            return
        if page.title and title_key:
            score = similarity.title_similarity(
                normalizer.normalize(page.title, norm_config), title_key
            )
            if score < min_similarity:
                return  # صفحه‌ی محصول دیگری است
        collected.append(page)

    # ۱) آدرس‌هایی که خودمان می‌دانیم (ستون شیت یا نگاشت ذخیره‌شده)
    for url in [*known_urls, *db.source_links(conn, title_key)]:
        if len(collected) >= max_sources:
            break
        take(url)

    # ۲) جستجو در سایت‌های منبع
    if len(collected) < max_sources and search_enabled:
        query = normalizer.main_keyword(title, norm_config)
        for site in sites:
            if len(collected) >= max_sources:
                break
            stats.searched += 1
            for url in search_site(
                site, query, title_key, fetcher, min_similarity, max_results_per_site, norm_config
            ):
                take(url)
                if len(collected) >= max_sources:
                    break
    return collected, stats


# ---------------------------------------------------------------------------
# ایمپورت نگاشت «عنوان → آدرس»
# ---------------------------------------------------------------------------

#: عنوان ستون‌هایی که آدرس منبع در آن‌هاست
URL_HEADERS = ("source_urls", "source_url", "urls", "url", "آدرس", "لینک", "منبع", "منابع")
#: عنوان ستون‌هایی که عنوان محصول در آن‌هاست
TITLE_HEADERS = ("canonical_title", "title", "raw_title", "عنوان", "عنوان محتوا", "نام محصول")

_URL_RE = re.compile(r"https?://\S+")


def import_links(
    conn: sqlite3.Connection,
    path: str | Path,
    norm_config: normalizer.NormalizerConfig | None = None,
    log: LogFn | None = None,
) -> tuple[int, int]:
    """ایمپورت فایل «عنوان → آدرس منبع» → ``(عنوان، آدرس)`` اضافه‌شده.

    فایل می‌تواند خروجی هر ابزاری باشد؛ فقط باید یک ستون عنوان و یک ستون
    آدرس داشته باشد (چند آدرس در یک سلول هم قبول است، با هر جداکننده‌ای).
    """
    norm_config = norm_config or normalizer.DEFAULT_CONFIG
    path = Path(path)
    rows = _read_table(path)
    if not rows:
        return 0, 0

    header = [str(cell or "").strip() for cell in rows[0]]
    keys = [details.label_key(cell) for cell in header]
    title_index = _find(keys, TITLE_HEADERS)
    url_index = _find(keys, URL_HEADERS)
    if title_index is None or url_index is None:
        raise ValueError(
            "ستون عنوان یا ستون آدرس پیدا نشد. "
            f"ستون‌های فایل: {'، '.join(cell for cell in header if cell)}"
        )

    titles = links = 0
    with db.transaction(conn):
        for raw in rows[1:]:
            if title_index >= len(raw) or url_index >= len(raw):
                continue
            title = str(raw[title_index] or "").strip()
            urls = _URL_RE.findall(str(raw[url_index] or ""))
            if not title or not urls:
                continue
            title_key = normalizer.normalize(title, norm_config)
            if not title_key:
                continue
            titles += 1
            for url in urls:
                domain = urllib.parse.urlsplit(url).netloc.lower()
                db.add_source_link(conn, title_key, url.rstrip("،,;"), domain)
                links += 1
    if log:
        log(f"{titles} عنوان و {links} آدرس ایمپورت شد.")
    return titles, links


def _find(keys: Sequence[str], names: Sequence[str]) -> int | None:
    wanted = [details.label_key(name) for name in names]
    for index, key in enumerate(keys):
        if key in wanted:
            return index
    return None


def _read_table(path: Path) -> list[list[str]]:
    if not path.exists():
        raise ValueError(f"فایل پیدا نشد: {path}")
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        try:
            from openpyxl import load_workbook  # type: ignore
        except ImportError as exc:  # pragma: no cover - وابسته به محیط
            raise ValueError("برای خواندن xlsx باید openpyxl نصب باشد.") from exc
        workbook = load_workbook(path, read_only=True, data_only=True)
        worksheet = workbook.active
        rows = [
            ["" if cell is None else str(cell) for cell in row]
            for row in worksheet.iter_rows(values_only=True)
        ]
        workbook.close()
        return rows
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [list(row) for row in csv.reader(handle)]
