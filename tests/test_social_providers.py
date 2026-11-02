from __future__ import annotations

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
