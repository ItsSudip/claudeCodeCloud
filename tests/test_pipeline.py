from __future__ import annotations

from datetime import date

from feed_filter.db.models import FilteredItem, RawItem, Report
from feed_filter.services.llm import MockLLMClient
from feed_filter.services.pipeline import Pipeline


class TestPipeline:
    def test_full_run_with_mocks(self, settings, session):
        pipeline = Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True)
        result = pipeline.run(report_date=date(2026, 10, 30))

        assert result.collection.inserted > 10
        assert result.filtering.evaluated == result.collection.inserted
        report = result.report
        assert report.report_date == "2026-10-30"
        assert report.item_count > 0
        assert "## Tech Breakthroughs" in report.markdown
        # outrage / clickbait fixtures never make it into the digest
        assert "You won't BELIEVE" not in report.markdown
        assert "disgusting" not in report.markdown.lower()
        assert (settings.reports_dir / "2026-10-30.md").exists()
        assert report.stats["providers"]["hackernews"]["inserted"] > 0

    def test_second_run_is_incremental(self, settings, session):
        pipeline = Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True)
        pipeline.run(report_date=date(2026, 10, 30))
        raw_before = session.query(RawItem).count()
        filtered_before = session.query(FilteredItem).count()

        second = pipeline.run(report_date=date(2026, 10, 31))
        assert second.collection.inserted == 0
        assert second.collection.duplicates == raw_before + 1  # fixtures contain one cross-post
        assert second.filtering.evaluated == 0
        assert session.query(FilteredItem).count() == filtered_before
        assert session.query(Report).count() == 2
        # the digest still covers the lookback window, so yesterday's items reappear
        assert second.report.item_count > 0

    def test_profile_change_triggers_reevaluation(self, settings, session):
        pipeline = Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True)
        pipeline.run()
        settings.profile_path.write_text(
            settings.profile_path.read_text() + "\n### Extra Theme (weight: 0.5)\nkeywords: zzz\n"
        )
        rerun = Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True)
        assert rerun.profile_row.id != pipeline.profile_row.id
        assert rerun.filter().evaluated > 0

    def test_individual_stages(self, settings, session):
        pipeline = Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True)
        assert pipeline.collect().inserted > 0
        assert pipeline.filter().evaluated > 0
        report = pipeline.summarize(report_date=date(2026, 10, 30))
        assert report.item_count > 0
