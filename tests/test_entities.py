"""تست مرحله B فاز ۲ — استخراج توکن تمایزدهنده."""

from __future__ import annotations

import pytest

from content_pipeline.core.entities import EntityExtractor


@pytest.fixture(scope="module")
def extractor() -> EntityExtractor:
    return EntityExtractor()


@pytest.mark.parametrize(
    "title,expected",
    [
        ("رمان تاوان خیانت آوا", ["آوا"]),
        ("رمان تاوان خیانت مهسا", ["مهسا"]),
        ("رمان تاوان خیانت از ناشناس", []),
        ("رمان تاوان خیانت ۴ برادر", ["۴ برادر"]),
    ],
)
def test_golden_examples_from_the_spec(extractor, title, expected):
    assert extractor.extract(title).tokens == expected


def test_commercial_words_do_not_change_entity_tokens(extractor):
    plain = extractor.extract("رمان تاوان خیانت آوا")
    noisy = extractor.extract("دانلود رایگان رمان تاوان خیانت آوا pdf کامل ۱۴۰۴")
    assert plain.key == noisy.key
    assert plain.base_title == noisy.base_title


def test_unknown_author_is_not_an_entity(extractor):
    for title in ("رمان الف از ناشناس", "رمان الف نویسنده نامشخص"):
        assert extractor.extract(title).is_empty


def test_volume_marker_binds_to_number(extractor):
    assert extractor.extract("جزوه فیزیک جلد ۲").tokens == ["جلد ۲"]
    assert extractor.extract("سریال ماه پارت ۵").tokens == ["پارت ۵"]


def test_year_is_not_an_entity_token(extractor):
    assert extractor.extract("سوالات آیین نامه ۱۴۰۴").is_empty


def test_latin_token_is_an_entity(extractor):
    assert "autocad" in extractor.extract("آموزش autocad مقدماتی").tokens


def test_author_marker_picks_following_name(extractor):
    assert extractor.extract("رمان شب از سحر").tokens == ["سحر"]


def test_base_title_is_identical_for_unknown_and_named_variants(extractor):
    unknown = extractor.extract("رمان تاوان خیانت از ناشناس")
    named = extractor.extract("رمان تاوان خیانت آوا")
    assert unknown.base_title == named.base_title


def test_extra_names_can_be_injected(extractor):
    custom = EntityExtractor(extra_names=frozenset({"شهرزادک"}))
    assert custom.extract("رمان الف شهرزادک").tokens == ["شهرزادک"]
    assert extractor.extract("رمان الف شهرزادک").is_empty
