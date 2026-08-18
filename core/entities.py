"""مرحله B فاز ۲ — استخراج توکن تمایزدهنده (entity tokens).

مثال‌های مرجع داکیومنت:

* «رمان تاوان خیانت آوا»        → ``{آوا}``
* «رمان تاوان خیانت مهسا»       → ``{مهسا}``
* «رمان تاوان خیانت از ناشناس»  → ``{}``  (نویسنده نامشخص)
* «رمان تاوان خیانت ۴ برادر»    → ``{۴ برادر}``

استخراج قانون‌محور است (بدون هزینه) و فقط در حالت‌های مبهم به LLM سپرده
می‌شود. اگر مبهم بماند، طبق «قانون طلایی» ادغام انجام نمی‌شود.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import normalizer as normalizer_markers
from .normalizer import (
    DEFAULT_CONFIG,
    UNKNOWN_MARKERS,
    NormalizerConfig,
    _normalize_chars,
    meaningful_tokens,
    tokenize,
)

_NAMES_FILE = Path(__file__).with_name("data") / "fa_given_names.txt"

#: نشانگرهای شخص و جلد از :mod:`core.normalizer` می‌آیند تا یک منبع حقیقت
#: داشته باشیم: همان‌جا هم به‌عنوان کلمه‌ی ایستا از عنوان نرمال‌شده حذف می‌شوند.
AUTHOR_MARKERS = normalizer_markers.AUTHOR_MARKERS
ROLE_MARKERS = normalizer_markers.ROLE_MARKERS
PERSON_MARKERS = normalizer_markers.PERSON_MARKERS
VOLUME_MARKERS = normalizer_markers.VOLUME_MARKERS

_LATIN_RE = re.compile(r"^[a-z][a-z0-9.\-]{1,}$")
_DIGIT_RE = re.compile(r"^[۰-۹0-9٠-٩]+$")
_YEAR_RE = re.compile(r"^(1[23]\d{2}|14\d{2}|19\d{2}|20\d{2})$")

_FA_TO_EN = {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_FA_TO_EN.update({ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})


@lru_cache(maxsize=1)
def _load_names() -> frozenset[str]:
    if not _NAMES_FILE.exists():  # pragma: no cover - فایل همراه پکیج است
        return frozenset()
    names = set()
    for line in _NAMES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(_normalize_chars(line, DEFAULT_CONFIG).replace("‌", ""))
    return frozenset(names)


def _is_year(token: str) -> bool:
    return bool(_YEAR_RE.match(token.translate(_FA_TO_EN)))


#: منبع‌هایی که یک «شخص» (نویسنده/مدرس) را نشان می‌دهند
PERSON_SOURCES: frozenset[str] = frozenset({"name", "author"})


@dataclass
class EntityResult:
    """خروجی استخراج برای یک عنوان."""

    tokens: list[str] = field(default_factory=list)
    #: منبع هر توکن: name / number / latin / author / volume
    sources: dict[str, str] = field(default_factory=dict)
    #: عنوان پایه پس از حذف توکن‌های تمایزدهنده
    base_tokens: list[str] = field(default_factory=list)

    @property
    def key(self) -> frozenset[str]:
        return frozenset(self.tokens)

    @property
    def persons(self) -> list[str]:
        """توکن‌های شخص — مهم‌ترین سیگنال هویت محصول."""
        return [t for t in self.tokens if self.sources.get(t) in PERSON_SOURCES]

    @property
    def others(self) -> list[str]:
        """بقیه‌ی توکن‌های تمایزدهنده: عدد، جلد/پارت، برند لاتین."""
        return [t for t in self.tokens if self.sources.get(t) not in PERSON_SOURCES]

    @property
    def person_key(self) -> frozenset[str]:
        """نام‌ها به کلمات تکی شکسته می‌شوند تا «الناز» و «الناز محمدی»
        هم‌پوشانی داشته باشند و اشتباهاً دو نویسنده‌ی متفاوت شمرده نشوند."""
        return frozenset(word for token in self.persons for word in token.split())

    @property
    def other_key(self) -> frozenset[str]:
        return frozenset(self.others)

    @property
    def is_empty(self) -> bool:
        return not self.tokens

    @property
    def base_title(self) -> str:
        return " ".join(self.base_tokens)


class EntityExtractor:
    """استخراج قانون‌محور توکن‌های تمایزدهنده."""

    def __init__(
        self,
        config: NormalizerConfig = DEFAULT_CONFIG,
        extra_names: frozenset[str] | None = None,
        unknown_markers: frozenset[str] | None = None,
    ) -> None:
        self.config = config
        self.names = _load_names() | frozenset(
            _normalize_chars(n, config).replace("‌", "") for n in (extra_names or ())
        )
        self.unknown = frozenset(
            _normalize_chars(m, config).replace("‌", "") for m in (unknown_markers or UNKNOWN_MARKERS)
        )
        self._stopwords = config.stopwords()

    # -- کمکی‌ها -------------------------------------------------------------
    def _usable(self, token: str) -> bool:
        return bool(token) and token not in self._stopwords and token not in self.unknown

    def _is_trailing(self, tokens: list[str], index: int) -> bool:
        """آیا بعد از این توکن، کلمه‌ی محتوایی دیگری نمانده؟

        نام شخص در عنوان‌های فارسی معمولاً آخر می‌آید؛ این تست جلوی
        «استاد مغرور» را می‌گیرد که در آن «مغرور» بخشی از عنوان است.
        """
        return all(not self._usable(t) for t in tokens[index + 1 :])

    # -- API -----------------------------------------------------------------
    def extract(self, title: str) -> EntityResult:
        raw = tokenize(title, self.config)
        found: list[str] = []
        sources: dict[str, str] = {}

        def add(token: str, source: str) -> None:
            if token and token not in found:
                found.append(token)
                sources[token] = source

        index = 0
        while index < len(raw):
            token = raw[index]

            # «جلد ۲»، «پارت ۵»
            if token in VOLUME_MARKERS and index + 1 < len(raw) and _DIGIT_RE.match(raw[index + 1]):
                add(f"{token} {raw[index + 1]}", "volume")
                index += 2
                continue

            # عدد تنها یا عدد + اسم («۴ برادر»).
            # عدد نباید نشانگرِ بعدی را ببلعد: در «ریاضی عمومی ۱ استاد کریمی»
            # عددْ سطح درس است و «استاد» شروع نام شخص، نه بخشی از عدد.
            if _DIGIT_RE.match(token) and not _is_year(token):
                nxt = raw[index + 1] if index + 1 < len(raw) else ""
                if nxt in AUTHOR_MARKERS or nxt in VOLUME_MARKERS:
                    add(token, "number")
                    index += 1
                    continue
                if nxt and self._usable(nxt) and not _DIGIT_RE.match(nxt):
                    add(f"{token} {nxt}", "number")
                    index += 2
                else:
                    add(token, "number")
                    index += 1
                continue

            # نشانگر شخص: توکن بعدی نام است، مگر «ناشناس»/«نامشخص».
            # برای نشانگرهای نقشی («استاد») سخت‌گیرتریم، چون خودشان می‌توانند
            # بخشی از عنوان باشند: در «رمان استاد مغرور الناز» نویسنده الناز
            # است، نه «مغرور».
            if token in PERSON_MARKERS and index + 1 < len(raw):
                candidate = raw[index + 1]
                if (
                    self._usable(candidate)
                    and not _DIGIT_RE.match(candidate)
                    and (
                        token in AUTHOR_MARKERS
                        or candidate in self.names
                        or self._is_trailing(raw, index + 1)
                    )
                ):
                    add(candidate, "author")
                index += 2
                continue

            # نام خاص از گازتیر
            if token in self.names and self._usable(token):
                add(token, "name")
                index += 1
                continue

            # توکن لاتین (نام برند/مدل)
            if _LATIN_RE.match(token) and self._usable(token):
                add(token, "latin")
                index += 1
                continue

            index += 1

        # عنوان پایه = عنوان بدون توکن‌های تمایزدهنده، بدون نشانگرهای «نامشخص»
        # و بدون خودِ نشانگرها («از»، «نوشته»، «استاد»، «جلد»).
        # نشانگرها ساختاری‌اند نه محتوایی؛ اگر بمانند، به‌عنوان کلمه‌ی «نادر»
        # کلید بلاک را می‌دزدند و دو عنوانِ واقعاً مشابه هرگز مقایسه نمی‌شوند.
        entity_words = {word for token in found for word in token.split()}
        base = [
            t
            for t in meaningful_tokens(title, self.config)
            if t not in entity_words
            and t not in self.unknown
            and t not in AUTHOR_MARKERS
            and t not in VOLUME_MARKERS
        ]
        return EntityResult(tokens=found, sources=sources, base_tokens=base)

    def extract_many(self, titles: list[str]) -> list[EntityResult]:
        return [self.extract(t) for t in titles]


def residual_tokens(a_tokens: list[str], b_tokens: list[str]) -> tuple[list[str], list[str]]:
    """توکن‌هایی که فقط در یکی از دو عنوان هستند — سیگنال «ابهام»."""
    set_a, set_b = set(a_tokens), set(b_tokens)
    return sorted(set_a - set_b), sorted(set_b - set_a)
