"""Local dashboard for browsing past Daily Digests.

Run with ``python main.py --serve`` and open http://127.0.0.1:8000. Everything is read-only;
the dashboard never triggers the pipeline, so it is safe to leave running.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import markdown as md
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from feed_filter import __version__
from feed_filter.config import Settings, load_settings
from feed_filter.db import Repository, create_engine_for, create_session_factory, init_db
from feed_filter.db.models import Report

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, session: Session = Depends(get_session)):
        repo = Repository(session)
        reports = repo.list_reports(limit=100)
        return templates.TemplateResponse(
            request,
            "index.html",
            {"reports": reports, "stats": repo.stats(), "version": __version__},
        )

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
    def api_reports(limit: int = 50, session: Session = Depends(get_session)) -> JSONResponse:
        repo = Repository(session)
        return JSONResponse([_report_summary(r) for r in repo.list_reports(limit=limit)])

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
