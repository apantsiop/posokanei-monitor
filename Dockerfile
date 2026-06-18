# PosoKanei Monitor — daily price-comparison pipeline + web UI.
# Zero Python dependencies, so this stays small (python:slim, no pip install).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Athens \
    PORT=8000

WORKDIR /app

# cron drives the daily job; tzdata makes the 06:00 schedule local; curl is for
# the container HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

# Application code (stdlib only — nothing to pip install).
COPY posokanei_scrape.py posokanei_snapshot.py posokanei_cartel_screen.py \
     posokanei_basket.py posokanei_timeseries.py \
     orchestrate.py webapp.py entrypoint.sh crontab ./

RUN chmod +x entrypoint.sh \
    && crontab crontab

# Data (catalogue, snapshots, reports, SQLite DB) lives on a volume so it
# survives container restarts.
VOLUME ["/app/posokanei_data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

ENTRYPOINT ["./entrypoint.sh"]
