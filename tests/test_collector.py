from __future__ import annotations

from feed_filter.providers.base import Provider, ProviderError
from feed_filter.services.collector import Collector
from tests.conftest import make_item


class _Good(Provider):
    name = "good"

    def collect(self):
        return [
            make_item(provider="good", external_id="1", url="https://g.example/1"),
            make_item(provider="good", external_id="2", url="https://g.example/2"),
        ]


class _Failing(Provider):
    name = "reddit"  # has a bundled fixture

    def collect(self):
        raise ProviderError("HTTP 403")


class _NoFixture(Provider):
    name = "nofixture"

    def collect(self):
        raise ProviderError("down")


class _Crashing(Provider):
    name = "crash"

    def collect(self):
        raise KeyError("bug")


class TestCollector:
    def test_collects_and_dedupes_across_runs(self, repo):
        collector = Collector(repo, [_Good()])
        first = collector.run()
        second = collector.run()
        assert first.inserted == 2 and first.duplicates == 0
        assert second.inserted == 0 and second.duplicates == 2
        assert repo.count_raw_items() == 2

    def test_failure_uses_mock_fallback_when_allowed(self, repo):
        result = Collector(repo, [_Failing()], allow_mock_fallback=True).run()
        run = result.runs[0]
        assert run.error == "HTTP 403"
        assert run.used_fallback is True
        assert run.inserted > 0
        assert result.as_dict()["providers"]["reddit"]["used_fallback"] is True

    def test_failure_without_fallback_records_error(self, repo):
        result = Collector(repo, [_Failing(), _NoFixture()], allow_mock_fallback=False).run()
        assert [r.error for r in result.runs] == ["HTTP 403", "down"]
        assert result.inserted == 0
        assert len(result.failures) == 2

    def test_no_fixture_available(self, repo):
        result = Collector(repo, [_NoFixture()], allow_mock_fallback=True).run()
        assert result.runs[0].used_fallback is False
        assert result.runs[0].inserted == 0

    def test_crash_in_one_provider_does_not_stop_others(self, repo):
        result = Collector(repo, [_Crashing(), _Good()]).run()
        assert result.runs[0].error.startswith("KeyError")
        assert result.runs[1].inserted == 2
