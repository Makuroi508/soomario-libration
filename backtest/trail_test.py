"""
trail_test.py — how wide should the trailing stop be?
═════════════════════════════════════════════════════
Sweeps TRAIL_PCT from 0.2% to 3.0% against the live book, with a train/test
split, and asks three separate questions:

  1. is there a better UNIFORM width than 0.55%?
  2. does per-coin tuning of the width survive out of sample?
  3. does the live per-coin set (0.20 CC / 0.40 kPEPE / 0.55 default /
     0.75 LINK,FARTCOIN,JTO / 1.00 SUI,PENGU,XMR) beat both?

WHY THIS IS RE-RUN RATHER THAN REUSED
An earlier sweep in this project found trail width to be a textbook overfit —
2.0% was best on train and worst on test. That ran on the OLD 21-coin uniform
config, before per-coin parameters existed and before the trail ARMING DELAY
was modelled. The delay changes the mechanics materially: a width that gets
brushed constantly when armed instantly may behave completely differently once
the position has had a bar to establish direction. So the question is reopened
rather than answered from the old result.

THE TENSION BEING MEASURED
Narrow trails bank small gains often but surrender to noise; wide trails let
winners run but give back more of each peak and let more positions reach the
10% hard stop. Both ends should therefore be worse than some middle, and the
useful output is where that middle sits OUT OF SAMPLE — an in-sample optimum
on a 10-value grid across 11 coins is 110 cells and means very little.

  python backtest/trail_test.py
"""
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import coin_fitness as F  # noqa: E402

D_MS = 86_400_000
GRID = [0.20, 0.30, 0.40, 0.55, 0.75, 1.00, 1.25, 1.50, 2.00, 3.00]
VARIANTS = [("{:.2f}%".format(t), t) for t in GRID] + [("LIVE (per-coin)", None)]


def with_trail(pct):
    """Force one trail width on every coin, leaving all other overrides intact."""
    base = F.params_for

    def patched(coin):
        p = dict(base(coin))
        if pct is not None:
            p["trail_pct"] = pct
        return p
    return patched


def run_variant(coins, pct, window=None, frac=0.20):
    orig = F.params_for
    F.params_for = with_trail(pct)
    try:
        return F.run(coins, window=window, frac=frac)
    finally:
        F.params_for = orig


def shape(run):
    """Win/loss geometry — a wider trail should raise avg win and avg loss both."""
    nets = [t["net_pct"] for t in run["trades"]]
    w = [x for x in nets if x > 0]
    l = [x for x in nets if x <= 0]
    return (st.mean(w) if w else 0.0, st.mean(l) if l else 0.0,
            100.0 * sum(1 for x in nets if abs(x) <= 0.35) / len(nets) if nets else 0.0)


def main():
    coins = F.load(F.LIVE_BOOK, verbose=False)
    lo, hi = F.span(coins)
    total = (hi - lo) / D_MS
    split = lo + int(total * 0.65 * D_MS)
    print("data {} .. {} ({:.0f} days, {} coins) | friction {}% | arming delays live"
          .format(datetime.fromtimestamp(lo / 1000, timezone.utc).date(),
                  datetime.fromtimestamp(hi / 1000, timezone.utc).date(),
                  total, len(coins), F.FRICTION))
    print("live widths: default {}%, CC 0.20, kPEPE 0.40, LINK/FARTCOIN/JTO 0.75, "
          "SUI/PENGU/XMR 1.00".format(config.TRAIL_PCT))

    print()
    print("─── FULL 12 MONTHS ───")
    print("  {:<18}{:>8}{:>9}{:>7}{:>7}{:>9}{:>8}{:>9}{:>9}".format(
        "trail", "trades", "avg%", "win%", "hard", "net%", "DD%", "avg win", "avg loss"))
    full = {}
    for label, t in VARIANTS:
        r = run_variant(coins, t)
        full[label] = r
        aw, al, _ = shape(r)
        print("  {:<18}{:>8}{:>9.3f}{:>6.1f}%{:>7}{:>8.1f}%{:>7.1f}%{:>9.2f}{:>9.2f}"
              .format(label, r["n"], r["avg"], r["win"], r["hard"], r["ret_pct"],
                      r["dd"], aw, al))

    print()
    print("─── TRAIN / TEST (65 / 35) ───")
    print("  {:<18}{:>12}{:>12}{:>12}{:>12}".format(
        "trail", "train avg%", "train net%", "test avg%", "test net%"))
    tr_runs, te_runs = {}, {}
    for label, t in VARIANTS:
        tr = run_variant(coins, t, window=(lo, split))
        te = run_variant(coins, t, window=(split, hi))
        if t is not None:
            tr_runs[t], te_runs[t] = tr, te
        print("  {:<18}{:>12.3f}{:>11.1f}%{:>12.3f}{:>11.1f}%".format(
            label, tr["avg"], tr["ret_pct"], te["avg"], te["ret_pct"]))

    print()
    print("─── PER-COIN OPTIMUM: chosen on TRAIN, scored on TEST ───")
    print("  10 widths x 11 coins is 110 cells; only the TEST column carries a claim.")
    live = full["LIVE (per-coin)"]
    print("  {:<10}{:>12}{:>10}{:>13}{:>13}{:>12}".format(
        "coin", "live width", "best(tr)", "train avg%", "test avg%", "live avg%"))
    beat = 0
    tested = 0
    for c in coins:
        cand = [(t, tr_runs[t]["per"].get(c)) for t in GRID]
        cand = [(t, st.mean(d["nets"])) for t, d in cand if d and d["n"] >= 5]
        if not cand:
            continue
        bt, bv = max(cand, key=lambda x: x[1])
        te = te_runs[bt]["per"].get(c)
        lv = live["per"].get(c)
        if not te or te["n"] < 3 or not lv or lv["n"] < 3:
            continue
        tv, lvv = st.mean(te["nets"]), st.mean(lv["nets"])
        tested += 1
        beat += tv > lvv
        print("  {:<10}{:>11.2f}%{:>9.2f}%{:>13.3f}{:>13.3f}{:>12.3f}".format(
            c, F.params_for(c)["trail_pct"], bt, bv, tv, lvv))
    print("  per-coin tuning beats the live setting on {}/{} coins out of sample"
          .format(beat, tested))

    print()
    print("─── VERDICT ON TEST ───")
    best_tr = max(GRID, key=lambda t: tr_runs[t]["avg"])
    best_te = max(GRID, key=lambda t: te_runs[t]["avg"])
    live_te = run_variant(coins, None, window=(split, hi))
    print("  best uniform picked on TRAIN : {:.2f}% -> test avg {:+.3f}%, net {:+.1f}%"
          .format(best_tr, te_runs[best_tr]["avg"], te_runs[best_tr]["ret_pct"]))
    print("  best uniform in hindsight    : {:.2f}% -> test avg {:+.3f}%  (not actionable)"
          .format(best_te, te_runs[best_te]["avg"]))
    print("  current default 0.55%        : test avg {:+.3f}%, net {:+.1f}%"
          .format(te_runs[0.55]["avg"], te_runs[0.55]["ret_pct"]))
    print("  LIVE per-coin set            : test avg {:+.3f}%, net {:+.1f}%"
          .format(live_te["avg"], live_te["ret_pct"]))

    print()
    print("─── SHADOW A/B CROSS-CHECK (the live 0.3 / 0.4 experiment) ───")
    print("  the bot is shadow-testing 0.3% and 0.4% against the live 0.55%:")
    for t in (0.30, 0.40, 0.55):
        print("    {:.2f}%  train {:+.3f}%   test {:+.3f}%".format(
            t, tr_runs[t]["avg"], te_runs[t]["avg"]))


if __name__ == "__main__":
    main()
