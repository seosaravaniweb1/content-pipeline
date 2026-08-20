"""تصمیم‌گیری بین چند منبع (فاز ۵).

:mod:`core.details` می‌گوید «این صفحه چه گفت». اینجا از چند صفحه‌ی مختلف یک
مقدار درمی‌آید و — مهم‌تر از خودِ مقدار — معلوم می‌شود **چند منبع** موافقش
بوده‌اند. همان عدد موافقت است که در گزارش می‌نشیند و کاربر با آن می‌فهمد کدام
ردیف را باید دستی چک کند.

قواعد کلی:

* هر منبع **یک رأی** دارد. یک سایت که سه جا نوشته «۴۰۰ صفحه» یک رأی است.
* رأی روی شکل نرمال‌شده شمرده می‌شود («آوا محمدی» و «آوا محمّدی» یکی‌اند).
* در تساوی، مقدار کامل‌تر (بلندتر) برنده است، نه اولی.
* هیچ‌وقت حدس نمی‌زنیم: نبودن شاهد یعنی خروجی خالی، نه مقدار پیش‌فرض.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable, Sequence

from . import normalizer, similarity
from .details import PageDetails, SummaryBlock, is_latin_name, label_key


@dataclass
class Decision:
    """یک مقدار نهایی، همراه با اینکه از کجا و با چند رأی آمده."""

    value: str = ""
    votes: int = 0
    sources: int = 0
    note: str = ""

    @property
    def filled(self) -> bool:
        return bool(str(self.value).strip())

    def as_dict(self) -> dict:
        return {"value": self.value, "votes": self.votes, "sources": self.sources, "note": self.note}


@dataclass
class MergedDetails:
    """خروجی ادغام همه‌ی منابع یک عنوان."""

    author: Decision = field(default_factory=Decision)
    translator: Decision = field(default_factory=Decision)
    summary: Decision = field(default_factory=Decision)
    pages: Decision = field(default_factory=Decision)
    nationality: Decision = field(default_factory=Decision)
    book_format: Decision = field(default_factory=Decision)
    #: برچسب‌های خام منابع، مرتب بر اساس رأی — ورودی :mod:`core.taxonomy`
    terms: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def evidence(self) -> dict:
        return {
            "author": self.author.as_dict(),
            "translator": self.translator.as_dict(),
            "summary": self.summary.as_dict(),
            "pages": self.pages.as_dict(),
            "nationality": self.nationality.as_dict(),
            "format": self.book_format.as_dict(),
            "source_count": len(self.sources),
        }


# ---------------------------------------------------------------------------
# اشخاص
# ---------------------------------------------------------------------------


def merge_persons(
    per_source: Sequence[Sequence[str]], combine_scripts: bool = True
) -> Decision:
    """نام نویسنده/مترجم مورد توافق منابع.

    اگر هم شکل فارسی و هم شکل لاتین نام رأی داشته باشند، هر دو نوشته می‌شوند
    («ریتا کنت (Rina Kent)») — این همان چیزی است که در شیت‌های واقعی می‌آید و
    برای رمان‌های ترجمه هر دو شکل جستجو می‌شوند.
    """
    votes: Counter[str] = Counter()
    display: dict[str, str] = {}
    for names in per_source:
        for name in dict.fromkeys(names):  # هر منبع، یک رأی برای هر نام
            key = label_key(name)
            if not key:
                continue
            votes[key] += 1
            # نمایش کامل‌تر می‌ماند: «آوا محمدی» به «آوا» می‌چربد
            if key not in display or len(name) > len(display[key]):
                display[key] = name
    if not votes:
        return Decision()

    def rank(item: tuple[str, int]) -> tuple[int, int]:
        key, count = item
        return (count, len(display[key]))

    persian = {k: v for k, v in votes.items() if not is_latin_name(display[k])}
    latin = {k: v for k, v in votes.items() if is_latin_name(display[k])}
    pool = persian or latin
    best_key, best_votes = max(pool.items(), key=rank)
    value = display[best_key]

    if combine_scripts and persian and latin:
        latin_key, _ = max(latin.items(), key=rank)
        value = f"{display[best_key]} ({display[latin_key]})"
    return Decision(value=value, votes=best_votes, sources=len(per_source))


# ---------------------------------------------------------------------------
# تعداد صفحات
# ---------------------------------------------------------------------------


def merge_pages(
    per_source: Sequence[Sequence[int]],
    tolerance: int = 10,
    min_agreement: int = 2,
    accept_single: bool = True,
) -> Decision:
    """عددی که بیشترین منبع رویش توافق دارند.

    مسئله‌ی واقعی: یکی دو صفحه تبلیغ به فایل اضافه کرده و عددش با بقیه چند
    واحد فرق دارد. پس عددها با تلورانس خوشه می‌شوند و بزرگ‌ترین خوشه برنده
    است؛ مقدار نهایی، میانه‌ی همان خوشه (نه میانگین، تا عدد پرت خوشه را جابه‌جا
    نکند). اگر فقط یک منبع عدد داده باشد، با ``accept_single`` نوشته می‌شود
    ولی در گزارش «یک منبع» علامت می‌خورد تا دستی چک شود.
    """
    firsts: list[int] = []
    for values in per_source:
        for value in values[:1]:  # عدد اصلی هر منبع
            firsts.append(int(value))
    if not firsts:
        return Decision()

    clusters: list[list[int]] = []
    for value in sorted(firsts):
        if clusters and value - clusters[-1][0] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])

    best = max(clusters, key=lambda group: (len(group), -abs(median(group))))
    agreement = len(best)
    value = int(median(best))
    if agreement < min_agreement and not accept_single:
        return Decision(note=f"فقط {agreement} منبع عدد داد")
    note = "" if agreement >= min_agreement else "تک‌منبعی — دستی چک شود"
    if len(clusters) > 1:
        spread = max(firsts) - min(firsts)
        note = (note + f" | اختلاف منابع: {spread} صفحه").strip(" |")
    return Decision(value=str(value), votes=agreement, sources=len(firsts), note=note)


# ---------------------------------------------------------------------------
# خلاصه
# ---------------------------------------------------------------------------

#: جمله‌ای که این‌قدر به جمله‌ی پذیرفته‌شده شبیه باشد، حرف تازه‌ای ندارد
NOVELTY_THRESHOLD = 0.62
MIN_SENTENCE_CHARS = 40


def merge_summary(
    per_source: Sequence[Sequence[SummaryBlock]],
    max_chars: int = 1200,
    min_chars: int = 200,
    max_sentences: int = 14,
    novelty: float = NOVELTY_THRESHOLD,
) -> Decision:
    """خلاصه‌ی جامع از همه‌ی منابع.

    خواسته‌ی اصلی همین است: «یکی یک جای رمان را گفته و دیگری جای دیگر را».
    پس ستون خلاصه با کپی یک منبع پر نمی‌شود؛ کامل‌ترین متن به‌عنوان ستون فقرات
    برداشته می‌شود و بعد از منابع دیگر فقط جمله‌هایی اضافه می‌شوند که **حرف
    تازه** دارند (شباهتشان به جمله‌های پذیرفته‌شده کم است). چون منبع‌ها متن
    همدیگر را کپی می‌کنند، بدون این فیلتر خروجی سه‌بار یک پاراگراف می‌شود.
    """
    # پاراگراف‌های هر منبع به ترتیب خودشان می‌مانند (وگرنه وسط داستان اول
    # می‌آید و اولش وسط)، ولی خودِ منبع‌ها بر اساس اعتماد و کامل بودن مرتب
    # می‌شوند: کامل‌ترین متن، ستون فقرات خلاصه است.
    ordered_sources: list[tuple[tuple[int, int], int, list[SummaryBlock]]] = []
    for index, blocks in enumerate(per_source):
        usable = [block for block in blocks if (block.text or "").strip()]
        if not usable:
            continue
        score = (max(int(block.rank) for block in usable), sum(len(b.text) for b in usable))
        ordered_sources.append(((-score[0], -score[1]), index, usable))
    if not ordered_sources:
        return Decision()
    ordered_sources.sort(key=lambda item: item[0])

    documents = [
        (int(block.rank), block.text.strip(), index)
        for _, index, blocks in ordered_sources
        for block in blocks
    ]

    from .details import sentences as split_sentences

    kept: list[str] = []
    kept_keys: list[str] = []
    used_sources: set[int] = set()
    total = 0

    for rank, text, source_index in documents:
        if total >= max_chars or len(kept) >= max_sentences:
            break
        for sentence in split_sentences(text):
            if total >= max_chars or len(kept) >= max_sentences:
                break
            sentence = sentence.strip()
            if len(sentence) < MIN_SENTENCE_CHARS:
                continue
            # مقایسه روی شکل نرمال‌شده‌ی *باکلمه* انجام می‌شود، نه کلید فشرده:
            # دو جمله با ترتیب متفاوت هم باید تکراری شمرده شوند.
            key = normalizer.normalize(sentence)
            if any(
                similarity.token_set_ratio(key, existing) >= novelty for existing in kept_keys
            ):
                continue  # همین حرف را یک منبع دیگر زده
            kept.append(sentence)
            kept_keys.append(key)
            used_sources.add(source_index)
            total += len(sentence) + 1

    summary = " ".join(kept).strip()
    if len(summary) < min_chars and summary:
        note = "کوتاه‌تر از حد انتظار — منابع خلاصه‌ی کاملی نداشتند"
    else:
        note = ""
    return Decision(
        value=summary,
        votes=len(used_sources),
        sources=len(per_source),
        note=note,
    )


# ---------------------------------------------------------------------------
# ملیت و فرمت
# ---------------------------------------------------------------------------


def merge_nationality(
    hints: Iterable[str], has_translator: bool = False, foreign_author: bool = False
) -> Decision:
    """``foreign`` / ``iranian`` / خالی.

    مترجم داشتن و لاتین بودن نام نویسنده، شاهدِ ساختاری‌اند و بر رأی‌گیری
    برچسب سایت‌ها می‌چربند: سایتی که همه‌ی رمان‌ها را «ایرانی» زده باشد
    نمی‌تواند رمانی که مترجم دارد را ایرانی کند.
    """
    counter = Counter(hint for hint in hints if hint)
    if has_translator or foreign_author:
        return Decision(
            value="foreign",
            votes=max(1, counter.get("foreign", 0)),
            sources=sum(counter.values()),
            note="مترجم/نام لاتین" if not counter.get("foreign") else "",
        )
    if not counter:
        return Decision(note="هیچ منبعی ملیت را مشخص نکرده بود")
    value, votes = counter.most_common(1)[0]
    return Decision(value=value, votes=votes, sources=sum(counter.values()))


def merge_formats(per_source: Sequence[Sequence[str]], default: str = "pdf") -> Decision:
    """فرمت‌های موجود: pdf همیشه، «صوتی» فقط اگر منبعی صریحاً گفته باشد."""
    counter: Counter[str] = Counter()
    for formats in per_source:
        for value in dict.fromkeys(formats):
            counter[value] += 1
    chosen = [key for key in ("pdf", "audio") if counter.get(key)]
    if default and default not in chosen:
        chosen.insert(0, default)
    return Decision(
        value=" | ".join(chosen),
        votes=max(counter.values()) if counter else 0,
        sources=len(per_source),
    )


# ---------------------------------------------------------------------------
# برچسب‌ها
# ---------------------------------------------------------------------------


def merge_terms(per_source: Sequence[Sequence[str]], limit: int = 40) -> list[str]:
    """تگ/دسته‌های خام منابع، مرتب‌شده بر اساس تعداد منبع موافق."""
    votes: Counter[str] = Counter()
    display: dict[str, str] = {}
    for terms in per_source:
        for term in dict.fromkeys(terms):
            key = label_key(term)
            if not key:
                continue
            votes[key] += 1
            display.setdefault(key, term)
    ordered = sorted(votes.items(), key=lambda item: (-item[1], display[item[0]]))
    return [display[key] for key, _ in ordered[:limit]]


# ---------------------------------------------------------------------------
# ادغام کامل
# ---------------------------------------------------------------------------


def merge_pages_options(settings: dict | None = None) -> dict:
    settings = settings or {}
    return {
        "tolerance": int(settings.get("tolerance", 10)),
        "min_agreement": int(settings.get("min_agreement", 2)),
        "accept_single": bool(settings.get("accept_single", True)),
    }


def merge(
    pages_of_sources: Sequence[PageDetails],
    summary_options: dict | None = None,
    pages_options: dict | None = None,
    combine_scripts: bool = True,
) -> MergedDetails:
    """همه‌ی صفحه‌های یک عنوان → یک ردیف آماده‌ی نوشتن در شیت."""
    summary_options = summary_options or {}
    merged = MergedDetails(sources=[page.url for page in pages_of_sources if page.url])

    merged.author = merge_persons(
        [page.authors for page in pages_of_sources], combine_scripts=combine_scripts
    )
    merged.translator = merge_persons(
        [page.translators for page in pages_of_sources], combine_scripts=combine_scripts
    )
    merged.pages = merge_pages(
        [page.page_counts for page in pages_of_sources], **merge_pages_options(pages_options)
    )
    merged.summary = merge_summary(
        [page.summaries for page in pages_of_sources],
        max_chars=int(summary_options.get("max_chars", 1200)),
        min_chars=int(summary_options.get("min_chars", 200)),
        max_sentences=int(summary_options.get("max_sentences", 14)),
    )
    merged.nationality = merge_nationality(
        [page.nationality for page in pages_of_sources],
        has_translator=merged.translator.filled,
        foreign_author=any(
            is_latin_name(name) for page in pages_of_sources for name in page.authors
        ),
    )
    merged.book_format = merge_formats([page.formats for page in pages_of_sources])
    merged.terms = merge_terms(
        [[*page.tags, *page.categories] for page in pages_of_sources]
    )
    return merged
