#!/usr/bin/env python3
"""Entry point: ``python main.py --digest`` (see ``python main.py --help``)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from feed_filter.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
