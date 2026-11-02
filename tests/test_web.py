from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from feed_filter.services.llm import MockLLMClient
from feed_filter.services.pipeline import Pipeline
from feed_filter.web.app import create_app, export_static_html


@pytest.fixture
def client(settings, session_factory):
    with session_factory() as session:
        Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True).run(
            report_date=date(2026, 10, 30)
        )
    return TestClient(create_app(settings, session_factory))


class TestDashboard:
    def test_health(self, client):
        assert client.get("/health").json()["status"] == "ok"

    def test_index_lists_reports(self, client):
        html = client.get("/").text
        assert "2026-10-30" in html
        assert "items ingested" in html

    def test_report_page_renders_markdown(self, client):
        html = client.get("/reports/1").text
        assert "<h1>Daily Digest - 2026-10-30</h1>" in html
        assert "<h2>Tech Breakthroughs</h2>" in html or "<h2>Business Insights</h2>" in html
        assert 'href="/reports/1/raw"' in html

    def test_raw_markdown(self, client):
        response = client.get("/reports/1/raw")
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text.startswith("# Daily Digest")

    def test_api(self, client):
        reports = client.get("/api/reports").json()
        assert reports[0]["id"] == 1 and reports[0]["item_count"] > 0
        latest = client.get("/api/reports/latest").json()
        assert latest["markdown"].startswith("# Daily Digest")
        assert client.get("/api/reports/1").json()["report_date"] == "2026-10-30"
        assert client.get("/api/stats").json()["reports"] == 1

    def test_404s(self, client):
        assert client.get("/reports/99").status_code == 404
        assert client.get("/api/reports/99").status_code == 404
        assert client.get("/reports/99/raw").status_code == 404

    def test_empty_dashboard(self, settings, session_factory):
        empty = TestClient(create_app(settings, session_factory))
        assert "No digests yet" in empty.get("/").text
        assert empty.get("/api/reports/latest").status_code == 404


class TestSessions:
    def test_index_has_mood_form(self, client):
        html = client.get("/").text
        assert 'action="/sessions"' in html
        assert '<option value="happy"' in html

    def test_form_builds_session_and_redirects(self, client):
        response = client.post(
            "/sessions", data={"mood": "happy", "minutes": "10"}, follow_redirects=False
        )
        assert response.status_code == 303
        location = response.headers["location"]
        page = client.get(location).text
        assert "session · happy" in page
        assert "<h1>10-minute session · happy</h1>" in page

    def test_api_builds_session(self, client):
        response = client.post("/api/sessions", json={"mood": "curious", "minutes": 8})
        assert response.status_code == 201
        body = response.json()
        assert body["kind"] == "session" and body["mood"] == "curious"
        assert body["target_minutes"] == 8
        assert body["markdown"].startswith("# 8-minute session · curious")
        listed = client.get("/api/reports", params={"kind": "session"}).json()
        assert [r["id"] for r in listed] == [body["id"]]
        assert client.get("/api/moods").json()[0]["name"] == "any"

    def test_unknown_mood_is_400(self, client):
        assert client.post("/api/sessions", json={"mood": "furious"}).status_code == 400
        assert client.post("/api/sessions", json={"mood": "any", "minutes": 0}).status_code == 422


class TestStaticExport:
    def test_export(self, settings, session_factory, tmp_path):
        with session_factory() as session:
            Pipeline(settings, session, llm=MockLLMClient(), mock_providers=True).run(
                report_date=date(2026, 10, 30)
            )
        out = tmp_path / "html"
        paths = export_static_html(session_factory, out)
        assert (out / "index.html").exists()
        assert len(paths) == 2
        page = (out / "2026-10-30-1.html").read_text()
        assert "<h1>Daily Digest - 2026-10-30</h1>" in page
        assert 'href="index.html"' in page
        assert "/api/docs" not in page
