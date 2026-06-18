#!/usr/bin/env python3
"""
posokanei_timeseries.py — Time-series collusion screen for PosoKanei.

This is the screen that actually distinguishes COORDINATION from COINCIDENCE.
A static snapshot only shows that prices are similar; that is explained just as
well by MSRP, resale-price maintenance, or open price-matching. What cartels
leave behind is a DYNAMIC fingerprint: rivals change the same product's price
at the same time, in the same direction, often by the same amount — far more
often than independent firms would.

Input:  posokanei_data/snapshots/*.csv   (from posokanei_snapshot.py, >=2 days)
Output: console report + posokanei_data/analysis_ts/*.csv + ts_summary.json

Screens implemented:
  A. Co-movement LIFT per retailer pair
       observed joint same-interval price changes / expected-under-independence.
       Lift ~1 = independent. Lift >> 1 = prices move together (suspicious).
  B. Same-DIRECTION rate among joint changes
       random sign -> ~50%. Coordinated up/down moves -> well above 50%.
  C. LOCKSTEP magnitude
       share of joint changes that land on the identical new price or move by
       the identical delta — a hallmark of explicit coordination, not matching.
  D. Price LEADERSHIP (lead-lag)
       when A moves a SKU and B follows it to the same level next interval (and
       the reverse). A strong asymmetry names a likely price leader.

Statistics are computed by hand (Python standard library only). Counts are
pooled across all shared products per retailer pair; a pair is only judged once
it clears MIN_OPPORTUNITIES joint change-opportunities.

================================  CAVEAT  ===================================
Lift > 1 and high co-movement are NECESSARY but not SUFFICIENT for collusion.
Common-cost shocks (a supplier raises wholesale prices; fuel/energy; FX; a VAT
change) move all rivals together legitimately. Commonly-owned banners share a
pricing backend. Use these flags to scope an investigation, request internal
records, and test against cost data — never as a standalone verdict.
=============================================================================
"""

import argparse
import csv
import glob
import json
import os
import statistics
from collections import defaultdict
from itertools import combinations

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posokanei_data")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
OUT_DIR = os.path.join(DATA_DIR, "analysis_ts")

# Treat two prices as equal within half a cent (float / rounding tolerance).
EPS = 0.005
# A price change counts only if it moves more than this fraction (ignore noise).
MIN_REL_MOVE = 0.005          # 0.5%
# A pair is judged only above this many joint change-opportunities.
MIN_OPPORTUNITIES = 30


# --------------------------------------------------------------------------- #
# Load snapshots -> series[(product_id, retailer)] = [(date, price), ...]
# --------------------------------------------------------------------------- #
def load_snapshots(snap_dir):
    files = sorted(glob.glob(os.path.join(snap_dir, "*.csv")))
    series = defaultdict(dict)        # (pid, ret) -> {date: price}
    meta = {}                         # pid -> (name, brand, category)
    dates = []
    for path in files:
        date = os.path.splitext(os.path.basename(path))[0]
        dates.append(date)
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    price = float(row["price"])
                except (TypeError, ValueError, KeyError):
                    continue
                if price <= 0:
                    continue
                pid, ret = row.get("product_id"), row.get("retailer")
                if not pid or not ret:
                    continue
                series[(pid, ret)][date] = price
                if pid not in meta:
                    meta[pid] = (row.get("name"), row.get("brand"), row.get("category"))
    return dates, series, meta


def sign(x):
    if x > EPS:
        return 1
    if x < -EPS:
        return -1
    return 0


def changed(prev, cur):
    """Did the price move by more than noise? Returns (moved, direction, delta)."""
    delta = cur - prev
    if prev > 0 and abs(delta) / prev > MIN_REL_MOVE and abs(delta) > EPS:
        return True, sign(delta), delta
    return False, 0, delta


# --------------------------------------------------------------------------- #
# Build per-key change events indexed by interval (the date the change lands on)
# --------------------------------------------------------------------------- #
def build_change_events(dates, series):
    """events[(pid, ret)] = {date_i: (moved, direction, new_price, delta)} for
    each interval (date_{i-1} -> date_i) where BOTH endpoints are observed."""
    events = {}
    for key, bydate in series.items():
        ev = {}
        prev_date = None
        for d in dates:
            if d not in bydate:
                prev_date = None      # gap; can't define a change across it
                continue
            if prev_date is not None:
                moved, direction, delta = changed(bydate[prev_date], bydate[d])
                ev[d] = (moved, direction, bydate[d], delta)
            prev_date = d
        if ev:
            events[key] = ev
    return events


# --------------------------------------------------------------------------- #
# Pairwise co-movement / direction / lockstep / leadership
# --------------------------------------------------------------------------- #
def pairwise_dynamics(dates, series, events, min_opps=MIN_OPPORTUNITIES):
    # index events by product -> retailer -> {date: event}
    by_product = defaultdict(dict)
    for (pid, ret), ev in events.items():
        by_product[pid][ret] = ev

    pair = defaultdict(lambda: {
        "opps": 0, "a_chg": 0, "b_chg": 0, "joint": 0,
        "same_dir": 0, "lockstep_price": 0, "lockstep_delta": 0,
        "a_leads": 0, "b_leads": 0,
    })

    date_index = {d: i for i, d in enumerate(dates)}

    for pid, rets in by_product.items():
        ret_list = sorted(rets)
        for a, b in combinations(ret_list, 2):
            ea, eb = rets[a], rets[b]
            common_dates = set(ea) & set(eb)
            acc = pair[(a, b)]
            for d in common_dates:
                ma, da, pa, dela = ea[d]
                mb, db, pb, delb = eb[d]
                acc["opps"] += 1
                acc["a_chg"] += 1 if ma else 0
                acc["b_chg"] += 1 if mb else 0
                if ma and mb:
                    acc["joint"] += 1
                    if da == db and da != 0:
                        acc["same_dir"] += 1
                    if abs(pa - pb) <= EPS:
                        acc["lockstep_price"] += 1
                    if abs(dela - delb) <= EPS:
                        acc["lockstep_delta"] += 1
            # leadership: A moves at d, B unchanged at d but matches A's new
            # price at the next interval (and vice versa).
            for d in set(ea) & set(eb):
                i = date_index[d]
                if i + 1 >= len(dates):
                    continue
                nd = dates[i + 1]
                ma, _, pa, _ = ea[d]
                mb, _, pb, _ = eb[d]
                # A leads: A changed now, B not yet; next interval B moves to ~A's price
                if ma and not mb and nd in eb:
                    mb2, _, pb2, _ = eb[nd]
                    if mb2 and abs(pb2 - pa) <= max(EPS, 0.02 * pa):
                        acc["a_leads"] += 1
                if mb and not ma and nd in ea:
                    ma2, _, pa2, _ = ea[nd]
                    if ma2 and abs(pa2 - pb) <= max(EPS, 0.02 * pb):
                        acc["b_leads"] += 1

    rows = []
    for (a, b), c in pair.items():
        if c["opps"] < min_opps:
            continue
        opps = c["opps"]
        pa = c["a_chg"] / opps
        pb = c["b_chg"] / opps
        expected_joint = pa * pb * opps
        lift = (c["joint"] / expected_joint) if expected_joint > 0 else None
        joint = c["joint"]
        rows.append({
            "retailer_a": a, "retailer_b": b,
            "opportunities": opps,
            "a_change_rate": round(pa, 4),
            "b_change_rate": round(pb, 4),
            "joint_changes": joint,
            "expected_joint": round(expected_joint, 2),
            "comovement_lift": round(lift, 2) if lift is not None else None,
            "same_direction_rate": round(c["same_dir"] / joint, 4) if joint else None,
            "lockstep_price_rate": round(c["lockstep_price"] / joint, 4) if joint else None,
            "lockstep_delta_rate": round(c["lockstep_delta"] / joint, 4) if joint else None,
            "a_leads_b": c["a_leads"],
            "b_leads_a": c["b_leads"],
        })
    rows.sort(key=lambda r: (r["comovement_lift"] or 0), reverse=True)
    return rows


# --------------------------------------------------------------------------- #
# Market-wide change cadence (context for reading the pair numbers)
# --------------------------------------------------------------------------- #
def market_overview(events):
    total_intervals = sum(len(ev) for ev in events.values())
    total_changes = sum(1 for ev in events.values() for v in ev.values() if v[0])
    return {
        "keys_tracked": len(events),
        "observed_intervals": total_intervals,
        "price_changes": total_changes,
        "overall_change_rate": round(total_changes / total_intervals, 4) if total_intervals else None,
    }


def pct(x):
    return f"{100 * x:.1f}%" if x is not None else "n/a"


def write_csv(path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Time-series coordination screen.")
    ap.add_argument("--snapshots-dir", default=SNAP_DIR)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--min-opps", type=int, default=MIN_OPPORTUNITIES)
    args = ap.parse_args()
    min_opps = args.min_opps

    dates, series, meta = load_snapshots(args.snapshots_dir)
    print(f"Loaded {len(dates)} snapshot(s): {', '.join(dates) if dates else '(none)'}")
    if len(dates) < 2:
        print("\nNeed at least 2 daily snapshots to measure price MOVEMENTS.")
        print("Run:  python3 posokanei_snapshot.py   once per day, then re-run this.")
        print("(With one snapshot, use posokanei_cartel_screen.py for the static screen.)")
        return

    os.makedirs(args.out, exist_ok=True)
    events = build_change_events(dates, series)
    overview = market_overview(events)
    pairs = pairwise_dynamics(dates, series, events, min_opps)

    write_csv(os.path.join(args.out, "pair_dynamics.csv"), pairs,
              ["retailer_a", "retailer_b", "opportunities", "a_change_rate",
               "b_change_rate", "joint_changes", "expected_joint",
               "comovement_lift", "same_direction_rate", "lockstep_price_rate",
               "lockstep_delta_rate", "a_leads_b", "b_leads_a"])

    summary = {"snapshots": dates, "market": overview,
               "pairs_evaluated": len(pairs), "top_pairs": pairs[:15]}
    with open(os.path.join(args.out, "ts_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # ---- report ----
    print("=" * 74)
    print("PosoKanei TIME-SERIES COORDINATION SCREEN")
    print("=" * 74)
    print(f"Window: {dates[0]} .. {dates[-1]}  ({len(dates)} snapshots)")
    print(f"Tracked (product x retailer) series: {overview['keys_tracked']}")
    print(f"Observed change-intervals: {overview['observed_intervals']}  "
          f"| price changes: {overview['price_changes']}  "
          f"| market change rate: {pct(overview['overall_change_rate'])}\n")

    if not pairs:
        print(f"No retailer pair reached {min_opps} joint opportunities yet.")
        print("Collect more daily snapshots and re-run.")
        return

    print(f"PAIRWISE DYNAMICS (pairs with >= {min_opps} opportunities), "
          f"ranked by co-movement lift")
    print(f"  {'A':<14}{'B':<14}{'opps':>6}{'joint':>7}{'lift':>7}"
          f"{'sameDir':>9}{'lockPx':>8}{'lead A/B':>10}")
    for r in pairs[:15]:
        print(f"  {r['retailer_a']:<14}{r['retailer_b']:<14}"
              f"{r['opportunities']:>6}{r['joint_changes']:>7}"
              f"{(r['comovement_lift'] if r['comovement_lift'] is not None else 0):>7}"
              f"{pct(r['same_direction_rate']):>9}{pct(r['lockstep_price_rate']):>8}"
              f"{str(r['a_leads_b'])+'/'+str(r['b_leads_a']):>10}")

    print("\n" + "=" * 74)
    print("HEURISTIC READING (flags scope an investigation, they do not convict)")
    print("=" * 74)
    flags = []
    for r in pairs:
        reasons = []
        if r["comovement_lift"] and r["comovement_lift"] >= 2.0 and r["joint_changes"] >= 10:
            reasons.append(f"lift {r['comovement_lift']}x")
        if r["same_direction_rate"] and r["same_direction_rate"] >= 0.8 and r["joint_changes"] >= 10:
            reasons.append(f"{pct(r['same_direction_rate'])} same-direction")
        if r["lockstep_price_rate"] and r["lockstep_price_rate"] >= 0.5 and r["joint_changes"] >= 10:
            reasons.append(f"{pct(r['lockstep_price_rate'])} identical new price")
        lead_tot = r["a_leads_b"] + r["b_leads_a"]
        if lead_tot >= 10 and max(r["a_leads_b"], r["b_leads_a"]) / lead_tot >= 0.8:
            leader = r["retailer_a"] if r["a_leads_b"] > r["b_leads_a"] else r["retailer_b"]
            reasons.append(f"price leader: {leader}")
        if reasons:
            flags.append((r["retailer_a"], r["retailer_b"], reasons))

    if flags:
        for a, b, reasons in flags:
            print(f"  [!] {a} <-> {b}: " + "; ".join(reasons))
    else:
        print("  No pair crossed the dynamic thresholds. Either competition is")
        print("  genuine, or more snapshots are needed to accumulate signal.")

    print("\n  Before concluding anything, rule out COMMON-COST shocks (wholesale,")
    print("  energy, FX, VAT) that move all rivals together lawfully, and check")
    print("  whether high-lift pairs are commonly-owned banners sharing a backend.")
    print(f"\n  Detail: {args.out}/pair_dynamics.csv, ts_summary.json")


if __name__ == "__main__":
    main()
