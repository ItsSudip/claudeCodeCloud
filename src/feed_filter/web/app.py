"""Local dashboard for browsing past Daily Digests.

Run with ``python main.py --serve`` and open http://127.0.0.1:8000. The dashboard never
triggers collection or LLM calls; the only write it does is building a mood session from items
that are already scored, so it is safe to leave running.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import markdown as md
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from feed_filter import __version__
from feed_filter.config import Settings, load_settings
from feed_filter.db import Repository, create_engine_for, create_session_factory, init_db
from feed_filter.db.models import Report
from feed_filter.services.profile import load_profile
from feed_filter.services.sessions import (
    MAX_SESSION_MINUTES,
    MIN_SESSION_MINUTES,
    SessionBuilder,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class SessionRequest(BaseModel):
    mood: str = "any"
    minutes: int = Field(10, ge=1, le=240)
    allow_repeats: bool = False


def render_markdown(text: str) -> str:
    return md.markdown(text, extensions=["extra", "sane_lists"])


def create_app(
    settings: Settings | None = None, session_factory: sessionmaker[Session] | None = None
) -> FastAPI:
    settings = settings or load_settings()
    if session_factory is None:
        engine = create_engine_for(settings.database_url)
        init_db(engine)
        session_factory = create_session_factory(engine)

    app = FastAPI(title="The Feed Filter", version=__version__, docs_url="/api/docs")
    app.state.settings = settings
    app.state.session_factory = session_factory

    def get_session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    def _profile():
        profile = load_profile(settings.profile_path)
        with session_factory() as session:
            profile_row = Repository(session).upsert_profile(profile.raw_markdown)
            session.commit()
            return profile, profile_row.id

    def _mood_options() -> list[dict]:
        profile, _ = _profile()
        return [
            {"name": name, "description": profile.moods.get(name).description}
            for name in profile.moods.names()
        ]

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, session: Session = Depends(get_session)):
        repo = Repository(session)
        reports = repo.list_reports(limit=100)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "reports": reports,
                "stats": repo.stats(),
                "version": __version__,
                "moods": _mood_options(),
                "min_minutes": MIN_SESSION_MINUTES,
                "max_minutes": MAX_SESSION_MINUTES,
            },
        )

    @app.post("/sessions")
    def create_session_form(
        mood: str = Form("any"),
        minutes: int = Form(10),
        allow_repeats: bool = Form(False),
        session: Session = Depends(get_session),
    ):
        report = _build_session(session, mood, minutes, allow_repeats)
        return RedirectResponse(url=f"/reports/{report.id}", status_code=303)

    @app.post("/api/sessions")
    def create_session_api(body: SessionRequest, session: Session = Depends(get_session)):
        report = _build_session(session, body.mood, body.minutes, body.allow_repeats)
        return JSONResponse(
            {**_report_summary(report), "markdown": report.markdown}, status_code=201
        )

    @app.get("/api/moods")
    def api_moods() -> list[dict]:
        return _mood_options()

    def _build_session(session: Session, mood: str, minutes: int, allow_repeats: bool) -> Report:
        profile, profile_id = _profile()
        builder = SessionBuilder(Repository(session), profile, profile_id=profile_id)
        try:
            return builder.build(mood, minutes, allow_repeats=allow_repeats)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc.args[0])) from exc

    @app.get("/reports/{report_id}", response_class=HTMLResponse)
    def report_page(report_id: int, request: Request, session: Session = Depends(get_session)):
        repo = Repository(session)
        report = repo.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        reports = repo.list_reports(limit=100)
        return templates.TemplateResponse(
            request,
            "report.html",
            {
                "report": report,
                "reports": reports,
                "body_html": render_markdown(report.markdown),
                "version": __version__,
            },
        )

    @app.get("/reports/{report_id}/raw", response_class=PlainTextResponse)
    def report_raw(report_id: int, session: Session = Depends(get_session)):
        report = Repository(session).get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        return report.markdown

    @app.get("/api/reports")
    def api_reports(
        limit: int = 50, kind: str | None = None, session: Session = Depends(get_session)
    ) -> JSONResponse:
        repo = Repository(session)
        return JSONResponse([_report_summary(r) for r in repo.list_reports(limit=limit, kind=kind)])

    @app.get("/api/reports/latest")
    def api_latest(session: Session = Depends(get_session)) -> JSONResponse:
        report = Repository(session).latest_report()
        if report is None:
            raise HTTPException(status_code=404, detail="No reports yet")
        return JSONResponse({**_report_summary(report), "markdown": report.markdown})

    @app.get("/api/reports/{report_id}")
    def api_report(report_id: int, session: Session = Depends(get_session)) -> JSONResponse:
        report = Repository(session).get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        return JSONResponse({**_report_summary(report), "markdown": report.markdown})

    @app.get("/api/stats")
    def api_stats(session: Session = Depends(get_session)) -> dict:
        return Repository(session).stats()

    return app


def _report_summary(report: Report) -> dict:
    return {
        "id": report.id,
        "report_date": report.report_date,
        "title": report.title,
        "item_count": report.item_count,
        "kind": report.kind,
        "mood": report.mood,
        "target_minutes": report.target_minutes,
        "total_minutes": report.total_minutes,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "stats": report.stats,
    }


def export_static_html(session_factory: sessionmaker[Session], output_dir: Path) -> list[Path]:
    """Write a standalone HTML page per report plus an index, for hosting without a server."""
    env = templates.env
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with session_factory() as session:
        repo = Repository(session)
        reports = repo.list_reports(limit=1000)
        index_html = env.get_template("static_index.html").render(
            reports=reports, stats=repo.stats(), version=__version__
        )
        index_path = output_dir / "index.html"
        index_path.write_text(index_html, encoding="utf-8")
        written.append(index_path)
        for report in reports:
            page = env.get_template("static_report.html").render(
                report=report, body_html=render_markdown(report.markdown), version=__version__
            )
            path = output_dir / f"{report.report_date}-{report.id}.html"
            path.write_text(page, encoding="utf-8")
            written.append(path)
    return written


app = None  # populated lazily by `uvicorn feed_filter.web.app:get_app --factory`


def get_app() -> FastAPI:
    return create_app()
