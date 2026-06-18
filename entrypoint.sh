#!/usr/bin/env bash
# Container entrypoint: start cron (for the daily pipeline) + the web UI.
set -e

mkdir -p /app/posokanei_data/logs

# Start the cron daemon. The root crontab (installed at build time from
# ./crontab) runs `orchestrate.py` once a day.
cron
echo "[entrypoint] cron started — daily pipeline scheduled (see /app/crontab)."

# First boot with an empty volume: kick one pipeline run in the background so
# the dashboard has data without waiting for tomorrow's cron tick. The web
# server still comes up immediately.
if [ ! -f /app/posokanei_data/products.json ]; then
  echo "[entrypoint] no catalogue yet — running initial pipeline in background..."
  ( cd /app && python orchestrate.py >> /app/posokanei_data/logs/initial.log 2>&1 ) &
else
  echo "[entrypoint] existing data found — skipping initial run."
fi

echo "[entrypoint] starting web UI on :${PORT:-8000}"
exec python webapp.py
