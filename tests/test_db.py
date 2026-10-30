from __future__ import annotations

from datetime import UTC, datetime, timedelta

from feed_filter.db.models import FilteredItem, RawItem, Report
from tests.conftest import make_item


class TestIngestDedupe:
    def test_inserts_then_skips_same_provider_id(self, repo):
        first = repo.ingest([make_item(external_id="1")])
        second = repo.ingest(
            [make_item(external_id="1", title="Edited title", url="https://other.example/x")]
        )
        assert (first.inserted, first.duplicates) == (1, 0)
        assert (second.inserted, second.duplicates) == (0, 1)
        assert repo.count_raw_items() == 1

    def test_skips_same_url_across_providers(self, repo):
        repo.ingest(
            [make_item(provider="hackernews", external_id="1", url="https://example.com/p")]
        )
        result = repo.ingest(
            [
                make_item(
                    provider="reddit",
                    external_id="r9",
                    url="https://www.example.com/p/?utm_source=t",
                )
            ]
        )
        assert result.duplicates == 1
        assert repo.count_raw_items() == 1

    def test_dedupes_within_one_batch(self, repo):
        result = repo.ingest(
            [
                make_item(external_id="1", url="https://example.com/a"),
                make_item(external_id="2", url="https://example.com/a"),
                make_item(external_id="3", url="https://example.com/b"),
            ]
        )
        assert (result.inserted, result.duplicates) == (2, 1)

    def test_empty_batch(self, repo):
        result = repo.ingest([])
        assert result.total == 0

    def test_stores_fields(self, repo, session):
        repo.ingest(
            [
                make_item(
                    external_id="42",
                    published_at=datetime(2026, 10, 29, tzinfo=UTC),
                    extra={"discussion_url": "https://news.ycombinator.com/item?id=42"},
                )
            ]
        )
        row = session.query(RawItem).one()
        assert row.provider == "hackernews"
        assert row.external_id == "42"
        assert row.score == 500
        assert row.extra["discussion_url"].endswith("42")
        assert row.ingested_at is not None


class TestProfiles:
    def test_upsert_is_idempotent(self, repo):
        a = repo.upsert_profile("# P\n\n## Themes\n### A\n")
        b = repo.upsert_profile("# P\n\n## Themes\n### A\n  \n")  # trailing whitespace ignored
        c = repo.upsert_profile("# P\n\n## Themes\n### B\n")
        assert a.id == b.id
        assert c.id != a.id


class TestFilteredQueries:
    def _seed(self, repo, session):
        profile = repo.upsert_profile("# P")
        repo.ingest(
            [
                make_item(external_id="1", url="https://example.com/1", score=10),
                make_item(external_id="2", url="https://example.com/2", score=900),
                make_item(external_id="3", url="https://example.com/3", score=None),
            ]
        )
        session.commit()
        return profile

    def test_unfiltered_items_orders_by_engagement_and_excludes_evaluated(self, repo, session):
        profile = self._seed(repo, session)
        items = repo.unfiltered_items(profile.id)
        assert [i.external_id for i in items] == ["2", "1", "3"]
        repo.add_filtered(
            [
                FilteredItem(
                    raw_item_id=items[0].id,
                    profile_id=profile.id,
                    relevance_score=8,
                    final_score=8.0,
                    theme="T",
                    summary="s",
                )
            ]
        )
        remaining = repo.unfiltered_items(profile.id, limit=5)
        assert [i.external_id for i in remaining] == ["1", "3"]

    def test_unfiltered_respects_other_profiles(self, repo, session):
        profile = self._seed(repo, session)
        other = repo.upsert_profile("# Other")
        items = repo.unfiltered_items(profile.id)
        repo.add_filtered(
            [
                FilteredItem(
                    raw_item_id=items[0].id,
                    profile_id=other.id,
                    relevance_score=8,
                    final_score=8.0,
                    theme="T",
                    summary="s",
                )
            ]
        )
        assert len(repo.unfiltered_items(profile.id)) == 3

    def test_top_filtered_excludes_flags_and_applies_since(self, repo, session):
        profile = self._seed(repo, session)
        items = repo.unfiltered_items(profile.id)
        old = datetime.now(UTC) - timedelta(days=10)
        repo.add_filtered(
            [
                FilteredItem(
                    raw_item_id=items[0].id,
                    profile_id=profile.id,
                    relevance_score=9,
                    final_score=9.0,
                    theme="T",
                    summary="good",
                ),
                FilteredItem(
                    raw_item_id=items[1].id,
                    profile_id=profile.id,
                    relevance_score=9,
                    final_score=0.0,
                    theme="T",
                    summary="bait",
                    is_clickbait=True,
                ),
                FilteredItem(
                    raw_item_id=items[2].id,
                    profile_id=profile.id,
                    relevance_score=7,
                    final_score=7.0,
                    theme="T",
                    summary="old",
                    evaluated_at=old,
                ),
            ]
        )
        session.commit()
        recent = repo.top_filtered(profile.id, since=datetime.now(UTC) - timedelta(days=1))
        assert [f.summary for f in recent] == ["good"]
        everything = repo.top_filtered(profile.id, include_excluded=True)
        assert len(everything) == 3
        assert everything[0].raw_item is not None  # eager loaded

    def test_recent_titles(self, repo, session):
        profile = self._seed(repo, session)
        items = repo.unfiltered_items(profile.id)
        repo.add_filtered(
            [
                FilteredItem(
                    raw_item_id=items[0].id,
                    profile_id=profile.id,
                    relevance_score=8,
                    final_score=8.0,
                    theme="T",
                    summary="s",
                )
            ]
        )
        session.commit()
        assert repo.recent_titles() == [items[0].title]
        assert repo.recent_titles(exclude_ids=[items[0].id]) == []


class TestReports:
    def test_report_listing(self, repo, session):
        repo.add_report(Report(report_date="2026-10-29", title="A", markdown="# A", item_count=1))
        repo.add_report(Report(report_date="2026-10-30", title="B", markdown="# B", item_count=2))
        session.commit()
        assert [r.title for r in repo.list_reports()] == ["B", "A"]
        assert repo.latest_report().title == "B"
        assert repo.get_report(1).title == "A"
        assert repo.get_report(999) is None
        assert repo.stats()["reports"] == 2
