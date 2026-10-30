from __future__ import annotations

import pytest

from feed_filter.cli import build_parser, main


@pytest.fixture
def cli_env(tmp_path, settings, monkeypatch):
    """Point the CLI at temp paths through environment variables."""
    db_url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("FEED_FILTER_DATABASE_URL", db_url)
    monkeypatch.setenv("FEED_FILTER_PROFILE", str(settings.profile_path))
    monkeypatch.setenv("FEED_FILTER_SOURCES", str(settings.sources_path))
    monkeypatch.setenv("FEED_FILTER_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("FEED_FILTER_LLM_PROVIDER", "mock")
    return tmp_path


class TestCLI:
    def test_help_when_no_action(self, capsys):
        assert main([]) == 1
        assert "--digest" in capsys.readouterr().out

    def test_parser_defaults(self):
        args = build_parser().parse_args(["--digest", "--mock", "--llm", "mock"])
        assert args.digest and args.mock and args.llm == "mock"

    def test_digest_then_list_and_show(self, cli_env, capsys):
        assert main(["--digest", "--mock", "--llm", "mock", "--date", "2026-10-30"]) == 0
        out = capsys.readouterr().out
        assert "Digest #1 for 2026-10-30" in out
        assert (cli_env / "reports" / "2026-10-30.md").exists()

        assert main(["--list-reports"]) == 0
        assert "2026-10-30" in capsys.readouterr().out

        assert main(["--show-report", "1"]) == 0
        assert capsys.readouterr().out.startswith("# Daily Digest - 2026-10-30")

        assert main(["--show-report", "42"]) == 2

    def test_stages_separately(self, cli_env, capsys):
        assert main(["--collect", "--mock"]) == 0
        assert "Collected" in capsys.readouterr().out
        assert main(["--filter", "--llm", "mock"]) == 0
        assert "Evaluated" in capsys.readouterr().out
        assert main(["--summarize", "--date", "2026-10-30"]) == 0
        assert "Digest #1" in capsys.readouterr().out

    def test_export_html(self, cli_env, capsys):
        main(["--digest", "--mock", "--llm", "mock"])
        capsys.readouterr()
        assert main(["--export-html", "--output", str(cli_env / "site")]) == 0
        assert (cli_env / "site" / "index.html").exists()

    def test_init_db(self, cli_env, capsys):
        assert main(["--init-db"]) == 0
        assert "Database ready" in capsys.readouterr().out

    def test_missing_profile(self, cli_env, monkeypatch, capsys):
        monkeypatch.setenv("FEED_FILTER_PROFILE", str(cli_env / "nope.md"))
        assert main(["--digest", "--mock", "--llm", "mock"]) == 2
        assert "not found" in capsys.readouterr().err
