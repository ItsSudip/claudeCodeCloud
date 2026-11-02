from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from feed_filter.db.models import FilteredItem, RawItem
from feed_filter.services.llm import MockLLMClient
from feed_filter.services.moods import (
    CONTENT_MOODS,
    MOOD_PRESETS,
    MoodConfig,
    estimate_minutes,
    normalize_kind,
    normalize_moods,
)
from feed_filter.services.pipeline import Pipeline
from feed_filter.services.profile import parse_profile
from feed_filter.services.sessions import (
    SessionBuilder,
    SessionPlan,
    item_minutes,
    mood_fit,
    pack,
    render_session_markdown,
)
from tests.conftest import SAMPLE_PROFILE


# ---------------------------------------------------------------- moods module
class TestMoods:
    def test_normalize(self):
        assert normalize_moods(["Feel Good", "funny", "bogus", "funny"]) == ["feel_good", "funny"]
        assert normalize_moods(None) == []
        assert normalize_kind("Reel") == "short_video"
        assert normalize_kind("photo") == "image"
        assert normalize_kind("nonsense") == "other"

    def test_estimate_minutes_priority(self):
        assert estimate_minutes(kind="short_video", duration_seconds=90) == 1.5
        assert estimate_minutes(kind="article", model_estimate=7.3) == 7.3
        words = " ".join(["word"] * 460)
        assert estimate_minutes(kind="article", body=words) == 2.0
        assert estimate_minutes(kind="image") == 0.5
        assert estimate_minutes(kind="post", body="short") == 1.0
        assert estimate_minutes(kind="podcast", model_estimate=10_000) == 240.0

    def test_presets_are_well_formed(self):
        for preset in MOOD_PRESETS.values():
            assert set(preset.include) <= set(CONTENT_MOODS)
            assert set(preset.exclude) <= set(CONTENT_MOODS)
            assert not set(preset.include) & set(preset.exclude)
        assert "intense" in MOOD_PRESETS["happy"].exclude
        assert MOOD_PRESETS["tired"].max_item_minutes == 4.0

    def test_config_get_and_override(self):
        cfg = MoodConfig()
        with pytest.raises(KeyError, match="Unknown mood"):
            cfg.get("furious")
        cfg.override("happy", ["funny"], ["intense", "calm"])
        assert cfg.get("Happy").include == ("funny",)
        assert cfg.get("happy").exclude == ("intense", "calm")
        cfg.override("commute", ["practical", "nope"])
        assert cfg.get("commute").include == ("practical",)
        assert "commute" in cfg.names()

    def test_profile_moods_section(self):
        spec = parse_profile(
            SAMPLE_PROFILE
            + "\n## Moods\nhappy: feel_good, funny | exclude: intense\ndeep: practical\n"
        )
        assert spec.moods.get("happy").include == ("feel_good", "funny")
        assert spec.moods.get("deep").include == ("practical",)
        assert spec.moods.get("sad").include == MOOD_PRESETS["sad"].include  # untouched


# ---------------------------------------------------------------- fit + pack
def _item(idx, *, score=8.0, moods=(), kind="post", minutes=1.0, source="S"):
    raw = RawItem(
        id=idx,
        provider="x",
        external_id=str(idx),
        content_hash=f"h{idx}",
        title=f"Item {idx}",
        url=f"https://e.example/{idx}",
        source_name=source,
    )
    row = FilteredItem(
        id=idx,
        raw_item_id=idx,
        relevance_score=int(score),
        final_score=score,
        theme="T",
        summary=f"Summary {idx}",
        takeaways=[],
        moods=list(moods),
        content_kind=kind,
        estimated_minutes=minutes,
    )
    row.raw_item = raw
    return row


class TestFitAndPack:
    def test_mood_fit_rules(self):
        happy = MOOD_PRESETS["happy"]
        assert mood_fit(_item(1, moods=["feel_good", "funny"]), happy) == 8 + 2.0
        assert mood_fit(_item(2, moods=["intense", "funny"]), happy) is None  # excluded wins
        assert mood_fit(_item(3, moods=["practical"]), happy) is None  # no overlap
        assert mood_fit(_item(4, moods=[]), happy) is None  # untagged never fits a specific mood
        assert mood_fit(_item(5, moods=[]), MOOD_PRESETS["any"]) == 9.0
        tired = MOOD_PRESETS["tired"]
        assert mood_fit(_item(6, moods=["calm"], minutes=9), tired) is None  # too long when tired
        assert (
            mood_fit(_item(7, moods=["calm"], kind="short_video", minutes=1), tired)
            == 8 + 1.0 + 0.5
        )

    def test_item_minutes_fallback(self):
        row = _item(1, minutes=0, kind="image")
        row.estimated_minutes = None
        assert item_minutes(row) == 0.5

    def test_pack_respects_budget_and_variety(self):
        candidates = [
            (_item(1, score=9, minutes=4.0, source="A"), 9.0),
            (_item(2, score=9, minutes=4.0, source="A"), 9.0),
            (_item(3, score=9, minutes=4.0, source="A"), 9.0),
            (_item(4, score=9, minutes=4.0, source="A"), 9.0),  # 4th from source A -> skipped
            (_item(5, score=7, minutes=1.0, source="B"), 7.0),
            (_item(6, score=6, minutes=30.0, source="C"), 6.0),  # never fits
            (_item(7, score=5, minutes=1.0, source="D"), 5.0),
        ]
        entries = pack(candidates, 10)
        ids = [e.item.id for e in entries]
        assert 4 not in ids and 6 not in ids
        total = sum(e.minutes for e in entries)
        assert 9.0 <= total <= 11.5
        assert entries[0].item.id == 1  # strongest leads
        assert entries[0].starts_at == 0.0
        assert entries[1].starts_at == entries[0].minutes

    def test_pack_alternates_short_and_long(self):
        candidates = [
            (
                _item(i, score=9 - i * 0.1, minutes=(0.5 if i % 2 else 3.0), source=f"S{i}"),
                9 - i * 0.1,
            )
            for i in range(1, 7)
        ]
        entries = pack(candidates, 12)
        kinds = ["short" if e.minutes <= 2 else "long" for e in entries[1:]]
        assert kinds[:4] == ["short", "long", "short", "long"]

    def test_render(self):
        plan = SessionPlan(mood=MOOD_PRESETS["happy"], target_minutes=10, considered=20, matched=5)
        plan.entries = pack(
            [
                (_item(1, moods=["feel_good"], kind="short_video", minutes=0.7), 9.0),
                (_item(2, moods=["funny"], minutes=8.0), 8.0),
            ],
            10,
        )
        md = render_session_markdown(plan, built_at=datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
        assert md.startswith("# 10-minute session · happy")
        assert "1. **[Item 1](https://e.example/1)**" in md
        assert "`0:00` · ~0.7 min · short video · S · feel good" in md
        assert "`0:42`" in md
        assert "Total ≈ 8.7 min of 10 requested" in md
        assert "Shorter than requested" in md
        empty = render_session_markdown(SessionPlan(mood=MOOD_PRESETS["any"], target_minutes=5))
        assert "Nothing scored recently fits this mood" in empty


# ---------------------------------------------------------------- end to end
class TestSessionBuilder:
    def test_builds_from_pipeline_output(self, settings, session):
        pipeline = Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True)
        pipeline.run()
        report = pipeline.build_session("happy", 10)
        assert report.kind == "session" and report.mood == "happy"
        assert report.target_minutes == 10
        assert report.item_count > 0
        assert 0 < report.total_minutes <= 11.5
        assert "Rescue puppy" in report.markdown or "Grandma" in report.markdown
        assert "DISGRACE" not in report.markdown and "gadget" not in report.markdown
        assert report.stats["item_ids"]

    def test_sessions_do_not_repeat_until_allowed(self, settings, session):
        pipeline = Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True)
        pipeline.run()
        first = pipeline.build_session("any", 5)
        second = pipeline.build_session("any", 5)
        assert not set(first.stats["item_ids"]) & set(second.stats["item_ids"])
        third = pipeline.build_session("any", 5, allow_repeats=True)
        assert set(third.stats["item_ids"]) & set(first.stats["item_ids"])

    def test_unknown_mood(self, settings, session):
        pipeline = Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True)
        with pytest.raises(KeyError):
            pipeline.build_session("furious", 10)

    def test_stale_items_ignored(self, settings, session):
        pipeline = Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True)
        pipeline.run()
        old = datetime.now(UTC) - timedelta(days=30)
        for row in session.query(FilteredItem).all():
            row.evaluated_at = old
        session.commit()
        builder = SessionBuilder(
            pipeline.repo, pipeline.profile, profile_id=pipeline.profile_row.id
        )
        assert builder.plan("any", 10).entries == []
