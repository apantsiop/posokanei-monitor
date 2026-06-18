# PosoKanei Monitor

[![Build and publish Docker image](https://github.com/apantsiop/posokanei-monitor/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/apantsiop/posokanei-monitor/actions/workflows/docker-publish.yml)

A self-contained daily pipeline + web dashboard that tracks Greek supermarket
prices from the public open-data service [posokanei.gov.gr](https://posokanei.gov.gr).

Once a day it **scrapes** the full catalogue, takes a price **snapshot**, and runs
three analyses — a **cheapest-supermarket basket**, a **collusion screen**, and a
**parallel-price-movement screen** — storing key metrics in a lightweight
**SQLite** DB and serving everything from a **web dashboard** with day-over-day
trends.

Everything is **pure Python standard library** — no pip install, no external
services. The only "database" is a single SQLite file.

![Dashboard](https://github.com/apantsiop/posokanei-monitor/assets/placeholder)

> ⚠️ The price analyses are **investigative screens, not verdicts** — identical or
> parallel prices have lawful explanations (MSRP, price-matching, common
> ownership, shared cost shocks). They scope investigation; they don't prove
> anything. Each report includes its own caveats.

## Run it

### From the published image (no build)

```bash
docker run -d --name posokanei -p 8000:8000 \
  -v "$PWD/posokanei_data:/app/posokanei_data" \
  -e TZ=Europe/Athens \
  ghcr.io/apantsiop/posokanei-monitor:latest
# open http://localhost:8000
```

The image is **multi-arch** (linux/amd64 + linux/arm64), so it also runs on ARM
hosts (e.g. an Oracle Cloud Ampere VM or a Raspberry Pi).

### With docker compose (builds locally)

```bash
docker compose up -d --build      # open http://localhost:8000
docker compose logs -f
docker compose exec posokanei python orchestrate.py   # force a run now
docker compose down
```

- First boot (empty data dir) kicks one pipeline run in the background; the UI is
  up immediately and fills in within a couple of minutes.
- `cron` inside the container re-runs the pipeline daily at **06:00** (container
  timezone; default `Europe/Athens`).
- Data persists in `./posokanei_data/` on the host.

### On the host, without Docker

```bash
python3 webapp.py                 # http://localhost:8000  (PORT=9000 to change)
```

Schedule the daily job in your own crontab (`crontab -e`):

```cron
0 6 * * * /path/to/posokanei-monitor/run_daily.sh
```

Trigger manually:

```bash
python3 orchestrate.py            # full live run
python3 orchestrate.py --limit 3  # smoke test (3 product pages)
python3 orchestrate.py --skip-scrape   # rebuild reports from existing data
```

## What the UI shows

| Route | Content |
|-------|---------|
| `/` | KPIs (cheapest chain, basket total, max overpay, % identical) + trend charts |
| `/report/basket` | "Who is cheapest?" infographic (cheapest = 100%) |
| `/report/cartel` | Collusion-screen infographic (identity, dispersion, pairs) |
| `/report/timeseries` | Parallel-movement screen (needs ≥2 daily snapshots) |
| `/runs` | Full run history; each row links to that day's archived reports |
| `/api/trends`, `/api/runs`, `/healthz` | JSON / liveness |

## How it fits together

```
posokanei_scrape.py        harvest catalogue + prices       ┐
posokanei_snapshot.py      one compact daily snapshot       │ called by
posokanei_cartel_screen.py collusion-screen HTML report     │ orchestrate.py
posokanei_basket.py        cheapest-supermarket HTML report │ (the cron job)
posokanei_timeseries.py    parallel-movement screen         ┘
orchestrate.py             the daily job (cron → runs all the above + writes DB)
webapp.py                  the web UI (dashboard, reports, trends, history)
```

### Data layout (`posokanei_data/`, gitignored)

```
products.json, prices.csv, ...   latest scrape
snapshots/<date>.csv             accumulating daily snapshots (time-series input)
analysis/                        latest collusion report + screen_summary.json
basket/                          latest basket report + basket_summary.json
analysis_ts/                     latest movement screen + ts_summary.json
runs/<date>/                     archived per-run HTML reports + summaries
logs/                            orchestrate + cron logs
posokanei.db                     SQLite: runs + basket_history (the trend data)
```

## Publishing

Pushing to `main` triggers `.github/workflows/docker-publish.yml`, which builds
the multi-arch image and pushes it to `ghcr.io/apantsiop/posokanei-monitor`
(`latest` + a short-SHA tag; version tags `vX.Y.Z` are tagged too).

## Notes

- The basket compares the **top-8 retailers by catalogue size** (the major Greek
  chains, ~1,000 shared products); tune via `orchestrate.py --top`.
- Data is the Greek government's public price-comparison API; the scraper
  identifies itself honestly and rate-limits politely.
