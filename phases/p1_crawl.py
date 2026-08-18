"""فاز ۱ — کراول و تشخیص موضوعی.

ترتیب تلاش برای هر دامنه:
1. ``robots.txt`` → آدرس sitemap
2. پارس ``sitemap.xml`` (و sitemap indexها)
3. اگر sitemap نبود → کراول محدود با عمق مشخص، فقط داخل همان دامنه
4. هر صفحه با ``curl_cffi``؛ در صورت ۴۰۳/محتوای خالی → Playwright

سپس شباهت هر عنوان با بردار مرجع موضوع سنجیده می‌شود.
"""

from __future__ import annotations

import csv
import re
import sqlite3
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..core import db, extract, normalizer
from ..core.config import Config, SiteConfig
from ..core.embeddings import TopicMatcher
from ..core.http import Fetcher

_SITEMAP_GUESSES = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/wp-sitemap.xml")


@dataclass
class CrawlStats:
    pages_fetched: int = 0
    titles_found: int = 0
    inserted: int = 0
    relevant: int = 0
    borderline: int = 0
    failures: int = 0
    not_product: int = 0
    stopped: bool = False
    per_site: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        parts = [
            f"فاز ۱ — صفحات: {self.pages_fetched}، صفحه‌ی محصول: {self.titles_found}، "
            f"غیرمحصول (رد شد): {self.not_product}، ذخیره‌شده: {self.inserted}، "
            f"مرتبط: {self.relevant}، مرزی: {self.borderline}، خطا: {self.failures}"
        ]
        if self.stopped:
            parts.append("  ⏹ به درخواست شما متوقف شد؛ آنچه تا اینجا پیدا شد ذخیره است.")
        return "\n".join(parts)


def is_listing_seed(url: str) -> bool:
    """آدرسی که کاربر داده، صفحه‌ی نتایج است یا ریشه‌ی سایت؟

    اگر مسیر یا کوئری داشته باشد (``/category/roman/`` یا ``/?s=رمان``) یعنی
    خودِ کاربر رفته و فهرست محصولات را ساخته؛ همان فهرست را دنبال می‌کنیم و
    سراغ sitemap کل سایت نمی‌رویم.
    """
    parts = urllib.parse.urlsplit(url)
    return bool(parts.query) or parts.path.strip("/") != ""


def collect_urls(
    site: SiteConfig, fetcher: Fetcher, max_urls: int, log=print
) -> list[str]:
    """URLهای کاندیدای یک منبع.

    * آدرس فهرست/جستجو → لینک‌های همان فهرست + صفحه‌های بعدی‌اش
    * ریشه‌ی دامنه → sitemap، و در نبودش کراول محدود
    """
    if is_listing_seed(site.base_url):
        log(f"  [{site.domain}] صفحه‌ی فهرست — لینک‌های همین فهرست دنبال می‌شوند")
        urls = _from_listing(site, fetcher, max_urls, log)
    else:
        urls = _from_sitemaps(site, fetcher, max_urls)
        if not urls:
            urls = _from_crawl(site, fetcher, max_urls)
    return _filter_urls(urls, site)[:max_urls]


#: لینک «صفحه‌ی بعد» در فروشگاه‌های فارسی
_NEXT_PAGE = re.compile(
    r'<a[^>]+rel=["\']next["\'][^>]*href=["\']([^"\']+)'
    r'|<a[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\']next'
    r'|<a[^>]+class=["\'][^"\']*\bnext\b[^"\']*["\'][^>]*href=["\']([^"\']+)',
    re.I,
)


def _next_page(page_html: str, base_url: str) -> str:
    match = _NEXT_PAGE.search(page_html)
    if match:
        href = next((g for g in match.groups() if g), "")
        if href:
            return urllib.parse.urljoin(base_url, href)
    return ""


def _from_listing(site: SiteConfig, fetcher: Fetcher, max_urls: int, log=print) -> list[str]:
    """لینک‌های یک صفحه‌ی فهرست/جستجو و صفحه‌های بعدی‌اش."""
    collected: list[str] = []
    seen: set[str] = set()
    # صفحه‌های فهرست جدا از لینک‌های جمع‌شده شمرده می‌شوند؛ وگرنه لینک «بعدی»
    # که خودش داخل همان صفحه است، تکراری شمرده می‌شود و صفحه‌بندی نمی‌چرخد.
    visited: set[str] = set()
    url = site.base_url
    pages = 0
    max_pages = max(1, site.max_depth * 10)
    while url and pages < max_pages and len(collected) < max_urls * 4:
        if url in visited:
            break
        visited.add(url)
        result = fetcher.fetch(url, force_browser=site.js)
        pages += 1
        if not result.ok:
            log(f"  [{site.domain}] صفحه‌ی فهرست باز نشد: {url}")
            break
        for link in extract.extract_links(result.html, result.final_url or url):
            if link in seen or not extract.same_domain(link, site.domain):
                continue
            seen.add(link)
            collected.append(link)
        following = _next_page(result.html, result.final_url or url)
        url = following if following and following not in visited else ""
    # خودِ صفحه‌های فهرست کاندیدای محصول نیستند؛ دوباره دانلودشان نمی‌کنیم
    collected = [url for url in collected if url not in visited]
    log(f"  [{site.domain}] {pages} صفحه‌ی فهرست، {len(collected)} لینک")
    return collected


def _from_sitemaps(site: SiteConfig, fetcher: Fetcher, max_urls: int) -> list[str]:
    candidates = list(fetcher.robots.sitemaps(site.base_url))
    if not candidates:
        candidates = [urllib.parse.urljoin(site.base_url + "/", guess.lstrip("/")) for guess in _SITEMAP_GUESSES]

    seen: set[str] = set()
    queue = deque(candidates)
    urls: list[str] = []
    while queue and len(urls) < max_urls * 4:
        sitemap_url = queue.popleft()
        if sitemap_url in seen:
            continue
        seen.add(sitemap_url)
        result = fetcher.fetch(sitemap_url, allow_browser=False)
        if result.status != 200 or not result.html.strip():
            continue
        found, nested = extract.parse_sitemap(result.html)
        urls.extend(found)
        for child in nested:
            if child not in seen and len(seen) < 60:
                queue.append(child)
    return urls


def _from_crawl(site: SiteConfig, fetcher: Fetcher, max_urls: int) -> list[str]:
    """BFS محدود به همان دامنه با سقف عمق و تعداد صفحه."""
    seeds = site.seed_urls or [site.base_url]
    queue: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
    visited: set[str] = set()
    collected: list[str] = []
    while queue and len(visited) < site.max_pages and len(collected) < max_urls * 3:
        url, depth = queue.popleft()
        if url in visited or depth > site.max_depth:
            continue
        visited.add(url)
        result = fetcher.fetch(url, force_browser=site.js)
        if not result.ok:
            continue
        collected.append(url)
        if depth == site.max_depth:
            continue
        for link in extract.extract_links(result.html, result.final_url or url):
            if link not in visited and extract.same_domain(link, site.domain):
                queue.append((link, depth + 1))
    return collected


def _filter_urls(urls: Iterable[str], site: SiteConfig) -> list[str]:
    include = [re.compile(p) for p in site.product_url_include]
    exclude = [re.compile(p) for p in site.product_url_exclude]
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not extract.same_domain(url, site.domain):
            continue
        if any(pattern.search(url) for pattern in exclude):
            continue
        if include and not any(pattern.search(url) for pattern in include):
            continue
        clean, _ = urllib.parse.urldefrag(url)
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def sites_for_run(conn: sqlite3.Connection, run_id: str, config: Config):
    """سایت‌های یک اجرا: اول فهرست خودِ اجرا (از پنل)، وگرنه ``sites`` در config.

    هر اجرا فهرست سایت‌های خودش را نگه می‌دارد تا اجرای بعدی با فهرست دیگر،
    اجرای قبلی را دست‌کاری نکند.
    """
    urls = db.run_sites(conn, run_id)
    if not urls:
        return config.sites
    defaults = {
        "max_depth": config.get("crawl.max_depth", 3),
        "max_pages": config.get("crawl.max_pages", 500),
    }
    return [SiteConfig.from_entry(url, defaults) for url in urls]


def run(
    conn: sqlite3.Connection,
    run_id: str,
    config: Config,
    fetcher: Fetcher,
    matcher: TopicMatcher,
    limit: int | None = None,
    verbose: bool = True,
    should_stop=None,
) -> CrawlStats:
    norm_config = normalizer.config_from_mapping(config.normalizer)
    stop = should_stop or (lambda: False)
    stats = CrawlStats()
    sites = sites_for_run(conn, run_id, config)
    if not sites:
        raise ValueError(
            "هیچ سایتی برای این اجرا ثبت نشده است. از تب «شروع» پنل لینک سایت‌ها"
            " را وارد کنید یا کلید sites را در config پر کنید."
        )

    # سقف محصول برای هر منبع — «فعلاً ۱۰ تا بگیر تا ببینم درست کار می‌کند»
    options = db.run_options(conn, run_id)
    per_site_cap = (
        limit
        or int(options.get("max_products_per_site") or 0)
        or int(config.get("crawl.max_products_per_site", 0))
        or None
    )
    require_product = bool(
        options.get("require_product_signals", config.get("crawl.require_product_signals", True))
    )
    force_js = bool(options.get("js"))
    if verbose and per_site_cap:
        print(f"  سقف این اجرا: {per_site_cap} محصول از هر منبع")
    if verbose and not require_product:
        print("  فیلتر «فقط صفحه‌ی محصول» خاموش است — برگه و پست هم وارد می‌شوند")

    for site in sites:
        if stop():
            stats.stopped = True
            break
        max_urls = min(site.max_pages, per_site_cap * 6 if per_site_cap else site.max_pages)
        if verbose:
            print(f"  [{site.domain}] جمع‌آوری URL...")
        if force_js:
            site.js = True
        urls = collect_urls(site, fetcher, max_urls, print if verbose else (lambda *_: None))
        if verbose:
            print(f"  [{site.domain}] {len(urls)} آدرس کاندیدا")

        found_here = 0
        for url in urls:
            if stop():
                stats.stopped = True
                break
            if per_site_cap and found_here >= per_site_cap:
                if verbose:
                    print(f"  [{site.domain}] به سقف {per_site_cap} محصول رسید")
                break
            result = fetcher.fetch(url, force_browser=site.js or force_js)
            stats.pages_fetched += 1
            if not result.ok:
                stats.failures += 1
                continue

            # فقط صفحه‌ی محصول — نه «درباره ما»، نه پست بلاگ، نه صفحه‌ی دسته
            if require_product:
                signals = extract.is_product_page(result.html, result.final_url or url)
                if not signals.is_product:
                    stats.not_product += 1
                    continue

            title = extract.extract_title(
                result.html, site.title_selector, site.domain, site.site_name
            )
            if not title.strip():
                continue
            stats.titles_found += 1
            found_here += 1
            with db.transaction(conn):
                db.insert_raw_product(
                    conn,
                    run_id=run_id,
                    source_domain=site.domain,
                    source_url=result.final_url or url,
                    raw_title=title,
                    normalized_title=normalizer.normalize(title, norm_config),
                )
        stats.per_site[site.domain] = found_here
        if verbose:
            print(f"  [{site.domain}] {found_here} محصول ذخیره شد")
        if stats.stopped:
            break

    stats.inserted = conn.execute(
        "SELECT COUNT(*) AS c FROM raw_products WHERE run_id=?", (run_id,)
    ).fetchone()["c"]
    score_relevance(conn, run_id, config, matcher, verbose=verbose, stats=stats)
    return stats


def score_relevance(
    conn: sqlite3.Connection,
    run_id: str,
    config: Config,
    matcher: TopicMatcher,
    verbose: bool = True,
    stats: CrawlStats | None = None,
) -> CrawlStats:
    """محاسبه‌ی ``topic_score`` و تعیین ``is_relevant``.

    * ``score >= threshold``        → مرتبط
    * ``review_threshold..threshold`` → مرزی (``is_relevant IS NULL``) برای بازبینی دستی
    * کمتر                          → نامرتبط
    """
    stats = stats or CrawlStats()
    fallback = (config.relevance_threshold, config.review_threshold)
    auto = bool(config.get("run.auto_threshold", True)) and bool(matcher.thresholds)
    if verbose and auto:
        limits = "، ".join(
            f"{label}: {rel}/{rev}" for label, (rel, rev) in matcher.thresholds.items()
        )
        print(f"  آستانه‌ی کالیبره‌شده (مرتبط/مرزی) — {limits}")
    rows = conn.execute(
        "SELECT id, raw_title, normalized_title FROM raw_products WHERE run_id=?", (run_id,)
    ).fetchall()

    batch_size = 64
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        titles = [r["normalized_title"] or r["raw_title"] or "" for r in chunk]
        verdicts = matcher.classify_batch(titles)
        with db.transaction(conn):
            for row, (label, score) in zip(chunk, verdicts):
                threshold, review = matcher.limits(label, fallback) if auto else fallback
                if score >= threshold:
                    is_relevant: bool | None = True
                    stats.relevant += 1
                elif score >= review:
                    is_relevant = None
                    stats.borderline += 1
                else:
                    is_relevant = False
                db.update_raw_relevance(
                    conn, int(row["id"]), float(score), is_relevant, label or None
                )

    path = export_borderline(conn, run_id, config)
    if verbose and path:
        print(f"  عنوان‌های مرزی برای بازبینی دستی: {path}")
    return stats


def export_borderline(conn: sqlite3.Connection, run_id: str, config: Config) -> str | None:
    """ذخیره‌ی عنوان‌های مرزی در یک فایل جدا (طبق داکیومنت فاز ۱)."""
    rows = db.borderline_raw_products(conn, run_id)
    if not rows:
        return None
    target = Path(config.db_path).parent / "review" / f"{run_id}_borderline.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "topic_score", "raw_title", "normalized_title", "source_url"])
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    round(row["topic_score"] or 0.0, 4),
                    row["raw_title"],
                    row["normalized_title"],
                    row["source_url"],
                ]
            )
    return str(target)
