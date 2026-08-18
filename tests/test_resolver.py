"""تست فاز ۲ — بلاک‌بندی، توکن تمایزدهنده و قانون ادغام.

سناریوی مرجع داکیومنت («آوا / مهسا / ناشناس / ۴ برادر») اینجا قفل شده است.
تا وقتی این تست‌ها سبز نشوند، سراغ فاز بعد نروید.
"""

from __future__ import annotations

import pytest

from content_pipeline.core import db, normalizer
from content_pipeline.core.config import load_config
from content_pipeline.core.entities import EntityExtractor
from content_pipeline.phases import p2_resolve
from content_pipeline.phases.p2_resolve import (
    Candidate,
    SafeClusters,
    candidate_pairs,
    classify_pair,
    pick_canonical_title,
    resolve,
)


@pytest.fixture(scope="module")
def extractor() -> EntityExtractor:
    return EntityExtractor()


def make(extractor: EntityExtractor, raw_id: int, title: str) -> Candidate:
    return Candidate(
        raw_id=raw_id,
        raw_title=title,
        normalized_title=normalizer.normalize(title),
        entity=extractor.extract(title),
    )


GOLDEN = [
    "رمان تاوان خیانت آوا",
    "دانلود رمان تاوان خیانت آوا pdf",
    "رمان تاوان خیانت مهسا",
    "رمان تاوان خیانت از ناشناس",
    "رمان تاوان خیانت ۴ برادر",
]


@pytest.fixture
def golden(extractor):
    return [make(extractor, i, t) for i, t in enumerate(GOLDEN)]


# ---------------------------------------------------------------------------
# مرحله B — توکن تمایزدهنده (جدول داکیومنت)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("رمان تاوان خیانت آوا", ["آوا"]),
        ("رمان تاوان خیانت مهسا", ["مهسا"]),
        ("رمان تاوان خیانت از ناشناس", []),
        ("رمان تاوان خیانت ۴ برادر", ["۴ برادر"]),
    ],
)
def test_entity_tokens_match_the_spec_table(extractor, title, expected):
    assert extractor.extract(title).tokens == expected


# ---------------------------------------------------------------------------
# مرحله C — قانون ادغام
# ---------------------------------------------------------------------------


def test_same_entity_tokens_merge(golden):
    verdict, score, confidence, _ = classify_pair(golden[0], golden[1])
    assert verdict == "merge"
    assert score >= 0.80
    assert confidence >= 0.9


def test_different_non_empty_entity_tokens_never_merge(golden):
    """خط قرمز: آوا در برابر مهسا."""
    verdict, _, _, reason = classify_pair(golden[0], golden[2])
    assert verdict == "no_merge"
    assert "خط قرمز" in reason


def test_number_entity_is_also_a_red_line(golden):
    assert classify_pair(golden[0], golden[4])[0] == "no_merge"


def test_one_empty_side_is_never_merged_directly(golden):
    """«از ناشناس» هیچ‌وقت مستقیم ادغام نمی‌شود؛ یا بازبینی می‌شود یا تصمیمش
    به سطح خوشه می‌رود."""
    verdict, _, _, _ = classify_pair(golden[0], golden[3])
    assert verdict in {"review", "candidate"}


def test_low_similarity_never_merges(extractor):
    a = make(extractor, 1, "جزوه ریاضی گسسته دانشگاهی")
    b = make(extractor, 2, "نمونه قرارداد اجاره مغازه")
    assert classify_pair(a, b)[0] == "no_merge"


def test_similarity_is_computed_on_normalized_title(extractor):
    """کلمات تبلیغاتی نباید روی تصمیم اثر بگذارند."""
    plain = make(extractor, 1, "رمان تاوان خیانت آوا")
    noisy = make(extractor, 2, "دانلود رایگان رمان تاوان خیانت آوا pdf کامل ۱۴۰۴")
    verdict, score, _, _ = classify_pair(plain, noisy)
    assert verdict == "merge"
    assert score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# خوشه‌بندی کامل
# ---------------------------------------------------------------------------


def test_golden_scenario_produces_four_products(golden):
    groups, _, review, stats = resolve(golden)
    assert len(groups) == 4, [[golden[i].raw_title for i in g] for g in groups]

    merged = [g for g in groups if len(g) > 1]
    assert len(merged) == 1
    assert set(merged[0]) == {0, 1}  # فقط دو نسخه‌ی «آوا» ادغام شدند
    assert stats.red_lines >= 1
    assert stats.merged_pairs == 1


def test_ambiguous_pair_flags_both_sides_for_review(golden):
    _, _, review, stats = resolve(golden)
    assert stats.review_pairs >= 1
    assert 3 in review  # «از ناشناس»


def test_no_llm_and_no_network_is_required(golden, monkeypatch):
    """فاز ۲ باید کاملاً آفلاین و رایگان باشد."""
    import urllib.request

    def explode(*args, **kwargs):  # pragma: no cover - نباید صدا زده شود
        raise AssertionError("فاز ۲ نباید هیچ درخواست شبکه‌ای بزند")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    groups, _, _, _ = resolve(golden)
    assert len(groups) == 4


def test_red_line_survives_transitive_merge(extractor):
    """A~B و B~C مجازند، ولی A و C خط قرمز دارند: نباید یک خوشه شوند."""
    items = [
        make(extractor, 1, "رمان تاوان خیانت آوا"),
        make(extractor, 2, "رمان تاوان خیانت"),
        make(extractor, 3, "رمان تاوان خیانت مهسا"),
    ]
    groups, _, _, _ = resolve(items)
    for group in groups:
        assert not ({0, 2} <= set(group))


# ---------------------------------------------------------------------------
# مرحله A — بلاک‌بندی
# ---------------------------------------------------------------------------


def test_blocking_reduces_pair_count(extractor):
    items = [make(extractor, i, f"جزوه ریاضی فصل {i}") for i in range(20)]
    items += [make(extractor, 100 + i, f"نمونه قرارداد اجاره نوع {i}") for i in range(20)]
    pairs = candidate_pairs(items)
    total = len(items) * (len(items) - 1) // 2
    assert len(pairs) < total
    assert all(not (a < 20 <= b) for a, b in pairs)


def test_blocking_keeps_true_duplicates_together(golden):
    assert (0, 1) in candidate_pairs(golden)


# ---------------------------------------------------------------------------
# عنوان نهایی و خوشه‌ی امن
# ---------------------------------------------------------------------------


def test_canonical_title_is_the_most_complete_one(extractor):
    members = [
        make(extractor, 1, "رمان تاوان خیانت"),
        make(extractor, 2, "دانلود رمان تاوان خیانت آوا pdf"),
    ]
    title = pick_canonical_title(members, normalizer.DEFAULT_CONFIG)
    assert "آوا" in title


def test_safe_clusters_refuses_forbidden_union():
    clusters = SafeClusters(3)
    clusters.forbid(0, 2)
    assert clusters.union(0, 1) is True
    assert clusters.union(1, 2) is False
    assert sorted(len(c) for c in clusters.clusters()) == [1, 2]


# ---------------------------------------------------------------------------
# اجرای فاز روی دیتابیس
# ---------------------------------------------------------------------------


def _seed(conn, run_id):
    with db.transaction(conn):
        for index, title in enumerate(GOLDEN):
            db.insert_raw_product(
                conn,
                run_id=run_id,
                source_domain=f"site{index % 2}.ir",
                source_url=f"https://site{index % 2}.ir/p/{index}",
                raw_title=title,
                normalized_title=normalizer.normalize(title),
                topic_score=0.9,
                is_relevant=True,
            )


def test_phase2_writes_canonical_products_and_mapping(tmp_path):
    conn = db.connect(tmp_path / "runs.db")
    run_id = db.create_run(conn, "رمان", run_id="r1")
    config = load_config(None)
    config.raw["database"]["path"] = str(tmp_path / "runs.db")
    _seed(conn, run_id)

    stats = p2_resolve.run(conn, run_id, config, verbose=False)
    products = db.canonical_products(conn, run_id)

    assert len(products) == 4 == stats.canonical_count
    titles = [p["canonical_title"] for p in products]
    assert any("آوا" in t for t in titles)
    assert any("مهسا" in t for t in titles)

    ava = next(p for p in products if "آوا" in p["canonical_title"])
    assert len(db.source_urls_for(conn, int(ava["id"]))) == 2
    assert sorted(db.source_domains_for(conn, int(ava["id"]))) == ["site0.ir", "site1.ir"]
    assert any(p["needs_review"] for p in products)
    conn.close()


def test_phase2_rerun_does_not_duplicate(tmp_path):
    conn = db.connect(tmp_path / "runs.db")
    run_id = db.create_run(conn, "رمان", run_id="r1")
    config = load_config(None)
    config.raw["database"]["path"] = str(tmp_path / "runs.db")
    _seed(conn, run_id)

    p2_resolve.run(conn, run_id, config, verbose=False)
    p2_resolve.run(conn, run_id, config, verbose=False)
    assert len(db.canonical_products(conn, run_id)) == 4
    conn.close()


# ---------------------------------------------------------------------------
# رگرسیون: نام مدرس/نویسنده نباید گم شود
# ---------------------------------------------------------------------------


def test_two_professors_are_never_merged(extractor):
    """اگر «استاد» نشانگر نام نباشد، هر دو عنوان توکن یکسان می‌گیرند و
    جزوه‌ی دو استاد متفاوت اشتباهی یکی می‌شود — دقیقاً همان merge غیرقابل بازگشت."""
    a = make(extractor, 1, "دانلود جزوه ریاضی عمومی ۱ استاد کریمی")
    b = make(extractor, 2, "جزوه ریاضی عمومی ۱ استاد رضایی")
    assert extractor.extract(a.raw_title).tokens == ["۱", "کریمی"]
    assert extractor.extract(b.raw_title).tokens == ["۱", "رضایی"]
    verdict, _, _, reason = classify_pair(a, b)
    assert verdict == "no_merge"
    assert "خط قرمز" in reason


@pytest.mark.parametrize(
    "marker", ["استاد", "دکتر", "مهندس", "نویسنده", "از", "نوشته"]
)
def test_person_markers_capture_the_following_name(extractor, marker):
    tokens = extractor.extract(f"جزوه شیمی آلی {marker} محمدی").tokens
    assert "محمدی" in tokens


def test_number_does_not_swallow_a_person_marker(extractor):
    assert extractor.extract("جزوه ریاضی ۲ استاد کریمی").tokens == ["۲", "کریمی"]
    # ولی «۴ برادر» طبق جدول داکیومنت یک واحد می‌ماند
    assert extractor.extract("رمان تاوان خیانت ۴ برادر").tokens == ["۴ برادر"]


def test_same_professor_still_merges(extractor):
    a = make(extractor, 1, "دانلود جزوه ریاضی عمومی ۱ استاد کریمی")
    b = make(extractor, 2, "جزوه ریاضی عمومی ۱ استاد کریمی pdf رایگان")
    assert classify_pair(a, b)[0] == "merge"


# ---------------------------------------------------------------------------
# نویسنده، مهم‌ترین سیگنال هویت
# ---------------------------------------------------------------------------


def test_two_novels_with_no_author_are_not_merged(extractor):
    """«رمان شب سرد» و «رمان شب گرم» دو رمان متفاوت‌اند، نه یکی."""
    a = make(extractor, 1, "رمان شب سرد")
    b = make(extractor, 2, "رمان شب گرم")
    verdict, score, _, _ = classify_pair(a, b)
    assert score >= 0.80  # شباهت متنی بالاست…
    assert verdict == "review"  # …ولی هیچ شاهدی بر یکی بودنشان نیست


def test_same_novel_with_noise_words_is_merged(extractor):
    """«رمان شب سرد» و «رمان شب سرد کامل» یک رمان‌اند."""
    a = make(extractor, 1, "رمان شب سرد")
    b = make(extractor, 2, "رمان شب سرد کامل")
    assert classify_pair(a, b)[0] == "merge"


def test_same_author_and_close_title_is_one_novel(extractor):
    """«رمان شب سرد از الناز» و «رمان شب سرما از الناز» یک رمان‌اند."""
    a = make(extractor, 1, "رمان شب سرد الناز")
    b = make(extractor, 2, "رمان شب سرما الناز")
    verdict, _, _, reason = classify_pair(a, b)
    assert verdict == "merge"
    assert "نویسنده یکسان" in reason


def test_different_authors_are_a_red_line(extractor):
    """«رمان شب هات از سحر» و «رمان شب گرم از الناز» دو رمان متفاوت‌اند."""
    a = make(extractor, 1, "رمان شب هات سحر")
    b = make(extractor, 2, "رمان شب گرم الناز")
    verdict, _, _, reason = classify_pair(a, b)
    assert verdict == "no_merge"
    assert "نویسنده متفاوت" in reason


def test_different_authors_beat_a_very_similar_title(extractor):
    """حتی وقتی عنوان‌ها تقریباً یکی‌اند، نویسنده‌ی متفاوت خط قرمز است."""
    a = make(extractor, 1, "رمان تاوان خیانت آوا")
    b = make(extractor, 2, "رمان تاوان خیانت مهسا")
    verdict, score, _, reason = classify_pair(a, b)
    assert score >= 0.85
    assert verdict == "no_merge"
    assert "نویسنده متفاوت" in reason


def test_two_novels_by_the_same_author_are_not_merged(extractor):
    """یک نویسنده چند رمان می‌نویسد؛ «نویسنده‌ی یکسان» به‌تنهایی کافی نیست."""
    for title_a, title_b in [
        ("رمان شب سرد الناز", "رمان شب بخیر الناز"),
        ("رمان تاوان خیانت الناز", "رمان شب سرد الناز"),
        ("رمان استاد مغرور سحر", "رمان دختر شیطون سحر"),
    ]:
        a = make(extractor, 1, title_a)
        b = make(extractor, 2, title_b)
        assert classify_pair(a, b)[0] != "merge", (title_a, title_b)


def test_surname_does_not_split_the_same_author(extractor):
    """«الناز» و «الناز محمدی» یک نویسنده‌اند، نه دو نفر."""
    a = make(extractor, 1, "رمان شب سرد الناز")
    b = make(extractor, 2, "رمان شب سرد الناز محمدی")
    assert a.entity.person_key & b.entity.person_key
    assert classify_pair(a, b)[0] == "merge"


def test_volume_is_still_a_red_line_for_the_same_author(extractor):
    a = make(extractor, 1, "رمان شب سرد الناز")
    b = make(extractor, 2, "رمان شب سرد الناز جلد ۲")
    assert classify_pair(a, b)[0] == "no_merge"


# ---------------------------------------------------------------------------
# استنتاج در سطح خوشه: عنوان بدون نویسنده
# ---------------------------------------------------------------------------


def test_unnamed_title_joins_the_single_author_in_the_field(extractor):
    """«رمان شب سرد» + «رمان شب سرد الناز» + «رمان شب سرد کامل» = یک رمان."""
    items = [
        make(extractor, 1, "رمان شب سرد"),
        make(extractor, 2, "رمان شب سرد الناز"),
        make(extractor, 3, "رمان شب سرد کامل"),
    ]
    groups, confidences, review, stats = resolve(items)
    assert len(groups) == 1
    # هر دو عنوان بی‌نویسنده به همان الناز چسبیده‌اند
    assert stats.inferred_merges == 2
    assert not review


def test_unnamed_title_stays_apart_when_two_authors_compete(extractor):
    """اگر دو نویسنده برای همان عنوان پایه باشند، معلوم نیست مال کدام است."""
    items = [
        make(extractor, 1, "رمان شب سرد"),
        make(extractor, 2, "رمان شب سرد الناز"),
        make(extractor, 3, "رمان شب سرد سحر"),
    ]
    groups, _, review, _ = resolve(items)
    assert len(groups) == 3
    assert 0 in review


def test_canonical_title_prefers_the_clean_variant_on_a_tie(extractor):
    """در تساویِ توکن معنادار، عنوان بدون کلمات تبلیغاتی انتخاب می‌شود."""
    members = [
        make(extractor, 1, "جزوه ریاضی عمومی ۱ استاد کریمی pdf رایگان"),
        make(extractor, 2, "دانلود جزوه ریاضی عمومی ۱ استاد کریمی"),
        make(extractor, 3, "جزوه ریاضی عمومی ۱ استاد کریمی"),
    ]
    assert pick_canonical_title(members, normalizer.DEFAULT_CONFIG) == (
        "جزوه ریاضی عمومی ۱ استاد کریمی"
    )


def test_role_word_in_the_title_is_not_mistaken_for_an_author(extractor):
    """در «رمان استاد مغرور الناز» نویسنده الناز است، نه «مغرور».

    اگر «مغرور» نویسنده شمرده شود، «رمان استاد مغرور الناز» و «رمان استاد
    مغرور سحر» توکن مشترک پیدا می‌کنند و اشتباهی ادغام می‌شوند.
    """
    assert extractor.extract("رمان استاد مغرور الناز").tokens == ["الناز"]
    assert extractor.extract("رمان استاد مغرور سحر").tokens == ["سحر"]
    a = make(extractor, 1, "رمان استاد مغرور الناز")
    b = make(extractor, 2, "رمان استاد مغرور سحر")
    verdict, _, _, reason = classify_pair(a, b)
    assert verdict == "no_merge"
    assert "نویسنده متفاوت" in reason


def test_role_marker_still_captures_a_trailing_surname(extractor):
    assert extractor.extract("جزوه ریاضی استاد کریمی").tokens == ["کریمی"]
    assert extractor.extract("جزوه ریاضی ۱ دکتر محمدی").tokens == ["۱", "محمدی"]


def test_author_marker_does_not_break_the_similarity(extractor):
    """«شب سرما نوشته الناز» و «شب سرد الناز» باید مقایسه و ادغام شوند؛
    اگر «نوشته» در عنوان نرمال‌شده بماند، شباهت زیر آستانه می‌افتد."""
    a = make(extractor, 1, "دانلود رمان شب سرد الناز pdf")
    b = make(extractor, 2, "رمان شب سرما نوشته الناز")
    verdict, score, _, _ = classify_pair(a, b)
    assert score >= 0.88
    assert verdict == "merge"


def test_marker_does_not_steal_a_blocking_key(extractor):
    """نشانگر نباید کلید بلاک را بدزدد، وگرنه دو عنوان مشابه هرگز مقایسه
    نمی‌شوند و ادغام بی‌سروصدا از دست می‌رود."""
    items = [
        make(extractor, 1, "دانلود رمان شب سرد الناز pdf"),
        make(extractor, 2, "رمان شب سرما نوشته الناز"),
        make(extractor, 3, "رمان تاوان خیانت آوا"),
    ]
    assert (0, 1) in candidate_pairs(items)
    groups, _, _, _ = resolve(items)
    assert sorted(len(g) for g in groups) == [1, 2]


def test_canonical_title_follows_the_majority_of_the_cluster(extractor):
    """اگر سه نسخه «شب سرد» و یک نسخه «شب سرما» در یک خوشه باشند، عنوان نهایی
    باید «شب سرد» باشد، نه نسخه‌ی تک‌افتاده."""
    members = [
        make(extractor, 1, "رمان شب سرد"),
        make(extractor, 2, "دانلود رمان شب سرد الناز pdf"),
        make(extractor, 3, "رمان شب سرد کامل"),
        make(extractor, 4, "رمان شب سرما نوشته الناز"),
    ]
    title = pick_canonical_title(members, normalizer.DEFAULT_CONFIG)
    assert "سرد" in title and "سرما" not in title
