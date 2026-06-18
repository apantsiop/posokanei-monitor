#!/usr/bin/env python3
"""
webapp.py — the UI. A zero-dependency web app (stdlib http.server) that surfaces
everything the daily orchestrator produces.

Bilingual (Greek / English): language is chosen via ?lang=el|en, remembered in a
cookie, and otherwise guessed from Accept-Language (default Greek).

Routes:
  GET /                         dashboard: latest KPIs, verdict, trend charts, links
  GET /runs                     run-history table
  GET /report/cartel[?date=]    the collusion-screen infographic  (latest or archived)
  GET /report/basket[?date=]    the cheapest-supermarket infographic
  GET /report/timeseries        parallel-movement screen status / summary
  GET /api/trends               JSON for the dashboard charts
  GET /api/runs                 JSON run history
  GET /healthz                  liveness probe

Reads from posokanei_data/ (SQLite DB + archived per-run HTML reports).

Usage:
  python3 webapp.py                 # serve on 0.0.0.0:8000
  PORT=9000 python3 webapp.py
"""

import datetime
import html
import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "posokanei_data")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
DB_PATH = os.path.join(DATA_DIR, "posokanei.db")
PORT = int(os.environ.get("PORT", "8000"))

LANGS = ("el", "en")
DEFAULT_LANG = "el"


# --------------------------------------------------------------------------- #
# i18n
# --------------------------------------------------------------------------- #
T = {
    "en": {
        "html_lang": "en",
        "nav_dashboard": "Dashboard",
        "nav_cheapest": "Cheapest",
        "nav_collusion": "Collusion",
        "nav_movement": "Movement",
        "nav_runs": "Runs",
        # dashboard
        "dash_title": "Daily price monitor",
        "data_from": "data from",
        "status": "status",
        "last_attempt": "last attempt",
        "kpi_cheapest": "cheapest chain",
        "kpi_basket_total": "basket total ({n:,} items)",
        "kpi_overpay": "priciest pays more",
        "kpi_identical": "identically-priced products",
        "card_cheapest_title": "Cheapest supermarket",
        "card_cheapest_desc": "Giant common basket rung up at every chain. →",
        "card_collusion_title": "Collusion screen",
        "card_collusion_desc": "Price-identity & dispersion markers. {n} pair(s) flagged. →",
        "card_movement_title": "Parallel movement",
        "card_movement_desc": "{n} daily snapshots collected. →",
        "chart_basket": "Basket total per retailer over time",
        "chart_cheap": "Cheapest basket & max overpay",
        "chart_ident": "Identically-priced products (%)",
        "chart_cat": "Catalogue size",
        "ax_basket_total": "€ basket total",
        "lbl_cheapest_total": "cheapest total (€)",
        "lbl_max_overpay": "max overpay (%)",
        "lbl_pct_identical": "% identical",
        "lbl_products": "products",
        "banner_failed": "Latest run {date} didn't complete",
        "banner_showing_last": "Showing the last successful run below.",
        "no_success_title": "Latest run {date} failed and there is no earlier successful run yet.",
        "api_outage_hint": "This is usually the upstream API (api.posokanei.gov.gr) being "
                           "temporarily unavailable. The pipeline retries on the next daily "
                           "run; no action needed.",
        "see_logs": "see logs",
        # empty
        "empty_title": "No data yet",
        "empty_desc": "The daily pipeline hasn't produced a run. It runs automatically via "
                      "cron, or trigger one now:",
        # runs
        "runs_title": "Run history",
        "th_date": "Date", "th_status": "Status", "th_catalogue": "Catalogue",
        "th_basket": "Basket", "th_cheapest": "Cheapest", "th_total": "Total",
        "th_overpay": "Overpay", "th_snaps": "Snaps", "th_duration": "Duration",
        "th_reports": "Reports", "th_notes": "Notes",
        "link_basket": "basket", "link_collusion": "collusion",
        "no_runs": "no runs yet",
        # movement
        "mv_title": "Parallel-movement screen",
        "mv_accum": "Accumulating snapshots — <strong>{n}</strong> collected so far, "
                    "need <strong>≥2 days</strong> to measure price <em>movements</em>.",
        "mv_desc": "A single snapshot can show identical prices but cannot distinguish "
                   "coordination from coincidence. Once two or more daily snapshots exist, "
                   "this screen tests whether competitors change prices in lockstep "
                   "(co-movement lift, same-direction rate, price leadership).",
        "mv_window": "Window {a} … {b} ({n} snapshots).",
        # misc
        "report_unavailable": "Report not available",
        "report_for": "for {date}",
        "report_run_first": "Run the pipeline first:",
        "back_dashboard": "← Dashboard",
        "not_found": "Not found",
    },
    "el": {
        "html_lang": "el",
        "nav_dashboard": "Πίνακας",
        "nav_cheapest": "Φθηνότερο",
        "nav_collusion": "Σύμπραξη",
        "nav_movement": "Μεταβολές",
        "nav_runs": "Εκτελέσεις",
        # dashboard
        "dash_title": "Ημερήσια παρακολούθηση τιμών",
        "data_from": "δεδομένα από",
        "status": "κατάσταση",
        "last_attempt": "τελευταία προσπάθεια",
        "kpi_cheapest": "φθηνότερη αλυσίδα",
        "kpi_basket_total": "σύνολο καλαθιού ({n:,} προϊόντα)",
        "kpi_overpay": "ακριβότερη πληρώνει παραπάνω",
        "kpi_identical": "προϊόντα με πανομοιότυπη τιμή",
        "card_cheapest_title": "Φθηνότερο σουπερμάρκετ",
        "card_cheapest_desc": "Τεράστιο κοινό καλάθι, υπολογισμένο σε κάθε αλυσίδα. →",
        "card_collusion_title": "Έλεγχος σύμπραξης",
        "card_collusion_desc": "Δείκτες ταύτισης & διασποράς τιμών. {n} ζεύγος/-η επισημάνθηκαν. →",
        "card_movement_title": "Παράλληλες μεταβολές",
        "card_movement_desc": "{n} ημερήσια στιγμιότυπα συλλέχθηκαν. →",
        "chart_basket": "Σύνολο καλαθιού ανά αλυσίδα με τον χρόνο",
        "chart_cheap": "Φθηνότερο καλάθι & μέγιστη υπερχρέωση",
        "chart_ident": "Προϊόντα με πανομοιότυπη τιμή (%)",
        "chart_cat": "Μέγεθος καταλόγου",
        "ax_basket_total": "€ σύνολο καλαθιού",
        "lbl_cheapest_total": "φθηνότερο σύνολο (€)",
        "lbl_max_overpay": "μέγιστη υπερχρέωση (%)",
        "lbl_pct_identical": "% πανομοιότυπων",
        "lbl_products": "προϊόντα",
        "banner_failed": "Η τελευταία εκτέλεση {date} δεν ολοκληρώθηκε",
        "banner_showing_last": "Εμφανίζεται παρακάτω η τελευταία επιτυχημένη εκτέλεση.",
        "no_success_title": "Η τελευταία εκτέλεση {date} απέτυχε και δεν υπάρχει προηγούμενη επιτυχημένη εκτέλεση ακόμη.",
        "api_outage_hint": "Συνήθως οφείλεται σε προσωρινή μη διαθεσιμότητα του εξωτερικού "
                           "API (api.posokanei.gov.gr). Η διαδικασία επαναλαμβάνεται στην "
                           "επόμενη ημερήσια εκτέλεση· δεν απαιτείται καμία ενέργεια.",
        "see_logs": "δείτε τα logs",
        # empty
        "empty_title": "Δεν υπάρχουν δεδομένα ακόμη",
        "empty_desc": "Η ημερήσια διαδικασία δεν έχει παράξει εκτέλεση. Εκτελείται αυτόματα "
                      "μέσω cron, ή ξεκινήστε μία τώρα:",
        # runs
        "runs_title": "Ιστορικό εκτελέσεων",
        "th_date": "Ημ/νία", "th_status": "Κατάσταση", "th_catalogue": "Κατάλογος",
        "th_basket": "Καλάθι", "th_cheapest": "Φθηνότερη", "th_total": "Σύνολο",
        "th_overpay": "Υπερχρέωση", "th_snaps": "Στιγμ.", "th_duration": "Διάρκεια",
        "th_reports": "Αναφορές", "th_notes": "Σημειώσεις",
        "link_basket": "καλάθι", "link_collusion": "σύμπραξη",
        "no_runs": "καμία εκτέλεση ακόμη",
        # movement
        "mv_title": "Έλεγχος παράλληλων μεταβολών",
        "mv_accum": "Συσσώρευση στιγμιότυπων — <strong>{n}</strong> έως τώρα, "
                    "απαιτούνται <strong>≥2 ημέρες</strong> για τη μέτρηση των <em>μεταβολών</em> τιμών.",
        "mv_desc": "Ένα μόνο στιγμιότυπο μπορεί να δείξει πανομοιότυπες τιμές αλλά δεν "
                   "ξεχωρίζει τη σύμπραξη από τη σύμπτωση. Μόλις υπάρξουν δύο ή περισσότερα "
                   "ημερήσια στιγμιότυπα, ο έλεγχος εξετάζει αν οι ανταγωνιστές αλλάζουν τιμές "
                   "συντονισμένα (συν-μεταβολή, ποσοστό ίδιας κατεύθυνσης, ηγεσία τιμών).",
        "mv_window": "Περίοδος {a} … {b} ({n} στιγμιότυπα).",
        # misc
        "report_unavailable": "Η αναφορά δεν είναι διαθέσιμη",
        "report_for": "για {date}",
        "report_run_first": "Εκτελέστε πρώτα τη διαδικασία:",
        "back_dashboard": "← Πίνακας",
        "not_found": "Δεν βρέθηκε",
    },
}


def pick_lang(q, cookie_header, accept_language):
    """Resolve language: ?lang= > cookie > Accept-Language > default."""
    val = (q.get("lang") or [None])[0]
    if val in LANGS:
        return val, True  # explicit -> remember it
    if cookie_header:
        for part in cookie_header.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "lang" and v in LANGS:
                return v, False
    al = (accept_language or "").lower()
    if "el" in al:
        return "el", False
    if "en" in al:
        return "en", False
    return DEFAULT_LANG, False


# --------------------------------------------------------------------------- #
# data access
# --------------------------------------------------------------------------- #
def query(sql, params=()):
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def latest_run():
    rows = query("SELECT * FROM runs ORDER BY run_date DESC LIMIT 1")
    return rows[0] if rows else None


def latest_ok_run():
    """Most recent run that actually produced data (so an upstream API outage —
    which records a failed run — doesn't blank out the dashboard)."""
    rows = query("SELECT * FROM runs WHERE status='ok' AND catalog_size IS NOT NULL "
                 "ORDER BY run_date DESC LIMIT 1")
    return rows[0] if rows else None


def trends():
    runs = query("SELECT * FROM runs ORDER BY run_date ASC")
    dates = [r["run_date"] for r in runs]
    hist = query("SELECT * FROM basket_history ORDER BY run_date ASC")
    by_ret = {}
    for h in hist:
        by_ret.setdefault(h["retailer"], {})[h["run_date"]] = h["total"]
    series = [{"retailer": r, "data": [by_ret[r].get(d) for d in dates]}
              for r in sorted(by_ret)]
    return {
        "dates": dates,
        "cheapest_total": [r["cheapest_total"] for r in runs],
        "max_overpay_pct": [r["max_overpay_pct"] for r in runs],
        "identical_share": [(r["identical_share"] or 0) * 100 for r in runs],
        "catalog_size": [r["catalog_size"] for r in runs],
        "basket_series": series,
    }


def report_path(kind, date=None):
    """Resolve a report file: archived per-run copy, or the latest live copy."""
    if date:
        p = os.path.join(RUNS_DIR, date, f"{kind}.html")
        return p if os.path.exists(p) else None
    live = {"cartel": os.path.join(DATA_DIR, "analysis", "report.html"),
            "basket": os.path.join(DATA_DIR, "basket", "report.html")}.get(kind)
    return live if live and os.path.exists(live) else None


# --------------------------------------------------------------------------- #
# HTML shell
# --------------------------------------------------------------------------- #
def lang_switch(lang, path):
    """EL | EN toggle that keeps you on the current page."""
    out = []
    for code in LANGS:
        label = code.upper()
        if code == lang:
            out.append(f'<span class="btn btn-sm btn-light disabled fw-bold">{label}</span>')
        else:
            out.append(f'<a class="btn btn-sm btn-outline-light" href="{path}?lang={code}">{label}</a>')
    return '<span class="ms-2">' + "".join(out) + '</span>'


def navbar(lang, path):
    """The app navigation bar. Self-contained (inline gradient style) so it can
    also be injected into the standalone report documents."""
    S = T[lang]
    return f"""<nav class="navbar navbar-dark mb-4" style="background:linear-gradient(135deg,#0d6efd,#6610f2)">
  <div class="container">
    <a class="navbar-brand" href="/"><i class="bi bi-graph-up-arrow"></i> PosoKanei Monitor</a>
    <div class="d-flex align-items-center flex-wrap gap-1">
      <a class="btn btn-sm btn-outline-light" href="/"><i class="bi bi-speedometer2"></i> {S['nav_dashboard']}</a>
      <a class="btn btn-sm btn-outline-light" href="/report/basket"><i class="bi bi-cart4"></i> {S['nav_cheapest']}</a>
      <a class="btn btn-sm btn-outline-light" href="/report/cartel"><i class="bi bi-shield-check"></i> {S['nav_collusion']}</a>
      <a class="btn btn-sm btn-outline-light" href="/report/timeseries"><i class="bi bi-activity"></i> {S['nav_movement']}</a>
      <a class="btn btn-sm btn-outline-light" href="/runs"><i class="bi bi-clock-history"></i> {S['nav_runs']}</a>
      {lang_switch(lang, path)}
    </div>
  </div>
</nav>"""


def page(title, body, lang, path):
    S = T[lang]
    return f"""<!doctype html>
<html lang="{S['html_lang']}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · PosoKanei Monitor</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
  body {{ background:#f5f6f8; }}
  .navbar {{ background:linear-gradient(135deg,#0d6efd,#6610f2); }}
  .card {{ border:0; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  .kpi {{ font-size:1.9rem; font-weight:700; line-height:1; }}
  .kpi-sub {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.05em; color:#6c757d; }}
</style>
</head><body>
{navbar(lang, path)}
<div class="container pb-5">{body}</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
</body></html>"""


def empty_state(lang, path):
    S = T[lang]
    return page(S["nav_dashboard"], f"""
    <div class="card"><div class="card-body text-center py-5">
      <h1 class="h4"><i class="bi bi-hourglass-split"></i> {S['empty_title']}</h1>
      <p class="text-muted">{S['empty_desc']}</p>
      <pre class="bg-light p-3 d-inline-block text-start rounded">python3 orchestrate.py</pre>
    </div></div>""", lang, path)


def dashboard(lang, path):
    S = T[lang]
    last = latest_run()
    run = latest_ok_run()
    if not run and not last:
        return empty_state(lang, path)
    if not run:
        return page(S["nav_dashboard"], f"""
        <div class="alert alert-danger"><i class="bi bi-exclamation-octagon"></i>
          {S['no_success_title'].format(date=last['run_date'])}<br>
          <span class="small">{html.escape(str(last['notes'] or S['see_logs']))}</span></div>
        <p class="text-muted">{S['api_outage_hint']}</p>""", lang, path)

    verdict_pct = run["max_overpay_pct"] or 0
    banner = ""
    if last and last["run_date"] != run["run_date"]:
        extra = " — " + html.escape(str(last["notes"])) if last["notes"] else ""
        banner = (f'<div class="alert alert-warning d-flex align-items-center mb-4">'
                  f'<i class="bi bi-exclamation-triangle me-2"></i><div>'
                  f'{S["banner_failed"].format(date=last["run_date"])}{extra}. '
                  f'{S["banner_showing_last"]}</div></div>')
    elif run["notes"]:
        banner = (f'<div class="alert alert-warning py-2 small mb-4">'
                  f'<i class="bi bi-exclamation-triangle"></i> {html.escape(str(run["notes"]))}</div>')

    dur = f"{run['duration_s']:.0f}s" if run["duration_s"] else ""
    last_attempt = (f"· {S['last_attempt']} {last['run_date']} ✗"
                    if last and last["run_date"] != run["run_date"] else "")
    chart_labels = json.dumps({
        "axBasketTotal": S["ax_basket_total"],
        "cheapestTotal": S["lbl_cheapest_total"],
        "maxOverpay": S["lbl_max_overpay"],
        "pctIdentical": S["lbl_pct_identical"],
        "products": S["lbl_products"],
    }, ensure_ascii=False)

    body = f"""
    {banner}
    <div class="d-flex justify-content-between align-items-end mb-3">
      <h1 class="h3 mb-0">{S['dash_title']}</h1>
      <span class="text-muted small">{S['data_from']} <strong>{run['run_date']}</strong>
        · {S['status']} <span class="badge text-bg-{'success' if run['status']=='ok' else 'danger'}">{run['status']}</span>
        · {dur} {last_attempt}</span>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-6 col-lg-3"><div class="card"><div class="card-body">
        <div class="kpi text-success">{html.escape(str(run['cheapest'] or '—'))}</div>
        <div class="kpi-sub">{S['kpi_cheapest']}</div></div></div></div>
      <div class="col-6 col-lg-3"><div class="card"><div class="card-body">
        <div class="kpi">€{run['cheapest_total'] or 0:,.0f}</div>
        <div class="kpi-sub">{S['kpi_basket_total'].format(n=run['basket_size'] or 0)}</div></div></div></div>
      <div class="col-6 col-lg-3"><div class="card"><div class="card-body">
        <div class="kpi">+{verdict_pct:.1f}%</div>
        <div class="kpi-sub">{S['kpi_overpay']}</div></div></div></div>
      <div class="col-6 col-lg-3"><div class="card"><div class="card-body">
        <div class="kpi">{(run['identical_share'] or 0)*100:.1f}%</div>
        <div class="kpi-sub">{S['kpi_identical']}</div></div></div></div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-lg-4"><a class="text-decoration-none" href="/report/basket">
        <div class="card h-100"><div class="card-body">
          <h2 class="h6"><i class="bi bi-cart4 text-primary"></i> {S['card_cheapest_title']}</h2>
          <p class="text-muted small mb-0">{S['card_cheapest_desc']}</p>
        </div></div></a></div>
      <div class="col-lg-4"><a class="text-decoration-none" href="/report/cartel">
        <div class="card h-100"><div class="card-body">
          <h2 class="h6"><i class="bi bi-shield-check text-primary"></i> {S['card_collusion_title']}</h2>
          <p class="text-muted small mb-0">{S['card_collusion_desc'].format(n=run['suspicious_pairs'] or 0)}</p>
        </div></div></a></div>
      <div class="col-lg-4"><a class="text-decoration-none" href="/report/timeseries">
        <div class="card h-100"><div class="card-body">
          <h2 class="h6"><i class="bi bi-activity text-primary"></i> {S['card_movement_title']}</h2>
          <p class="text-muted small mb-0">{S['card_movement_desc'].format(n=run['snapshots_count'] or 0)}</p>
        </div></div></a></div>
    </div>

    <div class="row g-3">
      <div class="col-lg-6"><div class="card"><div class="card-body">
        <h2 class="h6 mb-3">{S['chart_basket']}</h2>
        <canvas id="basketChart" height="160"></canvas></div></div></div>
      <div class="col-lg-6"><div class="card"><div class="card-body">
        <h2 class="h6 mb-3">{S['chart_cheap']}</h2>
        <canvas id="cheapChart" height="160"></canvas></div></div></div>
      <div class="col-lg-6"><div class="card"><div class="card-body">
        <h2 class="h6 mb-3">{S['chart_ident']}</h2>
        <canvas id="identChart" height="160"></canvas></div></div></div>
      <div class="col-lg-6"><div class="card"><div class="card-body">
        <h2 class="h6 mb-3">{S['chart_cat']}</h2>
        <canvas id="catChart" height="160"></canvas></div></div></div>
    </div>

    <script>
    const L = {chart_labels};
    fetch('/api/trends').then(r=>r.json()).then(D=>{{
      const palette=['#0d6efd','#198754','#dc3545','#fd7e14','#6610f2','#20c997','#d63384','#ffc107'];
      new Chart(basketChart,{{type:'line',data:{{labels:D.dates,datasets:D.basket_series.map((s,i)=>({{
        label:s.retailer,data:s.data,borderColor:palette[i%palette.length],
        backgroundColor:'transparent',tension:.25,spanGaps:true}}))}},
        options:{{plugins:{{legend:{{position:'bottom',labels:{{boxWidth:10,font:{{size:10}}}}}}}},
        scales:{{y:{{title:{{display:true,text:L.axBasketTotal}}}}}}}}}});
      new Chart(cheapChart,{{data:{{labels:D.dates,datasets:[
        {{type:'line',label:L.cheapestTotal,data:D.cheapest_total,borderColor:'#198754',backgroundColor:'transparent',yAxisID:'y',tension:.25}},
        {{type:'bar',label:L.maxOverpay,data:D.max_overpay_pct,backgroundColor:'rgba(220,53,69,.4)',yAxisID:'y1'}}]}},
        options:{{plugins:{{legend:{{position:'bottom'}}}},scales:{{
          y:{{position:'left',title:{{display:true,text:'€'}}}},
          y1:{{position:'right',grid:{{drawOnChartArea:false}},title:{{display:true,text:'%'}}}}}}}}}});
      new Chart(identChart,{{type:'line',data:{{labels:D.dates,datasets:[{{
        label:L.pctIdentical,data:D.identical_share,borderColor:'#fd7e14',
        backgroundColor:'rgba(253,126,20,.12)',fill:true,tension:.25}}]}},
        options:{{plugins:{{legend:{{display:false}}}}}}}});
      new Chart(catChart,{{type:'line',data:{{labels:D.dates,datasets:[{{
        label:L.products,data:D.catalog_size,borderColor:'#0d6efd',
        backgroundColor:'rgba(13,110,253,.12)',fill:true,tension:.25}}]}},
        options:{{plugins:{{legend:{{display:false}}}}}}}});
    }});
    </script>
    """
    return page(S["dash_title"], body, lang, path)


def runs_page(lang, path):
    S = T[lang]
    runs = query("SELECT * FROM runs ORDER BY run_date DESC")
    rows = []
    for r in runs:
        badge = "success" if r["status"] == "ok" else "danger"
        dur = f"{r['duration_s']:.0f}s" if r["duration_s"] else "—"
        rows.append(
            f"<tr><td>{r['run_date']}</td>"
            f"<td><span class='badge text-bg-{badge}'>{r['status']}</span></td>"
            f"<td class='text-end'>{r['catalog_size'] or '—'}</td>"
            f"<td class='text-end'>{r['basket_size'] or '—'}</td>"
            f"<td>{html.escape(str(r['cheapest'] or '—'))}</td>"
            f"<td class='text-end'>€{r['cheapest_total'] or 0:,.0f}</td>"
            f"<td class='text-end'>+{r['max_overpay_pct'] or 0:.1f}%</td>"
            f"<td class='text-end'>{r['snapshots_count'] or 0}</td>"
            f"<td class='text-end'>{dur}</td>"
            f"<td><a href='/report/basket?date={r['run_date']}'>{S['link_basket']}</a> · "
            f"<a href='/report/cartel?date={r['run_date']}'>{S['link_collusion']}</a></td>"
            f"<td class='small text-muted'>{html.escape(str(r['notes'] or ''))}</td></tr>")
    body = f"""
    <h1 class="h3 mb-3">{S['runs_title']}</h1>
    <div class="card"><div class="card-body table-responsive">
      <table class="table table-sm table-hover align-middle mb-0">
        <thead><tr><th>{S['th_date']}</th><th>{S['th_status']}</th><th class="text-end">{S['th_catalogue']}</th>
          <th class="text-end">{S['th_basket']}</th><th>{S['th_cheapest']}</th><th class="text-end">{S['th_total']}</th>
          <th class="text-end">{S['th_overpay']}</th><th class="text-end">{S['th_snaps']}</th>
          <th class="text-end">{S['th_duration']}</th><th>{S['th_reports']}</th><th>{S['th_notes']}</th></tr></thead>
        <tbody>{''.join(rows) or f'<tr><td colspan=11 class=text-center>{S["no_runs"]}</td></tr>'}</tbody>
      </table>
    </div></div>"""
    return page(S["runs_title"], body, lang, path)


def timeseries_page(lang, path):
    S = T[lang]
    p = os.path.join(DATA_DIR, "analysis_ts", "ts_summary.json")
    data = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
    snaps = data.get("snapshots", []) or []
    run = latest_run()
    have = run["snapshots_count"] if run else 0
    if len(snaps) < 2:
        body = f"""
        <h1 class="h3 mb-3">{S['mv_title']}</h1>
        <div class="card"><div class="card-body">
          <div class="alert alert-info"><i class="bi bi-hourglass-split"></i>
            {S['mv_accum'].format(n=have)}</div>
          <p class="text-muted mb-0">{S['mv_desc']}</p>
        </div></div>"""
        return page(S["mv_title"], body, lang, path)
    market = data.get("market", {})
    body = f"""
    <h1 class="h3 mb-3">{S['mv_title']}</h1>
    <p class="text-muted">{S['mv_window'].format(a=snaps[0], b=snaps[-1], n=len(snaps))}</p>
    <div class="card"><div class="card-body">
      <pre class="mb-0 small">{html.escape(json.dumps(market, indent=2, ensure_ascii=False))}</pre>
    </div></div>"""
    return page(S["mv_title"], body, lang, path)


def report_view(kind, date, lang, path):
    """Serve a generated report (a standalone HTML file) with the app navbar
    injected straight into its document — no iframe, so a reverse proxy's
    X-Frame-Options header can't block it. Returns (html, status)."""
    S = T[lang]
    title = S["nav_cheapest"] if kind == "basket" else S["nav_collusion"]
    fp = report_path(kind, date)
    if not fp:
        forwhom = " " + S["report_for"].format(date=html.escape(date)) if date else ""
        body = (f'<div class="card"><div class="card-body text-center py-5">'
                f'<h1 class="h4">{S["report_unavailable"]}{forwhom}</h1>'
                f'<p class="text-muted">{S["report_run_first"]} <code>python3 orchestrate.py</code></p>'
                f'<a class="btn btn-primary" href="/">{S["back_dashboard"]}</a></div></div>')
        return page(title, body, lang, path), 404
    with open(fp, encoding="utf-8") as f:
        doc = f.read()
    # Inject the navbar right after the report's <body> tag (its <head> already
    # loads Bootstrap, so the bar is styled; the bar carries its own gradient).
    nav = navbar(lang, path)
    new_doc, n = re.subn(r"(<body[^>]*>)", lambda m: m.group(1) + "\n" + nav, doc, count=1)
    if n == 0:  # no <body> found — fall back to prepending
        new_doc = nav + doc
    return new_doc, 200


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "PosoKaneiMonitor/1.0"

    def _send(self, body, ctype="text/html; charset=utf-8", code=200, set_lang=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if set_lang:
            self.send_header("Set-Cookie", f"lang={set_lang}; Path=/; Max-Age=31536000; SameSite=Lax")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        date = (q.get("date") or [None])[0]
        lang, explicit = pick_lang(q, self.headers.get("Cookie"),
                                   self.headers.get("Accept-Language"))
        set_lang = lang if explicit else None
        try:
            if path == "/":
                self._send(dashboard(lang, path), set_lang=set_lang)
            elif path == "/runs":
                self._send(runs_page(lang, path), set_lang=set_lang)
            elif path == "/report/timeseries":
                self._send(timeseries_page(lang, path), set_lang=set_lang)
            elif path in ("/report/cartel", "/report/basket"):
                kind = path.rsplit("/", 1)[1]
                html_out, code = report_view(kind, date, lang, path)
                self._send(html_out, code=code, set_lang=set_lang)
            elif path in ("/report/cartel/raw", "/report/basket/raw"):
                kind = path.split("/")[2]
                fp = report_path(kind, date)
                if fp:
                    with open(fp, "rb") as f:
                        self._send(f.read())
                else:
                    self._send("<!doctype html><p style='font:14px sans-serif;padding:2rem'>"
                               "report not available</p>", code=404)
            elif path == "/api/trends":
                self._send(json.dumps(trends()), "application/json")
            elif path == "/api/runs":
                self._send(json.dumps(query("SELECT * FROM runs ORDER BY run_date DESC")),
                           "application/json")
            elif path == "/healthz":
                self._send(json.dumps({"ok": True, "db": os.path.exists(DB_PATH)}),
                           "application/json")
            else:
                S = T[lang]
                self._send(page(S["not_found"], f"<h1 class='h4'>404 — {S['not_found']}</h1>",
                                lang, path), code=404, set_lang=set_lang)
        except BrokenPipeError:
            pass
        except Exception as e:  # never 500 the whole server
            self._send(page("Error", f"<pre>{html.escape(repr(e))}</pre>", DEFAULT_LANG, path),
                       code=500)

    def log_message(self, fmt, *a):  # quieter logs
        print(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] "
              f"{self.address_string()} {fmt % a}", flush=True)


def main():
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"PosoKanei Monitor on http://0.0.0.0:{PORT}  (DB: {DB_PATH})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
