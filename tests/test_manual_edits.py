"""تست ویرایش‌های دستی — راه بازگشت از ادغام اشتباه."""

from __future__ import annotations

import json

import pytest

from content_pipeline.core import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "runs.db")
    yield connection
    connection.close()


def make_product(conn, run_id, title, titles, tokens=(), suggest_status=None, keywords=()):
    """یک محصول یکپارچه با عنوان‌های خامش می‌سازد و شناسه‌اش را برمی‌گرداند."""
    canonical_id = db.insert_canonical(conn, run_id, title, list(tokens), 0.9, False)
    for index, raw_title in enumerate(titles):
        url = f"https://site{index}.ir/p/{abs(hash(raw_title)) % 10000}-{index}"
        db.insert_raw_product(
            conn,
            run_id=run_id,
            source_domain=f"site{index}.ir",
            source_url=url,
            raw_title=raw_title,
            normalized_title=raw_title,
            topic_score=0.9,
            is_relevant=True,
        )
        raw_id = conn.execute(
            "SELECT id FROM raw_products WHERE run_id=? AND source_url=?", (run_id, url)
        ).fetchone()["id"]
        db.map_raw_to_canonical(conn, raw_id, canonical_id)
    for position, keyword in enumerate(keywords):
        db.insert_keyword(conn, canonical_id, keyword, position)
    if suggest_status is not None:
        db.update_canonical(conn, canonical_id, suggest_status=suggest_status)
    conn.commit()
    return canonical_id


def raw_ids_of(conn, canonical_id):
    return [
        row["id"]
        for row in conn.execute(
            """SELECT r.id FROM product_mapping m JOIN raw_products r ON r.id=m.raw_id
               WHERE m.canonical_id=? ORDER BY r.id""",
            (canonical_id,),
        )
    ]


# ---------------------------------------------------------------- merge


def test_manual_merge_moves_titles_and_keywords(conn):
    run_id = db.create_run(conn, "رمان")
    target = make_product(conn, run_id, "رمان شب سرد", ["رمان شب سرد"], keywords=["شب سرد الناز"])
    source = make_product(
        conn, run_id, "رمان شب سرد الناز", ["رمان شب سرد الناز"], keywords=["شب سرد کامل"]
    )

    moved = db.merge_canonicals(conn, target, source)

    assert moved == 1
    assert len(raw_ids_of(conn, target)) == 2
    assert {k["keyword"] for k in db.keywords_for(conn, target)} == {
        "شب سرد الناز",
        "شب سرد کامل",
    }
    assert conn.execute(
        "SELECT COUNT(*) c FROM canonical_products WHERE id=?", (source,)
    ).fetchone()["c"] == 0


def test_manual_merge_unions_entity_tokens_and_clears_review(conn):
    run_id = db.create_run(conn, "رمان")
    target = make_product(conn, run_id, "الف", ["الف"], tokens=["الناز"])
    source = make_product(conn, run_id, "ب", ["ب"], tokens=["دوم"])
    db.set_needs_review(conn, target, True)

    db.merge_canonicals(conn, target, source)

    row = conn.execute("SELECT * FROM canonical_products WHERE id=?", (target,)).fetchone()
    assert set(json.loads(row["entity_tokens"])) == {"الناز", "دوم"}
    assert row["needs_review"] == 0
    assert row["merge_confidence"] == 1.0


def test_manual_merge_refreshes_suggest_status(conn):
    run_id = db.create_run(conn, "رمان")
    target = make_product(conn, run_id, "الف", ["الف"], suggest_status="no_suggest")
    source = make_product(conn, run_id, "ب", ["ب"], keywords=["ب کامل"])

    db.merge_canonicals(conn, target, source)

    row = conn.execute("SELECT * FROM canonical_products WHERE id=?", (target,)).fetchone()
    assert row["suggest_status"] == "has_suggest"


def test_manual_merge_rejects_cross_run_and_self(conn):
    first = db.create_run(conn, "رمان", run_id="r1")
    second = db.create_run(conn, "جزوه", run_id="r2")
    a = make_product(conn, first, "الف", ["الف"])
    b = make_product(conn, second, "ب", ["ب"])

    with pytest.raises(ValueError):
        db.merge_canonicals(conn, a, b)
    with pytest.raises(ValueError):
        db.merge_canonicals(conn, a, a)


# --------------------------------------------------------------- detach


def test_detach_creates_independent_product(conn):
    run_id = db.create_run(conn, "رمان")
    canonical_id = make_product(
        conn, run_id, "رمان شب سرد", ["رمان شب سرد الناز", "رمان شب گرم الناز"]
    )
    wrong_raw = raw_ids_of(conn, canonical_id)[1]

    new_id = db.detach_raw_product(conn, wrong_raw)

    assert new_id != canonical_id
    assert raw_ids_of(conn, canonical_id) == raw_ids_of(conn, canonical_id)[:1]
    assert raw_ids_of(conn, new_id) == [wrong_raw]
    row = conn.execute("SELECT * FROM canonical_products WHERE id=?", (new_id,)).fetchone()
    assert row["needs_review"] == 1
    assert row["canonical_title"] == "رمان شب گرم الناز"


def test_detach_refuses_when_it_is_the_only_member(conn):
    run_id = db.create_run(conn, "رمان")
    canonical_id = make_product(conn, run_id, "رمان شب سرد", ["رمان شب سرد"])
    only = raw_ids_of(conn, canonical_id)[0]

    with pytest.raises(ValueError):
        db.detach_raw_product(conn, only)


def test_detach_avoids_title_clash(conn):
    run_id = db.create_run(conn, "رمان")
    canonical_id = make_product(conn, run_id, "الف", ["الف تکراری", "ب"])
    make_product(conn, run_id, "الف تکراری", ["ج"])
    raw_id = raw_ids_of(conn, canonical_id)[0]

    new_id = db.detach_raw_product(conn, raw_id)

    title = conn.execute(
        "SELECT canonical_title t FROM canonical_products WHERE id=?", (new_id,)
    ).fetchone()["t"]
    assert title == "الف تکراری #2"


# --------------------------------------------------------------- rename


def test_rename_rejects_empty_and_duplicate(conn):
    run_id = db.create_run(conn, "رمان")
    first = make_product(conn, run_id, "الف", ["الف"])
    make_product(conn, run_id, "ب", ["ب"])

    db.rename_canonical(conn, first, "الف تازه")
    assert conn.execute(
        "SELECT canonical_title t FROM canonical_products WHERE id=?", (first,)
    ).fetchone()["t"] == "الف تازه"

    with pytest.raises(ValueError):
        db.rename_canonical(conn, first, "  ")
    with pytest.raises(ValueError):
        db.rename_canonical(conn, first, "ب")


# ------------------------------------------------------------ delete_run


def test_delete_run_removes_every_trace(conn):
    keep = db.create_run(conn, "رمان", run_id="keep")
    drop = db.create_run(conn, "رمان", run_id="drop")
    make_product(conn, keep, "الف", ["الف"], keywords=["الف کامل"])
    make_product(conn, drop, "ب", ["ب"], keywords=["ب کامل"])

    db.delete_run(conn, drop)

    assert db.get_run(conn, drop) is None
    assert conn.execute("SELECT COUNT(*) c FROM raw_products WHERE run_id=?", (drop,)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM canonical_products WHERE run_id=?", (drop,)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM lsi_keywords").fetchone()["c"] == 1
    assert db.get_run(conn, keep) is not None
