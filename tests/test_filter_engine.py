from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from feed_filter.db.models import FilteredItem
from feed_filter.services.filter_engine import (
    FilterEngine,
    compute_final_score,
    detect_repeats,
)
from feed_filter.services.llm import (
    EVALUATION_SCHEMA,
    AnthropicLLMClient,
    Evaluation,
    ItemForEvaluation,
    MockLLMClient,
    build_evaluation_prompt,
    build_llm_client,
    engagement_signal,
    parse_evaluations,
)
from tests.conftest import make_item


def _raw(repo, session, **kw):
    repo.ingest([make_item(**kw)])
    session.commit()
    return repo.unfiltered_items(1)[-1]


# ---------------------------------------------------------------- scoring maths
class TestScoring:
    def test_engagement_signal_bounds(self):
        assert engagement_signal(None, None) == 0.0
        assert engagement_signal(0, 0) == 0.0
        assert 0 < engagement_signal(10, 0) < engagement_signal(1000, 100) <= 1.0
        assert engagement_signal(1_000_000, 0) == 1.0

    def test_theme_weight_and_engagement(self, profile, repo, session):
        item = _raw(repo, session, score=0, comments=0)
        ev = Evaluation(ref="1", relevance_score=8, theme="Business Insights", summary="s")
        assert compute_final_score(ev, item, profile) == pytest.approx(8 * 0.8)
        item.score, item.comments = 5000, 500
        boosted = compute_final_score(ev, item, profile)
        assert 6.4 < boosted <= 6.9  # + engagement_boost(0.5) * signal(~0.95)

    def test_penalties_sink_flagged_items(self, profile, repo, session):
        item = _raw(repo, session)
        for flag in ("is_clickbait", "is_outrage", "is_repetitive"):
            ev = Evaluation(
                ref="1", relevance_score=10, theme="Tech Breakthroughs", summary="s", **{flag: True}
            )
            # a penalty of 10 wipes the relevance score; only the small engagement boost survives
            assert compute_final_score(ev, item, profile) <= profile.settings.engagement_boost
        ev = Evaluation(
            ref="1",
            relevance_score=10,
            theme="Tech Breakthroughs",
            summary="s",
            is_clickbait=True,
            is_outrage=True,
        )
        assert compute_final_score(ev, item, profile) == 0.0

    def test_score_is_clamped(self, profile, repo, session):
        item = _raw(repo, session, score=10**7, comments=10**6)
        ev = Evaluation(ref="1", relevance_score=10, theme="Tech Breakthroughs", summary="s")
        assert compute_final_score(ev, item, profile) == 10.0

    def test_evaluation_clamps_score_and_trims_takeaways(self):
        ev = Evaluation(
            ref="x",
            relevance_score=14,
            theme="t",
            summary="s",
            takeaways=[" a ", "", "b", "c", "d", "e", "f"],
        )
        assert ev.relevance_score == 10
        assert ev.takeaways == ["a", "b", "c", "d", "e"]
        assert Evaluation(ref="x", relevance_score=-3, theme="t", summary="s").relevance_score == 1


# ---------------------------------------------------------------- repeats
class TestDetectRepeats:
    def test_flags_near_duplicates_keeping_most_engaged(self, repo, session):
        repo.ingest(
            [
                make_item(
                    external_id="1",
                    url="https://a.example/1",
                    title="Postgres 18 released with async I/O",
                    score=100,
                ),
                make_item(
                    external_id="2",
                    url="https://a.example/2",
                    title="Postgres 18 released with async I/O!",
                    score=900,
                ),
                make_item(
                    external_id="3",
                    url="https://a.example/3",
                    title="Completely different story",
                    score=5,
                ),
            ]
        )
        session.commit()
        items = repo.unfiltered_items(1)
        repeats = detect_repeats(items, recent_titles=[])
        flagged = {i.external_id for i in items if i.id in repeats}
        assert flagged == {"1"}

    def test_flags_items_matching_recent_digests(self, repo, session):
        repo.ingest([make_item(external_id="1", title="Postgres 18 released with async I/O")])
        session.commit()
        items = repo.unfiltered_items(1)
        assert detect_repeats(items, ["postgres 18 released with async i/o"]) == {items[0].id}
        assert detect_repeats(items, ["Unrelated"]) == set()


# ---------------------------------------------------------------- prompt / parsing
class TestPromptAndParsing:
    def test_prompt_includes_refs_and_recent_titles(self):
        items = [
            ItemForEvaluation(ref="7", title="T", source="S", body="x" * 5000, score=3, comments=1)
        ]
        prompt = build_evaluation_prompt(items, ["old title"], ["A", "B"])
        payload = json.loads(prompt.split("\n\n", 1)[1])
        assert payload["allowed_themes"] == ["A", "B"]
        assert payload["recently_covered_titles"] == ["old title"]
        assert payload["items"][0]["ref"] == "7"
        assert len(payload["items"][0]["body"]) == 1200  # truncated for the prompt
        assert payload["items"][0]["engagement"] == {"score": 3, "comments": 1}

    def test_parse_fills_missing_and_drops_unknown_refs(self):
        raw = {
            "evaluations": [
                {
                    "ref": "1",
                    "relevance_score": 8,
                    "theme": "A",
                    "summary": "s",
                    "takeaways": ["t"],
                    "is_clickbait": False,
                    "is_outrage": False,
                    "is_repetitive": False,
                    "save_for_later": True,
                    "reasoning": "r",
                },
                {"ref": "999", "relevance_score": 9, "theme": "A", "summary": "unknown"},
                {"ref": "2"},  # minimal, still accepted with defaults
                "garbage",
            ]
        }
        results = parse_evaluations(raw, ["1", "2", "3"])
        assert [r.ref for r in results] == ["1", "2", "3"]
        assert results[0].save_for_later is True
        assert results[1].relevance_score == 1
        assert results[2].reasoning == "missing from model output"

    def test_schema_is_strict(self):
        item_schema = EVALUATION_SCHEMA["properties"]["evaluations"]["items"]
        assert item_schema["additionalProperties"] is False
        assert set(item_schema["required"]) == set(item_schema["properties"])


# ---------------------------------------------------------------- mock llm
class TestMockLLM:
    def test_scores_keywords_and_flags(self, profile):
        items = [
            ItemForEvaluation(
                ref="1",
                title="Postgres 18 released",
                source="HN",
                body="Async I/O benchmark results. Open source.",
            ),
            ItemForEvaluation(
                ref="2",
                title="You won't believe this CEO rant",
                source="HN",
                body="Disgusting behaviour, idiots everywhere.",
            ),
            ItemForEvaluation(
                ref="3", title="Best keyboard recommendations", source="HN", body="keys"
            ),
            ItemForEvaluation(ref="4", title="Postgres 18 released", source="Reddit", body="dup"),
        ]
        results = {
            e.ref: e for e in MockLLMClient().evaluate_batch(profile, items, ["Older story"])
        }
        assert results["1"].theme == "Tech Breakthroughs"
        assert results["1"].relevance_score >= 7
        assert results["2"].is_clickbait and results["2"].is_outrage
        assert results["3"].relevance_score <= 2  # matched an "avoid" entry
        assert results["4"].is_repetitive is True

    def test_overview(self, profile):
        text = MockLLMClient().compose_overview(profile, [{"title": "A"}, {"title": "B"}])
        assert "2 items" in text and "A; B" in text
        assert MockLLMClient().compose_overview(profile, []) == ""

    def test_factory(self):
        assert isinstance(build_llm_client("mock", model="m", effort="low"), MockLLMClient)
        with pytest.raises(ValueError):
            build_llm_client("openai", model="m", effort="low")


# ---------------------------------------------------------------- anthropic client (fake SDK)
class _FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _fake_client(*responses):
    messages = _FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def _response(text, stop_reason="end_turn", **extra):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
        **extra,
    )


class TestAnthropicClient:
    def test_request_shape_and_parsing(self, profile):
        payload = {
            "evaluations": [
                {
                    "ref": "1",
                    "relevance_score": 9,
                    "theme": "Tech Breakthroughs",
                    "summary": "s",
                    "takeaways": ["a"],
                    "is_clickbait": False,
                    "is_outrage": False,
                    "is_repetitive": False,
                    "save_for_later": False,
                    "reasoning": "r",
                }
            ]
        }
        fake, messages = _fake_client(_response(json.dumps(payload)))
        client = AnthropicLLMClient("claude-opus-5-5", effort="low", client=fake)
        results = client.evaluate_batch(
            profile, [ItemForEvaluation(ref="1", title="t", source="s")], []
        )
        assert results[0].relevance_score == 9

        call = messages.calls[0]
        assert call["model"] == "claude-opus-5-5"
        assert call["fallbacks"] == "default"
        assert call["betas"] == ["server-side-fallback-2026-07-01"]
        assert call["output_config"]["effort"] == "low"
        assert call["output_config"]["format"]["type"] == "json_schema"
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert "Tech Breakthroughs" in call["system"][0]["text"]
        assert call["messages"][0]["role"] == "user"
        assert "thinking" not in call  # adaptive thinking is the model default

    def test_refusal_is_surfaced(self, profile):
        refusal = _response(
            "",
            stop_reason="refusal",
            stop_details=SimpleNamespace(category="cyber", explanation="x"),
        )
        fake, _ = _fake_client(refusal)
        client = AnthropicLLMClient(client=fake)
        with pytest.raises(RuntimeError, match="declined.*cyber"):
            client.evaluate_batch(profile, [ItemForEvaluation(ref="1", title="t", source="s")], [])

    def test_max_tokens_is_surfaced(self, profile):
        fake, _ = _fake_client(_response("{", stop_reason="max_tokens"))
        with pytest.raises(RuntimeError, match="max_tokens"):
            AnthropicLLMClient(client=fake).evaluate_batch(
                profile, [ItemForEvaluation(ref="1", title="t", source="s")], []
            )

    def test_retries_server_errors_then_succeeds(self, profile, monkeypatch):
        import anthropic

        monkeypatch.setattr("feed_filter.services.llm.time.sleep", lambda s: None)
        http_response = httpx.Response(
            500, request=httpx.Request("POST", "https://api.anthropic.com")
        )
        error = anthropic.InternalServerError("boom", response=http_response, body=None)
        fake, messages = _fake_client(error, _response("note"))
        client = AnthropicLLMClient(client=fake, max_attempts=2)
        assert client.compose_overview(profile, [{"title": "x"}]) == "note"
        assert len(messages.calls) == 2
        assert "format" not in messages.calls[0]["output_config"]  # free text for the overview

    def test_invalid_json_raises(self, profile):
        fake, _ = _fake_client(_response("not json"))
        with pytest.raises(RuntimeError, match="invalid JSON"):
            AnthropicLLMClient(client=fake).evaluate_batch(
                profile, [ItemForEvaluation(ref="1", title="t", source="s")], []
            )


# ---------------------------------------------------------------- engine
class _ScriptedLLM:
    model_name = "scripted"

    def __init__(self, script):
        self.script = script
        self.batches = []

    def evaluate_batch(self, profile, items, recent_titles):
        self.batches.append([i.ref for i in items])
        return [
            self.script.get(
                i.ref,
                Evaluation(ref=i.ref, relevance_score=5, theme="Business Insights", summary="d"),
            )
            for i in items
        ]

    def compose_overview(self, profile, highlights):
        return "overview"


class TestFilterEngine:
    def test_persists_scores_in_batches(self, profile, repo, session):
        prof = repo.upsert_profile(profile.raw_markdown)
        repo.ingest(
            [
                make_item(
                    external_id=str(i), url=f"https://e.example/{i}", title=f"Story {i}", score=i
                )
                for i in range(1, 6)
            ]
        )
        session.commit()
        items = repo.unfiltered_items(prof.id)
        ref_of = {i.title: str(i.id) for i in items}
        llm = _ScriptedLLM(
            {
                ref_of["Story 5"]: Evaluation(
                    ref=ref_of["Story 5"],
                    relevance_score=9,
                    theme="tech breakthroughs",
                    summary="great",
                    takeaways=["x"],
                ),
                ref_of["Story 4"]: Evaluation(
                    ref=ref_of["Story 4"],
                    relevance_score=9,
                    theme="Tech Breakthroughs",
                    summary="bait",
                    is_clickbait=True,
                ),
            }
        )
        engine = FilterEngine(repo, llm, profile, profile_id=prof.id, batch_size=2)
        stats = engine.run()

        assert stats.evaluated == 5
        assert stats.batches == 3
        assert stats.clickbait == 1
        assert stats.kept == 1  # only Story 5 clears min_score 6 unflagged
        assert [len(b) for b in llm.batches] == [2, 2, 1]

        rows = {r.raw_item.title: r for r in session.query(FilteredItem).all()}
        assert rows["Story 5"].theme == "Tech Breakthroughs"  # canonicalised
        assert rows["Story 5"].final_score >= 9
        assert rows["Story 4"].final_score == 0.0
        assert rows["Story 1"].model == "scripted"
        # second run has nothing left to do
        assert engine.run().evaluated == 0

    def test_pre_flagged_repeats_override_llm(self, profile, repo, session):
        prof = repo.upsert_profile(profile.raw_markdown)
        repo.ingest(
            [
                make_item(
                    external_id="1", url="https://e.example/1", title="Same headline here", score=10
                ),
                make_item(
                    external_id="2",
                    url="https://e.example/2",
                    title="Same headline here",
                    score=900,
                ),
            ]
        )
        session.commit()
        engine = FilterEngine(repo, _ScriptedLLM({}), profile, profile_id=prof.id)
        stats = engine.run()
        assert stats.repetitive == 1
        flagged = [r for r in session.query(FilteredItem).all() if r.is_repetitive]
        assert flagged[0].raw_item.external_id == "1"  # the lower-engagement copy
