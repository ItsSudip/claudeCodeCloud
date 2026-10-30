#!/usr/bin/env bash
# Daily digest runner for cron / launchd / systemd timers.
# Example crontab (07:30 every day):  30 7 * * * /path/to/feed-filter/scripts/run_daily.sh >> /path/to/feed-filter/data/cron.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
python main.py --digest "$@"
