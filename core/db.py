"""لایه‌ی state پروژه: SQLite.

اصل معماری: هیچ داده‌ای فقط در RAM نمی‌ماند. هر فاز نتیجه‌اش را بلافاصله
می‌نویسد و با ``UNIQUE`` + ``INSERT OR IGNORE`` اجرای دوباره رکورد تکراری
نمی‌سازد.

اسکیما دقیقاً مطابق بخش ۵ داکیومنت است.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 5

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    target_topic  TEXT NOT NULL,
    created_at    TIMESTAMP,
    status        TEXT,
    current_phase INTEGER,
    categories    TEXT,   -- JSON: دسته‌های انتخاب‌شده‌ی این اجرا
    sites         TEXT,   -- JSON: سایت‌های همین اجرا (خالی = از config)
    options       TEXT    -- JSON: تنظیمات همین اجرا (سقف محصول، رندر جاوااسکریپت)
);

CREATE TABLE IF NOT EXISTS raw_products (
    id               INTEGER PRIMARY KEY,
    run_id           TEXT NOT NULL,
    source_domain    TEXT,
    source_url       TEXT,
    raw_title        TEXT,
    normalized_title TEXT,
    topic_score      REAL,
    is_relevant      BOOLEAN,
    category         TEXT,   -- دسته‌ای که این عنوان به آن نزدیک‌تر بوده
    UNIQUE(run_id, source_url)
);

CREATE TABLE IF NOT EXISTS canonical_products (
    id               INTEGER PRIMARY KEY,
    run_id           TEXT NOT NULL,
    canonical_title  TEXT,
    entity_tokens    TEXT,
    merge_confidence REAL,
    needs_review     BOOLEAN DEFAULT 0,
    suggest_status   TEXT,
    UNIQUE(run_id, canonical_title)
);

CREATE TABLE IF NOT EXISTS product_mapping (
    raw_id       INTEGER,
    canonical_id INTEGER,
    PRIMARY KEY (raw_id, canonical_id)
);

CREATE TABLE IF NOT EXISTS lsi_keywords (
    id           INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL,
    keyword      TEXT,
    position     INTEGER,
    source_query TEXT,   -- با کدام شکل از عنوان پیدا شد
    UNIQUE(canonical_id, keyword)
);

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key  TEXT PRIMARY KEY,
    response   TEXT,
    created_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_raw_run       ON raw_products(run_id, is_relevant);
CREATE INDEX IF NOT EXISTS idx_canon_run     ON canonical_products(run_id);
CREATE INDEX IF NOT EXISTS idx_mapping_canon ON product_mapping(canonical_id);
CREATE INDEX IF NOT EXISTS idx_lsi_canon     ON lsi_keywords(canonical_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """اتصال به دیتابیس و اطمینان از وجود اسکیما."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    init_schema(conn)
    return conn


NEW_COLUMNS: dict[str, dict[str, str]] = {
    "runs": {"categories": "TEXT", "sites": "TEXT", "options": "TEXT"},
    "raw_products": {"category": "TEXT"},
    "lsi_keywords": {"source_query": "TEXT"},
}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """ستون‌های تازه را به دیتابیس‌های ساخته‌شده با نسخه‌های قبلی اضافه می‌کند.

    ``CREATE TABLE IF NOT EXISTS`` جدول موجود را دست نمی‌زند، پس بدون این،
    اجرای قدیمی کاربر بعد از به‌روزرسانی می‌شکست.
    """
    for table, columns in NEW_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, kind in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


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
# runs
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def create_run(
    conn: sqlite3.Connection,
    topic: str,
    run_id: str | None = None,
    categories: Sequence[str] | None = None,
    sites: Sequence[str] | None = None,
    options: dict | None = None,
) -> str:
    run_id = run_id or new_run_id()
    with transaction(conn):
        conn.execute(
            """INSERT OR IGNORE INTO runs
               (run_id, target_topic, created_at, status, current_phase,
                categories, sites, options)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                run_id,
                topic,
                utcnow(),
                RUNNING,
                0,
                json.dumps(list(categories or []), ensure_ascii=False),
                json.dumps(list(sites or []), ensure_ascii=False),
                json.dumps(options or {}, ensure_ascii=False),
            ),
        )
    return run_id


def run_categories(conn: sqlite3.Connection, run_id: str) -> list[str]:
    row = get_run(conn, run_id)
    if row is None:
        return []
    return _json_list(row["categories"])


def run_options(conn: sqlite3.Connection, run_id: str) -> dict:
    """تنظیمات همین اجرا: سقف محصول در هر سایت، رندر جاوااسکریپت و ...."""
    row = get_run(conn, run_id)
    if row is None or not row["options"]:
        return {}
    try:
        data = json.loads(row["options"])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run_sites(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """سایت‌های همین اجرا. فهرست خالی یعنی «از config بخوان»."""
    row = get_run(conn, run_id)
    if row is None:
        return []
    return _json_list(row["sites"])


def set_run_plan(
    conn: sqlite3.Connection,
    run_id: str,
    categories: Sequence[str] | None = None,
    sites: Sequence[str] | None = None,
    topic: str | None = None,
    options: dict | None = None,
) -> None:
    """ویرایش نقشه‌ی یک اجرا: دسته‌ها، سایت‌ها، موضوع و تنظیمات."""
    sets, values = [], []
    if options is not None:
        sets.append("options=?")
        values.append(json.dumps(options, ensure_ascii=False))
    if categories is not None:
        sets.append("categories=?")
        values.append(json.dumps(list(categories), ensure_ascii=False))
    if sites is not None:
        sets.append("sites=?")
        values.append(json.dumps(list(sites), ensure_ascii=False))
    if topic is not None:
        sets.append("target_topic=?")
        values.append(topic)
    if not sets:
        return
    values.append(run_id)
    with transaction(conn):
        conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", values)


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()


def latest_run(conn: sqlite3.Connection, topic: str | None = None) -> sqlite3.Row | None:
    if topic:
        return conn.execute(
            "SELECT * FROM runs WHERE target_topic=? ORDER BY created_at DESC LIMIT 1", (topic,)
        ).fetchone()
    return conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()


def list_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


def set_run_status(
    conn: sqlite3.Connection, run_id: str, status: str, phase: int | None = None
) -> None:
    with transaction(conn):
        if phase is None:
            conn.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))
        else:
            conn.execute(
                "UPDATE runs SET status=?, current_phase=? WHERE run_id=?",
                (status, phase, run_id),
            )


# ---------------------------------------------------------------------------
# raw_products
# ---------------------------------------------------------------------------


def insert_raw_product(
    conn: sqlite3.Connection,
    run_id: str,
    source_domain: str,
    source_url: str,
    raw_title: str,
    normalized_title: str,
    topic_score: float | None = None,
    is_relevant: bool | None = None,
) -> None:
    """Idempotent — کلید یکتا ``(run_id, source_url)``."""
    conn.execute(
        """INSERT OR IGNORE INTO raw_products
           (run_id, source_domain, source_url, raw_title, normalized_title,
            topic_score, is_relevant)
           VALUES (?,?,?,?,?,?,?)""",
        (
            run_id,
            source_domain,
            source_url,
            raw_title,
            normalized_title,
            topic_score,
            None if is_relevant is None else int(is_relevant),
        ),
    )


def update_raw_relevance(
    conn: sqlite3.Connection,
    raw_id: int,
    topic_score: float,
    is_relevant: bool | None,
    category: str | None = None,
) -> None:
    conn.execute(
        "UPDATE raw_products SET topic_score=?, is_relevant=?, category=? WHERE id=?",
        (topic_score, None if is_relevant is None else int(is_relevant), category, raw_id),
    )


def category_of(conn: sqlite3.Connection, canonical_id: int) -> str:
    """دسته‌ی یک محصول یکپارچه = پرتکرارترین دسته‌ی عنوان‌های خامش."""
    row = conn.execute(
        """SELECT r.category AS category, COUNT(*) AS c
           FROM product_mapping m JOIN raw_products r ON r.id = m.raw_id
           WHERE m.canonical_id=? AND r.category IS NOT NULL AND r.category <> ''
           GROUP BY r.category ORDER BY c DESC, r.category LIMIT 1""",
        (canonical_id,),
    ).fetchone()
    return row["category"] if row else ""


def relevant_raw_products(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM raw_products WHERE run_id=? AND is_relevant=1 ORDER BY id", (run_id,)
    ).fetchall()


def borderline_raw_products(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """عنوان‌های مرزی (``is_relevant IS NULL``) — مقصدشان شیت C است."""
    return conn.execute(
        "SELECT * FROM raw_products WHERE run_id=? AND is_relevant IS NULL"
        " ORDER BY topic_score DESC",
        (run_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# canonical_products
# ---------------------------------------------------------------------------


def insert_canonical(
    conn: sqlite3.Connection,
    run_id: str,
    canonical_title: str,
    entity_tokens: Sequence[str],
    merge_confidence: float,
    needs_review: bool,
) -> int:
    conn.execute(
        """INSERT OR IGNORE INTO canonical_products
           (run_id, canonical_title, entity_tokens, merge_confidence, needs_review)
           VALUES (?,?,?,?,?)""",
        (
            run_id,
            canonical_title,
            json.dumps(list(entity_tokens), ensure_ascii=False),
            merge_confidence,
            int(needs_review),
        ),
    )
    row = conn.execute(
        "SELECT id FROM canonical_products WHERE run_id=? AND canonical_title=?",
        (run_id, canonical_title),
    ).fetchone()
    return int(row["id"])


def map_raw_to_canonical(conn: sqlite3.Connection, raw_id: int, canonical_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO product_mapping (raw_id, canonical_id) VALUES (?,?)",
        (raw_id, canonical_id),
    )


def canonical_products(
    conn: sqlite3.Connection, run_id: str, suggest_status: str | None = None
) -> list[sqlite3.Row]:
    if suggest_status is None:
        return conn.execute(
            "SELECT * FROM canonical_products WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM canonical_products WHERE run_id=? AND suggest_status=? ORDER BY id",
        (run_id, suggest_status),
    ).fetchall()


def update_canonical(conn: sqlite3.Connection, canonical_id: int, **fields: Any) -> None:
    allowed = {"suggest_status", "needs_review", "merge_confidence"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"unknown canonical field: {key}")
        sets.append(f"{key}=?")
        values.append(int(value) if isinstance(value, bool) else value)
    if not sets:
        return
    values.append(canonical_id)
    conn.execute(f"UPDATE canonical_products SET {', '.join(sets)} WHERE id=?", values)


def members_of(conn: sqlite3.Connection, canonical_id: int) -> list[sqlite3.Row]:
    """عنوان‌های خامی که در این محصول ادغام شده‌اند."""
    return conn.execute(
        """SELECT r.* FROM product_mapping m
           JOIN raw_products r ON r.id = m.raw_id
           WHERE m.canonical_id=? ORDER BY r.id""",
        (canonical_id,),
    ).fetchall()


def source_urls_for(conn: sqlite3.Connection, canonical_id: int) -> list[str]:
    return [row["source_url"] for row in members_of(conn, canonical_id)]


def source_domains_for(conn: sqlite3.Connection, canonical_id: int) -> list[str]:
    seen: list[str] = []
    for row in members_of(conn, canonical_id):
        domain = row["source_domain"] or ""
        if domain and domain not in seen:
            seen.append(domain)
    return seen


# ---------------------------------------------------------------------------
# lsi_keywords
# ---------------------------------------------------------------------------


def insert_keyword(
    conn: sqlite3.Connection,
    canonical_id: int,
    keyword: str,
    position: int,
    source_query: str = "",
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO lsi_keywords (canonical_id, keyword, position, source_query)
           VALUES (?,?,?,?)""",
        (canonical_id, keyword, position, source_query),
    )


def delete_keyword(conn: sqlite3.Connection, canonical_id: int, keyword: str) -> None:
    conn.execute(
        "DELETE FROM lsi_keywords WHERE canonical_id=? AND keyword=?", (canonical_id, keyword)
    )


def keywords_for(conn: sqlite3.Connection, canonical_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM lsi_keywords WHERE canonical_id=? ORDER BY position", (canonical_id,)
    ).fetchall()


def all_keywords(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT k.* FROM lsi_keywords k
           JOIN canonical_products c ON c.id = k.canonical_id
           WHERE c.run_id=? ORDER BY k.canonical_id, k.position""",
        (run_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# ویرایش دستی (پنل وب)
# ---------------------------------------------------------------------------


def merge_canonicals(conn: sqlite3.Connection, target_id: int, source_id: int) -> int:
    """ادغام دستی دو محصول. همه‌ی عنوان‌های خام و کلمات ``source`` به
    ``target`` منتقل و رکورد مبدأ حذف می‌شود. خروجی: تعداد عنوان منتقل‌شده."""
    if target_id == source_id:
        raise ValueError("محصول مبدأ و مقصد یکی است.")
    target = conn.execute(
        "SELECT * FROM canonical_products WHERE id=?", (target_id,)
    ).fetchone()
    source = conn.execute(
        "SELECT * FROM canonical_products WHERE id=?", (source_id,)
    ).fetchone()
    if target is None or source is None:
        raise ValueError("محصول پیدا نشد.")
    if target["run_id"] != source["run_id"]:
        raise ValueError("دو محصول از دو اجرای متفاوت‌اند.")

    moved = 0
    with transaction(conn):
        for row in conn.execute(
            "SELECT raw_id FROM product_mapping WHERE canonical_id=?", (source_id,)
        ).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO product_mapping (raw_id, canonical_id) VALUES (?,?)",
                (row["raw_id"], target_id),
            )
            moved += 1
        for row in conn.execute(
            "SELECT keyword, position FROM lsi_keywords WHERE canonical_id=?", (source_id,)
        ).fetchall():
            conn.execute(
                """INSERT OR IGNORE INTO lsi_keywords (canonical_id, keyword, position)
                   VALUES (?,?,?)""",
                (target_id, row["keyword"], row["position"]),
            )
        # توکن‌های تمایزدهنده‌ی هر دو طرف نگه داشته می‌شوند
        tokens = json.loads(target["entity_tokens"] or "[]")
        for token in json.loads(source["entity_tokens"] or "[]"):
            if token not in tokens:
                tokens.append(token)

        conn.execute("DELETE FROM product_mapping WHERE canonical_id=?", (source_id,))
        conn.execute("DELETE FROM lsi_keywords WHERE canonical_id=?", (source_id,))
        conn.execute("DELETE FROM canonical_products WHERE id=?", (source_id,))
        conn.execute(
            """UPDATE canonical_products
               SET entity_tokens=?, needs_review=0, merge_confidence=1.0
               WHERE id=?""",
            (json.dumps(tokens, ensure_ascii=False), target_id),
        )
        _refresh_suggest_status(conn, target_id)
    return moved


def detach_raw_product(conn: sqlite3.Connection, raw_id: int) -> int:
    """جدا کردن یک عنوان خام از محصولش و ساخت یک محصول مستقل برای آن.

    راه بازگشت از یک ادغام اشتباه. خروجی: شناسه‌ی محصول تازه.
    """
    raw = conn.execute("SELECT * FROM raw_products WHERE id=?", (raw_id,)).fetchone()
    if raw is None:
        raise ValueError("عنوان خام پیدا نشد.")
    current = conn.execute(
        "SELECT canonical_id FROM product_mapping WHERE raw_id=?", (raw_id,)
    ).fetchone()
    siblings = 0
    if current is not None:
        siblings = int(
            conn.execute(
                "SELECT COUNT(*) c FROM product_mapping WHERE canonical_id=?",
                (current["canonical_id"],),
            ).fetchone()["c"]
        )
        if siblings <= 1:
            raise ValueError("این عنوان تنها عضو محصولش است؛ چیزی برای جدا کردن نیست.")

    title = raw["raw_title"] or raw["normalized_title"] or f"عنوان {raw_id}"
    with transaction(conn):
        base, index = title, 2
        while conn.execute(
            "SELECT 1 FROM canonical_products WHERE run_id=? AND canonical_title=?",
            (raw["run_id"], title),
        ).fetchone():
            title = f"{base} #{index}"
            index += 1
        new_id = insert_canonical(conn, raw["run_id"], title, [], 1.0, True)
        if current is not None:
            conn.execute(
                "DELETE FROM product_mapping WHERE raw_id=? AND canonical_id=?",
                (raw_id, current["canonical_id"]),
            )
        conn.execute(
            "INSERT OR IGNORE INTO product_mapping (raw_id, canonical_id) VALUES (?,?)",
            (raw_id, new_id),
        )
    return new_id


def rename_canonical(conn: sqlite3.Connection, canonical_id: int, title: str) -> None:
    title = (title or "").strip()
    if not title:
        raise ValueError("عنوان خالی است.")
    row = conn.execute(
        "SELECT run_id FROM canonical_products WHERE id=?", (canonical_id,)
    ).fetchone()
    if row is None:
        raise ValueError("محصول پیدا نشد.")
    clash = conn.execute(
        "SELECT id FROM canonical_products WHERE run_id=? AND canonical_title=? AND id<>?",
        (row["run_id"], title, canonical_id),
    ).fetchone()
    if clash is not None:
        raise ValueError("محصول دیگری با همین عنوان وجود دارد.")
    with transaction(conn):
        conn.execute(
            "UPDATE canonical_products SET canonical_title=? WHERE id=?", (title, canonical_id)
        )


def set_needs_review(conn: sqlite3.Connection, canonical_id: int, flag: bool) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE canonical_products SET needs_review=? WHERE id=?",
            (int(flag), canonical_id),
        )


def _refresh_suggest_status(conn: sqlite3.Connection, canonical_id: int) -> None:
    """پس از ادغام دستی، وضعیت ساجست باید با کلمات واقعی بخواند."""
    row = conn.execute(
        "SELECT suggest_status FROM canonical_products WHERE id=?", (canonical_id,)
    ).fetchone()
    if row is None or row["suggest_status"] is None:
        return
    count = conn.execute(
        "SELECT COUNT(*) c FROM lsi_keywords WHERE canonical_id=?", (canonical_id,)
    ).fetchone()["c"]
    conn.execute(
        "UPDATE canonical_products SET suggest_status=? WHERE id=?",
        ("has_suggest" if count else "no_suggest", canonical_id),
    )


def delete_run(conn: sqlite3.Connection, run_id: str) -> None:
    """حذف کامل یک اجرا با همه‌ی داده‌هایش."""
    with transaction(conn):
        conn.execute(
            """DELETE FROM product_mapping WHERE canonical_id IN
               (SELECT id FROM canonical_products WHERE run_id=?)""",
            (run_id,),
        )
        conn.execute(
            """DELETE FROM lsi_keywords WHERE canonical_id IN
               (SELECT id FROM canonical_products WHERE run_id=?)""",
            (run_id,),
        )
        conn.execute("DELETE FROM canonical_products WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM raw_products WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
