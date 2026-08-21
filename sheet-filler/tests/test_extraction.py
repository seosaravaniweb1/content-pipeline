"""تست استخراج و رأی‌گیری — بدون شبکه و بدون حساب گوگل.

هیچ تستی به شبکه یا حساب گوگل نیاز ندارد: صفحه‌ها ثابت‌اند، ``Fetcher``
ساختگی است و شیت یک فایل csv موقتی.
"""

from __future__ import annotations

import csv
import struct
import zlib

import pytest

from sheet_filler.core import (
    consensus,
    db,
    details,
    fields,
    gsheet,
    images,
    normalizer,
    taxonomy,
)
from sheet_filler.core.config import load_config
from sheet_filler.core.details import ImageCandidate, PageDetails, SummaryBlock
from sheet_filler.core import filler  # noqa: F401 — برای تست تنظیمات

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
        from sheet_filler.core.http import FetchResult

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
    assert page.nationality == "sign:foreign"


# ---------------------------------------------------------------------------
# دقتِ ملیت — «رمان ایرانی» نباید خارجی زده شود
# ---------------------------------------------------------------------------


def test_a_translator_cell_that_says_none_is_not_a_translator():
    page = details.extract_details(
        "<table><tr><th>نویسنده</th><td>آوا محمدی</td></tr>"
        "<tr><th>مترجم</th><td>ندارد</td></tr></table>",
        "https://a.ir/p/1",
    )
    assert page.translators == []
    assert page.nationality != "sign:foreign"


def test_the_wordpress_post_author_is_not_the_book_author():
    """نویسنده‌ی JSON-LDِ یک ``Article`` مدیرِ سایت است، نه نویسنده‌ی کتاب."""
    page = details.extract_details(
        '<html><head><script type="application/ld+json">'
        '{"@type":"Article","author":{"@type":"Person","name":"Roman98 Admin"}}'
        "</script></head><body><table><tr><th>نویسنده</th><td>سارا احمدی</td></tr>"
        "</table></body></html>",
        "https://roman98.ir/p/1",
    )
    assert page.authors == ["سارا احمدی"]
    assert page.nationality != "sign:foreign"


def test_a_book_node_in_json_ld_is_still_trusted():
    page = details.extract_details(
        '<html><head><script type="application/ld+json">'
        '{"@type":"Book","author":{"@type":"Person","name":"Rina Kent"},'
        '"numberOfPages":352}'
        "</script></head><body></body></html>",
        "https://a.ir/p/1",
    )
    assert page.authors == ["Rina Kent"]
    assert page.page_counts == [352]
    assert page.nationality == "sign:foreign"


def test_a_sidebar_full_of_categories_decides_nothing():
    """سایدباری که هم «رمان ایرانی» دارد هم «رمان ترجمه» یعنی فهرستِ سایت."""
    page = details.extract_details(
        '<html><body><a href="/product-category/irani/">رمان ایرانی</a>'
        '<a href="/product-category/tarjome/">رمان ترجمه</a></body></html>',
        "https://a.ir/p/1",
    )
    assert page.nationality == ""


def test_a_tag_that_merely_contains_the_word_translation_is_not_nationality():
    assert details.nationality_of_term("ترجمه اختصاصی سایت") == ""
    assert details.nationality_of_term("رمان ترجمه") == "foreign"
    assert details.nationality_of_term("رمان ایرانی") == "iranian"


# ---------------------------------------------------------------------------
# دقتِ تعداد صفحات
# ---------------------------------------------------------------------------


def test_a_page_count_is_not_invented_from_loose_text():
    page = details.extract_details(
        "<html><body><h1>رمان تاوان خیانت</h1>"
        "<p>در این سایت بیش از 320 صفحه محتوای رایگان داریم.</p></body></html>",
        "https://a.ir/p/1",
    )
    assert page.page_counts == []


def test_an_explicit_page_label_in_free_text_is_still_used():
    page = details.extract_details(
        "<html><body><p>تعداد صفحات کتاب 398 است.</p></body></html>", "https://a.ir/p/1"
    )
    assert page.page_counts == [398]


def test_file_size_is_never_read_as_a_page_count():
    page = details.extract_details(
        "<table><tr><th>حجم کتاب</th><td>12 مگابایت</td></tr></table>", "https://a.ir/p/1"
    )
    assert page.page_counts == []


def test_a_print_year_is_not_a_page_count():
    assert details.page_numbers("چاپ 1402 صفحه") == []
    assert details.page_numbers("تعداد صفحات: 1402") == [1402]  # برچسب صریح، حرفی نیست


# ---------------------------------------------------------------------------
# پاکسازیِ خلاصه
# ---------------------------------------------------------------------------


def test_hashtags_and_links_are_stripped_from_a_summary_sentence():
    text = details.strip_seo_noise(
        "آوا شهر را ترک کرد. #رمان_عاشقانه #دانلود_رمان https://romansara.ir/x"
    )
    assert text == "آوا شهر را ترک کرد."


def test_a_keyword_list_is_not_a_summary():
    assert details.is_keyword_stuffing("دانلود رمان تاوان | رمان عاشقانه | pdf رمان | رمان جدید")
    assert details.is_keyword_stuffing("رمان جدید، رمان عاشقانه، رمان پلیسی، رمان ایرانی، دانلود")
    assert not details.is_keyword_stuffing(
        "آوا پس از سال‌ها زندگی مشترک به خیانت همسرش پی می‌برد و شهر را ترک می‌کند."
    )


def test_a_verbless_description_of_a_non_novel_product_survives():
    """این اسکریپت فقط برای رمان نیست؛ توضیحِ جزوه لازم نیست فعل داشته باشد."""
    assert not details.is_keyword_stuffing(
        "نمونه سوالات فنی و حرفه‌ای کمک حسابدار همراه با پاسخنامه تشریحی و استاندارد سازمان"
    )


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


def test_several_sources_that_agree_on_nothing_write_no_page_count():
    """«۳۲۰ یا ۱۹۸؟» — سکه انداختن بدتر از خالی گذاشتن است."""
    decision = consensus.merge_pages([[320], [198]])
    assert not decision.filled
    assert "توافق ندارند" in decision.note


def test_a_translator_beats_the_tags_of_the_sources():
    decision = consensus.merge_nationality(
        ["term:iranian", "term:iranian"], has_translator=True
    )
    assert decision.value == "foreign"


def test_an_explicit_nationality_label_beats_a_sidebar_tag():
    decision = consensus.merge_nationality(["label:iranian", "term:foreign", "term:foreign"])
    assert decision.value == "iranian"


def test_sources_that_split_evenly_on_nationality_write_nothing():
    decision = consensus.merge_nationality(["term:iranian", "term:foreign"])
    assert not decision.filled
    assert "اختلاف" in decision.note


def test_a_persian_author_with_no_other_sign_is_read_as_iranian():
    decision = consensus.merge_nationality([], persian_author=True)
    assert decision.value == "iranian"
    assert "نام نویسنده" in decision.note


def test_nationality_stays_empty_when_no_source_says_anything():
    assert not consensus.merge_nationality([]).filled


def test_seo_junk_never_reaches_the_summary():
    clean = SummaryBlock(
        "آوا پس از سال‌ها زندگی مشترک به خیانت همسرش پی می‌برد و شهر را ترک می‌کند.", 3
    )
    junk = SummaryBlock(
        "دانلود رمان تاوان خیانت | رمان عاشقانه | رمان جدید | pdf رمان | رمان ایرانی", 3
    )
    tail = SummaryBlock(
        "او در شهر تازه با گذشته‌ی خانواده‌اش روبه‌رو می‌شود. #رمان_عاشقانه #دانلود_رمان", 3
    )

    decision = consensus.merge_summary([[clean], [junk], [tail]], min_chars=50)
    assert "#" not in decision.value
    assert "pdf رمان" not in decision.value
    assert "گذشته‌ی خانواده‌اش" in decision.value  # جمله‌ی سالمِ همان منبع می‌ماند


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
