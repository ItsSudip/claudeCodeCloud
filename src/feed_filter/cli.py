"""Command-line interface.

python main.py --digest            # full pipeline: collect -> filter -> summarize
python main.py --collect           # only ingest new items
python main.py --filter            # only evaluate un-scored items with the LLM
python main.py --summarize         # only build a digest from already-scored items
python main.py --session --mood happy --minutes 10   # mood-matched, time-boxed playlist
python main.py --digest --session --mood curious     # refresh, then build a session
python main.py --serve             # start the local dashboard
python main.py --list-reports      # list stored digests
python main.py --show-report 3     # print one digest as markdown
python main.py --export-html       # write standalone HTML pages for every digest
python main.py --digest --mock --llm mock   # offline dry run with fixtures + heuristic scorer
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from feed_filter import __version__
from feed_filter.config import Settings, load_settings
from feed_filter.db import Repository, create_engine_for, create_session_factory, init_db
from feed_filter.logging_config import configure_logging

log = logging.getLogger("feed_filter.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feed-filter",
        description=(
            "The Feed Filter: aggregate, filter and summarize your feeds into a Daily Digest."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    actions = parser.add_argument_group("actions")
    actions.add_argument("--digest", action="store_true", help="run the full pipeline")
    actions.add_argument("--collect", action="store_true", help="collect new items only")
    actions.add_argument("--filter", action="store_true", help="score un-evaluated items only")
    actions.add_argument(
        "--summarize", action="store_true", help="build a digest from scored items"
    )
    actions.add_argument(
        "--session",
        action="store_true",
        help="build a mood-matched, time-boxed session from scored items (see --mood/--minutes)",
    )
    actions.add_argument("--serve", action="store_true", help="start the local dashboard")
    actions.add_argument("--list-reports", action="store_true", help="list stored digests")
    actions.add_argument("--show-report", type=int, metavar="ID", help="print a digest as markdown")
    actions.add_argument("--export-html", action="store_true", help="export digests as static HTML")
    actions.add_argument(
        "--init-db", action="store_true", help="create the database schema and exit"
    )

    options = parser.add_argument_group("options")
    options.add_argument(
        "--mood",
        default="any",
        help="how you feel right now: happy, sad, tired, curious, focused, bored, stressed, any",
    )
    options.add_argument(
        "--minutes",
        type=int,
        default=10,
        help="how much time you have for the session (5-20 recommended, default 10)",
    )
    options.add_argument(
        "--allow-repeats",
        action="store_true",
        help="let a session include items already served in a recent session",
    )
    options.add_argument(
        "--mock", action="store_true", help="use bundled fixture data instead of live providers"
    )
    options.add_argument(
        "--llm",
        choices=["anthropic", "mock"],
        default=None,
        help="LLM backend (default: FEED_FILTER_LLM_PROVIDER or anthropic)",
    )
    options.add_argument("--model", default=None, help="override the Claude model id")
    options.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="report date (YYYY-MM-DD), defaults to today (UTC)",
    )
    options.add_argument("--profile", type=Path, default=None, help="path to interest_profile.md")
    options.add_argument("--db", default=None, help="SQLAlchemy database URL")
    options.add_argument(
        "--output",
        type=Path,
        default=None,
        help="directory for --export-html (default: reports/html)",
    )
    options.add_argument("--host", default=None, help="dashboard host")
    options.add_argument("--port", type=int, default=None, help="dashboard port")
    options.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    options.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    return load_settings(
        llm_provider=args.llm,
        anthropic_model=args.model,
        profile_path=args.profile,
        database_url=args.db,
        dashboard_host=args.host,
        dashboard_port=args.port,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    actions = [
        args.digest,
        args.collect,
        args.filter,
        args.summarize,
        args.serve,
        args.session,
        args.list_reports,
        args.show_report is not None,
        args.export_html,
        args.init_db,
    ]
    if not any(actions):
        parser.print_help()
        return 1

    settings = _settings_from_args(args)
    engine = create_engine_for(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)

    if args.init_db:
        print(f"Database ready at {settings.database_url}")
        return 0

    if args.serve:
        import uvicorn

        from feed_filter.web.app import create_app

        app = create_app(settings, session_factory)
        print(f"Dashboard: http://{settings.dashboard_host}:{settings.dashboard_port}")
        uvicorn.run(
            app, host=settings.dashboard_host, port=settings.dashboard_port, log_level="info"
        )
        return 0

    if args.list_reports:
        with session_factory() as session:
            reports = Repository(session).list_reports()
            if not reports:
                print("No reports yet. Run: python main.py --digest")
            for r in reports:
                created = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "?"
                label = f"session/{r.mood}/{r.target_minutes}m" if r.kind == "session" else "digest"
                print(
                    f"#{r.id:<4} {r.report_date}  {label:<22} {r.item_count:>3} items  "
                    f"generated {created}"
                )
        return 0

    if args.show_report is not None:
        with session_factory() as session:
            report = Repository(session).get_report(args.show_report)
            if report is None:
                print(f"Report #{args.show_report} not found", file=sys.stderr)
                return 2
            print(report.markdown)
        return 0

    if args.export_html:
        from feed_filter.web.app import export_static_html

        out = args.output or (settings.reports_dir / "html")
        paths = export_static_html(session_factory, out)
        print(f"Wrote {len(paths)} file(s) to {out}")
        return 0

    from feed_filter.services.pipeline import Pipeline

    with session_factory() as session:
        try:
            pipeline = Pipeline(settings, session, mock_providers=args.mock)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        if args.digest:
            result = pipeline.run(report_date=args.date)
            report = result.report
            assert report is not None
            print(f"\nDigest #{report.id} for {report.report_date}: {report.item_count} items")
            print(f"Saved to {settings.reports_dir / (report.report_date + '.md')}")
            if result.collection and result.collection.failures:
                for failure in result.collection.failures:
                    note = " (mock fallback used)" if failure.used_fallback else ""
                    print(f"  ! {failure.provider}: {failure.error}{note}")
        elif args.collect:
            result = pipeline.collect()
            print(f"Collected {result.inserted} new items ({result.duplicates} duplicates skipped)")
            for failure in result.failures:
                print(f"  ! {failure.provider}: {failure.error}")
        elif args.filter:
            stats = pipeline.filter()
            print(
                f"Evaluated {stats.evaluated} items; {stats.kept} cleared the threshold "
                f"(clickbait {stats.clickbait}, outrage {stats.outrage}, "
                f"repetitive {stats.repetitive})"
            )
        elif args.summarize:
            report = pipeline.summarize(report_date=args.date)
            print(f"Digest #{report.id} for {report.report_date}: {report.item_count} items")
            print(f"Saved to {settings.reports_dir / (report.report_date + '.md')}")

        if args.session:
            try:
                report = pipeline.build_session(
                    args.mood, args.minutes, allow_repeats=args.allow_repeats
                )
            except KeyError as exc:
                print(str(exc.args[0]), file=sys.stderr)
                return 2
            print()
            print(report.markdown)
            print(
                f"Stored as report #{report.id}; view it with --show-report {report.id} or --serve"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
