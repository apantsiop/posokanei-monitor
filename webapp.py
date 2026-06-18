#!/usr/bin/env python3
"""
webapp.py — the UI. A zero-dependency web app (stdlib http.server) that surfaces
everything the daily orchestrator produces.

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
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "posokanei_data")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
DB_PATH = os.path.join(DATA_DIR, "posokanei.db")
PORT = int(os.environ.get("PORT", "8000"))


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


def trends():
    runs = query("SELECT * FROM runs ORDER BY run_date ASC")
    dates = [r["run_date"] for r in runs]
    hist = query("SELECT * FROM basket_history ORDER BY run_date ASC")
    # per-retailer total over time
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
def page(title, body):
    return f"""<!doctype html>
<html lang="en"><head>
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
<nav class="navbar navbar-dark mb-4">
  <div class="container">
    <a class="navbar-brand" href="/"><i class="bi bi-graph-up-arrow"></i> PosoKanei Monitor</a>
    <div>
      <a class="btn btn-sm btn-outline-light" href="/"><i class="bi bi-speedometer2"></i> Dashboard</a>
      <a class="btn btn-sm btn-outline-light" href="/report/basket"><i class="bi bi-cart4"></i> Cheapest</a>
      <a class="btn btn-sm btn-outline-light" href="/report/cartel"><i class="bi bi-shield-check"></i> Collusion</a>
      <a class="btn btn-sm btn-outline-light" href="/report/timeseries"><i class="bi bi-activity"></i> Movement</a>
      <a class="btn btn-sm btn-outline-light" href="/runs"><i class="bi bi-clock-history"></i> Runs</a>
    </div>
  </div>
</nav>
<div class="container pb-5">{body}</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
</body></html>"""


def empty_state():
    return page("Dashboard", """
    <div class="card"><div class="card-body text-center py-5">
      <h1 class="h4"><i class="bi bi-hourglass-split"></i> No data yet</h1>
      <p class="text-muted">The daily pipeline hasn't produced a run.
         It runs automatically via cron, or trigger one now:</p>
      <pre class="bg-light p-3 d-inline-block text-start rounded">python3 orchestrate.py</pre>
    </div></div>""")


def dashboard():
    run = latest_run()
    if not run:
        return empty_state()
    verdict_pct = run["max_overpay_pct"] or 0
    notes = run["notes"]
    note_html = (f'<div class="alert alert-warning py-2 small mb-4">'
                 f'<i class="bi bi-exclamation-triangle"></i> {html.escape(notes)}</div>'
                 if notes else "")
    body = f"""
    {note_html}
    <div class="d-flex justify-content-between align-items-end mb-3">
      <h1 class="h3 mb-0">Daily price monitor</h1>
      <span class="text-muted small">latest run <strong>{run['run_date']}</strong>
        · status <span class="badge text-bg-{'success' if run['status']=='ok' else 'danger'}">{run['status']}</span>
        · {run['duration_s'] and f"{run['duration_s']:.0f}s"} </span>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-6 col-lg-3"><div class="card"><div class="card-body">
        <div class="kpi text-success">{html.escape(str(run['cheapest'] or '—'))}</div>
        <div class="kpi-sub">cheapest chain</div></div></div></div>
      <div class="col-6 col-lg-3"><div class="card"><div class="card-body">
        <div class="kpi">€{run['cheapest_total'] or 0:,.0f}</div>
        <div class="kpi-sub">basket total ({run['basket_size'] or 0:,} items)</div></div></div></div>
      <div class="col-6 col-lg-3"><div class="card"><div class="card-body">
        <div class="kpi">+{verdict_pct:.1f}%</div>
        <div class="kpi-sub">priciest pays more</div></div></div></div>
      <div class="col-6 col-lg-3"><div class="card"><div class="card-body">
        <div class="kpi">{(run['identical_share'] or 0)*100:.1f}%</div>
        <div class="kpi-sub">identically-priced products</div></div></div></div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-lg-4"><a class="text-decoration-none" href="/report/basket">
        <div class="card h-100"><div class="card-body">
          <h2 class="h6"><i class="bi bi-cart4 text-primary"></i> Cheapest supermarket</h2>
          <p class="text-muted small mb-0">Giant common basket rung up at every chain. →</p>
        </div></div></a></div>
      <div class="col-lg-4"><a class="text-decoration-none" href="/report/cartel">
        <div class="card h-100"><div class="card-body">
          <h2 class="h6"><i class="bi bi-shield-check text-primary"></i> Collusion screen</h2>
          <p class="text-muted small mb-0">Price-identity & dispersion markers. {run['suspicious_pairs'] or 0} pair(s) flagged. →</p>
        </div></div></a></div>
      <div class="col-lg-4"><a class="text-decoration-none" href="/report/timeseries">
        <div class="card h-100"><div class="card-body">
          <h2 class="h6"><i class="bi bi-activity text-primary"></i> Parallel movement</h2>
          <p class="text-muted small mb-0">{run['snapshots_count'] or 0} daily snapshots collected. →</p>
        </div></div></a></div>
    </div>

    <div class="row g-3">
      <div class="col-lg-6"><div class="card"><div class="card-body">
        <h2 class="h6 mb-3">Basket total per retailer over time</h2>
        <canvas id="basketChart" height="160"></canvas></div></div></div>
      <div class="col-lg-6"><div class="card"><div class="card-body">
        <h2 class="h6 mb-3">Cheapest basket &amp; max overpay</h2>
        <canvas id="cheapChart" height="160"></canvas></div></div></div>
      <div class="col-lg-6"><div class="card"><div class="card-body">
        <h2 class="h6 mb-3">Identically-priced products (%)</h2>
        <canvas id="identChart" height="160"></canvas></div></div></div>
      <div class="col-lg-6"><div class="card"><div class="card-body">
        <h2 class="h6 mb-3">Catalogue size</h2>
        <canvas id="catChart" height="160"></canvas></div></div></div>
    </div>

    <script>
    fetch('/api/trends').then(r=>r.json()).then(D=>{{
      const palette=['#0d6efd','#198754','#dc3545','#fd7e14','#6610f2','#20c997','#d63384','#ffc107'];
      new Chart(basketChart,{{type:'line',data:{{labels:D.dates,datasets:D.basket_series.map((s,i)=>({{
        label:s.retailer,data:s.data,borderColor:palette[i%palette.length],
        backgroundColor:'transparent',tension:.25,spanGaps:true}}))}},
        options:{{plugins:{{legend:{{position:'bottom',labels:{{boxWidth:10,font:{{size:10}}}}}}}},
        scales:{{y:{{title:{{display:true,text:'€ basket total'}}}}}}}}}});
      new Chart(cheapChart,{{data:{{labels:D.dates,datasets:[
        {{type:'line',label:'cheapest total (€)',data:D.cheapest_total,borderColor:'#198754',backgroundColor:'transparent',yAxisID:'y',tension:.25}},
        {{type:'bar',label:'max overpay (%)',data:D.max_overpay_pct,backgroundColor:'rgba(220,53,69,.4)',yAxisID:'y1'}}]}},
        options:{{plugins:{{legend:{{position:'bottom'}}}},scales:{{
          y:{{position:'left',title:{{display:true,text:'€'}}}},
          y1:{{position:'right',grid:{{drawOnChartArea:false}},title:{{display:true,text:'%'}}}}}}}}}});
      new Chart(identChart,{{type:'line',data:{{labels:D.dates,datasets:[{{
        label:'% identical',data:D.identical_share,borderColor:'#fd7e14',
        backgroundColor:'rgba(253,126,20,.12)',fill:true,tension:.25}}]}},
        options:{{plugins:{{legend:{{display:false}}}}}}}});
      new Chart(catChart,{{type:'line',data:{{labels:D.dates,datasets:[{{
        label:'products',data:D.catalog_size,borderColor:'#0d6efd',
        backgroundColor:'rgba(13,110,253,.12)',fill:true,tension:.25}}]}},
        options:{{plugins:{{legend:{{display:false}}}}}}}});
    }});
    </script>
    """
    return page("Dashboard", body)


def runs_page():
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
            f"<td><a href='/report/basket?date={r['run_date']}'>basket</a> · "
            f"<a href='/report/cartel?date={r['run_date']}'>collusion</a></td>"
            f"<td class='small text-muted'>{html.escape(str(r['notes'] or ''))}</td></tr>")
    body = f"""
    <h1 class="h3 mb-3">Run history</h1>
    <div class="card"><div class="card-body table-responsive">
      <table class="table table-sm table-hover align-middle mb-0">
        <thead><tr><th>Date</th><th>Status</th><th class="text-end">Catalogue</th>
          <th class="text-end">Basket</th><th>Cheapest</th><th class="text-end">Total</th>
          <th class="text-end">Overpay</th><th class="text-end">Snaps</th>
          <th class="text-end">Duration</th><th>Reports</th><th>Notes</th></tr></thead>
        <tbody>{''.join(rows) or '<tr><td colspan=11 class=text-center>no runs yet</td></tr>'}</tbody>
      </table>
    </div></div>"""
    return page("Runs", body)


def timeseries_page():
    # prefer latest live summary, fall back to archived
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
        <h1 class="h3 mb-3">Parallel-movement screen</h1>
        <div class="card"><div class="card-body">
          <div class="alert alert-info"><i class="bi bi-hourglass-split"></i>
            Accumulating snapshots — <strong>{have}</strong> collected so far,
            need <strong>≥2 days</strong> to measure price <em>movements</em>.</div>
          <p class="text-muted mb-0">A single snapshot can show identical prices but
            cannot distinguish coordination from coincidence. Once two or more daily
            snapshots exist, this screen tests whether competitors change prices in
            lockstep (co-movement lift, same-direction rate, price leadership).</p>
        </div></div>"""
        return page("Movement", body)
    market = data.get("market", {})
    body = f"""
    <h1 class="h3 mb-3">Parallel-movement screen</h1>
    <p class="text-muted">Window {snaps[0]} … {snaps[-1]} ({len(snaps)} snapshots).</p>
    <div class="card"><div class="card-body">
      <pre class="mb-0 small">{html.escape(json.dumps(market, indent=2, ensure_ascii=False))}</pre>
    </div></div>"""
    return page("Movement", body)


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "PosoKaneiMonitor/1.0"

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
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
        try:
            if path == "/":
                self._send(dashboard())
            elif path == "/runs":
                self._send(runs_page())
            elif path == "/report/timeseries":
                self._send(timeseries_page())
            elif path in ("/report/cartel", "/report/basket"):
                kind = path.rsplit("/", 1)[1]
                fp = report_path(kind, date)
                if fp:
                    with open(fp, "rb") as f:
                        self._send(f.read())
                else:
                    self._send(page("Not ready", f"""
                      <div class="card"><div class="card-body text-center py-5">
                      <h1 class="h4">Report not available{f' for {html.escape(date)}' if date else ''}</h1>
                      <p class="text-muted">Run the pipeline first: <code>python3 orchestrate.py</code></p>
                      <a class="btn btn-primary" href="/">← Dashboard</a></div></div>"""), code=404)
            elif path == "/api/trends":
                self._send(json.dumps(trends()), "application/json")
            elif path == "/api/runs":
                self._send(json.dumps(query("SELECT * FROM runs ORDER BY run_date DESC")),
                           "application/json")
            elif path == "/healthz":
                self._send(json.dumps({"ok": True, "db": os.path.exists(DB_PATH)}),
                           "application/json")
            else:
                self._send(page("Not found", "<h1 class='h4'>404</h1>"), code=404)
        except BrokenPipeError:
            pass
        except Exception as e:  # never 500 the whole server
            self._send(page("Error", f"<pre>{html.escape(repr(e))}</pre>"), code=500)

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
