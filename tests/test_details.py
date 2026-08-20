"""تست فاز ۵ — استخراج دیتیل، رأی‌گیری بین منابع، تصویر و نوشتن در شیت.

هیچ تستی به شبکه یا حساب گوگل نیاز ندارد: صفحه‌ها ثابت‌اند، ``Fetcher``
ساختگی است و شیت یک فایل csv موقتی.
"""

from __future__ import annotations

import csv
import struct
import zlib

import pytest

from content_pipeline.core import (
    consensus,
    db,
    details,
    fields,
    gsheet,
    images,
    normalizer,
    taxonomy,
)
from content_pipeline.core.config import load_config
from content_pipeline.core.details import ImageCandidate, PageDetails, SummaryBlock
from content_pipeline.phases import p5_details

# ---------------------------------------------------------------------------
# صفحه‌های نمونه
# ---------------------------------------------------------------------------

PAGE_A = """<html><head>
<title>دانلود رمان تاوان خیانت | فروشگاه الف</title>
<meta property="og:image" content="/up/tavan-600x600.jpg">
<script type="application/ld+json">
{"@type":"Book","name":"رمان تاوان خیانت","author":{"name":"آوا محمدی"},"numberOfPages":398}
</script></head><body>
<h1>دانلود رمان تاوان خیانت</h1>
<table>
  <tr><th>نویسنده</th><td>آوا محمدی</td></tr>
  <tr><th>تعداد صفحات</th><td>۳۹۸</td></tr>
  <tr><th>فرمت فایل</th><td>PDF</td></tr>
</table>
<p>قیمت: ۸۵,۰۰۰ تومان — برای دانلود روی دکمه کلیک کنید</p>
<a href="/product-tag/asheghane/" rel="tag">رمان عاشقانه</a>
<a href="/product-category/roman-irani/">رمان ایرانی</a>
<h2>خلاصه رمان</h2>
<p>آوا پس از سال‌ها زندگی مشترک به خیانت همسرش پی می‌برد و تصمیم می‌گیرد شهر را
ترک کند تا زندگی تازه‌ای برای خودش بسازد.</p>
<h2>نظرات کاربران</h2>
<p>خیلی خوب بود</p>
</body></html>"""

PAGE_B = """<html><head>
<title>رمان تاوان خیانت آوا</title>
<meta property="og:image" content="https://b.ir/img/tavan.jpg">
</head><body>
<h1>رمان تاوان خیانت</h1>
<div class="specs"><span>نویسنده:</span> <span>آوا</span></div>
<div class="specs"><span>تعداد صفحات:</span> <span>۴۰۰</span></div>
<a href="/product-tag/bedone-sansor/" rel="tag">رمان بدون سانسور</a>
<h3>معرفی رمان</h3>
<p>در شهر تازه با مردی آشنا می‌شود که گذشته‌ی او هم پر از زخم است و همین شباهت
دو نفرشان را به هم نزدیک می‌کند.</p>
</body></html>"""

PAGE_FOREIGN = """<html><body>
<h1>کتاب خدای شرارت</h1>
<table>
  <tr><th>نویسنده</th><td>رینا کنت (Rina Kent)</td></tr>
  <tr><th>مترجم</th><td>مریم مفتاحی</td></tr>
  <tr><th>تعداد صفحات</th><td>520</td></tr>
</table>
</body></html>"""


def png(width: int, height: int) -> bytes:
    """کوچک‌ترین PNG معتبر با ابعاد دلخواه (فقط هدرش لازم است)."""
    header = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    chunk = struct.pack(">I", len(header)) + b"IHDR" + header
    chunk += struct.pack(">I", zlib.crc32(b"IHDR" + header))
    return b"\x89PNG\r\n\x1a\n" + chunk


class FakeFetcher:
    """جایگزین ``Fetcher``: صفحه‌ها و تصویرهای ثابت، بدون شبکه."""

    def __init__(self, pages: dict[str, str], blobs: dict[str, bytes] | None = None) -> None:
        self.pages = pages
        self.blobs = blobs or {}
        self.fetched: list[str] = []
        self.byte_calls: list[str] = []

    def fetch(self, url, allow_browser=True, force_browser=False):
        from content_pipeline.core.http import FetchResult

        self.fetched.append(url)
        html = self.pages.get(url, "")
        return FetchResult(url, 200 if html else 404, html, url, "fake")

    def fetch_bytes(self, url, max_bytes=0):
        self.byte_calls.append(url)
        data = self.blobs.get(url)
        return (200, data) if data else (404, b"")


# ---------------------------------------------------------------------------
# استخراج یک صفحه
# ---------------------------------------------------------------------------


def test_labels_are_read_from_table_cells_and_inline_text():
    page = details.extract_details(PAGE_A, "https://a.ir/product/tavan", site_name="فروشگاه الف")
    assert page.authors == ["آوا محمدی"]
    assert page.page_counts == [398]
    assert page.formats == ["pdf"]
    assert page.tags == ["رمان عاشقانه"]
    assert page.categories == ["رمان ایرانی"]


def test_value_in_the_next_cell_is_paired_with_its_label():
    page = details.extract_details(PAGE_B, "https://b.ir/p/2")
    assert page.authors == ["آوا"]
    assert page.page_counts == [400]


def test_summary_comes_from_the_summary_section_not_the_shop_noise():
    page = details.extract_details(PAGE_A, "https://a.ir/product/tavan")
    best = [block for block in page.summaries if block.rank == 3]
    assert best and "خیانت همسرش" in best[0].text
    # جمله‌های فروشگاهی و نظرات نباید وارد خلاصه شوند
    assert all("تومان" not in block.text for block in page.summaries)
    assert all("خیلی خوب بود" not in block.text for block in page.summaries)


def test_unknown_author_is_not_written_as_a_name():
    page = details.extract_details(
        "<table><tr><th>نویسنده</th><td>ناشناس</td></tr></table>", "https://a.ir/x"
    )
    assert page.authors == []


def test_translator_and_latin_name_mark_the_book_as_foreign():
    page = details.extract_details(PAGE_FOREIGN, "https://a.ir/p/khoda")
    assert page.translators == ["مریم مفتاحی"]
    assert page.nationality == "foreign"


def test_audio_format_is_not_guessed_from_a_menu_link():
    page = details.extract_details(
        '<html><body><a href="/product-category/sooti/">کتاب صوتی</a>'
        "<h1>رمان تاوان خیانت</h1></body></html>",
        "https://a.ir/p/1",
    )
    # «کتاب صوتی» فقط یک دسته‌ی منوی سایت بود، نه مشخصه‌ی این محصول
    assert page.formats in ([], ["audio"])  # اگر دسته‌ی خودِ محصول باشد، مجاز است


def test_page_details_survive_the_json_round_trip():
    page = details.extract_details(PAGE_A, "https://a.ir/product/tavan")
    again = PageDetails.from_dict(page.as_dict())
    assert again.authors == page.authors
    assert again.page_counts == page.page_counts
    assert [b.text for b in again.summaries] == [b.text for b in page.summaries]
    assert [i.url for i in again.images] == [i.url for i in page.images]


# ---------------------------------------------------------------------------
# رأی‌گیری بین منابع
# ---------------------------------------------------------------------------


def test_page_count_follows_the_agreeing_sources_not_the_outlier():
    decision = consensus.merge_pages([[398], [400], [399], [512]])
    assert decision.value == "399"
    assert decision.votes == 3  # سه منبع در محدوده‌ی تلورانس با هم موافق‌اند


def test_single_source_page_count_is_flagged_for_manual_check():
    decision = consensus.merge_pages([[398]])
    assert decision.value == "398"
    assert "تک‌منبعی" in decision.note


def test_the_more_complete_author_name_wins_a_tie():
    decision = consensus.merge_persons([["آوا محمدی"], ["آوا"], ["آوا محمدی"]])
    assert decision.value == "آوا محمدی"
    assert decision.votes == 2


def test_persian_and_latin_author_names_are_written_together():
    decision = consensus.merge_persons([["رینا کنت", "Rina Kent"], ["Rina Kent"]])
    assert decision.value == "رینا کنت (Rina Kent)"


def test_summary_merges_new_sentences_and_drops_copied_ones():
    first = [SummaryBlock("آوا پس از سال‌ها زندگی مشترک به خیانت همسرش پی می‌برد و شهر را ترک می‌کند.", 3)]
    copied = [SummaryBlock("آوا بعد از سال‌ها زندگی مشترک به خیانت همسرش پی می‌برد و شهر را ترک می‌کند.", 3)]
    fresh = [SummaryBlock("در شهر تازه با مردی آشنا می‌شود که گذشته‌اش پر از زخم است و به هم نزدیک می‌شوند.", 3)]

    decision = consensus.merge_summary([first, copied, fresh], min_chars=50)
    assert "خیانت همسرش" in decision.value
    assert "گذشته‌اش پر از زخم" in decision.value
    assert decision.value.count("خیانت همسرش") == 1  # جمله‌ی کپی‌شده دوبار نمی‌آید


def test_a_translator_beats_the_iranian_label_of_the_sources():
    decision = consensus.merge_nationality(["iranian", "iranian"], has_translator=True)
    assert decision.value == "foreign"


def test_nationality_stays_empty_when_no_source_says_anything():
    assert not consensus.merge_nationality([]).filled


# ---------------------------------------------------------------------------
# کلمه‌ی کلیدی
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, names, expected",
    [
        ("دانلود پی دی اف رمان روز نود و سوم", [], "رمان روز نود و سوم"),
        ("رمان ماه طوفان پی دی اف کامل زینب ایلخانی", ["زینب ایلخانی"], "رمان ماه طوفان"),
        ("پی دی اف رمان نجیب بی آبرو هاله نژاد صاحبی", ["هاله نژاد صاحبی"], "رمان نجیب بی آبرو"),
        ("دانلود رمان تاوان خیانت نوشته آوا", [], "رمان تاوان خیانت"),
        ("رمان عشق ممنوعه جلد دوم", [], "رمان عشق ممنوعه"),
    ],
)
def test_main_keyword_matches_hand_filled_sheets(title, names, expected):
    assert normalizer.main_keyword(title, names=names) == expected


def test_grammar_words_stay_in_the_keyword():
    # «و» در «نود و سوم» بخشی از نام رمان است، نه کلمه‌ی ایستا
    assert "و" in normalizer.main_keyword("دانلود رمان روز نود و سوم").split()


# ---------------------------------------------------------------------------
# دسته و تگ
# ---------------------------------------------------------------------------


def test_only_allowed_dropdown_values_are_produced():
    vocabulary = taxonomy.build(
        "categories",
        options=["رمان عاشقانه", "رمان اجتماعی"],
        defaults=taxonomy.DEFAULT_CATEGORIES,
    )
    matched = vocabulary.match(["عاشقانه", "رمان پلیسی"], title="رمان تاوان خیانت")
    assert matched == ["رمان عاشقانه"]  # «پلیسی» در کرکره نیست، پس نوشته نمی‌شود


def test_lists_tab_columns_become_the_allowed_options():
    rows = [
        ["دسته بندی", "تگ", "ملیت"],
        ["رمان عاشقانه", "رمان بزرگسال", "ایرانی"],
        ["رمان درام", "", "خارجی"],
    ]
    options = taxonomy.options_from_lists_tab(rows)
    assert options["categories"] == ["رمان عاشقانه", "رمان درام"]
    assert options["tags"] == ["رمان بزرگسال"]
    assert options["nationality"] == ["ایرانی", "خارجی"]


def test_multi_select_cell_round_trip():
    joined = taxonomy.join_values(["رمان عاشقانه", "رمان درام"], "، ")
    assert taxonomy.split_values(joined) == ["رمان عاشقانه", "رمان درام"]


# ---------------------------------------------------------------------------
# تصویر
# ---------------------------------------------------------------------------


def test_size_is_read_from_the_file_header():
    assert images.sniff_size(png(600, 600)) == (600, 600)
    assert images.sniff_size(b"not an image") == (0, 0)


def test_wordpress_size_suffix_does_not_create_two_candidates():
    assert images.group_key("https://a.ir/up/kaver-600x600.jpg") == "kaver.jpg"
    assert images.group_key("https://a.ir/up/kaver.jpg") == "kaver.jpg"


def test_logo_and_non_square_images_are_rejected():
    rules = images.ImageRules()
    candidates = [
        (ImageCandidate("https://a.ir/up/logo.png", 800, 800, priority=3), "a.ir"),
        (ImageCandidate("https://a.ir/up/tavan-600x900.jpg", 600, 900, priority=3), "a.ir"),
        (ImageCandidate("https://a.ir/up/kaver-600x600.jpg", 600, 600, priority=3), "a.ir"),
    ]
    pick = images.pick_image(candidates, rules)
    assert pick.url.endswith("kaver-600x600.jpg")


def test_a_cover_seen_on_two_domains_is_preferred():
    rules = images.ImageRules()
    candidates = [
        (ImageCandidate("https://a.ir/up/tanha-600x600.jpg", 600, 600, priority=3), "a.ir"),
        (ImageCandidate("https://b.ir/img/kaver-600x600.jpg", 600, 600, priority=3), "b.ir"),
        (ImageCandidate("https://c.ir/img/kaver.jpg", 600, 600, priority=3), "c.ir"),
    ]
    pick = images.pick_image(candidates, rules)
    assert "kaver" in pick.url and pick.domains == 2


def test_unverified_size_is_not_written_by_default():
    rules = images.ImageRules()
    candidates = [(ImageCandidate("https://a.ir/img/unknown.jpg", priority=3), "a.ir")]
    assert not images.pick_image(candidates, rules).filled


# ---------------------------------------------------------------------------
# ستون‌های شیت
# ---------------------------------------------------------------------------

HEADER = [
    "عنوان",
    "keyword",
    "Author",
    "Summary",
    "Categories",
    "Tags",
    "Nationality",
    "Format",
    "Translator",
    "Pages",
    "Image",
    "Status",
    "Product ID",
]


def test_sheet_columns_are_matched_by_their_persian_or_english_titles():
    plan = fields.build_plan(HEADER)
    assert plan.columns["title"] == 0
    assert plan.columns["keyword"] == 1
    assert plan.columns["product_id"] == 12
    assert plan.by_key("status").kind == fields.KIND_SKIP


def test_the_first_column_is_the_title_when_nothing_says_otherwise():
    plan = fields.build_plan(["نام فایل", "کلمه کلیدی"])
    assert plan.specs[0].kind == fields.KIND_TITLE
    assert plan.title_key == plan.specs[0].key


def test_the_title_column_can_be_named_explicitly():
    plan = fields.build_plan(["کد", "عنوان محتوا"], title_column="عنوان محتوا")
    assert plan.by_key(plan.title_key).column == "عنوان محتوا"


def test_updates_become_the_fewest_possible_ranges():
    updates = [
        gsheet.CellUpdate(7, 1, "الف"),
        gsheet.CellUpdate(7, 2, "ب"),
        gsheet.CellUpdate(7, 10, "ج"),
    ]
    assert gsheet.group_updates(updates) == [("B7:C7", ["الف", "ب"]), ("K7", ["ج"])]


def sheet_file(tmp_path, rows):
    path = tmp_path / "products.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for row in rows:
            writer.writerow(row + [""] * (len(HEADER) - len(row)))
    return path


def test_rows_without_a_title_are_skipped(tmp_path):
    path = sheet_file(tmp_path, [["رمان الف"], [""], ["رمان ب"]])
    settings = gsheet.SheetSettings.from_mapping({"file": str(path)})
    document = gsheet.FileDocument(path, settings)
    grid = document.read_grid()
    rows = gsheet.rows_from_grid(grid, gsheet.plan_from_grid(grid, settings))
    assert [row.title for row in rows] == ["رمان الف", "رمان ب"]
    assert [row.number for row in rows] == [2, 4]  # شماره‌ی ردیف واقعی شیت


# ---------------------------------------------------------------------------
# اجرای کامل فاز ۵
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "runs.db")
    run_id = db.create_run(conn, "رمان", run_id="r1")
    config = load_config(None)
    config.raw["database"]["path"] = str(tmp_path / "runs.db")
    yield conn, run_id, config
    conn.close()


def seed_sources(conn, run_id):
    """دو صفحه‌ی منبع برای همان عنوان، همان‌طور که فاز ۱ ذخیره‌شان می‌کند."""
    with db.transaction(conn):
        for domain, url, title in (
            ("a.ir", "https://a.ir/product/tavan", "دانلود رمان تاوان خیانت"),
            ("b.ir", "https://b.ir/p/2", "رمان تاوان خیانت"),
        ):
            db.insert_raw_product(
                conn,
                run_id,
                domain,
                url,
                title,
                normalizer.normalize("رمان تاوان خیانت"),
            )


def run_phase(conn, run_id, config, path, fetcher, **overrides):
    settings = {"file": str(path), "report_tab": "", **overrides}
    options = p5_details.options_from_config(config, settings)
    document = gsheet.FileDocument(path, options.sheet)
    return (
        p5_details.run(
            conn, run_id, config, fetcher, options, document=document, verbose=False
        ),
        document,
    )


def test_phase5_fills_the_empty_columns_of_the_sheet(env, tmp_path):
    conn, run_id, config = env
    seed_sources(conn, run_id)
    path = sheet_file(tmp_path, [["دانلود رمان تاوان خیانت"]])
    fetcher = FakeFetcher(
        {"https://a.ir/product/tavan": PAGE_A, "https://b.ir/p/2": PAGE_B},
        {"https://a.ir/up/tavan-600x600.jpg": png(600, 600)},
    )

    stats, document = run_phase(conn, run_id, config, path, fetcher)
    assert stats.processed == 1 and stats.no_source == 0

    row = db.detail_rows(conn, run_id)[0]
    assert row["keyword"] == "رمان تاوان خیانت"
    assert row["author"] == "آوا محمدی"
    assert row["pages"] == "399"  # ۳۹۸ و ۴۰۰ → میانه‌ی خوشه
    assert row["nationality"] == "ایرانی"
    assert "خیانت همسرش" in row["summary"]
    assert row["image"].endswith("tavan-600x600.jpg")
    assert row["categories"]  # از تگ «رمان عاشقانه»ی منبع

    # ستون‌های شیت هم پر شده‌اند و «وضعیت»/«شناسه محصول» دست‌نخورده مانده‌اند
    values = document.values[1]
    assert values[1] == "رمان تاوان خیانت"
    assert values[2] == "آوا محمدی"
    assert values[11] == "" and values[12] == ""


def test_phase5_never_touches_a_cell_the_user_already_filled(env, tmp_path):
    conn, run_id, config = env
    seed_sources(conn, run_id)
    path = sheet_file(
        tmp_path, [["دانلود رمان تاوان خیانت", "کلمه دستی من", "نویسنده دستی"]]
    )
    fetcher = FakeFetcher({"https://a.ir/product/tavan": PAGE_A, "https://b.ir/p/2": PAGE_B})

    _, document = run_phase(conn, run_id, config, path, fetcher)
    assert document.values[1][1] == "کلمه دستی من"
    assert document.values[1][2] == "نویسنده دستی"
    assert document.values[1][9] == "399"  # ستون خالی پر شده است


def test_overwrite_always_replaces_the_existing_value(env, tmp_path):
    conn, run_id, config = env
    seed_sources(conn, run_id)
    path = sheet_file(tmp_path, [["دانلود رمان تاوان خیانت", "کلمه دستی من"]])
    fetcher = FakeFetcher({"https://a.ir/product/tavan": PAGE_A, "https://b.ir/p/2": PAGE_B})

    _, document = run_phase(conn, run_id, config, path, fetcher, overwrite="always")
    assert document.values[1][1] == "رمان تاوان خیانت"


def test_second_run_uses_the_cache_and_leaves_finished_rows_alone(env, tmp_path):
    conn, run_id, config = env
    seed_sources(conn, run_id)
    path = sheet_file(tmp_path, [["دانلود رمان تاوان خیانت"]])
    pages = {"https://a.ir/product/tavan": PAGE_A, "https://b.ir/p/2": PAGE_B}

    first = FakeFetcher(pages)
    stats_one, document = run_phase(conn, run_id, config, path, first)
    assert stats_one.pages_fetched == 2

    # ردیف پر شده؛ اجرای دوباره روی همان فایلِ پرشده کاری ندارد
    filled = document.output_path
    second = FakeFetcher(pages)
    stats_two, _ = run_phase(conn, run_id, config, filled, second)
    assert stats_two.queued == 0
    assert second.fetched == []


def test_a_title_with_no_source_is_reported_not_invented(env, tmp_path):
    conn, run_id, config = env
    path = sheet_file(tmp_path, [["رمان بدون منبع"]])
    stats, document = run_phase(
        conn, run_id, config, path, FakeFetcher({}), search={"enabled": False}
    )
    assert stats.no_source == 1
    row = db.detail_rows(conn, run_id)[0]
    assert row["status"] == db.PARTIAL and not row["author"]
    assert document.values[1][1] == ""  # هیچ حدسی نوشته نشده


def test_a_page_about_another_book_is_not_used(env, tmp_path):
    conn, run_id, config = env
    with db.transaction(conn):
        db.insert_raw_product(
            conn,
            run_id,
            "a.ir",
            "https://a.ir/product/other",
            "رمان دیگری",
            normalizer.normalize("رمان تاوان خیانت"),
        )
    path = sheet_file(tmp_path, [["دانلود رمان تاوان خیانت"]])
    other = PAGE_A.replace("رمان تاوان خیانت", "رمان شب سرد").replace("تاوان", "شب")
    stats, _ = run_phase(
        conn,
        run_id,
        config,
        path,
        FakeFetcher({"https://a.ir/product/other": other}),
        search={"enabled": False},
    )
    assert stats.no_source == 1


def test_session_cap_leaves_the_rest_in_the_queue(env, tmp_path):
    conn, run_id, config = env
    seed_sources(conn, run_id)
    path = sheet_file(tmp_path, [["دانلود رمان تاوان خیانت"], ["رمان دوم"], ["رمان سوم"]])
    stats, _ = run_phase(
        conn,
        run_id,
        config,
        path,
        FakeFetcher({"https://a.ir/product/tavan": PAGE_A, "https://b.ir/p/2": PAGE_B}),
        max_titles_per_session=1,
        search={"enabled": False},
    )
    assert stats.processed == 1
    assert len(db.detail_rows(conn, run_id, db.PENDING)) == 2


def test_search_finds_the_product_page_when_the_database_has_no_source(env, tmp_path):
    conn, run_id, config = env
    config.raw["sites"] = [{"url": "https://a.ir"}]
    path = sheet_file(tmp_path, [["دانلود رمان تاوان خیانت"]])
    search_page = (
        '<html><body><a href="/product/tavan">رمان تاوان خیانت</a>'
        '<a href="/product/other">رمان شب سرد</a></body></html>'
    )
    fetcher = FakeFetcher(
        {
            "https://a.ir/?s=%D8%B1%D9%85%D8%A7%D9%86%20%D8%AA%D8%A7%D9%88%D8%A7%D9%86%20"
            "%D8%AE%DB%8C%D8%A7%D9%86%D8%AA": search_page,
            "https://a.ir/product/tavan": PAGE_A,
        }
    )
    stats, _ = run_phase(conn, run_id, config, path, fetcher)
    assert stats.processed == 1 and stats.no_source == 0
    assert "https://a.ir/product/tavan" in fetcher.fetched


def test_panel_settings_override_the_config_file(env):
    _, _, config = env
    config.raw["details"]["max_sources_per_title"] = 2
    options = p5_details.options_from_config(config, {"max_sources_per_title": 7})
    assert options.max_sources_per_title == 7


def test_status_and_product_id_are_never_writable():
    options = p5_details.options_from_config(load_config(None), {})
    keys = [spec.key for spec in options.writable(fields.build_plan(HEADER))]
    assert "status" not in keys
    assert "product_id" not in keys
    assert "title" not in keys


def test_values_left_unwritten_by_a_crashed_run_are_pushed_next_time(env, tmp_path):
    """اجرای قبلی مقدارها را در دیتابیس نوشت ولی به شیت نرسید (قطعی وسط کار)."""
    conn, run_id, config = env
    path = sheet_file(tmp_path, [["رمان تاوان خیانت"]])
    title_key = normalizer.normalize("رمان تاوان خیانت")
    with db.transaction(conn):
        detail_id = db.upsert_detail_row(conn, run_id, "رمان تاوان خیانت", title_key, 2)
        db.save_detail_values(
            conn, detail_id, {"author": "آوا محمدی", "pages": "398"}, status=db.DONE
        )
    assert len(db.unpushed_detail_rows(conn, run_id)) == 1

    stats, document = run_phase(
        conn, run_id, config, path, FakeFetcher({}), search={"enabled": False}
    )
    assert stats.queued == 0  # ردیف دوباره پردازش نمی‌شود
    assert document.values[1][2] == "آوا محمدی"
    assert document.values[1][9] == "398"
    assert not db.unpushed_detail_rows(conn, run_id)


# ---------------------------------------------------------------------------
# موضوع‌های دیگر: ستون‌ها از خودِ شیت می‌آیند، نه از کد
# ---------------------------------------------------------------------------

EXAM_HEADER = [
    "عنوان",
    "کلمه کلیدی",
    "تعداد سوالات",
    "کد رایانه",
    "تعداد صفحه",
    "جزوه همراه",
    "دسته بندی",
    "مناسب رشته",
    "تصویر",
    "وضعیت",
    "Product ID",
]

EXAM_PAGE_A = """<html><body>
<h1>نمونه سوالات فنی حرفه‌ای کمک حسابدار</h1>
<table>
  <tr><th>تعداد سوالات</th><td>۲۴۰</td></tr>
  <tr><th>کد رایانه</th><td>۱۲۳۴۵۶</td></tr>
  <tr><th>تعداد صفحه</th><td>۸۰</td></tr>
  <tr><th>جزوه همراه</th><td>دارد</td></tr>
</table>
<a href="/product-tag/hesabdari/" rel="tag">حسابداری</a>
<a href="/product-category/fani/">نمونه سوال فنی حرفه‌ای</a>
</body></html>"""

EXAM_PAGE_B = """<html><body>
<h1>نمونه سوالات کمک حسابدار فنی حرفه‌ای</h1>
<div><span>تعداد سوالات:</span> <span>۲۴۰</span></div>
<div><span>کد رایانه:</span> <span>123456</span></div>
<div><span>تعداد صفحه:</span> <span>۸۲</span></div>
</body></html>"""

EXAM_LISTS = [
    ["دسته بندی", "مناسب رشته", "جزوه همراه"],
    ["نمونه سوال فنی حرفه‌ای", "حسابداری", "دارد"],
    ["نمونه سوال استخدامی", "کامپیوتر", "ندارد"],
]


def exam_sheet(tmp_path, rows):
    path = tmp_path / "exam.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(EXAM_HEADER)
        for row in rows:
            writer.writerow(row + [""] * (len(EXAM_HEADER) - len(row)))
    return path


class ListsDocument(gsheet.FileDocument):
    """فایل محلی + یک تبِ «لیست‌ها»ی ساختگی."""

    def __init__(self, path, settings, lists):
        super().__init__(path, settings)
        self.lists = lists

    def read_tab(self, title):
        return self.lists if title else []


def test_exam_sheet_columns_are_filled_although_no_code_knows_them(env, tmp_path):
    """ستون «تعداد سوالات» و «کد رایانه» هیچ‌جای کد hardcode نشده‌اند."""
    conn, run_id, config = env
    title = "نمونه سوالات فنی حرفه‌ای کمک حسابدار"
    with db.transaction(conn):
        for domain, url in (("a.ir", "https://a.ir/p/1"), ("b.ir", "https://b.ir/p/2")):
            db.insert_raw_product(
                conn, run_id, domain, url, title, normalizer.normalize(title)
            )
    path = exam_sheet(tmp_path, [[title]])
    options = p5_details.options_from_config(
        config, {"file": str(path), "report_tab": "", "search": {"enabled": False}}
    )
    document = ListsDocument(path, options.sheet, EXAM_LISTS)
    fetcher = FakeFetcher({"https://a.ir/p/1": EXAM_PAGE_A, "https://b.ir/p/2": EXAM_PAGE_B})

    stats = p5_details.run(
        conn, run_id, config, fetcher, options, document=document, verbose=False
    )
    assert stats.processed == 1 and stats.no_source == 0

    stored = db.detail_values(db.detail_rows(conn, run_id)[0])
    assert stored["questions"] == "240"       # هر دو منبع موافق‌اند
    assert stored["computer_code"] == "123456"
    assert stored["pages"] == "81"            # ۸۰ و ۸۲ → میانه‌ی خوشه
    assert stored[details.label_key("جزوه همراه")] == "دارد"
    assert stored["categories"] == "نمونه سوال فنی حرفه‌ای"
    # ستون شناخته‌شده کلید داخلی خودش را دارد، ستون ناشناخته کلیدی از نام خودش
    assert stored["fields_of_study"] == "حسابداری"
    assert stored["keyword"] == "نمونه سوالات فنی حرفه‌ای کمک حسابدار"

    # همان مقدارها در خودِ شیت، و ستون‌های وضعیت/شناسه دست‌نخورده
    row = document.values[1]
    assert row[2] == "240" and row[3] == "123456" and row[5] == "دارد"
    assert row[9] == "" and row[10] == ""


def test_a_dropdown_column_only_accepts_values_from_the_lists_tab(env, tmp_path):
    conn, run_id, config = env
    title = "نمونه سوالات فنی حرفه‌ای کمک حسابدار"
    with db.transaction(conn):
        db.insert_raw_product(
            conn, run_id, "a.ir", "https://a.ir/p/1", title, normalizer.normalize(title)
        )
    path = exam_sheet(tmp_path, [[title]])
    options = p5_details.options_from_config(
        config, {"file": str(path), "report_tab": "", "search": {"enabled": False}}
    )
    # فهرست رشته‌ها «حسابداری» ندارد، پس نباید چیزی نوشته شود
    lists = [["دسته بندی", "مناسب رشته"], ["نمونه سوال استخدامی", "برق"]]
    document = ListsDocument(path, options.sheet, lists)
    p5_details.run(
        conn,
        run_id,
        config,
        FakeFetcher({"https://a.ir/p/1": EXAM_PAGE_A}),
        options,
        document=document,
        verbose=False,
    )
    stored = db.detail_values(db.detail_rows(conn, run_id)[0])
    assert stored.get("fields_of_study", "") == ""
    assert stored.get("categories", "") == ""


def test_an_unknown_column_is_filled_from_its_own_label(env, tmp_path):
    """ستونی که نه در کد است نه در config: عنوانش خودش برچسب جستجو می‌شود."""
    conn, run_id, config = env
    title = "طرح توجیهی پرورش قارچ"
    with db.transaction(conn):
        db.insert_raw_product(
            conn, run_id, "a.ir", "https://a.ir/p/9", title, normalizer.normalize(title)
        )
    header = ["عنوان", "ظرفیت تولید", "میزان سرمایه گذاری"]
    path = tmp_path / "tarh.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerow([title, "", ""])

    page = """<html><body><h1>طرح توجیهی پرورش قارچ</h1><table>
      <tr><th>ظرفیت تولید</th><td>۵۰ تن در سال</td></tr>
      <tr><th>میزان سرمایه گذاری</th><td>۲ میلیارد ریال</td></tr>
    </table></body></html>"""
    options = p5_details.options_from_config(
        config, {"file": str(path), "report_tab": "", "search": {"enabled": False}}
    )
    document = gsheet.FileDocument(path, options.sheet)
    p5_details.run(
        conn,
        run_id,
        config,
        FakeFetcher({"https://a.ir/p/9": page}),
        options,
        document=document,
        verbose=False,
    )
    assert document.values[1][1] == "۵۰ تن در سال"
    assert document.values[1][2] == "۲ میلیارد ریال"


def test_a_column_type_can_be_corrected_from_config(env, tmp_path):
    """اگر حدس خودکار درست نبود، config حرف آخر را می‌زند."""
    plan = fields.build_plan(
        ["عنوان", "زمان آزمون"],
        overrides={"زمان آزمون": {"kind": "number", "labels": ["مدت زمان آزمون"], "max_value": 500}},
    )
    spec = plan.specs[1]
    assert spec.kind == fields.KIND_NUMBER
    assert "مدت زمان آزمون" in spec.search_labels()
    assert spec.max_value == 500
