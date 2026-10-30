"""Provider contract shared by every collector.

A provider turns some remote source (API, RSS feed, scraped page, or a local fixture file)
into a list of :class:`CollectedItem`. Providers are deliberately small and synchronous:
the collector service runs them, handles errors and persists the results.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "igshid",
    "ref",
    "ref_src",
    "s",
    "t",
    "si",
}


def normalize_url(url: str | None) -> str | None:
    """Strip tracking params, fragments and trailing slashes so equivalent links hash equal."""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "").strip().lower()


@dataclass
class CollectedItem:
    """A single piece of content from any provider, before it is stored."""

    provider: str
    external_id: str
    title: str
    url: str | None = None
    author: str | None = None
    body: str | None = None
    source_name: str | None = None
    score: int | None = None
    comments: int | None = None
    published_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = re.sub(r"\s+", " ", (self.title or "").strip()) or "(untitled)"
        self.external_id = str(self.external_id)
        self.url = normalize_url(self.url)
        if self.published_at is not None and self.published_at.tzinfo is None:
            self.published_at = self.published_at.replace(tzinfo=UTC)
        if self.body:
            self.body = self.body.strip()[:8000]

    @property
    def content_hash(self) -> str:
        """Provider-independent identity: the canonical URL if there is one, else the title."""
        basis = self.url or f"{self.provider}:{normalize_title(self.title)}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class ProviderError(RuntimeError):
    """Raised by providers for recoverable errors (auth, rate limit, parse failures)."""


class Provider(ABC):
    """Base class for all providers."""

    name: str = "base"

    @abstractmethod
    def collect(self) -> list[CollectedItem]:
        """Fetch and return items. Must raise :class:`ProviderError` on failure."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.__class__.__name__} name={self.name!r}>"


def parse_timestamp(value: Any) -> datetime | None:
    """Best-effort conversion of epoch seconds / ISO strings / time tuples to aware datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime

                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if isinstance(value, tuple | list) and len(value) >= 6:  # feedparser struct_time
        try:
            return datetime(*value[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            return None
    return None
