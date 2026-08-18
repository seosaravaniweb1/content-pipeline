"""بارگذاری ``config.yaml`` (قالب بخش ۱۲ داکیومنت)."""

from __future__ import annotations

import copy

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover - yaml در requirements هست
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


class ConfigError(RuntimeError):
    pass


@dataclass
class SiteConfig:
    """یک دامنه‌ی رقیب.

    در ``config.yaml`` هم می‌توان فقط آدرس داد (قالب داکیومنت)::

        sites:
          - https://example1.ir

    و هم برای دقت بیشتر، تنظیمات هر سایت را جدا کرد::

        sites:
          - url: https://example1.ir
            product_url_include: ["/product/"]
    """

    domain: str
    base_url: str
    #: الگوهای regex برای شناسایی URL محصول (خالی = همه‌ی URLها)
    product_url_include: list[str] = field(default_factory=list)
    product_url_exclude: list[str] = field(default_factory=list)
    #: CSS selector عنوان (پیش‌فرض: h1 → og:title → title)
    title_selector: str | None = None
    #: نام سایت، برای حذف از انتهای <title> (اگر og:site_name نبود)
    site_name: str = ""
    #: نقطه‌ی شروع کراول وقتی sitemap نیست
    seed_urls: list[str] = field(default_factory=list)
    #: اجبار به Playwright برای این دامنه
    js: bool = False
    max_pages: int = 500
    max_depth: int = 3

    @classmethod
    def from_entry(cls, entry: Any, defaults: dict | None = None) -> "SiteConfig":
        defaults = defaults or {}
        data: dict[str, Any] = {"url": entry} if isinstance(entry, str) else dict(entry)
        base_url = str(data.get("url") or data.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ConfigError(f"آدرس سایت نامعتبر است: {entry!r}")
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        domain = data.get("domain") or re.sub(r"^https?://", "", base_url).split("/")[0]
        return cls(
            domain=domain,
            base_url=base_url,
            product_url_include=list(data.get("product_url_include", []) or []),
            product_url_exclude=list(data.get("product_url_exclude", []) or []),
            title_selector=data.get("title_selector"),
            site_name=str(data.get("site_name", "") or ""),
            seed_urls=list(data.get("seed_urls", []) or []),
            js=bool(data.get("js", False)),
            max_pages=int(data.get("max_pages", defaults.get("max_pages", 500))),
            max_depth=int(data.get("max_depth", defaults.get("max_depth", 3))),
        )


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path | None = None

    # -- دسترسی‌های پرتکرار --------------------------------------------------
    @property
    def db_path(self) -> str:
        return self.get("database.path", "content_pipeline/data/runs.db")

    @property
    def target_topic(self) -> str:
        return self.get("run.target_topic", "")

    @property
    def topic_examples(self) -> list[str]:
        return self.get("run.topic_examples", []) or []

    @property
    def relevance_threshold(self) -> float:
        return float(self.get("run.relevance_threshold", 0.65))

    @property
    def review_threshold(self) -> float:
        return float(self.get("run.review_threshold", 0.50))

    @property
    def sites(self) -> list[SiteConfig]:
        defaults = {
            "max_depth": self.get("crawl.max_depth", 3),
            "max_pages": self.get("crawl.max_pages", 500),
        }
        return [SiteConfig.from_entry(entry, defaults) for entry in self.get("sites", []) or []]

    @property
    def normalizer(self) -> dict:
        return self.get("normalizer", {}) or {}

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default


DEFAULTS: dict[str, Any] = {
    "database": {"path": "content_pipeline/data/runs.db"},
    "run": {
        "categories": [],
        "target_topic": "",
        "topic_examples": [],
        "auto_threshold": True,
        "relevance_threshold": 0.65,
        "review_threshold": 0.50,
    },
    "sites": [],
    "crawl": {
        "max_depth": 3,
        "max_pages": 500,
        "delay_seconds": 1.0,
        "timeout": 20,
        "user_agent": "ContentFeedBot/1.0 (+set-a-contact-url-in-config)",
        "respect_robots": True,
        "playwright_fallback": True,
    },
    "normalizer": {},
    "resolve": {
        "similarity_threshold": 0.80,
        "blocking_top_tokens": 3,
        # نردبان آستانه بر اساس شواهد هویتی — بخش «فاز ۲» در README
        "strong_match_threshold": 0.88,
        "no_entity_threshold": 0.95,
    },
    "suggest": {
        "hl": "fa",
        "gl": "ir",
        "delay_min": 2,
        "delay_max": 4,
        "max_per_session": 300,
        "max_query_words": 8,
        "max_variants_per_product": 4,
        "enough_keywords": 8,
        "min_keyword_coverage": 1.0,
        "query_prefixes": ["دانلود", "پی دی اف"],
        "max_consecutive_failures": 3,
        "max_consecutive_empty": 40,
        "timeout": 15,
        "cache_ttl_days": 30,
    },
    "output": {
        "sheets": ["ready", "archive", "all", "review"],
        "keyword_columns": 10,
        "xlsx_path": "content_pipeline/data/output-{run_id}.xlsx",
        "sheet_id": "",
        "service_account_json": "",
        "tabs": {
            "ready": "آماده تولید محتوا",
            "archive": "آرشیو آینده",
            "all": "همه محصولات",
            "review": "نیاز به بازبینی دستی",
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """ادغام عمیق با **کپی کامل**.

    بدون کپی، دیکشنری‌های تودرتوی ``DEFAULTS`` بین همه‌ی Configها مشترک
    می‌مانند و یک تغییر (مثلاً انتخاب شیت‌ها در پنل) روی همه‌ی اجراهای بعدی
    همان پروسه اثر می‌گذارد.
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(path: str | Path | None = None) -> Config:
    """خواندن config با پیش‌فرض‌های امن. نبودن فایل خطا نیست."""
    data: dict[str, Any] = {}
    resolved: Path | None = None
    if path:
        resolved = Path(path)
        if not resolved.exists():
            raise ConfigError(
                f"فایل config پیدا نشد: {resolved}\n"
                "یک نسخه از نمونه بسازید:\n"
                "  Windows : copy content_pipeline\\config.example.yaml config.yaml\n"
                "  Linux/Mac: cp content_pipeline/config.example.yaml config.yaml"
            )
        if yaml is None:  # pragma: no cover
            raise ConfigError("برای خواندن config به PyYAML نیاز است: pip install pyyaml")
        # utf-8-sig یعنی اگر Notepad ویندوز فایل را با BOM ذخیره کرده باشد
        # هم درست خوانده شود؛ روی فایل بدون BOM هیچ فرقی نمی‌کند.
        data = yaml.safe_load(resolved.read_text(encoding="utf-8-sig")) or {}
    return Config(raw=_deep_merge(DEFAULTS, data), path=resolved)
