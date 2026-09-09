"""
trail_twoparam.py — split the trail into ARM threshold and TRAIL offset.
════════════════════════════════════════════════════════════════════════
The live bot uses ONE number for two jobs. `TRAIL_PCT = 0.55` is both:

  A  the profit at which the trail switches on, and
  B  how far behind the peak the stop then sits.

Because A == B, at the instant the trail arms the stop sits at
peak - 0.55% = entry, i.e. exactly breakeven. A trade that ticks to +0.55% and
falls straight back exits for zero gross, which is a small loss after fees.

Splitting them gives a two-parameter family:

  B < A   the stop sits in PROFIT the moment it arms — a locked-in gain, at the
          cost of a tighter leash that exits sooner.
  B > A   the stop starts BELOW entry — more room to breathe and bigger
          winners, but a trade can arm and still end negative.

WHY THIS IS BEING TESTED
The TradingView export of the same strategy shows materially fatter winners
than this harness reproduces: median gross +1.156% against +0.659%, with 61
trades landing in the 1-3% band against 39. Entry counts match within 5%, so
the signal is the same and the difference is in the exit. A two-parameter trail
is the most likely explanation, and if some (A, B) both reproduces TradingView
AND survives out of sample, it is worth having.

Guard against the obvious trap: this is a 2-D grid, so the best cell is
expected to look good by chance. Selection happens on TRAIN only and the TEST
column is the one that counts.

  python backtest/trail_twoparam.py
"""
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coin_fitness as F  # noqa: E402
import overfit_test as O  # noqa: E402

D_MS = 86_400_000
BAR_4H = 4 * 60 * 60 * 1000
ARMS = [0.30, 0.55, 0.80, 1.10, 1.50, 2.00]
BANDS = [0.30, 0.40, 0.55, 0.75, 1.00, 1.50]


def exit_two(m1, entry_t, entry_px, side, arm_pct, band_pct, hard_pct,
             arm_delay_sec):
    """Exit walk with the arm threshold and the trail offset kept separate."""
    ts, op, hi, lo, cl = m1
    i = int(np.searchsorted(ts, entry_t, side="left"))
    n = len(ts)
    if i >= n:
        return None
    is_long = side == "long"
    band = entry_px * (band_pct / 100.0)
    hs = entry_px * (1 - hard_pct / 100.0) if is_long else entry_px * (1 + hard_pct / 100.0)
    arm_px = entry_px * (1 + arm_pct / 100.0) if is_long else entry_px * (1 - arm_pct / 100.0)
    arm_after = entry_t + arm_delay_sec * 1000
    peak, armed = entry_px, False
    while i < n:
        t = int(ts[i])
        o, h, l = float(op[i]), float(hi[i]), float(lo[i])
        eff = hs
        if armed:
            tstop = (peak - band) if is_long else (peak + band)
            eff = max(tstop, hs) if is_long else min(tstop, hs)
        if (l <= eff) if is_long else (h >= eff):
            fill = min(o, eff) if is_long else max(o, eff)
            return t, fill, ("TRAIL" if armed else "HARD_STOP")
        peak = max(peak, h) if is_long else min(peak, l)
        if not armed and t >= arm_after:
            if (h >= arm_px) if is_long else (l <= arm_px):
                armed = True
        i += 1
    return int(ts[n - 1]), float(cl[n - 1]), "MARKOUT"


_CACHE = {}


def patch(arm_pct, band_pct):
    """Swap coin_fitness's cached exit for the two-parameter version."""
    def cached_exit(coin, j, side, p):
        key = (coin, j, side, arm_pct, band_pct, p["hard_stop_pct"],
               p["arm_delay_sec"])
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        d = F._DATA[coin]
        px = d["b4"][j][4]
        res = exit_two(d["m1"], d["b4"][j][0] + BAR_4H, px, side, arm_pct,
                       band_pct, p["hard_stop_pct"], p["arm_delay_sec"])
        _CACHE[key] = res
        return res
    return cached_exit


def run(coins, arm_pct, band_pct, window=None, mode="frozen"):
    o_exit, o_par = F.cached_exit, F.params_for
    F.cached_exit = patch(arm_pct, band_pct)
    F.params_for = (lambda c: dict(O.FROZEN)) if mode == "frozen" else o_par
    try:
        return F.run(coins, window=window, frac=0.20)
    finally:
        F.cached_exit, F.params_for = o_exit, o_par


def gross(run_):
    return [t["net_pct"] + F.FRICTION for t in run_["trades"]]


def main():
    F._ORIG_PARAMS = F.params_for
    coins = F.load(F.LIVE_BOOK, verbose=False)
    lo, hi = F.span(coins)
    split = lo + int((hi - lo) * 0.65)
    print("data {} .. {} | book of {} | friction {}%".format(
        datetime.fromtimestamp(lo / 1000, timezone.utc).date(),
        datetime.fromtimestamp(hi / 1000, timezone.utc).date(),
        len(coins), F.FRICTION))
    print("live rule is arm 0.55 / band 0.55 — the stop sits at breakeven when it arms")

    # ── 1. can any (A,B) reproduce TradingView's HYPE winner profile? ──
    print()
    print("═══ 1. HYPE ONLY — which (arm, band) matches TradingView? ═══")
    print("  TradingView reference: median gross +1.156%, 42% of trades in the 1-3% band")
    print("  {:>6}{:>7}{:>8}{:>14}{:>12}{:>10}".format(
        "arm", "band", "n", "median gross", "1-3% band", "mean"))
    for a in ARMS:
        for b in BANDS:
            if b > a * 1.2:
                continue
            r = run(["HYPE"], a, b)
            g = gross(r)
            if len(g) < 20:
                continue
            share = 100 * sum(1 for x in g if 1 < x <= 3) / len(g)
            print("  {:>6.2f}{:>7.2f}{:>8}{:>14.3f}{:>11.0f}%{:>10.3f}".format(
                a, b, len(g), st.median(g), share, st.mean(g)))

    # ── 2. full book, train/test ──
    print()
    print("═══ 2. FULL BOOK — chosen on TRAIN, scored on TEST (avg %/trade) ═══")
    print("  {:>6}{:>7}{:>11}{:>11}{:>11}{:>9}".format(
        "arm", "band", "train", "TEST", "test net%", "hard"))
    rows = []
    for a in ARMS:
        for b in BANDS:
            tr = run(coins, a, b, window=(lo, split))
            te = run(coins, a, b, window=(split, hi))
            rows.append((a, b, tr["avg"], te["avg"], te["ret_pct"], te["hard"]))
            mark = "  <- live rule" if (a == 0.55 and b == 0.55) else ""
            print("  {:>6.2f}{:>7.2f}{:>11.3f}{:>11.3f}{:>10.1f}%{:>9}{}".format(
                a, b, tr["avg"], te["avg"], te["ret_pct"], te["hard"], mark))

    best_tr = max(rows, key=lambda r: r[2])
    best_te = max(rows, key=lambda r: r[3])
    live = next(r for r in rows if r[0] == 0.55 and r[1] == 0.55)
    print()
    print("  best on TRAIN     arm {:.2f} / band {:.2f} -> TEST {:+.3f}%  (this is the honest pick)"
          .format(best_tr[0], best_tr[1], best_tr[3]))
    print("  best on TEST      arm {:.2f} / band {:.2f} -> TEST {:+.3f}%  (hindsight, not actionable)"
          .format(best_te[0], best_te[1], best_te[3]))
    print("  live rule 0.55/0.55                        -> TEST {:+.3f}%".format(live[3]))


if __name__ == "__main__":
    main()
