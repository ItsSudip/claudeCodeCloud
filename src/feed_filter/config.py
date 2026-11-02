"""Application settings.

Settings are read from environment variables (optionally loaded from a ``.env`` file)
and from ``sources.toml`` which lists what to collect. Nothing here touches the network.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class SourcesConfig:
    """What to collect, loaded from ``sources.toml``."""

    rss_feeds: list[str] = field(default_factory=list)
    subreddits: list[str] = field(default_factory=list)
    reddit_listing: str = "top"
    reddit_timeframe: str = "day"
    reddit_limit: int = 25
    hn_min_points: int = 50
    hn_limit: int = 50
    x_accounts: list[str] = field(default_factory=list)
    x_search_queries: list[str] = field(default_factory=list)
    x_limit: int = 50
    instagram_accounts: list[str] = field(default_factory=list)
    instagram_limit: int = 20
    facebook_pages: list[str] = field(default_factory=list)
    facebook_limit: int = 20

    @classmethod
    def from_file(cls, path: Path) -> SourcesConfig:
        if not path.exists():
            return cls()
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        rss = data.get("rss", {})
        reddit = data.get("reddit", {})
        hn = data.get("hackernews", {})
        x = data.get("x", {})
        instagram = data.get("instagram", {})
        facebook = data.get("facebook", {})
        return cls(
            rss_feeds=list(rss.get("feeds", [])),
            subreddits=list(reddit.get("subreddits", [])),
            reddit_listing=str(reddit.get("listing", "top")),
            reddit_timeframe=str(reddit.get("timeframe", "day")),
            reddit_limit=int(reddit.get("limit", 25)),
            hn_min_points=int(hn.get("min_points", 50)),
            hn_limit=int(hn.get("limit", 50)),
            x_accounts=list(x.get("accounts", [])),
            x_search_queries=list(x.get("search_queries", [])),
            x_limit=int(x.get("limit", 50)),
            instagram_accounts=list(instagram.get("accounts", [])),
            instagram_limit=int(instagram.get("limit", 20)),
            facebook_pages=list(facebook.get("pages", [])),
            facebook_limit=int(facebook.get("limit", 20)),
        )


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Build with :func:`load_settings`."""

    project_root: Path
    database_url: str
    profile_path: Path
    sources_path: Path
    reports_dir: Path

    # LLM
    llm_provider: str  # "anthropic" | "mock"
    anthropic_model: str
    anthropic_effort: str
    llm_batch_size: int
    llm_max_items: int

    # Providers
    enabled_providers: tuple[str, ...]
    allow_mock_fallback: bool
    http_timeout: float
    user_agent: str

    # Credentials (all optional)
    reddit_client_id: str | None
    reddit_client_secret: str | None
    reddit_access_token: str | None
    x_bearer_token: str | None
    x_auth_cookie: str | None
    x_nitter_base_url: str | None
    instagram_access_token: str | None
    instagram_user_id: str | None
    instagram_session_cookie: str | None
    facebook_access_token: str | None
    facebook_rssbridge_url: str | None

    # Dashboard
    dashboard_host: str
    dashboard_port: int

    @property
    def sources(self) -> SourcesConfig:
        return SourcesConfig.from_file(self.sources_path)


def load_settings(env_file: Path | None = None, **overrides: object) -> Settings:
    """Load settings from the environment (and ``.env``), applying keyword overrides."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    root = PROJECT_ROOT
    values: dict[str, object] = dict(
        project_root=root,
        database_url=os.getenv(
            "FEED_FILTER_DATABASE_URL", f"sqlite:///{root / 'data' / 'feed_filter.db'}"
        ),
        profile_path=Path(os.getenv("FEED_FILTER_PROFILE", root / "interest_profile.md")),
        sources_path=Path(os.getenv("FEED_FILTER_SOURCES", root / "sources.toml")),
        reports_dir=Path(os.getenv("FEED_FILTER_REPORTS_DIR", root / "reports")),
        llm_provider=os.getenv("FEED_FILTER_LLM_PROVIDER", "anthropic").strip().lower(),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5"),
        anthropic_effort=os.getenv("ANTHROPIC_EFFORT", "medium"),
        llm_batch_size=_env_int("FEED_FILTER_LLM_BATCH_SIZE", 12),
        llm_max_items=_env_int("FEED_FILTER_LLM_MAX_ITEMS", 200),
        enabled_providers=tuple(
            p.strip().lower()
            for p in os.getenv(
                "FEED_FILTER_PROVIDERS", "hackernews,reddit,rss,x,instagram,facebook"
            ).split(",")
            if p.strip()
        ),
        allow_mock_fallback=_env_bool("FEED_FILTER_ALLOW_MOCK_FALLBACK", True),
        http_timeout=float(os.getenv("FEED_FILTER_HTTP_TIMEOUT", "20")),
        user_agent=os.getenv(
            "FEED_FILTER_USER_AGENT",
            "feed-filter/0.1 (+https://github.com/itssudip/claudecodecloud)",
        ),
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID") or None,
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET") or None,
        reddit_access_token=os.getenv("REDDIT_ACCESS_TOKEN") or None,
        x_bearer_token=os.getenv("X_BEARER_TOKEN") or None,
        x_auth_cookie=os.getenv("X_AUTH_COOKIE") or None,
        x_nitter_base_url=(os.getenv("X_NITTER_BASE_URL") or "").rstrip("/") or None,
        instagram_access_token=os.getenv("INSTAGRAM_ACCESS_TOKEN") or None,
        instagram_user_id=os.getenv("INSTAGRAM_USER_ID") or None,
        instagram_session_cookie=os.getenv("INSTAGRAM_SESSION_COOKIE") or None,
        facebook_access_token=os.getenv("FACEBOOK_ACCESS_TOKEN") or None,
        facebook_rssbridge_url=(os.getenv("FACEBOOK_RSSBRIDGE_URL") or "").rstrip("/") or None,
        dashboard_host=os.getenv("FEED_FILTER_DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=_env_int("FEED_FILTER_DASHBOARD_PORT", 8000),
    )
    values.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(**values)  # type: ignore[arg-type]
