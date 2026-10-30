"""Reddit provider.

Two modes:

* **Anonymous JSON** (default): ``https://www.reddit.com/r/<sub>/top.json``. Works without
  credentials but Reddit rate-limits aggressively and sometimes blocks cloud IPs (403/429).
* **OAuth**: set ``REDDIT_ACCESS_TOKEN`` (or ``REDDIT_CLIENT_ID`` + ``REDDIT_CLIENT_SECRET`` for
  the client-credentials flow) and requests go to ``oauth.reddit.com`` with a bearer token,
  which is far more reliable.

If both fail and mock fallback is enabled, the collector swaps in fixture data so the rest of
the pipeline still runs.
"""

from __future__ import annotations

import logging

import httpx

from feed_filter.providers.base import CollectedItem, Provider, ProviderError, parse_timestamp

log = logging.getLogger(__name__)

PUBLIC_BASE = "https://www.reddit.com"
OAUTH_BASE = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"


class RedditProvider(Provider):
    name = "reddit"

    def __init__(
        self,
        subreddits: list[str],
        *,
        listing: str = "top",
        timeframe: str = "day",
        limit: int = 25,
        access_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        user_agent: str = "feed-filter/0.1",
    ):
        self.subreddits = [s.strip().removeprefix("r/") for s in subreddits if s.strip()]
        self.listing = listing
        self.timeframe = timeframe
        self.limit = min(max(limit, 1), 100)
        self.access_token = access_token
        self.client_id = client_id
        self.client_secret = client_secret
        self._client = http_client or httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )

    # ------------------------------------------------------------------ auth
    def _ensure_token(self) -> None:
        if self.access_token or not (self.client_id and self.client_secret):
            return
        try:
            response = self._client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
            )
            response.raise_for_status()
            self.access_token = response.json().get("access_token")
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Reddit OAuth token request failed: {exc}") from exc

    # ------------------------------------------------------------------ fetch
    def _fetch_listing(self, subreddit: str) -> list[dict]:
        self._ensure_token()
        if self.access_token:
            url = f"{OAUTH_BASE}/r/{subreddit}/{self.listing}"
            headers = {"Authorization": f"Bearer {self.access_token}"}
        else:
            url = f"{PUBLIC_BASE}/r/{subreddit}/{self.listing}.json"
            headers = {}
        params = {"t": self.timeframe, "limit": self.limit, "raw_json": 1}
        try:
            response = self._client.get(url, params=params, headers=headers)
            if response.status_code in (401, 403, 429):
                raise ProviderError(
                    f"Reddit refused r/{subreddit} with HTTP {response.status_code}. "
                    "Set REDDIT_ACCESS_TOKEN or REDDIT_CLIENT_ID/SECRET (see README)."
                )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Reddit request for r/{subreddit} failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"Reddit returned invalid JSON for r/{subreddit}: {exc}") from exc
        return [child.get("data", {}) for child in payload.get("data", {}).get("children", [])]

    def collect(self) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        failures: list[str] = []
        for subreddit in self.subreddits:
            try:
                posts = self._fetch_listing(subreddit)
            except ProviderError as exc:
                log.warning("reddit: %s", exc)
                failures.append(str(exc))
                continue
            for post in posts:
                if not post.get("id") or not post.get("title"):
                    continue
                if post.get("stickied") or post.get("over_18"):
                    continue
                permalink = f"https://www.reddit.com{post.get('permalink', '')}"
                external_url = post.get("url_overridden_by_dest") or post.get("url")
                is_self = bool(post.get("is_self"))
                items.append(
                    CollectedItem(
                        provider=self.name,
                        external_id=str(post["id"]),
                        title=post["title"],
                        url=permalink if is_self else (external_url or permalink),
                        author=post.get("author"),
                        body=post.get("selftext") or None,
                        source_name=f"r/{post.get('subreddit', subreddit)}",
                        score=post.get("score"),
                        comments=post.get("num_comments"),
                        published_at=parse_timestamp(post.get("created_utc")),
                        extra={"discussion_url": permalink, "flair": post.get("link_flair_text")},
                    )
                )
        if not items and failures:
            raise ProviderError("; ".join(failures))
        log.info("reddit: collected %d posts from %d subreddits", len(items), len(self.subreddits))
        return items
