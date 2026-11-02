from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from feed_filter.providers import build_providers, mock_fallback_for
from feed_filter.providers.base import (
    CollectedItem,
    ProviderError,
    normalize_url,
    parse_timestamp,
)
from feed_filter.providers.hackernews import HackerNewsProvider
from feed_filter.providers.mock import MockProvider
from feed_filter.providers.reddit import RedditProvider
from feed_filter.providers.rss import RSSProvider, strip_html
from feed_filter.providers.x import XProvider
from tests.conftest import FIXTURES, make_item, mock_client


# ---------------------------------------------------------------- base helpers
class TestBase:
    def test_normalize_url_strips_tracking_and_www(self):
        assert (
            normalize_url("https://www.Example.com/a/b/?utm_source=x&id=2#frag")
            == "https://example.com/a/b?id=2"
        )
        assert normalize_url("") is None
        assert normalize_url(None) is None

    def test_content_hash_is_url_based_and_provider_independent(self):
        a = make_item(
            provider="hackernews", external_id="1", url="https://example.com/p?utm_medium=a"
        )
        b = make_item(provider="reddit", external_id="zz", url="https://www.example.com/p/")
        assert a.content_hash == b.content_hash

    def test_content_hash_falls_back_to_title(self):
        a = make_item(url=None, title="Hello   World ")
        b = make_item(url=None, title="hello world", external_id="2")
        assert a.content_hash == b.content_hash

    def test_item_normalises_title_and_timezone(self):
        item = CollectedItem(
            provider="x",
            external_id=1,
            title="  multi\n line ",
            published_at=datetime(2026, 1, 1, 12, 0),
        )
        assert item.title == "multi line"
        assert item.external_id == "1"
        assert item.published_at.tzinfo is UTC

    @pytest.mark.parametrize(
        "value, expected",
        [
            (1_700_000_000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
            ("2026-10-29T14:10:00Z", datetime(2026, 10, 29, 14, 10, tzinfo=UTC)),
            ("Wed, 29 Oct 2026 08:00:00 GMT", datetime(2026, 10, 29, 8, 0, tzinfo=UTC)),
            ((2026, 10, 29, 8, 0, 0, 2, 302, 0), datetime(2026, 10, 29, 8, 0, tzinfo=UTC)),
            ("not a date", None),
            (None, None),
        ],
    )
    def test_parse_timestamp(self, value, expected):
        assert parse_timestamp(value) == expected


# ---------------------------------------------------------------- mock provider
class TestMockProvider:
    def test_loads_bundled_fixture(self):
        items = MockProvider("hackernews.json").collect()
        assert len(items) >= 5
        assert all(i.provider == "hackernews" for i in items)
        assert all(i.published_at is not None for i in items)

    def test_missing_fixture_raises(self, tmp_path):
        with pytest.raises(ProviderError):
            MockProvider(tmp_path / "nope.json").collect()

    def test_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(ProviderError):
            MockProvider(bad).collect()

    def test_fallback_registry(self):
        assert mock_fallback_for("reddit") is not None
        assert mock_fallback_for("does-not-exist") is None


# ---------------------------------------------------------------- hacker news
class TestHackerNews:
    def test_collects_stories(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "1",
                            "title": "Story one",
                            "url": "https://example.com/1",
                            "author": "a",
                            "points": 120,
                            "num_comments": 10,
                            "created_at_i": 1_761_700_000,
                        },
                        {
                            "objectID": "2",
                            "title": "Ask HN: no url",
                            "url": None,
                            "author": "b",
                            "points": 80,
                            "num_comments": 3,
                            "story_text": "body",
                            "created_at_i": 1_761_700_000,
                        },
                        {"objectID": "3", "title": None},  # skipped
                    ]
                },
            )

        provider = HackerNewsProvider(min_points=75, limit=20, http_client=mock_client(handler))
        items = provider.collect()
        assert [i.external_id for i in items] == ["1", "2"]
        assert items[1].url == "https://news.ycombinator.com/item?id=2"
        assert items[1].body == "body"
        assert items[0].extra["discussion_url"].endswith("id=1")
        assert "points>=75" in captured["params"]["numericFilters"]
        assert captured["params"]["hitsPerPage"] == "20"

    def test_http_error_raises_provider_error(self):
        provider = HackerNewsProvider(http_client=mock_client(lambda r: httpx.Response(503)))
        with pytest.raises(ProviderError):
            provider.collect()


# ---------------------------------------------------------------- reddit
def _reddit_payload(sub="programming"):
    return {
        "data": {
            "children": [
                {
                    "data": {
                        "id": "abc",
                        "title": "Great post",
                        "permalink": f"/r/{sub}/comments/abc/x/",
                        "url": "https://example.com/article",
                        "is_self": False,
                        "author": "u1",
                        "score": 100,
                        "num_comments": 5,
                        "created_utc": 1_761_700_000.0,
                        "subreddit": sub,
                        "link_flair_text": "News",
                    }
                },
                {
                    "data": {
                        "id": "def",
                        "title": "Self post",
                        "permalink": f"/r/{sub}/comments/def/y/",
                        "url": f"https://www.reddit.com/r/{sub}/comments/def/y/",
                        "is_self": True,
                        "selftext": "body text",
                        "author": "u2",
                        "score": 10,
                        "num_comments": 1,
                        "created_utc": 1_761_700_000.0,
                        "subreddit": sub,
                    }
                },
                {"data": {"id": "sticky", "title": "Pinned", "stickied": True, "permalink": "/x"}},
            ]
        }
    }


class TestReddit:
    def test_anonymous_listing(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_reddit_payload())

        provider = RedditProvider(["r/programming"], http_client=mock_client(handler))
        items = provider.collect()
        assert seen[0].url.host == "www.reddit.com"
        assert seen[0].url.path == "/r/programming/top.json"
        assert "Authorization" not in seen[0].headers
        assert [i.external_id for i in items] == ["abc", "def"]
        assert items[0].url == "https://example.com/article"
        assert items[1].url.endswith("/comments/def/y")
        assert items[1].body == "body text"
        assert items[0].source_name == "r/programming"

    def test_oauth_token_uses_oauth_host(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_reddit_payload())

        provider = RedditProvider(
            ["programming"], access_token="tok", http_client=mock_client(handler)
        )
        provider.collect()
        assert seen[0].url.host == "oauth.reddit.com"
        assert seen[0].headers["Authorization"] == "Bearer tok"

    def test_client_credentials_flow(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/access_token"):
                assert request.headers["Authorization"].startswith("Basic ")
                return httpx.Response(200, json={"access_token": "fresh"})
            return httpx.Response(200, json=_reddit_payload())

        provider = RedditProvider(
            ["programming"],
            client_id="id",
            client_secret="secret",
            http_client=mock_client(handler),
        )
        provider.collect()
        assert seen[1].headers["Authorization"] == "Bearer fresh"

    def test_forbidden_raises_provider_error(self):
        provider = RedditProvider(
            ["programming"], http_client=mock_client(lambda r: httpx.Response(403))
        )
        with pytest.raises(ProviderError, match="REDDIT_ACCESS_TOKEN"):
            provider.collect()

    def test_partial_failure_keeps_successful_subreddits(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/r/bad/" in request.url.path:
                return httpx.Response(429)
            return httpx.Response(200, json=_reddit_payload("good"))

        provider = RedditProvider(["good", "bad"], http_client=mock_client(handler))
        items = provider.collect()
        assert len(items) == 2


# ---------------------------------------------------------------- rss
class TestRSS:
    def test_parses_feed(self):
        xml = (FIXTURES / "sample_feed.xml").read_bytes()
        provider = RSSProvider(
            ["https://example.com/feed.xml"],
            http_client=mock_client(lambda r: httpx.Response(200, content=xml)),
        )
        items = provider.collect()
        assert len(items) == 2
        first = items[0]
        assert first.title == "A practical primer on distributed tracing"
        assert first.url == "https://example.com/blog/tracing"  # utm stripped
        assert first.external_id == "https://example.com/blog/tracing"
        assert first.body == "Sampling strategies & context propagation."
        assert first.source_name == "Example Engineering Blog"
        assert first.published_at == datetime(2026, 10, 29, 8, 0, tzinfo=UTC)
        assert first.extra["tags"] == ["observability"]

    def test_bad_feed_raises(self):
        provider = RSSProvider(
            ["https://example.com/feed.xml"],
            http_client=mock_client(lambda r: httpx.Response(200, content=b"garbage")),
        )
        with pytest.raises(ProviderError):
            provider.collect()

    def test_strip_html(self):
        assert strip_html("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"
        assert strip_html(None) is None


# ---------------------------------------------------------------- x
class TestX:
    def test_api_v2_strategy(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer token"
            path = request.url.path
            if path.endswith("/users/by/username/someone"):
                return httpx.Response(200, json={"data": {"id": "42", "username": "someone"}})
            if path.endswith("/users/42/tweets"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "900",
                                "text": "A long tweet about postgres",
                                "author_id": "42",
                                "created_at": "2026-10-29T10:00:00.000Z",
                                "public_metrics": {
                                    "like_count": 5,
                                    "reply_count": 1,
                                    "retweet_count": 2,
                                },
                            }
                        ],
                        "includes": {"users": [{"id": "42", "username": "someone"}]},
                    },
                )
            if path.endswith("/tweets/search/recent"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "901",
                                "text": "search hit",
                                "author_id": "7",
                                "public_metrics": {},
                            }
                        ],
                        "includes": {"users": [{"id": "7", "username": "other"}]},
                    },
                )
            return httpx.Response(404)

        provider = XProvider(
            ["@someone"], ["postgres"], bearer_token="token", http_client=mock_client(handler)
        )
        items = provider.collect()
        assert {i.external_id for i in items} == {"900", "901"}
        by_id = {i.external_id: i for i in items}
        assert by_id["900"].url == "https://x.com/someone/status/900"
        assert by_id["900"].author == "@someone"
        assert by_id["900"].score == 5
        assert by_id["901"].author == "@other"

    def test_nitter_strategy_rewrites_provider(self):
        xml = (FIXTURES / "sample_feed.xml").read_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://nitter.test/someone/rss"
            return httpx.Response(200, content=xml)

        provider = XProvider(
            ["someone"], nitter_base_url="https://nitter.test/", http_client=mock_client(handler)
        )
        items = provider.collect()
        assert len(items) == 2
        assert all(i.provider == "x" and i.extra["strategy"] == "nitter" for i in items)

    def test_syndication_strategy_with_cookie(self):
        payload = {
            "props": {
                "pageProps": {
                    "timeline": {
                        "entries": [
                            {
                                "content": {
                                    "tweet": {
                                        "id_str": "77",
                                        "full_text": "hello from syndication",
                                        "favorite_count": 3,
                                        "reply_count": 0,
                                        "retweet_count": 1,
                                        "created_at": "Wed Oct 29 10:00:00 +0000 2026",
                                    }
                                }
                            },
                            {"content": {"tweet": {"id_str": None, "full_text": "skipped"}}},
                        ]
                    }
                }
            }
        }
        html = (
            '<html><script id="__NEXT_DATA__" type="application/json">'
            f"{json.dumps(payload)}</script></html>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Cookie"] == "auth_token=abc; ct0=def"
            return httpx.Response(200, text=html)

        provider = XProvider(
            ["someone"], auth_cookie="auth_token=abc; ct0=def", http_client=mock_client(handler)
        )
        items = provider.collect()
        assert [i.external_id for i in items] == ["77"]
        assert items[0].extra["strategy"] == "syndication"

    def test_all_strategies_failing_raises(self):
        provider = XProvider(
            ["someone"], bearer_token="bad", http_client=mock_client(lambda r: httpx.Response(401))
        )
        with pytest.raises(ProviderError, match="All X strategies failed"):
            provider.collect()

    def test_nothing_configured_returns_empty(self):
        assert XProvider([], []).collect() == []


# ---------------------------------------------------------------- registry
class TestRegistry:
    def test_build_providers_from_settings(self, settings):
        providers = build_providers(settings)
        names = [p.name for p in providers]
        assert names == ["hackernews", "reddit", "rss", "x", "instagram", "facebook"]

    def test_build_mock_providers(self, settings):
        providers = build_providers(settings, mock=True)
        assert all(isinstance(p, MockProvider) for p in providers)
        assert [p.name for p in providers] == [
            "hackernews",
            "reddit",
            "rss",
            "x",
            "instagram",
            "facebook",
        ]

    def test_unknown_provider_rejected(self, settings):
        from dataclasses import replace

        with pytest.raises(ValueError, match="Unknown provider"):
            build_providers(replace(settings, enabled_providers=("myspace",)))
