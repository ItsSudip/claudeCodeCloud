"""SQLAlchemy ORM models.

Tables
------
raw_items          Everything ingested, exactly once per (provider, external_id). ``content_hash``
                   adds a second, provider-independent dedupe key so the same article shared on
                   Reddit and Hacker News is only stored once.
filtered_items     The LLM's verdict on a raw item: relevance score, theme, takeaways and flags.
interest_profiles  Snapshots of the interest-profile markdown, so each report records which
                   version of the profile produced it.
reports            Generated Daily Digest / mood Session markdown, plus metadata about the run.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RawItem(Base):
    __tablename__ = "raw_items"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_raw_items_provider_external_id"),
        Index("ix_raw_items_content_hash", "content_hash", unique=True),
        Index("ix_raw_items_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # upvotes / likes
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    filtered: Mapped[FilteredItem | None] = relationship(
        back_populates="raw_item", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RawItem {self.provider}:{self.external_id} {self.title[:40]!r}>"


class InterestProfile(Base):
    __tablename__ = "interest_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    reports: Mapped[list[Report]] = relationship(back_populates="profile")


class FilteredItem(Base):
    __tablename__ = "filtered_items"
    __table_args__ = (
        UniqueConstraint("raw_item_id", "profile_id", name="uq_filtered_items_raw_profile"),
        Index("ix_filtered_items_final_score", "final_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_item_id: Mapped[int] = mapped_column(
        ForeignKey("raw_items.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("interest_profiles.id", ondelete="SET NULL"), nullable=True
    )
    relevance_score: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-10 from the LLM
    final_score: Mapped[float] = mapped_column(Float, nullable=False)  # after weights/penalties
    theme: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    takeaways: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_clickbait: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_outrage: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_repetitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    save_for_later: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    moods: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    content_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    estimated_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    raw_item: Mapped[RawItem] = relationship(back_populates="filtered")

    @property
    def excluded(self) -> bool:
        return self.is_clickbait or self.is_outrage or self.is_repetitive


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_report_date", "report_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("interest_profiles.id", ondelete="SET NULL"), nullable=True
    )
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="digest")
    mood: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    profile: Mapped[InterestProfile | None] = relationship(back_populates="reports")
