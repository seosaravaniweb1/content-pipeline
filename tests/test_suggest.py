"""تست کش و قواعد ایمنی گوگل ساجست (بخش ۹ داکیومنت).

هیچ تستی به شبکه وصل نمی‌شود؛ لایه‌ی ``_fetch`` جایگزین می‌شود.
"""

from __future__ import annotations

import pytest

from content_pipeline.core import cache, db
from content_pipeline.core.suggest import (
    SessionLimitReached,
    SuggestBlocked,
    SuggestClient,
    SuggestConfig,
    _parse,
)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "runs.db")
    yield connection
    connection.close()


def build(responses, conn=None, **overrides):
    """کلاینتی که به‌جای شبکه از یک دیکشنری پاسخ می‌خواند."""
    options = dict(delay_min=0, delay_max=0, max_per_session=300)
    options.update(overrides)
    slept: list[float] = []
    client = SuggestClient(
        SuggestConfig(**options),
        cache=cache.QueryCache(conn, ttl_days=None) if conn is not None else None,
        sleep=slept.append,
    )
    calls: list[str] = []

    def fake_fetch(query):
        calls.append(query)
        value = responses.get(query, responses.get("*"))
        if isinstance(value, Exception):
            raise value
        return value

    client._fetch = fake_fetch  # type: ignore[assignment]
    client.calls = calls  # type: ignore[attr-defined]
    client.slept = slept  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# پارس پاسخ
# ---------------------------------------------------------------------------


def test_parses_firefox_response_shape():
    assert _parse(["رمان الف", ["رمان الف pdf", "رمان الف کامل"]]) == [
        "رمان الف pdf",
        "رمان الف کامل",
    ]


def test_parse_handles_empty_and_malformed():
    assert _parse(None) == []
    assert _parse(["رمان الف", []]) == []
    assert _parse({"unexpected": True}) == []


# ---------------------------------------------------------------------------
# کش
# ---------------------------------------------------------------------------


def test_cached_query_is_not_sent_twice(conn):
    client = build({"رمان الف": ["رمان الف", ["الف ۱"]]}, conn)
    assert client.suggest("رمان الف") == ["الف ۱"]
    assert client.suggest("رمان الف") == ["الف ۱"]
    assert client.calls == ["رمان الف"]
    assert client.queries_sent == 1


def test_cache_survives_a_new_client(conn):
    build({"رمان الف": ["رمان الف", ["الف ۱"]]}, conn).suggest("رمان الف")
    fresh = build({}, conn)  # پاسخی ندارد؛ باید از کش بخواند
    assert fresh.suggest("رمان الف") == ["الف ۱"]
    assert fresh.queries_sent == 0


def test_empty_result_is_cached_as_a_valid_answer(conn):
    client = build({"رمان ب": ["رمان ب", []]}, conn, max_consecutive_failures=99)
    assert client.suggest("رمان ب") == []
    assert client.suggest("رمان ب") == []
    assert client.calls == ["رمان ب"]


def test_network_error_is_not_cached(conn):
    client = build({"رمان ج": RuntimeError("boom")}, conn, max_consecutive_failures=99)
    assert client.suggest("رمان ج") == []
    assert client.suggest("رمان ج") == []
    assert client.calls == ["رمان ج", "رمان ج"]  # دوباره تلاش شد


def test_cache_key_is_query_specific():
    a = cache.cache_key("google-suggest", {"q": "الف", "hl": "fa"})
    b = cache.cache_key("google-suggest", {"hl": "fa", "q": "الف"})
    assert a == b
    assert a != cache.cache_key("google-suggest", {"q": "ب", "hl": "fa"})


# ---------------------------------------------------------------------------
# قواعد ایمنی
# ---------------------------------------------------------------------------


def test_empty_responses_are_answers_not_failures():
    """پاسخ ۲۰۰ با فهرست خالی یعنی «این عبارت ساجست ندارد».

    اگر این را خطا بشماریم، هر سایتی که عنوان‌های خاص دارد بعد از سه محصول
    متوقف می‌شود و همه‌چیز اشتباهاً «بلاک شدیم» گزارش می‌شود.
    """
    client = build({"*": ["q", []]}, max_consecutive_failures=3, max_consecutive_empty=40)
    for index in range(10):
        assert client.suggest(f"عبارت {index}") == []
    assert client.consecutive_failures == 0
    assert client.consecutive_empty == 10


def test_too_many_empty_responses_in_a_row_stop_with_a_different_message():
    client = build({"*": ["q", []]}, max_consecutive_failures=3, max_consecutive_empty=3)
    client.suggest("الف")
    client.suggest("ب")
    with pytest.raises(SuggestBlocked) as error:
        client.suggest("ج")
    assert "بلاک نشده‌اید" in str(error.value)


def test_three_consecutive_errors_stop_the_phase():
    client = build({"*": RuntimeError("HTTP 429")}, max_consecutive_failures=3)
    client.suggest("الف")
    client.suggest("ب")
    with pytest.raises(SuggestBlocked):
        client.suggest("ج")


def test_a_successful_response_resets_the_error_counter():
    client = build(
        {"الف": RuntimeError("HTTP 500"), "ب": ["ب", ["ب ۱"]], "*": RuntimeError("HTTP 500")},
        max_consecutive_failures=3,
    )
    client.suggest("الف")
    client.suggest("ب")  # موفق → شمارنده صفر می‌شود
    client.suggest("ج")
    client.suggest("د")
    assert client.consecutive_failures == 2  # هنوز به سقف نرسیده


def test_a_successful_response_resets_the_empty_counter():
    client = build(
        {"ب": ["ب", ["ب ۱"]], "*": ["x", []]},
        max_consecutive_failures=3,
        max_consecutive_empty=3,
    )
    client.suggest("الف")
    client.suggest("ب")  # یک پاسخ واقعی
    client.suggest("ج")
    assert client.consecutive_empty == 1
    assert client.empty_total == 2


def test_session_query_limit_is_enforced():
    client = build({"*": ["q", ["s"]]}, max_per_session=2)
    client.suggest("الف")
    client.suggest("ب")
    with pytest.raises(SessionLimitReached):
        client.suggest("ج")
    assert client.remaining == 0


def test_delay_is_applied_between_live_requests():
    client = build({"*": ["q", ["s"]]}, delay_min=2, delay_max=4)
    client.suggest("الف")
    client.suggest("ب")
    assert len(client.slept) == 2
    assert all(2 <= d <= 4 for d in client.slept)


def test_cached_answers_skip_the_delay(conn):
    client = build({"*": ["q", ["s"]]}, conn, delay_min=2, delay_max=4)
    client.suggest("الف")
    client.suggest("الف")
    assert len(client.slept) == 1  # فقط برای درخواست واقعی


def test_empty_query_is_never_sent():
    client = build({"*": ["q", ["s"]]})
    assert client.suggest("   ") == []
    assert client.calls == []
