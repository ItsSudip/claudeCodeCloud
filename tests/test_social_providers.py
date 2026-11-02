from __future__ import annotations

import json

import httpx
import pytest

from feed_filter.providers.base import ProviderError
from feed_filter.providers.facebook import FacebookProvider
from feed_filter.providers.instagram import InstagramProvider
from feed_filter.providers.mock import MockProvider
from tests.conftest import FIXTURES, mock_client

IG_MEDIA = {
    "data": [
        {
            "id": "17900001",
            "caption": "Rescue puppy meets the ocean 🌊\nmore text",
            "media_type": "VIDEO",
            "media_product_type": "REELS",
            "media_url": "https://cdn.example/4001.mp4",
            "thumbnail_url": "https://cdn.example/4001.jpg",
            "permalink": "https://www.instagram.com/reel/C4001/",
            "timestamp": "2026-10-31T18:00:00+0000",
            "like_count": 182000,
            "comments_count": 2100,
            "username": "dodo",
        },
        {
            "id": "17900002",
            "media_type": "CAROUSEL_ALBUM",
            "permalink": "https://www.instagram.com/p/C4002/",
            "timestamp": "2026-10-31T10:00:00+0000",
            "like_count": 10,
            "comments_count": 0,
        },
        {"id": "17900003"},  # no permalink -> skipped
    ]
}


class TestInstagramGraph:
    def test_business_discovery(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert request.url.params["access_token"] == "tok"
            if request.url.path.endswith("/17841400000/media"):
                return httpx.Response(200, json=IG_MEDIA)
            assert "business_discovery.username(dodo)" in request.url.params["fields"]
            return httpx.Response(200, json={"business_discovery": {"media": IG_MEDIA}})

        provider = InstagramProvider(
            ["@dodo", "me"],
            access_token="tok",
            user_id="17841400000",
            http_client=mock_client(handler),
        )
        items = provider.collect()
        assert len(items) == 4
        reel = items[0]
        assert reel.external_id == "17900001"
        assert reel.title == "Rescue puppy meets the ocean 🌊"
        assert reel.media_type == "short_video"
        assert reel.media_url == "https://cdn.example/4001.mp4"
        assert reel.author == "@dodo"
        assert reel.score == 182000
        assert reel.extra["strategy"] == "graph_api"
        album = items[1]
        assert album.media_type == "image"
        assert album.title.startswith("@dodo image")
        assert seen[1].url.path.endswith("/17841400000/media")

    def test_graph_error_message(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token"}})

        provider = InstagramProvider(
            ["dodo"], access_token="bad", user_id="1", http_client=mock_client(handler)
        )
        with pytest.raises(ProviderError, match="Invalid OAuth"):
            provider.collect()

    def test_token_without_user_id(self):
        provider = InstagramProvider(
            ["dodo"], access_token="tok", http_client=mock_client(lambda r: httpx.Response(500))
        )
        with pytest.raises(ProviderError, match="INSTAGRAM_USER_ID"):
            provider.collect()


class TestInstagramWeb:
    def test_web_profile_scrape(self):
        payload = {
            "data": {
                "user": {
                    "edge_owner_to_timeline_media": {
                        "edges": [
                            {
                                "node": {
                                    "id": "1",
                                    "shortcode": "ABC",
                                    "is_video": True,
                                    "video_duration": 42.5,
                                    "edge_media_to_caption": {
                                        "edges": [{"node": {"text": "Zoomies!"}}]
                                    },
                                    "edge_liked_by": {"count": 5},
                                    "edge_media_to_comment": {"count": 1},
                                    "taken_at_timestamp": 1_761_900_000,
                                    "video_url": "https://cdn/v.mp4",
                                    "display_url": "https://cdn/t.jpg",
                                }
                            },
                            {
                                "node": {
                                    "id": "2",
                                    "shortcode": "DEF",
                                    "is_video": False,
                                    "edge_media_to_caption": {"edges": []},
                                    "taken_at_timestamp": 1_761_900_000,
                                    "display_url": "https://cdn/p.jpg",
                                }
                            },
                            {
                                "node": {
                                    "id": "3",
                                    "shortcode": "LONG",
                                    "is_video": True,
                                    "video_duration": 600,
                                    "edge_media_to_caption": {"edges": []},
                                }
                            },
                        ]
                    }
                }
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-ig-app-id"] == "936619743392459"
            assert request.headers["Cookie"] == "sessionid=abc"
            assert request.headers["User-Agent"].startswith("Mozilla/5.0")
            if "web_profile_info" not in request.url.path:  # the GraphQL attempt; let it fail
                return httpx.Response(
                    404, text="<html class='logged-in'>", headers={"content-type": "text/html"}
                )
            assert request.headers["X-Requested-With"] == "XMLHttpRequest"
            assert request.url.params["username"] == "dodo"
            return httpx.Response(200, json=payload)

        provider = InstagramProvider(
            ["dodo"], session_cookie="sessionid=abc", http_client=mock_client(handler)
        )
        items = provider.collect()
        assert [i.external_id for i in items] == ["1", "2", "3"]
        assert items[0].url == "https://instagram.com/reel/ABC"
        assert items[0].duration_seconds == 42
        assert items[0].media_type == "short_video"
        assert items[0].body == "Zoomies!"
        assert items[1].media_type == "image" and items[1].url.endswith("/p/DEF")
        assert items[2].media_type == "long_video"

    def test_login_wall_raises(self):
        provider = InstagramProvider(
            ["dodo"],
            session_cookie="sessionid=old",
            http_client=mock_client(lambda r: httpx.Response(401)),
        )
        with pytest.raises(ProviderError, match="refresh INSTAGRAM_SESSION_COOKIE"):
            provider.collect()

    def test_no_credentials(self):
        with pytest.raises(ProviderError, match="INSTAGRAM_ACCESS_TOKEN"):
            InstagramProvider(["dodo"]).collect()

    def test_no_accounts(self):
        assert InstagramProvider([]).collect() == []


FB_POSTS = {
    "data": [
        {
            "id": "100_1",
            "message": "Repair café opens!\nDetails below",
            "permalink_url": "https://www.facebook.com/lib/posts/1",
            "created_time": "2026-10-31T15:00:00+0000",
            "full_picture": "https://cdn/1.jpg",
            "attachments": {
                "data": [{"media_type": "photo", "url": "https://www.facebook.com/photo/1"}]
            },
            "shares": {"count": 12},
            "reactions": {"summary": {"total_count": 4300}},
            "comments": {"summary": {"total_count": 210}},
        },
        {
            "id": "100_2",
            "permalink_url": "https://www.facebook.com/lib/posts/2",
            "created_time": "2026-10-31T11:00:00+0000",
            "attachments": {
                "data": [
                    {
                        "media_type": "link",
                        "type": "share",
                        "url": "https://example.com/careers",
                        "title": "We're hiring",
                        "description": "A guide to our week",
                    }
                ]
            },
        },
        {
            "id": "100_3",
            "story": "Lib updated their cover photo.",
            "permalink_url": "https://www.facebook.com/lib/posts/3",
        },
        {"id": "100_4", "message": "no permalink"},
    ]
}


class TestFacebookGraph:
    def test_page_posts(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/citylibrary/posts")
            assert request.url.params["access_token"] == "tok"
            assert "reactions.summary" in request.url.params["fields"]
            return httpx.Response(200, json=FB_POSTS)

        provider = FacebookProvider(
            ["citylibrary/"], access_token="tok", http_client=mock_client(handler)
        )
        items = provider.collect()
        assert [i.external_id for i in items] == ["100_1", "100_2", "100_3"]
        photo, link, story = items
        assert photo.title == "Repair café opens!"
        assert photo.media_type == "image"
        assert photo.score == 4300 and photo.comments == 210
        assert photo.extra["shares"] == 12
        assert photo.url == "https://facebook.com/lib/posts/1"
        assert link.media_type == "article"
        assert link.url == "https://example.com/careers"  # external link becomes the item URL
        assert link.extra["discussion_url"] == "https://www.facebook.com/lib/posts/2"
        assert link.title == "We're hiring" and link.body == "A guide to our week"
        assert story.media_type == "post" and story.title.startswith("Lib updated")

    def test_graph_error(self):
        handler = lambda r: httpx.Response(400, json={"error": {"message": "(#10) permission"}})  # noqa: E731
        provider = FacebookProvider(["page"], access_token="tok", http_client=mock_client(handler))
        with pytest.raises(ProviderError, match="permission"):
            provider.collect()


class TestFacebookRSSBridge:
    def test_rssbridge_feed(self):
        xml = (FIXTURES / "sample_feed.xml").read_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "bridge.test"
            assert request.url.params["bridge"] == "FacebookBridge"
            assert request.url.params["u"] == "citylibrary"
            return httpx.Response(200, content=xml)

        provider = FacebookProvider(
            ["citylibrary"], rssbridge_url="https://bridge.test/", http_client=mock_client(handler)
        )
        items = provider.collect()
        assert len(items) == 2
        assert all(i.provider == "facebook" for i in items)
        assert items[0].source_name.startswith("Facebook · ")
        assert items[0].extra["strategy"] == "rssbridge"
        assert items[0].media_type == "post"

    def test_no_credentials(self):
        with pytest.raises(ProviderError, match="FACEBOOK_ACCESS_TOKEN"):
            FacebookProvider(["page"]).collect()

    def test_all_fail(self):
        provider = FacebookProvider(
            ["page"],
            access_token="t",
            rssbridge_url="https://b.test",
            http_client=mock_client(lambda r: httpx.Response(503)),
        )
        with pytest.raises(ProviderError, match="All Facebook strategies failed"):
            provider.collect()


class TestFixtures:
    @pytest.mark.parametrize("name", ["instagram", "facebook"])
    def test_bundled_fixture(self, name):
        items = MockProvider(f"{name}.json").collect()
        assert len(items) >= 4
        assert all(i.provider == name for i in items)
        assert any(i.media_type for i in items)
        if name == "instagram":
            assert any(i.duration_seconds for i in items)


GRAPHQL_PAGE = {
    "data": {
        "xdt_api__v1__feed__user_timeline_graphql_connection": {
            "edges": [
                {
                    "node": {
                        "code": "De9qfjXILDz",
                        "pk": "4016553328910577907",
                        "media_type": 2,
                        "product_type": "clips",
                        "video_duration": 42.4,
                        "caption": {"text": "Rescue puppy meets the ocean\nmore"},
                        "taken_at": 1793034128,
                        "like_count": 1200,
                        "comment_count": 30,
                        "user": {"username": "natgeo"},
                        "video_versions": [{"url": "https://cdn/v.mp4"}],
                        "image_versions2": {"candidates": [{"url": "https://cdn/t.jpg"}]},
                    }
                },
                {
                    "node": {
                        "code": "AbC",
                        "pk": "2",
                        "media_type": 8,
                        "product_type": "carousel_container",
                        "caption": None,
                        "taken_at": 1793034000,
                        "like_count": 5,
                        "image_versions2": {"candidates": [{"url": "https://cdn/p.jpg"}]},
                    }
                },
                {"node": {"code": None, "pk": "3"}},
            ],
            "page_info": {"has_next_page": True, "end_cursor": "x"},
        }
    }
}


class TestInstagramGraphQL:
    def _handler(self, seen):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "GET" and request.url.path == "/natgeo/":
                html = (
                    '<html class="logged-in"><script>["DTSGInitialData",[],{"token":"DTSG123"}],'
                    '["LSD",[],{"token":"LSD456"}]</script></html>'
                )
                return httpx.Response(200, text=html, headers={"content-type": "text/html"})
            if request.method == "POST" and request.url.path == "/graphql/query":
                return httpx.Response(200, json=GRAPHQL_PAGE)
            return httpx.Response(
                404,
                text="<html class='logged-in'>Page Not Found</html>",
                headers={"content-type": "text/html"},
            )

        return handler

    def test_graphql_request_and_parse(self):
        seen = []
        provider = InstagramProvider(
            ["natgeo"],
            session_cookie="sessionid=s; csrftoken=c; ds_user_id=42",
            graphql_doc_id="777",
            http_client=mock_client(self._handler(seen)),
        )
        items = provider.collect()
        post = next(r for r in seen if r.method == "POST")
        form = dict(httpx.QueryParams(post.content.decode()))
        assert form["doc_id"] == "777"
        assert form["fb_dtsg"] == "DTSG123" and form["lsd"] == "LSD456"
        assert form["av"] == "42"
        assert form["fb_api_req_friendly_name"] == "PolarisProfilePostsQuery"
        variables = json.loads(form["variables"])
        assert variables["username"] == "natgeo" and variables["data"]["count"] == 20
        assert post.headers["x-fb-lsd"] == "LSD456"
        assert post.headers["x-csrftoken"] == "c"
        assert post.headers["x-ig-app-id"] == "936619743392459"
        assert post.headers["Referer"] == "https://www.instagram.com/natgeo/"

        assert [i.external_id for i in items] == ["4016553328910577907", "2"]
        reel, album = items
        assert reel.url == "https://instagram.com/reel/De9qfjXILDz"
        assert reel.media_type == "short_video" and reel.duration_seconds == 42
        assert reel.title == "Rescue puppy meets the ocean"
        assert reel.media_url == "https://cdn/v.mp4"
        assert reel.score == 1200 and reel.comments == 30
        assert reel.extra["strategy"] == "graphql"
        assert album.media_type == "image" and album.url.endswith("/p/AbC")
        assert album.title == "@natgeo image"

    def test_graphql_default_doc_id_and_fallback_to_web_profile(self):
        """A stale doc id gets a non-JSON answer; the provider then tries web_profile_info."""
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.url.path == "/graphql/query":
                return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})
            if "web_profile_info" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "user": {
                                "edge_owner_to_timeline_media": {
                                    "edges": [
                                        {
                                            "node": {
                                                "id": "9",
                                                "shortcode": "Z",
                                                "is_video": False,
                                                "edge_media_to_caption": {"edges": []},
                                            }
                                        }
                                    ]
                                }
                            }
                        }
                    },
                )
            return httpx.Response(
                200, text="<html class='logged-in'></html>", headers={"content-type": "text/html"}
            )

        provider = InstagramProvider(
            ["natgeo"], session_cookie="sessionid=s", http_client=mock_client(handler)
        )
        items = provider.collect()
        assert provider.graphql_doc_id  # built-in default is used when none configured
        assert ("POST", "/graphql/query") in seen
        assert [i.external_id for i in items] == ["9"]

    def test_graphql_login_page_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text='<html class="not-logged-in">login</html>',
                headers={"content-type": "text/html"},
            )

        provider = InstagramProvider(
            ["natgeo"], session_cookie="sessionid=old", http_client=mock_client(handler)
        )
        with pytest.raises(ProviderError, match="login page"):
            provider.collect()
