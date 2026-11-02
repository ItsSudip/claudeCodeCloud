"""End-to-end orchestration: collect -> filter -> summarize."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from feed_filter.config import Settings
from feed_filter.db.models import Report
from feed_filter.db.repository import Repository
from feed_filter.providers import Provider, build_providers
from feed_filter.services.collector import CollectionResult, Collector
from feed_filter.services.filter_engine import FilterEngine, FilterStats
from feed_filter.services.llm import LLMClient, build_llm_client
from feed_filter.services.profile import InterestProfileSpec, load_profile
from feed_filter.services.sessions import SessionBuilder
from feed_filter.services.summarizer import Summarizer

log = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    collection: CollectionResult | None = None
    filtering: FilterStats | None = None
    report: Report | None = None


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        session: Session,
        *,
        llm: LLMClient | None = None,
        providers: list[Provider] | None = None,
        profile: InterestProfileSpec | None = None,
        mock_providers: bool = False,
    ):
        self.settings = settings
        self.session = session
        self.repo = Repository(session)
        self.profile = profile or load_profile(settings.profile_path)
        self.profile_row = self.repo.upsert_profile(self.profile.raw_markdown)
        self.session.commit()
        self.llm = llm or build_llm_client(
            settings.llm_provider, model=settings.anthropic_model, effort=settings.anthropic_effort
        )
        self.providers = (
            providers if providers is not None else build_providers(settings, mock=mock_providers)
        )

    def collect(self) -> CollectionResult:
        collector = Collector(
            self.repo, self.providers, allow_mock_fallback=self.settings.allow_mock_fallback
        )
        return collector.run()

    def filter(self) -> FilterStats:
        engine = FilterEngine(
            self.repo,
            self.llm,
            self.profile,
            profile_id=self.profile_row.id,
            batch_size=self.settings.llm_batch_size,
            max_items=self.settings.llm_max_items,
        )
        return engine.run()

    def summarize(self, *, report_date: date | None = None, stats: dict | None = None) -> Report:
        summarizer = Summarizer(
            self.repo,
            self.profile,
            profile_id=self.profile_row.id,
            llm=self.llm,
            reports_dir=self.settings.reports_dir,
        )
        return summarizer.build(report_date=report_date, stats=stats)

    def build_session(self, mood: str, minutes: int, *, allow_repeats: bool = False) -> Report:
        builder = SessionBuilder(self.repo, self.profile, profile_id=self.profile_row.id)
        return builder.build(mood, minutes, allow_repeats=allow_repeats)

    def run(self, *, report_date: date | None = None) -> PipelineResult:
        result = PipelineResult()
        log.info("pipeline: collecting from %d provider(s)", len(self.providers))
        result.collection = self.collect()
        log.info("pipeline: filtering")
        result.filtering = self.filter()
        log.info("pipeline: summarizing")
        stats = {**result.collection.as_dict(), **result.filtering.as_dict()}
        result.report = self.summarize(report_date=report_date, stats=stats)
        log.info(
            "pipeline: report #%s ready (%d items)", result.report.id, result.report.item_count
        )
        return result
