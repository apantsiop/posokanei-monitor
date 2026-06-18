#!/usr/bin/env python3
"""
posokanei_snapshot.py — Capture one compact daily price snapshot for later
time-series collusion analysis.

Run this ONCE PER DAY (e.g. from cron). Each run writes:
    posokanei_data/snapshots/<YYYY-MM-DD>.csv
with one row per (product, retailer): the raw price + discount flag + the
retailer's own last_updated stamp.

These accumulating daily files are the input to posokanei_timeseries.py, which
is what actually distinguishes coordination from coincidence (a single snapshot
cannot — see that script's header).

Usage:
    python3 posokanei_snapshot.py                 # live fetch full catalog
    python3 posokanei_snapshot.py --date 2026-06-17
    python3 posokanei_snapshot.py --from-json posokanei_data/products.json
"""

import argparse
import csv
import datetime
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posokanei_data")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")

COLS = ["product_id", "name", "brand", "category", "retailer",
        "price", "is_discount", "last_updated"]


def rows_from_products(products):
    for p in products:
        pid = p.get("id")
        for rp in p.get("retailer_prices") or []:
            price = rp.get("price")
            ret = rp.get("retailer")
            if price is None or ret is None:
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            yield {
                "product_id": pid,
                "name": p.get("name"),
                "brand": p.get("brand"),
                "category": p.get("category"),
                "retailer": ret,
                "price": price,
                "is_discount": bool(rp.get("is_discount")),
                "last_updated": rp.get("last_updated"),
            }


def load_products(args):
    if args.from_json:
        import json
        with open(args.from_json, encoding="utf-8") as f:
            return json.load(f)
    # live fetch — reuse the scraper's paginator
    from posokanei_scrape import iter_all_products
    return list(iter_all_products(countries=args.countries,
                                  max_pages=args.limit, delay=args.delay))


def main():
    ap = argparse.ArgumentParser(description="Daily price snapshot for time-series analysis.")
    ap.add_argument("--date", default=None, help="snapshot date label (YYYY-MM-DD); default today")
    ap.add_argument("--countries", default="all")
    ap.add_argument("--limit", type=int, default=None, help="cap product pages (smoke test)")
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--from-json", default=None,
                    help="build the snapshot from an existing products.json instead of fetching")
    ap.add_argument("--out", default=SNAP_DIR)
    args = ap.parse_args()

    date = args.date or datetime.date.today().isoformat()
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{date}.csv")

    products = load_products(args)
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for row in rows_from_products(products):
            w.writerow(row)
            n += 1

    print(f"Snapshot {date}: {n} price points from {len(products)} products -> {path}")


if __name__ == "__main__":
    main()
