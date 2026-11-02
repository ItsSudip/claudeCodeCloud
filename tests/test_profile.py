from __future__ import annotations

import pytest

from feed_filter.services.profile import load_profile, parse_profile
from tests.conftest import PROJECT_ROOT, SAMPLE_PROFILE


class TestParseProfile:
    def test_parses_themes_weights_keywords(self):
        spec = parse_profile(SAMPLE_PROFILE)
        names = spec.theme_names()
        assert names[:2] == ["Tech Breakthroughs", "Business Insights"]
        assert "Saved for Later" in names  # auto-added
        tech = spec.themes[0]
        assert tech.weight == 1.0
        assert "postgres" in tech.keywords
        assert "database internals" in tech.description
        assert spec.themes[1].weight == 0.8

    def test_about_avoid_and_settings(self):
        spec = parse_profile(SAMPLE_PROFILE)
        assert spec.about.startswith("Engineer building")
        assert spec.avoid == ["keyboard recommendations", "tabs vs spaces"]
        assert spec.settings.min_score == 6
        assert spec.settings.max_items_per_theme == 3
        assert spec.settings.max_items_total == 5
        assert spec.settings.digest_days == 7
        assert spec.settings.clickbait_penalty == 10  # default kept
        assert spec.settings.session_min_score == 4.0

    def test_defaults_when_empty(self):
        spec = parse_profile("# Nothing here\n")
        assert spec.theme_names() == ["Tech Breakthroughs", "Business Insights", "Saved for Later"]
        assert spec.settings.min_score == 6.0

    def test_unknown_settings_ignored(self):
        spec = parse_profile("## Settings\nmin_score: 7\nmystery: 3\nmax_items_total: abc\n")
        assert spec.settings.min_score == 7
        assert spec.settings.max_items_total == 20

    @pytest.mark.parametrize(
        "given, expected",
        [
            ("tech breakthroughs", "Tech Breakthroughs"),
            ("Business", "Business Insights"),
            ("Business Insights & Strategy", "Business Insights"),
            ("Cooking", "Saved for Later"),
            (None, "Saved for Later"),
        ],
    )
    def test_canonical_theme(self, given, expected):
        assert parse_profile(SAMPLE_PROFILE).canonical_theme(given) == expected

    def test_theme_weight_lookup(self):
        spec = parse_profile(SAMPLE_PROFILE)
        assert spec.theme_weight("business insights") == 0.8
        assert spec.theme_weight("unknown") == 1.0

    def test_prompt_text_contains_everything_the_model_needs(self):
        text = parse_profile(SAMPLE_PROFILE).to_prompt_text()
        assert "## About the reader" in text
        assert "### Tech Breakthroughs (weight 1)" in text
        assert "Keywords: llm, postgres" in text
        assert "- tabs vs spaces" in text

    def test_bundled_profile_parses(self):
        spec = load_profile(PROJECT_ROOT / "interest_profile.md")
        assert len(spec.themes) >= 3
        assert spec.settings.min_score == 6

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_profile(tmp_path / "missing.md")
