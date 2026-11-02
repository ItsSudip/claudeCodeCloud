from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from feed_filter.db.models import FilteredItem, RawItem
from feed_filter.services.llm import MockLLMClient
from feed_filter.services.summarizer import Summarizer, render_markdown, select_for_digest


def _filtered(idx, *, score, theme="Tech Breakthroughs", title=None, url=None, **flags):
    raw = RawItem(
        id=idx,
        provider="hackernews",
        external_id=str(idx),
        content_hash=f"hash{url or idx}",
        title=title or f"Story {idx}",
        url=url or f"https://e.example/{idx}",
        source_name="Hacker News",
        score=100,
        comments=5,
        extra={"discussion_url": f"https://news.ycombinator.com/item?id={idx}"},
    )
    row = FilteredItem(
        id=idx,
        raw_item_id=idx,
        relevance_score=int(score),
        final_score=score,
        theme=theme,
        summary=f"Summary {idx}",
        takeaways=[f"Takeaway {idx}"],
        **flags,
    )
    row.raw_item = raw
    return row


class TestSelectForDigest:
    def test_groups_caps_and_excludes(self, profile):
        # profile: max 3 per theme, max 5 total, min_score 6
        candidates = [
            _filtered(1, score=9.5),
            _filtered(2, score=9.0),
            _filtered(3, score=8.5),
            _filtered(4, score=8.0),  # 4th tech item -> capped
            _filtered(5, score=9.0, theme="Business Insights"),
            _filtered(6, score=7.0, theme="Business Insights"),
            _filtered(7, score=8.0, theme="Business Insights"),  # would be 6th overall -> total cap
            _filtered(8, score=9.9, is_clickbait=True),
            _filtered(9, score=9.9, is_outrage=True),
            _filtered(10, score=9.9, is_repetitive=True),
            _filtered(11, score=5.5),  # below threshold
            _filtered(12, score=7.0, theme="Saved for Later"),
            _filtered(13, score=4.0, theme="Saved for Later"),  # below save_for_later_min_score
            _filtered(
                14, score=5.5, theme="Tech Breakthroughs", save_for_later=True
            ),  # routed to later
        ]
        sections = select_for_digest(candidates, profile)
        assert [i.id for i in sections.themes["Tech Breakthroughs"]] == [1, 2, 3]
        assert [i.id for i in sections.themes["Business Insights"]] == [5, 7]
        assert [i.id for i in sections.saved_for_later] == [12, 14]
        assert sections.excluded == {"clickbait": 1, "outrage": 1, "repetitive": 1}
        assert sections.below_threshold == 2
        assert sections.item_count == 7

    def test_same_url_only_once(self, profile):
        candidates = [
            _filtered(1, score=9.0, url="https://e.example/same"),
            _filtered(2, score=8.0, url="https://e.example/same"),
        ]
        sections = select_for_digest(candidates, profile)
        assert [i.id for i in sections.themes["Tech Breakthroughs"]] == [1]
        assert sections.excluded["repetitive"] == 1


class TestRenderMarkdown:
    def test_structure(self, profile):
        sections = select_for_digest(
            [
                _filtered(1, score=9.0, title="Big [bracket] story"),
                _filtered(2, score=8.0, theme="Business Insights"),
                _filtered(3, score=7.0, theme="Saved for Later"),
            ],
            profile,
        )
        md = render_markdown(
            sections,
            report_date=date(2026, 10, 30),
            profile=profile,
            overview="Editor's note.",
            stats={"collected": 10, "duplicates": 2, "evaluated": 9},
        )
        assert md.startswith("# Daily Digest - 2026-10-30")
        assert "> Editor's note." in md
        assert "## At a glance" in md and "Collected: 10 new items (2 duplicates skipped)" in md
        assert (
            md.index("## Tech Breakthroughs")
            < md.index("## Business Insights")
            < md.index("## Saved for Later")
        )
        assert "### [Big [bracket] story](https://e.example/1)" in md
        assert "- Takeaway 1" in md
        assert "[Discussion](https://news.ycombinator.com/item?id=1)" in md
        assert "relevance 9/10 · score 9" in md

    def test_empty_digest_message(self, profile):
        md = render_markdown(
            select_for_digest([], profile), report_date=date(2026, 1, 1), profile=profile
        )
        assert "Nothing cleared the bar today" in md


class TestSummarizerService:
    def test_builds_and_persists_report(self, profile, repo, session, tmp_path):
        prof = repo.upsert_profile(profile.raw_markdown)
        raw = RawItem(
            provider="rss",
            external_id="a",
            content_hash="h",
            title="Great read",
            url="https://e.example/a",
            source_name="Blog",
        )
        session.add(raw)
        session.flush()
        session.add(
            FilteredItem(
                raw_item_id=raw.id,
                profile_id=prof.id,
                relevance_score=9,
                final_score=9.0,
                theme="Tech Breakthroughs",
                summary="s",
                takeaways=["t"],
            )
        )
        stale = RawItem(
            provider="rss",
            external_id="b",
            content_hash="h2",
            title="Old read",
            url="https://e.example/b",
        )
        session.add(stale)
        session.flush()
        session.add(
            FilteredItem(
                raw_item_id=stale.id,
                profile_id=prof.id,
                relevance_score=9,
                final_score=9.0,
                theme="Tech Breakthroughs",
                summary="old",
                evaluated_at=datetime.now(UTC) - timedelta(days=30),
            )
        )
        session.commit()

        summarizer = Summarizer(
            repo, profile, profile_id=prof.id, llm=MockLLMClient(), reports_dir=tmp_path / "reports"
        )
        report = summarizer.build(report_date=date(2026, 10, 30), stats={"collected": 1})
        assert report.id is not None
        assert report.item_count == 1
        assert "Great read" in report.markdown and "Old read" not in report.markdown
        assert report.stats["themes"] == {
            "Tech Breakthroughs": 1,
            "Business Insights": 0,
            "Good Vibes": 0,
        }
        assert (tmp_path / "reports" / "2026-10-30.md").read_text() == report.markdown
        assert repo.latest_report().id == report.id
