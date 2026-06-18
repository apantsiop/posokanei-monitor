#!/usr/bin/env python3
"""
posokanei_cartel_screen.py — Screen PosoKanei price data for patterns that are
*consistent with* price coordination / cartel-like behaviour among retailers.

Input:  posokanei_data/products.json  (produced by posokanei_scrape.py)
Output: console report + ./posokanei_data/analysis/*.csv + screen_summary.json

================================  READ THIS  =================================
This is a SCREEN, not a verdict. A single cross-sectional price snapshot CANNOT
prove collusion. Identical / parallel prices have many lawful explanations:
  * Manufacturer Suggested Retail Price (MSRP) / recommended price
  * Resale Price Maintenance imposed by the supplier (vertical, not a cartel)
  * Competitive price-matching of an observable rival
  * Loss-leader promotions on the same hero SKUs
  * Data-pipeline rounding / dedup / normalization artifacts
Genuine cartel detection needs TIME SERIES (synchronized moves, structural
breaks, market-sharing). The metrics here are the standard "collusion screens"
used by competition authorities to decide WHERE TO LOOK — treat every flag as a
hypothesis to investigate, never as proof.
=============================================================================

Screens implemented (Harrington-style empirical collusion markers):
  1. Price-identity / parallelism screen  — how often rivals post identical prices
  2. Dispersion (variance) screen         — abnormally low cross-retailer spread
  3. Pairwise retailer screen             — identity rate + fixed-markup relations
  4. Focal-point / price-ending screen    — clustering at .99/.95/.00 etc.
  5. Discount-synchronization screen      — same SKU on offer at many chains at once

Zero third-party dependencies (Python standard library only).
"""

import csv
import datetime
import html
import json
import os
import statistics
from collections import Counter, defaultdict
from itertools import combinations

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posokanei_data")
PRODUCTS_JSON = os.path.join(DATA_DIR, "products.json")
OUT_DIR = os.path.join(DATA_DIR, "analysis")

# A pair of retailers is "shared-heavy" enough to judge only above this count.
MIN_SHARED = 15
# Relative spread (range/min) below this counts a product as "near-identical".
NEAR_IDENTICAL_REL = 0.01  # within 1%


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def load_products():
    if not os.path.exists(PRODUCTS_JSON):
        raise SystemExit(f"Missing {PRODUCTS_JSON}. Run posokanei_scrape.py first.")
    with open(PRODUCTS_JSON, encoding="utf-8") as f:
        return json.load(f)


def retailer_price_points(prod):
    """Return [(retailer, price, is_discount), ...] with valid positive prices.
    Uses RAW price (price_normalized is unreliable for pcs/multipacks)."""
    out = []
    for rp in prod.get("retailer_prices") or []:
        p = rp.get("price")
        r = rp.get("retailer")
        if p is None or r is None:
            continue
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if p > 0:
            out.append((r, p, bool(rp.get("is_discount"))))
    return out


# --------------------------------------------------------------------------- #
# Screen 1 + 2: per-product identity & dispersion
# --------------------------------------------------------------------------- #
def per_product_stats(products):
    rows = []
    for prod in products:
        pts = retailer_price_points(prod)
        prices = [p for _, p, _ in pts]
        if len(prices) < 2:
            continue
        lo, hi = min(prices), max(prices)
        mean = statistics.fmean(prices)
        rng = hi - lo
        rel = rng / lo if lo else 0.0
        cv = (statistics.pstdev(prices) / mean) if mean else 0.0
        modal_price, modal_count = Counter(round(p, 2) for p in prices).most_common(1)[0]
        rows.append({
            "id": prod.get("id"),
            "name": prod.get("name"),
            "brand": prod.get("brand"),
            "category": prod.get("category"),
            "n_retailers": len(prices),
            "n_distinct": len(set(round(p, 2) for p in prices)),
            "modal_share": modal_count / len(prices),
            "min_price": round(lo, 4),
            "max_price": round(hi, 4),
            "mean_price": round(mean, 4),
            "abs_spread": round(rng, 4),
            "rel_spread": round(rel, 5),
            "cv": round(cv, 5),
            "all_identical": rng == 0,
            "near_identical": rel <= NEAR_IDENTICAL_REL,
        })
    return rows


# --------------------------------------------------------------------------- #
# Screen 3: pairwise retailer relationships
# --------------------------------------------------------------------------- #
def pairwise_screen(products):
    # shared[pair] = list of (price_a, price_b)
    shared = defaultdict(list)
    for prod in products:
        pts = retailer_price_points(prod)
        byret = {}
        for r, p, _ in pts:
            byret.setdefault(r, p)  # first listed price for that retailer
        for a, b in combinations(sorted(byret), 2):
            shared[(a, b)].append((byret[a], byret[b]))

    rows = []
    for (a, b), pairs in shared.items():
        n = len(pairs)
        if n < MIN_SHARED:
            continue
        identical = sum(1 for x, y in pairs if round(x, 2) == round(y, 2))
        ratios = [x / y for x, y in pairs if y]
        abs_pct = [abs(x - y) / ((x + y) / 2) for x, y in pairs if (x + y)]
        med_ratio = statistics.median(ratios) if ratios else None
        # consistency of the markup relationship: low stdev of ratio => one
        # retailer is a near-fixed multiple of the other (RPM-like).
        ratio_cv = (statistics.pstdev(ratios) / statistics.fmean(ratios)
                    if len(ratios) > 1 and statistics.fmean(ratios) else 0.0)
        rows.append({
            "retailer_a": a,
            "retailer_b": b,
            "n_shared": n,
            "identity_rate": round(identical / n, 4),
            "mean_abs_pct_diff": round(statistics.fmean(abs_pct), 4) if abs_pct else None,
            "median_ratio_a_over_b": round(med_ratio, 4) if med_ratio else None,
            "ratio_cv": round(ratio_cv, 4),
        })
    rows.sort(key=lambda r: r["identity_rate"], reverse=True)
    return rows


# --------------------------------------------------------------------------- #
# Screen 4: focal-point / price-ending clustering
# --------------------------------------------------------------------------- #
def price_ending_screen(products):
    overall = Counter()
    per_ret = defaultdict(Counter)
    total = 0
    per_ret_total = Counter()
    for prod in products:
        for r, p, _ in retailer_price_points(prod):
            cents = int(round(p * 100)) % 100
            overall[cents] += 1
            per_ret[r][cents] += 1
            total += 1
            per_ret_total[r] += 1

    top_overall = [(c, n, round(n / total, 4)) for c, n in overall.most_common(10)] if total else []

    # Concentration: share of each retailer's prices ending in the "magic" set.
    magic = {99, 95, 0, 49, 50, 98, 90}
    ret_focal = []
    for r, c in per_ret.items():
        tot = per_ret_total[r]
        if tot < 30:
            continue
        focal = sum(c[m] for m in magic)
        ret_focal.append({"retailer": r, "n_prices": tot,
                          "focal_share": round(focal / tot, 4)})
    ret_focal.sort(key=lambda x: x["focal_share"], reverse=True)
    return {"total_price_points": total, "top_endings": top_overall,
            "retailer_focal": ret_focal}


# --------------------------------------------------------------------------- #
# Screen 5: discount synchronization
# --------------------------------------------------------------------------- #
def discount_sync_screen(products):
    n_with_any_discount = 0
    n_multi_discount = 0  # discounted at >=2 retailers simultaneously
    discount_counts = Counter()
    for prod in products:
        pts = retailer_price_points(prod)
        if len(pts) < 2:
            continue
        d = sum(1 for _, _, isd in pts if isd)
        if d >= 1:
            n_with_any_discount += 1
        if d >= 2:
            n_multi_discount += 1
        discount_counts[d] += 1
    return {
        "products_with_any_discount": n_with_any_discount,
        "products_discounted_at_2plus_retailers": n_multi_discount,
        "share_of_discounted_that_are_synchronized": round(
            n_multi_discount / n_with_any_discount, 4) if n_with_any_discount else None,
    }


# --------------------------------------------------------------------------- #
# Reporting helpers
# --------------------------------------------------------------------------- #
def pct(x):
    return f"{100 * x:.1f}%" if x is not None else "n/a"


def write_csv(path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- #
# HTML infographic (Bootstrap 5 + Chart.js, both via CDN — single self-contained file)
# --------------------------------------------------------------------------- #
def histogram(values, edges):
    """Count values into [edges[i], edges[i+1]) buckets; last bucket is open."""
    counts = [0] * len(edges)
    for v in values:
        placed = False
        for i in range(len(edges) - 1):
            if edges[i] <= v < edges[i + 1]:
                counts[i] += 1
                placed = True
                break
        if not placed and v >= edges[-1]:
            counts[-1] += 1
    return counts


def render_html(summary, charts, flags, verdict, generated):
    s1, s2 = summary["screen1_identity"], summary["screen2_dispersion"]
    disc = summary["screen5_discount_sync"]

    badge = {"High concern": "danger", "Moderate concern": "warning",
             "Competitive (low concern)": "success"}[verdict["level"]]

    # KPI cards
    def kpi(value, label, sub=""):
        sub_html = f'<div class="small text-secondary">{html.escape(sub)}</div>' if sub else ""
        return f"""<div class="col-6 col-lg-3">
          <div class="card kpi h-100 shadow-sm border-0"><div class="card-body text-center">
            <div class="kpi-num">{html.escape(str(value))}</div>
            <div class="text-uppercase small fw-semibold text-secondary">{html.escape(label)}</div>
            {sub_html}
          </div></div></div>"""

    kpis = "".join([
        kpi(s1["all_identical"], "products fully identical", f"{100*s1['all_identical_share']:.1f}% of multi-retailer"),
        kpi(f"{100*s2['median_cv']:.1f}%", "median price dispersion", "low = suspicious"),
        kpi(verdict["suspicious_pairs"], "suspicious retailer pairs", f"{summary['screen3_pairs']['pairs_evaluated']} pairs evaluated"),
        kpi(summary["products_analyzed_multiretailer"], "products compared", "sold by 2+ retailers"),
    ])

    # Flags
    if flags:
        flag_items = "".join(
            f'<li class="list-group-item d-flex align-items-start gap-2">'
            f'<span class="badge text-bg-warning rounded-pill mt-1">!</span>'
            f'<span>{html.escape(f)}</span></li>' for f in flags)
        flags_html = f'<ul class="list-group list-group-flush">{flag_items}</ul>'
    else:
        flags_html = ('<div class="p-3 text-success"><strong>No screen crossed its '
                      'heuristic threshold.</strong> Patterns look competitive on this dataset.</div>')

    # Pairs table
    pair_rows = "".join(
        f"<tr><td>{html.escape(p['retailer_a'])}</td><td>{html.escape(p['retailer_b'])}</td>"
        f"<td class='text-end'>{p['n_shared']}</td>"
        f"<td class='text-end fw-semibold'>{100*p['identity_rate']:.1f}%</td>"
        f"<td class='text-end'>{p['ratio_cv']}</td></tr>"
        for p in summary["screen3_pairs"]["top_identity_pairs"])

    data_json = json.dumps(charts)

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PosoKanei — Price-Coordination Screen</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
  body {{ background:#f5f7fb; }}
  .hero {{ background:linear-gradient(135deg,#1e3a8a,#2563eb 55%,#0ea5e9); color:#fff; }}
  .kpi-num {{ font-size:2.2rem; font-weight:800; line-height:1; color:#1e3a8a; }}
  .card-title-bar {{ font-weight:700; }}
  .chart-box {{ position:relative; height:300px; }}
  .section-num {{ display:inline-flex; width:1.9rem; height:1.9rem; border-radius:50%;
                  background:#2563eb; color:#fff; align-items:center; justify-content:center;
                  font-weight:700; margin-right:.5rem; }}
  footer {{ color:#64748b; }}
</style></head>
<body>

<div class="hero py-5 mb-4 shadow">
  <div class="container">
    <div class="d-flex flex-wrap justify-content-between align-items-center gap-3">
      <div>
        <div class="text-uppercase small fw-semibold opacity-75">PosoKanei · Greek supermarket price comparison</div>
        <h1 class="display-6 fw-bold mb-1">Price-Coordination Screen</h1>
        <p class="mb-0 opacity-75">Statistical screening for cartel-like pricing &middot; generated {html.escape(generated)}</p>
      </div>
      <div class="text-center">
        <div class="badge text-bg-{badge} fs-6 p-3 rounded-4">{html.escape(verdict['level'])}</div>
        <div class="small opacity-75 mt-2" style="max-width:16rem">{html.escape(verdict['headline'])}</div>
      </div>
    </div>
  </div>
</div>

<div class="container pb-5">

  <div class="alert alert-{badge} d-flex align-items-center" role="alert">
    <i class="bi bi-info-circle-fill me-2 fs-5"></i>
    <div><strong>This is a screen, not a verdict.</strong> Identical/parallel prices also arise lawfully (MSRP,
    resale-price maintenance, price-matching, commonly-owned banners). Proving a cartel needs time-series evidence of
    synchronized price <em>movements</em> — see the companion time-series screen.</div>
  </div>

  <div class="row g-3 mb-4">{kpis}</div>

  <div class="row g-4">
    <div class="col-lg-6"><div class="card shadow-sm border-0 h-100"><div class="card-body">
      <h5 class="card-title-bar"><span class="section-num">1</span>Price identity &amp; parallelism</h5>
      <p class="text-secondary small">How often rivals selling the same product post the very same price.</p>
      <div class="chart-box"><canvas id="identityChart"></canvas></div>
    </div></div></div>

    <div class="col-lg-6"><div class="card shadow-sm border-0 h-100"><div class="card-body">
      <h5 class="card-title-bar"><span class="section-num">2</span>Cross-retailer dispersion</h5>
      <p class="text-secondary small">Spread of prices across retailers per product. Healthy competition shows
      dispersion; a cartel compresses it (bars piled on the left = suspicious).</p>
      <div class="chart-box"><canvas id="cvChart"></canvas></div>
    </div></div></div>

    <div class="col-lg-7"><div class="card shadow-sm border-0 h-100"><div class="card-body">
      <h5 class="card-title-bar"><span class="section-num">3</span>Retailer pairs — price-match rate</h5>
      <p class="text-secondary small">Share of shared products where the two chains post an identical price.</p>
      <div class="chart-box"><canvas id="pairsChart"></canvas></div>
      <div class="table-responsive mt-3"><table class="table table-sm align-middle mb-0">
        <thead><tr><th>Retailer A</th><th>Retailer B</th><th class="text-end">Shared</th>
        <th class="text-end">Identical</th><th class="text-end">Ratio CV</th></tr></thead>
        <tbody>{pair_rows}</tbody></table></div>
    </div></div></div>

    <div class="col-lg-5"><div class="card shadow-sm border-0 h-100"><div class="card-body">
      <h5 class="card-title-bar"><span class="section-num">4</span>Price-ending focal points</h5>
      <p class="text-secondary small">Clustering at psychological endings (.99/.95/.00) hints at shared
      recommended pricing.</p>
      <div class="chart-box"><canvas id="endingChart"></canvas></div>
    </div></div></div>

    <div class="col-lg-7"><div class="card shadow-sm border-0 h-100"><div class="card-body">
      <h5 class="card-title-bar"><i class="bi bi-flag-fill text-warning me-2"></i>Heuristic flags</h5>
      <p class="text-secondary small">Where to investigate — not findings of wrongdoing.</p>
      {flags_html}
    </div></div></div>

    <div class="col-lg-5"><div class="card shadow-sm border-0 h-100"><div class="card-body">
      <h5 class="card-title-bar"><span class="section-num">5</span>Discount synchronization</h5>
      <p class="text-secondary small">Same product on offer at multiple chains at once.</p>
      <div class="display-5 fw-bold text-primary">{(100*disc['share_of_discounted_that_are_synchronized']) if disc['share_of_discounted_that_are_synchronized'] else 0:.1f}%</div>
      <div class="text-secondary">of discounted products are discounted at 2+ retailers simultaneously
      ({disc['products_discounted_at_2plus_retailers']} of {disc['products_with_any_discount']}).</div>
    </div></div></div>
  </div>

  <div class="card border-0 shadow-sm mt-4"><div class="card-body">
    <h6 class="fw-bold"><i class="bi bi-shield-exclamation me-1"></i>Method &amp; caveats</h6>
    <p class="small text-secondary mb-1">Built from a single price snapshot via the public PosoKanei API. The screens
    follow the empirical "collusion markers" competition authorities use to scope investigations: price identity,
    low dispersion, pairwise matching, and focal-point clustering. A near-constant price ratio between two chains
    (low <em>ratio CV</em>) is consistent with resale-price maintenance / MSRP rather than a horizontal cartel.</p>
    <p class="small text-secondary mb-0">None of these metrics prove collusion. Rule out common-cost shocks
    (wholesale, energy, FX, VAT) and common ownership before drawing conclusions, and corroborate with time-series
    analysis of synchronized price changes.</p>
  </div></div>

  <footer class="text-center small mt-4">
    PosoKanei coordination screen · automated statistical screening · for analysis, not legal conclusions
  </footer>
</div>

<script>
const D = {data_json};
const palette = ['#2563eb','#0ea5e9','#22c55e','#f59e0b','#ef4444','#8b5cf6','#ec4899','#14b8a6','#64748b','#eab308'];
Chart.defaults.font.family = 'system-ui, sans-serif';

new Chart(identityChart, {{ type:'doughnut',
  data:{{ labels:['All identical','Within 1%','Genuinely different'],
    datasets:[{{ data:D.identity, backgroundColor:['#ef4444','#f59e0b','#22c55e'] }}] }},
  options:{{ maintainAspectRatio:false, plugins:{{ legend:{{ position:'bottom' }} }} }} }});

new Chart(cvChart, {{ type:'bar',
  data:{{ labels:D.cv_labels, datasets:[{{ label:'# products', data:D.cv_counts,
    backgroundColor:D.cv_labels.map((_,i)=> i<2 ? '#ef4444' : i<3 ? '#f59e0b' : '#22c55e') }}] }},
  options:{{ maintainAspectRatio:false, plugins:{{ legend:{{ display:false }} }},
    scales:{{ y:{{ beginAtZero:true, title:{{ display:true, text:'products' }} }},
              x:{{ title:{{ display:true, text:'price dispersion (CV) across retailers' }} }} }} }} }});

new Chart(pairsChart, {{ type:'bar',
  data:{{ labels:D.pair_labels, datasets:[{{ label:'identical-price rate',
    data:D.pair_rates, backgroundColor:D.pair_rates.map(v=> v>=50?'#ef4444':v>=30?'#f59e0b':'#2563eb') }}] }},
  options:{{ indexAxis:'y', maintainAspectRatio:false, plugins:{{ legend:{{ display:false }} }},
    scales:{{ x:{{ beginAtZero:true, max:100, title:{{ display:true, text:'% of shared products at identical price' }} }} }} }} }});

new Chart(endingChart, {{ type:'bar',
  data:{{ labels:D.ending_labels, datasets:[{{ label:'% of prices', data:D.ending_shares,
    backgroundColor:'#0ea5e9' }}] }},
  options:{{ maintainAspectRatio:false, plugins:{{ legend:{{ display:false }} }},
    scales:{{ y:{{ beginAtZero:true, title:{{ display:true, text:'% of all prices' }} }},
              x:{{ title:{{ display:true, text:'price ending (cents)' }} }} }} }} }});
</script>
</body></html>"""


def compute_flags(summary, pairs):
    s1, s2 = summary["screen1_identity"], summary["screen2_dispersion"]
    disc = summary["screen5_discount_sync"]
    flags = []
    if s1["deep_all_identical_share"] and s1["deep_all_identical_share"] >= 0.30:
        flags.append(f"High identical pricing among 4+ rivals ({pct(s1['deep_all_identical_share'])}).")
    if s2["median_cv"] < 0.02:
        flags.append(f"Very low price dispersion (median CV {pct(s2['median_cv'])}).")
    hot = [r for r in pairs if r["identity_rate"] >= 0.5 and r["n_shared"] >= MIN_SHARED]
    if hot:
        names = ", ".join(f"{r['retailer_a']}↔{r['retailer_b']} ({pct(r['identity_rate'])})" for r in hot[:5])
        flags.append(f"{len(hot)} retailer pair(s) match prices ≥50% of the time: {names}.")
    rpm = [r for r in pairs if r["ratio_cv"] <= 0.03 and r["n_shared"] >= MIN_SHARED]
    if rpm:
        flags.append(f"{len(rpm)} pair(s) hold a near-constant price ratio (possible MSRP / resale-price maintenance).")
    if disc["share_of_discounted_that_are_synchronized"] and \
            disc["share_of_discounted_that_are_synchronized"] >= 0.4:
        flags.append("Discounts frequently coincide across chains.")
    return flags, hot, rpm


def compute_verdict(summary, hot):
    s1, s2 = summary["screen1_identity"], summary["screen2_dispersion"]
    deep = s1["deep_all_identical_share"] or 0
    if s2["median_cv"] < 0.02 or deep >= 0.30:
        level, headline = "High concern", "Compressed dispersion and/or widespread identical pricing."
    elif s2["median_cv"] < 0.05:
        level, headline = "Moderate concern", "Some coordination markers; investigate flagged pairs."
    else:
        level, headline = "Competitive (low concern)", "Healthy price dispersion market-wide; check flagged pairs."
    return {"level": level, "headline": headline, "suspicious_pairs": len(hot)}


def build_charts(pp, summary):
    s1 = summary["screen1_identity"]
    n = summary["products_analyzed_multiretailer"]
    all_id = s1["all_identical"]
    near_only = max(0, s1["near_identical_within_1pct"] - all_id)
    different = max(0, n - all_id - near_only)

    cvs = [r["cv"] for r in pp]
    edges = [0, 0.01, 0.02, 0.05, 0.10, 0.20, 0.40]
    cv_labels = ["<1%", "1-2%", "2-5%", "5-10%", "10-20%", "20-40%", "40%+"]

    pairs_top = summary["screen3_pairs"]["top_identity_pairs"]
    endings = summary["screen4_focal"]["top_endings"][:8]
    return {
        "identity": [all_id, near_only, different],
        "cv_labels": cv_labels,
        "cv_counts": histogram(cvs, edges),
        "pair_labels": [f"{p['retailer_a']} x {p['retailer_b']}" for p in pairs_top],
        "pair_rates": [round(100 * p["identity_rate"], 1) for p in pairs_top],
        "ending_labels": [f".{c:02d}" for c, _, _ in endings],
        "ending_shares": [round(100 * sh, 2) for _, _, sh in endings],
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    products = load_products()

    pp = per_product_stats(products)
    n_multi = len(pp)
    if n_multi == 0:
        raise SystemExit("No products sold by >=2 retailers — nothing to compare.")

    # ---- Screen 1 & 2 aggregates ----
    n_all_ident = sum(1 for r in pp if r["all_identical"])
    n_near_ident = sum(1 for r in pp if r["near_identical"])
    cvs = sorted(r["cv"] for r in pp)
    rels = sorted(r["rel_spread"] for r in pp)
    med_cv = statistics.median(cvs)
    med_rel = statistics.median(rels)

    # Restrict the strongest signal to products with many rivals: when 4+ chains
    # all post the byte-identical price, coincidence is far less plausible.
    deep = [r for r in pp if r["n_retailers"] >= 4]
    deep_ident = [r for r in deep if r["all_identical"]]

    pairs = pairwise_screen(products)
    endings = price_ending_screen(products)
    disc = discount_sync_screen(products)

    # ---- write detail CSVs ----
    flagged = sorted([r for r in pp if r["near_identical"] and r["n_retailers"] >= 3],
                     key=lambda r: (-r["n_retailers"], r["rel_spread"]))
    write_csv(os.path.join(OUT_DIR, "flagged_products.csv"), flagged,
              ["id", "name", "brand", "category", "n_retailers", "n_distinct",
               "modal_share", "min_price", "max_price", "abs_spread",
               "rel_spread", "cv", "all_identical"])
    write_csv(os.path.join(OUT_DIR, "per_product_stats.csv"), pp,
              ["id", "name", "brand", "category", "n_retailers", "n_distinct",
               "modal_share", "min_price", "max_price", "mean_price",
               "abs_spread", "rel_spread", "cv", "all_identical", "near_identical"])
    write_csv(os.path.join(OUT_DIR, "retailer_pairs.csv"), pairs,
              ["retailer_a", "retailer_b", "n_shared", "identity_rate",
               "mean_abs_pct_diff", "median_ratio_a_over_b", "ratio_cv"])

    summary = {
        "products_analyzed_multiretailer": n_multi,
        "screen1_identity": {
            "all_identical": n_all_ident,
            "all_identical_share": round(n_all_ident / n_multi, 4),
            "near_identical_within_1pct": n_near_ident,
            "near_identical_share": round(n_near_ident / n_multi, 4),
            "deep_4plus_retailers": len(deep),
            "deep_all_identical": len(deep_ident),
            "deep_all_identical_share": round(len(deep_ident) / len(deep), 4) if deep else None,
        },
        "screen2_dispersion": {
            "median_cv": round(med_cv, 4),
            "median_rel_spread": round(med_rel, 4),
            "share_cv_under_1pct": round(sum(1 for c in cvs if c < 0.01) / n_multi, 4),
            "share_cv_under_2pct": round(sum(1 for c in cvs if c < 0.02) / n_multi, 4),
        },
        "screen3_pairs": {
            "pairs_evaluated": len(pairs),
            "top_identity_pairs": pairs[:10],
        },
        "screen4_focal": endings,
        "screen5_discount_sync": disc,
    }
    with open(os.path.join(OUT_DIR, "screen_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # ---- shared interpretation + HTML infographic ----
    flags, hot, rpm = compute_flags(summary, pairs)
    verdict = compute_verdict(summary, hot)
    charts = build_charts(pp, summary)
    html_path = os.path.join(OUT_DIR, "report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(summary, charts, flags, verdict,
                            datetime.date.today().isoformat()))

    # ---- console report ----
    s1, s2 = summary["screen1_identity"], summary["screen2_dispersion"]
    print("=" * 70)
    print("PosoKanei CARTEL / PRICE-COORDINATION SCREEN")
    print("=" * 70)
    print(f"Products sold by >=2 retailers: {n_multi}\n")

    print("SCREEN 1 — Price identity / parallelism")
    print(f"  All retailers post the EXACT same price : {s1['all_identical']:>5}  "
          f"({pct(s1['all_identical_share'])})")
    print(f"  Prices within 1% of each other         : {s1['near_identical_within_1pct']:>5}  "
          f"({pct(s1['near_identical_share'])})")
    print(f"  Products with 4+ rivals                : {s1['deep_4plus_retailers']:>5}")
    print(f"    ...of those, ALL identical           : {s1['deep_all_identical']:>5}  "
          f"({pct(s1['deep_all_identical_share'])})  <- strongest single signal\n")

    print("SCREEN 2 — Cross-retailer dispersion (low = suspicious)")
    print(f"  Median coefficient of variation        : {pct(s2['median_cv'])}")
    print(f"  Median relative spread (max-min)/min    : {pct(s2['median_rel_spread'])}")
    print(f"  Share of products with CV < 1%          : {pct(s2['share_cv_under_1pct'])}")
    print(f"  Share of products with CV < 2%          : {pct(s2['share_cv_under_2pct'])}\n")

    print(f"SCREEN 3 — Pairwise retailer identity (pairs with >= {MIN_SHARED} shared SKUs)")
    if pairs:
        print(f"  {'retailer_a':<16}{'retailer_b':<16}{'shared':>7}{'identical':>11}{'ratioCV':>9}")
        for r in pairs[:10]:
            print(f"  {r['retailer_a']:<16}{r['retailer_b']:<16}{r['n_shared']:>7}"
                  f"{pct(r['identity_rate']):>11}{r['ratio_cv']:>9}")
    else:
        print("  (not enough shared SKUs at this dataset size)")
    print()

    print("SCREEN 4 — Price-ending clustering (focal points)")
    print(f"  Total price points: {endings['total_price_points']}")
    print("  Most common cent endings:", ", ".join(
        f".{c:02d}={pct(sh)}" for c, _, sh in endings["top_endings"][:6]))
    if endings["retailer_focal"]:
        top = endings["retailer_focal"][0]
        print(f"  Highest focal-pricing retailer: {top['retailer']} "
              f"({pct(top['focal_share'])} of prices end in .99/.95/.00/.49/...)\n")

    print("SCREEN 5 — Discount synchronization")
    print(f"  Products on discount somewhere          : {disc['products_with_any_discount']}")
    print(f"  ...discounted at 2+ chains at once       : "
          f"{disc['products_discounted_at_2plus_retailers']}  "
          f"({pct(disc['share_of_discounted_that_are_synchronized'])} of discounted)\n")

    # ---- heuristic interpretation ----
    print("=" * 70)
    print("HEURISTIC READING (flags = where to investigate, NOT proof)")
    print("=" * 70)
    if flags:
        for fl in flags:
            print(f"  [!] {fl}")
    else:
        print("  No screen crossed its heuristic threshold on this dataset.")
    print("\n  Reminder: identical prices are commonly lawful (MSRP, resale-price")
    print("  maintenance, price-matching, promos). Proving a cartel needs TIME-")
    print("  SERIES evidence of synchronized moves — re-run the scraper daily and")
    print("  test for parallel price changes over time.")
    print(f"\n  Detail written to: {OUT_DIR}/")
    print("    report.html  <- open this (Bootstrap + Chart.js infographic)")
    print("    flagged_products.csv, per_product_stats.csv, retailer_pairs.csv,")
    print("    screen_summary.json")


if __name__ == "__main__":
    main()
