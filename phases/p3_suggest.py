"""فاز ۳ — گوگل ساجست و تفکیک دو گروه.

* برای هر ``canonical_product`` یک کوئری ساجست (با تأخیر و سقف نشست)
* نتایج در جدول ``lsi_keywords`` با ``canonical_id`` — نه رشته‌ی درهم در یک سلول
* جلوگیری از تداخل LSI: یک کلمه فقط برای نزدیک‌ترین محصول می‌ماند
* نتیجه‌ی خالی → ``no_suggest`` (رکورد حذف نمی‌شود؛ به شیت B می‌رود)

قواعد ایمنی endpoint غیررسمی در :mod:`core.suggest` اعمال شده‌اند.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from ..core import db, normalizer, similarity
from ..core.config import Config
from ..core.suggest import SessionLimitReached, SuggestBlocked, SuggestClient

HAS_SUGGEST = "has_suggest"
NO_SUGGEST = "no_suggest"


@dataclass
class SuggestStats:
    products: int = 0
    queried: int = 0
    from_cache: int = 0
    with_suggest: int = 0
    without_suggest: int = 0
    keywords_stored: int = 0
    dropped_irrelevant: int = 0
    conflicts_resolved: int = 0
    stopped_early: str = ""

    def render(self) -> str:
        line = (
            f"فاز ۳ — محصولات: {self.products}، کوئری زده‌شده: {self.queried}، "
            f"از کش: {self.from_cache}، دارای ساجست: {self.with_suggest}، "
            f"بدون ساجست: {self.without_suggest}، کلمات: {self.keywords_stored}، "
            f"بی‌ربط (حذف‌شده): {self.dropped_irrelevant}، "
            f"تداخل حل‌شده: {self.conflicts_resolved}"
        )
        if self.stopped_early:
            line += f"\n  ⚠ توقف زودهنگام: {self.stopped_early}"
        return line


def pending_products(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """محصولاتی که هنوز وضعیت ساجست ندارند — پایه‌ی resume."""
    return conn.execute(
        "SELECT * FROM canonical_products WHERE run_id=? AND suggest_status IS NULL ORDER BY id",
        (run_id,),
    ).fetchall()


def run(
    conn: sqlite3.Connection,
    run_id: str,
    config: Config,
    client: SuggestClient,
    verbose: bool = True,
    should_stop=None,
) -> SuggestStats:
    norm_config = normalizer.config_from_mapping(config.normalizer)
    stats = SuggestStats()
    products = pending_products(conn, run_id)
    stats.products = len(products)

    max_words = int(config.get("suggest.max_query_words", 8))
    max_variants = max(1, int(config.get("suggest.max_variants_per_product", 4)))
    enough = max(1, int(config.get("suggest.enough_keywords", 8)))
    min_coverage = float(config.get("suggest.min_keyword_coverage", 1.0))
    prefixes = config.get("suggest.query_prefixes", list(normalizer.DEFAULT_QUERY_PREFIXES))
    stop = should_stop or (lambda: False)

    for index, row in enumerate(products):
        if stop():
            stats.stopped_early = "به درخواست شما متوقف شد؛ بقیه در صف می‌مانند."
            if verbose:
                print(f"  {stats.stopped_early}")
            break
        canonical_id = int(row["id"])
        title = row["canonical_title"] or ""
        # عنوان فروشگاهی («دانلود رمان ... نسخه کامل pdf») کوئری‌ای است که کسی
        # تایپ نمی‌کند؛ گوگل برایش پیشنهادی ندارد و همه‌چیز «بدون ساجست» می‌شود.
        variants = normalizer.search_variants(
            title, norm_config, max_words, prefixes=prefixes
        )[:max_variants] or [title]
        if verbose and index < 3:
            print(f"  کوئری‌ها: {' | '.join(variants)}")

        found: dict[str, str] = {}  # کلمه → کوئری‌ای که پیدایش کرد
        halted = ""
        for variant in variants:
            if stop() or len(found) >= enough:
                break
            before = client.queries_sent
            try:
                suggestions = client.suggest(variant)
            except (SuggestBlocked, SessionLimitReached) as exc:
                # داده‌ی تا اینجا در دیتابیس محفوظ است؛ با --resume-from 3 ادامه می‌دهید.
                halted = str(exc)
                break
            if client.queries_sent == before:
                stats.from_cache += 1
            else:
                stats.queried += 1

            for keyword in suggestions:
                keyword = keyword.strip()
                if not keyword or keyword in found:
                    continue
                # گوگل کنار پیشنهادهای مرتبط، شبیه‌ها را هم می‌دهد
                if not normalizer.covers(keyword, variant, norm_config, min_coverage):
                    stats.dropped_irrelevant += 1
                    continue
                found[keyword] = variant

        if halted and not found:
            # این محصول اصلاً جوابی نگرفت؛ «بدون ساجست» علامتش نمی‌زنیم تا با
            # ادامه‌ی فاز ۳ دوباره پرسیده شود.
            stats.stopped_early = halted
            if verbose:
                print(f"  {halted}")
            break

        with db.transaction(conn):
            for position, (keyword, source) in enumerate(found.items(), start=1):
                db.insert_keyword(conn, canonical_id, keyword, position, source)
                stats.keywords_stored += 1
            db.update_canonical(
                conn,
                canonical_id,
                suggest_status=HAS_SUGGEST if found else NO_SUGGEST,
            )
        if found:
            stats.with_suggest += 1
        else:
            stats.without_suggest += 1

        if halted:
            stats.stopped_early = halted
            if verbose:
                print(f"  {halted}")
            break

    stats.conflicts_resolved = resolve_conflicts(conn, run_id, norm_config)
    _refresh_status(conn, run_id)
    return stats


def resolve_conflicts(
    conn: sqlite3.Connection, run_id: str, norm_config: normalizer.NormalizerConfig
) -> int:
    """اگر یک LSI به دو محصول نسبت داده شد، فقط برای نزدیک‌ترین محصول می‌ماند."""
    titles = {
        int(row["id"]): normalizer.normalize(row["canonical_title"] or "", norm_config)
        for row in db.canonical_products(conn, run_id)
    }
    owners: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in db.all_keywords(conn, run_id):
        owners[row["keyword"]].append((int(row["canonical_id"]), int(row["position"])))

    removed = 0
    with db.transaction(conn):
        for keyword, claims in owners.items():
            if len(claims) < 2:
                continue
            normalized_keyword = normalizer.normalize(keyword, norm_config)

            def score(claim: tuple[int, int]) -> tuple[float, int, int]:
                canonical_id, position = claim
                text_score = similarity.token_set_ratio(
                    normalized_keyword, titles.get(canonical_id, "")
                )
                # در تساوی: جایگاه بهتر (کوچک‌تر)، سپس id کوچک‌تر
                return (text_score, -position, -canonical_id)

            winner = max(claims, key=score)
            for canonical_id, _ in claims:
                if canonical_id != winner[0]:
                    db.delete_keyword(conn, canonical_id, keyword)
                    removed += 1
    return removed


def _refresh_status(conn: sqlite3.Connection, run_id: str) -> None:
    """پس از حذف تداخل‌ها ممکن است محصولی بدون هیچ کلمه‌ای بماند."""
    with db.transaction(conn):
        for row in db.canonical_products(conn, run_id):
            if row["suggest_status"] is None:
                continue
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM lsi_keywords WHERE canonical_id=?", (row["id"],)
            ).fetchone()["c"]
            db.update_canonical(
                conn, int(row["id"]), suggest_status=HAS_SUGGEST if count else NO_SUGGEST
            )
