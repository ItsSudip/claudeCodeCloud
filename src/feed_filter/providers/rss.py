"""RSS / Atom provider built on feedparser."""

from __future__ import annotations

import logging
import re
from html import unescape

import feedparser
import httpx

from feed_filter.providers.base import CollectedItem, Provider, ProviderError, parse_timestamp

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None, limit: int = 2000) -> str | None:
    if not text:
        return None
    cleaned = unescape(_TAG_RE.sub(" ", text))
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;:!?)\]])", r"\1", cleaned).strip()
    return cleaned[:limit] or None


class RSSProvider(Provider):
    name = "rss"

    def __init__(
        self,
        feed_urls: list[str],
        *,
        max_per_feed: int = 30,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        user_agent: str = "feed-filter/0.1",
    ):
        self.feed_urls = [u.strip() for u in feed_urls if u.strip()]
        self.max_per_feed = max_per_feed
        self._client = http_client or httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )

    def _fetch(self, url: str) -> feedparser.FeedParserDict:
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"RSS fetch failed for {url}: {exc}") from exc
        parsed = feedparser.parse(response.content)
        if parsed.bozo and not parsed.entries:
            raise ProviderError(f"RSS parse failed for {url}: {parsed.bozo_exception}")
        return parsed

    def collect(self) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        failures: list[str] = []
        for url in self.feed_urls:
            try:
                parsed = self._fetch(url)
            except ProviderError as exc:
                log.warning("rss: %s", exc)
                failures.append(str(exc))
                continue
            feed_title = parsed.feed.get("title") or url
            for entry in parsed.entries[: self.max_per_feed]:
                link = entry.get("link")
                external_id = entry.get("id") or link or entry.get("title")
                if not external_id or not entry.get("title"):
                    continue
                content = None
                if entry.get("content"):
                    content = entry["content"][0].get("value")
                body = strip_html(content or entry.get("summary"))
                items.append(
                    CollectedItem(
                        provider=self.name,
                        external_id=str(external_id)[:255],
                        title=entry["title"],
                        url=link,
                        author=entry.get("author"),
                        body=body,
                        source_name=feed_title,
                        published_at=parse_timestamp(
                            entry.get("published_parsed") or entry.get("updated_parsed")
                        ),
                        extra={
                            "feed_url": url,
                            "tags": [t.get("term") for t in entry.get("tags", [])],
                        },
                    )
                )
        if not items and failures:
            raise ProviderError("; ".join(failures))
        log.info("rss: collected %d entries from %d feeds", len(items), len(self.feed_urls))
        return items
