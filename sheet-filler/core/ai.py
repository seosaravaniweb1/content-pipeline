"""لایه‌ی هوش مصنوعی — **بازبینِ** نتیجه‌ی قواعد، نه جایگزینش.

چرا اصلاً لازم است: قاعده‌ها الگو می‌بینند، معنا نمی‌فهمند. سه چیز هست که با
هیچ قاعده‌ای درست نمی‌شود:

* **کتابِ هم‌نام** — «رمان شب بی‌ستاره» از دو نویسنده‌ی متفاوت وجود دارد؛ فقط
  کسی که متن را می‌فهمد می‌تواند بگوید این صفحه کدام‌شان است.
* **خلاصه** — چسباندن جمله‌های چند سایت خلاصه نیست. مدل متن را می‌خواند و
  خلاصه‌ی واقعی می‌نویسد.
* **دسته و تگ** — «رمان عاشقانه» بودن از روی متن داستان فهمیده می‌شود، نه از
  روی تگ‌های سایت.

سه قاعده‌ی سفت که این ماژول را از «حدس‌زن» جدا می‌کند:

1. **زمین‌گیر (grounded)**: مدل فقط از متنی که به او داده‌ایم استفاده می‌کند.
   دانسته‌های خودش درباره‌ی کتاب، پاسخ معتبر نیست — چون همان‌ها را از خودش
   می‌سازد.
2. **بازبین، نه نویسنده‌ی اول**: قواعد اول جواب می‌دهند، مدل بعد نگاه می‌کند.
   جایی که مدل چیزی نداشته باشد، جوابِ قواعد سر جایش می‌ماند.
3. **قابل ردیابی**: هر سلولی که مدل عوض کرده در گزارش علامت «AI» می‌خورد.

سه ارائه‌دهنده:

* ``local`` — مدلی که روی کامپیوتر خودتان اجرا می‌شود (Ollama). **رایگان**،
  بدون کلید، بدون سقف؛ در عوض کندتر و ضعیف‌تر.
* ``openrouter`` — دروازه‌ای به ده‌ها مدل با یک کلید و یک کیف پول. مدل‌های
  ``:free`` هزینه ندارند (با سقف تعداد)، بقیه ارزان‌اند.
* ``claude`` — Claude از طریق SDK رسمی. دقیق‌ترین، ولی گران‌تر.

پیش‌فرض: خاموش. هیچ‌جای برنامه به این ماژول وابسته نیست.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Sequence

from .details import PageDetails

LOCAL = "local"
OPENROUTER = "openrouter"
CLAUDE = "claude"
PROVIDERS = (LOCAL, OPENROUTER, CLAUDE)

#: مدل‌های پیشنهادی برای هر ارائه‌دهنده (فقط پیش‌فرض؛ هر اسمی قابل دادن است).
#: برای openrouter اسمِ ثابتی ننوشته‌ایم چون فهرستِ مدل‌ها مدام عوض می‌شود؛
#: ``run.py models`` فهرستِ زنده را می‌آورد.
DEFAULT_MODEL = {LOCAL: "qwen3:8b", OPENROUTER: "", CLAUDE: "claude-haiku-4-5"}

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

#: «همه‌ی ردیف‌ها» یا «فقط ردیف‌هایی که قواعد رویشان گیر کرده‌اند»
SCOPE_ALL = "all"
SCOPE_UNSURE = "uncertain"


@dataclass
class AIConfig:
    """تنظیم‌های لایه‌ی هوش مصنوعی."""

    enabled: bool = False
    provider: str = LOCAL
    model: str = ""
    #: آدرس سرویس محلی (Ollama). برای openrouter و claude استفاده نمی‌شود.
    base_url: str = "http://127.0.0.1:11434"
    #: کلید سرویس. خالی یعنی از متغیر محیطی خوانده شود
    #: (``OPENROUTER_API_KEY`` یا ``ANTHROPIC_API_KEY``).
    api_key: str = ""
    scope: str = SCOPE_UNSURE
    timeout: int = 180
    #: چند منبع و چقدر از هر منبع به مدل داده شود. سقفِ هزینه و زمان همین است.
    max_sources: int = 4
    max_chars_per_source: int = 2500
    #: کمتر از این اطمینان، جوابِ مدل نوشته نمی‌شود
    min_confidence: float = 0.6

    @classmethod
    def from_mapping(cls, data: dict | None) -> "AIConfig":
        data = dict(data or {})
        provider = str(data.get("provider") or LOCAL).strip().lower()
        if provider not in PROVIDERS:
            provider = LOCAL
        scope = str(data.get("scope") or SCOPE_UNSURE).strip().lower()
        return cls(
            enabled=bool(data.get("enabled", False)),
            provider=provider,
            model=str(data.get("model") or "").strip() or DEFAULT_MODEL[provider],
            base_url=str(data.get("base_url") or "http://127.0.0.1:11434").strip(),
            api_key=str(data.get("api_key") or "").strip(),
            scope=scope if scope in (SCOPE_ALL, SCOPE_UNSURE) else SCOPE_UNSURE,
            timeout=int(data.get("timeout", 180) or 180),
            max_sources=int(data.get("max_sources", 4) or 4),
            max_chars_per_source=int(data.get("max_chars_per_source", 2500) or 2500),
            min_confidence=float(data.get("min_confidence", 0.6) or 0.6),
        )


@dataclass
class FieldAsk:
    """یک ستون، آن‌طور که به مدل توضیح داده می‌شود."""

    key: str
    column: str
    kind: str
    options: tuple[str, ...] = ()
    max_values: int = 3
    guess: str = ""


@dataclass
class Review:
    """جوابِ مدل، بعد از اعتبارسنجی."""

    values: dict[str, str] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    rejected_sources: list[str] = field(default_factory=list)
    note: str = ""
    ok: bool = True

    @property
    def filled(self) -> bool:
        return bool(self.values or self.rejected_sources)


# ---------------------------------------------------------------------------
# دستور کار مدل
# ---------------------------------------------------------------------------

SYSTEM = """تو دستیارِ کنترل کیفیتِ یک پایگاه‌داده‌ی محصولات فارسی هستی.

به تو عنوانِ یک محصول و متنِ چند صفحه‌ی منبع داده می‌شود. کارَت این است:

۱. تعیین کن هر صفحه واقعاً درباره‌ی **همین** محصول است یا محصولِ هم‌نامِ دیگری
   (مثلاً کتابی با همین اسم ولی از نویسنده‌ی دیگر). صفحه‌ی نامربوط را رد کن.
۲. برای ستون‌های خواسته‌شده مقدار بده.

قواعدِ غیرقابل‌مذاکره:

* **فقط از متنی که به تو داده شده استفاده کن.** اگر خودت این کتاب را
  می‌شناسی، دانسته‌ات را ننویس. چیزی که در متنِ منابع نیست، وجود ندارد.
* اگر مقداری در منابع نبود، همان ستون را خالی («») برگردان. خالی گذاشتن
  درست است؛ ساختن مقدار، خطای فاحش است.
* برای هر ستون یک عددِ اطمینان بین ۰ تا ۱ بده. اگر مطمئن نیستی عدد پایین بده.
* خروجی فقط و فقط JSON باشد، بدون توضیح و بدون بلوک کد.
* همه‌ی متن‌ها فارسی باشند، با نیم‌فاصله‌ی درست.
* در خلاصه هرگز هشتگ، لینک، اسم سایت، «دانلود»، قیمت یا کلمه‌ی کلیدیِ تکراری
  ننویس. خلاصه باید روایتِ خودِ داستان باشد."""

SCHEMA_NOTE = """شکل خروجی:

{
  "sources": [{"id": 1, "same_product": true, "why": "دلیل کوتاه"}, ...],
  "fields": {"<کلید ستون>": {"value": "...", "confidence": 0.0}},
  "note": "هر چیزی که آدم باید بداند، کوتاه"
}"""

KIND_HINT = {
    "summary": "خلاصه‌ی جامع از همه‌ی منابع، ۳ تا ۶ جمله، روایتِ داستان",
    "person": "فقط نام شخص، بدون عنوان و بدون توضیح",
    "number": "فقط عدد، با رقم لاتین",
    "choice": "دقیقاً یکی از گزینه‌های مجاز",
    "multi_choice": "چند گزینه از فهرست مجاز، جدا شده با «، »",
    "text": "مقدار کوتاه",
    "keyword": "کلمه‌ی کلیدی اصلی",
    "boolean": "یکی از دو گزینه‌ی مجاز",
}


def build_prompt(title: str, asks: Sequence[FieldAsk], pages: Sequence[PageDetails],
                 config: AIConfig) -> str:
    """متنِ کاربر: عنوان + ستون‌های خواسته‌شده + شواهدِ هر منبع."""
    lines = [f"عنوان محصول: {title}", "", "ستون‌هایی که باید مقدار بگیرند:"]
    for ask in asks:
        bits = [f"- {ask.key} («{ask.column}»)", KIND_HINT.get(ask.kind, ask.kind)]
        if ask.options:
            allowed = "، ".join(ask.options[:60])
            bits.append(f"گزینه‌های مجاز: {allowed}")
            if ask.kind == "multi_choice":
                bits.append(f"حداکثر {ask.max_values} تا")
        if ask.guess:
            bits.append(f"حدسِ فعلیِ برنامه: {ask.guess[:120]}")
        lines.append(" — ".join(bits))

    lines += ["", "منابع:"]
    for index, page in enumerate(pages[: config.max_sources], start=1):
        lines.append(f"\n### منبع {index} ({page.domain})")
        lines.append(page_evidence(page, config.max_chars_per_source))
    lines += ["", SCHEMA_NOTE]
    return "\n".join(lines)


def page_evidence(page: PageDetails, max_chars: int) -> str:
    """متنِ یک صفحه، خلاصه‌شده — نه HTML خام.

    HTML خام هم گران است هم پر از منو و فوتر؛ آنچه مدل لازم دارد همان چیزی
    است که استخراج‌گر بیرون کشیده.
    """
    parts = [f"عنوان صفحه: {page.title}"]
    if page.authors:
        parts.append("نویسنده(های) اعلام‌شده: " + "، ".join(page.authors))
    if page.translators:
        parts.append("مترجم(های) اعلام‌شده: " + "، ".join(page.translators))
    if page.page_counts:
        parts.append("تعداد صفحه‌ی اعلام‌شده: " + "، ".join(str(n) for n in page.page_counts))
    labels = []
    for key, values in list(page.labeled.items())[:40]:
        clean = [str(value).strip() for value in values if str(value).strip()][:2]
        if clean:
            labels.append(f"{key}: {' | '.join(clean)}")
    if labels:
        parts.append("مشخصات: " + "؛ ".join(labels))
    if page.categories:
        parts.append("دسته‌های سایت: " + "، ".join(page.categories[:12]))
    if page.tags:
        parts.append("تگ‌های سایت: " + "، ".join(page.tags[:12]))
    body = " ".join(block.text for block in page.summaries)
    if body:
        parts.append("متن: " + body)
    text = "\n".join(parts)
    return text if len(text) <= max_chars else text[:max_chars] + " …"


# ---------------------------------------------------------------------------
# ارائه‌دهنده‌ها
# ---------------------------------------------------------------------------


class AIError(RuntimeError):
    """خطای لایه‌ی هوش مصنوعی — هیچ‌وقت اجرا را نمی‌اندازد، فقط لاگ می‌شود."""


def ask_model(config: AIConfig, prompt: str) -> str:
    if config.provider == CLAUDE:
        return _ask_claude(config, prompt)
    if config.provider == OPENROUTER:
        return _ask_openrouter(config, prompt)
    return _ask_local(config, prompt)


def _ask_claude(config: AIConfig, prompt: str) -> str:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - بستگی به نصب کاربر
        raise AIError("کتابخانه‌ی anthropic نصب نیست: pip install anthropic") from exc

    kwargs: dict[str, Any] = {}
    if config.api_key:
        kwargs["api_key"] = config.api_key
    client = anthropic.Anthropic(timeout=float(config.timeout), **kwargs)
    try:
        response = client.messages.create(
            model=config.model or DEFAULT_MODEL[CLAUDE],
            max_tokens=4096,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — هر خطای شبکه/کلید یکسان رفتار می‌شود
        raise AIError(f"{type(exc).__name__}: {exc}") from exc
    return "".join(block.text for block in response.content if block.type == "text")


def _ask_openrouter(config: AIConfig, prompt: str) -> str:
    """OpenRouter — یک کلید، ده‌ها مدل، از جمله مدل‌های رایگانِ ``:free``."""
    key = config.api_key or _env("OPENROUTER_API_KEY")
    if not key:
        raise AIError("کلید OpenRouter داده نشده (تنظیمِ «کلید سرویس» یا OPENROUTER_API_KEY)")
    if not config.model:
        raise AIError("مدل انتخاب نشده — با «python -m sheet_filler.run models» فهرست را ببینید")
    payload = json.dumps(
        {
            "model": config.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        OPENROUTER_BASE + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            # OpenRouter این دو را برای شناسایی برنامه می‌خواهد (اختیاری ولی مؤدبانه)
            "HTTP-Referer": "https://github.com/sheet-filler",
            "X-Title": "Sheet Filler",
        },
    )
    body = _json_request(request, config.timeout)
    if body.get("error"):
        raise AIError(str((body["error"] or {}).get("message") or body["error"])[:200])
    choices = body.get("choices") or []
    if not choices:
        raise AIError("OpenRouter جوابی برنگرداند")
    return str(((choices[0] or {}).get("message") or {}).get("content") or "")


def openrouter_models(api_key: str = "", timeout: int = 30) -> list[dict]:
    """فهرستِ **زنده**‌ی مدل‌های OpenRouter، مرتب: رایگان‌ها اول، بعد ارزان‌ها.

    اسم و قیمتِ مدل‌ها مدام عوض می‌شود، پس هیچ فهرستی داخل کد نوشته نشده؛
    همیشه از خودِ سرویس پرسیده می‌شود.
    """
    headers = {"Content-Type": "application/json"}
    key = api_key or _env("OPENROUTER_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = _json_request(urllib.request.Request(OPENROUTER_BASE + "/models", headers=headers), timeout)
    out: list[dict] = []
    for item in body.get("data") or []:
        if not isinstance(item, dict):
            continue
        pricing = item.get("pricing") or {}
        try:
            prompt_cost = float(pricing.get("prompt") or 0)
            completion_cost = float(pricing.get("completion") or 0)
        except (TypeError, ValueError):
            prompt_cost = completion_cost = 0.0
        out.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                # قیمتِ OpenRouter بر حسب دلار به ازای **هر توکن** است
                "in_per_m": round(prompt_cost * 1_000_000, 3),
                "out_per_m": round(completion_cost * 1_000_000, 3),
                "context": int(item.get("context_length") or 0),
                "free": prompt_cost == 0 and completion_cost == 0,
            }
        )
    out.sort(key=lambda row: (not row["free"], row["in_per_m"] + row["out_per_m"], row["id"]))
    return [row for row in out if row["id"]]


def _json_request(request: urllib.request.Request, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
        raise AIError(f"HTTP {exc.code} — {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise AIError(f"به سرویس وصل نشد — {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise AIError(f"{type(exc).__name__}: {exc}") from exc


def _env(name: str) -> str:
    import os

    return (os.environ.get(name) or "").strip()


def _ask_local(config: AIConfig, prompt: str) -> str:
    """Ollama — سرویسِ محلی، بدون کلید و بدون هزینه."""
    payload = json.dumps(
        {
            "model": config.model or DEFAULT_MODEL[LOCAL],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    url = config.base_url.rstrip("/") + "/api/chat"
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        raise AIError(
            f"به سرویس محلی وصل نشد ({url}). Ollama بالا است؟ — {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise AIError(f"{type(exc).__name__}: {exc}") from exc
    return str((body.get("message") or {}).get("content") or "")


# ---------------------------------------------------------------------------
# اعتبارسنجیِ جوابِ مدل
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_reply(text: str) -> dict:
    """متنِ مدل → dict. مدل‌های محلی گاهی دور JSON حرف می‌زنند."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        match = _JSON_BLOCK.search(text)
        if not match:
            raise AIError("جوابِ مدل JSON نبود")
        try:
            data = json.loads(match.group(0))
        except (TypeError, ValueError) as exc:
            raise AIError("جوابِ مدل JSONِ سالم نبود") from exc
    if not isinstance(data, dict):
        raise AIError("جوابِ مدل شکل درستی نداشت")
    return data


def review(
    title: str,
    pages: Sequence[PageDetails],
    asks: Sequence[FieldAsk],
    config: AIConfig,
    ask: Any = None,
) -> Review:
    """یک ردیف را به مدل بده و جوابِ **اعتبارسنجی‌شده** بگیر.

    ``ask`` فقط برای تست تزریق می‌شود؛ در عمل :func:`ask_model` است.
    """
    if not pages or not asks:
        return Review(ok=True, note="چیزی برای بازبینی نبود")
    caller = ask or ask_model
    used = list(pages)[: config.max_sources]
    data = parse_reply(caller(config, build_prompt(title, asks, used, config)))

    result = Review(note=str(data.get("note") or "")[:200])
    for item in data.get("sources") or []:
        if not isinstance(item, dict) or item.get("same_product") is not False:
            continue
        try:
            page = used[int(item.get("id", 0)) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        result.rejected_sources.append(page.url)

    by_key = {ask_.key: ask_ for ask_ in asks}
    for key, payload in (data.get("fields") or {}).items():
        spec = by_key.get(str(key))
        if spec is None or not isinstance(payload, dict):
            continue
        value = _clean_value(payload.get("value"), spec)
        if not value:
            continue
        try:
            score = float(payload.get("confidence", 0))
        except (TypeError, ValueError):
            score = 0.0
        if score < config.min_confidence:
            continue
        result.values[spec.key] = value
        result.confidence[spec.key] = round(score, 2)
    return result


def _clean_value(raw: Any, ask: FieldAsk) -> str:
    """مقدارِ مدل → مقداری که واقعاً می‌شود در شیت نوشت.

    مهم‌ترین بخش: برای ستونِ کرکره‌ای، هر چیزی که در فهرستِ مجاز نباشد دور
    ریخته می‌شود. مدل اجازه ندارد گزینه‌ی تازه بسازد — کرکره‌ی گوگل‌شیت
    قبولش نمی‌کند و ردیف قرمز می‌شود.
    """
    if isinstance(raw, (list, tuple)):
        parts = [str(item).strip() for item in raw if str(item).strip()]
    else:
        parts = [part.strip() for part in str(raw or "").split("،") if part.strip()]
    if not parts:
        return ""

    if ask.options:
        allowed = {_key(option): option for option in ask.options}
        parts = [allowed[_key(part)] for part in parts if _key(part) in allowed]
        if not parts:
            return ""
    if ask.kind in ("choice", "boolean", "number", "person", "keyword", "text"):
        parts = parts[:1]
    elif ask.kind == "multi_choice":
        parts = parts[: max(1, ask.max_values)]
    else:
        return str(raw or "").strip()
    return "، ".join(dict.fromkeys(parts))


def _key(text: str) -> str:
    return re.sub(r"[\s‌_\-]+", "", str(text or "")).strip().lower()
