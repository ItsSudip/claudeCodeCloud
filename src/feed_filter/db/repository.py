"""Repository: all database reads/writes go through here so services stay storage-agnostic."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from feed_filter.db.models import FilteredItem, InterestProfile, RawItem, Report
from feed_filter.providers.base import CollectedItem

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    inserted: int = 0
    duplicates: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.duplicates


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Repository:
    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------ raw items
    def ingest(self, items: Iterable[CollectedItem]) -> IngestResult:
        """Insert items, skipping anything already present by (provider, external_id) or hash."""
        result = IngestResult()
        batch = list(items)
        if not batch:
            return result

        hashes = {item.content_hash for item in batch}
        keys = {(item.provider, item.external_id) for item in batch}

        existing_hashes = set(
            self.session.scalars(
                select(RawItem.content_hash).where(RawItem.content_hash.in_(hashes))
            )
        )
        candidate_rows = self.session.execute(
            select(RawItem.provider, RawItem.external_id).where(
                RawItem.external_id.in_({k[1] for k in keys})
            )
        ).all()
        existing_keys = {(row.provider, row.external_id) for row in candidate_rows}

        seen_in_batch: set[str] = set()
        for item in batch:
            if (
                item.content_hash in existing_hashes
                or item.content_hash in seen_in_batch
                or (item.provider, item.external_id) in existing_keys
            ):
                result.duplicates += 1
                continue
            seen_in_batch.add(item.content_hash)
            self.session.add(
                RawItem(
                    provider=item.provider,
                    external_id=item.external_id,
                    content_hash=item.content_hash,
                    title=item.title,
                    url=item.url,
                    author=item.author,
                    body=item.body,
                    source_name=item.source_name,
                    score=item.score,
                    comments=item.comments,
                    published_at=item.published_at,
                    extra=item.extra or None,
                )
            )
            result.inserted += 1
        self.session.flush()
        return result

    def unfiltered_items(
        self, profile_id: int, *, since: datetime | None = None, limit: int | None = None
    ) -> list[RawItem]:
        """Raw items that have not been evaluated against ``profile_id`` yet."""
        evaluated = select(FilteredItem.raw_item_id).where(FilteredItem.profile_id == profile_id)
        stmt = select(RawItem).where(RawItem.id.not_in(evaluated))
        if since is not None:
            stmt = stmt.where(
                (RawItem.published_at >= since) | (RawItem.published_at.is_(None))
            ).where(RawItem.ingested_at >= since - timedelta(days=1))
        stmt = stmt.order_by(RawItem.score.desc().nullslast(), RawItem.id.asc())
        if limit:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def count_raw_items(self) -> int:
        return int(self.session.scalar(select(func.count(RawItem.id))) or 0)

    def recent_titles(self, *, days: int = 7, exclude_ids: Sequence[int] = ()) -> list[str]:
        """Titles already digested recently, used to flag repetitive news."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(RawItem.title)
            .join(FilteredItem, FilteredItem.raw_item_id == RawItem.id)
            .where(FilteredItem.evaluated_at >= cutoff)
        )
        if exclude_ids:
            stmt = stmt.where(RawItem.id.not_in(list(exclude_ids)))
        return list(self.session.scalars(stmt))

    # ------------------------------------------------------------------ profiles
    def upsert_profile(self, markdown: str) -> InterestProfile:
        content_hash = sha256(markdown.strip())
        existing = self.session.scalar(
            select(InterestProfile).where(InterestProfile.content_hash == content_hash)
        )
        if existing:
            return existing
        profile = InterestProfile(content_hash=content_hash, markdown=markdown)
        self.session.add(profile)
        self.session.flush()
        log.info("Stored new interest profile snapshot #%s", profile.id)
        return profile

    # ------------------------------------------------------------------ filtered items
    def add_filtered(self, filtered: Iterable[FilteredItem]) -> int:
        count = 0
        for item in filtered:
            self.session.add(item)
            count += 1
        self.session.flush()
        return count

    def top_filtered(
        self,
        profile_id: int,
        *,
        since: datetime | None = None,
        min_score: float = 0.0,
        limit: int | None = None,
        include_excluded: bool = False,
    ) -> list[FilteredItem]:
        stmt = (
            select(FilteredItem)
            .options(selectinload(FilteredItem.raw_item))
            .where(FilteredItem.profile_id == profile_id)
            .where(FilteredItem.final_score >= min_score)
        )
        if since is not None:
            stmt = stmt.where(FilteredItem.evaluated_at >= since)
        if not include_excluded:
            stmt = stmt.where(
                FilteredItem.is_clickbait.is_(False),
                FilteredItem.is_outrage.is_(False),
                FilteredItem.is_repetitive.is_(False),
            )
        stmt = stmt.order_by(FilteredItem.final_score.desc(), FilteredItem.id.asc())
        if limit:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def filtered_for_items(self, raw_item_ids: Sequence[int]) -> list[FilteredItem]:
        if not raw_item_ids:
            return []
        stmt = (
            select(FilteredItem)
            .options(selectinload(FilteredItem.raw_item))
            .where(FilteredItem.raw_item_id.in_(list(raw_item_ids)))
        )
        return list(self.session.scalars(stmt))

    # ------------------------------------------------------------------ reports
    def add_report(self, report: Report) -> Report:
        self.session.add(report)
        self.session.flush()
        return report

    def list_reports(self, limit: int = 50) -> list[Report]:
        stmt = select(Report).order_by(Report.created_at.desc(), Report.id.desc()).limit(limit)
        return list(self.session.scalars(stmt))

    def get_report(self, report_id: int) -> Report | None:
        return self.session.get(Report, report_id)

    def latest_report(self) -> Report | None:
        return self.session.scalar(
            select(Report).order_by(Report.created_at.desc(), Report.id.desc()).limit(1)
        )

    def stats(self) -> dict[str, int]:
        return {
            "raw_items": self.count_raw_items(),
            "filtered_items": int(self.session.scalar(select(func.count(FilteredItem.id))) or 0),
            "reports": int(self.session.scalar(select(func.count(Report.id))) or 0),
            "profiles": int(self.session.scalar(select(func.count(InterestProfile.id))) or 0),
        }
