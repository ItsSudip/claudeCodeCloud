"""Instagram provider.

Instagram has no free read API for arbitrary accounts, so this provider layers three strategies:

1. **Graph API (official)** - set ``INSTAGRAM_ACCESS_TOKEN`` and ``INSTAGRAM_USER_ID`` (the
   Instagram *professional* account linked to your Facebook Page). For each handle in
   ``sources.toml`` it calls *Business Discovery*, which returns recent media of any public
   business/creator account. The special handle ``me`` fetches your own media.
   Docs: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/business-discovery
2. **GraphQL with your session cookie** - set ``INSTAGRAM_SESSION_COOKIE`` to the cookie string
   from a logged-in browser (``sessionid=...; csrftoken=...; ds_user_id=...``). Replays the
   ``PolarisProfilePostsQuery`` request instagram.com itself makes when you open a profile.
   The ``doc_id`` rotates with Instagram's web releases; override it with
   ``INSTAGRAM_GRAPHQL_DOC_ID`` taken from DevTools ("Copy as cURL" on the graphql request).
   The older ``/api/v1/users/web_profile_info/`` endpoint is tried afterwards in case it still
   answers. Instagram changes both without notice; treat them as plug-in points.
3. Nothing works -> the collector substitutes the bundled fixture when fallback is enabled.

Reels and videos carry a duration where the API exposes one, so sessions can be timed precisely.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from feed_filter.providers.base import CollectedItem, Provider, ProviderError, parse_timestamp

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"
# instagram.com serves a "Page Not Found" HTML page to this endpoint unless the request looks
# like the website's own XHR: browser User-Agent, app id, XMLHttpRequest marker. Two hosts serve
# it; the second is tried when the first answers with HTML.
WEB_PROFILE_URLS = (
    "https://www.instagram.com/api/v1/users/web_profile_info/",
    "https://i.instagram.com/api/v1/users/web_profile_info/",
)
WEB_APP_ID = "936619743392459"  # public x-ig-app-id used by instagram.com itself
GRAPHQL_URL = "https://www.instagram.com/graphql/query"
GRAPHQL_FRIENDLY_NAME = "PolarisProfilePostsQuery"
# Instagram rotates GraphQL doc ids with its web releases. Override with INSTAGRAM_GRAPHQL_DOC_ID
# (copy it from a "Copy as cURL" of the profile request in your browser's DevTools).
DEFAULT_GRAPHQL_DOC_ID = "9916845311755308"
ASBD_ID = "129477"
_LOGIN_PAGE_RE = re.compile(r'class="[^"]*not-logged-in')


def _is_login_page(html: str) -> bool:
    return bool(_LOGIN_PAGE_RE.search(html[:4000]))


_DTSG_RE = re.compile(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"')
_LSD_RE = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def web_headers(session_cookie: str | None) -> dict[str, str]:
    """Headers that make the web profile endpoint answer with JSON instead of a 404 page."""
    cookie = (session_cookie or "").strip().rstrip(";")
    if cookie and "=" not in cookie:  # a bare value was pasted; name it
        cookie = f"sessionid={cookie}"
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "x-ig-app-id": WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Referer": "https://www.instagram.com/",
        "Cookie": cookie,
    }
    # Instagram expects the CSRF cookie echoed as a header when it is present.
    csrf = parse_cookies(cookie).get("csrftoken")
    if csrf:
        headers["x-csrftoken"] = csrf
    return headers


def parse_cookies(cookie: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip() and value:
            result[name.strip().lower()] = value.strip()
    return result


def _graphql_kind(node: dict) -> tuple[str, int | None]:
    """Map a GraphQL media node to (content kind, duration seconds)."""
    media_type = node.get("media_type")  # 1 image, 2 video, 8 carousel
    product = (node.get("product_type") or "").lower()
    duration = node.get("video_duration")
    seconds = int(round(float(duration))) if duration else None
    if media_type == 2 or product == "clips" or node.get("video_versions"):
        if seconds and seconds > 180:
            return "long_video", seconds
        return "short_video", seconds
    return "image", None


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
        graphql_doc_id: str | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        user_agent: str = "feed-filter/0.1",
    ):
        self.accounts = [a.strip().lstrip("@") for a in accounts if a.strip()]
        self.limit = min(max(limit, 1), 50)
        self.access_token = access_token
        self.user_id = user_id
        self.session_cookie = session_cookie
        self.graphql_doc_id = graphql_doc_id or DEFAULT_GRAPHQL_DOC_ID
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
    # ------------------------------------------------------------------ strategy 2: GraphQL (web)
    def _page_tokens(self, handle: str, headers: dict[str, str]) -> dict[str, str]:
        """Load the profile page to pick up the per-session tokens the GraphQL call wants."""
        tokens: dict[str, str] = {}
        try:
            response = self._client.get(
                f"https://www.instagram.com/{handle}/",
                headers={**headers, "Accept": "text/html,*/*"},
            )
        except httpx.HTTPError as exc:
            log.debug("instagram: profile page for @%s failed: %s", handle, exc)
            return tokens
        html = response.text
        if _is_login_page(html):
            raise ProviderError(
                f"@{handle}: Instagram returned a login page; INSTAGRAM_SESSION_COOKIE "
                "is missing or expired"
            )
        for key, pattern in (("fb_dtsg", _DTSG_RE), ("lsd", _LSD_RE)):
            match = pattern.search(html)
            if match:
                tokens[key] = match.group(1)
        return tokens

    def _graphql_posts(self, handle: str, headers: dict[str, str]) -> list[dict]:
        cookies = parse_cookies(headers.get("Cookie"))
        tokens = self._page_tokens(handle, headers)
        lsd = tokens.get("lsd") or "AVqbxe3J_YA"
        variables = {
            "data": {
                "count": min(self.limit, 33),
                "include_reel_media_seen_timestamp": True,
                "include_relationship_info": True,
                "latest_besties_reel_media": True,
                "latest_reel_media": True,
            },
            "username": handle,
            "__relay_internal__pv__PolarisIsLoggedInrelayprovider": bool(cookies.get("sessionid")),
            "__relay_internal__pv__PolarisShareSheetV3relayprovider": True,
        }
        form = {
            "av": cookies.get("ds_user_id", "0"),
            "__d": "www",
            "__user": "0",
            "__a": "1",
            "__req": "1",
            "__comet_req": "7",
            "lsd": lsd,
            "jazoest": "2" + str(sum(ord(c) for c in lsd)),
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": GRAPHQL_FRIENDLY_NAME,
            "server_timestamps": "true",
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": self.graphql_doc_id,
        }
        if tokens.get("fb_dtsg"):
            form["fb_dtsg"] = tokens["fb_dtsg"]
        post_headers = {
            **headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "x-fb-lsd": lsd,
            "x-asbd-id": ASBD_ID,
            "x-fb-friendly-name": GRAPHQL_FRIENDLY_NAME,
            "Origin": "https://www.instagram.com",
            "Referer": f"https://www.instagram.com/{handle}/",
        }
        response = self._client.post(GRAPHQL_URL, data=form, headers=post_headers)
        if response.status_code in (401, 403, 429):
            raise ProviderError(
                f"Instagram GraphQL refused @{handle} (HTTP {response.status_code}); "
                "refresh INSTAGRAM_SESSION_COOKIE"
            )
        if "json" not in response.headers.get("content-type", ""):
            raise ProviderError(
                f"@{handle}: GraphQL answered with {response.status_code} non-JSON "
                "(doc id stale? set INSTAGRAM_GRAPHQL_DOC_ID from DevTools)"
            )
        payload = response.json()
        if payload.get("errors"):
            raise ProviderError(f"@{handle}: GraphQL errors: {str(payload['errors'])[:200]}")
        data = payload.get("data") or {}
        connection = data.get("xdt_api__v1__feed__user_timeline_graphql_connection")
        if connection is None:
            raise ProviderError(
                f"@{handle}: unexpected GraphQL shape (keys: {list(data)[:5]}); "
                "set INSTAGRAM_GRAPHQL_DOC_ID from DevTools"
            )
        return [edge.get("node", {}) for edge in connection.get("edges", [])]

    def _graphql_items(self, nodes: list[dict], handle: str) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        for node in nodes:
            code = node.get("code")
            pk = node.get("pk") or node.get("id")
            if not code or not pk:
                continue
            caption = (
                (node.get("caption") or {}).get("text")
                if isinstance(node.get("caption"), dict)
                else None
            )
            username = (node.get("user") or node.get("owner") or {}).get("username") or handle
            kind, seconds = _graphql_kind(node)
            path = (
                "reel"
                if kind.endswith("video") and (node.get("product_type") or "").lower() == "clips"
                else "p"
            )
            video = (node.get("video_versions") or [{}])[0].get("url")
            image = ((node.get("image_versions2") or {}).get("candidates") or [{}])[0].get("url")
            items.append(
                CollectedItem(
                    provider=self.name,
                    external_id=str(pk),
                    title=_title_from_caption(caption, f"@{username} {kind.replace('_', ' ')}"),
                    url=f"https://www.instagram.com/{path}/{code}/",
                    author=f"@{username}",
                    body=caption,
                    source_name="Instagram",
                    score=node.get("like_count"),
                    comments=node.get("comment_count"),
                    published_at=parse_timestamp(node.get("taken_at")),
                    media_type=kind,
                    media_url=video or image,
                    duration_seconds=seconds,
                    extra={"strategy": "graphql", "thumbnail": image},
                )
            )
        return items

    def _collect_graphql(self) -> list[CollectedItem]:
        headers = web_headers(self.session_cookie)
        items: list[CollectedItem] = []
        errors: list[str] = []
        for handle in self.accounts:
            if handle.lower() == "me":
                continue
            try:
                nodes = self._graphql_posts(handle, headers)
            except httpx.HTTPError as exc:
                errors.append(f"@{handle}: {exc}")
                continue
            except ValueError as exc:
                errors.append(f"@{handle}: invalid JSON from GraphQL: {exc}")
                continue
            except ProviderError as exc:
                if "refresh INSTAGRAM_SESSION_COOKIE" in str(exc) or "login page" in str(exc):
                    raise
                errors.append(str(exc))
                continue
            items.extend(self._graphql_items(nodes, handle))
        if not items and errors:
            raise ProviderError("; ".join(errors))
        return items

    # ------------------------------------------------------------- strategy 3: web_profile_info
    def _fetch_web_profile(self, handle: str, headers: dict[str, str]) -> dict:
        """Try each web host; return the user object or raise ProviderError."""
        last_error = "no response"
        for url in WEB_PROFILE_URLS:
            response = self._client.get(url, params={"username": handle}, headers=headers)
            if response.status_code in (401, 403, 429):
                raise ProviderError(
                    f"Instagram web endpoint refused @{handle} (HTTP {response.status_code}); "
                    "refresh INSTAGRAM_SESSION_COOKIE"
                )
            content_type = response.headers.get("content-type", "")
            if response.status_code == 404 and "json" in content_type:
                raise ProviderError(f"@{handle}: account not found")
            if "json" not in content_type:
                # HTML back: either a login wall or the host refusing non-browser traffic.
                if not _is_login_page(response.text):
                    last_error = f"HTTP {response.status_code} HTML from {url} (endpoint blocked)"
                else:
                    raise ProviderError(
                        f"@{handle}: Instagram returned a login page; INSTAGRAM_SESSION_COOKIE "
                        "is missing or expired"
                    )
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                last_error = f"invalid JSON from {url}: {exc}"
                continue
            user = payload.get("data", {}).get("user")
            if user is None:
                last_error = f"{url}: no user in response ({str(payload)[:120]})"
                continue
            return user
        raise ProviderError(f"@{handle}: {last_error}")

    def _collect_web(self) -> list[CollectedItem]:
        headers = web_headers(self.session_cookie)
        items: list[CollectedItem] = []
        errors: list[str] = []
        for handle in self.accounts:
            if handle.lower() == "me":
                continue
            try:
                user = self._fetch_web_profile(handle, headers)
            except httpx.HTTPError as exc:
                errors.append(f"@{handle}: {exc}")
                continue
            except ProviderError as exc:
                if "refresh INSTAGRAM_SESSION_COOKIE" in str(exc) or "login page" in str(exc):
                    raise
                errors.append(str(exc))
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
            strategies.append(("graphql", self._collect_graphql))
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
