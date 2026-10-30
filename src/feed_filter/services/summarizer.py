"""Daily Digest generation.

Takes the top-scoring :class:`FilteredItem` rows, groups them by theme in the order the profile
lists them, drops anything flagged as clickbait / outrage / repetitive, and renders a structured
Markdown report. The report is stored in the database and written to ``reports/``.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from feed_filter.db.models import FilteredItem, Report
from feed_filter.db.repository import Repository
from feed_filter.services.llm import LLMClient
from feed_filter.services.profile import InterestProfileSpec

log = logging.getLogger(__name__)

SAVED_FOR_LATER = "Saved for Later"


@dataclass
class DigestSections:
    themes: OrderedDict[str, list[FilteredItem]] = field(default_factory=OrderedDict)
    saved_for_later: list[FilteredItem] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    below_threshold: int = 0

    @property
    def item_count(self) -> int:
        return sum(len(v) for v in self.themes.values()) + len(self.saved_for_later)

    def all_items(self) -> list[FilteredItem]:
        items = [i for group in self.themes.values() for i in group]
        return items + self.saved_for_later


def select_for_digest(
    candidates: list[FilteredItem], profile: InterestProfileSpec
) -> DigestSections:
    """Partition candidates into themed sections respecting the profile's caps."""
    settings = profile.settings
    sections = DigestSections()
    for name in profile.theme_names():
        if name != SAVED_FOR_LATER:
            sections.themes[name] = []
    sections.excluded = {"clickbait": 0, "outrage": 0, "repetitive": 0}

    ordered = sorted(candidates, key=lambda i: (-i.final_score, i.id))
    seen_hashes: set[str] = set()
    total = 0
    for item in ordered:
        if item.is_clickbait:
            sections.excluded["clickbait"] += 1
            continue
        if item.is_outrage:
            sections.excluded["outrage"] += 1
            continue
        if item.is_repetitive:
            sections.excluded["repetitive"] += 1
            continue
        if item.raw_item.content_hash in seen_hashes:
            sections.excluded["repetitive"] += 1
            continue

        goes_to_later = item.theme == SAVED_FOR_LATER or (
            item.save_for_later and item.final_score < settings.min_score
        )
        if goes_to_later:
            if item.final_score < settings.save_for_later_min_score:
                sections.below_threshold += 1
                continue
            if len(sections.saved_for_later) >= settings.max_items_per_theme:
                continue
            sections.saved_for_later.append(item)
            seen_hashes.add(item.raw_item.content_hash)
            continue

        if item.final_score < settings.min_score:
            sections.below_threshold += 1
            continue
        if total >= settings.max_items_total:
            continue
        bucket = sections.themes.setdefault(item.theme, [])
        if len(bucket) >= settings.max_items_per_theme:
            continue
        bucket.append(item)
        seen_hashes.add(item.raw_item.content_hash)
        total += 1
    return sections


def _fmt_engagement(item: FilteredItem) -> str:
    raw = item.raw_item
    parts = []
    if raw.score is not None:
        parts.append(f"{raw.score:,} points")
    if raw.comments is not None:
        parts.append(f"{raw.comments:,} comments")
    return " · ".join(parts)


def _render_item(item: FilteredItem) -> list[str]:
    raw = item.raw_item
    source = raw.source_name or raw.provider
    title_line = f"### [{raw.title}]({raw.url})" if raw.url else f"### {raw.title}"
    meta = [f"**{source}**"]
    if raw.author:
        meta.append(raw.author)
    engagement = _fmt_engagement(item)
    if engagement:
        meta.append(engagement)
    meta.append(f"relevance {item.relevance_score}/10 · score {item.final_score:g}")
    lines = [title_line, " · ".join(meta), ""]
    if item.summary:
        lines += [item.summary.strip(), ""]
    if item.takeaways:
        lines += [f"- {t}" for t in item.takeaways] + [""]
    discussion = (raw.extra or {}).get("discussion_url")
    if discussion and discussion != raw.url:
        lines += [f"[Discussion]({discussion})", ""]
    return lines


def render_markdown(
    sections: DigestSections,
    *,
    report_date: date,
    profile: InterestProfileSpec,
    overview: str = "",
    stats: dict | None = None,
) -> str:
    lines = [f"# Daily Digest - {report_date.isoformat()}", ""]
    lines.append(
        f"_Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{sections.item_count} items selected_"
    )
    lines.append("")
    if overview:
        lines += ["> " + overview.replace("\n", "\n> "), ""]

    if stats:
        lines += ["## At a glance", ""]
        lines.append(
            f"- Collected: {stats.get('collected', 0)} new items "
            f"({stats.get('duplicates', 0)} duplicates skipped)"
        )
        lines.append(f"- Evaluated: {stats.get('evaluated', 0)} items")
        excluded = sections.excluded
        lines.append(
            f"- Filtered out: {excluded.get('clickbait', 0)} clickbait, "
            f"{excluded.get('outrage', 0)} outrage, {excluded.get('repetitive', 0)} repetitive, "
            f"{sections.below_threshold} below the relevance threshold "
            f"({profile.settings.min_score:g})"
        )
        lines.append("")

    any_theme = False
    for theme, items in sections.themes.items():
        if not items:
            continue
        any_theme = True
        lines += [f"## {theme}", ""]
        for item in items:
            lines += _render_item(item)
    if sections.saved_for_later:
        lines += [f"## {SAVED_FOR_LATER}", ""]
        for item in sections.saved_for_later:
            lines += _render_item(item)
    if not any_theme and not sections.saved_for_later:
        lines += ["_Nothing cleared the bar today. Quiet day, or time to tune the profile._", ""]

    lines += ["---", "_The Feed Filter · relevance scored against your interest profile_"]
    return "\n".join(lines).rstrip() + "\n"


class Summarizer:
    def __init__(
        self,
        repo: Repository,
        profile: InterestProfileSpec,
        *,
        profile_id: int,
        llm: LLMClient | None = None,
        reports_dir: Path | None = None,
    ):
        self.repo = repo
        self.profile = profile
        self.profile_id = profile_id
        self.llm = llm
        self.reports_dir = reports_dir

    def build(
        self,
        *,
        report_date: date | None = None,
        stats: dict | None = None,
        lookback_days: int | None = None,
    ) -> Report:
        report_date = report_date or datetime.now(UTC).date()
        days = lookback_days or self.profile.settings.digest_days
        since = datetime.now(UTC) - timedelta(days=days)
        candidates = self.repo.top_filtered(self.profile_id, since=since, include_excluded=True)
        sections = select_for_digest(candidates, self.profile)

        overview = ""
        if self.llm is not None and sections.item_count:
            highlights = [
                {"title": i.raw_item.title, "theme": i.theme, "summary": i.summary}
                for i in sections.all_items()[:8]
            ]
            try:
                overview = self.llm.compose_overview(self.profile, highlights)
            except Exception as exc:  # the digest is still useful without an intro
                log.warning("summarizer: overview generation failed: %s", exc)

        markdown = render_markdown(
            sections, report_date=report_date, profile=self.profile, overview=overview, stats=stats
        )
        report = Report(
            report_date=report_date.isoformat(),
            title=f"Daily Digest - {report_date.isoformat()}",
            markdown=markdown,
            profile_id=self.profile_id,
            item_count=sections.item_count,
            stats={
                **(stats or {}),
                "excluded": sections.excluded,
                "below_threshold": sections.below_threshold,
                "themes": {k: len(v) for k, v in sections.themes.items()},
                "saved_for_later": len(sections.saved_for_later),
            },
        )
        self.repo.add_report(report)
        self.repo.session.commit()

        if self.reports_dir is not None:
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            path = self.reports_dir / f"{report_date.isoformat()}.md"
            path.write_text(markdown, encoding="utf-8")
            log.info("summarizer: wrote %s", path)
        return report
