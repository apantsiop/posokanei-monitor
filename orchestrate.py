#!/usr/bin/env python3
"""
orchestrate.py — the single daily job (run from cron).

One invocation does the whole pipeline:
  1. scrape       full catalogue + per-retailer prices   (posokanei_scrape.py)
  2. snapshot     compact daily price snapshot            (posokanei_snapshot.py)
  3. cartel       collusion-screen HTML report            (posokanei_cartel_screen.py)
  4. basket       cheapest-supermarket HTML report        (posokanei_basket.py)
  5. timeseries   parallel-movement screen (>=2 days)     (posokanei_timeseries.py)

then it:
  - archives the three HTML reports into posokanei_data/runs/<date>/
  - records the run + key metrics into a lightweight SQLite DB (posokanei.db)
    so the web app can draw day-over-day trends.

A file lock prevents two runs overlapping. Individual report steps are
non-fatal: if the time-series step has too few snapshots it just records a
warning and the run still counts as successful.

Zero third-party dependencies (stdlib only: subprocess, sqlite3, json...).

Usage:
  python3 orchestrate.py                  # full live run
  python3 orchestrate.py --limit 3        # smoke test (3 product pages)
  python3 orchestrate.py --skip-scrape    # rebuild reports from existing data
"""

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "posokanei_data")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
DB_PATH = os.path.join(DATA_DIR, "posokanei.db")
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOCK_PATH = os.path.join(DATA_DIR, ".orchestrate.lock")

PY = sys.executable or "python3"


# --------------------------------------------------------------------------- #
# DB
# --------------------------------------------------------------------------- #
def db_connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS runs (
      run_date        TEXT PRIMARY KEY,
      started_at      TEXT,
      finished_at     TEXT,
      duration_s      REAL,
      status          TEXT,
      catalog_size    INTEGER,
      basket_size     INTEGER,
      n_retailers     INTEGER,
      cheapest        TEXT,
      cheapest_total  REAL,
      priciest        TEXT,
      priciest_total  REAL,
      max_overpay_pct REAL,
      identical_share REAL,
      median_cv       REAL,
      suspicious_pairs INTEGER,
      snapshots_count INTEGER,
      ts_window_days  INTEGER,
      notes           TEXT
    );
    CREATE TABLE IF NOT EXISTS basket_history (
      run_date  TEXT,
      retailer  TEXT,
      total     REAL,
      idx       REAL,
      wins      REAL,
      rank      INTEGER,
      PRIMARY KEY (run_date, retailer)
    );
    """)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg, fh=None):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def run_step(name, cmd, fh):
    log(f"--> {name}: {' '.join(cmd)}", fh)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    dt = time.time() - t0
    if fh:
        fh.write(proc.stdout or "")
        fh.flush()
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-3:])
    if proc.returncode == 0:
        log(f"    ok ({dt:.1f}s)  {tail.splitlines()[-1] if tail else ''}", fh)
    else:
        log(f"    FAILED rc={proc.returncode} ({dt:.1f}s)\n{tail}", fh)
    return proc.returncode


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, just not ours to signal
    except OSError:
        return False


def acquire_lock():
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Steal the lock if its holder is gone (e.g. the container was restarted
        # mid-run, leaving the lock on the persistent volume) or it's very old.
        stale = False
        try:
            pid = int((open(LOCK_PATH).read().strip() or "0"))
        except (OSError, ValueError):
            pid = 0
        if pid and not _pid_alive(pid):
            stale = True
        try:
            if time.time() - os.path.getmtime(LOCK_PATH) > 6 * 3600:
                stale = True
        except OSError:
            pass
        if stale:
            try:
                os.unlink(LOCK_PATH)
            except OSError:
                pass
            return acquire_lock()
        return None
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return LOCK_PATH


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Daily PosoKanei pipeline orchestrator.")
    ap.add_argument("--date", default=None, help="run label (YYYY-MM-DD); default today")
    ap.add_argument("--limit", type=int, default=None, help="cap product pages (smoke test)")
    ap.add_argument("--delay", type=float, default=0.3, help="seconds between API pages")
    ap.add_argument("--top", type=int, default=8, help="retailers in the basket comparison")
    ap.add_argument("--skip-scrape", action="store_true",
                    help="reuse existing products.json instead of fetching")
    args = ap.parse_args()

    date = args.date or datetime.date.today().isoformat()
    for d in (DATA_DIR, RUNS_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)

    lock = acquire_lock()
    if not lock:
        print("Another orchestrate run holds the lock; exiting.", file=sys.stderr)
        return 75  # EX_TEMPFAIL

    log_path = os.path.join(LOG_DIR, f"{date}.log")
    fh = open(log_path, "a", encoding="utf-8")
    started = datetime.datetime.now()
    notes = []
    status = "ok"
    try:
        log(f"=== orchestrate run {date} ===", fh)
        products_json = os.path.join(DATA_DIR, "products.json")

        # 1) scrape
        if args.skip_scrape and os.path.exists(products_json):
            log("skip-scrape: reusing existing products.json", fh)
        else:
            cmd = [PY, "posokanei_scrape.py", "--delay", str(args.delay)]
            if args.limit:
                cmd += ["--limit", str(args.limit)]
            if run_step("scrape", cmd, fh) != 0:
                status = "failed"
                notes.append("scrape failed")
                raise SystemExit("scrape failed — aborting run")

        # 2) snapshot (build from the json we just scraped: no second fetch)
        run_step("snapshot",
                 [PY, "posokanei_snapshot.py", "--date", date,
                  "--from-json", products_json], fh)

        # 3) cartel screen
        if run_step("cartel-screen", [PY, "posokanei_cartel_screen.py"], fh) != 0:
            notes.append("cartel-screen failed")

        # 4) basket
        if run_step("basket", [PY, "posokanei_basket.py", "--top", str(args.top)], fh) != 0:
            notes.append("basket failed")

        # 5) timeseries (non-fatal: needs >=2 snapshots)
        if run_step("timeseries", [PY, "posokanei_timeseries.py"], fh) != 0:
            notes.append("timeseries skipped/insufficient snapshots")

        # ---- archive reports for this run ----
        run_archive = os.path.join(RUNS_DIR, date)
        os.makedirs(run_archive, exist_ok=True)
        import shutil
        archived = []
        for src, dst in [
            (os.path.join(DATA_DIR, "analysis", "report.html"), "cartel.html"),
            (os.path.join(DATA_DIR, "basket", "report.html"), "basket.html"),
            (os.path.join(DATA_DIR, "analysis", "screen_summary.json"), "screen_summary.json"),
            (os.path.join(DATA_DIR, "basket", "basket_summary.json"), "basket_summary.json"),
            (os.path.join(DATA_DIR, "analysis_ts", "ts_summary.json"), "ts_summary.json"),
        ]:
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(run_archive, dst))
                archived.append(dst)
        log(f"archived: {', '.join(archived)}", fh)

        # ---- collect metrics ----
        screen = load_json(os.path.join(DATA_DIR, "analysis", "screen_summary.json"))
        basket = load_json(os.path.join(DATA_DIR, "basket", "basket_summary.json"))
        ts = load_json(os.path.join(DATA_DIR, "analysis_ts", "ts_summary.json"))
        snaps = [f for f in os.listdir(os.path.join(DATA_DIR, "snapshots"))
                 if f.endswith(".csv")] if os.path.isdir(os.path.join(DATA_DIR, "snapshots")) else []

        s1 = screen.get("screen1_identity", {})
        s2 = screen.get("screen2_dispersion", {})
        s3 = screen.get("screen3_pairs", {})
        suspicious = sum(1 for p in s3.get("top_identity_pairs", [])
                         if p.get("identity_rate", 0) >= 0.5)

        finished = datetime.datetime.now()
        conn = db_connect()
        conn.execute("""
          INSERT INTO runs (run_date, started_at, finished_at, duration_s, status,
            catalog_size, basket_size, n_retailers, cheapest, cheapest_total,
            priciest, priciest_total, max_overpay_pct, identical_share, median_cv,
            suspicious_pairs, snapshots_count, ts_window_days, notes)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(run_date) DO UPDATE SET
            started_at=excluded.started_at, finished_at=excluded.finished_at,
            duration_s=excluded.duration_s, status=excluded.status,
            catalog_size=excluded.catalog_size, basket_size=excluded.basket_size,
            n_retailers=excluded.n_retailers, cheapest=excluded.cheapest,
            cheapest_total=excluded.cheapest_total, priciest=excluded.priciest,
            priciest_total=excluded.priciest_total, max_overpay_pct=excluded.max_overpay_pct,
            identical_share=excluded.identical_share, median_cv=excluded.median_cv,
            suspicious_pairs=excluded.suspicious_pairs, snapshots_count=excluded.snapshots_count,
            ts_window_days=excluded.ts_window_days, notes=excluded.notes
        """, (
            date, started.isoformat(timespec="seconds"),
            finished.isoformat(timespec="seconds"),
            (finished - started).total_seconds(), status,
            basket.get("catalog_size"), basket.get("basket_size"),
            basket.get("n_retailers"), basket.get("cheapest"),
            basket.get("cheapest_total"), basket.get("priciest"),
            basket.get("priciest_total"), basket.get("max_overpay_pct"),
            s1.get("all_identical_share"), s2.get("median_cv"),
            suspicious, len(snaps), len(ts.get("snapshots", []) or []),
            "; ".join(notes) or None,
        ))
        conn.execute("DELETE FROM basket_history WHERE run_date=?", (date,))
        for rank, r in enumerate(basket.get("ranking", []) or [], start=1):
            conn.execute(
                "INSERT INTO basket_history (run_date, retailer, total, idx, wins, rank) "
                "VALUES (?,?,?,?,?,?)",
                (date, r.get("retailer"), r.get("total"), r.get("index"),
                 r.get("wins"), rank))
        conn.commit()
        conn.close()

        log(f"=== run {date} complete: status={status}, "
            f"basket={basket.get('basket_size')} items, "
            f"cheapest={basket.get('cheapest')} "
            f"(€{basket.get('cheapest_total')}), "
            f"snapshots={len(snaps)} ===", fh)
        return 0
    except SystemExit as e:
        log(f"ABORTED: {e}", fh)
        # still record the failed run
        try:
            conn = db_connect()
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_date, started_at, finished_at, "
                "duration_s, status, notes) VALUES (?,?,?,?,?,?)",
                (date, started.isoformat(timespec="seconds"),
                 datetime.datetime.now().isoformat(timespec="seconds"),
                 (datetime.datetime.now() - started).total_seconds(),
                 "failed", "; ".join(notes) or str(e)))
            conn.commit(); conn.close()
        except Exception:
            pass
        return 1
    finally:
        fh.close()
        try:
            os.unlink(LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
