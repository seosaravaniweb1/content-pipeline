"""تنظیمات کاربر — **یک** جای تعریف، نه پنج جا.

قبلاً هر تنظیم در پنج فایل تکرار می‌شد (config، dataclass، سرور پنل، جاوااسکریپت،
نمونه‌ی yaml) و اضافه کردن یک گزینه یعنی پنج تغییر. اینجا هر تنظیم **یک بار**
تعریف می‌شود و بقیه از رویش ساخته می‌شوند: فرم پنل از همین فهرست رندر می‌شود،
اعتبارسنجی از همین‌جا می‌آید و مقدار پیش‌فرض هم همین‌جاست.

قاعده‌ی انتخاب: چیزی اینجا می‌آید که **کاربر باید تصمیم بگیرد**. هر چیزی که
برنامه می‌تواند خودش بفهمد (شناسه‌ی شیت از روی آدرس، دامنه‌ی منابع از روی
لینک‌ها، سرعت و دسته‌بندی نوشتن) تنظیم نیست — خودکار است.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

TEXT = "text"
NUMBER = "number"
BOOL = "bool"
CHOICE = "choice"
LINES = "lines"


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str
    label: str
    default: Any = ""
    hint: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    #: ``True`` یعنی در بخش «پیشرفته»ی پنل می‌نشیند
    advanced: bool = False
    #: فقط برای عددها
    minimum: int = 0

    def coerce(self, value: Any) -> Any:
        if self.kind == BOOL:
            return bool(value)
        if self.kind == NUMBER:
            try:
                return max(self.minimum, int(value or 0))
            except (TypeError, ValueError):
                raise ValueError(f"«{self.label}» باید عدد باشد.") from None
        if self.kind == LINES:
            return clean_sites(value)
        if self.kind == CHOICE:
            allowed = [key for key, _ in self.choices]
            text = str(value or "").strip()
            return text if text in allowed else self.default
        return str(value or "").strip()


#: تنظیم‌هایی که کاربر می‌بیند. بقیه‌ی رفتارها خودکارند یا در ``config.yaml``
#: به‌عنوان تنظیم پیشرفته می‌مانند.
FIELDS: tuple[Setting, ...] = (
    Setting(
        "sheet_url",
        TEXT,
        "آدرس گوگل‌شیت",
        hint="کل آدرس را از نوار مرورگر کپی کنید؛ شناسه‌اش خودکار درمی‌آید.",
    ),
    Setting(
        "service_account_json",
        TEXT,
        "فایل service account",
        hint="مسیر فایل json. اگر کنار config.yaml باشد خودکار پیدا می‌شود.",
    ),
    Setting("tab", TEXT, "تبِ محصولات", hint="خالی = اولین تب شیت"),
    Setting(
        "file",
        TEXT,
        "یا فایل محلی",
        hint="csv/xlsx — برای وقتی هنوز service account ندارید",
    ),
    Setting(
        "sites",
        LINES,
        "سایت‌های منبع",
        default=(),
        hint="هر خط یک آدرس. خالی بگذارید تا از روی لینک‌های ایمپورت‌شده خودش پیدا کند.",
    ),
    Setting(
        "overwrite",
        CHOICE,
        "سلول‌های پرشده",
        default="empty",
        choices=(
            ("empty", "دست نخورند — فقط سلول خالی پر شود"),
            ("mine", "فقط چیزی که خودِ برنامه نوشته بود دوباره ساخته شود"),
            ("always", "همه بازنویسی شوند"),
        ),
        hint="«فقط چیزی که خودِ برنامه نوشته بود» برای وقتی است که قواعد دقیق‌تر شده‌اند "
        "و می‌خواهید ردیف‌های قبلی از نو ساخته شوند، بدون اینکه دست‌نوشته‌های شما عوض شود.",
    ),
    Setting(
        "auto",
        BOOL,
        "حالت خودکار",
        default=False,
        hint="خودش هر چند دقیقه یک‌بار شیت را می‌خواند و ردیف‌های تازه را پر می‌کند.",
    ),
    Setting(
        "auto_every_minutes",
        NUMBER,
        "هر چند دقیقه",
        default=30,
        minimum=1,
        hint="فقط وقتی حالت خودکار روشن است.",
    ),
    Setting(
        "image",
        BOOL,
        "جمع‌آوری تصویر کاور",
        default=True,
        hint="خاموشش کنید تا ستون تصویر دست‌نخورده بماند و اجرا وقتش را صرف کاور نکند.",
    ),
    # --- پیشرفته ---------------------------------------------------------
    Setting(
        "sources_column",
        TEXT,
        "ستون آدرس منابع",
        advanced=True,
        hint="اگر لینک صفحه‌ی محصول در خود شیت است، نام آن ستون.",
    ),
    Setting("title_column", TEXT, "ستون عنوان", advanced=True, hint="خالی = ستون اول"),
    Setting("lists_tab", TEXT, "تبِ لیست‌ها", default="لیست‌ها", advanced=True),
    Setting("report_tab", TEXT, "تبِ گزارش", default="گزارش تکمیل", advanced=True),
    Setting("header_row", NUMBER, "سطر عنوان ستون‌ها", default=1, minimum=1, advanced=True),
    Setting(
        "max_sources_per_title",
        NUMBER,
        "حداکثر منبع برای هر عنوان",
        default=5,
        minimum=1,
        advanced=True,
    ),
    Setting(
        "limit",
        NUMBER,
        "سقف ردیف در هر اجرا",
        default=0,
        advanced=True,
        hint="۰ = تا آخر شیت. برای آزمایش، دکمه‌ی «آزمایش با ۲۰ ردیف» هست.",
    ),
    Setting(
        "retry_partial",
        BOOL,
        "ردیف‌های ناقص دوباره امتحان شوند",
        default=False,
        advanced=True,
    ),
)

BY_KEY = {setting.key: setting for setting in FIELDS}


def defaults() -> dict[str, Any]:
    return {setting.key: setting.default for setting in FIELDS}


def describe(advanced: bool | None = None) -> list[dict]:
    """فهرست تنظیم‌ها برای رندر شدن در پنل."""
    return [
        {
            "key": setting.key,
            "kind": setting.kind,
            "label": setting.label,
            "hint": setting.hint,
            "choices": [{"value": key, "label": label} for key, label in setting.choices],
            "advanced": setting.advanced,
        }
        for setting in FIELDS
        if advanced is None or setting.advanced == advanced
    ]


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    """ورودی خام (پنل یا config) → مقدارهای تمیز و تایپ‌شده.

    کلیدهای ناشناخته دست‌نخورده رد می‌شوند تا تنظیم‌های پیشرفته‌ی
    ``config.yaml`` از بین نروند.
    """
    out: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        setting = BY_KEY.get(key)
        out[key] = setting.coerce(value) if setting else value
    return out


# ---------------------------------------------------------------------------
# چیزهایی که خودکار فهمیده می‌شوند
# ---------------------------------------------------------------------------

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]{20,})")
_GID_RE = re.compile(r"[#&?]gid=(\d+)")


def sheet_id_from(text: str) -> str:
    """آدرس کامل گوگل‌شیت → شناسه. خودِ شناسه هم قبول است.

    کاربر نباید مجبور باشد تکه‌ی وسط آدرس را دستی جدا کند؛ کل آدرس را از نوار
    مرورگر کپی می‌کند و همین کافی است.
    """
    text = str(text or "").strip()
    if not text:
        return ""
    match = _SHEET_ID_RE.search(text)
    if match:
        return match.group(1)
    if "/" in text or " " in text:
        return ""
    return text


def find_service_account(hint: str, near: Path | None = None) -> str:
    """فایل service account: مسیر داده‌شده، وگرنه جستجوی کنار config.

    فایل کلید گوگل یک json با کلید ``"type": "service_account"`` است؛ همین را
    کنار config یا در پوشه‌ی جاری می‌گردیم تا کاربر مسیر تایپ نکند.
    """
    hint = str(hint or "").strip()
    if hint and Path(hint).exists():
        return hint
    folders = [folder for folder in (near, Path.cwd()) if folder]
    for folder in folders:
        base = folder if folder.is_dir() else folder.parent
        for candidate in sorted(base.glob("*.json")):
            try:
                text = candidate.read_text(encoding="utf-8")[:400]
            except OSError:  # pragma: no cover - فایل قفل‌شده
                continue
            if '"type"' in text and "service_account" in text:
                return str(candidate)
    return hint


MAX_SITES = 100


def clean_sites(raw: Any) -> list[str]:
    """متن چسبانده‌شده‌ی کاربر → فهرست آدرس یکتا و مرتب."""
    if isinstance(raw, str):
        lines: Sequence[str] = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        lines = [str(item) for item in raw]
    else:
        return []
    out: list[str] = []
    for line in lines:
        url = str(line).strip().strip(",،")
        if not url or url.startswith("#"):
            continue
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if "." not in url.split("//", 1)[-1]:
            continue
        url = url.rstrip("/")
        if url not in out:
            out.append(url)
        if len(out) >= MAX_SITES:
            break
    return out
