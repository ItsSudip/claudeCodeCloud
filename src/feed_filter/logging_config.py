"""Logging setup shared by the CLI and the dashboard."""

from __future__ import annotations

import logging
import os


def configure_logging(verbose: bool = False) -> None:
    level_name = os.getenv("FEED_FILTER_LOG_LEVEL", "DEBUG" if verbose else "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party chatter is rarely useful at INFO.
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
