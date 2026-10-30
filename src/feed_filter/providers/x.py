"""X (Twitter) provider.

X's official API is expensive and heavily rate-limited, so this provider supports three
strategies, tried in order until one yields items:

1. **Official API v2** - set ``X_BEARER_TOKEN``. Uses recent search for each query in
   ``sources.toml`` and the user-tweets endpoint for each account. Needs at least the Basic tier.
2. **Nitter RSS scrape** - set ``X_NITTER_BASE_URL`` to a Nitter instance you run or trust
   (e.g. ``https://nitter.example.org``). Each account becomes ``<base>/<handle>/rss``.
   No credentials needed; quality depends on the instance.
3. **Syndication timeline + auth cookie** - the lightweight ``syndication.twitter.com`` widget
   endpoint. Works anonymously for some public accounts; set ``X_AUTH_COOKIE`` (your browser's
   ``auth_token=...; ct0=...`` cookie string) to improve reliability. This is a best-effort
   scrape and X changes it without notice - treat it as a plug-in point, not a guarantee.

If none succeed the collector can fall back to the bundled mock fixture so the pipeline still
runs end-to-end.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from feed_filter.providers.base import CollectedItem, Provider, ProviderError, parse_timestamp
from feed_filter.providers.rss import RSSProvider

log = logging.getLogger(__name__)

API_BASE = "https://api.twitter.com/2"
SYNDICATION_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


class XProvider(Provider):
    name = "x"

    def __init__(
        self,
        accounts: list[str],
        search_queries: list[str] | None = None,
        *,
        limit: int = 50,
        bearer_token: str | None = None,
        auth_cookie: str | None = None,
        nitter_base_url: str | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        user_agent: str = "feed-filter/0.1",
    ):
        self.accounts = [a.strip().lstrip("@") for a in accounts if a.strip()]
        self.search_queries = [q.strip() for q in (search_queries or []) if q.strip()]
        self.limit = min(max(limit, 10), 100)
        self.bearer_token = bearer_token
        self.auth_cookie = auth_cookie
        self.nitter_base_url = nitter_base_url.rstrip("/") if nitter_base_url else None
        self._client = http_client or httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )

    # ------------------------------------------------------------------ strategy 1: API v2
    def _api_get(self, path: str, params: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        try:
            response = self._client.get(f"{API_BASE}{path}", params=params, headers=headers)
            if response.status_code in (401, 403):
                raise ProviderError(
                    f"X API rejected the bearer token (HTTP {response.status_code})"
                )
            if response.status_code == 429:
                raise ProviderError("X API rate limit reached (HTTP 429)")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"X API request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"X API returned invalid JSON: {exc}") from exc

    def _tweets_from_api_payload(
        self, payload: dict, default_handle: str | None
    ) -> list[CollectedItem]:
        users = {u["id"]: u for u in payload.get("includes", {}).get("users", [])}
        items: list[CollectedItem] = []
        for tweet in payload.get("data", []):
            author = users.get(tweet.get("author_id"), {})
            handle = author.get("username") or default_handle or "unknown"
            metrics = tweet.get("public_metrics", {})
            text = tweet.get("text", "")
            items.append(
                CollectedItem(
                    provider=self.name,
                    external_id=str(tweet["id"]),
                    title=text[:200],
                    url=f"https://x.com/{handle}/status/{tweet['id']}",
                    author=f"@{handle}",
                    body=text,
                    source_name="X",
                    score=metrics.get("like_count"),
                    comments=metrics.get("reply_count"),
                    published_at=parse_timestamp(tweet.get("created_at")),
                    extra={"retweets": metrics.get("retweet_count"), "strategy": "api_v2"},
                )
            )
        return items

    def _collect_api(self) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        fields = {
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "username",
        }
        for query in self.search_queries:
            payload = self._api_get(
                "/tweets/search/recent",
                {
                    "query": f"{query} -is:retweet -is:reply lang:en",
                    "max_results": self.limit,
                    **fields,
                },
            )
            items.extend(self._tweets_from_api_payload(payload, None))
        for handle in self.accounts:
            user = self._api_get(f"/users/by/username/{handle}", {})
            user_id = user.get("data", {}).get("id")
            if not user_id:
                continue
            payload = self._api_get(
                f"/users/{user_id}/tweets",
                {"max_results": self.limit, "exclude": "retweets,replies", **fields},
            )
            items.extend(self._tweets_from_api_payload(payload, handle))
        return items

    # ------------------------------------------------------------------ strategy 2: Nitter RSS
    def _collect_nitter(self) -> list[CollectedItem]:
        assert self.nitter_base_url
        feeds = [f"{self.nitter_base_url}/{handle}/rss" for handle in self.accounts]
        rss = RSSProvider(feeds, http_client=self._client, max_per_feed=self.limit)
        items = rss.collect()
        for item in items:
            item.provider = self.name
            item.source_name = "X"
            item.extra["strategy"] = "nitter"
        return items

    # ------------------------------------------------------------------ strategy 3: syndication
    def _collect_syndication(self) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        headers = {}
        if self.auth_cookie:
            headers["Cookie"] = self.auth_cookie
        for handle in self.accounts:
            try:
                response = self._client.get(SYNDICATION_URL.format(handle=handle), headers=headers)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("x: syndication fetch failed for @%s: %s", handle, exc)
                continue
            match = _NEXT_DATA_RE.search(response.text)
            if not match:
                log.warning("x: syndication page for @%s had no __NEXT_DATA__ payload", handle)
                continue
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            entries = (
                data.get("props", {}).get("pageProps", {}).get("timeline", {}).get("entries", [])
            )
            for entry in entries[: self.limit]:
                tweet = entry.get("content", {}).get("tweet", {})
                tweet_id = tweet.get("id_str")
                text = tweet.get("full_text") or tweet.get("text")
                if not tweet_id or not text:
                    continue
                items.append(
                    CollectedItem(
                        provider=self.name,
                        external_id=str(tweet_id),
                        title=text[:200],
                        url=f"https://x.com/{handle}/status/{tweet_id}",
                        author=f"@{handle}",
                        body=text,
                        source_name="X",
                        score=tweet.get("favorite_count"),
                        comments=tweet.get("reply_count"),
                        published_at=parse_timestamp(tweet.get("created_at")),
                        extra={"retweets": tweet.get("retweet_count"), "strategy": "syndication"},
                    )
                )
        return items

    # ------------------------------------------------------------------ orchestrate
    def collect(self) -> list[CollectedItem]:
        if not self.accounts and not self.search_queries:
            log.info("x: no accounts or search queries configured; skipping")
            return []
        errors: list[str] = []
        strategies = []
        if self.bearer_token:
            strategies.append(("api_v2", self._collect_api))
        if self.nitter_base_url and self.accounts:
            strategies.append(("nitter", self._collect_nitter))
        if self.accounts:
            strategies.append(("syndication", self._collect_syndication))

        for label, strategy in strategies:
            try:
                items = strategy()
            except ProviderError as exc:
                log.warning("x[%s]: %s", label, exc)
                errors.append(f"{label}: {exc}")
                continue
            if items:
                log.info("x[%s]: collected %d posts", label, len(items))
                return items
            errors.append(f"{label}: no items")
        raise ProviderError("All X strategies failed: " + "; ".join(errors))
