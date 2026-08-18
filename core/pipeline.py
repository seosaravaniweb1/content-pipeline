"""اجرای فازها — منبع مشترک CLI و پنل وب.

هر دو رابط کاربری از همین توابع استفاده می‌کنند تا رفتارشان از هم جدا نیفتد.
هیچ وابستگی‌ای به typer یا HTTP اینجا نیست؛ فقط ``log`` که یک تابع چاپ است.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Sequence

from . import db, presets
from .cache import QueryCache
from .config import Config
from .embeddings import TopicMatcher, get_encoder
from .http import fetcher_from_config
from .suggest import suggest_from_config
from ..output import exporter
from ..phases import p1_crawl, p2_resolve, p3_suggest

LogFn = Callable[[str], None]

PHASE_NAMES: dict[int, str] = {
    1: "کراول و تشخیص موضوعی",
    2: "Entity Resolution",
    3: "گوگل ساجست",
    4: "خروجی",
}
LAST_PHASE = 4


def topic_matcher(
    config: Config,
    log: LogFn = print,
    categories: Sequence[str] | None = None,
) -> TopicMatcher:
    """بردار مرجع تشخیص موضوعی.

    اگر اجرا دسته دارد (انتخاب کاربر در پنل) از همان‌ها ساخته می‌شود و هر
    عنوان به نزدیک‌ترین دسته نسبت داده می‌شود؛ وگرنه از ``run.target_topic``
    و نمونه‌عنوان‌های config.
    """
    encoder = get_encoder(config.get("embeddings.model"))
    log(f"انکودر: {encoder.name}")

    chosen = presets.resolve(categories or config.get("run.categories") or [])
    if chosen:
        log(f"دسته‌ها: {'، '.join(p.label for p in chosen)}")
        pairs = []
        extra = list(config.topic_examples)
        for preset in chosen:
            examples = list(preset.examples) or extra
            pairs.append((preset.label, examples))
        return TopicMatcher.build_categories(encoder, pairs)

    if not config.target_topic:
        raise ValueError(
            "نه دسته‌ای انتخاب شده و نه run.target_topic پر است."
            " از تب «شروع» پنل نوع محصول را انتخاب کنید."
        )
    return TopicMatcher.build_categories(
        encoder, [(config.target_topic, list(config.topic_examples))]
    )


def run_phase(
    conn: sqlite3.Connection,
    run_id: str,
    config: Config,
    phase: int,
    log: LogFn = print,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """اجرای یک فاز. خروجی، خط خلاصه‌ی همان فاز است.

    ``should_stop`` بین واحدهای کار (هر صفحه، هر کوئری) پرسیده می‌شود تا لغو
    وسط فاز هم کار کند، نه فقط بین دو فاز. داده‌ی تا آن لحظه نوشته شده است.
    """
    if phase == 1:
        matcher = topic_matcher(config, log, db.run_categories(conn, run_id))
        with fetcher_from_config(config) as fetcher:
            log(f"backend HTTP: {fetcher.backend}")
            if fetcher.backend != "curl_cffi":
                log(
                    "⚠ curl_cffi نصب نیست؛ اثرانگشت TLS ممکن است شناسایی شود. "
                    "نصب کنید: pip install curl_cffi"
                )
            summary = p1_crawl.run(
                conn, run_id, config, fetcher, matcher, should_stop=should_stop
            ).render()

    elif phase == 2:
        summary = p2_resolve.run(conn, run_id, config).render()

    elif phase == 3:
        cache = QueryCache(conn, ttl_days=int(config.get("suggest.cache_ttl_days", 30)))
        client = suggest_from_config(config, cache)
        pending = len(p3_suggest.pending_products(conn, run_id))
        log(
            f"{pending} محصول در صف؛ سقف این نشست: {client.config.max_per_session} کوئری، "
            f"تأخیر {client.config.delay_min}–{client.config.delay_max} ثانیه"
        )
        summary = p3_suggest.run(
            conn, run_id, config, client, should_stop=should_stop
        ).render()
        log(f"کش: {cache.hits} hit / {cache.misses} miss")

    elif phase == 4:
        summary = exporter.run(conn, run_id, config).render()

    else:
        raise ValueError(f"شماره فاز نامعتبر: {phase}")

    db.set_run_status(conn, run_id, db.RUNNING, phase)
    if phase == LAST_PHASE:
        db.set_run_status(conn, run_id, db.COMPLETED, LAST_PHASE)
    return summary


def counters(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    """شمارنده‌های وضعیت یک اجرا — هم برای CLI و هم برای داشبورد."""
    queries = {
        "raw": "SELECT COUNT(*) c FROM raw_products WHERE run_id=?",
        "relevant": "SELECT COUNT(*) c FROM raw_products WHERE run_id=? AND is_relevant=1",
        "borderline": "SELECT COUNT(*) c FROM raw_products WHERE run_id=? AND is_relevant IS NULL",
        "canonical": "SELECT COUNT(*) c FROM canonical_products WHERE run_id=?",
        "needs_review": "SELECT COUNT(*) c FROM canonical_products WHERE run_id=? AND needs_review=1",
        "has_suggest": "SELECT COUNT(*) c FROM canonical_products WHERE run_id=? AND suggest_status='has_suggest'",
        "no_suggest": "SELECT COUNT(*) c FROM canonical_products WHERE run_id=? AND suggest_status='no_suggest'",
        "pending_suggest": "SELECT COUNT(*) c FROM canonical_products WHERE run_id=? AND suggest_status IS NULL",
        "keywords": (
            "SELECT COUNT(*) c FROM lsi_keywords k JOIN canonical_products p"
            " ON p.id=k.canonical_id WHERE p.run_id=?"
        ),
    }
    return {
        key: int(conn.execute(query, (run_id,)).fetchone()["c"])
        for key, query in queries.items()
    }
