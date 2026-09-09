"""
equity_2k.py — $2,000 started 12 months ago: where would it be today?
═════════════════════════════════════════════════════════════════════
Runs the live Hyperliquid sizing (NOTIONAL_FRAC 0.20, 2x leverage, 10 slots,
11 coins) from a $2,000 start across the last 12 months, under both
configurations, and reports the month-by-month path rather than only the end
number — the shape is what tells you whether you would have stayed with it.

Also re-runs the trail and arming sweeps on top of each base, scored on the
windows that are NOT in-sample for the per-coin grid (dated 2026-08-16), to
answer a second question: if the overrides come off, are 0.55% and one bar
still the right frozen numbers?

Caveat carried throughout: the whole 12-month record is in-sample for the
overrides, so the LIVE column here is the optimistic one by construction.
"""
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coin_fitness as F  # noqa: E402
import overfit_test as O  # noqa: E402

D_MS = 86_400_000
START = 2000.0
FIT = datetime(2026, 8, 16, tzinfo=timezone.utc)
JUL = datetime(2026, 7, 1, tzinfo=timezone.utc)


def curve(run, start=START):
    """Compounding equity path from the trade sequence, plus monthly buckets."""
    eq, peak, mdd = start, start, 0.0
    months = defaultdict(lambda: {"start": None, "end": None, "n": 0})
    pts = []
    for t in sorted(run["trades"], key=lambda z: z["closed_t"]):
        when = datetime.fromtimestamp(t["closed_t"] / 1000, timezone.utc)
        key = when.strftime("%Y-%m")
        m = months[key]
        if m["start"] is None:
            m["start"] = eq
        eq *= (1.0 + t["net_pct"] * 0.20 / 100.0 *
               (0.5 if t["coin"].upper() in F.WATCH else 1.0))
        m["end"] = eq
        m["n"] += 1
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak * 100.0)
        pts.append((when, eq))
    return eq, mdd, months, pts


def main():
    F._ORIG_PARAMS = F.params_for
    coins = F.load(F.LIVE_BOOK, verbose=False)
    lo, hi = F.span(coins)
    print("12 months {} .. {} | 11 coins | NOTIONAL_FRAC 0.20, 2x, 10 slots | "
          "friction {}%".format(
              datetime.fromtimestamp(lo / 1000, timezone.utc).date(),
              datetime.fromtimestamp(hi / 1000, timezone.utc).date(), F.FRICTION))
    print("start ${:,.2f}".format(START))

    res = {}
    for mode, label in (("live", "LIVE (per-coin overrides)"),
                        ("frozen", "FROZEN (HYPE settings everywhere)")):
        r = O.run_as(coins, mode)
        end, mdd, months, pts = curve(r)
        res[mode] = (r, end, mdd, months)
        print()
        print("─── {} ───".format(label))
        print("  ends at ${:,.2f}   ({:+.1f}%, {:+,.2f})   max DD {:.1f}%   "
              "{} trades".format(end, (end / START - 1) * 100, end - START, mdd, r["n"]))
        print("  {:<10}{:>12}{:>12}{:>10}{:>7}".format(
            "month", "start", "end", "return", "n"))
        for k in sorted(months):
            m = months[k]
            if m["start"] is None:
                continue
            print("  {:<10}{:>12,.2f}{:>12,.2f}{:>9.2f}%{:>7}".format(
                k, m["start"], m["end"],
                (m["end"] / m["start"] - 1) * 100, m["n"]))

    le, fe = res["live"][1], res["frozen"][1]
    print()
    print("  LIVE ends ${:,.2f} vs FROZEN ${:,.2f} — a ${:,.2f} gap, but the whole"
          .format(le, fe, le - fe))
    print("  12 months is in-sample for the overrides, so this gap is the")
    print("  optimistic reading by construction. See the post-fit columns below.")

    # ── do the sweeps land differently on each base? ──
    fit_ms = int(FIT.timestamp() * 1000)
    jul_ms = int(JUL.timestamp() * 1000)
    print()
    print("═══ TRAIL SWEEP ON EACH BASE (avg %/trade) ═══")
    print("  {:>8}{:>12}{:>12}{:>12}{:>12}{:>12}{:>12}".format(
        "trail", "LIVE 12mo", "FROZ 12mo", "LIVE Jul+", "FROZ Jul+",
        "LIVE post", "FROZ post"))
    for t in (0.30, 0.40, 0.55, 0.75, 1.00, 1.50):
        row = [t]
        for w in (None, (jul_ms, hi), (fit_ms, hi)):
            for mode in ("live", "frozen"):
                row.append(O.run_as(coins, mode, window=w, trail=t)["avg"])
        print("  {:>7.2f}%{:>12.3f}{:>12.3f}{:>12.3f}{:>12.3f}{:>12.3f}{:>12.3f}"
              .format(*row))

    print()
    print("═══ ARMING SWEEP ON EACH BASE (avg %/trade) ═══")
    print("  {:>8}{:>12}{:>12}{:>12}{:>12}{:>12}{:>12}".format(
        "delay", "LIVE 12mo", "FROZ 12mo", "LIVE Jul+", "FROZ Jul+",
        "LIVE post", "FROZ post"))
    for m in (0, 30, 60, 240, 480):
        row = [m]
        for w in (None, (jul_ms, hi), (fit_ms, hi)):
            for mode in ("live", "frozen"):
                row.append(O.run_as(coins, mode, window=w, delay=m)["avg"])
        print("  {:>7.0f}m{:>12.3f}{:>12.3f}{:>12.3f}{:>12.3f}{:>12.3f}{:>12.3f}"
              .format(*row))


if __name__ == "__main__":
    main()
