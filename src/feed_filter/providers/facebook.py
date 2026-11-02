"""Facebook provider (Pages).

1. **Graph API (official)** - set ``FACEBOOK_ACCESS_TOKEN`` (a Page token for pages you manage,
   or a user/app token with *Page Public Content Access* for other public pages). Each page in
   ``sources.toml`` is queried with ``/{page}/posts``.
   Docs: https://developers.facebook.com/docs/graph-api/reference/page/feed
2. **RSS-Bridge** - set ``FACEBOOK_RSSBRIDGE_URL`` to an RSS-Bridge instance you run; its
   FacebookBridge turns a public page into an Atom feed. No Facebook credentials needed.
3. Nothing works -> the collector substitutes the bundled fixture when fallback is enabled.
"""

from __future__ import annotations

import logging

import httpx

from feed_filter.providers.base import CollectedItem, Provider, ProviderError, parse_timestamp
from feed_filter.providers.rss import RSSProvider

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"
POST_FIELDS = (
    "id,message,story,permalink_url,created_time,full_picture,"
    "attachments{media_type,type,url,title,description},"
    "shares,reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)"
)


def _title(post: dict) -> str:
    message = (post.get("message") or "").strip()
    if message:
        first = message.splitlines()[0].strip()
        return (first[:140] + "…") if len(first) > 140 else first
    attachments = post.get("attachments", {}).get("data", [])
    if attachments and attachments[0].get("title"):
        return attachments[0]["title"]
    return (post.get("story") or "Facebook post").strip()


def _kind(post: dict) -> str:
    attachments = post.get("attachments", {}).get("data", [])
    if not attachments:
        return "post"
    media_type = (attachments[0].get("media_type") or attachments[0].get("type") or "").lower()
    if "video" in media_type:
        return "short_video"
    if "photo" in media_type or "album" in media_type or "image" in media_type:
        return "image"
    if "link" in media_type or "share" in media_type:
        return "article"
    return "post"


class FacebookProvider(Provider):
    name = "facebook"

    def __init__(
        self,
        pages: list[str],
        *,
        limit: int = 20,
        access_token: str | None = None,
        rssbridge_url: str | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        user_agent: str = "feed-filter/0.1",
    ):
        self.pages = [p.strip().strip("/") for p in pages if p.strip()]
        self.limit = min(max(limit, 1), 100)
        self.access_token = access_token
        self.rssbridge_url = rssbridge_url.rstrip("/") if rssbridge_url else None
        self._client = http_client or httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )

    # ------------------------------------------------------------------ strategy 1: Graph API
    def _collect_graph(self) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        errors: list[str] = []
        for page in self.pages:
            try:
                response = self._client.get(
                    f"{GRAPH_BASE}/{page}/posts",
                    params={
                        "fields": POST_FIELDS,
                        "limit": self.limit,
                        "access_token": self.access_token,
                    },
                )
                payload = response.json()
            except httpx.HTTPError as exc:
                errors.append(f"{page}: {exc}")
                continue
            except ValueError:
                errors.append(f"{page}: invalid JSON")
                continue
            if response.status_code >= 400 or "error" in payload:
                errors.append(
                    f"{page}: {payload.get('error', {}).get('message', response.status_code)}"
                )
                continue
            for post in payload.get("data", []):
                post_id = post.get("id")
                permalink = post.get("permalink_url")
                if not post_id or not permalink:
                    continue
                attachments = post.get("attachments", {}).get("data", [])
                link = attachments[0].get("url") if attachments else None
                kind = _kind(post)
                items.append(
                    CollectedItem(
                        provider=self.name,
                        external_id=str(post_id),
                        title=_title(post),
                        url=link if kind == "article" and link else permalink,
                        author=page,
                        body=post.get("message")
                        or (attachments[0].get("description") if attachments else None),
                        source_name=f"Facebook · {page}",
                        score=post.get("reactions", {}).get("summary", {}).get("total_count"),
                        comments=post.get("comments", {}).get("summary", {}).get("total_count"),
                        published_at=parse_timestamp(post.get("created_time")),
                        media_type=kind,
                        media_url=post.get("full_picture"),
                        extra={
                            "strategy": "graph_api",
                            "discussion_url": permalink,
                            "shares": post.get("shares", {}).get("count"),
                        },
                    )
                )
        if not items and errors:
            raise ProviderError("; ".join(errors))
        return items

    # ------------------------------------------------------------------ strategy 2: RSS-Bridge
    def _collect_rssbridge(self) -> list[CollectedItem]:
        assert self.rssbridge_url
        feeds = [
            f"{self.rssbridge_url}/?action=display&bridge=FacebookBridge&context=User&u={page}&format=Atom"
            for page in self.pages
        ]
        rss = RSSProvider(feeds, http_client=self._client, max_per_feed=self.limit)
        items = rss.collect()
        for item in items:
            item.provider = self.name
            item.source_name = f"Facebook · {item.source_name}"
            item.media_type = item.media_type or "post"
            item.extra["strategy"] = "rssbridge"
        return items

    # ------------------------------------------------------------------ orchestrate
    def collect(self) -> list[CollectedItem]:
        if not self.pages:
            log.info("facebook: no pages configured; skipping")
            return []
        strategies = []
        if self.access_token:
            strategies.append(("graph_api", self._collect_graph))
        if self.rssbridge_url:
            strategies.append(("rssbridge", self._collect_rssbridge))
        if not strategies:
            raise ProviderError(
                "Facebook needs FACEBOOK_ACCESS_TOKEN or FACEBOOK_RSSBRIDGE_URL (see README)"
            )
        errors: list[str] = []
        for label, strategy in strategies:
            try:
                items = strategy()
            except ProviderError as exc:
                log.warning("facebook[%s]: %s", label, exc)
                errors.append(f"{label}: {exc}")
                continue
            if items:
                log.info("facebook[%s]: collected %d posts", label, len(items))
                return items
            errors.append(f"{label}: no items")
        raise ProviderError("All Facebook strategies failed: " + "; ".join(errors))
