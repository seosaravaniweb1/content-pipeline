"""تست موتور تکمیل و پنل — بدون شبکه و بدون حساب گوگل.

صفحه‌های منبع ثابت‌اند، ``Fetcher`` ساختگی است و شیت یک فایل csv موقتی.
"""

from __future__ import annotations

import csv
import json
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request
import zlib

import pytest

from sheet_filler.core import db, filler, fields, gsheet, normalizer, sources
from sheet_filler.core.config import load_config
from sheet_filler.web import server as web_server

# ---------------------------------------------------------------------------
# صفحه‌های نمونه
# ---------------------------------------------------------------------------

NOVEL_PAGE = """<html><head>
<title>دانلود رمان تاوان خیانت | فروشگاه الف</title>
<meta property="og:image" content="/up/tavan-600x600.jpg">
</head><body>
<h1>دانلود رمان تاوان خیانت</h1>
<table>
  <tr><th>نویسنده</th><td>آوا محمدی</td></tr>
  <tr><th>تعداد صفحات</th><td>۳۹۸</td></tr>
</table>
<a href="/product-tag/asheghane/" rel="tag">رمان عاشقانه</a>
<h2>خلاصه رمان</h2>
<p>آوا پس از سال‌ها زندگی مشترک به خیانت همسرش پی می‌برد و تصمیم می‌گیرد شهر را
ترک کند تا زندگی تازه‌ای بسازد.</p>
</body></html>"""

EXAM_PAGE = """<html><body>
<h1>نمونه سوالات فنی حرفه‌ای کمک حسابدار</h1>
<table>
  <tr><th>تعداد سوالات</th><td>۲۴۰</td></tr>
  <tr><th>کد رایانه</th><td>۱۲۳۴۵۶</td></tr>
  <tr><th>تعداد صفحه</th><td>۸۰</td></tr>
  <tr><th>جزوه همراه</th><td>دارد</td></tr>
</table>
<a href="/product-category/fani/">نمونه سوال فنی حرفه‌ای</a>
</body></html>"""

NOVEL_HEADER = [
    "عنوان", "keyword", "Author", "Summary", "Categories", "Tags",
    "Nationality", "Format", "Translator", "Pages", "Image", "Status", "Product ID",
]
EXAM_HEADER = [
    "عنوان", "کلمه کلیدی", "تعداد سوالات", "کد رایانه", "تعداد صفحه",
    "جزوه همراه", "دسته بندی", "تصویر", "وضعیت", "Product ID",
]


def png(width: int, height: int) -> bytes:
    header = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    chunk = struct.pack(">I", len(header)) + b"IHDR" + header
    chunk += struct.pack(">I", zlib.crc32(b"IHDR" + header))
    return b"\x89PNG\r\n\x1a\n" + chunk


class FakeFetcher:
    """جایگزین ``Fetcher``: صفحه‌های ثابت، بدون شبکه."""

    def __init__(self, pages: dict[str, str], blobs: dict[str, bytes] | None = None) -> None:
        self.pages = pages
        self.blobs = blobs or {}
        self.fetched: list[str] = []

    def fetch(self, url, allow_browser=True, force_browser=False):
        from sheet_filler.core.http import FetchResult

        self.fetched.append(url)
        html = self.pages.get(url, "")
        return FetchResult(url, 200 if html else 404, html, url, "fake")

    def fetch_bytes(self, url, max_bytes=0):
        data = self.blobs.get(url)
        return (200, data) if data else (404, b"")


@pytest.fixture
def env(tmp_path):
    config = load_config(None)
    config.raw["database"]["path"] = str(tmp_path / "filler.db")
    conn = db.connect(config.db_path)
    yield conn, config
    conn.close()


def sheet_file(tmp_path, header, rows, name="sheet.csv"):
    path = tmp_path / name
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row + [""] * (len(header) - len(row)))
    return path


def fill(conn, config, path, fetcher, **overrides):
    settings = {"file": str(path), "report_tab": "", **overrides}
    options = filler.options_from_config(config, settings)
    document = gsheet.FileDocument(path, options.sheet)
    stats = filler.run(conn, config, options, fetcher, document=document, log=lambda _: None)
    return stats, document, options


def link(conn, title, url):
    with db.transaction(conn):
        db.add_source_link(conn, normalizer.normalize(title), url)


# ---------------------------------------------------------------------------
# پر کردن شیت
# ---------------------------------------------------------------------------


def test_a_novel_sheet_is_filled_from_the_source_page(env, tmp_path):
    conn, config = env
    title = "دانلود رمان تاوان خیانت"
    link(conn, title, "https://a.ir/p/1")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[title]])
    fetcher = FakeFetcher(
        {"https://a.ir/p/1": NOVEL_PAGE}, {"https://a.ir/up/tavan-600x600.jpg": png(600, 600)}
    )

    stats, document, _ = fill(conn, config, path, fetcher, search={"enabled": False})
    assert stats.processed == 1 and stats.no_source == 0
    row = document.values[1]
    assert row[1] == "رمان تاوان خیانت"      # کلمه‌ی کلیدی
    assert row[2] == "آوا محمدی"              # نویسنده
    assert row[9] == "398"                     # تعداد صفحات
    assert row[10].endswith("tavan-600x600.jpg")
    assert row[11] == "" and row[12] == ""    # وضعیت و شناسه محصول دست‌نخورده


def test_an_exam_sheet_is_filled_although_no_code_knows_its_columns(env, tmp_path):
    conn, config = env
    title = "نمونه سوالات فنی حرفه‌ای کمک حسابدار"
    link(conn, title, "https://a.ir/p/2")
    path = sheet_file(tmp_path, EXAM_HEADER, [[title]], name="exam.csv")

    stats, document, options = fill(
        conn, config, path, FakeFetcher({"https://a.ir/p/2": EXAM_PAGE}), search={"enabled": False}
    )
    assert stats.processed == 1
    row = document.values[1]
    assert row[2] == "240"        # تعداد سوالات
    assert row[3] == "123456"     # کد رایانه، با رقم لاتین
    assert row[4] == "80"         # تعداد صفحه
    assert row[5] == "دارد"       # جزوه همراه
    assert row[8] == "" and row[9] == ""  # وضعیت و شناسه محصول

    stored = db.row_values(db.rows_of(conn, options.key)[0])
    assert stored["questions"] == "240"


def test_filled_cells_are_never_overwritten(env, tmp_path):
    conn, config = env
    title = "دانلود رمان تاوان خیانت"
    link(conn, title, "https://a.ir/p/1")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[title, "کلمه دستی من", "نویسنده دستی"]])

    _, document, _ = fill(
        conn, config, path, FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE}), search={"enabled": False}
    )
    assert document.values[1][1] == "کلمه دستی من"
    assert document.values[1][2] == "نویسنده دستی"
    assert document.values[1][9] == "398"  # ستون خالی پر شده است


FILLED_NOVEL = [
    "دانلود رمان تاوان خیانت", "رمان تاوان خیانت", "آوا محمدی", "خلاصه‌ی دستی من",
    "رمان عاشقانه", "عاشقانه", "ایرانی", "PDF", "", "398", "",
]


def test_a_row_that_is_already_filled_is_not_worked_again(env, tmp_path):
    """ردیفی که فقط ستون‌های اختیاری‌اش (تصویر، مترجم) خالی است، رد می‌شود."""
    conn, config = env
    link(conn, FILLED_NOVEL[0], "https://a.ir/p/1")
    path = sheet_file(tmp_path, NOVEL_HEADER, [FILLED_NOVEL])
    fetcher = FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE})

    stats, document, _ = fill(conn, config, path, fetcher, search={"enabled": False})
    assert stats.already_filled == 1
    assert stats.queued == 0 and stats.processed == 0
    assert fetcher.fetched == []                       # حتی یک صفحه هم دانلود نشد
    assert document.values[1] == FILLED_NOVEL + ["", ""]


def test_an_untouched_row_is_queued_even_when_every_column_is_optional(env, tmp_path):
    conn, config = env
    title = "دانلود رمان تاوان خیانت"
    link(conn, title, "https://a.ir/p/1")
    path = sheet_file(tmp_path, ["عنوان", "Translator", "Image"], [[title]], name="thin.csv")

    stats, _, _ = fill(
        conn, config, path, FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE}), search={"enabled": False}
    )
    assert stats.already_filled == 0 and stats.processed == 1


def test_with_images_off_the_image_column_is_left_alone(env, tmp_path):
    """پیدا نشدن کاور نباید جلوی پر شدن بقیه‌ی ستون‌ها را بگیرد."""
    conn, config = env
    title = "دانلود رمان تاوان خیانت"
    link(conn, title, "https://a.ir/p/1")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[title]])
    fetcher = FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE})

    stats, document, options = fill(
        conn, config, path, fetcher, search={"enabled": False}, image=False
    )
    assert stats.processed == 1
    assert document.values[1][2] == "آوا محمدی"        # بقیه پر شده
    assert document.values[1][10] == ""                 # ستون تصویر دست‌نخورده
    assert "Image" not in (db.rows_of(conn, options.key)[0]["note"] or "")
    assert "tavan-600x600" not in " ".join(fetcher.fetched)  # سراغ کاور هم نرفت


def test_a_missing_cover_never_marks_a_row_incomplete(env, tmp_path):
    conn, config = env
    title = "دانلود رمان تاوان خیانت"
    link(conn, title, "https://a.ir/p/1")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[title]])
    # تصویر صفحه دانلود می‌شود ولی مربع نیست: هیچ کاوری انتخاب نمی‌شود
    fetcher = FakeFetcher(
        {"https://a.ir/p/1": NOVEL_PAGE}, {"https://a.ir/up/tavan-600x600.jpg": png(600, 200)}
    )

    stats, document, options = fill(conn, config, path, fetcher, search={"enabled": False})
    assert stats.processed == 1
    assert document.values[1][10] == ""
    # نبودِ کاور در دلیلِ «ناقص» بودن ردیف نمی‌آید
    assert "Image" not in (db.rows_of(conn, options.key)[0]["note"] or "")


def test_overwrite_always_replaces_the_existing_value(env, tmp_path):
    conn, config = env
    title = "دانلود رمان تاوان خیانت"
    link(conn, title, "https://a.ir/p/1")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[title, "کلمه دستی من"]])

    _, document, _ = fill(
        conn,
        config,
        path,
        FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE}),
        search={"enabled": False},
        overwrite="always",
    )
    assert document.values[1][1] == "رمان تاوان خیانت"


def test_one_exploding_row_does_not_take_the_run_down(env, tmp_path, monkeypatch):
    """در ۱۵ هزار ردیف حتماً یکی می‌ترکد؛ بقیه باید پر شوند."""
    conn, config = env
    good, bad = "دانلود رمان تاوان خیانت", "رمان مسئله‌دار"
    link(conn, good, "https://a.ir/p/1")
    link(conn, bad, "https://a.ir/p/boom")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[bad], [good]])

    real = sources.collect

    def explode(conn_, title, *args, **kwargs):
        if title == bad:
            raise RuntimeError("صفحه‌ی خراب")
        return real(conn_, title, *args, **kwargs)

    monkeypatch.setattr(sources, "collect", explode)
    fetcher = FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE, "https://a.ir/p/boom": NOVEL_PAGE})

    stats, document, options = fill(conn, config, path, fetcher, search={"enabled": False})
    assert stats.failed == 1
    assert document.values[2][2] == "آوا محمدی"      # ردیف سالم پر شده
    rows = {row["title"]: row for row in db.rows_of(conn, options.key)}
    assert "خطا" in (rows[bad]["note"] or "")


def test_overwrite_mine_rebuilds_its_own_cells_but_not_yours(env, tmp_path):
    """قواعد که دقیق‌تر شدند، ردیف‌های قبلی از نو ساخته می‌شوند — بی‌آنکه
    چیزی که خودتان نوشته‌اید عوض شود."""
    conn, config = env
    title = "دانلود رمان تاوان خیانت"
    link(conn, title, "https://a.ir/p/1")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[title]])
    fetcher = FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE})

    _, document, options = fill(conn, config, path, fetcher, search={"enabled": False})
    assert document.values[1][2] == "آوا محمدی"     # نوشته‌ی خودِ برنامه

    # شیت بعد از اجرای اول، به‌علاوه‌ی یک دست‌نوشته‌ی کاربر در ستون خلاصه
    filled = list(document.values[1])
    filled[3] = "خلاصه‌ای که خودم نوشتم"
    sheet_file(tmp_path, NOVEL_HEADER, [filled], name=path.name)  # همان شیت، پرشده

    # منبع عوض شده: اجرا با «mine» باید سلولِ خودش را از نو بسازد
    db.clear_pages(conn)
    changed = NOVEL_PAGE.replace("آوا محمدی", "سارا احمدی")
    _, document, _ = fill(
        conn,
        config,
        path,
        FakeFetcher({"https://a.ir/p/1": changed}),
        search={"enabled": False},
        overwrite="mine",
    )
    assert document.values[1][2] == "سارا احمدی"
    assert document.values[1][3] == "خلاصه‌ای که خودم نوشتم"


def test_a_title_with_no_source_is_reported_not_invented(env, tmp_path):
    conn, config = env
    path = sheet_file(tmp_path, NOVEL_HEADER, [["رمان بدون منبع"]])
    stats, document, options = fill(
        conn, config, path, FakeFetcher({}), search={"enabled": False}
    )
    assert stats.no_source == 1
    assert document.values[1][1] == ""  # هیچ حدسی نوشته نشده
    assert db.rows_of(conn, options.key)[0]["status"] == db.PARTIAL


def test_a_page_about_another_product_is_not_used(env, tmp_path):
    conn, config = env
    title = "دانلود رمان تاوان خیانت"
    link(conn, title, "https://a.ir/other")
    other = NOVEL_PAGE.replace("تاوان خیانت", "شب سرد")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[title]])
    stats, _, _ = fill(
        conn, config, path, FakeFetcher({"https://a.ir/other": other}), search={"enabled": False}
    )
    assert stats.no_source == 1


def test_the_row_cap_leaves_the_rest_in_the_queue(env, tmp_path):
    conn, config = env
    title = "دانلود رمان تاوان خیانت"
    link(conn, title, "https://a.ir/p/1")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[title], ["رمان دوم"], ["رمان سوم"]])
    stats, _, options = fill(
        conn,
        config,
        path,
        FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE}),
        search={"enabled": False},
        limit=1,
    )
    assert stats.processed == 1
    assert len(db.rows_of(conn, options.key, db.PENDING)) == 2


def test_second_run_uses_the_cache_and_leaves_finished_rows_alone(env, tmp_path):
    conn, config = env
    title = "دانلود رمان تاوان خیانت"
    link(conn, title, "https://a.ir/p/1")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[title]])
    pages = {"https://a.ir/p/1": NOVEL_PAGE}

    first = FakeFetcher(pages)
    stats_one, _, _ = fill(conn, config, path, first, search={"enabled": False})
    assert stats_one.pages_fetched == 1

    # همان شیت، بار دوم: ردیفِ تمام‌شده دوباره پردازش نمی‌شود
    second = FakeFetcher(pages)
    stats_two, _, _ = fill(conn, config, path, second, search={"enabled": False})
    assert stats_two.queued == 0
    assert second.fetched == []

    # و با «صف را از نو بساز»، دوباره می‌آید ولی صفحه را از کش می‌خواند
    options = filler.options_from_config(config, {"file": str(path)})
    db.reset_rows(conn, options.key)
    third = FakeFetcher(pages)
    stats_three, _, _ = fill(conn, config, path, third, search={"enabled": False})
    assert stats_three.queued == 1
    assert stats_three.from_cache == 1 and third.fetched == []


def test_values_left_unwritten_by_a_crashed_run_are_pushed_next_time(env, tmp_path):
    conn, config = env
    title = "رمان تاوان خیانت"
    path = sheet_file(tmp_path, NOVEL_HEADER, [[title]])
    options = filler.options_from_config(config, {"file": str(path)})
    with db.transaction(conn):
        row_id = db.upsert_row(conn, options.key, title, normalizer.normalize(title), 2)
        db.save_values(conn, row_id, {"author": "آوا محمدی", "pages": "398"}, status=db.DONE)
    assert len(db.unpushed_rows(conn, options.key)) == 1

    stats, document, _ = fill(conn, config, path, FakeFetcher({}), search={"enabled": False})
    assert stats.queued == 0
    assert document.values[1][2] == "آوا محمدی"
    assert document.values[1][9] == "398"
    assert not db.unpushed_rows(conn, options.key)


def test_the_sheet_column_of_source_urls_is_used(env, tmp_path):
    """اگر آدرس منبع در خودِ شیت باشد، نه جستجویی لازم است نه ایمپورتی."""
    conn, config = env
    header = [*NOVEL_HEADER, "منابع"]
    path = sheet_file(
        tmp_path, header, [["دانلود رمان تاوان خیانت", *[""] * 12, "https://a.ir/p/1"]]
    )
    fetcher = FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE})
    stats, document, _ = fill(
        conn, config, path, fetcher, sources_column="منابع", search={"enabled": False}
    )
    assert stats.processed == 1 and stats.no_source == 0
    assert document.values[1][2] == "آوا محمدی"


def test_search_finds_the_product_page_when_no_url_is_known(env, tmp_path):
    conn, config = env
    config.raw["sites"] = [{"url": "https://a.ir", "search_url": "{base}/s?q={query}"}]
    path = sheet_file(tmp_path, NOVEL_HEADER, [["دانلود رمان تاوان خیانت"]])
    search_page = (
        '<a href="/product/tavan">رمان تاوان خیانت</a><a href="/product/x">رمان شب سرد</a>'
    )
    query = urllib.parse.quote("رمان تاوان خیانت")
    fetcher = FakeFetcher(
        {f"https://a.ir/s?q={query}": search_page, "https://a.ir/product/tavan": NOVEL_PAGE}
    )
    stats, _, _ = fill(conn, config, path, fetcher)
    assert stats.processed == 1 and stats.no_source == 0
    assert "https://a.ir/product/tavan" in fetcher.fetched
    assert "https://a.ir/product/x" not in fetcher.fetched  # عنوانش نمی‌خورد


# ---------------------------------------------------------------------------
# نگاشت «عنوان → آدرس»
# ---------------------------------------------------------------------------


def test_links_can_be_imported_from_any_file_with_a_title_and_url_column(env, tmp_path):
    conn, _ = env
    path = tmp_path / "sources.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["canonical_title", "source_urls"])
        writer.writerow(["رمان تاوان خیانت", "https://a.ir/p/1 | https://b.ir/p/2"])
    titles, links = sources.import_links(conn, path)
    assert (titles, links) == (1, 2)
    stored = db.source_links(conn, normalizer.normalize("رمان تاوان خیانت"))
    assert stored == ["https://a.ir/p/1", "https://b.ir/p/2"]


def test_a_file_without_a_url_column_is_refused_with_a_clear_message(env, tmp_path):
    conn, _ = env
    path = tmp_path / "bad.csv"
    path.write_text("ستون یک,ستون دو\nالف,ب\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="ستون عنوان یا ستون آدرس"):
        sources.import_links(conn, path)


# ---------------------------------------------------------------------------
# تنظیمات و ستون‌ها
# ---------------------------------------------------------------------------


def test_status_and_product_id_are_never_writable(env):
    _, config = env
    options = filler.options_from_config(config, {})
    keys = [spec.key for spec in options.writable(fields.build_plan(NOVEL_HEADER))]
    assert "status" not in keys and "product_id" not in keys and "title" not in keys


def test_panel_settings_override_the_config_file(env):
    _, config = env
    config.raw["fill"]["max_sources_per_title"] = 2
    assert filler.options_from_config(config, {"max_sources_per_title": 7}).max_sources_per_title == 7


def test_each_sheet_keeps_its_own_progress(env, tmp_path):
    """یک نصب، چند موضوع: هر شیت ردیف‌ها و پیشرفت خودش را دارد."""
    conn, config = env
    link(conn, "دانلود رمان تاوان خیانت", "https://a.ir/p/1")
    link(conn, "نمونه سوالات فنی حرفه‌ای کمک حسابدار", "https://a.ir/p/2")
    novel = sheet_file(tmp_path, NOVEL_HEADER, [["دانلود رمان تاوان خیانت"]], "novel.csv")
    exam = sheet_file(tmp_path, EXAM_HEADER, [["نمونه سوالات فنی حرفه‌ای کمک حسابدار"]], "exam.csv")
    fetcher = FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE, "https://a.ir/p/2": EXAM_PAGE})

    _, _, novel_options = fill(conn, config, novel, fetcher, search={"enabled": False})
    _, _, exam_options = fill(conn, config, exam, fetcher, search={"enabled": False})

    assert novel_options.key != exam_options.key
    assert db.counts_of(conn, novel_options.key)["total"] == 1
    assert db.counts_of(conn, exam_options.key)["total"] == 1
    assert {row["sheet_key"] for row in db.list_sheets(conn)} == {
        novel_options.key,
        exam_options.key,
    }


# ---------------------------------------------------------------------------
# پنل
# ---------------------------------------------------------------------------

CONFIG_TEMPLATE = """
database:
  path: {db_path}
"""


@pytest.fixture
def panel(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(db_path=(tmp_path / "filler.db").as_posix()), encoding="utf-8"
    )
    config = load_config(config_path)
    httpd, state = web_server.build_server(config, config_path, port=0, token="tok")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]

    def call(path, method="GET", body=None, token="tok", params=None):
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "X-Panel-Token": token,
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw.startswith(b"{") else raw)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, raw

    call.state = state  # type: ignore[attr-defined]
    yield call
    httpd.shutdown()
    httpd.server_close()


def test_api_requires_the_session_token(panel):
    status, payload = panel("/api/state", token="wrong")
    assert status == 403 and "توکن" in payload["error"]


def test_panel_starts_empty_and_saves_settings(panel, tmp_path):
    status, data = panel("/api/state")
    assert status == 200 and data["ready"] is False
    # فرم پنل از فهرست تنظیمات ساخته می‌شود، نه از کد جاوااسکریپت
    assert {field["key"] for field in data["fields"]} >= {"sheet_url", "sites", "auto"}

    path = sheet_file(tmp_path, EXAM_HEADER, [["نمونه سوالات کمک حسابدار"]], "exam.csv")
    status, saved = panel(
        "/api/settings",
        method="POST",
        body={"file": str(path), "max_sources_per_title": 3, "overwrite": "always"},
    )
    assert status == 200 and saved["ready"] is True
    assert saved["values"]["max_sources_per_title"] == 3

    _, again = panel("/api/state")
    assert again["values"]["file"] == str(path)  # در دیتابیس ماندگار است


def test_a_sheet_without_a_service_account_is_refused(panel):
    status, payload = panel(
        "/api/settings",
        method="POST",
        body={"sheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvW/edit"},
    )
    assert status == 400 and "سرویس‌اکانت" in payload["error"]


def test_a_broken_sheet_url_is_refused(panel):
    status, payload = panel("/api/settings", method="POST", body={"sheet_url": "https://ex.com/x"})
    assert status == 400 and "آدرس گوگل‌شیت" in payload["error"]


def test_panel_inspects_a_sheet_without_writing(panel, tmp_path):
    path = sheet_file(tmp_path, EXAM_HEADER, [["نمونه سوالات کمک حسابدار"]], "exam.csv")
    panel("/api/settings", method="POST", body={"file": str(path)})
    status, data = panel("/api/inspect", method="POST", body={})
    assert status == 200 and data["rows"] == 1
    kinds = {item["column"]: item["kind"] for item in data["plan"]}
    assert kinds["تعداد سوالات"] == "number"
    assert kinds["کد رایانه"] == "text"
    assert kinds["جزوه همراه"] == "boolean"
    assert kinds["وضعیت"] == "skip"
    # هیچ چیزی نوشته نشده است
    assert path.read_text(encoding="utf-8-sig").count("نمونه سوالات کمک حسابدار") == 1


def test_panel_imports_links_and_reports_the_total(panel, tmp_path):
    source = tmp_path / "sources.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["عنوان", "لینک"])
        writer.writerow(["رمان تاوان خیانت", "https://a.ir/p/1"])
    status, data = panel("/api/links", method="POST", body={"file": str(source)})
    assert status == 200 and data["links"] == 1
    _, state = panel("/api/state")
    assert state["links"] == 1


def test_sites_pasted_in_the_panel_are_cleaned(panel):
    status, data = panel(
        "/api/settings",
        method="POST",
        body={"sites": "shop1.ir\n  https://shop2.ir  \n\n# یادداشت\nنه-یک-آدرس"},
    )
    assert status == 200
    assert data["values"]["sites"] == ["https://shop1.ir", "https://shop2.ir"]


# ---------------------------------------------------------------------------
# اتوماتیک‌سازی
# ---------------------------------------------------------------------------


def test_a_whole_sheet_url_is_enough(env):
    """کاربر کل آدرس را کپی می‌کند؛ شناسه خودکار درمی‌آید."""
    _, config = env
    options = filler.options_from_config(
        config,
        {"sheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEfGh_IjKlMnOpQrS/edit#gid=7"},
    )
    assert options.sheet.sheet_id == "1AbCdEfGh_IjKlMnOpQrS"


def test_auto_mode_refills_the_sheet_each_round(env, tmp_path):
    """عنوانی که بعداً به شیت اضافه شود، در دور بعدی خودش پر می‌شود."""
    conn, config = env
    first_title = "دانلود رمان تاوان خیانت"
    second_title = "نمونه سوالات فنی حرفه‌ای کمک حسابدار"
    link(conn, first_title, "https://a.ir/p/1")
    link(conn, second_title, "https://a.ir/p/2")
    path = sheet_file(tmp_path, NOVEL_HEADER, [[first_title]])
    fetcher = FakeFetcher({"https://a.ir/p/1": NOVEL_PAGE, "https://a.ir/p/2": NOVEL_PAGE})
    options = filler.options_from_config(
        config,
        {"file": str(path), "report_tab": "", "auto": True, "auto_every_minutes": 1},
    )
    options.search_enabled = False

    rounds: list[int] = []

    def between_rounds(_seconds: float) -> None:
        """وسط خوابِ بین دورها، یک عنوان تازه به شیت اضافه می‌کنیم."""
        if rounds:
            return
        rounds.append(1)
        with path.open("a", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle).writerow([second_title] + [""] * (len(NOVEL_HEADER) - 1))

    stats = filler.run_auto(
        conn,
        config,
        options,
        lambda: fetcher,
        log=lambda _: None,
        sleep=between_rounds,
        rounds=2,
    )
    assert stats.processed == 2  # یکی در دور اول، یکی در دور دوم
    titles = {row["title"] for row in db.rows_of(conn, options.key)}
    assert titles == {first_title, second_title}


def test_auto_mode_stops_when_asked(env, tmp_path):
    conn, config = env
    path = sheet_file(tmp_path, NOVEL_HEADER, [["رمان الف"]])
    options = filler.options_from_config(config, {"file": str(path), "report_tab": "", "auto": True})
    options.search_enabled = False
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # بعد از شروع، «توقف» زده می‌شود

    stats = filler.run_auto(
        conn, config, options, lambda: FakeFetcher({}), log=lambda _: None, should_stop=stop
    )
    assert "دور اجرا شد" in stats.stopped_early


def test_sites_are_learned_from_imported_links(env):
    """کاربر سایتی تنظیم نکرده ولی آدرس‌ها را داده — همان دامنه‌ها منبع می‌شوند."""
    conn, config = env
    link(conn, "رمان الف", "https://shop1.ir/p/1")
    link(conn, "رمان ب", "https://shop2.ir/p/2")
    options = filler.options_from_config(config, {"file": "x.csv"})
    assert options.sites == []
    ordered = filler._ordered_sites(conn, options)
    assert {site.domain for site in ordered} == {"shop1.ir", "shop2.ir"}


def test_domains_that_worked_are_tried_first(env):
    conn, config = env
    options = filler.options_from_config(
        config, {"file": "x.csv", "sites": ["https://slow.ir", "https://good.ir"]}
    )
    with db.transaction(conn):
        for _ in range(3):
            db.note_domain(conn, "good.ir")
    assert [site.domain for site in filler._ordered_sites(conn, options)][0] == "good.ir"


def test_a_row_without_sources_is_retried_after_a_while(env, tmp_path):
    """ردیف بی‌منبع بعد از چند روز خودش دوباره امتحان می‌شود."""
    conn, config = env
    path = sheet_file(tmp_path, NOVEL_HEADER, [["رمان بدون منبع"]])
    options = filler.options_from_config(config, {"file": str(path)})
    title_key = normalizer.normalize("رمان بدون منبع")
    with db.transaction(conn):
        row_id = db.upsert_row(conn, options.key, "رمان بدون منبع", title_key, 2)
        db.save_values(conn, row_id, {}, status=db.PARTIAL, note="هیچ صفحه‌ی منبعی پیدا نشد")

    assert filler._needs_work(conn, options.key, title_key, options) is False
    # همان ردیف، ولی ثبت‌شده در گذشته
    with db.transaction(conn):
        conn.execute(
            "UPDATE rows SET updated_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", row_id)
        )
    assert filler._needs_work(conn, options.key, title_key, options) is True


def test_settings_have_one_definition(env):
    """هر تنظیمِ پنل باید در FillOptions هم شناخته شود (تعریف تکراری نداریم)."""
    from sheet_filler.core import settings as settings_module

    unknown = {
        field.key
        for field in settings_module.FIELDS
        if field.key not in filler.FillOptions.__dataclass_fields__
        and field.key not in {"sheet_url", "service_account_json", "tab", "file",
                              "title_column", "sources_column", "lists_tab",
                              "report_tab", "header_row", "image"}
    }
    assert not unknown, f"تنظیم‌های بی‌صاحب: {unknown}"
