"""کش کوئری‌ها.

اصل ۵ معماری: هر کوئری ساجست یک‌بار زده می‌شود و در ``api_cache`` می‌ماند، تا
اجرای دوباره‌ی فاز ۳ هیچ درخواست تکراری به گوگل نزند (هم سریع‌تر، هم خطر بلاک
شدن کمتر).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import db


def cache_key(namespace: str, params: dict[str, Any]) -> str:
    payload = json.dumps(
        {"ns": namespace, "params": params},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get(conn: sqlite3.Connection, key: str, ttl_days: int | None = None) -> Any | None:
    row = conn.execute("SELECT * FROM api_cache WHERE cache_key=?", (key,)).fetchone()
    if row is None:
        return None
    if ttl_days:
        try:
            created = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            created = None
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created > timedelta(days=ttl_days):
                return None
    try:
        return json.loads(row["response"])
    except json.JSONDecodeError:
        return None


def put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO api_cache (cache_key, response, created_at) VALUES (?,?,?)",
            (key, json.dumps(value, ensure_ascii=False), db.utcnow()),
        )


class QueryCache:
    """پوشش کش دور یک کوئری خارجی، با شمارش hit/miss."""

    def __init__(self, conn: sqlite3.Connection, ttl_days: int | None = 30) -> None:
        self.conn = conn
        self.ttl_days = ttl_days
        self.hits = 0
        self.misses = 0

    def call(self, namespace: str, params: dict[str, Any], fn: Callable[[], Any]) -> Any:
        key = cache_key(namespace, params)
        cached = get(self.conn, key, self.ttl_days)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        value = fn()
        put(self.conn, key, value)
        return value

    def peek(self, namespace: str, params: dict[str, Any]) -> bool:
        """آیا این کوئری از کش پاسخ می‌گیرد؟ (برای تخمین تعداد درخواست‌ها)"""
        return get(self.conn, cache_key(namespace, params), self.ttl_days) is not None
