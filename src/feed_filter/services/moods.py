"""Mood vocabulary and time estimation.

Every scored item is tagged with one or more *content moods* (what the item does to you) and an
estimated consumption time. A *reader mood* ("happy", "tired", ...) maps to the content moods
that fit it, so a session can be packed for how you feel right now instead of what the
algorithm wants you to scroll.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# What a piece of content does to the reader. Kept small so the model tags consistently.
CONTENT_MOODS: tuple[str, ...] = (
    "feel_good",  # warm, wholesome, uplifting; leaves you smiling
    "funny",  # humour, memes, comedic takes
    "inspiring",  # achievements, stories of effort paying off, craft
    "calm",  # slow, soothing, nature, ambient, gentle long reads
    "curious",  # explainers, science, "how it works", learning
    "thought_provoking",  # essays, debates, ideas that need chewing on
    "energizing",  # hype, launches, momentum, sports, music
    "practical",  # how-tos, tools, tips you can apply today
    "intense",  # heavy, sad, scary, conflict-laden or stressful
)

# Kinds of content, which drive the default time estimate.
CONTENT_KINDS: tuple[str, ...] = (
    "article",
    "post",
    "thread",
    "short_video",
    "long_video",
    "image",
    "podcast",
    "paper",
    "other",
)

WORDS_PER_MINUTE = 230.0

_KIND_DEFAULT_MINUTES: dict[str, float] = {
    "article": 6.0,
    "post": 1.0,
    "thread": 3.0,
    "short_video": 0.75,
    "long_video": 12.0,
    "image": 0.5,
    "podcast": 40.0,
    "paper": 25.0,
    "other": 2.0,
}


@dataclass(frozen=True)
class MoodPreset:
    """How a reader mood maps onto content moods."""

    name: str
    description: str
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    max_item_minutes: float | None = None  # e.g. tired -> keep items short
    prefer_kinds: tuple[str, ...] = ()


MOOD_PRESETS: dict[str, MoodPreset] = {
    preset.name: preset
    for preset in (
        MoodPreset(
            "happy",
            "Keep the good mood going: wholesome, funny and inspiring things only.",
            include=("feel_good", "funny", "inspiring", "energizing"),
            exclude=("intense",),
        ),
        MoodPreset(
            "sad",
            "Gentle lift: warm and calming content, nothing heavy or provocative.",
            include=("feel_good", "calm", "inspiring"),
            exclude=("intense", "thought_provoking", "energizing"),
        ),
        MoodPreset(
            "tired",
            "Low effort: short, light, soothing items; no deep reads.",
            include=("feel_good", "funny", "calm"),
            exclude=("intense", "thought_provoking"),
            max_item_minutes=4.0,
            prefer_kinds=("short_video", "image", "post"),
        ),
        MoodPreset(
            "curious",
            "Learn something: explainers, research and ideas worth chewing on.",
            include=("curious", "thought_provoking", "inspiring", "practical"),
            exclude=("intense",),
        ),
        MoodPreset(
            "focused",
            "Deep work fuel: practical, substantive material, long form welcome.",
            include=("practical", "curious", "thought_provoking"),
            exclude=("funny", "intense"),
            prefer_kinds=("article", "paper", "thread", "long_video"),
        ),
        MoodPreset(
            "bored",
            "Shake things up: funny, energizing and surprising.",
            include=("funny", "energizing", "curious", "feel_good"),
            exclude=("calm", "intense"),
        ),
        MoodPreset(
            "stressed",
            "Decompress: calm and feel-good, short, nothing that raises the pulse.",
            include=("calm", "feel_good", "funny"),
            exclude=("intense", "energizing", "thought_provoking"),
            max_item_minutes=6.0,
        ),
        MoodPreset(
            "any",
            "No mood filter: the best of everything that cleared the bar.",
            include=CONTENT_MOODS,
        ),
    )
}


@dataclass
class MoodConfig:
    """Mood presets after applying overrides from the interest profile's ``## Moods`` section."""

    presets: dict[str, MoodPreset] = field(default_factory=lambda: dict(MOOD_PRESETS))

    def get(self, name: str) -> MoodPreset:
        key = (name or "any").strip().lower().replace("-", "_").replace(" ", "_")
        if key not in self.presets:
            raise KeyError(f"Unknown mood {name!r}. Known moods: {', '.join(sorted(self.presets))}")
        return self.presets[key]

    def names(self) -> list[str]:
        return sorted(self.presets)

    def override(self, name: str, include: list[str], exclude: list[str] | None = None) -> None:
        """Define or replace a mood preset, e.g. from ``happy: feel_good, funny`` in the profile."""
        key = name.strip().lower().replace("-", "_").replace(" ", "_")
        include_t = tuple(m for m in include if m in CONTENT_MOODS)
        exclude_t = tuple(m for m in (exclude or []) if m in CONTENT_MOODS)
        base = self.presets.get(key)
        self.presets[key] = MoodPreset(
            key,
            base.description if base else f"Custom mood '{key}' from the interest profile.",
            include=include_t or (base.include if base else CONTENT_MOODS),
            exclude=exclude_t if exclude is not None else (base.exclude if base else ()),
            max_item_minutes=base.max_item_minutes if base else None,
            prefer_kinds=base.prefer_kinds if base else (),
        )


def normalize_moods(values: list[str] | None) -> list[str]:
    """Keep only known content moods, deduplicated, in vocabulary order."""
    if not values:
        return []
    wanted = {str(v).strip().lower().replace("-", "_").replace(" ", "_") for v in values}
    return [m for m in CONTENT_MOODS if m in wanted]


def normalize_kind(value: str | None) -> str:
    key = (value or "other").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "video": "short_video",
        "reel": "short_video",
        "reels": "short_video",
        "story": "post",
        "tweet": "post",
        "photo": "image",
        "link": "article",
    }
    key = aliases.get(key, key)
    return key if key in CONTENT_KINDS else "other"


def estimate_minutes(
    *,
    kind: str,
    body: str | None = None,
    duration_seconds: int | None = None,
    model_estimate: float | None = None,
) -> float:
    """Best estimate of how long the item takes to consume.

    Priority: a known media duration, then the model's estimate, then words / 230 wpm for
    text, then a per-kind default.
    """
    kind = normalize_kind(kind)
    if duration_seconds and duration_seconds > 0:
        return round(max(0.25, duration_seconds / 60.0), 2)
    if model_estimate is not None and model_estimate > 0:
        return round(max(0.25, min(float(model_estimate), 240.0)), 2)
    words = len((body or "").split())
    if kind in ("article", "post", "thread", "paper", "other") and words >= 80:
        return round(max(0.5, words / WORDS_PER_MINUTE), 2)
    return _KIND_DEFAULT_MINUTES.get(kind, 2.0)
