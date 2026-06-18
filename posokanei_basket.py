#!/usr/bin/env python3
"""
posokanei_basket.py — "Who is cheapest?" basket comparison.

Hypothetical scenario: build ONE giant shopping cart containing every product
that *all* of the compared retailers carry, then ring it up at each retailer.
Whoever totals lowest is the cheapest supermarket for that common basket.

Why "common" products only
---------------------------
You can only compare a basket fairly if every retailer actually stocks every
item in it. No single product is carried by all 22 retailers on PosoKanei (the
list mixes big Greek chains with tiny international ones), so the basket is the
*intersection*: products carried by every retailer in the comparison set.

The more retailers you demand, the smaller that intersection gets. By default
we compare the top-N retailers by catalogue size (N=8 -> ~1,123 shared
products: the major Greek chains). Tune with --top / --retailers.

Pricing
-------
Uses the raw shelf `price` (the amount you actually pay, current discount
included), not price_normalized — because we buy the *same product id* at every
retailer, raw prices are already like-for-like. If a retailer lists the same
product more than once, the lowest of its prices is used.

Outputs (into ./posokanei_data/basket/):
  - report.html          Bootstrap 5 + Chart.js infographic (cheapest = 100%)
  - basket_totals.csv     one row per retailer: total, index, wins, ...
  - basket_items.csv      one row per basket product x retailer price
  - basket_summary.json   machine-readable summary

Zero third-party dependencies (Python standard library only).

Usage:
  python3 posokanei_basket.py                 # top-8 retailers by coverage
  python3 posokanei_basket.py --top 6
  python3 posokanei_basket.py --retailers masoutis,sklavenitis,ab_vasilopoulos
"""

import argparse
import csv
import datetime
import html
import json
import os
from collections import Counter, defaultdict

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posokanei_data")
PRODUCTS_JSON = os.path.join(DATA_DIR, "products.json")
OUT_DIR = os.path.join(DATA_DIR, "basket")


# --------------------------------------------------------------------------- #
# Load & index
# --------------------------------------------------------------------------- #
def load_products(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def retailer_prices(prod):
    """{retailer: lowest_price} for one product (raw shelf price)."""
    best = {}
    names = {}
    for rp in prod.get("retailer_prices") or []:
        r = rp.get("retailer")
        p = rp.get("price")
        if not r or p is None:
            continue
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        if r not in best or p < best[r]:
            best[r] = p
        names.setdefault(r, rp.get("retailer_display_name") or r)
    return best, names


def coverage(products):
    """retailer -> #products carried, plus display-name map."""
    cnt = Counter()
    disp = {}
    for prod in products:
        best, names = retailer_prices(prod)
        for r in best:
            cnt[r] += 1
            disp.setdefault(r, names.get(r, r))
    return cnt, disp


def choose_retailers(args, cnt):
    if args.retailers:
        chosen = [r.strip() for r in args.retailers.split(",") if r.strip()]
        missing = [r for r in chosen if r not in cnt]
        if missing:
            raise SystemExit(f"Unknown retailer(s): {', '.join(missing)}\n"
                             f"Available: {', '.join(r for r, _ in cnt.most_common())}")
        return chosen
    return [r for r, _ in cnt.most_common(args.top)]


# --------------------------------------------------------------------------- #
# Basket
# --------------------------------------------------------------------------- #
def build_basket(products, retailers):
    """Products carried by EVERY retailer in `retailers`.
    Returns list of dicts: {id, name, brand, category, prices:{r:price}}."""
    rset = set(retailers)
    basket = []
    for prod in products:
        best, _ = retailer_prices(prod)
        if rset <= set(best):  # every compared retailer stocks it
            basket.append({
                "id": prod.get("id"),
                "name": prod.get("name"),
                "brand": prod.get("brand"),
                "category": prod.get("category"),
                "prices": {r: best[r] for r in retailers},
            })
    return basket


def tally(basket, retailers):
    totals = {r: 0.0 for r in retailers}
    wins = Counter()
    for item in basket:
        prices = item["prices"]
        for r in retailers:
            totals[r] += prices[r]
        lo = min(prices.values())
        # split a tie among all retailers sharing the lowest price
        winners = [r for r in retailers if abs(prices[r] - lo) < 1e-9]
        for r in winners:
            wins[r] += 1.0 / len(winners)
    return totals, wins


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def render_html(summary, rows, generated):
    cheapest_total = rows[0]["total"]
    data_json = json.dumps({
        "labels": [r["display"] for r in rows],
        "index": [r["index"] for r in rows],
        # price-competitiveness on a true 0-100 scale: cheapest = 100%, pricier
        # chains proportionally lower (cheapest basket / this basket * 100).
        "competitiveness": [round(100.0 * cheapest_total / r["total"], 1) for r in rows],
        "overpay": [round(r["index"] - 100.0, 1) for r in rows],
        "totals": [round(r["total"], 2) for r in rows],
        "wins": [round(r["wins"], 1) for r in rows],
    }, ensure_ascii=False)

    cheapest = rows[0]
    dearest = rows[-1]
    spread = dearest["index"] - 100.0
    n_items = summary["basket_size"]

    table_rows = []
    for i, r in enumerate(rows):
        rank = i + 1
        badge = ""
        if rank == 1:
            badge = ' <span class="badge text-bg-success">cheapest</span>'
        elif rank == len(rows):
            badge = ' <span class="badge text-bg-danger">priciest</span>'
        extra = r["total"] - cheapest["total"]
        table_rows.append(
            f"<tr>"
            f"<td class='text-muted'>{rank}</td>"
            f"<td><strong>{html.escape(r['display'])}</strong>{badge}</td>"
            f"<td class='text-end'>€{r['total']:,.2f}</td>"
            f"<td class='text-end'>{r['index']:.1f}%</td>"
            f"<td class='text-end'>{'—' if extra < 0.005 else f'+€{extra:,.2f}'}</td>"
            f"<td class='text-end'>{r['avg']:.2f}</td>"
            f"<td class='text-end'>{r['wins']:.0f}</td>"
            f"</tr>"
        )
    table_html = "\n".join(table_rows)

    retailer_chips = " ".join(
        f"<span class='badge rounded-pill text-bg-light border me-1 mb-1'>{html.escape(r['display'])}</span>"
        for r in sorted(rows, key=lambda x: x["display"].lower())
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PosoKanei — Cheapest Supermarket Basket</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
  body {{ background:#f5f6f8; }}
  .hero {{ background:linear-gradient(135deg,#0d6efd,#6610f2); color:#fff; }}
  .kpi {{ font-size:2rem; font-weight:700; line-height:1; }}
  .kpi-sub {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.05em; opacity:.75; }}
  .card {{ border:0; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  footer {{ font-size:.8rem; }}
</style>
</head>
<body>

<div class="hero py-5 mb-4">
  <div class="container">
    <h1 class="display-6 mb-1"><i class="bi bi-cart4"></i> Who is the cheapest supermarket?</h1>
    <p class="lead mb-3">One giant cart of <strong>{n_items:,} products</strong> that
       <strong>all {summary['n_retailers']} compared chains</strong> stock, rung up at each one.</p>
    <div class="row g-4">
      <div class="col-6 col-md-3">
        <div class="kpi">{html.escape(cheapest['display'])}</div>
        <div class="kpi-sub">cheapest (100%)</div>
      </div>
      <div class="col-6 col-md-3">
        <div class="kpi">€{cheapest['total']:,.0f}</div>
        <div class="kpi-sub">cheapest basket total</div>
      </div>
      <div class="col-6 col-md-3">
        <div class="kpi">+{spread:.1f}%</div>
        <div class="kpi-sub">priciest vs cheapest</div>
      </div>
      <div class="col-6 col-md-3">
        <div class="kpi">€{dearest['total'] - cheapest['total']:,.0f}</div>
        <div class="kpi-sub">most you'd overpay</div>
      </div>
    </div>
  </div>
</div>

<div class="container pb-5">

  <div class="card mb-4">
    <div class="card-body">
      <h2 class="h5 mb-3">Price-competitiveness index — cheapest = 100%</h2>
      <p class="text-muted small mb-3">Each bar is the cheapest basket as a share of that
         retailer's basket (cheapest&nbsp;÷&nbsp;retailer). The cheapest sits at 100%; a
         shorter bar means a pricier basket. Hover for the exact total and how much more you'd pay.</p>
      <canvas id="indexChart" height="120"></canvas>
    </div>
  </div>

  <div class="row g-4 mb-4">
    <div class="col-lg-7">
      <div class="card h-100">
        <div class="card-body">
          <h2 class="h5 mb-3">Full ranking</h2>
          <div class="table-responsive">
            <table class="table table-sm table-hover align-middle mb-0">
              <thead>
                <tr>
                  <th>#</th><th>Retailer</th>
                  <th class="text-end">Basket total</th>
                  <th class="text-end">Index</th>
                  <th class="text-end">Extra vs cheapest</th>
                  <th class="text-end">Avg / item</th>
                  <th class="text-end">Items cheapest</th>
                </tr>
              </thead>
              <tbody>{table_html}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
    <div class="col-lg-5">
      <div class="card h-100">
        <div class="card-body">
          <h2 class="h5 mb-3">Where each chain wins</h2>
          <p class="text-muted small mb-3">Number of basket items on which each retailer
             has the (equal-)lowest price.</p>
          <canvas id="winsChart"></canvas>
        </div>
      </div>
    </div>
  </div>

  <div class="card mb-4">
    <div class="card-body">
      <h2 class="h6 mb-2"><i class="bi bi-info-circle"></i> What's in the comparison</h2>
      <p class="mb-2 small">Compared retailers (basket = products carried by <em>all</em> of them):</p>
      <div class="mb-3">{retailer_chips}</div>
      <ul class="small text-muted mb-0">
        <li>Basket = the <strong>{n_items:,}</strong> products every one of these chains stocks.</li>
        <li>Prices are the raw shelf price (current discounts included); the same product id is
            bought at each retailer, so totals are strictly like-for-like.</li>
        <li>This is an <em>unweighted</em> basket — every product counts once, regardless of how
            often a household actually buys it. Loyalty-card prices are not modelled.</li>
        <li>Adding more (smaller-catalogue) chains shrinks the common basket; this run used the
            {summary['n_retailers']} selected.</li>
      </ul>
    </div>
  </div>

  <footer class="text-muted text-center pt-2">
    Generated {generated} from PosoKanei open data · {summary['catalog_size']:,} products in catalogue ·
    basket of {n_items:,} shared items across {summary['n_retailers']} retailers.
  </footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script>
const D = {data_json};
const cheapestColor = '#198754';
// colour by how far below 100% (i.e. how much pricier) each bar is
const barColors = D.competitiveness.map((v,i) => i === 0 ? cheapestColor
    : `rgba(220,53,69,${{0.35 + 0.55*Math.min(1,(100-v)/15)}})`);

new Chart(document.getElementById('indexChart'), {{
  type: 'bar',
  data: {{
    labels: D.labels,
    datasets: [{{
      label: 'Price-competitiveness (%)',
      data: D.competitiveness,
      backgroundColor: barColors,
      borderRadius: 4,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{ callbacks: {{
        label: (c) => {{
          const i = c.dataIndex;
          const more = D.overpay[i] > 0 ? `  ·  +${{D.overpay[i].toFixed(1)}}% vs cheapest` : '  ·  cheapest';
          return ` ${{c.parsed.x.toFixed(1)}}%  (€${{D.totals[i].toLocaleString()}})${{more}}`;
        }}
      }} }}
    }},
    scales: {{
      x: {{
        min: 0,
        max: 100,
        title: {{ display: true, text: 'Price-competitiveness — cheapest = 100%' }},
        ticks: {{ callback: (v) => v + '%' }}
      }}
    }}
  }}
}});

new Chart(document.getElementById('winsChart'), {{
  type: 'bar',
  data: {{
    labels: D.labels,
    datasets: [{{
      label: 'Items where cheapest',
      data: D.wins,
      backgroundColor: '#0d6efd',
      borderRadius: 4,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ title: {{ display: true, text: '# basket items at lowest price' }} }} }}
  }}
}});
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def write_totals_csv(path, rows):
    cols = ["rank", "retailer", "display", "basket_total", "index_pct",
            "extra_vs_cheapest", "avg_per_item", "items_cheapest"]
    cheapest = rows[0]["total"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i, r in enumerate(rows):
            w.writerow([i + 1, r["retailer"], r["display"], f"{r['total']:.2f}",
                        f"{r['index']:.2f}", f"{r['total'] - cheapest:.2f}",
                        f"{r['avg']:.4f}", f"{r['wins']:.1f}"])


def write_items_csv(path, basket, retailers):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "name", "brand", "category",
                    "cheapest_retailer", "cheapest_price"] + retailers)
        for item in basket:
            prices = item["prices"]
            lo = min(prices.values())
            winner = min(retailers, key=lambda r: prices[r])
            w.writerow([item["id"], item["name"], item["brand"], item["category"],
                        winner, f"{lo:.2f}"] + [f"{prices[r]:.2f}" for r in retailers])


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Cheapest-supermarket basket comparison.")
    ap.add_argument("--products", default=PRODUCTS_JSON)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--top", type=int, default=8,
                    help="compare the top-N retailers by catalogue size (default 8)")
    ap.add_argument("--retailers", default=None,
                    help="explicit comma-separated retailer keys (overrides --top)")
    args = ap.parse_args()

    products = load_products(args.products)
    cnt, disp = coverage(products)
    retailers = choose_retailers(args, cnt)
    if len(retailers) < 2:
        raise SystemExit("Need at least 2 retailers to compare.")

    basket = build_basket(products, retailers)
    if not basket:
        raise SystemExit("No product is carried by all selected retailers — "
                         "reduce --top or pick higher-coverage --retailers.")

    totals, wins = tally(basket, retailers)
    n = len(basket)

    rows = []
    for r in retailers:
        rows.append({
            "retailer": r,
            "display": disp.get(r, r),
            "total": totals[r],
            "avg": totals[r] / n,
            "wins": wins.get(r, 0.0),
        })
    rows.sort(key=lambda x: x["total"])
    cheapest_total = rows[0]["total"]
    for r in rows:
        r["index"] = 100.0 * r["total"] / cheapest_total

    os.makedirs(args.out, exist_ok=True)
    summary = {
        "generated": datetime.date.today().isoformat(),
        "catalog_size": len(products),
        "basket_size": n,
        "n_retailers": len(retailers),
        "retailers": retailers,
        "cheapest": rows[0]["retailer"],
        "cheapest_total": round(cheapest_total, 2),
        "priciest": rows[-1]["retailer"],
        "priciest_total": round(rows[-1]["total"], 2),
        "max_overpay_pct": round(rows[-1]["index"] - 100.0, 2),
        "ranking": [{"retailer": r["retailer"], "total": round(r["total"], 2),
                     "index": round(r["index"], 2), "wins": round(r["wins"], 1)}
                    for r in rows],
    }
    with open(os.path.join(args.out, "basket_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    write_totals_csv(os.path.join(args.out, "basket_totals.csv"), rows)
    write_items_csv(os.path.join(args.out, "basket_items.csv"), basket, retailers)
    html_path = os.path.join(args.out, "report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(summary, rows, summary["generated"]))

    # console summary
    print(f"Basket: {n} products carried by all {len(retailers)} retailers "
          f"({', '.join(retailers)})\n")
    w = max(len(r["display"]) for r in rows)
    print(f"  {'#':>2}  {'retailer':<{w}}  {'total':>11}  {'index':>7}  {'wins':>6}")
    for i, r in enumerate(rows):
        print(f"  {i+1:>2}  {r['display']:<{w}}  €{r['total']:>9,.2f}  "
              f"{r['index']:>6.1f}%  {r['wins']:>6.0f}")
    print(f"\nCheapest: {rows[0]['display']} (€{cheapest_total:,.2f}). "
          f"Priciest pays {summary['max_overpay_pct']:.1f}% more "
          f"(€{rows[-1]['total'] - cheapest_total:,.2f} extra).")
    print(f"\nWrote -> {args.out}/")
    print("  report.html        <- open this (Bootstrap + Chart.js infographic)")
    print("  basket_totals.csv  basket_items.csv  basket_summary.json")


if __name__ == "__main__":
    main()
