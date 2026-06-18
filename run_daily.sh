#!/usr/bin/env bash
# Host helper for the daily pipeline (use this from a host crontab instead of
# Docker if you prefer). It cds to the repo and runs the orchestrator with the
# repo's python, logging to posokanei_data/logs/cron.log.
#
# Example crontab line (run `crontab -e` and add):
#   0 6 * * * /home/punchy/Projects/posokanei/run_daily.sh
set -e
cd "$(dirname "$0")"
mkdir -p posokanei_data/logs
exec python3 orchestrate.py "$@" >> posokanei_data/logs/cron.log 2>&1
