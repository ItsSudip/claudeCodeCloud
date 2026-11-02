"""Instagram provider.

Instagram has no free read API for arbitrary accounts, so this provider layers three strategies:

1. **Graph API (official)** - set ``INSTAGRAM_ACCESS_TOKEN`` and ``INSTAGRAM_USER_ID`` (the
   Instagram *professional* account linked to your Facebook Page). For each handle in
   ``sources.toml`` it calls *Business Discovery*, which returns recent media of any public
   business/creator account. The special handle ``me`` fetches your own media.
   Docs: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/business-discovery
2. **Web profile scrape with your session cookie** - set ``INSTAGRAM_SESSION_COOKIE`` to the
   ``sessionid=...`` cookie from a logged-in browser. Uses the same JSON endpoint the website
   uses (``/api/v1/users/web_profile_info/``). Instagram changes this without notice and may
   rate-limit or log out the session; treat it as a plug-in point.
3. Nothing works -> the collector substitutes the bundled fixture when fallback is enabled.

Reels and videos carry a duration where the API exposes one, so sessions can be timed precisely.
"""

from __future__ import annotations

import logging

import httpx

from feed_filter.providers.base import CollectedItem, Provider, ProviderError, parse_timestamp

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"
WEB_PROFILE_URL = "https://www.instagram.com/api/v1/users/web_profile_info/"
WEB_APP_ID = "936619743392459"  # public x-ig-app-id used by instagram.com itself

MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,"
    "like_count,comments_count,username"
)


def _title_from_caption(caption: str | None, fallback: str) -> str:
    if not caption:
        return fallback
    first_line = caption.strip().splitlines()[0].strip()
    return (first_line[:140] + "…") if len(first_line) > 140 else (first_line or fallback)


def _kind(media_type: str | None, product_type: str | None = None) -> str:
    media_type = (media_type or "").upper()
    if media_type == "VIDEO":
        return "short_video" if (product_type or "").upper() in ("REELS", "") else "long_video"
    if media_type in ("IMAGE", "CAROUSEL_ALBUM"):
        return "image"
    return "post"


class InstagramProvider(Provider):
    name = "instagram"

    def __init__(
        self,
        accounts: list[str],
        *,
        limit: int = 20,
        access_token: str | None = None,
        user_id: str | None = None,
        session_cookie: str | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        user_agent: str = "feed-filter/0.1",
    ):
        self.accounts = [a.strip().lstrip("@") for a in accounts if a.strip()]
        self.limit = min(max(limit, 1), 50)
        self.access_token = access_token
        self.user_id = user_id
        self.session_cookie = session_cookie
        self._client = http_client or httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )

    # ------------------------------------------------------------------ strategy 1: Graph API
    def _graph_get(self, path: str, params: dict) -> dict:
        try:
            response = self._client.get(
                f"{GRAPH_BASE}/{path}", params={**params, "access_token": self.access_token}
            )
            payload = response.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Instagram Graph API request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"Instagram Graph API returned invalid JSON: {exc}") from exc
        if response.status_code >= 400 or "error" in payload:
            message = payload.get("error", {}).get("message", f"HTTP {response.status_code}")
            raise ProviderError(f"Instagram Graph API error: {message}")
        return payload

    def _media_to_items(self, media: list[dict], handle: str) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        for node in media:
            media_id = node.get("id")
            permalink = node.get("permalink")
            if not media_id or not permalink:
                continue
            username = node.get("username") or handle
            caption = node.get("caption")
            kind = _kind(node.get("media_type"), node.get("media_product_type"))
            items.append(
                CollectedItem(
                    provider=self.name,
                    external_id=str(media_id),
                    title=_title_from_caption(caption, f"@{username} {kind.replace('_', ' ')}"),
                    url=permalink,
                    author=f"@{username}",
                    body=caption,
                    source_name="Instagram",
                    score=node.get("like_count"),
                    comments=node.get("comments_count"),
                    published_at=parse_timestamp(node.get("timestamp")),
                    media_type=kind,
                    media_url=node.get("media_url") or node.get("thumbnail_url"),
                    extra={"strategy": "graph_api", "thumbnail": node.get("thumbnail_url")},
                )
            )
        return items

    def _collect_graph(self) -> list[CollectedItem]:
        if not self.user_id:
            raise ProviderError("INSTAGRAM_USER_ID is required alongside INSTAGRAM_ACCESS_TOKEN")
        items: list[CollectedItem] = []
        for handle in self.accounts:
            if handle.lower() == "me":
                payload = self._graph_get(
                    f"{self.user_id}/media", {"fields": MEDIA_FIELDS, "limit": self.limit}
                )
                items.extend(self._media_to_items(payload.get("data", []), "me"))
                continue
            fields = (
                f"business_discovery.username({handle})"
                f"{{username,media.limit({self.limit}){{{MEDIA_FIELDS}}}}}"
            )
            payload = self._graph_get(self.user_id, {"fields": fields})
            discovery = payload.get("business_discovery", {})
            items.extend(self._media_to_items(discovery.get("media", {}).get("data", []), handle))
        return items

    # ------------------------------------------------------------------ strategy 2: web scrape
    def _collect_web(self) -> list[CollectedItem]:
        headers = {
            "x-ig-app-id": WEB_APP_ID,
            "Cookie": self.session_cookie or "",
            "Referer": "https://www.instagram.com/",
        }
        items: list[CollectedItem] = []
        errors: list[str] = []
        for handle in self.accounts:
            if handle.lower() == "me":
                continue
            try:
                response = self._client.get(
                    WEB_PROFILE_URL, params={"username": handle}, headers=headers
                )
                if response.status_code in (401, 403, 429):
                    raise ProviderError(
                        f"Instagram web endpoint refused @{handle} (HTTP {response.status_code}); "
                        "refresh INSTAGRAM_SESSION_COOKIE"
                    )
                response.raise_for_status()
                user = response.json().get("data", {}).get("user") or {}
            except httpx.HTTPError as exc:
                errors.append(f"@{handle}: {exc}")
                continue
            except ValueError:
                errors.append(f"@{handle}: non-JSON response (login wall?)")
                continue
            edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])
            for edge in edges[: self.limit]:
                node = edge.get("node", {})
                shortcode = node.get("shortcode")
                if not shortcode:
                    continue
                caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
                caption = caption_edges[0]["node"].get("text") if caption_edges else None
                is_video = bool(node.get("is_video"))
                duration = node.get("video_duration")
                kind = "short_video" if is_video else "image"
                if is_video and duration and duration > 180:
                    kind = "long_video"
                path = "reel" if is_video else "p"
                items.append(
                    CollectedItem(
                        provider=self.name,
                        external_id=str(node.get("id") or shortcode),
                        title=_title_from_caption(caption, f"@{handle} {kind.replace('_', ' ')}"),
                        url=f"https://www.instagram.com/{path}/{shortcode}/",
                        author=f"@{handle}",
                        body=caption,
                        source_name="Instagram",
                        score=node.get("edge_liked_by", {}).get("count"),
                        comments=node.get("edge_media_to_comment", {}).get("count"),
                        published_at=parse_timestamp(node.get("taken_at_timestamp")),
                        media_type=kind,
                        media_url=node.get("video_url") or node.get("display_url"),
                        duration_seconds=int(duration) if duration else None,
                        extra={"strategy": "web_profile", "thumbnail": node.get("display_url")},
                    )
                )
        if not items and errors:
            raise ProviderError("; ".join(errors))
        return items

    # ------------------------------------------------------------------ orchestrate
    def collect(self) -> list[CollectedItem]:
        if not self.accounts:
            log.info("instagram: no accounts configured; skipping")
            return []
        strategies = []
        if self.access_token:
            strategies.append(("graph_api", self._collect_graph))
        if self.session_cookie:
            strategies.append(("web_profile", self._collect_web))
        if not strategies:
            raise ProviderError(
                "Instagram needs INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_USER_ID or "
                "INSTAGRAM_SESSION_COOKIE (see README)"
            )
        errors: list[str] = []
        for label, strategy in strategies:
            try:
                items = strategy()
            except ProviderError as exc:
                log.warning("instagram[%s]: %s", label, exc)
                errors.append(f"{label}: {exc}")
                continue
            if items:
                log.info("instagram[%s]: collected %d posts", label, len(items))
                return items
            errors.append(f"{label}: no items")
        raise ProviderError("All Instagram strategies failed: " + "; ".join(errors))
