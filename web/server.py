"""سرور پنل مدیریت — فقط کتابخانه‌ی استاندارد پایتون.

نکات امنیتی که عمداً این‌طور نوشته شده‌اند:

* سرور فقط روی ``127.0.0.1`` بالا می‌آید. باز کردنش روی شبکه یعنی هر کسی
  می‌تواند کراول راه بیندازد و config شما را بازنویسی کند.
* هدر ``Host`` باید لوکال باشد؛ این جلوی DNS rebinding را می‌گیرد.
* هر درخواست ``/api/*`` به توکن نشست نیاز دارد. توکن در همان URL که در
  مرورگر باز می‌شود می‌آید و صفحه آن را در ``sessionStorage`` نگه می‌دارد؛
  پس صفحه‌های دیگر مرورگر نمی‌توانند به API دست بزنند.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import shutil
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

from ..core import db, normalizer, pipeline, presets
from ..output import exporter
from ..core.config import Config, ConfigError, load_config
from . import jobs

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 2 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# وضعیت مشترک بین تردهای درخواست
# ---------------------------------------------------------------------------


class PanelState:
    def __init__(self, config: Config, config_path: Path | None, token: str) -> None:
        self.config = config
        self.config_path = config_path
        self.token = token
        self.runner = jobs.JobRunner(config)
        self._local = threading.local()
        self._lock = threading.Lock()

    # هر ترد اتصال sqlite خودش را دارد
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self.config.db_path)
            self._local.conn = conn
        return conn

    def close_conn(self) -> None:
        """اتصال ترد جاری. هر درخواست HTTP ترد تازه‌ای دارد؛ بدون این،
        اتصال‌ها تا بسته‌شدن سرور روی هم جمع می‌شوند."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def reload_config(self) -> None:
        """بعد از ذخیره‌ی config، اتصال‌های باز باید دور ریخته شوند."""
        with self._lock:
            self.config = load_config(self.config_path) if self.config_path else self.config
            self.runner.config = self.config
        self.close_conn()

    def resolve_run(self, run_id: str | None) -> str:
        if run_id:
            if db.get_run(self.conn(), run_id) is None:
                raise ApiError(f"run_id پیدا نشد: {run_id}", HTTPStatus.NOT_FOUND)
            return run_id
        row = db.latest_run(self.conn())
        if row is None:
            raise ApiError("هنوز هیچ اجرایی ساخته نشده است.", HTTPStatus.NOT_FOUND)
        return row["run_id"]


# ---------------------------------------------------------------------------
# سریال‌سازی ردیف‌ها
# ---------------------------------------------------------------------------


def run_dict(row: sqlite3.Row) -> dict:
    return {
        "run_id": row["run_id"],
        "target_topic": row["target_topic"],
        "status": row["status"],
        "current_phase": row["current_phase"],
        "created_at": row["created_at"],
    }


def product_dict(conn: sqlite3.Connection, row: sqlite3.Row, detail: bool = False) -> dict:
    data = {
        "id": row["id"],
        "title": row["canonical_title"],
        "needs_review": bool(row["needs_review"]),
        "merge_confidence": row["merge_confidence"],
        "suggest_status": row["suggest_status"],
        "domains": db.source_domains_for(conn, row["id"]),
        "member_count": int(
            conn.execute(
                "SELECT COUNT(*) c FROM product_mapping WHERE canonical_id=?", (row["id"],)
            ).fetchone()["c"]
        ),
        "keyword_count": int(
            conn.execute(
                "SELECT COUNT(*) c FROM lsi_keywords WHERE canonical_id=?", (row["id"],)
            ).fetchone()["c"]
        ),
    }
    if detail:
        data["entity_tokens"] = json.loads(row["entity_tokens"] or "[]")
        data["members"] = [
            {
                "raw_id": m["id"],
                "raw_title": m["raw_title"],
                "normalized_title": m["normalized_title"],
                "domain": m["source_domain"],
                "url": m["source_url"],
                "topic_score": m["topic_score"],
            }
            for m in db.members_of(conn, row["id"])
        ]
        data["keywords"] = [k["keyword"] for k in db.keywords_for(conn, row["id"])]
    return data


def suggest_queries_of(state: "PanelState", title: str) -> list[str]:
    """شکل‌هایی از عنوان که از گوگل پرسیده می‌شوند — قابل دیدن قبل از اجرا."""
    norm_config = normalizer.config_from_mapping(state.config.normalizer)
    max_words = int(state.config.get("suggest.max_query_words", 8))
    limit = max(1, int(state.config.get("suggest.max_variants_per_product", 4)))
    prefixes = state.config.get(
        "suggest.query_prefixes", list(normalizer.DEFAULT_QUERY_PREFIXES)
    )
    variants = normalizer.search_variants(title, norm_config, max_words, prefixes=prefixes)
    return variants[:limit] or [title]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def api_state(state: PanelState, query: dict) -> dict:
    conn = state.conn()
    runs = [run_dict(row) for row in db.list_runs(conn, limit=50)]
    run_id = query.get("run_id") or (runs[0]["run_id"] if runs else "")
    current = None
    if run_id:
        row = db.get_run(conn, run_id)
        if row is None:
            run_id = runs[0]["run_id"] if runs else ""
            row = db.get_run(conn, run_id) if run_id else None
        if row is not None:
            current = run_dict(row)
            current["counters"] = pipeline.counters(conn, run_id)
    return {
        "runs": runs,
        "current": current,
        "phases": [{"n": n, "name": name} for n, name in pipeline.PHASE_NAMES.items()],
        "config_path": str(state.config_path) if state.config_path else "",
        "target_topic": state.config.target_topic,
        "db_path": state.config.db_path,
        "job": state.runner.snapshot(int(query.get("since") or 0)),
    }


MAX_SITES = 300


def clean_sites(raw: object) -> list[str]:
    """متن چسبانده‌شده‌ی کاربر → فهرست آدرس یکتا.

    هر چیزی که آدرس نباشد کنار گذاشته می‌شود و آدرس بدون ``http`` هم قبول
    است (``example.ir`` → ``https://example.ir``) چون کاربر معمولاً از اکسل
    یا مرورگر کپی می‌کند.
    """
    if isinstance(raw, str):
        items = raw.replace(",", "\n").split("\n")
    elif isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        url = str(item).strip().strip("\u200c").rstrip("/")
        if not url or url.startswith("#"):
            continue
        if "://" not in url:
            url = f"https://{url}"
        parts = urlparse(url)
        if parts.scheme not in {"http", "https"} or "." not in parts.netloc:
            continue
        key = parts.netloc.lower() + parts.path.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def api_presets(state: PanelState, query: dict) -> dict:
    return {"presets": presets.as_dicts(), "max_sites": MAX_SITES}


def clean_options(raw: object) -> dict:
    """تنظیمات اجرا از پنل — فقط کلیدهای شناخته‌شده و در بازه‌ی معقول."""
    data = raw if isinstance(raw, dict) else {}
    cap = data.get("max_products_per_site")
    try:
        cap = max(0, min(100000, int(cap or 0)))
    except (TypeError, ValueError):
        cap = 0
    return {
        "max_products_per_site": cap,
        "require_product_signals": bool(data.get("require_product_signals", True)),
        "js": bool(data.get("js")),
    }


def api_create_run(state: PanelState, body: dict) -> dict:
    categories = [str(c) for c in (body.get("categories") or []) if str(c).strip()]
    sites = clean_sites(body.get("sites"))
    if len(sites) > MAX_SITES:
        raise ApiError(f"حداکثر {MAX_SITES} سایت در هر اجرا؛ شما {len(sites)} تا دادید.")
    labels = [preset.label for preset in presets.resolve(categories)]
    topic = (body.get("topic") or "، ".join(labels) or state.config.target_topic or "").strip()
    if not topic:
        raise ApiError("نوع محصول انتخاب نشده است. حداقل یک دسته را تیک بزنید.")
    options = clean_options(body.get("options"))
    run_id = db.create_run(
        state.conn(), topic, categories=labels, sites=sites, options=options
    )
    return {
        "run_id": run_id,
        "target_topic": topic,
        "categories": labels,
        "sites": len(sites),
        "options": options,
    }


def api_plan(state: PanelState, query: dict) -> dict:
    """نقشه‌ی اجرا: دسته‌ها و سایت‌هایش."""
    conn = state.conn()
    run_id = state.resolve_run(query.get("run_id"))
    row = db.get_run(conn, run_id)
    sites = db.run_sites(conn, run_id)
    return {
        "run_id": run_id,
        "target_topic": row["target_topic"],
        "categories": db.run_categories(conn, run_id),
        "options": db.run_options(conn, run_id) or clean_options({}),
        "sites": sites,
        "from_config": not sites,
        "config_sites": [site.base_url for site in state.config.sites] if not sites else [],
    }


def api_save_plan(state: PanelState, body: dict) -> dict:
    _guard_idle(state)
    conn = state.conn()
    run_id = state.resolve_run(body.get("run_id"))
    categories = None
    if "categories" in body:
        categories = [preset.label for preset in presets.resolve(body.get("categories") or [])]
    sites = None
    if "sites" in body:
        sites = clean_sites(body.get("sites"))
        if len(sites) > MAX_SITES:
            raise ApiError(f"حداکثر {MAX_SITES} سایت در هر اجرا؛ شما {len(sites)} تا دادید.")
    topic = body.get("topic")
    if topic is None and categories:
        topic = "، ".join(categories)
    options = clean_options(body.get("options")) if "options" in body else None
    db.set_run_plan(
        conn, run_id, categories=categories, sites=sites, topic=topic, options=options
    )
    return api_plan(state, {"run_id": run_id})


def api_delete_run(state: PanelState, body: dict) -> dict:
    run_id = body.get("run_id") or ""
    if state.runner.running and state.runner.snapshot()["run_id"] == run_id:
        raise ApiError("این اجرا در حال اجراست؛ اول لغوش کنید.", HTTPStatus.CONFLICT)
    state.resolve_run(run_id)
    db.delete_run(state.conn(), run_id)
    return {"deleted": run_id}


def api_get_config(state: PanelState, query: dict) -> dict:
    path = state.config_path
    text = ""
    if path is not None and path.exists():
        text = path.read_text(encoding="utf-8-sig")
    return {"path": str(path) if path else "", "text": text, "editable": path is not None}


def api_save_config(state: PanelState, body: dict) -> dict:
    path = state.config_path
    if path is None:
        raise ApiError("پنل بدون فایل config اجرا شده؛ چیزی برای ذخیره نیست.")
    text = (body.get("text") or "").lstrip("\ufeff")
    try:
        import yaml  # noqa: PLC0415 — اختیاری است
    except ImportError:  # pragma: no cover
        raise ApiError("PyYAML نصب نیست: pip install pyyaml") from None
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ApiError(f"YAML نامعتبر است: {exc}") from None
    if parsed is not None and not isinstance(parsed, dict):
        raise ApiError("ریشه‌ی config باید یک نگاشت (کلید: مقدار) باشد.")
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(text, encoding="utf-8")
    try:
        state.reload_config()
    except ConfigError as exc:
        raise ApiError(str(exc)) from None
    return {"saved": str(path), "backup": str(path.with_suffix(path.suffix + ".bak"))}


def api_job(state: PanelState, query: dict) -> dict:
    return state.runner.snapshot(int(query.get("since") or 0))


def api_start_job(state: PanelState, body: dict) -> dict:
    run_id = state.resolve_run(body.get("run_id"))
    phases = body.get("phases") or []
    if not isinstance(phases, list):
        raise ApiError("phases باید فهرست باشد.")
    try:
        state.runner.start(run_id, phases, state.config)
    except jobs.JobBusy as exc:
        raise ApiError(str(exc), HTTPStatus.CONFLICT) from None
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    return state.runner.snapshot()


def api_cancel_job(state: PanelState, body: dict) -> dict:
    return {"cancelled": state.runner.cancel()}


def api_products(state: PanelState, query: dict) -> dict:
    conn = state.conn()
    run_id = state.resolve_run(query.get("run_id"))
    where = ["run_id=?"]
    params: list[Any] = [run_id]
    mode = query.get("filter") or "all"
    if mode == "has_suggest":
        where.append("suggest_status='has_suggest'")
    elif mode == "no_suggest":
        where.append("suggest_status='no_suggest'")
    elif mode == "pending":
        where.append("suggest_status IS NULL")
    elif mode == "needs_review":
        where.append("needs_review=1")
    elif mode == "merged":
        where.append("id IN (SELECT canonical_id FROM product_mapping"
                     " GROUP BY canonical_id HAVING COUNT(*)>1)")
    term = (query.get("q") or "").strip()
    if term:
        where.append("canonical_title LIKE ?")
        params.append(f"%{term}%")

    clause = " AND ".join(where)
    total = int(
        conn.execute(
            f"SELECT COUNT(*) c FROM canonical_products WHERE {clause}", params
        ).fetchone()["c"]
    )
    limit = max(1, min(500, int(query.get("limit") or 100)))
    offset = max(0, int(query.get("offset") or 0))
    rows = conn.execute(
        f"SELECT * FROM canonical_products WHERE {clause}"
        " ORDER BY needs_review DESC, id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {
        "run_id": run_id,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [product_dict(conn, row) for row in rows],
    }


def api_product(state: PanelState, query: dict) -> dict:
    conn = state.conn()
    row = conn.execute(
        "SELECT * FROM canonical_products WHERE id=?", (int(query.get("id") or 0),)
    ).fetchone()
    if row is None:
        raise ApiError("محصول پیدا نشد.", HTTPStatus.NOT_FOUND)
    data = product_dict(conn, row, detail=True)
    data["suggest_queries"] = suggest_queries_of(state, row["canonical_title"] or "")
    data["keyword_sources"] = {
        item["keyword"]: item["source_query"] or ""
        for item in db.keywords_for(conn, int(row["id"]))
    }
    return data


def _guard_idle(state: PanelState) -> None:
    if state.runner.running:
        raise ApiError(
            "در حین اجرای فازها ویرایش دستی ممکن نیست؛ صبر کنید یا لغو کنید.",
            HTTPStatus.CONFLICT,
        )


def api_merge(state: PanelState, body: dict) -> dict:
    _guard_idle(state)
    try:
        moved = db.merge_canonicals(
            state.conn(), int(body.get("target_id") or 0), int(body.get("source_id") or 0)
        )
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    return {"moved": moved}


def api_detach(state: PanelState, body: dict) -> dict:
    _guard_idle(state)
    try:
        new_id = db.detach_raw_product(state.conn(), int(body.get("raw_id") or 0))
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    return {"canonical_id": new_id}


def api_rename(state: PanelState, body: dict) -> dict:
    _guard_idle(state)
    try:
        db.rename_canonical(state.conn(), int(body.get("id") or 0), body.get("title") or "")
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    return {"ok": True}


def api_review_flag(state: PanelState, body: dict) -> dict:
    db.set_needs_review(state.conn(), int(body.get("id") or 0), bool(body.get("flag")))
    return {"ok": True}


def api_review(state: PanelState, query: dict) -> dict:
    conn = state.conn()
    run_id = state.resolve_run(query.get("run_id"))
    products = conn.execute(
        "SELECT * FROM canonical_products WHERE run_id=? AND needs_review=1 ORDER BY id",
        (run_id,),
    ).fetchall()
    borderline = db.borderline_raw_products(conn, run_id)
    return {
        "run_id": run_id,
        "products": [product_dict(conn, row, detail=True) for row in products],
        "borderline": [
            {
                "raw_id": row["id"],
                "raw_title": row["raw_title"],
                "domain": row["source_domain"],
                "url": row["source_url"],
                "topic_score": row["topic_score"],
            }
            for row in borderline
        ],
    }


def api_normalize(state: PanelState, query: dict) -> dict:
    text = query.get("text") or ""
    norm_config = normalizer.config_from_mapping(state.config.normalizer)
    return {
        "input": text,
        "display": normalizer.normalize_display(text, norm_config),
        "match": normalizer.normalize(text, norm_config),
        "tokens": normalizer.meaningful_tokens(text, norm_config),
    }


def output_files(state: PanelState, run_id: str) -> list[Path]:
    """فایل‌هایی که فاز ۴ روی دیسک نوشته است (اگر نوشته باشد)."""
    template = str(
        state.config.get("output.xlsx_path", "content_pipeline/data/output-{run_id}.xlsx")
    )
    path = Path(template.replace("{run_id}", run_id))
    if path.exists():
        return [path]
    return sorted(path.parent.glob(f"{path.stem}__*.csv"))


def api_output(state: PanelState, query: dict) -> dict:
    """چه چیزهایی قابل دانلود است.

    خروجی همین‌جا از دیتابیس ساخته می‌شود، نه از فایل قبلی؛ پس ادغام‌های دستی
    که بعد از فاز ۴ انجام داده‌اید هم در دانلود دیده می‌شوند.
    """
    conn = state.conn()
    run_id = state.resolve_run(query.get("run_id"))
    counts = pipeline.counters(conn, run_id)
    hints = {
        "ready": "محصولاتی که در گوگل ساجست دارند",
        "archive": "محصولاتی که ساجست ندارند",
        "all": "همه با هم، با ستون «ساجست: دارد/ندارد»",
        "review": "مواردی که ابزار مطمئن نبوده",
    }
    tabs = state.config.get("output.tabs", {}) or {}
    titles = {
        "ready": tabs.get("ready", "آماده تولید محتوا"),
        "archive": tabs.get("archive", "آرشیو آینده"),
        "all": tabs.get("all", "همه محصولات"),
        "review": tabs.get("review", "نیاز به بازبینی دستی"),
    }
    rows = {
        "ready": counts["has_suggest"],
        "archive": counts["no_suggest"] + counts["pending_suggest"],
        "all": counts["canonical"],
        "review": counts["needs_review"] + counts["borderline"],
    }
    return {
        "run_id": run_id,
        "xlsx": _xlsx_available(),
        "downloads": [
            {"key": key, "title": titles[key], "hint": hints[key], "rows": rows[key]}
            for key in exporter.SHEET_KEYS
        ],
        "files": [
            {"name": item.name, "size": item.stat().st_size}
            for item in output_files(state, run_id)
        ],
    }


def _xlsx_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("openpyxl") is not None


def api_sheets(state: PanelState, query: dict) -> dict:
    """فهرست خروجی‌های ممکن و اینکه کدام‌ها در فاز ۴ ساخته می‌شوند."""
    tabs = state.config.get("output.tabs", {}) or {}
    names = {
        "ready": tabs.get("ready", "آماده تولید محتوا"),
        "archive": tabs.get("archive", "آرشیو آینده"),
        "all": tabs.get("all", "همه محصولات"),
        "review": tabs.get("review", "نیاز به بازبینی دستی"),
    }
    hints = {
        "ready": "محصولاتی که در گوگل ساجست دارند",
        "archive": "محصولاتی که ساجست ندارند",
        "all": "همه با هم، با ستون «ساجست: دارد/ندارد»",
        "review": "مواردی که ابزار مطمئن نبوده",
    }
    return {
        "selected": exporter.selected_sheets(state.config),
        "sheets": [
            {"key": key, "title": names[key], "hint": hints[key]} for key in exporter.SHEET_KEYS
        ],
    }


def api_save_sheets(state: PanelState, body: dict) -> dict:
    """انتخاب شیت‌ها در همین نشست اثر می‌گذارد (روی فایلی که فاز ۴ می‌سازد).

    دانلود جداگانه‌ی هر فهرست مستقل از این انتخاب همیشه در دسترس است.
    """
    chosen = [str(key) for key in (body.get("sheets") or []) if str(key) in exporter.SHEET_KEYS]
    if not chosen:
        raise ApiError("حداقل یک خروجی را انتخاب کنید.")
    state.config.raw.setdefault("output", {})["sheets"] = chosen
    state.runner.config = state.config
    return {"selected": chosen}


GET_ROUTES: dict[str, Callable[[PanelState, dict], Any]] = {
    "/api/state": api_state,
    "/api/config": api_get_config,
    "/api/job": api_job,
    "/api/products": api_products,
    "/api/product": api_product,
    "/api/review": api_review,
    "/api/normalize": api_normalize,
    "/api/output": api_output,
    "/api/presets": api_presets,
    "/api/plan": api_plan,
    "/api/sheets": api_sheets,
}

POST_ROUTES: dict[str, Callable[[PanelState, dict], Any]] = {
    "/api/runs": api_create_run,
    "/api/runs/delete": api_delete_run,
    "/api/config": api_save_config,
    "/api/job": api_start_job,
    "/api/job/cancel": api_cancel_job,
    "/api/products/merge": api_merge,
    "/api/products/detach": api_detach,
    "/api/products/rename": api_rename,
    "/api/products/review": api_review_flag,
    "/api/plan": api_save_plan,
    "/api/sheets": api_save_sheets,
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ContentPipelinePanel"
    panel: PanelState  # از طرف سرور ست می‌شود

    # ------------------------------------------------------------ کمکی‌ها
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass  # لاگ پیش‌فرض روی هر پولینگ ترمینال را پر می‌کند

    def finish(self) -> None:
        super().finish()
        self.panel.close_conn()  # ترد این اتصال TCP تمام شد

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in {"127.0.0.1", "localhost", "::1", ""}

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-Panel-Token") or query.get("t") or ""
        return secrets.compare_digest(given, self.panel.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError("بدنه‌ی درخواست بیش از حد بزرگ است.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("بدنه‌ی JSON نامعتبر است.") from None
        if not isinstance(data, dict):
            raise ApiError("بدنه باید یک شیء JSON باشد.")
        return data

    # -------------------------------------------------------------- مسیرها
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path

        if not self._host_ok():
            self._error("درخواست از میزبان غیرمجاز.", HTTPStatus.FORBIDDEN)
            return

        if path.startswith("/api/") or path == "/download":
            if not self._token_ok(query):
                self._error("توکن نامعتبر است. پنل را از همان لینک ترمینال باز کنید.",
                            HTTPStatus.FORBIDDEN)
                return
        if path == "/download":
            self._download(query)
            return
        handler = GET_ROUTES.get(path)
        if handler is not None:
            self._dispatch(handler, query)
            return
        self._static(path)

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if not self._host_ok():
            self._error("درخواست از میزبان غیرمجاز.", HTTPStatus.FORBIDDEN)
            return
        if not self._token_ok(query):
            self._error("توکن نامعتبر است.", HTTPStatus.FORBIDDEN)
            return
        handler = POST_ROUTES.get(parsed.path)
        if handler is None:
            self._error("مسیر پیدا نشد.", HTTPStatus.NOT_FOUND)
            return
        try:
            payload = handler(self.panel, self._body())
        except ApiError as exc:
            self._error(str(exc), exc.status)
        except Exception as exc:  # noqa: BLE001 — خطا باید در پنل دیده شود
            self._error(f"خطای داخلی: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            self._json(payload)

    def _dispatch(self, handler: Callable[[PanelState, dict], Any], query: dict) -> None:
        try:
            payload = handler(self.panel, query)
        except ApiError as exc:
            self._error(str(exc), exc.status)
        except Exception as exc:  # noqa: BLE001
            self._error(f"خطای داخلی: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            self._json(payload)

    def _download(self, query: dict) -> None:
        """یک فهرست (یا کل فایل) را همین‌جا می‌سازد و می‌فرستد."""
        try:
            run_id = self.panel.resolve_run(query.get("run_id"))
            key = query.get("key") or "workbook"
            fmt = (query.get("format") or "").lower()
            conn = self.panel.conn()
            config = self.panel.config

            if key == "workbook":
                sheets = [
                    exporter.build_one(conn, run_id, config, name)
                    for name in exporter.SHEET_KEYS
                ]
                label = "همه-فهرست‌ها"
            elif key in exporter.SHEET_KEYS:
                sheets = [exporter.build_one(conn, run_id, config, key)]
                label = sheets[0].title.replace(" ", "-")
            else:
                self._error("نام فهرست نامعتبر است.", HTTPStatus.NOT_FOUND)
                return
        except ApiError as exc:
            self._error(str(exc), exc.status)
            return
        except ValueError as exc:
            self._error(str(exc))
            return

        payload = None if fmt == "csv" else exporter.sheets_to_xlsx(sheets)
        if payload is not None:
            name, mime = f"{label}-{run_id}.xlsx", (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        elif len(sheets) == 1:
            payload, name, mime = (
                exporter.sheet_to_csv(sheets[0]),
                f"{label}-{run_id}.csv",
                "text/csv; charset=utf-8",
            )
        else:
            # بدون openpyxl نمی‌شود چند شیت را در یک فایل داد
            self._error(
                "برای فایل چندشیتی به openpyxl نیاز است: pip install openpyxl"
                " — یا هر فهرست را جداگانه دانلود کنید.",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return

        ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "_")
        self._send(
            HTTPStatus.OK,
            payload,
            mime,
            {
                "Content-Disposition": f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(name, safe='')}"
            },
        )

    def _static(self, path: str) -> None:
        name = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._error("پیدا نشد.", HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(
    config: Config,
    config_path: str | Path | None,
    host: str = "127.0.0.1",
    port: int = 8000,
    token: str | None = None,
) -> tuple[PanelServer, PanelState]:
    path = Path(config_path) if config_path else None
    state = PanelState(config, path, token or secrets.token_urlsafe(16))
    handler = type("BoundPanelHandler", (PanelHandler,), {"panel": state})
    server = PanelServer((host, port), handler)
    return server, state


def url_for(server: PanelServer, token: str) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}/?t={token}"
