"""فاز ۲ — Entity Resolution (حساس‌ترین فاز).

سه مرحله، به ترتیب بخش ۸ داکیومنت:

* **A — بلاک‌بندی:** به‌جای مقایسه‌ی O(n²)، عنوان‌ها بر اساس اشتراک کلمات کلیدی
  گروه‌بندی می‌شوند و مقایسه فقط داخل هر گروه انجام می‌شود.
* **B — استخراج توکن تمایزدهنده:** :mod:`core.entities` (قانون‌محور و رایگان).
* **C — قانون ادغام:**

  .. code-block:: text

     نویسنده‌ها متفاوت                →  ادغام نکن (خط قرمز)
     جلد/شماره/برند متفاوت            →  ادغام نکن (خط قرمز)

     نویسنده‌ها یکسان                 →  ادغام اگر similarity >= 0.88
     فقط یک طرف نویسنده دارد          →  اگر تنها یک نویسنده در میدان است ادغام،
                                          وگرنه needs_review
     هیچ‌کدام نویسنده ندارند           →  ادغام فقط اگر similarity >= 0.95،
                                          وگرنه needs_review

**قانون طلایی:** در حالت شک ادغام نکن. یک merge اشتباه، داده را برای همیشه
خراب می‌کند و برگشت‌پذیر نیست.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..core import db, normalizer, similarity
from ..core.config import Config
from ..core.entities import EntityExtractor, EntityResult

HIGH_CONFIDENCE = 0.95
#: ادغامی که از روی نویسنده‌ی خوشه استنتاج شده، نه از روی تطبیق مستقیم
INFERRED_CONFIDENCE = 0.85
REVIEW_CONFIDENCE = 0.50

#: نردبان آستانه: هرچه شواهد هویتی کمتر، سخت‌گیری روی متن عنوان بیشتر.
#:
#: * نویسنده‌ی هر دو طرف معلوم و یکی است → :data:`STRONG_MATCH_THRESHOLD`
#: * فقط یک طرف نویسنده دارد            → :data:`STRONG_MATCH_THRESHOLD` + یکتایی نویسنده
#: * هیچ‌کدام نویسنده ندارند             → :data:`NO_ENTITY_THRESHOLD`
#:
#: نکته‌ی مهم: «نویسنده‌ی یکسان» آستانه را **پایین نمی‌آورد**، چون یک نویسنده
#: چند رمان می‌نویسد. اندازه‌گیری روی نمونه‌های واقعی نشان داد دو رمان متفاوت از
#: یک نویسنده تا شباهت ۰.۸۶ هم می‌روند («شب سرد الناز» / «شب بخیر الناز»)، پس
#: مرز بالای آن باند گذاشته شده است.
STRONG_MATCH_THRESHOLD = 0.88

#: بدون هیچ نشانه‌ای از نویسنده، فقط عنوان‌های عملاً یکسان ادغام می‌شوند
#: («شب سرد» = «شب سرد کامل»، ولی «شب سرد» ≠ «شب گرم» با شباهت ۰.۸۲).
NO_ENTITY_THRESHOLD = 0.95


@dataclass
class Candidate:
    """یک عنوان خام آماده‌ی مقایسه."""

    raw_id: int
    raw_title: str
    normalized_title: str
    entity: EntityResult

    @property
    def display_title(self) -> str:
        """شکل خوانا برای ``canonical_title`` (کلمات ایستا حفظ می‌شوند)."""
        return normalizer.normalize_display(self.raw_title)

    @property
    def tokens(self) -> list[str]:
        """کلمات کلیدی برای بلاک‌بندی.

        توکن‌های تمایزدهنده (نام، عدد، جلد) کنار گذاشته می‌شوند تا عنوان‌هایی که
        *پایه‌ی* یکسان دارند در یک بلاک بیفتند — دقیقاً همان جفت‌هایی که باید
        مقایسه شوند. مقایسه‌ی نهایی همچنان روی ``normalized_title`` است.
        """
        return self.entity.base_tokens or self.normalized_title.split()


@dataclass
class Decision:
    a: int
    b: int
    score: float
    verdict: str  # merge | no_merge | review
    confidence: float
    reason: str


@dataclass
class ResolveStats:
    raw_count: int = 0
    pairs_compared: int = 0
    merged_pairs: int = 0
    inferred_merges: int = 0
    red_lines: int = 0
    review_pairs: int = 0
    canonical_count: int = 0
    needs_review: int = 0
    decisions: list[Decision] = field(default_factory=list)

    def render(self) -> str:
        return (
            f"فاز ۲ — ورودی: {self.raw_count}، مقایسه: {self.pairs_compared}، "
            f"ادغام: {self.merged_pairs} (از روی نویسنده: {self.inferred_merges})، "
            f"خط قرمز: {self.red_lines}، "
            f"مبهم (بازبینی): {self.review_pairs}، "
            f"محصول نهایی: {self.canonical_count} (بازبینی: {self.needs_review})"
        )


# ---------------------------------------------------------------------------
# مرحله A — بلاک‌بندی
# ---------------------------------------------------------------------------


def build_blocks(candidates: Sequence[Candidate], top_tokens: int = 3) -> dict[str, list[int]]:
    """گروه‌بندی بر اساس نادرترین کلمات هر عنوان (کلید بلاک)."""
    document_freq: Counter[str] = Counter()
    for candidate in candidates:
        document_freq.update(set(candidate.tokens))

    total = max(len(candidates), 1)
    blocks: dict[str, list[int]] = defaultdict(list)
    for index, candidate in enumerate(candidates):
        tokens = sorted(
            set(candidate.tokens),
            key=lambda t: (math.log(total / (1 + document_freq[t])), t),
            reverse=True,
        )
        if not tokens:
            blocks[f"__empty__{index}"].append(index)
            continue
        for token in tokens[: max(1, top_tokens)]:
            blocks[token].append(index)
    return blocks


def candidate_pairs(
    candidates: Sequence[Candidate], top_tokens: int = 3, max_block_size: int = 400
) -> list[tuple[int, int]]:
    pairs: set[tuple[int, int]] = set()
    for members in build_blocks(candidates, top_tokens).values():
        if len(members) < 2 or len(members) > max_block_size:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = sorted((members[i], members[j]))
                pairs.add((a, b))
    return sorted(pairs)


# ---------------------------------------------------------------------------
# مرحله C — قانون ادغام
# ---------------------------------------------------------------------------


def classify_pair(
    a: Candidate,
    b: Candidate,
    threshold: float = 0.80,
    strong_match_threshold: float = STRONG_MATCH_THRESHOLD,
    no_entity_threshold: float = NO_ENTITY_THRESHOLD,
) -> tuple[str, float, float, str]:
    """خروجی: ``(verdict, score, confidence, reason)``.

    ``verdict`` یکی از ``merge`` / ``no_merge`` / ``review`` / ``candidate`` است.
    ``candidate`` یعنی تصمیم پایانی در سطح خوشه گرفته می‌شود
    (:func:`infer_by_author`). هیچ کال خارجی‌ای اینجا زده نمی‌شود.

    **نویسنده مهم‌ترین سیگنال هویت است:**

    * نویسنده‌ی متفاوت → خط قرمز، حتی اگر عنوان‌ها خیلی شبیه باشند
      («شب هات از سحر» ≠ «شب گرم از الناز»)
    * نویسنده‌ی یکسان → همان رمان است، به شرطی که عنوان هم واقعاً نزدیک باشد
      («شب سرد از الناز» = «شب سرما از الناز»، ولی ≠ «شب بخیر از الناز»)
    * هیچ‌کدام نویسنده ندارند → فقط عنوان‌های عملاً یکسان ادغام می‌شوند
      («شب سرد» = «شب سرد کامل»، ولی ≠ «شب گرم»)
    """
    score = similarity.title_similarity(a.normalized_title, b.normalized_title)
    persons_a, persons_b = a.entity.person_key, b.entity.person_key
    others_a, others_b = a.entity.other_key, b.entity.other_key
    key_a, key_b = a.entity.key, b.entity.key

    # ۱) خط قرمز نویسنده — بالاترین اولویت
    if persons_a and persons_b and not (persons_a & persons_b):
        return "no_merge", score, 0.0, "خط قرمز: نویسنده متفاوت"

    # ۲) خط قرمز سایر توکن‌های تمایزدهنده (جلد ۱ در برابر جلد ۲، برند متفاوت)
    if others_a and others_b and others_a != others_b:
        return "no_merge", score, 0.0, "خط قرمز: توکن تمایزدهنده متفاوت"

    if score < threshold:
        return "no_merge", score, 0.0, "شباهت عنوان کمتر از آستانه"

    strong = score >= strong_match_threshold

    # ۳) عدم تقارن در توکن‌های غیرشخصی («شب سرد» در برابر «شب سرد جلد ۲»)
    if others_a != others_b:
        if key_a and key_b:
            return "no_merge", score, 0.0, "خط قرمز: توکن تمایزدهنده متفاوت"
        if strong:
            return "candidate", score, 0.0, "یک طرف توکن تمایزدهنده ندارد"
        return "review", score, 0.0, "توکن تمایزدهنده‌ی نامتقارن — بازبینی دستی"

    # ۴) نویسنده‌ی یکسان
    if persons_a & persons_b:
        if strong:
            return "merge", score, HIGH_CONFIDENCE, "نویسنده یکسان و عنوان نزدیک"
        return "review", score, 0.0, "نویسنده یکسان ولی عنوان‌ها به‌اندازه‌ی کافی نزدیک نیستند"

    # ۵) فقط یک طرف نویسنده دارد → تصمیم در سطح خوشه
    if persons_a or persons_b:
        if strong:
            return "candidate", score, 0.0, "فقط یک طرف نویسنده دارد"
        return "review", score, 0.0, "فقط یک طرف نویسنده دارد و عنوان‌ها دور هستند"

    # ۶) هیچ نشانه‌ای از نویسنده نیست — فقط شباهت متن
    if score >= no_entity_threshold:
        return "merge", score, HIGH_CONFIDENCE, "عنوان‌ها عملاً یکسان‌اند"
    return "review", score, 0.0, "بدون نویسنده و بدون شباهت کافی — بازبینی دستی"


# ---------------------------------------------------------------------------
# خوشه‌بندی با رعایت خط قرمز
# ---------------------------------------------------------------------------


class SafeClusters:
    """اتحاد حریصانه‌ای که هرگز دو عضو «خط قرمز» را در یک خوشه نمی‌گذارد."""

    def __init__(self, size: int) -> None:
        self.members: list[set[int]] = [{i} for i in range(size)]
        self.owner: list[int] = list(range(size))
        self.forbidden: set[tuple[int, int]] = set()

    def forbid(self, a: int, b: int) -> None:
        self.forbidden.add(tuple(sorted((a, b))))  # type: ignore[arg-type]

    def _blocked(self, group_a: set[int], group_b: set[int]) -> bool:
        for x in group_a:
            for y in group_b:
                if tuple(sorted((x, y))) in self.forbidden:
                    return True
        return False

    def union(self, a: int, b: int) -> bool:
        root_a, root_b = self.owner[a], self.owner[b]
        if root_a == root_b:
            return True
        group_a, group_b = self.members[root_a], self.members[root_b]
        if self._blocked(group_a, group_b):
            return False
        group_a |= group_b
        for member in group_b:
            self.owner[member] = root_a
        self.members[root_b] = set()
        return True

    def clusters(self) -> list[list[int]]:
        return [sorted(group) for group in self.members if group]


# ---------------------------------------------------------------------------
# استنتاج در سطح خوشه: عنوانِ بدون نویسنده به کدام محصول تعلق دارد؟
# ---------------------------------------------------------------------------


def _uncertain_sides(candidates: Sequence[Candidate], a: int, b: int) -> set[int]:
    """کدام طرفِ یک جفت مبهم واقعاً نیاز به بازبینی دارد؟

    اگر فقط یکی از دو عنوان نویسنده داشته باشد، آن یکی هویت روشنی دارد و
    ابهام مالِ طرف بی‌نویسنده است؛ در غیر این صورت هر دو مبهم‌اند.
    """
    has_a = bool(candidates[a].entity.person_key)
    has_b = bool(candidates[b].entity.person_key)
    if has_a and not has_b:
        return {b}
    if has_b and not has_a:
        return {a}
    return {a, b}


def _split_named(
    candidates: Sequence[Candidate], a: int, b: int
) -> tuple[int, int]:
    """کدام طرف «بدون نویسنده» است؟ خروجی ``(unnamed, named)``."""
    if not candidates[a].entity.person_key:
        return a, b
    if not candidates[b].entity.person_key:
        return b, a
    return (a, b) if len(candidates[a].entity.key) <= len(candidates[b].entity.key) else (b, a)


def infer_by_author(
    candidates: Sequence[Candidate],
    pending: dict[int, list[tuple[int, float]]],
    accepted: list[tuple[int, int, float]],
    review: set[int],
    stats: ResolveStats,
) -> None:
    """عنوانی که نویسنده ندارد را فقط وقتی به یک محصول می‌چسبانیم که **تنها یک**
    نویسنده برای آن عنوان پایه وجود داشته باشد.

    * «رمان شب سرد» + «رمان شب سرد الناز» → یک رمان (فقط یک نویسنده در میدان)
    * «رمان تاوان خیانت از ناشناس» + آوا و مهسا و ۴ برادر → معلوم نیست مال کدام
      است، پس ادغام نمی‌شود و به شیت C می‌رود
    """
    for unnamed, partners in pending.items():
        author_keys = {candidates[named].entity.person_key for named, _ in partners}
        single_author = len(author_keys) == 1 and all(author_keys)
        if single_author:
            best = max(partners, key=lambda item: item[1])[0]
            accepted.append((unnamed, best, INFERRED_CONFIDENCE))
            stats.inferred_merges += 1
        else:
            stats.review_pairs += 1
            review.add(unnamed)


# ---------------------------------------------------------------------------
# هسته‌ی فاز (بدون دیتابیس، تا مستقیم قابل تست باشد)
# ---------------------------------------------------------------------------


def resolve(
    candidates: Sequence[Candidate],
    threshold: float = 0.80,
    top_tokens: int = 3,
    strong_match_threshold: float = STRONG_MATCH_THRESHOLD,
    no_entity_threshold: float = NO_ENTITY_THRESHOLD,
) -> tuple[list[list[int]], dict[int, float], set[int], ResolveStats]:
    stats = ResolveStats(raw_count=len(candidates))
    clusters = SafeClusters(len(candidates))
    pairs = candidate_pairs(candidates, top_tokens)
    stats.pairs_compared = len(pairs)

    accepted: list[tuple[int, int, float]] = []
    review: set[int] = set()
    #: عنوان بدون نویسنده → فهرست (عنوان نویسنده‌دار، شباهت)
    pending: dict[int, list[tuple[int, float]]] = defaultdict(list)

    for a, b in pairs:
        verdict, score, confidence, reason = classify_pair(
            candidates[a], candidates[b], threshold, strong_match_threshold, no_entity_threshold
        )
        if verdict == "merge":
            accepted.append((a, b, confidence))
        elif verdict == "candidate":
            unnamed, named = _split_named(candidates, a, b)
            pending[unnamed].append((named, score))
        elif verdict == "review":
            # ادغام نمی‌شود. طرفی که نویسنده دارد هویت روشنی دارد و علامت
            # نمی‌خورد؛ ابهام مالِ طرفی است که نویسنده‌اش معلوم نیست.
            stats.review_pairs += 1
            review.update(_uncertain_sides(candidates, a, b))
        elif reason.startswith("خط قرمز"):
            clusters.forbid(a, b)
            stats.red_lines += 1
        stats.decisions.append(Decision(a, b, score, verdict, confidence, reason))

    infer_by_author(candidates, pending, accepted, review, stats)

    accepted.sort(key=lambda item: item[2], reverse=True)
    node_confidence: dict[int, float] = {}
    for a, b, confidence in accepted:
        if clusters.union(a, b):
            stats.merged_pairs += 1
            for node in (a, b):
                node_confidence[node] = min(node_confidence.get(node, 1.0), confidence)
        else:
            # ادغامی که به‌خاطر خط قرمز رد شد، خودش یک سیگنال بازبینی است.
            review.update({a, b})

    groups = clusters.clusters()
    stats.canonical_count = len(groups)
    return groups, node_confidence, review, stats


def pick_canonical_title(
    members: Sequence[Candidate], norm_config: normalizer.NormalizerConfig
) -> str:
    """کامل‌ترین عنوان خوشه: بیشترین توکن معنادار، سپس بلندترین، سپس الفبایی."""

    # اگر خوشه چند نسخه از یک عنوان و یک نسخه‌ی کمی متفاوت داشته باشد، عنوان
    # اکثریت نماینده‌ی بهتری است («شب سرد» ×۳ در برابر «شب سرما» ×۱).
    votes = Counter(c.normalized_title for c in members)

    def sort_key(candidate: Candidate) -> tuple[int, int, int, str]:
        meaningful = normalizer.meaningful_tokens(candidate.raw_title, norm_config)
        display = normalizer.normalize_display(candidate.raw_title, norm_config)
        # رأی اکثریت؛ سپس بیشترین توکن معنادار؛ در تساوی، تمیزترین (کوتاه‌ترین)
        # عنوان — تا «... pdf رایگان» به‌عنوان عنوان نهایی انتخاب نشود.
        return (votes[candidate.normalized_title], len(meaningful), -len(display), display)

    best = max(members, key=sort_key)
    return normalizer.normalize_display(best.raw_title, norm_config).strip() or best.raw_title


# ---------------------------------------------------------------------------
# اجرای فاز روی دیتابیس
# ---------------------------------------------------------------------------


def load_candidates(
    conn: sqlite3.Connection, run_id: str, extractor: EntityExtractor
) -> list[Candidate]:
    return [
        Candidate(
            raw_id=int(row["id"]),
            raw_title=row["raw_title"] or "",
            normalized_title=row["normalized_title"] or "",
            entity=extractor.extract(row["raw_title"] or ""),
        )
        for row in db.relevant_raw_products(conn, run_id)
    ]


def run(
    conn: sqlite3.Connection, run_id: str, config: Config, verbose: bool = True
) -> ResolveStats:
    norm_config = normalizer.config_from_mapping(config.normalizer)
    extractor = EntityExtractor(
        norm_config, extra_names=frozenset(config.get("normalizer.extra_names", []) or [])
    )
    candidates = load_candidates(conn, run_id, extractor)
    if not candidates:
        raise ValueError("هیچ عنوان مرتبطی برای فاز ۲ وجود ندارد. اول فاز ۱ را اجرا کنید.")

    groups, node_confidence, review, stats = resolve(
        candidates,
        threshold=float(config.get("resolve.similarity_threshold", 0.80)),
        top_tokens=int(config.get("resolve.blocking_top_tokens", 3)),
        strong_match_threshold=float(
            config.get("resolve.strong_match_threshold", STRONG_MATCH_THRESHOLD)
        ),
        no_entity_threshold=float(
            config.get("resolve.no_entity_threshold", NO_ENTITY_THRESHOLD)
        ),
    )

    used_titles: set[str] = set()
    with db.transaction(conn):
        for group in groups:
            members = [candidates[i] for i in group]
            title = _unique_title(pick_canonical_title(members, norm_config), members, used_titles)
            used_titles.add(title)

            tokens: list[str] = []
            for member in members:
                for token in member.entity.tokens:
                    if token not in tokens:
                        tokens.append(token)

            # خوشه‌ای که واقعاً ادغام شده، شواهد قوی داشته؛ ابهامِ قبلیِ اعضایش
            # با همان ادغام رفع شده است و دیگر لازم نیست به شیت C برود.
            flagged = bool(review & set(group)) and len(group) == 1
            if len(group) == 1:
                confidence = REVIEW_CONFIDENCE if flagged else 1.0
            else:
                confidence = min(node_confidence.get(i, HIGH_CONFIDENCE) for i in group)
            if flagged:
                stats.needs_review += 1

            canonical_id = db.insert_canonical(
                conn, run_id, title, tokens, round(confidence, 4), flagged
            )
            for member in members:
                db.map_raw_to_canonical(conn, member.raw_id, canonical_id)

    path = dump_decisions(stats, candidates, config, run_id)
    if verbose and path:
        print(f"  لاگ تصمیم‌های ادغام: {path}")
    return stats


def _unique_title(title: str, members: Sequence[Candidate], used: set[str]) -> str:
    """جلوگیری از تخطی ``UNIQUE(run_id, canonical_title)`` به‌شکل قابل‌پیش‌بینی."""
    if title not in used:
        return title
    tokens = " ".join(sorted({t for m in members for t in m.entity.tokens}))
    if tokens and f"{title} {tokens}" not in used:
        return f"{title} {tokens}"
    index = 2
    while f"{title} #{index}" in used:
        index += 1
    return f"{title} #{index}"


def dump_decisions(
    stats: ResolveStats, candidates: Sequence[Candidate], config: Config, run_id: str
) -> str:
    """ثبت تصمیم‌های فاز ۲ برای ممیزی دستی — ادغام باید قابل ردیابی باشد."""
    target_dir = Path(config.db_path).parent / "review"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{run_id}_merge_decisions.json"
    payload = [
        {
            "a": candidates[d.a].raw_title,
            "b": candidates[d.b].raw_title,
            "score": d.score,
            "verdict": d.verdict,
            "confidence": d.confidence,
            "reason": d.reason,
        }
        for d in stats.decisions
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
