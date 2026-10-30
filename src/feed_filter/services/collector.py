"""Collection service: run every provider, tolerate failures, dedupe and persist."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from feed_filter.db.repository import Repository
from feed_filter.providers import Provider, ProviderError, mock_fallback_for

log = logging.getLogger(__name__)


@dataclass
class ProviderRun:
    provider: str
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    error: str | None = None
    used_fallback: bool = False


@dataclass
class CollectionResult:
    runs: list[ProviderRun] = field(default_factory=list)

    @property
    def inserted(self) -> int:
        return sum(r.inserted for r in self.runs)

    @property
    def duplicates(self) -> int:
        return sum(r.duplicates for r in self.runs)

    @property
    def failures(self) -> list[ProviderRun]:
        return [r for r in self.runs if r.error]

    def as_dict(self) -> dict:
        return {
            "collected": self.inserted,
            "duplicates": self.duplicates,
            "providers": {
                r.provider: {
                    "fetched": r.fetched,
                    "inserted": r.inserted,
                    "duplicates": r.duplicates,
                    "error": r.error,
                    "used_fallback": r.used_fallback,
                }
                for r in self.runs
            },
        }


class Collector:
    def __init__(
        self, repo: Repository, providers: list[Provider], *, allow_mock_fallback: bool = False
    ):
        self.repo = repo
        self.providers = providers
        self.allow_mock_fallback = allow_mock_fallback

    def run(self) -> CollectionResult:
        result = CollectionResult()
        for provider in self.providers:
            run = ProviderRun(provider=provider.name)
            try:
                items = provider.collect()
            except ProviderError as exc:
                log.error("collect[%s] failed: %s", provider.name, exc)
                run.error = str(exc)
                items = []
                if self.allow_mock_fallback:
                    fallback = mock_fallback_for(provider.name)
                    if fallback is not None:
                        log.warning("collect[%s]: using mock fixture fallback", provider.name)
                        try:
                            items = fallback.collect()
                            run.used_fallback = True
                        except ProviderError as fallback_exc:
                            log.error(
                                "collect[%s] fallback failed: %s", provider.name, fallback_exc
                            )
            except Exception as exc:  # a bug in one provider must not stop the others
                log.exception("collect[%s] crashed", provider.name)
                run.error = f"{type(exc).__name__}: {exc}"
                items = []

            run.fetched = len(items)
            if items:
                ingest = self.repo.ingest(items)
                run.inserted, run.duplicates = ingest.inserted, ingest.duplicates
                self.repo.session.commit()
            log.info(
                "collect[%s]: fetched=%d inserted=%d duplicates=%d%s",
                provider.name,
                run.fetched,
                run.inserted,
                run.duplicates,
                " (fallback)" if run.used_fallback else "",
            )
            result.runs.append(run)
        return result
