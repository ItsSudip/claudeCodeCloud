"""Mood-matched sessions: "I have 10 minutes and I'm feeling X, what should I open?"

A session is a time-boxed, ordered playlist of already-scored items that fit a reader mood
(see :mod:`feed_filter.services.moods`). No scrolling: every entry says what it is, how long it
takes and why it is there. Sessions are stored as reports of kind ``session`` so the dashboard
and the CLI can show them like digests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from feed_filter.db.models import FilteredItem, Report
from feed_filter.db.repository import Repository
from feed_filter.services.moods import MoodPreset, estimate_minutes
from feed_filter.services.profile import InterestProfileSpec

log = logging.getLogger(__name__)

MIN_SESSION_MINUTES = 5
MAX_SESSION_MINUTES = 20
OVERSHOOT = 1.15  # may exceed the target by this factor
UNDERSHOOT = 0.9  # stop packing once this fraction of the target is reached


@dataclass
class SessionEntry:
    item: FilteredItem
    minutes: float
    fit: float  # ranking score used to pick it
    starts_at: float = 0.0  # running clock, minutes


@dataclass
class SessionPlan:
    mood: MoodPreset
    target_minutes: int
    entries: list[SessionEntry] = field(default_factory=list)
    considered: int = 0
    matched: int = 0

    @property
    def total_minutes(self) -> float:
        return round(sum(e.minutes for e in self.entries), 1)

    @property
    def short(self) -> bool:
        return self.total_minutes < self.target_minutes * UNDERSHOOT


def item_minutes(item: FilteredItem) -> float:
    if item.estimated_minutes and item.estimated_minutes > 0:
        return float(item.estimated_minutes)
    raw = item.raw_item
    return estimate_minutes(
        kind=item.content_kind or raw.media_type or "other",
        body=raw.body,
        duration_seconds=raw.duration_seconds,
    )


def mood_fit(item: FilteredItem, preset: MoodPreset) -> float | None:
    """Ranking score for ``item`` under ``preset``; ``None`` means it does not fit."""
    moods = set(item.moods or [])
    if moods & set(preset.exclude):
        return None
    if preset.name != "any":
        if not moods:
            return None
        overlap = len(moods & set(preset.include))
        if overlap == 0:
            return None
    else:
        overlap = 1
    minutes = item_minutes(item)
    if preset.max_item_minutes and minutes > preset.max_item_minutes:
        return None
    fit = item.final_score + 1.0 * overlap
    if preset.prefer_kinds and (item.content_kind or "") in preset.prefer_kinds:
        fit += 0.5
    return round(fit, 3)


def pack(
    candidates: list[tuple[FilteredItem, float]],
    target_minutes: int,
    *,
    max_per_source: int = 3,
) -> list[SessionEntry]:
    """Greedy knapsack: best fit first, keep sources varied, stop near the target."""
    budget = target_minutes * OVERSHOOT
    enough = target_minutes * UNDERSHOOT
    ordered = sorted(candidates, key=lambda c: (-c[1], c[0].id))
    chosen: list[SessionEntry] = []
    per_source: dict[str, int] = {}
    total = 0.0
    for item, fit in ordered:
        if total >= enough:
            break
        minutes = item_minutes(item)
        if total + minutes > budget:
            continue
        source = item.raw_item.source_name or item.raw_item.provider
        if per_source.get(source, 0) >= max_per_source:
            continue
        per_source[source] = per_source.get(source, 0) + 1
        chosen.append(SessionEntry(item=item, minutes=round(minutes, 2), fit=fit))
        total += minutes
    # Open with the strongest piece, then alternate short and long so attention doesn't flag.
    chosen.sort(key=lambda e: -e.fit)
    if len(chosen) > 2:
        lead, rest = chosen[0], chosen[1:]
        shorts = sorted([e for e in rest if e.minutes <= 2], key=lambda e: -e.fit)
        longs = sorted([e for e in rest if e.minutes > 2], key=lambda e: -e.fit)
        mixed: list[SessionEntry] = []
        while shorts or longs:
            if shorts:
                mixed.append(shorts.pop(0))
            if longs:
                mixed.append(longs.pop(0))
        chosen = [lead] + mixed
    clock = 0.0
    for entry in chosen:
        entry.starts_at = round(clock, 1)
        clock += entry.minutes
    return chosen


def _clock(minutes: float) -> str:
    total = int(round(minutes * 60))
    return f"{total // 60}:{total % 60:02d}"


def render_session_markdown(plan: SessionPlan, *, built_at: datetime | None = None) -> str:
    built_at = built_at or datetime.now(UTC)
    mood = plan.mood
    lines = [f"# {plan.target_minutes}-minute session · {mood.name.replace('_', ' ')}", ""]
    lines.append(
        f"_Built {built_at.strftime('%Y-%m-%d %H:%M UTC')} · {len(plan.entries)} items · "
        f"~{plan.total_minutes:g} min · {mood.description}_"
    )
    lines.append("")
    if not plan.entries:
        lines += [
            "_Nothing scored recently fits this mood. Run `python main.py --digest` to collect "
            "and score fresh content, or try `--mood any`._",
            "",
        ]
    for index, entry in enumerate(plan.entries, start=1):
        raw = entry.item.raw_item
        source = raw.source_name or raw.provider
        kind = (entry.item.content_kind or raw.media_type or "post").replace("_", " ")
        title = f"[{raw.title}]({raw.url})" if raw.url else raw.title
        meta = [f"~{entry.minutes:g} min", kind, source]
        if raw.author and raw.author not in source:
            meta.append(raw.author)
        if entry.item.moods:
            meta.append(", ".join(m.replace("_", " ") for m in entry.item.moods))
        lines.append(f"{index}. **{title}**  ")
        lines.append(f"   `{_clock(entry.starts_at)}` · " + " · ".join(meta) + "  ")
        if entry.item.summary:
            lines.append(f"   {entry.item.summary.strip()}")
        lines.append("")
    if plan.entries:
        note = ""
        if plan.short:
            note = (
                " Shorter than requested: not enough fresh content matched this mood "
                "(widen your sources or try another mood)."
            )
        lines.append(
            f"**Total ≈ {plan.total_minutes:g} min of {plan.target_minutes} requested** · "
            f"{plan.matched} of {plan.considered} recent items matched the mood.{note}"
        )
        lines.append("")
    lines += ["---", "_The Feed Filter · session built from your scored feed, no scrolling needed_"]
    return "\n".join(lines).rstrip() + "\n"


class SessionBuilder:
    def __init__(
        self,
        repo: Repository,
        profile: InterestProfileSpec,
        *,
        profile_id: int,
        min_score: float | None = None,
    ):
        self.repo = repo
        self.profile = profile
        self.profile_id = profile_id
        # Sessions are about mood fit, so they use their own (lower) relevance bar.
        self.min_score = min_score if min_score is not None else profile.settings.session_min_score

    def plan(
        self,
        mood: str,
        minutes: int,
        *,
        lookback_days: int | None = None,
        allow_repeats: bool = False,
    ) -> SessionPlan:
        preset = self.profile.moods.get(mood)
        minutes = int(max(1, min(minutes, 240)))
        days = lookback_days or max(self.profile.settings.digest_days, 3)
        since = datetime.now(UTC) - timedelta(days=days)
        candidates = self.repo.top_filtered(self.profile_id, since=since, min_score=self.min_score)
        used: set[int] = set() if allow_repeats else self.repo.session_item_ids(since=since)

        plan = SessionPlan(mood=preset, target_minutes=minutes, considered=len(candidates))
        fitting: list[tuple[FilteredItem, float]] = []
        for item in candidates:
            if item.raw_item_id in used:
                continue
            fit = mood_fit(item, preset)
            if fit is not None:
                fitting.append((item, fit))
        plan.matched = len(fitting)
        plan.entries = pack(fitting, minutes)
        log.info(
            "session: mood=%s target=%dmin -> %d items, %.1f min (%d/%d matched)",
            preset.name,
            minutes,
            len(plan.entries),
            plan.total_minutes,
            plan.matched,
            plan.considered,
        )
        return plan

    def build(
        self,
        mood: str,
        minutes: int,
        *,
        lookback_days: int | None = None,
        allow_repeats: bool = False,
    ) -> Report:
        plan = self.plan(mood, minutes, lookback_days=lookback_days, allow_repeats=allow_repeats)
        now = datetime.now(UTC)
        markdown = render_session_markdown(plan, built_at=now)
        report = Report(
            report_date=now.date().isoformat(),
            title=(
                f"{plan.target_minutes}-min {plan.mood.name} session · {now.strftime('%H:%M')} UTC"
            ),
            markdown=markdown,
            profile_id=self.profile_id,
            item_count=len(plan.entries),
            kind="session",
            mood=plan.mood.name,
            target_minutes=plan.target_minutes,
            total_minutes=plan.total_minutes,
            stats={
                "item_ids": [e.item.raw_item_id for e in plan.entries],
                "considered": plan.considered,
                "matched": plan.matched,
                "short": plan.short,
            },
        )
        self.repo.add_report(report)
        self.repo.session.commit()
        return report
