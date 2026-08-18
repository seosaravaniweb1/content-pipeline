"""تست‌های فاز ۰ روی نمونه‌های واقعی فارسی — حیاتی‌ترین تست پروژه.

داکیومنت بخش ۶: «قبل از رفتن به فاز بعد، این تست با حداقل ۲۰ نمونه‌ی واقعی
باید پاس شود.» فهرست :data:`REAL_SAMPLES` همان بیست نمونه است؛ عنوان‌های سایت
خودتان را به همان فهرست اضافه کنید.
"""

from __future__ import annotations

import pytest

from content_pipeline.core import normalizer as N

#: (عنوان خام از سایت رقیب، خروجی انتظاری فاز ۰)
REAL_SAMPLES: list[tuple[str, str]] = [
    ("دانلود رمان تاوان خیانت آوا PDF کامل ۱۴۰۴", "رمان تاوان خیانت آوا"),
    ("خرید جزوه‌ی فیزیک ۱ دانشگاهی رایگان", "جزوه فیزیک ۱ دانشگاهی"),
    ("نمونه قرارداد اجاره مغازه (فایل ورد)", "نمونه قرارداد اجاره مغازه"),
    ("سوالات آيين نامه راهنمايي و رانندگي جديد", "سوالات آیین نامه راهنمایی رانندگی"),
    ("دانلود رایگان جزوه ریاضی عمومی ۱ نسخه کامل", "جزوه ریاضی عمومی ۱"),
    ("جزوه خانواده پیام نور pdf 1403", "جزوه خانواده پیام نور"),
    ("دانلود کتاب‌های درسی پایه هفتم", "کتابهای درسی پایه هفتم"),
    ("خريد نمونه سوالات استخدامي آموزش و پرورش", "نمونه سوالات استخدامی آموزش پرورش"),
    ("جزوه‌ی شیمی آلی دکتر محمدی — نسخه اورجینال", "جزوه شیمی آلی محمدی"),
    ("رمان استاد مغرور جلد ۲ کامل", "رمان مغرور ۲"),
    ("دانلود   رمان   صیغه   اجباری", "رمان صیغه اجباری"),
    ("نمونه قرارداد کار موقت ۱۴۰۳ ورد و pdf", "نمونه قرارداد کار موقت"),
    ("جزوه فيزيك پيش دانشگاهي مهندس رضايي", "جزوه فیزیک پیش دانشگاهی رضایی"),
    ("رمان تاوان خیانت از ناشناس", "رمان تاوان خیانت ناشناس"),
    ("دانلود جزوه آمار و احتمال مهندسی", "جزوه آمار احتمال مهندسی"),
    ("سوالات آیین‌نامه اصلی ۱۴۰۴ با جواب", "سوالات آییننامه اصلی جواب"),
    ("جزوه ی زبان تخصصی مدیریت", "جزوه زبان تخصصی مدیریت"),
    ("نمونه قرارداد پیمانکاری ساختمان — لینک مستقیم", "نمونه قرارداد پیمانکاری ساختمان"),
    ("رمان عاشقانه ایرانی جدید ۱۴۰۴", "رمان عاشقانه ایرانی"),
    ("دانلود جزوه‌ی حسابداری صنعتی ۲ (اورجینال)", "جزوه حسابداری صنعتی ۲"),
]


def test_at_least_twenty_real_samples_are_covered():
    assert len(REAL_SAMPLES) >= 20


@pytest.mark.parametrize("raw,expected", REAL_SAMPLES, ids=range(len(REAL_SAMPLES)))
def test_real_samples(raw, expected):
    assert N.normalize(raw) == expected


# ---------------------------------------------------------------------------
# قواعد تک‌به‌تک بخش ۶
# ---------------------------------------------------------------------------


def test_arabic_letters_become_persian():
    assert N.normalize_display("كتاب عربي") == "کتاب عربی"
    assert N.normalize_display("مدرسة") == "مدرسه"


def test_diacritics_and_tatweel_removed():
    assert N.normalize_display("کِتابِ مُفید") == "کتاب مفید"
    assert N.normalize_display("کــــتاب") == "کتاب"


def test_digits_unified_to_persian_by_default():
    assert N.normalize_display("جزوه 12 و ١٣") == "جزوه ۱۲ و ۱۳"


def test_digits_can_target_english():
    config = N.NormalizerConfig(digit_target="en")
    assert N.normalize_display("جزوه ۱۲", config) == "جزوه 12"


def test_zwnj_variants_collapse_to_same_matching_form():
    a = N.normalize("کتاب‌های درسی")
    c = N.normalize("کتاب‌‌‌های درسی")
    assert a == c


def test_person_and_volume_markers_are_stopwords():
    """«نوشته/از/استاد/جلد» ساختاری‌اند: «شب سرما نوشته الناز» و «شب سرما الناز»
    باید عنوان نرمال‌شده‌ی یکسان بدهند، وگرنه شباهتشان بی‌دلیل پایین می‌آید."""
    assert N.normalize("رمان شب سرما نوشته الناز") == N.normalize("رمان شب سرما الناز")
    assert N.normalize("رمان شب بخیر از الناز") == N.normalize("رمان شب بخیر الناز")
    assert N.normalize("جزوه ریاضی استاد کریمی") == N.normalize("جزوه ریاضی کریمی")


def test_commercial_stopwords_removed():
    assert N.normalize("دانلود رایگان جزوه ریاضی pdf") == "جزوه ریاضی"
    assert N.normalize("خرید نمونه قرارداد کامل ۱۴۰۴") == "نمونه قرارداد"
    assert N.normalize("جزوه نسخه اورجینال") == "جزوه"


def test_year_tokens_removed_but_other_numbers_kept():
    assert "۱۴۰۴" not in N.normalize("جزوه فیزیک ۱۴۰۴")
    assert "۱۴۰۳" not in N.normalize("جزوه فیزیک ۱۴۰۳")
    assert "۲" in N.normalize("جزوه فیزیک جلد ۲")


def test_multiple_spaces_collapse_and_trim():
    assert N.normalize_display("  جزوه    ریاضی\t\tگسسته  ") == "جزوه ریاضی گسسته"


def test_punctuation_becomes_space():
    assert N.normalize_display("جزوه (ریاضی) — گسسته!") == "جزوه ریاضی گسسته"


def test_raw_title_is_never_mutated():
    raw = "دانلود كتاب"
    N.normalize(raw)
    assert raw == "دانلود كتاب"


def test_empty_and_whitespace_input():
    assert N.normalize("") == ""
    assert N.normalize("   ") == ""
    assert N.meaningful_tokens("") == []


def test_stopwords_are_configurable():
    config = N.NormalizerConfig(use_extra_stopwords=False, custom_stopwords=frozenset({"جزوه"}))
    assert N.normalize("جزوه ریاضی", config) == "ریاضی"


def test_normalize_display_keeps_stopwords():
    assert "دانلود" in N.normalize_display("دانلود رمان")
    assert "دانلود" not in N.normalize("دانلود رمان")


def test_normalization_is_idempotent():
    once = N.normalize("دانلود كتاب‌هاي رياضي ۱۴۰۴")
    assert N.normalize(once) == once


@pytest.mark.parametrize(
    "a,b",
    [
        ("كتاب رياضي", "کتاب ریاضی"),
        ("دانلود جزوه شيمي", "جزوه شیمی"),
        ("رمان   تاوان    خیانت", "رمان تاوان خیانت"),
        ("جزوه‌ی فیزیک", "جزوه فیزیک"),
    ],
)
def test_equivalent_titles_normalize_identically(a, b):
    assert N.normalize(a) == N.normalize(b)
