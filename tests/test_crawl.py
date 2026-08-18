"""تست فاز ۱ — استخراج، sitemap، فیلتر URL و تشخیص موضوعی."""

from __future__ import annotations

import pytest

from content_pipeline.core import db, extract
from content_pipeline.core.config import SiteConfig, load_config
from content_pipeline.core.embeddings import HashingEncoder, TopicMatcher, cosine
from content_pipeline.core.http import FetchResult
from content_pipeline.phases import p1_crawl

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.ir/product/alef</loc></url>
  <url><loc>https://shop.ir/product/be</loc></url>
  <url><loc>https://shop.ir/blog/post</loc></url>
</urlset>"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://shop.ir/sitemap-products.xml</loc></sitemap>
</sitemapindex>"""


def page(title: str, product: bool = True) -> str:
    """صفحه‌ی نمونه. پیش‌فرض یک صفحه‌ی محصول واقعی است (قیمت + سبد خرید)،
    چون فاز ۱ عمداً هر چیزی جز صفحه‌ی محصول را رد می‌کند."""
    body = f"<h1>{title}</h1>"
    if product:
        body += '<span class="price">۸۵,۰۰۰ تومان</span><button class="add-to-cart">افزودن به سبد خرید</button>'
    return f"<html><head><title>سایت</title></head><body>{body}</body></html>"


# ---------------------------------------------------------------------------
# استخراج
# ---------------------------------------------------------------------------


def test_parse_sitemap_urls():
    urls, nested = extract.parse_sitemap(SITEMAP)
    assert len(urls) == 3 and not nested


def test_parse_sitemap_index():
    urls, nested = extract.parse_sitemap(SITEMAP_INDEX)
    assert not urls and nested == ["https://shop.ir/sitemap-products.xml"]


def test_title_precedence_h1_then_og_then_title():
    assert extract.extract_title(page("رمان الف")) == "رمان الف"
    og = '<html><head><meta property="og:title" content="رمان ب"><title>سایت</title></head></html>'
    assert extract.extract_title(og) == "رمان ب"
    assert extract.extract_title("<html><head><title>فقط تایتل</title></head></html>") == "فقط تایتل"


def test_links_are_absolute_and_deduplicated():
    html = '<a href="/a">a</a><a href="/a">a</a><a href="https://x.ir/b">b</a><a href="#x">c</a>'
    links = extract.extract_links(html, "https://shop.ir/page")
    assert "https://shop.ir/a" in links
    assert "https://x.ir/b" in links
    assert len(links) == 2


def test_same_domain_matches_subdomains():
    assert extract.same_domain("https://www.shop.ir/x", "www.shop.ir")
    assert not extract.same_domain("https://other.ir/x", "shop.ir")


def test_content_metrics_and_score():
    html = "<article>" + ("کلمه " * 500) + "<h2>الف</h2><h3>ب</h3><table></table><ul></ul><img></article>"
    metrics = extract.extract_content(html, "https://shop.ir/x")
    assert metrics.word_count >= 400
    assert metrics.h2_count == 1 and metrics.h3_count == 1
    assert metrics.has_table and metrics.has_list
    assert metrics.score() > 3


def test_url_filtering_respects_include_and_exclude():
    site = SiteConfig(
        domain="shop.ir",
        base_url="https://shop.ir",
        product_url_include=[r"/product/"],
        product_url_exclude=[r"/product/be$"],
    )
    urls = ["https://shop.ir/product/alef", "https://shop.ir/product/be", "https://shop.ir/blog/x"]
    assert p1_crawl._filter_urls(urls, site) == ["https://shop.ir/product/alef"]


# ---------------------------------------------------------------------------
# تشخیص موضوعی
# ---------------------------------------------------------------------------


def test_topic_matcher_ranks_relevant_titles_higher():
    matcher = TopicMatcher.build(
        HashingEncoder(), "رمان", ["رمان تاوان خیانت", "رمان عاشقانه ایرانی"]
    )
    relevant = matcher.score("رمان تاوان خیانت آوا")
    irrelevant = matcher.score("قیمت لوله پلی اتیلن صنعتی")
    assert relevant > irrelevant


def test_cosine_bounds():
    encoder = HashingEncoder()
    a, b = encoder.encode(["رمان الف", "رمان الف"])
    assert cosine(a, b) == pytest.approx(1.0, abs=1e-6)
    assert 0.0 <= cosine(*encoder.encode(["الف", "کاملاً متفاوت"])) <= 1.0


# ---------------------------------------------------------------------------
# اجرای فاز ۱ با گیرنده‌ی ساختگی
# ---------------------------------------------------------------------------


class FakeRobots:
    def sitemaps(self, base):
        return ["https://shop.ir/sitemap.xml"]

    def allowed(self, url):
        return True


class FakeFetcher:
    backend = "fake"

    def __init__(self, pages):
        self.pages = pages
        self.robots = FakeRobots()
        self.requested: list[str] = []

    def fetch(self, url, allow_browser=True, force_browser=False):
        self.requested.append(url)
        body = self.pages.get(url)
        if body is None:
            return FetchResult(url, 404, "", via="fake")
        return FetchResult(url, 200, body, url, "fake")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "runs.db")
    run_id = db.create_run(conn, "رمان", run_id="r1")
    config = load_config(None)
    config.raw["run"] = {
        "target_topic": "رمان",
        "topic_examples": ["رمان تاوان خیانت", "رمان عاشقانه"],
        "relevance_threshold": 0.5,
        "review_threshold": 0.35,
    }
    config.raw["sites"] = [
        {
            "url": "https://shop.ir",
            "domain": "shop.ir",
            "product_url_include": ["/product/"],
        }
    ]
    config.raw["database"]["path"] = str(tmp_path / "runs.db")
    yield conn, run_id, config
    conn.close()


def test_phase1_stores_titles_and_scores(env):
    conn, run_id, config = env
    fetcher = FakeFetcher(
        {
            "https://shop.ir/sitemap.xml": SITEMAP,
            "https://shop.ir/product/alef": page("رمان تاوان خیانت آوا"),
            "https://shop.ir/product/be": page("قیمت لوله پلی اتیلن صنعتی"),
        }
    )
    matcher = TopicMatcher.build(
        HashingEncoder(), config.target_topic, config.topic_examples
    )
    stats = p1_crawl.run(conn, run_id, config, fetcher, matcher, verbose=False)

    assert stats.titles_found == 2
    # صفحه‌ی بلاگ به‌خاطر فیلتر include اصلاً درخواست نشد
    assert "https://shop.ir/blog/post" not in fetcher.requested

    rows = conn.execute(
        "SELECT raw_title, normalized_title, topic_score, is_relevant FROM raw_products"
        " WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    assert rows[0]["normalized_title"] == "رمان تاوان خیانت آوا"
    assert rows[0]["topic_score"] > rows[1]["topic_score"]


def test_phase1_is_idempotent(env):
    conn, run_id, config = env
    pages = {
        "https://shop.ir/sitemap.xml": SITEMAP,
        "https://shop.ir/product/alef": page("رمان تاوان خیانت آوا"),
        "https://shop.ir/product/be": page("رمان تاوان خیانت مهسا"),
    }
    matcher = TopicMatcher.build(HashingEncoder(), "رمان", ["رمان تاوان خیانت"])
    p1_crawl.run(conn, run_id, config, FakeFetcher(pages), matcher, verbose=False)
    p1_crawl.run(conn, run_id, config, FakeFetcher(pages), matcher, verbose=False)
    assert conn.execute("SELECT COUNT(*) c FROM raw_products").fetchone()["c"] == 2


def test_borderline_titles_are_exported_for_manual_review(env, tmp_path):
    conn, run_id, config = env
    config.raw["run"]["relevance_threshold"] = 0.99
    config.raw["run"]["review_threshold"] = 0.01
    pages = {
        "https://shop.ir/sitemap.xml": SITEMAP,
        "https://shop.ir/product/alef": page("رمان تاوان خیانت آوا"),
    }
    matcher = TopicMatcher.build(HashingEncoder(), "رمان", ["رمان تاوان خیانت"])
    p1_crawl.run(conn, run_id, config, FakeFetcher(pages), matcher, verbose=False)

    path = p1_crawl.export_borderline(conn, run_id, config)
    assert path and "borderline" in path
    assert "رمان تاوان خیانت آوا" in open(path, encoding="utf-8-sig").read()


# ---------------------------------------------------------------------------
# حذف نام سایت از <title>
# ---------------------------------------------------------------------------


def test_site_name_is_stripped_using_the_domain_label():
    html = "<html><head><title>رمان تاوان خیانت | ketabshop</title></head></html>"
    assert extract.extract_title(html, None, "ketabshop.ir") == "رمان تاوان خیانت"


def test_site_name_can_be_declared_in_config():
    html = "<html><head><title>رمان تاوان خیانت | فروشگاه الف</title></head></html>"
    assert (
        extract.extract_title(html, None, "unrelated.ir", "فروشگاه الف")
        == "رمان تاوان خیانت"
    )


def test_site_name_stripped_using_og_site_name():
    html = (
        '<html><head><meta property="og:site_name" content="کتابخانه ب">'
        "<title>جزوه ریاضی - کتابخانه ب</title></head></html>"
    )
    assert extract.extract_title(html) == "جزوه ریاضی"


def test_title_without_separator_is_left_alone():
    html = "<html><head><title>رمان تاوان خیانت</title></head></html>"
    assert extract.extract_title(html, None, "shop.ir") == "رمان تاوان خیانت"


def test_unidentified_site_name_leaves_the_title_intact():
    """محافظه‌کارانه: حذف کورکورانه می‌تواند توکن تمایزدهنده را نابود کند."""
    title = "رمان تاوان خیانت - جلد دوم"
    assert extract.strip_site_name(title, "", "unrelated.ir") == title


def test_h1_wins_over_title_so_no_stripping_needed():
    html = "<html><head><title>هرچیزی | سایت</title></head><body><h1>عنوان اصلی</h1></body></html>"
    assert extract.extract_title(html, None, "site.ir") == "عنوان اصلی"


# ---------------------------------------------------------------------------
# قالب ساده‌ی sites در config
# ---------------------------------------------------------------------------


def test_plain_url_site_entry_is_supported():
    from content_pipeline.core.config import Config, DEFAULTS

    config = Config(raw={**DEFAULTS, "sites": ["https://example1.ir", "example2.ir"]})
    sites = config.sites
    assert [s.domain for s in sites] == ["example1.ir", "example2.ir"]
    assert sites[1].base_url == "https://example2.ir"


# ---------------------------------------------------------------------------
# فقط صفحه‌ی محصول
# ---------------------------------------------------------------------------

WOO = ('<html><body class="single-product"><h1>رمان شب سرد</h1>'
       '<span class="price">۸۵,۰۰۰ تومان</span>'
       '<button class="add-to-cart">افزودن به سبد خرید</button></body></html>')
JSONLD = '<script type="application/ld+json">{"@type":"Product","name":"رمان"}</script><h1>رمان</h1>'
ABOUT = "<html><body><h1>درباره ما</h1><p>ما بهترین قیمت را داریم</p></body></html>"
BLOG = ('<script type="application/ld+json">{"@type":"BlogPosting"}</script>'
        "<h1>راهنمای خرید رمان</h1><p>قیمت رمان از ۵۰۰۰۰ تومان</p>")


def test_woocommerce_product_is_recognised():
    assert extract.is_product_page(WOO, "https://shop.ir/product/1").is_product


def test_structured_data_alone_is_enough():
    signals = extract.is_product_page(JSONLD, "https://shop.ir/x/1")
    assert signals.is_product and "ساخت‌یافته" in signals.why


def test_about_page_is_not_a_product():
    signals = extract.is_product_page(ABOUT, "https://shop.ir/about-us")
    assert not signals.is_product


def test_blog_post_is_not_a_product_even_with_a_price_in_the_text():
    """پست بلاگ درباره‌ی قیمت‌ها، محصول نیست."""
    assert not extract.is_product_page(BLOG, "https://shop.ir/blog/guide").is_product


def test_price_plus_cart_is_enough_without_structured_data():
    html = "<h1>رمان</h1><p>قیمت: ۵۰۰۰۰ تومان</p><a>افزودن به سبد خرید</a>"
    assert extract.is_product_page(html, "https://shop.ir/p/9").is_product


def test_phase1_skips_pages_that_are_not_products(env):
    conn, run_id, config = env
    config.raw["sites"] = [{"url": "https://shop.ir", "domain": "shop.ir"}]
    fetcher = FakeFetcher(
        {
            "https://shop.ir/sitemap.xml": (
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://shop.ir/product/1</loc></url>"
                "<url><loc>https://shop.ir/about-us</loc></url>"
                "<url><loc>https://shop.ir/blog/guide</loc></url></urlset>"
            ),
            "https://shop.ir/product/1": WOO,
            "https://shop.ir/about-us": ABOUT,
            "https://shop.ir/blog/guide": BLOG,
        }
    )
    matcher = TopicMatcher.build(HashingEncoder(), "رمان", ["رمان شب سرد"])

    stats = p1_crawl.run(conn, run_id, config, fetcher, matcher, verbose=False)

    assert stats.titles_found == 1
    assert stats.not_product == 2
    titles = [r["raw_title"] for r in conn.execute("SELECT raw_title FROM raw_products")]
    assert titles == ["رمان شب سرد"]


def test_the_filter_can_be_turned_off(env):
    conn, run_id, config = env
    config.raw["sites"] = [{"url": "https://shop.ir", "domain": "shop.ir"}]
    config.raw["crawl"]["require_product_signals"] = False
    fetcher = FakeFetcher(
        {
            "https://shop.ir/sitemap.xml": (
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://shop.ir/about-us</loc></url></urlset>"
            ),
            "https://shop.ir/about-us": ABOUT,
        }
    )
    matcher = TopicMatcher.build(HashingEncoder(), "رمان", ["رمان شب سرد"])

    stats = p1_crawl.run(conn, run_id, config, fetcher, matcher, verbose=False)
    assert stats.titles_found == 1


# ---------------------------------------------------------------------------
# آدرس صفحه‌ی نتایج به‌جای کل سایت
# ---------------------------------------------------------------------------


def test_a_search_url_is_treated_as_a_listing():
    assert not p1_crawl.is_listing_seed("https://shop.ir")
    assert not p1_crawl.is_listing_seed("https://shop.ir/")
    assert p1_crawl.is_listing_seed("https://shop.ir/?s=رمان")
    assert p1_crawl.is_listing_seed("https://shop.ir/category/roman/")


def test_listing_seed_follows_pagination_and_ignores_the_sitemap(env):
    conn, run_id, config = env
    config.raw["sites"] = [{"url": "https://shop.ir/?s=رمان", "domain": "shop.ir"}]
    fetcher = FakeFetcher(
        {
            "https://shop.ir/?s=رمان": (
                '<a href="/product/1">۱</a><a href="/about-us">درباره</a>'
                '<a class="next" href="/?s=رمان&page=2">بعدی</a>'
            ),
            "https://shop.ir/?s=رمان&page=2": '<a href="/product/2">۲</a>',
            "https://shop.ir/product/1": WOO,
            "https://shop.ir/product/2": WOO.replace("شب سرد", "شب گرم"),
            "https://shop.ir/about-us": ABOUT,
        }
    )
    matcher = TopicMatcher.build(HashingEncoder(), "رمان", ["رمان شب سرد"])

    stats = p1_crawl.run(conn, run_id, config, fetcher, matcher, verbose=False)

    assert "https://shop.ir/sitemap.xml" not in fetcher.requested
    assert stats.titles_found == 2       # هر دو صفحه‌ی فهرست دیده شدند
    assert stats.not_product == 1        # «درباره ما» رد شد


# ---------------------------------------------------------------------------
# سقف و لغو
# ---------------------------------------------------------------------------


def _many_products(count):
    pages = {
        "https://shop.ir/sitemap.xml": (
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>https://shop.ir/product/{i}</loc></url>" for i in range(count))
            + "</urlset>"
        )
    }
    for index in range(count):
        pages[f"https://shop.ir/product/{index}"] = WOO.replace("شب سرد", f"شماره {index}")
    return pages


def test_per_site_cap_stops_after_the_requested_count(env):
    """«فعلاً ده تا بگیر تا ببینم درست کار می‌کند»."""
    conn, run_id, config = env
    config.raw["sites"] = [{"url": "https://shop.ir", "domain": "shop.ir"}]
    db.set_run_plan(conn, run_id, options={"max_products_per_site": 3})
    fetcher = FakeFetcher(_many_products(20))
    matcher = TopicMatcher.build(HashingEncoder(), "رمان", ["رمان شب سرد"])

    stats = p1_crawl.run(conn, run_id, config, fetcher, matcher, verbose=False)

    assert stats.titles_found == 3
    assert stats.pages_fetched < 20  # بقیه اصلاً دانلود نشدند


def test_cancel_stops_the_crawl_in_the_middle(env):
    """لغو باید وسط فاز اثر کند، نه بعد از تمام شدنش."""
    conn, run_id, config = env
    config.raw["sites"] = [{"url": "https://shop.ir", "domain": "shop.ir"}]
    fetcher = FakeFetcher(_many_products(30))
    matcher = TopicMatcher.build(HashingEncoder(), "رمان", ["رمان شب سرد"])
    seen = {"count": 0}

    def should_stop():
        seen["count"] += 1
        return seen["count"] > 5

    stats = p1_crawl.run(
        conn, run_id, config, fetcher, matcher, verbose=False, should_stop=should_stop
    )

    assert stats.stopped
    assert stats.titles_found < 30
    assert "متوقف" in stats.render()
