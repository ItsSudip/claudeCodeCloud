"""AI filtering & analysis engine.

Pipeline per run:

1. Load raw items not yet evaluated against the current profile.
2. Pre-flag near-duplicate titles within the batch and against recent digests (cheap, no LLM).
3. Send items to the LLM in batches; receive a 1-10 relevance score, theme, summary, takeaways
   and quality flags for each.
4. Compute ``final_score`` = ``relevance_score × theme_weight + engagement_boost × engagement``
   minus penalties for clickbait / outrage / repetition, and persist a :class:`FilteredItem`.

The final score (not the raw LLM score) decides what goes into the digest. See README
"How scoring works" for the full rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher

from feed_filter.db.models import FilteredItem, RawItem
from feed_filter.db.repository import Repository
from feed_filter.providers.base import normalize_title
from feed_filter.services.llm import Evaluation, ItemForEvaluation, LLMClient, engagement_signal
from feed_filter.services.profile import InterestProfileSpec

log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.88


@dataclass
class FilterStats:
    evaluated: int = 0
    kept: int = 0
    clickbait: int = 0
    outrage: int = 0
    repetitive: int = 0
    batches: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


def _is_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.5:
        return False
    return SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD


def detect_repeats(items: list[RawItem], recent_titles: list[str]) -> set[int]:
    """Return ids of items whose titles repeat a recent digest title or an earlier batch item.

    Within the batch, the item with the highest engagement wins; the others are flagged.
    """
    recent = [normalize_title(t) for t in recent_titles]
    ordered = sorted(items, key=lambda i: (i.score or 0) + 2 * (i.comments or 0), reverse=True)
    kept: list[str] = []
    repeats: set[int] = set()
    for item in ordered:
        title = normalize_title(item.title)
        if any(_is_similar(title, r) for r in recent) or any(_is_similar(title, k) for k in kept):
            repeats.add(item.id)
        else:
            kept.append(title)
    return repeats


def compute_final_score(
    evaluation: Evaluation, item: RawItem, profile: InterestProfileSpec
) -> float:
    settings = profile.settings
    theme = profile.canonical_theme(evaluation.theme)
    score = evaluation.relevance_score * profile.theme_weight(theme)
    score += settings.engagement_boost * engagement_signal(item.score, item.comments)
    if evaluation.is_clickbait:
        score -= settings.clickbait_penalty
    if evaluation.is_outrage:
        score -= settings.outrage_penalty
    if evaluation.is_repetitive:
        score -= settings.repetitive_penalty
    return round(max(0.0, min(10.0, score)), 2)


def to_eval_item(item: RawItem) -> ItemForEvaluation:
    return ItemForEvaluation(
        ref=str(item.id),
        title=item.title,
        source=item.source_name or item.provider,
        url=item.url,
        author=item.author,
        body=item.body,
        score=item.score,
        comments=item.comments,
    )


class FilterEngine:
    def __init__(
        self,
        repo: Repository,
        llm: LLMClient,
        profile: InterestProfileSpec,
        *,
        profile_id: int,
        batch_size: int = 12,
        max_items: int = 200,
    ):
        self.repo = repo
        self.llm = llm
        self.profile = profile
        self.profile_id = profile_id
        self.batch_size = max(1, batch_size)
        self.max_items = max_items

    def run(self, items: list[RawItem] | None = None) -> FilterStats:
        stats = FilterStats()
        if items is None:
            items = self.repo.unfiltered_items(self.profile_id, limit=self.max_items)
        if not items:
            log.info("filter: nothing new to evaluate")
            return stats

        recent_titles = self.repo.recent_titles(exclude_ids=[i.id for i in items])
        repeats = detect_repeats(items, recent_titles)
        log.info(
            "filter: evaluating %d items with %s (%d pre-flagged as repeats)",
            len(items),
            self.llm.model_name,
            len(repeats),
        )

        for start in range(0, len(items), self.batch_size):
            batch = items[start : start + self.batch_size]
            evaluations = self.llm.evaluate_batch(
                self.profile, [to_eval_item(i) for i in batch], recent_titles
            )
            stats.batches += 1
            by_ref = {e.ref: e for e in evaluations}
            filtered: list[FilteredItem] = []
            for item in batch:
                evaluation = by_ref.get(str(item.id))
                if evaluation is None:  # defensive; parse_evaluations normally guarantees this
                    evaluation = Evaluation(
                        ref=str(item.id),
                        relevance_score=1,
                        theme="Saved for Later",
                        summary=item.title,
                    )
                if item.id in repeats:
                    evaluation.is_repetitive = True
                theme = self.profile.canonical_theme(evaluation.theme)
                final = compute_final_score(evaluation, item, self.profile)
                row = FilteredItem(
                    raw_item_id=item.id,
                    profile_id=self.profile_id,
                    relevance_score=evaluation.relevance_score,
                    final_score=final,
                    theme=theme,
                    summary=evaluation.summary or item.title,
                    takeaways=evaluation.takeaways,
                    is_clickbait=evaluation.is_clickbait,
                    is_outrage=evaluation.is_outrage,
                    is_repetitive=evaluation.is_repetitive,
                    save_for_later=evaluation.save_for_later,
                    reasoning=evaluation.reasoning,
                    model=self.llm.model_name,
                )
                filtered.append(row)
                stats.evaluated += 1
                stats.clickbait += int(row.is_clickbait)
                stats.outrage += int(row.is_outrage)
                stats.repetitive += int(row.is_repetitive)
                if not row.excluded and final >= self.profile.settings.min_score:
                    stats.kept += 1
            self.repo.add_filtered(filtered)
            self.repo.session.commit()
        log.info("filter: done - %s", stats.as_dict())
        return stats
