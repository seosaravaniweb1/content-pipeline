"""لایه‌ی HTTP.

ترتیب تلاش طبق داکیومنت:
1. ``curl_cffi`` با ``impersonate="chrome"`` (عبور از Cloudflare)
2. اگر ۴۰۳/۴۲۹/۵۰۳ یا صفحه‌ی پوسته‌ای → fallback به Playwright

نکته: ``cloudscraper`` (متروک) و ``requests`` خام (اثرانگشت TLS شناسایی‌شونده)
عمداً استفاده نشده‌اند. اگر ``curl_cffi`` نصب نباشد، ``urllib`` کتابخانه‌ی
استاندارد به‌عنوان آخرین چاره به کار می‌رود و CLI هشدار می‌دهد.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any

from . import extract

DEFAULT_UA = "ContentFeedBot/1.0 (+set-a-contact-url-in-config)"

_EMPTY_THRESHOLD = 200  # کمتر از این تعداد کاراکتر HTML یعنی «محتوای خالی»


@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    final_url: str = ""
    via: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html.strip())

    @property
    def looks_empty(self) -> bool:
        """صفحه‌ی پوسته‌ای (احتمالاً JS-heavy) — نامزد fallback مرورگر."""
        return len(self.html.strip()) < _EMPTY_THRESHOLD


class RateLimiter:
    """حداکثر N درخواست در ثانیه به ازای هر دامنه (رعایت اخلاق کراول)."""

    def __init__(self, requests_per_second: float = 1.0) -> None:
        self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, domain: str) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            last = self._last.get(domain, 0.0)
            delay = self.min_interval - (now - last)
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._last[domain] = now


class RobotsCache:
    """خواندن و کش ``robots.txt`` — هم برای اجازه و هم برای یافتن sitemap."""

    def __init__(self, user_agent: str = DEFAULT_UA, timeout: int = 15) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _parser(self, base: str) -> urllib.robotparser.RobotFileParser | None:
        origin = _origin(base)
        if origin in self._parsers:
            return self._parsers[origin]
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(urllib.parse.urljoin(origin, "/robots.txt"))
        try:
            request = urllib.request.Request(
                parser.url, headers={"User-Agent": self.user_agent}
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                parser.parse(response.read().decode("utf-8", "replace").splitlines())
        except Exception:
            parser = None  # type: ignore[assignment]
        self._parsers[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parser = self._parser(url)
        if parser is None:
            return True  # robots.txt در دسترس نبود → محدودیتی اعمال نمی‌کنیم
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def sitemaps(self, base: str) -> list[str]:
        parser = self._parser(base)
        if parser is None:
            return []
        return list(parser.site_maps() or [])


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme or 'https'}://{parts.netloc}"


def domain_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


class Fetcher:
    """گیرنده‌ی صفحات با rate limit، robots و fallback مرورگر."""

    def __init__(
        self,
        user_agent: str = DEFAULT_UA,
        requests_per_second: float = 1.0,
        timeout: int = 20,
        respect_robots: bool = True,
        playwright_fallback: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.playwright_fallback = playwright_fallback
        self.limiter = RateLimiter(requests_per_second)
        self.robots = RobotsCache(user_agent, timeout)
        self._session: Any = None
        self._backend = self._pick_backend()
        self._browser: Any = None
        self._playwright: Any = None

    # -- backend ------------------------------------------------------------
    def _pick_backend(self) -> str:
        try:
            from curl_cffi import requests as curl_requests  # type: ignore

            self._session = curl_requests.Session()
            return "curl_cffi"
        except ImportError:
            # عمداً به requests برنمی‌گردیم — داکیومنت آن را رد کرده است.
            return "urllib"

    @property
    def backend(self) -> str:
        return self._backend

    # -- fetch --------------------------------------------------------------
    def fetch(
        self, url: str, allow_browser: bool = True, force_browser: bool = False
    ) -> FetchResult:
        if self.respect_robots and not self.robots.allowed(url):
            return FetchResult(url, 999, "", via="robots-disallow", error="robots.txt disallow")

        self.limiter.wait(domain_of(url))
        if force_browser and allow_browser and self.playwright_fallback:
            # سایت‌هایی که در config با js: true علامت خورده‌اند
            browser_result = self._fetch_browser(url)
            if browser_result is not None and browser_result.ok:
                return browser_result
        result = self._fetch_http(url)
        needs_browser = result.status in {0, 403, 429, 503} or (
            result.status == 200 and result.looks_empty
        )
        if needs_browser and allow_browser and self.playwright_fallback:
            browser_result = self._fetch_browser(url)
            if browser_result is not None:
                return browser_result
        return result

    def _fetch_http(self, url: str) -> FetchResult:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.6",
        }
        try:
            if self._backend == "curl_cffi":
                response = self._session.get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    impersonate="chrome",
                    allow_redirects=True,
                )
                # خودمان رمزگشایی می‌کنیم تا صفحات فارسیِ بدون هدر charset
                # (که اغلب windows-1256 هستند) سالم بخوانند
                text = extract.decode_html(
                    response.content or b"", response.headers.get("content-type")
                )
                return FetchResult(url, response.status_code, text, str(response.url), "curl_cffi")
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = extract.decode_html(response.read(), response.headers.get("content-type"))
                return FetchResult(url, response.status, text, response.geturl(), "urllib")
        except urllib.error.HTTPError as exc:  # pragma: no cover - وابسته به شبکه
            return FetchResult(url, exc.code, "", via="urllib", error=str(exc))
        except Exception as exc:  # pragma: no cover - وابسته به شبکه
            return FetchResult(url, 0, "", via=self._backend, error=str(exc))

    def fetch_bytes(self, url: str) -> tuple[int, bytes]:
        """دانلود دودویی (برای تصاویر)."""
        self.limiter.wait(domain_of(url))
        headers = {"User-Agent": self.user_agent}
        try:
            if self._backend == "curl_cffi":  # pragma: no cover - نیازمند curl_cffi
                response = self._session.get(
                    url, headers=headers, timeout=self.timeout, impersonate="chrome"
                )
                return response.status_code, response.content
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except Exception:  # pragma: no cover - وابسته به شبکه
            return 0, b""

    # -- playwright ---------------------------------------------------------
    def _ensure_browser(self) -> Any:  # pragma: no cover - نیازمند playwright
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            self.playwright_fallback = False
            return None
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception:
            self.playwright_fallback = False
            self._browser = None
        return self._browser

    def _fetch_browser(self, url: str) -> FetchResult | None:  # pragma: no cover
        browser = self._ensure_browser()
        if browser is None:
            return None
        try:
            context = browser.new_context(user_agent=self.user_agent, locale="fa-IR")
            page = context.new_page()
            response = page.goto(url, timeout=self.timeout * 1000, wait_until="domcontentloaded")
            html = page.content()
            status = response.status if response else 0
            context.close()
            return FetchResult(url, status, html, page.url, "playwright")
        except Exception as exc:
            return FetchResult(url, 0, "", via="playwright", error=str(exc))

    def close(self) -> None:
        if self._browser is not None:  # pragma: no cover
            try:
                self._browser.close()
                if self._playwright is not None:
                    self._playwright.stop()
            except Exception:
                pass
            self._browser = None
        if self._session is not None and hasattr(self._session, "close"):
            try:
                self._session.close()
            except Exception:
                pass

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def fetcher_from_config(config: Any) -> Fetcher:
    # داکیومنت: حداکثر ۱ درخواست در ثانیه به هر دامنه (crawl.delay_seconds)
    delay = float(config.get("crawl.delay_seconds", 1.0))
    return Fetcher(
        user_agent=config.get("crawl.user_agent", DEFAULT_UA),
        requests_per_second=(1.0 / delay) if delay > 0 else 0.0,
        timeout=int(config.get("crawl.timeout", 20)),
        respect_robots=bool(config.get("crawl.respect_robots", True)),
        playwright_fallback=bool(config.get("crawl.playwright_fallback", True)),
    )
