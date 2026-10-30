from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from feed_filter.config import Settings, load_settings
from feed_filter.db import Repository, create_engine_for, create_session_factory, init_db
from feed_filter.providers.base import CollectedItem
from feed_filter.services.profile import InterestProfileSpec, parse_profile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

SAMPLE_PROFILE = """# Interest Profile

## About Me
Engineer building data infrastructure and LLM products.

## Themes

### Tech Breakthroughs (weight: 1.0)
keywords: llm, postgres, rust, wasm, benchmark, open source, eval
Model releases, database internals, systems work.

### Business Insights (weight: 0.8)
keywords: startup, arr, pricing, growth, capex, margins
Lessons about building companies.

## Avoid
- keyboard recommendations
- tabs vs spaces

## Settings
min_score: 6
max_items_per_theme: 3
max_items_total: 5
engagement_boost: 0.5
digest_days: 7
"""


@pytest.fixture
def profile() -> InterestProfileSpec:
    return parse_profile(SAMPLE_PROFILE)


@pytest.fixture
def session_factory():
    engine = create_engine_for("sqlite://")  # in-memory
    init_db(engine)
    return create_session_factory(engine)


@pytest.fixture
def session(session_factory) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def repo(session: Session) -> Repository:
    return Repository(session)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    profile_path = tmp_path / "interest_profile.md"
    profile_path.write_text(SAMPLE_PROFILE, encoding="utf-8")
    sources_path = tmp_path / "sources.toml"
    sources_path.write_text(
        '[rss]\nfeeds = ["https://example.com/feed.xml"]\n'
        '[reddit]\nsubreddits = ["programming"]\n'
        '[x]\naccounts = ["someone"]\n',
        encoding="utf-8",
    )
    for var in (
        "ANTHROPIC_API_KEY",
        "X_BEARER_TOKEN",
        "X_NITTER_BASE_URL",
        "X_AUTH_COOKIE",
        "REDDIT_ACCESS_TOKEN",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    return load_settings(
        env_file=tmp_path / ".env.missing",
        database_url="sqlite://",
        profile_path=profile_path,
        sources_path=sources_path,
        reports_dir=tmp_path / "reports",
        llm_provider="mock",
        allow_mock_fallback=True,
    )


def make_item(**overrides) -> CollectedItem:
    base = dict(
        provider="hackernews",
        external_id="1",
        title="Postgres 18 released with async I/O",
        url="https://example.com/postgres-18",
        author="pg",
        body="Release notes mention async I/O and faster vacuum.",
        source_name="Hacker News",
        score=500,
        comments=120,
    )
    base.update(overrides)
    return CollectedItem(**base)


def mock_client(handler) -> httpx.Client:
    """httpx client whose transport is a python callable (request -> Response)."""
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "test"})
