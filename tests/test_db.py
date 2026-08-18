"""تست لایه‌ی state: idempotency، resume و یکتایی."""

from __future__ import annotations

import pytest

from content_pipeline.core import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "runs.db")
    yield connection
    connection.close()


def insert(conn, run_id, url="https://a.ir/p/1", title="رمان الف"):
    with db.transaction(conn):
        db.insert_raw_product(
            conn,
            run_id=run_id,
            source_domain="a.ir",
            source_url=url,
            raw_title=title,
            normalized_title="رمان الف",
            topic_score=0.9,
            is_relevant=True,
        )


def test_schema_is_created(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "runs",
        "raw_products",
        "canonical_products",
        "product_mapping",
        "lsi_keywords",
        "api_cache",
    } <= tables


def test_rerunning_a_phase_does_not_duplicate_rows(conn):
    run_id = db.create_run(conn, "رمان")
    for _ in range(3):
        insert(conn, run_id)
    assert conn.execute("SELECT COUNT(*) c FROM raw_products").fetchone()["c"] == 1


def test_create_run_is_idempotent_for_same_id(conn):
    db.create_run(conn, "رمان", run_id="fixed")
    db.create_run(conn, "رمان", run_id="fixed")
    assert len(db.list_runs(conn)) == 1


def test_canonical_unique_per_run(conn):
    run_id = db.create_run(conn, "رمان")
    with db.transaction(conn):
        first = db.insert_canonical(conn, run_id, "رمان الف", ["آوا"], 0.95, False)
        second = db.insert_canonical(conn, run_id, "رمان الف", ["آوا"], 0.95, False)
    assert first == second
    assert conn.execute("SELECT COUNT(*) c FROM canonical_products").fetchone()["c"] == 1


def test_same_title_in_two_runs_is_allowed(conn):
    a = db.create_run(conn, "رمان", run_id="r1")
    b = db.create_run(conn, "رمان", run_id="r2")
    with db.transaction(conn):
        db.insert_canonical(conn, a, "رمان الف", [], 1.0, False)
        db.insert_canonical(conn, b, "رمان الف", [], 1.0, False)
    assert conn.execute("SELECT COUNT(*) c FROM canonical_products").fetchone()["c"] == 2


def test_keyword_unique_per_product(conn):
    run_id = db.create_run(conn, "رمان")
    with db.transaction(conn):
        canonical_id = db.insert_canonical(conn, run_id, "رمان الف", [], 1.0, False)
        db.insert_keyword(conn, canonical_id, "رمان الف pdf", 1)
        db.insert_keyword(conn, canonical_id, "رمان الف pdf", 5)
    assert len(db.keywords_for(conn, canonical_id)) == 1


def test_resume_sees_previous_phase_data(tmp_path):
    path = tmp_path / "runs.db"
    first = db.connect(path)
    run_id = db.create_run(first, "رمان")
    insert(first, run_id)
    db.set_run_status(first, run_id, db.RUNNING, 1)
    first.close()

    # اجرای بعدی، فرآیند جدید — همه‌چیز باید روی دیسک مانده باشد
    second = db.connect(path)
    assert len(db.relevant_raw_products(second, run_id)) == 1
    assert db.get_run(second, run_id)["current_phase"] == 1
    second.close()


def test_mapping_and_source_urls(conn):
    run_id = db.create_run(conn, "رمان")
    insert(conn, run_id, "https://a.ir/p/1")
    insert(conn, run_id, "https://b.ir/p/2")
    rows = db.relevant_raw_products(conn, run_id)
    with db.transaction(conn):
        canonical_id = db.insert_canonical(conn, run_id, "رمان الف", [], 1.0, False)
        for row in rows:
            db.map_raw_to_canonical(conn, int(row["id"]), canonical_id)
            db.map_raw_to_canonical(conn, int(row["id"]), canonical_id)  # تکراری
    assert db.source_urls_for(conn, canonical_id) == ["https://a.ir/p/1", "https://b.ir/p/2"]


def test_borderline_products_are_kept_with_null_relevance(conn):
    run_id = db.create_run(conn, "رمان")
    with db.transaction(conn):
        db.insert_raw_product(
            conn, run_id, "a.ir", "https://a.ir/x", "عنوان مرزی", "عنوان مرزی",
            topic_score=0.55, is_relevant=None,
        )
    assert len(db.borderline_raw_products(conn, run_id)) == 1
    assert len(db.relevant_raw_products(conn, run_id)) == 0


def test_source_domains_are_deduplicated(conn):
    run_id = db.create_run(conn, "رمان")
    insert(conn, run_id, "https://a.ir/p/1")
    insert(conn, run_id, "https://a.ir/p/2")
    insert(conn, run_id, "https://b.ir/p/3")
    rows = db.relevant_raw_products(conn, run_id)
    with db.transaction(conn):
        canonical_id = db.insert_canonical(conn, run_id, "رمان الف", [], 1.0, False)
        for row in rows:
            db.map_raw_to_canonical(conn, int(row["id"]), canonical_id)
    assert db.source_domains_for(conn, canonical_id) == ["a.ir"]
    assert len(db.source_urls_for(conn, canonical_id)) == 3
