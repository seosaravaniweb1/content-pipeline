"""شباهت متنی. ``rapidfuzz`` اگر باشد، وگرنه ``difflib`` استاندارد."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Sequence

try:  # pragma: no cover
    from rapidfuzz import fuzz  # type: ignore

    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    fuzz = None  # type: ignore[assignment]
    _HAS_RAPIDFUZZ = False


def ratio(a: str, b: str) -> float:
    """شباهت کاراکتری در بازه‌ی [0, 1]."""
    if not a or not b:
        return 0.0
    if _HAS_RAPIDFUZZ:
        return fuzz.ratio(a, b) / 100.0
    return SequenceMatcher(None, a, b).ratio()


def token_set_ratio(a: str, b: str) -> float:
    """شباهت مجموعه‌ی توکن‌ها — نسبت به ترتیب کلمات مقاوم است."""
    if not a or not b:
        return 0.0
    if _HAS_RAPIDFUZZ:
        return fuzz.token_set_ratio(a, b) / 100.0
    set_a, set_b = set(a.split()), set(b.split())
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    return (2 * len(intersection)) / (len(set_a) + len(set_b))


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def title_similarity(a: str, b: str) -> float:
    """معیار نهاییِ فاز ۲: ترکیب محافظه‌کارانه‌ی توکنی و کاراکتری.

    از میانگین وزنی استفاده می‌شود تا نه ترتیب کلمات و نه غلط‌های تایپی
    به‌تنهایی تصمیم را عوض کنند.
    """
    return round(0.6 * token_set_ratio(a, b) + 0.4 * ratio(a, b), 4)


def backend() -> str:
    return "rapidfuzz" if _HAS_RAPIDFUZZ else "difflib"
