"""Hacker News provider.

Uses the public Algolia search API (no auth, generous limits) to pull the day's best stories.
Docs: https://hn.algolia.com/api
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from feed_filter.providers.base import CollectedItem, Provider, ProviderError, parse_timestamp

log = logging.getLogger(__name__)

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"


class HackerNewsProvider(Provider):
    name = "hackernews"

    def __init__(
        self,
        *,
        min_points: int = 50,
        limit: int = 50,
        hours: int = 24,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        user_agent: str = "feed-filter/0.1",
    ):
        self.min_points = min_points
        self.limit = min(max(limit, 1), 100)
        self.hours = hours
        self._client = http_client or httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )

    def collect(self) -> list[CollectedItem]:
        since = int((datetime.now(UTC) - timedelta(hours=self.hours)).timestamp())
        params = {
            "tags": "story",
            "numericFilters": f"points>={self.min_points},created_at_i>={since}",
            "hitsPerPage": self.limit,
        }
        try:
            response = self._client.get(ALGOLIA_URL, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Hacker News request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"Hacker News returned invalid JSON: {exc}") from exc

        items: list[CollectedItem] = []
        for hit in payload.get("hits", []):
            object_id = hit.get("objectID")
            title = hit.get("title") or hit.get("story_title")
            if not object_id or not title:
                continue
            hn_url = f"https://news.ycombinator.com/item?id={object_id}"
            items.append(
                CollectedItem(
                    provider=self.name,
                    external_id=str(object_id),
                    title=title,
                    url=hit.get("url") or hn_url,
                    author=hit.get("author"),
                    body=hit.get("story_text"),
                    source_name="Hacker News",
                    score=hit.get("points"),
                    comments=hit.get("num_comments"),
                    published_at=parse_timestamp(hit.get("created_at_i") or hit.get("created_at")),
                    extra={"discussion_url": hn_url},
                )
            )
        log.info("hackernews: collected %d stories (>=%d points)", len(items), self.min_points)
        return items
