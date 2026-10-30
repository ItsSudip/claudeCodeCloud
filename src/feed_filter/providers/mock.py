"""Fixture-backed provider.

Used (a) in tests, (b) as a fallback when a real provider cannot authenticate, and (c) to try
the whole pipeline end-to-end without any network access (``--mock``).

Fixture files are JSON lists of objects with the same fields as :class:`CollectedItem`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from feed_filter.providers.base import CollectedItem, Provider, ProviderError, parse_timestamp

log = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class MockProvider(Provider):
    name = "mock"

    def __init__(self, fixture: str | Path, *, provider_name: str | None = None):
        self.fixture_path = Path(fixture)
        if not self.fixture_path.is_absolute() and not self.fixture_path.exists():
            self.fixture_path = FIXTURES_DIR / self.fixture_path
        self.name = provider_name or self.fixture_path.stem

    def collect(self) -> list[CollectedItem]:
        if not self.fixture_path.exists():
            raise ProviderError(f"Mock fixture not found: {self.fixture_path}")
        try:
            rows = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Invalid JSON in {self.fixture_path}: {exc}") from exc
        items: list[CollectedItem] = []
        for row in rows:
            row = dict(row)
            row.setdefault("provider", self.name)
            row["published_at"] = parse_timestamp(row.get("published_at"))
            items.append(CollectedItem(**row))
        log.info("mock[%s]: loaded %d items from %s", self.name, len(items), self.fixture_path.name)
        return items
