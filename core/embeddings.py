"""بردارسازی و شباهت متنی.

اگر ``sentence-transformers`` نصب باشد از ``intfloat/multilingual-e5-large``
استفاده می‌شود (کیفیت بالاتر برای فارسی). در غیر این صورت یک انکودر جایگزینِ
tf-idfِ hashing با کتابخانه‌ی استاندارد کار می‌کند تا پایپ‌لاین همیشه اجرا شود.

هر دو مسیر یک قرارداد دارند: ``encode(texts) -> list[list[float]]`` با بردار
نرمال‌شده، و ``cosine(a, b) -> float`` در بازه‌ی [0, 1].
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Sequence

_DIM = 512


class Encoder(Protocol):
    name: str

    def encode(self, texts: Sequence[str], kind: str = "passage") -> list[list[float]]: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    # بردارها نرمال‌اند؛ ولی برای اطمینان نرمال می‌کنیم.
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    value = dot / (na * nb)
    return max(0.0, min(1.0, value))


def _normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


class HashingEncoder:
    """انکودر جایگزین بدون وابستگی: ترکیب توکن‌های کلمه‌ای و n-gramهای کاراکتری.

    برای فارسی به‌اندازه‌ی کافی خوب هست که فاز ۱ (تشخیص موضوعی) کار کند، ولی
    جایگزین embedding واقعی نیست — با نصب ``sentence-transformers`` ارتقا دهید.
    """

    name = "hashing-tfidf"

    def __init__(self, dim: int = _DIM, char_ngram: int = 3) -> None:
        self.dim = dim
        self.char_ngram = char_ngram

    def _features(self, text: str) -> Counter[str]:
        tokens = [t for t in re.split(r"\s+", text.strip()) if t]
        features: Counter[str] = Counter()
        for token in tokens:
            features[f"w:{token}"] += 2  # وزن بیشتر برای کلمه‌ی کامل
            padded = f" {token} "
            for i in range(len(padded) - self.char_ngram + 1):
                features[f"c:{padded[i : i + self.char_ngram]}"] += 1
        return features

    def encode(self, texts: Sequence[str], kind: str = "passage") -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            features = self._features(text or "")
            for feature, count in features.items():
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign * (1.0 + math.log(count))
            vectors.append(_normalize_vector(vector))
        return vectors


class E5Encoder:  # pragma: no cover - نیازمند sentence-transformers
    """``intfloat/multilingual-e5-large`` با پیشوندهای query/passage."""

    name = "multilingual-e5-large"

    def __init__(self, model_name: str = "intfloat/multilingual-e5-large") -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore

        self.model = SentenceTransformer(model_name)
        self.name = model_name

    def encode(self, texts: Sequence[str], kind: str = "passage") -> list[list[float]]:
        prefix = "query: " if kind == "query" else "passage: "
        vectors = self.model.encode(
            [prefix + (t or "") for t in texts],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [list(map(float, v)) for v in vectors]


def get_encoder(prefer_model: str | None = None) -> Encoder:
    """انکودر موجود را برمی‌گرداند و در صورت نبودن مدل سنگین، fallback می‌کند."""
    try:  # pragma: no cover - وابسته به محیط
        return E5Encoder(prefer_model or "intfloat/multilingual-e5-large")
    except Exception:
        return HashingEncoder()


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _calibrate(
    encoder: "Encoder",
    named: Sequence[tuple[str, list[float]]],
    examples_by_label: dict[str, Sequence[str]],
) -> dict[str, tuple[float, float]]:
    """آستانه‌ی هر دسته را از فاصله‌ی نمونه‌های خودش تا نمونه‌های بی‌ربط می‌سازد.

    آستانه‌ی ثابت با انکودرهای مختلف معنای یکسانی ندارد: انکودر جایگزین به
    عنوان مرتبط ۰.۵ می‌دهد و e5 به عنوان بی‌ربط ۰.۷. پس به‌جای عدد ثابت،
    مرز را وسط دو ابر نقطه می‌گذاریم.
    """
    from .presets import NEGATIVE_SAMPLES

    negatives = encoder.encode(list(NEGATIVE_SAMPLES), kind="passage")
    out: dict[str, tuple[float, float]] = {}
    for label, reference in named:
        examples = [e for e in examples_by_label.get(label, ()) if e]
        if not examples:
            continue
        positive = [cosine(v, reference) for v in encoder.encode(examples, kind="passage")]
        negative = [cosine(v, reference) for v in negatives]
        low = _percentile(positive, 0.15)      # پایین‌ترین نمونه‌های خودی
        high = _percentile(negative, 0.90)     # بالاترین نمونه‌های بی‌ربط
        if low <= high:  # دو ابر روی هم افتاده‌اند — کالیبراسیون قابل اتکا نیست
            continue
        gap = low - high
        out[label] = (
            round(min(0.95, max(0.05, high + 0.50 * gap)), 4),
            round(min(0.95, max(0.03, high + 0.25 * gap)), 4),
        )
    return out


@dataclass
class TopicMatcher:
    """بردار مرجع موضوع = میانگین بردارهای ``target_topic`` + نمونه‌عنوان‌ها."""

    encoder: Encoder
    reference: list[float]
    # (نام دسته، بردار مرجع) — خالی یعنی حالت تک‌موضوعی قدیمی
    categories: list[tuple[str, list[float]]] = field(default_factory=list)
    # نام دسته → (آستانه‌ی مرتبط، آستانه‌ی مرزی) — از روی نمونه‌ها کالیبره می‌شود
    thresholds: dict[str, tuple[float, float]] = field(default_factory=dict)

    @classmethod
    def build(cls, encoder: Encoder, topic: str, examples: Iterable[str]) -> "TopicMatcher":
        texts = [topic] + [e for e in examples if e]
        vectors = encoder.encode(texts, kind="query")
        if not vectors:
            raise ValueError("برای ساخت بردار مرجع حداقل به یک متن نیاز است.")
        length = len(vectors[0])
        summed = [0.0] * length
        for vector in vectors:
            for i, value in enumerate(vector):
                summed[i] += value
        return cls(encoder=encoder, reference=_normalize_vector(summed))

    @classmethod
    def build_categories(
        cls, encoder: Encoder, categories: Sequence[tuple[str, Sequence[str]]]
    ) -> "TopicMatcher":
        """چند دسته با هم — هر دسته بردار مرجع خودش را دارد.

        امتیاز هر عنوان، بیشترین شباهت به میان دسته‌هاست. اگر همه‌ی دسته‌ها را
        در یک بردار میانگین می‌گرفتیم، «رمان» و «کتاب درسی» به یک نقطه‌ی وسطِ
        بی‌معنا تبدیل می‌شدند و هیچ‌کدام درست تشخیص داده نمی‌شدند.
        """
        named: list[tuple[str, list[float]]] = []
        for label, examples in categories:
            texts = [label] + [e for e in examples if e]
            vectors = encoder.encode(texts, kind="query")
            if not vectors:
                continue
            summed = [0.0] * len(vectors[0])
            for vector in vectors:
                for i, value in enumerate(vector):
                    summed[i] += value
            named.append((label, _normalize_vector(summed)))
        if not named:
            raise ValueError("برای ساخت بردار مرجع حداقل به یک دسته نیاز است.")
        overall = [0.0] * len(named[0][1])
        for _, vector in named:
            for i, value in enumerate(vector):
                overall[i] += value
        matcher = cls(encoder=encoder, reference=_normalize_vector(overall), categories=named)
        matcher.thresholds = _calibrate(encoder, named, dict(categories))
        return matcher

    def limits(self, label: str, fallback: tuple[float, float]) -> tuple[float, float]:
        """(آستانه‌ی مرتبط، آستانه‌ی مرزی) برای یک دسته؛ اگر کالیبره نشده،
        همان مقادیر config."""
        return self.thresholds.get(label, fallback)

    @property
    def labels(self) -> list[str]:
        return [label for label, _ in self.categories]

    def _best(self, vector: Sequence[float]) -> tuple[str, float]:
        if not self.categories:
            return "", cosine(vector, self.reference)
        best_label, best_score = "", -1.0
        for label, reference in self.categories:
            score = cosine(vector, reference)
            if score > best_score:
                best_label, best_score = label, score
        return best_label, best_score

    def score(self, title: str) -> float:
        return self.classify(title)[1]

    def classify(self, title: str) -> tuple[str, float]:
        """(نام دسته، امتیاز). بدون دسته‌بندی، نام خالی برمی‌گردد."""
        vector = self.encoder.encode([title], kind="passage")[0]
        return self._best(vector)

    def score_batch(self, titles: Sequence[str]) -> list[float]:
        return [score for _, score in self.classify_batch(titles)]

    def classify_batch(self, titles: Sequence[str]) -> list[tuple[str, float]]:
        vectors = self.encoder.encode(list(titles), kind="passage")
        return [self._best(v) for v in vectors]
