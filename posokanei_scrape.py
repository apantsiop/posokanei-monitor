#!/usr/bin/env python3
"""
posokanei_scrape.py — Harvest the full PosoKanei catalog (categories, products,
and per-retailer prices) from the public API at https://api.posokanei.gov.gr.

PosoKanei is the Greek government supermarket price-comparison service. The
Flutter web UI is backed by a plain paginated REST API; this script talks to
that API directly. No scraping of the rendered page is needed.

Outputs (written next to this script, into ./posokanei_data/):
  - categories.json   full hierarchical category tree (as served)
  - categories.csv    flattened: category_id, name, name_en, depth, parent path, counts
  - products.json     every product object, verbatim from the API
  - prices.csv        LONG format: one row per (product x retailer) price point
  - products.csv      one row per product (summary: min/avg/max price, etc.)

Zero third-party dependencies (uses only the Python standard library).

Usage:
  python3 posokanei_scrape.py                 # default: countries=all, page_size=100
  python3 posokanei_scrape.py --countries GR  # restrict to Greek retailers
  python3 posokanei_scrape.py --limit 5       # only first 5 product pages (smoke test)
"""

import argparse
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.posokanei.gov.gr"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posokanei_data")

# The API rejects page_size > 100 with HTTP 422.
MAX_PAGE_SIZE = 100


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def fetch_json(path, params=None, retries=5, backoff=1.5, timeout=30):
    """GET {API}{path}?params and return parsed JSON, with retries + backoff."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    # Identify the client honestly; this is a public API.
                    "User-Agent": "posokanei-scrape/1.0 (+public-data harvester)",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 4xx (except 429) won't get better by retrying.
            if e.code != 429 and 400 <= e.code < 500:
                raise
            last_err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e

        if attempt < retries:
            wait = backoff ** attempt
            print(f"  ! {url} failed ({last_err}); retry {attempt}/{retries} in {wait:.1f}s",
                  file=sys.stderr)
            time.sleep(wait)

    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_err}")


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #
def get_category_tree():
    """Fetch the full category tree with product counts."""
    return fetch_json(
        "/meta/categories/tree",
        {"include_counts": "true", "include_hidden": "false"},
    )


def flatten_categories(tree):
    """Walk the nested tree -> flat list of rows with a readable parent path."""
    rows = []

    def walk(node, path):
        name = node.get("name", "")
        full_path = path + [name]
        rows.append({
            "category_id": node.get("category_id"),
            "name": name,
            "name_en": node.get("name_en"),
            "depth": node.get("depth"),
            "path": " > ".join(p for p in full_path if p),
            "product_count": node.get("product_count"),
            "total_product_count": node.get("total_product_count"),
            "hidden": node.get("hidden"),
            "is_leaf": not node.get("children"),
        })
        for child in node.get("children", []) or []:
            walk(child, full_path)

    for root in tree.get("tree", []) or []:
        walk(root, [])
    return rows


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
def iter_all_products(countries="all", page_size=MAX_PAGE_SIZE, max_pages=None,
                      delay=0.3):
    """Yield every product object by paging /products. No category filter
    needed — the unfiltered endpoint returns the whole catalog."""
    page = 1
    total = None
    while True:
        params = {
            "page": page,
            "page_size": page_size,
            "sort_by": "unit_price",
            "sort_order": "asc",
            "countries": countries,
        }
        data = fetch_json("/products", params)
        if total is None:
            total = data.get("total")
            print(f"Catalog: {total} products, "
                  f"{data.get('total_pages')} pages @ page_size={page_size}")

        products = data.get("products", []) or []
        for p in products:
            yield p

        print(f"  page {page}/{data.get('total_pages')} "
              f"({len(products)} products, {data.get('query_time_ms')} ms)")

        if max_pages and page >= max_pages:
            break
        if not data.get("has_next"):
            break
        page += 1
        time.sleep(delay)  # be polite


# --------------------------------------------------------------------------- #
# CSV writers
# --------------------------------------------------------------------------- #
def write_categories_csv(path, rows):
    cols = ["category_id", "name", "name_en", "depth", "path",
            "product_count", "total_product_count", "is_leaf", "hidden"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_products_csv(path, products):
    """One row per product (summary)."""
    cols = ["id", "name", "brand", "category", "subcategory", "private_label",
            "unit", "unit_quantity", "min_price", "avg_price", "max_price",
            "min_unit_price", "retailer_count", "retailers",
            "available_countries", "is_international", "updated_at"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in products:
            ps = p.get("price_stats") or {}
            w.writerow({
                "id": p.get("id"),
                "name": p.get("name"),
                "brand": p.get("brand"),
                "category": p.get("category"),
                "subcategory": p.get("subcategory"),
                "private_label": p.get("private_label"),
                "unit": p.get("unit"),
                "unit_quantity": p.get("unit_quantity"),
                "min_price": ps.get("min_price"),
                "avg_price": ps.get("avg_price"),
                "max_price": ps.get("max_price"),
                "min_unit_price": ps.get("min_unit_price"),
                "retailer_count": ps.get("retailer_count"),
                "retailers": "|".join(p.get("retailers") or []),
                "available_countries": "|".join(p.get("available_countries") or []),
                "is_international": p.get("is_international"),
                "updated_at": p.get("updated_at"),
            })


def write_prices_csv(path, products):
    """LONG format: one row per (product, retailer) price point — this is the
    table you want for actual price comparison / analysis."""
    cols = ["product_id", "product_name", "brand", "category", "subcategory",
            "unit", "unit_quantity", "retailer", "retailer_display_name",
            "country", "price", "price_normalized", "is_discount",
            "discount_percentage", "last_updated"]
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in products:
            for rp in p.get("retailer_prices") or []:
                w.writerow({
                    "product_id": p.get("id"),
                    "product_name": p.get("name"),
                    "brand": p.get("brand"),
                    "category": p.get("category"),
                    "subcategory": p.get("subcategory"),
                    "unit": p.get("unit"),
                    "unit_quantity": p.get("unit_quantity"),
                    "retailer": rp.get("retailer"),
                    "retailer_display_name": rp.get("retailer_display_name"),
                    "country": rp.get("country"),
                    "price": rp.get("price"),
                    "price_normalized": rp.get("price_normalized"),
                    "is_discount": rp.get("is_discount"),
                    "discount_percentage": rp.get("discount_percentage"),
                    "last_updated": rp.get("last_updated"),
                })
                n += 1
    return n


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Harvest PosoKanei catalog + prices.")
    ap.add_argument("--countries", default="all",
                    help="country scope filter, e.g. 'all' or 'GR' (default: all)")
    ap.add_argument("--page-size", type=int, default=MAX_PAGE_SIZE,
                    help=f"products per page, max {MAX_PAGE_SIZE} (default: {MAX_PAGE_SIZE})")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N product pages (smoke test)")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="seconds between product pages (default: 0.3)")
    ap.add_argument("--out", default=OUT_DIR, help="output directory")
    args = ap.parse_args()

    page_size = min(args.page_size, MAX_PAGE_SIZE)
    os.makedirs(args.out, exist_ok=True)

    # 1) Categories
    print("Fetching category tree ...")
    tree = get_category_tree()
    with open(os.path.join(args.out, "categories.json"), "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, indent=2)
    cat_rows = flatten_categories(tree)
    write_categories_csv(os.path.join(args.out, "categories.csv"), cat_rows)
    leaves = sum(1 for r in cat_rows if r["is_leaf"])
    print(f"  {len(cat_rows)} categories ({leaves} leaves) -> categories.csv / .json")

    # 2) Products + prices
    print(f"\nFetching products (countries={args.countries}) ...")
    products = list(iter_all_products(
        countries=args.countries, page_size=page_size,
        max_pages=args.limit, delay=args.delay,
    ))

    with open(os.path.join(args.out, "products.json"), "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    write_products_csv(os.path.join(args.out, "products.csv"), products)
    n_prices = write_prices_csv(os.path.join(args.out, "prices.csv"), products)

    print(f"\nDone:")
    print(f"  {len(products)} products  -> products.json / products.csv")
    print(f"  {n_prices} price rows -> prices.csv")
    print(f"  output dir: {args.out}")


if __name__ == "__main__":
    main()
