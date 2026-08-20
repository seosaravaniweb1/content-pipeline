"""بارگذاری ``config.yaml`` این برنامه.

فقط کلیدهای همین کار اینجاست: اتصال به شیت، سایت‌های منبع، قواعد پر کردن.
هیچ ربطی به تنظیمات ابزار استخراج ندارد و همان‌جا هم هیچ کلیدی از اینجا لازم
نیست — دو برنامه‌ی جدا با دو فایل تنظیمات جدا.
"""

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
    """خطای قابل‌نمایش به کاربر (نه traceback)."""


@dataclass
class SourceSite:
    """یک سایت منبع که دیتیل محصولات از آن خوانده می‌شود."""

    base_url: str
    domain: str = ""
    #: قالب آدرس جستجوی همان سایت. پیش‌فرض وردپرس است.
    search_url: str = ""
    #: الگوهای آدرس صفحه‌ی محصول (خالی = همه)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    #: نام سایت، برای حذف از انتهای ``<title>``
    site_name: str = ""
    #: این سایت با مرورگر گرفته شود (سایت‌های JS-heavy)
    js: bool = False

    @classmethod
    def from_entry(cls, entry: Any, default_search: str) -> "SourceSite":
        data: dict[str, Any] = {"url": entry} if isinstance(entry, str) else dict(entry or {})
        base = str(data.get("url") or data.get("base_url") or "").strip().rstrip("/")
        if not base:
            raise ConfigError(f"آدرس سایت نامعتبر است: {entry!r}")
        if not base.startswith(("http://", "https://")):
            base = "https://" + base
        return cls(
            base_url=base,
            domain=str(data.get("domain") or re.sub(r"^https?://", "", base).split("/")[0]),
            search_url=str(data.get("search_url") or default_search),
            include=[str(x) for x in (data.get("include") or data.get("product_url_include") or [])],
            exclude=[str(x) for x in (data.get("exclude") or data.get("product_url_exclude") or [])],
            site_name=str(data.get("site_name") or ""),
            js=bool(data.get("js", False)),
        )


DEFAULT_SEARCH_URL = "{base}/?s={query}"

DEFAULTS: dict[str, Any] = {
    "database": {"path": "sheet_filler/data/filler.db"},
    # --- شیت --------------------------------------------------------------
    "sheet": {
        "sheet_id": "",
        "service_account_json": "",
        "tab": "",
        "lists_tab": "لیست‌ها",
        "report_tab": "گزارش تکمیل",
        "header_row": 1,
        "file": "",
        "title_column": "",
        "sources_column": "",
        "columns": {},
        "never_write": ["title", "status", "product_id", "وضعیت", "شناسه محصول"],
        "batch_ranges": 400,
        "value_input_option": "RAW",
    },
    # --- منابع ------------------------------------------------------------
    "sites": [],
    "crawl": {
        "delay_seconds": 1.0,
        "timeout": 20,
        "user_agent": "SheetFillerBot/1.0 (+set-a-contact-url-in-config)",
        "respect_robots": True,
        "playwright_fallback": True,
    },
    "search": {
        "enabled": True,
        "url_template": DEFAULT_SEARCH_URL,
        "max_results_per_site": 8,
    },
    # --- قواعد پر کردن ------------------------------------------------------
    "fill": {
        "overwrite": "empty",
        "retry_partial": False,
        "limit": 0,
        "max_titles_per_session": 0,
        "max_pages_per_session": 0,
        "max_sources_per_title": 5,
        "min_title_similarity": 0.72,
        "batch_rows": 100,
        "cache_ttl_days": 30,
        "multi_select_separator": "، ",
        "max_categories": 3,
        "max_tags": 5,
        "combine_author_scripts": True,
        "labels": {
            "iranian": "ایرانی",
            "foreign": "خارجی",
            "pdf": "پی دی اف",
            "audio": "صوتی",
        },
        "summary": {"min_chars": 200, "max_chars": 1200, "max_sentences": 14},
        "numbers": {"min_agreement": 2, "accept_single": True},
        "taxonomy": {},
    },
    "image": {
        "enabled": True,
        "min_side": 400,
        "square_tolerance": 0.04,
        "max_downloads": 6,
        "require_square": True,
        "allow_unknown_size": False,
        "reject_url_patterns": [],
        "prefer_domains": [],
        "block_domains": [],
    },
    "output": {"xlsx_path": "sheet_filler/data/filled-{stamp}.xlsx"},
}


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path | None = None

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    @property
    def db_path(self) -> str:
        return self.get("database.path", "sheet_filler/data/filler.db")

    @property
    def sites(self) -> list[SourceSite]:
        default_search = self.get("search.url_template", DEFAULT_SEARCH_URL)
        return [
            SourceSite.from_entry(entry, default_search)
            for entry in (self.get("sites", []) or [])
        ]


def _deep_merge(base: dict, override: dict) -> dict:
    """ادغام عمیق با کپی کامل (بدون به‌اشتراک‌گذاری دیکشنری‌های تودرتو)."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
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
                "  Windows : copy sheet_filler\\config.example.yaml config.yaml\n"
                "  Linux/Mac: cp sheet_filler/config.example.yaml config.yaml"
            )
        if yaml is None:  # pragma: no cover
            raise ConfigError("برای خواندن config به PyYAML نیاز است: pip install pyyaml")
        # utf-8-sig یعنی فایلی که Notepad ویندوز با BOM ذخیره کرده هم درست خوانده شود
        data = yaml.safe_load(resolved.read_text(encoding="utf-8-sig")) or {}
    if data and not isinstance(data, dict):
        raise ConfigError("ریشه‌ی config باید یک نگاشت (کلید: مقدار) باشد.")
    return Config(raw=_deep_merge(DEFAULTS, data), path=resolved)
