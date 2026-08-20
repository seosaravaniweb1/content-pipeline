"""پنل مدیریت «تکمیل‌کننده‌ی گوگل‌شیت».

یک سرور کوچک با کتابخانه‌ی استاندارد پایتون — بدون Flask، بدون CDN، بدون
هیچ وابستگی اضافه. فقط روی ``127.0.0.1`` گوش می‌دهد، هدر ``Host`` را چک
می‌کند و هر درخواست API به توکن نشست نیاز دارد؛ توکن در همان لینکی است که
ترمینال چاپ می‌کند.

پنجِ کاری که از پنل انجام می‌شود:

1. اتصال به شیت (شناسه، سرویس‌اکانت، تب) و **بررسی ستون‌ها بدون نوشتن**
2. معرفی سایت‌های منبع و ایمپورت نگاشت «عنوان → آدرس»
3. تنظیم قواعد پر کردن و اجرا، با لاگ زنده و دکمه‌ی لغو
4. دیدن ردیف‌ها و وضعیتشان
5. ویرایش ``config.yaml``
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

from ..core import db, filler, gsheet, normalizer, sources, taxonomy
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

    def conn(self) -> sqlite3.Connection:
        """هر ترد اتصال sqlite خودش را دارد (اتصال بین تردها قابل اشتراک نیست)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self.config.db_path)
            self._local.conn = conn
        return conn

    def close_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def reload_config(self) -> None:
        with self._lock:
            self.config = load_config(self.config_path) if self.config_path else self.config
            self.runner.config = self.config
        self.close_conn()

    # -- تنظیمات ذخیره‌شده‌ی پنل -------------------------------------------
    def settings(self) -> dict:
        stored = db.get_setting(self.conn(), "settings", {}) or {}
        return stored if isinstance(stored, dict) else {}

    def options(self, extra: dict | None = None) -> filler.FillOptions:
        merged = {**self.settings(), **{k: v for k, v in (extra or {}).items() if v is not None}}
        return filler.options_from_config(self.config, merged)


def _guard_idle(state: PanelState) -> None:
    if state.runner.running:
        raise ApiError(
            "در حین اجرا نمی‌شود تنظیمات را عوض کرد؛ صبر کنید یا لغو کنید.",
            HTTPStatus.CONFLICT,
        )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

#: کلیدهای متنی که از فرم پنل پذیرفته می‌شوند
TEXT_KEYS = (
    "sheet_id",
    "service_account_json",
    "tab",
    "lists_tab",
    "report_tab",
    "file",
    "title_column",
    "sources_column",
    "overwrite",
    "multi_select_separator",
)
INT_KEYS = (
    "header_row",
    "limit",
    "max_titles_per_session",
    "max_pages_per_session",
    "max_sources_per_title",
    "batch_rows",
    "max_categories",
    "max_tags",
)
BOOL_KEYS = ("retry_partial",)


def api_state(state: PanelState, query: dict) -> dict:
    conn = state.conn()
    options = state.options()
    key = options.key
    sheets = [
        {
            "key": row["sheet_key"],
            "title": row["title"] or row["sheet_key"],
            "updated_at": row["updated_at"],
            "current": row["sheet_key"] == key,
        }
        for row in db.list_sheets(conn)
    ]
    return {
        "settings": _settings_dict(options),
        "sites": [site.base_url for site in options.sites],
        "ready": bool(options.sheet.sheet_id or options.sheet.file),
        "gspread": _gspread_available(),
        "counts": db.counts_of(conn, key),
        "links": db.link_count(conn),
        "plan": db.sheet_plan(conn, key),
        "kinds": filler.KIND_LABELS,
        "sheets": sheets,
        "config_path": str(state.config_path) if state.config_path else "",
        "db_path": state.config.db_path,
        "job": state.runner.snapshot(int(query.get("since") or 0)),
    }


def _settings_dict(options: filler.FillOptions) -> dict:
    return {
        "sheet_id": options.sheet.sheet_id,
        "service_account_json": options.sheet.service_account_json,
        "tab": options.sheet.tab,
        "lists_tab": options.sheet.lists_tab,
        "report_tab": options.sheet.report_tab,
        "header_row": options.sheet.header_row,
        "file": options.sheet.file,
        "title_column": options.sheet.title_column,
        "sources_column": options.sheet.sources_column,
        "overwrite": options.overwrite,
        "retry_partial": options.retry_partial,
        "limit": options.limit,
        "max_titles_per_session": options.max_titles_per_session,
        "max_pages_per_session": options.max_pages_per_session,
        "max_sources_per_title": options.max_sources_per_title,
        "batch_rows": options.batch_rows,
        "max_categories": options.max_categories,
        "max_tags": options.max_tags,
        "multi_select_separator": options.separator,
        "search_enabled": options.search_enabled,
        "image_enabled": options.image.enabled,
        "image_min_side": options.image.min_side,
    }


def _gspread_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("gspread") is not None


def api_save_settings(state: PanelState, body: dict) -> dict:
    """ذخیره‌ی تنظیمات پنل در دیتابیس.

    عمداً در ``config.yaml`` نوشته نمی‌شود: بازنویسی YAML توضیحات و ترتیب
    فایل شما را از بین می‌برد. این مقدارها روی config سوار می‌شوند و CLI هم
    از همان‌ها استفاده می‌کند.
    """
    _guard_idle(state)
    settings = state.settings()
    for key in TEXT_KEYS:
        if key in body:
            settings[key] = str(body.get(key) or "").strip()
    for key in INT_KEYS:
        if key in body:
            try:
                settings[key] = max(0, int(body.get(key) or 0))
            except (TypeError, ValueError):
                raise ApiError(f"مقدار «{key}» باید عدد باشد.") from None
    for key in BOOL_KEYS:
        if key in body:
            settings[key] = bool(body.get(key))
    if "search_enabled" in body:
        settings["search"] = {**(settings.get("search") or {}), "enabled": bool(body["search_enabled"])}
    if "image_enabled" in body or "image_min_side" in body:
        image = dict(settings.get("image") or {})
        if "image_enabled" in body:
            image["enabled"] = bool(body.get("image_enabled"))
        if "image_min_side" in body:
            try:
                image["min_side"] = max(0, int(body.get("image_min_side") or 0))
            except (TypeError, ValueError):
                raise ApiError("حداقل اندازه‌ی تصویر باید عدد باشد.") from None
        settings["image"] = image
    if "sites" in body:
        settings["sites"] = clean_sites(body.get("sites"))

    if settings.get("overwrite") not in ("empty", "always"):
        settings["overwrite"] = "empty"
    if settings.get("sheet_id") and not settings.get("service_account_json"):
        raise ApiError(
            "برای گوگل‌شیت، مسیر فایل json سرویس‌اکانت هم لازم است."
            " (یا به‌جای شیت، یک فایل csv/xlsx بدهید.)"
        )
    db.set_setting(state.conn(), "settings", settings)
    state.runner.config = state.config
    return api_state(state, {})


MAX_SITES = 100


def clean_sites(raw: object) -> list[str]:
    """متن چسبانده‌شده‌ی کاربر → فهرست آدرس یکتا."""
    if isinstance(raw, str):
        lines = raw.splitlines()
    elif isinstance(raw, list):
        lines = [str(item) for item in raw]
    else:
        return []
    out: list[str] = []
    for line in lines:
        url = line.strip().strip(",،")
        if not url or url.startswith("#"):
            continue
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if "." not in url.split("//", 1)[-1]:
            continue
        if url not in out:
            out.append(url)
        if len(out) >= MAX_SITES:
            break
    return out


def api_inspect(state: PanelState, body: dict) -> dict:
    """شیت را می‌خواند و ستون‌ها را گزارش می‌دهد — بدون نوشتن هیچ چیزی."""
    _guard_idle(state)
    options = state.options({k: v for k, v in body.items() if k in TEXT_KEYS})
    if not (options.sheet.sheet_id or options.sheet.file):
        raise ApiError("اول شناسه‌ی شیت یا مسیر فایل را بدهید.")
    try:
        plan, rows, lists = filler.inspect(options)
    except gsheet.SheetError as exc:
        raise ApiError(str(exc)) from None
    writable = {spec.key for spec in options.writable(plan)}
    return {
        "rows": len(rows),
        "lists": {name: len(values) for name, values in lists.items()},
        "sample": [row.title for row in rows[:5]],
        "plan": [
            {
                "column": spec.column,
                "key": spec.key,
                "kind": spec.kind,
                "kind_label": filler.KIND_LABELS.get(spec.kind, spec.kind),
                "writable": spec.key in writable,
                "options": len(spec.options),
                "labels": list(spec.search_labels()),
            }
            for spec in plan.specs
        ],
    }


def api_rows(state: PanelState, query: dict) -> dict:
    conn = state.conn()
    options = state.options()
    key = options.key
    status = query.get("status") or None
    if status not in (None, db.PENDING, db.PARTIAL, db.DONE):
        raise ApiError("وضعیت نامعتبر است.")
    plan = [item for item in db.sheet_plan(conn, key) if item.get("kind") not in ("skip", "title")]
    rows = db.rows_of(conn, key, status, limit=int(query.get("limit") or 60))
    return {
        "plan": plan,
        "counts": db.counts_of(conn, key),
        "rows": [
            {
                "row_number": row["row_number"],
                "title": row["title"],
                "status": row["status"] or db.PENDING,
                "pushed": bool(row["pushed"]),
                "note": row["note"] or "",
                "sources": json.loads(row["sources"] or "[]"),
                "values": db.row_values(row),
            }
            for row in rows
        ],
    }


def api_start(state: PanelState, body: dict) -> dict:
    options = state.options({k: v for k, v in body.items() if k in TEXT_KEYS})
    if not (options.sheet.sheet_id or options.sheet.file):
        raise ApiError("اول شیت را تنظیم کنید.")
    try:
        state.runner.start(options, state.config)
    except jobs.JobBusy as exc:
        raise ApiError(str(exc), HTTPStatus.CONFLICT) from None
    return state.runner.snapshot()


def api_cancel(state: PanelState, body: dict) -> dict:
    return {"cancelled": state.runner.cancel()}


def api_job(state: PanelState, query: dict) -> dict:
    return state.runner.snapshot(int(query.get("since") or 0))


def api_reset(state: PanelState, body: dict) -> dict:
    """همه‌ی ردیف‌های این شیت دوباره در صف."""
    _guard_idle(state)
    return {"reset": db.reset_rows(state.conn(), state.options().key)}


def api_forget_sheet(state: PanelState, body: dict) -> dict:
    """حذف کامل سابقه‌ی یک شیت (خودِ گوگل‌شیت دست نمی‌خورد)."""
    _guard_idle(state)
    key = str(body.get("key") or "")
    if not key:
        raise ApiError("کلید شیت لازم است.")
    db.forget_sheet(state.conn(), key)
    return {"forgotten": key}


def api_clear_cache(state: PanelState, body: dict) -> dict:
    """پاک کردن کش صفحه‌ها — وقتی منابع محتوایشان را عوض کرده‌اند."""
    _guard_idle(state)
    return {"cleared": db.clear_pages(state.conn())}


def api_import_links(state: PanelState, body: dict) -> dict:
    """ایمپورت نگاشت «عنوان → آدرس منبع» از یک فایل csv/xlsx."""
    _guard_idle(state)
    path = str(body.get("file") or "").strip()
    if not path:
        raise ApiError("مسیر فایل را بدهید.")
    norm_config = normalizer.config_from_mapping(state.config.get("normalizer", {}))
    try:
        titles, links = sources.import_links(state.conn(), path, norm_config)
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    return {"titles": titles, "links": links, "total": db.link_count(state.conn())}


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
    text = (body.get("text") or "").lstrip("﻿")
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


def api_keyword(state: PanelState, query: dict) -> dict:
    """ابزار کمکی: کلمه‌ی کلیدی یک عنوان چه می‌شود."""
    text = query.get("text") or ""
    norm_config = normalizer.config_from_mapping(state.config.get("normalizer", {}))
    return {
        "title": text,
        "keyword": normalizer.main_keyword(text, norm_config),
        "match_key": normalizer.normalize(text, norm_config),
    }


GET_ROUTES: dict[str, Callable[[PanelState, dict], Any]] = {
    "/api/state": api_state,
    "/api/job": api_job,
    "/api/rows": api_rows,
    "/api/config": api_get_config,
    "/api/keyword": api_keyword,
}

POST_ROUTES: dict[str, Callable[[PanelState, dict], Any]] = {
    "/api/settings": api_save_settings,
    "/api/inspect": api_inspect,
    "/api/start": api_start,
    "/api/cancel": api_cancel,
    "/api/reset": api_reset,
    "/api/sheets/forget": api_forget_sheet,
    "/api/cache/clear": api_clear_cache,
    "/api/links": api_import_links,
    "/api/config": api_save_config,
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SheetFillerPanel"
    panel: PanelState  # از طرف سرور ست می‌شود

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass  # لاگ پیش‌فرض روی هر پولینگ ترمینال را پر می‌کند

    def finish(self) -> None:
        super().finish()
        self.panel.close_conn()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

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
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("بدنه‌ی JSON نامعتبر است.") from None
        if not isinstance(data, dict):
            raise ApiError("بدنه باید یک شیء JSON باشد.")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if not self._host_ok():
            self._error("درخواست از میزبان غیرمجاز.", HTTPStatus.FORBIDDEN)
            return
        if parsed.path.startswith("/api/") or parsed.path == "/download":
            if not self._token_ok(query):
                self._error(
                    "توکن نامعتبر است. پنل را از همان لینک ترمینال باز کنید.",
                    HTTPStatus.FORBIDDEN,
                )
                return
        if parsed.path == "/download":
            self._download(query)
            return
        handler = GET_ROUTES.get(parsed.path)
        if handler is not None:
            self._dispatch(handler, query)
            return
        self._static(parsed.path)

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
            body = self._body()
        except ApiError as exc:
            self._error(str(exc), exc.status)
            return
        self._dispatch(handler, body)

    def _dispatch(self, handler: Callable[[PanelState, dict], Any], payload: dict) -> None:
        try:
            self._json(handler(self.panel, payload))
        except ApiError as exc:
            self._error(str(exc), exc.status)
        except Exception as exc:  # noqa: BLE001 — پیام خطا باید به پنل برسد
            self._error(f"خطای داخلی: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _download(self, query: dict) -> None:
        """خروجی محلی از ردیف‌های همین شیت (csv یا xlsx)."""
        state = self.panel
        conn = state.conn()
        key = state.options().key
        plan = db.sheet_plan(conn, key)
        if not plan:
            self._error("هنوز چیزی برای دانلود نیست؛ اول یک بار اجرا کنید.")
            return
        header = [str(item.get("column")) for item in plan]
        rows = []
        for record in db.rows_of(conn, key):
            values = db.row_values(record)
            values.setdefault("title", record["title"] or "")
            rows.append([values.get(str(item.get("key")), "") for item in plan])

        fmt = (query.get("format") or "csv").lower()
        name = f"filled-{key.replace(':', '-')}"
        if fmt == "xlsx":
            payload = _xlsx_bytes(header, rows)
            if payload is None:
                self._error("openpyxl نصب نیست: pip install openpyxl")
                return
            body, mime, suffix = payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
        else:
            import csv
            import io

            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(header)
            writer.writerows(rows)
            body, mime, suffix = buffer.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "csv"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}.{suffix}"
        )
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        target = (STATIC_DIR / (path.lstrip("/") or "index.html")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._error("صفحه پیدا نشد.", HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)


def _xlsx_bytes(header: list[str], rows: list[list]) -> bytes | None:
    try:
        from openpyxl import Workbook  # type: ignore
    except ImportError:
        return None
    import io

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "محصولات"
    worksheet.sheet_view.rightToLeft = True
    worksheet.append(header)
    worksheet.freeze_panes = "A2"
    for row in rows:
        worksheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(
    config: Config,
    config_path: str | Path | None,
    host: str = "127.0.0.1",
    port: int = 8050,
    token: str | None = None,
) -> tuple[PanelServer, PanelState]:
    state = PanelState(
        config,
        Path(config_path) if config_path else None,
        token or secrets.token_urlsafe(16),
    )
    handler = type("BoundHandler", (PanelHandler,), {"panel": state})
    return PanelServer((host, port), handler), state


def url_for(server: PanelServer, token: str) -> str:
    host, port = server.server_address[0], server.server_address[1]
    if host in ("0.0.0.0", "::"):  # noqa: S104 — فقط برای نمایش
        host = "127.0.0.1"
    return f"http://{host}:{port}/?t={token}"
