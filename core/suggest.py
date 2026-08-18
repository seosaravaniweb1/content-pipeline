"""کلاینت گوگل ساجست (رایگان، endpoint غیررسمی).

.. code-block:: text

    https://suggestqueries.google.com/complete/search?client=firefox&hl=fa&q=<عنوان>

این endpoint **رسمی نیست**، پس قواعد ایمنی بخش ۹ داکیومنت اینجا اجباری‌اند و
در خود کلاینت اعمال شده‌اند تا هیچ فراخوانی‌ای نتواند دورشان بزند:

* تأخیر تصادفی ۲ تا ۴ ثانیه بین درخواست‌ها
* حداکثر ۳۰۰ کوئری در هر نشست
* هر نتیجه در ``api_cache`` ذخیره می‌شود (کوئری تکراری زده نمی‌شود)
* سه **خطای** پشت‌سرهم (HTTP غیر ۲۰۰، پاسخ غیرقابل‌پارس، خطای شبکه) → توقف؛
  این‌ها نشانه‌ی بلاک شدن‌اند
* پاسخ ۲۰۰ با فهرست خالی یعنی «این عبارت ساجست ندارد» — یک نتیجه‌ی معتبر،
  نه خطا. تعداد زیادی خالیِ پشت‌سرهم فقط هشدار می‌دهد، چون ممکن است واقعاً
  عبارت‌ها ساجست نداشته باشند
"""

from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

ENDPOINT = "https://suggestqueries.google.com/complete/search"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class SuggestBlocked(RuntimeError):
    """احتمال بلاک شدن — فاز ۳ باید فوراً متوقف شود."""


class SessionLimitReached(RuntimeError):
    """سقف کوئری‌های این نشست پر شده است."""


class _TransientFailure(RuntimeError):
    """خطای موقتی یک کوئری — نباید کش شود."""


@dataclass
class SuggestConfig:
    hl: str = "fa"
    gl: str = "ir"
    delay_min: float = 2.0
    delay_max: float = 4.0
    max_per_session: int = 300
    max_consecutive_failures: int = 3   # خطاهای واقعی (بلاک شدن)
    max_consecutive_empty: int = 40     # پاسخ‌های خالیِ پشت‌سرهم (شاید واقعاً ساجست ندارند)
    timeout: int = 15
    user_agent: str = DEFAULT_UA


class SuggestClient:
    """گیرنده‌ی ساجست با کش، تأخیر تصادفی و محافظ بلاک."""

    namespace = "google-suggest"

    def __init__(
        self,
        config: SuggestConfig | None = None,
        cache: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or SuggestConfig()
        self.cache = cache
        self._sleep = sleep
        self.queries_sent = 0
        self.consecutive_failures = 0
        self.consecutive_empty = 0
        self.empty_total = 0
        self._last_request: float | None = None
        self._session = self._build_session()

    # -- backend -------------------------------------------------------------
    def _build_session(self) -> Any:
        try:  # pragma: no cover - وابسته به محیط
            from curl_cffi import requests as curl_requests  # type: ignore

            self.backend = "curl_cffi"
            return curl_requests.Session()
        except ImportError:
            # طبق داکیومنت requests خام استفاده نمی‌شود؛ urllib فقط برای این است
            # که ابزار بدون curl_cffi هم اجرا شود (با ریسک شناسایی بالاتر).
            self.backend = "urllib"
            return None

    # -- کنترل نرخ -----------------------------------------------------------
    def _wait(self) -> None:
        delay = random.uniform(self.config.delay_min, self.config.delay_max)
        if self._last_request is not None:
            elapsed = time.monotonic() - self._last_request
            delay = max(0.0, delay - elapsed)
        if delay > 0:
            self._sleep(delay)
        self._last_request = time.monotonic()

    @property
    def remaining(self) -> int:
        return max(0, self.config.max_per_session - self.queries_sent)

    # -- API -----------------------------------------------------------------
    def suggest(self, query: str) -> list[str]:
        """کلمات پیشنهادی گوگل برای یک عنوان.

        خروجی خالی یعنی «ساجست ندارد» — که یک نتیجه‌ی معتبر است، نه خطا.
        اگر پاسخ از کش بیاید، نه تأخیری اعمال می‌شود و نه از سقف نشست کم می‌شود.
        """
        query = (query or "").strip()
        if not query:
            return []
        params = {"q": query, "hl": self.config.hl, "gl": self.config.gl}
        try:
            if self.cache is not None:
                raw = self.cache.call(self.namespace, params, lambda: self._live_fetch(query))
            else:
                raw = self._live_fetch(query)
        except _TransientFailure:
            # خطای موقتی کش نمی‌شود تا در اجرای بعدی دوباره تلاش شود.
            return []
        return _parse(raw)

    def _live_fetch(self, query: str) -> Any:
        if self.queries_sent >= self.config.max_per_session:
            raise SessionLimitReached(
                f"سقف {self.config.max_per_session} کوئری در این نشست پر شد. "
                "بعداً با --resume-from 3 ادامه دهید؛ نتایج قبلی از کش خوانده می‌شوند."
            )
        self._wait()
        self.queries_sent += 1
        try:
            raw = self._fetch(query)
        except Exception as exc:
            self._register_failure(f"{exc}")
            raise _TransientFailure(str(exc)) from exc

        if _parse(raw):
            self.consecutive_failures = 0
            self.consecutive_empty = 0
        else:
            self._register_empty()
        return raw

    def _register_failure(self, reason: str) -> None:
        """خطای واقعی: HTTP غیر ۲۰۰، پاسخ غیرقابل‌پارس، یا خطای شبکه.

        بلاک شدن گوگل خودش را این‌طور نشان می‌دهد (۴۲۹ یا صفحه‌ی کپچا که
        JSON نیست)، نه با پاسخ ۲۰۰ و فهرست خالی.
        """
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.config.max_consecutive_failures:
            raise SuggestBlocked(
                f"{self.consecutive_failures} خطای پشت‌سرهم ({reason}). "
                "احتمال بلاک شدن توسط گوگل. فاز ۳ متوقف شد؛ چند ساعت بعد با "
                "--resume-from 3 ادامه دهید (نتایج قبلی در کش هستند)."
            )

    def _register_empty(self) -> None:
        """پاسخ سالم ولی بدون پیشنهاد — یعنی این عبارت ساجست ندارد.

        این خطا نیست: خیلی از عنوان‌های خاص واقعاً در گوگل ساجست ندارند.
        فقط اگر تعداد خیلی زیادی پشت‌سرهم خالی باشد متوقف می‌شویم، چون آن‌وقت
        احتمالاً کوئری‌ها بد ساخته شده‌اند یا چیزی در مسیر پاسخ را خالی می‌کند.
        """
        self.consecutive_failures = 0
        self.consecutive_empty += 1
        self.empty_total += 1
        if self.consecutive_empty >= self.config.max_consecutive_empty:
            raise SuggestBlocked(
                f"{self.consecutive_empty} پاسخ خالیِ پشت‌سرهم. پاسخ‌ها سالم بودند، "
                "پس بلاک نشده‌اید؛ یا این عنوان‌ها واقعاً در گوگل ساجست ندارند یا "
                "کوئری‌ها بیش از حد بلندند. با --resume-from 3 ادامه دهید یا "
                "suggest.max_consecutive_empty را بالا ببرید."
            )

    def _fetch(self, query: str) -> Any:
        url = ENDPOINT + "?" + urllib.parse.urlencode(
            {"client": "firefox", "hl": self.config.hl, "gl": self.config.gl, "q": query}
        )
        headers = {"User-Agent": self.config.user_agent, "Accept": "*/*"}
        if self._session is not None:  # pragma: no cover - نیازمند curl_cffi
            response = self._session.get(
                url, headers=headers, timeout=self.config.timeout, impersonate="chrome"
            )
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            return json.loads(response.text)
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            return json.loads(response.read().decode("utf-8", "replace"))


def _parse(raw: Any) -> list[str]:
    """قالب ``client=firefox``: ``["query", ["s1", "s2", ...]]``."""
    if not raw:
        return []
    if isinstance(raw, list) and len(raw) >= 2 and isinstance(raw[1], list):
        return [str(item).strip() for item in raw[1] if str(item).strip()]
    return []


def suggest_from_config(config: Any, cache: Any | None = None) -> SuggestClient:
    return SuggestClient(
        SuggestConfig(
            hl=config.get("suggest.hl", "fa"),
            gl=config.get("suggest.gl", "ir"),
            delay_min=float(config.get("suggest.delay_min", 2)),
            delay_max=float(config.get("suggest.delay_max", 4)),
            max_per_session=int(config.get("suggest.max_per_session", 300)),
            max_consecutive_failures=int(config.get("suggest.max_consecutive_failures", 3)),
            max_consecutive_empty=int(config.get("suggest.max_consecutive_empty", 40)),
            timeout=int(config.get("suggest.timeout", 15)),
        ),
        cache=cache,
    )
