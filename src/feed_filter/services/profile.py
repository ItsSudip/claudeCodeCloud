"""Parse the structured ``interest_profile.md`` into a typed :class:`InterestProfileSpec`.

The markdown format is intentionally simple so it stays hand-editable::

    # Interest Profile
    ## About Me
    Free text that is passed verbatim to the LLM.

    ## Themes
    ### Tech Breakthroughs (weight: 1.0)
    keywords: llm, open source, compilers
    Free-text description of what belongs here.

    ## Avoid
    - outrage bait
    - celebrity news

    ## Moods
    happy: feel_good, funny, inspiring | exclude: intense
    deep: practical, curious

    ## Settings
    min_score: 6
    max_items_per_theme: 5
    engagement_boost: 0.5
    digest_days: 1

Anything not recognized is ignored, so you can add notes freely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from feed_filter.services.moods import MoodConfig

_THEME_HEADING_RE = re.compile(
    r"^###\s+(?P<name>[^(]+?)\s*(?:\(\s*weight\s*[:=]\s*(?P<weight>[0-9.]+)\s*\))?\s*$"
)
_SECTION_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")
_KV_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_ ]*?)\s*[:=]\s*(?P<value>.+?)\s*$")

DEFAULT_THEMES = ["Tech Breakthroughs", "Business Insights", "Saved for Later"]


@dataclass
class Theme:
    name: str
    weight: float = 1.0
    keywords: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class ProfileSettings:
    min_score: float = 6.0
    max_items_per_theme: int = 5
    max_items_total: int = 20
    engagement_boost: float = 0.5
    clickbait_penalty: float = 10.0
    outrage_penalty: float = 10.0
    repetitive_penalty: float = 10.0
    digest_days: int = 1
    save_for_later_min_score: float = 5.0
    session_min_score: float = 4.0

    _INT_FIELDS = {"max_items_per_theme", "max_items_total", "digest_days"}

    def apply(self, key: str, value: str) -> None:
        key = key.strip().lower().replace(" ", "_")
        if not hasattr(self, key) or key.startswith("_"):
            return
        try:
            setattr(self, key, int(value) if key in self._INT_FIELDS else float(value))
        except ValueError:
            pass


@dataclass
class InterestProfileSpec:
    about: str = ""
    themes: list[Theme] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    settings: ProfileSettings = field(default_factory=ProfileSettings)
    moods: MoodConfig = field(default_factory=MoodConfig)
    raw_markdown: str = ""

    def theme_weight(self, theme_name: str) -> float:
        for theme in self.themes:
            if theme.name.lower() == theme_name.strip().lower():
                return theme.weight
        return 1.0

    def theme_names(self) -> list[str]:
        return [t.name for t in self.themes]

    def canonical_theme(self, theme_name: str | None) -> str:
        """Map a free-text theme from the LLM onto a configured theme name (case-insensitive)."""
        if not theme_name:
            return "Saved for Later"
        wanted = theme_name.strip().lower()
        for theme in self.themes:
            if theme.name.lower() == wanted:
                return theme.name
        for theme in self.themes:
            if wanted in theme.name.lower() or theme.name.lower() in wanted:
                return theme.name
        return "Saved for Later"

    def to_prompt_text(self) -> str:
        """Render the profile in the form shown to the LLM."""
        lines = ["# Interest Profile", ""]
        if self.about:
            lines += ["## About the reader", self.about.strip(), ""]
        lines.append("## Themes (score items against these)")
        for theme in self.themes:
            lines.append(f"### {theme.name} (weight {theme.weight:g})")
            if theme.keywords:
                lines.append("Keywords: " + ", ".join(theme.keywords))
            if theme.description:
                lines.append(theme.description.strip())
            lines.append("")
        if self.avoid:
            lines.append("## Not interested in / actively avoid")
            lines += [f"- {a}" for a in self.avoid]
        return "\n".join(lines).strip()


def parse_profile(markdown: str) -> InterestProfileSpec:
    spec = InterestProfileSpec(raw_markdown=markdown)
    section: str | None = None
    current_theme: Theme | None = None
    about_lines: list[str] = []

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group("name").strip().lower()
            current_theme = None
            continue
        if line.startswith("# "):
            continue

        if section in {"themes", "theme"}:
            heading = _THEME_HEADING_RE.match(line)
            if heading:
                current_theme = Theme(
                    name=heading.group("name").strip(),
                    weight=float(heading.group("weight") or 1.0),
                )
                spec.themes.append(current_theme)
                continue
            if current_theme is None or not line.strip():
                continue
            kv = _KV_RE.match(line)
            if kv and kv.group("key").strip().lower() in {"keywords", "keyword", "tags"}:
                current_theme.keywords.extend(
                    k.strip() for k in kv.group("value").split(",") if k.strip()
                )
            else:
                current_theme.description = (
                    current_theme.description + "\n" + line.strip()
                ).strip()
        elif section in {"about me", "about", "who i am"}:
            about_lines.append(line)
        elif section in {"avoid", "not interested", "exclude", "filters"}:
            stripped = line.strip()
            if stripped.startswith(("-", "*")):
                spec.avoid.append(stripped.lstrip("-* ").strip())
        elif section in {"settings", "config", "configuration"}:
            kv = _KV_RE.match(line.lstrip("-* "))
            if kv:
                spec.settings.apply(kv.group("key"), kv.group("value"))
        elif section in {"moods", "mood"}:
            kv = _KV_RE.match(line.lstrip("-* "))
            if kv:
                include_part, _, exclude_part = kv.group("value").partition("|")
                include = [m.strip() for m in include_part.split(",") if m.strip()]
                exclude = None
                if exclude_part.strip():
                    exclude = [
                        m.strip()
                        for m in exclude_part.replace("exclude:", "").split(",")
                        if m.strip()
                    ]
                spec.moods.override(kv.group("key"), include, exclude)

    spec.about = "\n".join(about_lines).strip()
    if not spec.themes:
        spec.themes = [Theme(name=name) for name in DEFAULT_THEMES]
    if not any(t.name.lower() == "saved for later" for t in spec.themes):
        spec.themes.append(
            Theme(
                name="Saved for Later",
                weight=0.6,
                description="Interesting but not urgent; worth a deeper read later.",
            )
        )
    return spec


def load_profile(path: Path) -> InterestProfileSpec:
    if not path.exists():
        raise FileNotFoundError(
            f"Interest profile not found at {path}. "
            "Copy interest_profile.example.md to get started."
        )
    return parse_profile(path.read_text(encoding="utf-8"))
