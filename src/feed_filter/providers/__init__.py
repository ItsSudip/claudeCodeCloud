"""Provider registry.

:func:`build_providers` turns settings into concrete provider instances. Each provider is
independent, so adding a new source means adding one module here and one line in the registry.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from feed_filter.config import Settings
from feed_filter.providers.base import CollectedItem, Provider, ProviderError
from feed_filter.providers.hackernews import HackerNewsProvider
from feed_filter.providers.mock import FIXTURES_DIR, MockProvider
from feed_filter.providers.reddit import RedditProvider
from feed_filter.providers.rss import RSSProvider
from feed_filter.providers.x import XProvider

__all__ = [
    "CollectedItem",
    "HackerNewsProvider",
    "MockProvider",
    "Provider",
    "ProviderError",
    "RSSProvider",
    "RedditProvider",
    "XProvider",
    "build_providers",
    "mock_fallback_for",
]

ProviderFactory = Callable[[Settings, httpx.Client], Provider | None]


def _hn(settings: Settings, client: httpx.Client) -> Provider:
    src = settings.sources
    return HackerNewsProvider(min_points=src.hn_min_points, limit=src.hn_limit, http_client=client)


def _reddit(settings: Settings, client: httpx.Client) -> Provider | None:
    src = settings.sources
    if not src.subreddits:
        return None
    return RedditProvider(
        src.subreddits,
        listing=src.reddit_listing,
        timeframe=src.reddit_timeframe,
        limit=src.reddit_limit,
        access_token=settings.reddit_access_token,
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        http_client=client,
    )


def _rss(settings: Settings, client: httpx.Client) -> Provider | None:
    src = settings.sources
    if not src.rss_feeds:
        return None
    return RSSProvider(src.rss_feeds, http_client=client)


def _x(settings: Settings, client: httpx.Client) -> Provider | None:
    src = settings.sources
    if not src.x_accounts and not src.x_search_queries:
        return None
    return XProvider(
        src.x_accounts,
        src.x_search_queries,
        limit=src.x_limit,
        bearer_token=settings.x_bearer_token,
        auth_cookie=settings.x_auth_cookie,
        nitter_base_url=settings.x_nitter_base_url,
        http_client=client,
    )


REGISTRY: dict[str, ProviderFactory] = {
    "hackernews": _hn,
    "reddit": _reddit,
    "rss": _rss,
    "x": _x,
}


def mock_fallback_for(provider_name: str) -> MockProvider | None:
    """Return the bundled fixture provider for ``provider_name`` if one exists."""
    fixture = FIXTURES_DIR / f"{provider_name}.json"
    if not fixture.exists():
        return None
    return MockProvider(fixture, provider_name=provider_name)


def build_providers(
    settings: Settings, *, http_client: httpx.Client | None = None, mock: bool = False
) -> list[Provider]:
    """Instantiate the enabled providers (or their mock equivalents when ``mock`` is True)."""
    client = http_client or httpx.Client(
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )
    providers: list[Provider] = []
    for name in settings.enabled_providers:
        if mock:
            fallback = mock_fallback_for(name)
            if fallback:
                providers.append(fallback)
            continue
        factory = REGISTRY.get(name)
        if factory is None:
            raise ValueError(f"Unknown provider {name!r}. Known: {sorted(REGISTRY)}")
        provider = factory(settings, client)
        if provider is not None:
            providers.append(provider)
    return providers
