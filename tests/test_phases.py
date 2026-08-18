"""تست فاز ۳ و خروجی، با کلاینت ساختگی (بدون شبکه)."""

from __future__ import annotations

import pytest

from content_pipeline.core import db, pipeline
from content_pipeline.core.config import load_config
from content_pipeline.core.suggest import SuggestBlocked
from content_pipeline.output import exporter
from content_pipeline.phases import p3_suggest


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "runs.db")
    run_id = db.create_run(conn, "رمان", run_id="r1")
    config = load_config(None)
    config.raw["database"]["path"] = str(tmp_path / "runs.db")
    config.raw["output"]["xlsx_path"] = str(tmp_path / "out.xlsx")
    yield conn, run_id, config
    conn.close()


def add_product(conn, run_id, title, needs_review=False, confidence=0.95):
    with db.transaction(conn):
        return db.insert_canonical(conn, run_id, title, [], confidence, needs_review)


class FakeSuggest:
    """جایگزین SuggestClient با پاسخ ثابت."""

    def __init__(self, suggestions=None, blow_up_after=None):
        self.suggestions = suggestions or {}
        self.blow_up_after = blow_up_after
        self.queries_sent = 0
        self.queries: list[str] = []

        class _Config:
            max_per_session = 300
            delay_min = 0
            delay_max = 0

        self.config = _Config()

    def suggest(self, query):
        self.queries.append(query)
        if self.blow_up_after is not None and self.queries_sent >= self.blow_up_after:
            raise SuggestBlocked("سه پاسخ خالی پشت‌سرهم")
        self.queries_sent += 1
        return self.suggestions.get(query, [])


# ---------------------------------------------------------------------------
# فاز ۳
# ---------------------------------------------------------------------------


def test_empty_result_becomes_no_suggest(env):
    conn, run_id, config = env
    add_product(conn, run_id, "رمان الف")
    stats = p3_suggest.run(conn, run_id, config, FakeSuggest(), verbose=False)

    row = db.canonical_products(conn, run_id)[0]
    assert row["suggest_status"] == p3_suggest.NO_SUGGEST
    assert stats.without_suggest == 1
    assert len(db.canonical_products(conn, run_id)) == 1  # رکورد حذف نمی‌شود


def test_keywords_are_stored_as_rows_not_a_blob(env):
    conn, run_id, config = env
    add_product(conn, run_id, "رمان تاوان خیانت")
    client = FakeSuggest({"رمان تاوان خیانت": ["رمان تاوان خیانت pdf", "رمان تاوان خیانت آوا"]})
    p3_suggest.run(conn, run_id, config, client, verbose=False)

    canonical_id = db.canonical_products(conn, run_id)[0]["id"]
    rows = db.keywords_for(conn, canonical_id)
    assert len(rows) == 2
    assert [r["position"] for r in rows] == [1, 2]


def test_lsi_conflict_goes_to_the_closest_product(env):
    """یک کلمه فقط به نزدیک‌ترین محصول می‌چسبد.

    تداخل واقعی وقتی پیش می‌آید که کلمه، عنوان هر دو محصول را پوشش بدهد —
    مثل «رمان تاوان خیانت» و «رمان تاوان خیانت آوا».
    """
    conn, run_id, config = env
    config.raw["suggest"]["max_variants_per_product"] = 1
    add_product(conn, run_id, "رمان تاوان خیانت آوا")
    add_product(conn, run_id, "رمان تاوان خیانت")
    shared = "رمان تاوان خیانت آوا جلد دوم"
    client = FakeSuggest(
        {"رمان تاوان خیانت آوا": [shared], "رمان تاوان خیانت": [shared]}
    )
    stats = p3_suggest.run(conn, run_id, config, client, verbose=False)

    assert stats.conflicts_resolved == 1
    owners = {
        row["canonical_id"] for row in db.all_keywords(conn, run_id) if row["keyword"] == shared
    }
    winner_id = next(
        r["id"] for r in db.canonical_products(conn, run_id) if "آوا" in r["canonical_title"]
    )
    assert owners == {winner_id}


def test_product_losing_all_keywords_becomes_no_suggest(env):
    conn, run_id, config = env
    add_product(conn, run_id, "رمان تاوان خیانت آوا")
    add_product(conn, run_id, "جزوه ریاضی گسسته")
    client = FakeSuggest(
        {
            "رمان تاوان خیانت آوا": ["رمان تاوان خیانت آوا pdf"],
            "جزوه ریاضی گسسته": ["رمان تاوان خیانت آوا pdf"],
        }
    )
    p3_suggest.run(conn, run_id, config, client, verbose=False)
    loser = next(
        r for r in db.canonical_products(conn, run_id) if "ریاضی" in r["canonical_title"]
    )
    assert loser["suggest_status"] == p3_suggest.NO_SUGGEST


def test_blocked_run_stops_but_keeps_what_it_had(env):
    conn, run_id, config = env
    for index in range(4):
        add_product(conn, run_id, f"محصول {index}")
    config.raw["suggest"]["max_variants_per_product"] = 1
    # کوئری با ارقام فارسی فرستاده می‌شود (نرمال‌سازی فاز ۰) و کلمه‌ی پیشنهادی
    # باید عنوان را پوشش بدهد، وگرنه بی‌ربط شمرده و حذف می‌شود
    client = FakeSuggest(
        {"محصول ۰": ["محصول ۰ کامل"], "محصول ۱": ["محصول ۱ کامل"]}, blow_up_after=2
    )

    stats = p3_suggest.run(conn, run_id, config, client, verbose=False)
    assert stats.stopped_early
    assert stats.with_suggest == 2
    # دو محصول باقی‌مانده هنوز در صف‌اند و با --resume-from 3 ادامه می‌یابند
    assert len(p3_suggest.pending_products(conn, run_id)) == 2


def test_resume_only_processes_pending_products(env):
    conn, run_id, config = env
    add_product(conn, run_id, "محصول الف")
    add_product(conn, run_id, "محصول ب")
    first = FakeSuggest({"محصول الف": ["الف ۱"], "محصول ب": ["ب ۱"]})
    p3_suggest.run(conn, run_id, config, first, verbose=False)

    second = FakeSuggest({"محصول الف": ["الف ۱"], "محصول ب": ["ب ۱"]})
    stats = p3_suggest.run(conn, run_id, config, second, verbose=False)
    assert stats.products == 0
    assert second.queries_sent == 0


# ---------------------------------------------------------------------------
# خروجی
# ---------------------------------------------------------------------------


def _prepare_export(conn, run_id):
    with db.transaction(conn):
        db.insert_raw_product(
            conn, run_id, "a.ir", "https://a.ir/1", "محصول الف", "محصول الف", 0.9, True
        )
        db.insert_raw_product(
            conn, run_id, "b.ir", "https://b.ir/x", "عنوان مرزی", "عنوان مرزی", 0.55, None
        )
        ready = db.insert_canonical(conn, run_id, "محصول الف", [], 0.95, False)
        archived = db.insert_canonical(conn, run_id, "محصول ب", [], 0.95, False)
        flagged = db.insert_canonical(conn, run_id, "محصول ج", [], 0.50, True)
        db.update_canonical(conn, ready, suggest_status=p3_suggest.HAS_SUGGEST)
        db.update_canonical(conn, archived, suggest_status=p3_suggest.NO_SUGGEST)
        db.update_canonical(conn, flagged, suggest_status=p3_suggest.NO_SUGGEST)
        db.insert_keyword(conn, ready, "محصول الف pdf", 1)
        db.insert_keyword(conn, ready, "محصول الف کامل", 2)
        raw_id = conn.execute(
            "SELECT id FROM raw_products WHERE source_url='https://a.ir/1'"
        ).fetchone()["id"]
        db.map_raw_to_canonical(conn, int(raw_id), ready)
    return ready


def test_four_sheets_are_produced(env):
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    ready, archive, everything, review = exporter.build_sheets(conn, run_id, config)

    assert ready.title == "آماده تولید محتوا"
    assert archive.title == "آرشیو آینده"
    assert everything.title == "همه محصولات"
    assert review.title == "نیاز به بازبینی دستی"
    assert archive.header == exporter.ARCHIVE_HEADER
    # ستون‌های «ساجست ۱..N» به شیت‌های کلیدواژه‌دار اضافه می‌شوند
    assert ready.header[: len(exporter.READY_HEADER)] == exporter.READY_HEADER
    assert everything.header[: len(exporter.ALL_HEADER)] == exporter.ALL_HEADER
    assert ready.header[-1] == "suggest_10"


def test_ready_sheet_carries_keywords_and_counts(env):
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    ready = exporter.build_sheets(conn, run_id, config)[0]

    row = ready.rows[0]
    assert row[0] == "محصول الف"
    assert row[1] == "محصول الف pdf | محصول الف کامل"
    assert row[2] == 2  # lsi_count
    assert row[4] == "a.ir"  # source_domains
    assert row[6] == 1  # merge_count


def test_archive_sheet_has_no_keyword_column(env):
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    archive = exporter.build_sheets(conn, run_id, config)[1]
    assert "lsi_keywords" not in archive.header
    assert len(archive.rows) == 2  # محصول ب و ج


def test_review_sheet_mixes_merge_doubts_and_borderline_titles(env):
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    review = exporter.build_sheets(conn, run_id, config)[3]

    kinds = {row[0] for row in review.rows}
    assert kinds == {"ادغام مبهم", "ارتباط مرزی با موضوع"}
    titles = {row[1] for row in review.rows}
    assert {"محصول ج", "عنوان مرزی"} <= titles


def test_export_writes_a_local_file(env):
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    stats = exporter.run(conn, run_id, config, push_to_sheets=False, verbose=False)
    assert stats.local_path
    assert stats.ready_rows == 1
    assert stats.archive_rows == 2
    assert stats.all_rows == 3       # همه‌ی محصولات در یک شیت
    assert stats.review_rows == 2


# ---------------------------------------------------------------------------
# اجرای مشترک CLI و پنل
# ---------------------------------------------------------------------------


def test_pipeline_run_phase_four_exports_and_completes_the_run(env):
    conn, run_id, config = env
    add_product(conn, run_id, "رمان شب سرد")

    lines: list[str] = []
    summary = pipeline.run_phase(conn, run_id, config, 4, log=lines.append)

    assert "خروجی" in summary
    assert db.get_run(conn, run_id)["status"] == db.COMPLETED
    assert db.get_run(conn, run_id)["current_phase"] == 4


def test_pipeline_run_phase_rejects_unknown_phase(env):
    conn, run_id, config = env
    with pytest.raises(ValueError):
        pipeline.run_phase(conn, run_id, config, 9)


# ---------------------------------------------------------------------------
# انتخاب خروجی و ستون ساجست
# ---------------------------------------------------------------------------


def test_all_products_sheet_marks_which_have_suggest(env):
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    everything = exporter.build_sheets(conn, run_id, config)[2]

    status = {row[0]: row[1] for row in everything.rows}
    assert status["محصول الف"] == "دارد"
    assert status["محصول ب"] == "ندارد"
    # ساجست‌دارها بالای شیت می‌آیند
    assert everything.rows[0][1] == "دارد"


def test_user_can_ask_for_only_the_sheets_they_want(env):
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    config.raw["output"]["sheets"] = ["all"]

    sheets = exporter.build_sheets(conn, run_id, config)

    assert [sheet.title for sheet in sheets] == ["همه محصولات"]
    stats = exporter.run(conn, run_id, config, push_to_sheets=False, verbose=False)
    assert stats.all_rows == 3
    assert stats.ready_rows == 0  # ساخته نشده، نه اینکه خالی باشد


def test_only_products_with_suggest(env):
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    config.raw["output"]["sheets"] = ["ready"]

    sheets = exporter.build_sheets(conn, run_id, config)

    assert [sheet.title for sheet in sheets] == ["آماده تولید محتوا"]
    assert len(sheets[0].rows) == 1


def test_unknown_sheet_names_fall_back_to_everything(env):
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    config.raw["output"]["sheets"] = ["چیز عجیب"]
    assert len(exporter.build_sheets(conn, run_id, config)) == 4


def test_category_column_follows_the_raw_titles(env):
    conn, run_id, config = env
    canonical = _prepare_export(conn, run_id)
    conn.execute(
        "UPDATE raw_products SET category='رمان' WHERE run_id=? AND raw_title LIKE 'محصول الف%'",
        (run_id,),
    )
    conn.commit()

    ready = exporter.build_sheets(conn, run_id, config)[0]
    assert db.category_of(conn, canonical) == "رمان"
    assert ready.rows[0][3] == "رمان"


# ---------------------------------------------------------------------------
# کوئری‌ای که واقعاً به گوگل می‌رود
# ---------------------------------------------------------------------------


def test_shop_noise_is_stripped_before_asking_google(env):
    """عنوان فروشگاهی کوئری‌ای است که هیچ‌کس تایپ نمی‌کند.

    اگر عنوان خام فرستاده شود، گوگل چیزی برنمی‌گرداند و همه‌ی محصولات
    اشتباهاً «بدون ساجست» علامت می‌خورند — همان چیزی که روی یک سایت واقعی دیده شد.
    """
    conn, run_id, config = env
    add_product(conn, run_id, "دانلود رمان بهار رضوانی از حدیثه نسخه کامل و اصلی pdf")
    client = FakeSuggest({"رمان بهار رضوانی حدیثه": ["رمان بهار رضوانی حدیثه کامل"]})

    stats = p3_suggest.run(conn, run_id, config, client, verbose=False)

    assert client.queries[0] == "رمان بهار رضوانی حدیثه"
    assert stats.with_suggest == 1


def test_query_length_is_capped(env):
    conn, run_id, config = env
    add_product(conn, run_id, "یک دو سه چهار پنج شش هفت هشت نه ده یازده")
    config.raw.setdefault("suggest", {})["max_query_words"] = 4
    config.raw["suggest"]["max_variants_per_product"] = 1
    client = FakeSuggest({})

    p3_suggest.run(conn, run_id, config, client, verbose=False)

    assert client.queries == ["یک دو سه چهار"]


# ---------------------------------------------------------------------------
# چند شکل کوئری و فیلتر ربط
# ---------------------------------------------------------------------------


def test_a_shorter_query_is_tried_when_the_full_one_is_empty(env):
    """عنوان کامل ساجست ندارد ولی شکل کوتاه‌ترش دارد — روی نمونه‌ی واقعی دیده شد."""
    conn, run_id, config = env
    add_product(conn, run_id, "دانلود رمان ارباب تعصبی من اثر ناشناس نسخه کامل pdf")
    client = FakeSuggest({"رمان ارباب تعصبی من": ["رمان ارباب تعصبی من رایگان"]})

    stats = p3_suggest.run(conn, run_id, config, client, verbose=False)

    assert client.queries[0] == "رمان ارباب تعصبی من ناشناس"   # اول دقیق‌ترین
    assert "رمان ارباب تعصبی من" in client.queries             # بعد کوتاه‌تر
    assert stats.with_suggest == 1
    canonical_id = db.canonical_products(conn, run_id)[0]["id"]
    assert [k["keyword"] for k in db.keywords_for(conn, canonical_id)] == [
        "رمان ارباب تعصبی من رایگان"
    ]


def test_a_prefixed_query_is_tried_too(env):
    """«پی دی اف رمان ...» گاهی نتیجه می‌دهد که خود عنوان نمی‌دهد."""
    conn, run_id, config = env
    add_product(conn, run_id, "رمان ارباب تعصبی من")
    client = FakeSuggest(
        {"پی دی اف رمان ارباب تعصبی من": ["پی دی اف رمان ارباب تعصبی من کامل"]}
    )

    stats = p3_suggest.run(conn, run_id, config, client, verbose=False)

    assert stats.with_suggest == 1
    assert any(q.startswith("پی دی اف") for q in client.queries)


def test_irrelevant_suggestions_are_dropped(env):
    """گوگل کنار پیشنهادهای مرتبط، شبیه‌های بی‌ربط هم می‌دهد."""
    conn, run_id, config = env
    config.raw["suggest"]["max_variants_per_product"] = 1
    add_product(conn, run_id, "رمان ارباب تعصبی")
    client = FakeSuggest(
        {
            "رمان ارباب تعصبی": [
                "رمان ارباب تعصبی من",        # مرتبط
                "جلد دوم رمان ارباب تعصبی من",  # مرتبط
                "رمان ارباب",                  # بی‌ربط — «تعصبی» ندارد
                "رمان غرور و تعصب",            # بی‌ربط
            ]
        }
    )

    stats = p3_suggest.run(conn, run_id, config, client, verbose=False)

    canonical_id = db.canonical_products(conn, run_id)[0]["id"]
    kept = [k["keyword"] for k in db.keywords_for(conn, canonical_id)]
    assert kept == ["رمان ارباب تعصبی من", "جلد دوم رمان ارباب تعصبی من"]
    assert stats.dropped_irrelevant == 2


def test_the_query_that_found_each_keyword_is_recorded(env):
    conn, run_id, config = env
    add_product(conn, run_id, "رمان ارباب تعصبی من")
    client = FakeSuggest({"رمان ارباب تعصبی من": ["رمان ارباب تعصبی من رایگان"]})

    p3_suggest.run(conn, run_id, config, client, verbose=False)

    canonical_id = db.canonical_products(conn, run_id)[0]["id"]
    row = db.keywords_for(conn, canonical_id)[0]
    assert row["source_query"] == "رمان ارباب تعصبی من"


def test_variants_stop_once_enough_keywords_are_found(env):
    conn, run_id, config = env
    config.raw["suggest"]["enough_keywords"] = 2
    add_product(conn, run_id, "رمان ارباب تعصبی من ناشناس")
    client = FakeSuggest(
        {"رمان ارباب تعصبی من ناشناس": [
            "رمان ارباب تعصبی من ناشناس کامل",
            "رمان ارباب تعصبی من ناشناس رایگان",
        ]}
    )

    p3_suggest.run(conn, run_id, config, client, verbose=False)

    assert client.queries == ["رمان ارباب تعصبی من ناشناس"]  # دومی لازم نشد


def test_each_suggestion_gets_its_own_column(env):
    """«ساجست‌های هر عنوان توی ستون خودش قرار بگیره»."""
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    config.raw["output"]["keyword_columns"] = 3

    everything = exporter.build_sheets(conn, run_id, config)[2]

    assert everything.header[-3:] == ["suggest_1", "suggest_2", "suggest_3"]
    row = next(r for r in everything.rows if r[0] == "محصول الف")
    assert row[-3:] == ["محصول الف pdf", "محصول الف کامل", ""]


def test_keyword_columns_can_be_switched_off(env):
    conn, run_id, config = env
    _prepare_export(conn, run_id)
    config.raw["output"]["keyword_columns"] = 0

    ready = exporter.build_sheets(conn, run_id, config)[0]
    assert ready.header == exporter.READY_HEADER
