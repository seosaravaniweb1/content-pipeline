"""لایه‌ی state این برنامه: SQLite.

اصل کار: هیچ چیزی فقط در RAM نمی‌ماند. هر ردیفی که تکمیل می‌شود بلافاصله
نوشته می‌شود، پس اجرای نیمه‌تمام روی ۱۵ هزار عنوان از همان‌جا ادامه پیدا
می‌کند و صفحه‌ای که یک‌بار دانلود شده دوباره دانلود نمی‌شود.

اینجا خبری از «اجرا» (run) و فازهای کراول نیست؛ واحد کار، **یک شیت** است:

    sheet_key = شناسه‌ی شیت + نام تب  (یا مسیر فایل محلی)

پس یک نصب می‌تواند هم‌زمان شیت رمان، شیت نمونه‌سوال و شیت جزوه را — هر کدام
با ستون‌ها و پیشرفت خودش — نگه دارد.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1

PENDING = "pending"
PARTIAL = "partial"
DONE = "done"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets (
    sheet_key   TEXT PRIMARY KEY,
    title       TEXT,      -- نام خوانا برای پنل
    plan        TEXT,      -- JSON: ستون‌های همین شیت و نوعشان
    created_at  TIMESTAMP,
    updated_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rows (
    id           INTEGER PRIMARY KEY,
    sheet_key    TEXT NOT NULL,
    row_number   INTEGER,   -- شماره‌ی ردیف در شیت (۰ = ورودی فایلی)
    title        TEXT,
    title_key    TEXT,      -- عنوان نرمال‌شده؛ کلید یکتای همان شیت
    field_values TEXT,      -- JSON: مقدار همه‌ی ستون‌ها
    sources      TEXT,      -- JSON: آدرس صفحه‌هایی که این ردیف از آن‌ها ساخته شد
    evidence     TEXT,      -- JSON: برای هر ستون، رأی و دلیل
    status       TEXT,      -- pending | partial | done
    note         TEXT,
    pushed       BOOLEAN DEFAULT 0,
    updated_at   TIMESTAMP,
    UNIQUE(sheet_key, title_key)
);

-- کش استخراج صفحه‌ها: یک صفحه‌ی منبع دو بار دانلود نمی‌شود
CREATE TABLE IF NOT EXISTS pages (
    url        TEXT PRIMARY KEY,
    domain     TEXT,
    payload    TEXT,      -- JSON خروجی core.details
    fetched_at TIMESTAMP,
    version    INTEGER DEFAULT 0   -- نسخه‌ی قواعد استخراج (core.details)
);

-- نگاشت «عنوان → آدرس منبع». اگر از جای دیگری (مثلاً خروجی ابزار استخراج)
-- آدرس صفحه‌ها را دارید، اینجا ایمپورت می‌شود و دیگر جستجویی لازم نیست.
CREATE TABLE IF NOT EXISTS source_links (
    title_key  TEXT NOT NULL,
    url        TEXT NOT NULL,
    domain     TEXT,
    added_at   TIMESTAMP,
    PRIMARY KEY (title_key, url)
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMP
);

-- دامنه‌هایی که تا حالا جواب داده‌اند: دفعه‌ی بعد اول سراغ همان‌ها می‌رویم
CREATE TABLE IF NOT EXISTS domains (
    domain   TEXT PRIMARY KEY,
    hits     INTEGER DEFAULT 0,
    last_at  TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rows_sheet  ON rows(sheet_key, status);
CREATE INDEX IF NOT EXISTS idx_links_title ON source_links(title_key);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn


#: ستون‌هایی که بعداً اضافه شده‌اند. ``CREATE TABLE IF NOT EXISTS`` روی
#: دیتابیسِ موجود کاری نمی‌کند، پس ستون تازه باید صریح اضافه شود — وگرنه
#: نصبِ به‌روزشده روی دیتابیسِ قدیمیِ کاربر می‌ترکد.
_LATE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("pages", "version", "INTEGER DEFAULT 0"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, definition in _LATE_COLUMNS:
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


# ---------------------------------------------------------------------------
# شیت‌ها
# ---------------------------------------------------------------------------


def sheet_key(sheet_id: str = "", tab: str = "", file: str = "") -> str:
    """کلید یکتای یک شیت — واحد کار این برنامه."""
    if sheet_id:
        return f"gs:{sheet_id}:{tab}" if tab else f"gs:{sheet_id}"
    if file:
        return f"file:{Path(file).name}"
    return "unknown"


def remember_sheet(
    conn: sqlite3.Connection, key: str, title: str, plan: Sequence[dict] | None = None
) -> None:
    with transaction(conn):
        conn.execute(
            """INSERT INTO sheets (sheet_key, title, plan, created_at, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(sheet_key) DO UPDATE SET
                   title=excluded.title,
                   plan=COALESCE(excluded.plan, sheets.plan),
                   updated_at=excluded.updated_at""",
            (
                key,
                title,
                json.dumps(list(plan), ensure_ascii=False) if plan is not None else None,
                utcnow(),
                utcnow(),
            ),
        )


def sheet_plan(conn: sqlite3.Connection, key: str) -> list[dict]:
    row = conn.execute("SELECT plan FROM sheets WHERE sheet_key=?", (key,)).fetchone()
    if row is None or not row["plan"]:
        return []
    try:
        data = json.loads(row["plan"])
    except (TypeError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def list_sheets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sheets ORDER BY updated_at DESC").fetchall()


def forget_sheet(conn: sqlite3.Connection, key: str) -> None:
    """حذف کامل یک شیت با همه‌ی ردیف‌هایش (کش صفحه‌ها می‌ماند)."""
    with transaction(conn):
        conn.execute("DELETE FROM rows WHERE sheet_key=?", (key,))
        conn.execute("DELETE FROM sheets WHERE sheet_key=?", (key,))


# ---------------------------------------------------------------------------
# ردیف‌ها
# ---------------------------------------------------------------------------


def upsert_row(
    conn: sqlite3.Connection, key: str, title: str, title_key: str, row_number: int = 0
) -> int:
    """ثبت یک عنوان در صف (idempotent)."""
    conn.execute(
        """INSERT INTO rows (sheet_key, row_number, title, title_key, status, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(sheet_key, title_key) DO UPDATE SET
               row_number=excluded.row_number,
               title=excluded.title""",
        (key, row_number, title, title_key, PENDING, utcnow()),
    )
    found = conn.execute(
        "SELECT id FROM rows WHERE sheet_key=? AND title_key=?", (key, title_key)
    ).fetchone()
    return int(found["id"])


def save_values(
    conn: sqlite3.Connection,
    row_id: int,
    values: dict[str, Any],
    sources: Sequence[str] = (),
    evidence: dict[str, Any] | None = None,
    status: str = DONE,
    note: str = "",
) -> None:
    """نوشتن مقدار ستون‌های یک ردیف.

    مقدارهای تازه روی مقدارهای قبلی سوار می‌شوند و ستون خالی چیزی را پاک
    نمی‌کند: اجرای دوباره نباید کارِ اجرای قبلی را خراب کند.
    """
    stored = dict(row_values(get_row(conn, row_id)))
    for name, value in (values or {}).items():
        if value not in (None, ""):
            stored[str(name)] = str(value)
    conn.execute(
        """UPDATE rows SET field_values=?, sources=?, evidence=?, status=?, note=?,
                           pushed=0, updated_at=? WHERE id=?""",
        (
            json.dumps(stored, ensure_ascii=False),
            json.dumps(list(sources), ensure_ascii=False),
            json.dumps(evidence or {}, ensure_ascii=False),
            status,
            note,
            utcnow(),
            row_id,
        ),
    )


def get_row(conn: sqlite3.Connection, row_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM rows WHERE id=?", (row_id,)).fetchone()


def row_by_key(conn: sqlite3.Connection, key: str, title_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM rows WHERE sheet_key=? AND title_key=?", (key, title_key)
    ).fetchone()


def row_values(row: sqlite3.Row | None) -> dict[str, str]:
    if row is None or not row["field_values"]:
        return {}
    try:
        data = json.loads(row["field_values"])
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v not in (None, "")}


def rows_of(
    conn: sqlite3.Connection, key: str, status: str | None = None, limit: int = 0
) -> list[sqlite3.Row]:
    query = "SELECT * FROM rows WHERE sheet_key=?"
    params: list[Any] = [key]
    if status:
        query += " AND status=?"
        params.append(status)
    query += " ORDER BY row_number, id"
    if limit:
        query += f" LIMIT {int(limit)}"
    return conn.execute(query, params).fetchall()


def unpushed_rows(conn: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    """ردیف‌های تکمیل‌شده‌ای که هنوز در شیت نوشته نشده‌اند."""
    return conn.execute(
        """SELECT * FROM rows WHERE sheet_key=? AND pushed=0 AND status IN (?,?)
           ORDER BY row_number, id""",
        (key, DONE, PARTIAL),
    ).fetchall()


def mark_pushed(conn: sqlite3.Connection, row_ids: Sequence[int]) -> None:
    if not row_ids:
        return
    with transaction(conn):
        conn.executemany("UPDATE rows SET pushed=1 WHERE id=?", [(int(i),) for i in row_ids])


def reset_rows(conn: sqlite3.Connection, key: str) -> int:
    """همه‌ی ردیف‌ها دوباره به صف — برای اجرای کامل از نو."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE rows SET status=?, pushed=0 WHERE sheet_key=?", (PENDING, key)
        )
    return int(cursor.rowcount or 0)


def counts_of(conn: sqlite3.Connection, key: str) -> dict[str, int]:
    queries = {
        "total": "SELECT COUNT(*) c FROM rows WHERE sheet_key=?",
        "done": "SELECT COUNT(*) c FROM rows WHERE sheet_key=? AND status='done'",
        "partial": "SELECT COUNT(*) c FROM rows WHERE sheet_key=? AND status='partial'",
        "pending": (
            "SELECT COUNT(*) c FROM rows WHERE sheet_key=?"
            " AND (status IS NULL OR status='pending')"
        ),
        "pushed": "SELECT COUNT(*) c FROM rows WHERE sheet_key=? AND pushed=1",
    }
    return {
        name: int(conn.execute(query, (key,)).fetchone()["c"]) for name, query in queries.items()
    }


# ---------------------------------------------------------------------------
# کش صفحه‌ها
# ---------------------------------------------------------------------------


def get_page(
    conn: sqlite3.Connection, url: str, ttl_days: int | None = None, version: int = 0
) -> dict | None:
    row = conn.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()
    if row is None:
        return None
    if version and int(row["version"] or 0) != version:
        # قواعد استخراج عوض شده‌اند؛ نتیجه‌ی قدیمی دیگر معتبر نیست
        return None
    if ttl_days:
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
        except (TypeError, ValueError):
            fetched = None
        if fetched is not None:
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fetched > timedelta(days=ttl_days):
                return None
    try:
        data = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def put_page(
    conn: sqlite3.Connection, url: str, domain: str, payload: dict, version: int = 0
) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO pages (url, domain, payload, fetched_at, version) "
            "VALUES (?,?,?,?,?)",
            (url, domain, json.dumps(payload, ensure_ascii=False), utcnow(), int(version)),
        )


def clear_pages(conn: sqlite3.Connection) -> int:
    with transaction(conn):
        cursor = conn.execute("DELETE FROM pages")
    return int(cursor.rowcount or 0)


# ---------------------------------------------------------------------------
# نگاشت عنوان → منبع
# ---------------------------------------------------------------------------


def add_source_link(conn: sqlite3.Connection, title_key: str, url: str, domain: str = "") -> None:
    """ثبت «این عنوان، این آدرس».

    دامنه اگر داده نشود از خود آدرس درمی‌آید؛ صداکننده نباید یادش برود، چون
    همین دامنه‌ها بعداً به‌عنوان سایت منبع استفاده می‌شوند.
    """
    if not domain:
        from urllib.parse import urlsplit

        domain = urlsplit(url).netloc.lower()
    conn.execute(
        """INSERT OR IGNORE INTO source_links (title_key, url, domain, added_at)
           VALUES (?,?,?,?)""",
        (title_key, url, domain, utcnow()),
    )


def source_links(conn: sqlite3.Connection, title_key: str, limit: int = 20) -> list[str]:
    rows = conn.execute(
        "SELECT url FROM source_links WHERE title_key=? ORDER BY rowid LIMIT ?",
        (title_key, int(limit)),
    ).fetchall()
    return [row["url"] for row in rows]


def link_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) c FROM source_links").fetchone()["c"])


# ---------------------------------------------------------------------------
# دامنه‌های موفق (خودکار یاد گرفته می‌شوند)
# ---------------------------------------------------------------------------


def note_domain(conn: sqlite3.Connection, domain: str) -> None:
    """این دامنه یک بار دیگر جواب داد."""
    if not domain:
        return
    conn.execute(
        """INSERT INTO domains (domain, hits, last_at) VALUES (?,1,?)
           ON CONFLICT(domain) DO UPDATE SET hits=hits+1, last_at=excluded.last_at""",
        (domain, utcnow()),
    )


def domain_scores(conn: sqlite3.Connection) -> dict[str, int]:
    """``دامنه → تعداد دفعاتی که برای یک عنوان به درد خورده``."""
    return {
        row["domain"]: int(row["hits"] or 0)
        for row in conn.execute("SELECT domain, hits FROM domains").fetchall()
    }


def known_domains(conn: sqlite3.Connection, limit: int = 20) -> list[str]:
    """دامنه‌هایی که از لینک‌های ذخیره‌شده می‌شناسیم.

    وقتی کاربر سایتی تنظیم نکرده ولی نگاشت «عنوان → آدرس» ایمپورت کرده،
    همین‌ها سایت‌های منبع‌اند و لازم نیست دوباره بپرسیم.
    """
    rows = conn.execute(
        """SELECT domain, COUNT(*) c FROM source_links
           WHERE domain IS NOT NULL AND domain <> '' GROUP BY domain
           ORDER BY c DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [row["domain"] for row in rows]


def older_than(timestamp: str | None, days: int) -> bool:
    """آیا این زمان از ``days`` روز پیش قدیمی‌تر است؟"""
    if not timestamp or days <= 0:
        return False
    try:
        moment = datetime.fromisoformat(str(timestamp))
    except (TypeError, ValueError):
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - moment > timedelta(days=days)


# ---------------------------------------------------------------------------
# تنظیمات پنل
# ---------------------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
            (key, json.dumps(value, ensure_ascii=False), utcnow()),
        )
