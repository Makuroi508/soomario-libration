"""
analyze_live.py — ground-truth analysis of real fills (no fill model).
══════════════════════════════════════════════════════════════════════
WHY THIS EXISTS
The backtest harness cannot answer the diversification question, because
Hyperliquid only retains ~5000 sub-4h candles (1m ~= 3.5d, 5m ~= 17d,
15m ~= 52d) and returns EMPTY for historical sub-4h windows. A 0.55%
trailing stop cannot be resolved on 4h bars (median 4h range 1.7-3.2%,
i.e. 3-6x the band), so exits are unsimulatable over any useful span.

Real fills need no fill model. They ARE the fills. This analyses the live
trade history directly to answer the two levers that the data can support:
coin selection, and NOTIONAL_FRAC.

  python backtest/analyze_live.py "../trade_history (1).csv"
"""
import csv
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HARD_STOP_THRESHOLD = -8.0     # % on notional; the live hard stop is -10%
TS_FMT = "%m/%d/%Y - %H:%M:%S"


def load_trips(path):
    """Pair Open->Close per coin chronologically into round-trips."""
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        r["ts"] = datetime.strptime(r["time"].strip(), TS_FMT)
        r["ntl"] = float(r["ntl"]); r["pnl"] = float(r["closedPnl"])
    rows.sort(key=lambda r: r["ts"])

    openq, trips = defaultdict(list), []
    for r in rows:
        if r["dir"].startswith("Open"):
            openq[r["coin"]].append(r)
        else:
            if not openq[r["coin"]]:
                continue                      # close without a matching open in-window
            o = openq[r["coin"]].pop(0)
            net = o["pnl"] + r["pnl"]         # open fee + close realized
            trips.append({"coin": r["coin"], "side": o["dir"].split()[-1],
                          "open_ts": o["ts"], "close_ts": r["ts"], "ntl": o["ntl"],
                          "net": net, "ret_pct": net / o["ntl"] * 100})
    return trips, rows


def concurrency(trips):
    """Sample open-position count on a 5-minute grid across the whole window."""
    if not trips:
        return []
    lo = min(t["open_ts"] for t in trips)
    hi = max(t["close_ts"] for t in trips)
    grid, cur = [], lo
    ev = sorted([(t["open_ts"], 1) for t in trips] + [(t["close_ts"], -1) for t in trips])
    i, n = 0, 0
    while cur <= hi:
        while i < len(ev) and ev[i][0] <= cur:
            n += ev[i][1]; i += 1
        grid.append(n)
        cur += timedelta(minutes=5)
    return grid


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "../trade_history (1).csv"
    trips, rows = load_trips(path)
    total = sum(t["net"] for t in trips)
    wins = [t for t in trips if t["net"] > 0]
    hard = [t for t in trips if t["ret_pct"] <= HARD_STOP_THRESHOLD]

    span = (max(r["ts"] for r in rows) - min(r["ts"] for r in rows)).total_seconds() / 86400
    print(f"═══ LIVE GROUND TRUTH — {len(rows)} fills, {len(trips)} round-trips, "
          f"{span:.1f} days ═══")
    print(f"  net realized   ${total:+.2f}   win rate {100*len(wins)/len(trips):.1f}%   "
          f"avg ${total/len(trips):+.3f}/trip  ({st.mean(t['ret_pct'] for t in trips):+.3f}%)")
    print(f"  hard stops     {len(hard)} trips  ${sum(t['net'] for t in hard):+.2f}   "
          f"(median {st.median([t['ret_pct'] for t in hard]):.1f}% on notional)")
    print(f"  non-stop trips {len(trips)-len(hard)}      "
          f"${total - sum(t['net'] for t in hard):+.2f}")
    print(f"  -> the tail is {abs(sum(t['net'] for t in hard))/ (total - sum(t['net'] for t in hard))*100:.0f}% "
          f"of gross winnings")

    grid = concurrency(trips)
    cap = 10
    print(f"\n  slot use: mean {st.mean(grid):.1f}/{cap}, median {st.median(grid):.0f}, "
          f"max {max(grid)}, full {100*sum(1 for g in grid if g>=cap)/len(grid):.0f}% of time, "
          f"2+ free {100*sum(1 for g in grid if g<=cap-2)/len(grid):.0f}%")

    # ── per coin ──
    per = defaultdict(lambda: {"n": 0, "net": 0.0, "w": 0, "hard": 0, "hard_pnl": 0.0,
                               "rets": []})
    for t in trips:
        d = per[t["coin"]]
        d["n"] += 1; d["net"] += t["net"]; d["w"] += t["net"] > 0
        d["rets"].append(t["ret_pct"])
        if t["ret_pct"] <= HARD_STOP_THRESHOLD:
            d["hard"] += 1; d["hard_pnl"] += t["net"]
    print(f"\n  {'coin':<10}{'trips':>6}{'win%':>6}{'hard':>5}{'stop $':>9}"
          f"{'net $':>9}{'avg %':>8}{'vol σ%':>8}")
    for c, d in sorted(per.items(), key=lambda kv: kv[1]["net"]):
        vol = st.pstdev(d["rets"]) if len(d["rets"]) > 1 else 0
        print(f"  {c:<10}{d['n']:>6}{100*d['w']/d['n']:>5.0f}%{d['hard']:>5}"
              f"{d['hard_pnl']:>9.2f}{d['net']:>9.2f}{st.mean(d['rets']):>8.2f}{vol:>8.2f}")

    # ── is the damage a generalizable VOLATILITY effect or coin-specific luck? ──
    xs = [(st.pstdev(d["rets"]), d["net"], c) for c, d in per.items() if d["n"] >= 5]
    if len(xs) >= 5:
        vols = [x[0] for x in xs]
        med = st.median(vols)
        lo_g = [x for x in xs if x[0] <= med]
        hi_g = [x for x in xs if x[0] > med]
        print(f"\n  ── volatility split (per-trip return σ, coins with >=5 trips) ──")
        print(f"  LOW  vol (σ<={med:.1f}%): {len(lo_g):>2} coins  "
              f"net ${sum(x[1] for x in lo_g):+8.2f}  "
              f"avg ${sum(x[1] for x in lo_g)/len(lo_g):+7.2f}/coin")
        print(f"  HIGH vol (σ> {med:.1f}%): {len(hi_g):>2} coins  "
              f"net ${sum(x[1] for x in hi_g):+8.2f}  "
              f"avg ${sum(x[1] for x in hi_g)/len(hi_g):+7.2f}/coin")
        hard_lo = sum(per[x[2]]["hard"] for x in lo_g)
        hard_hi = sum(per[x[2]]["hard"] for x in hi_g)
        n_lo = sum(per[x[2]]["n"] for x in lo_g); n_hi = sum(per[x[2]]["n"] for x in hi_g)
        print(f"  hard-stop rate: low-vol {100*hard_lo/n_lo:.1f}% of trips   "
              f"high-vol {100*hard_hi/n_hi:.1f}% of trips")

    # ── NOTIONAL_FRAC rescale (not slot-bound -> trade set is ~unchanged) ──
    print(f"\n  ── NOTIONAL_FRAC rescale (mechanical; slots not binding) ──")
    print(f"  {'frac':>6}{'slots':>7}{'net $':>10}{'worst trip':>12}{'worst day':>11}")
    byday = defaultdict(float)
    for t in trips:
        byday[t["close_ts"].date()] += t["net"]
    worst_day = min(byday.values())
    worst_trip = min(t["net"] for t in trips)
    for frac in (0.20, 0.15, 0.10, 0.05):
        k = frac / 0.20
        print(f"  {frac:>6.2f}{int(2/frac):>7}{total*k:>10.2f}{worst_trip*k:>12.2f}"
              f"{worst_day*k:>11.2f}")

    # ── counterfactual: drop a coin (in-sample; read as a bound, not a forecast) ──
    print(f"\n  ── drop-one counterfactual (IN-SAMPLE — an upper bound, not a forecast) ──")
    for c, d in sorted(per.items(), key=lambda kv: kv[1]["net"])[:4]:
        print(f"  without {c:<9} net ${total - d['net']:+8.2f}  "
              f"({(total-d['net'])/total-1:+.1%} vs actual ${total:+.2f})")


if __name__ == "__main__":
    main()
