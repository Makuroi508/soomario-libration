"""Driver for coin_fitness: per-coin results, the drop test, and property fits."""
import argparse
import random
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coin_fitness as F  # noqa: E402

D_MS = 86_400_000


def pcorr(xs, ys):
    if len(xs) < 3:
        return 0.0
    mx, my = st.mean(xs), st.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    return num / den if den else 0.0


def coin_props(coin, window):
    """Structural properties measured ON THE TRAIN WINDOW only.

    The point of using a property rather than realized P&L is that a property
    can be measured before the trades exist, so a rule built on it is not a
    restatement of the outcome it is meant to predict.
    """
    d = F._DATA[coin]
    lo, hi = window
    bars = [b for b in d["b4"] if lo <= b[0] <= hi]
    if len(bars) < 20:
        return None
    rets = [(bars[i][4] - bars[i - 1][4]) / bars[i - 1][4] * 100
            for i in range(1, len(bars)) if bars[i - 1][4]]
    rngs = [(b[2] - b[3]) / b[3] * 100 for b in bars if b[3]]
    p = F.params_for(coin)
    return {
        "vol_4h_pct": st.pstdev(rets) if len(rets) > 1 else 0.0,
        "median_bar_range_pct": st.median(rngs) if rngs else 0.0,
        # how many trail-widths fit inside a typical bar: high = the stop sits
        # inside the noise and gets brushed constantly
        "range_over_trail": (st.median(rngs) / p["trail_pct"]) if rngs and p["trail_pct"] else 0.0,
        "stop_over_vol": (p["hard_stop_pct"] / st.pstdev(rets)) if len(rets) > 1 and st.pstdev(rets) else 0.0,
        "trail_pct": p["trail_pct"],
        "arm_delay_min": p["arm_delay_sec"] / 60,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", type=int, default=140)
    ap.add_argument("--splits", type=int, default=3)
    ap.add_argument("--frac", type=float, default=0.20)
    ap.add_argument("--boot", type=int, default=10000)
    a = ap.parse_args()
    random.seed(31)

    coins = F.load(F.LIVE_BOOK)
    lo, hi = F.span(coins)
    total_d = (hi - lo) / D_MS
    print("data {:.1f}d  {} coins: {}".format(total_d, len(coins), ", ".join(coins)))
    print("per-coin overrides in force:")
    for c in coins:
        p = F.params_for(c)
        tag = " [WATCH 0.5x]" if c in F.WATCH else ""
        print("  {:<9} L{:.0f}/S{:.0f}  trail {:.2f}%  stop {:.1f}%  arm {:.0f}m{}".format(
            c, p["long_level"], p["short_level"], p["trail_pct"],
            p["hard_stop_pct"], p["arm_delay_sec"] / 60, tag))

    # ── full-period per-coin ──
    full = F.run(coins, frac=a.frac)
    print("\n── FULL PERIOD, live config ──")
    print("  trades {}  net {:+.1f}%  avg {:+.3f}%/trade  win {:.1f}%  hard {}  DD {:.1f}%  blocked {}"
          .format(full["n"], full["ret_pct"], full["avg"], full["win"],
                  full["hard"], full["dd"], full["blocked"]))
    print("  {:<10}{:>6}{:>7}{:>6}{:>10}{:>9}".format(
        "coin", "n", "win%", "hard", "net $", "avg %"))
    for c, d in sorted(full["per"].items(), key=lambda kv: kv[1]["pnl"]):
        print("  {:<10}{:>6}{:>6.0f}%{:>6}{:>10.2f}{:>9.3f}".format(
            c, d["n"], 100 * d["w"] / d["n"], d["hard"], d["pnl"], st.mean(d["nets"])))

    # ── THE DROP TEST: rank on train, evaluate on test ──
    print("\n── DROP TEST — rank coins on TRAIN, drop the worst k, score on TEST ──")
    print("   (dropping on in-sample rank is only useful if it survives here)")
    splits = []
    for s in range(a.splits):
        tr_d = a.train - s * 20
        split = lo + int(tr_d * D_MS)
        if split >= hi - 20 * D_MS:
            continue
        splits.append((tr_d, (lo, split), (split, hi)))

    rows = []
    for tr_d, TR, TE in splits:
        base_tr = F.run(coins, window=TR, frac=a.frac)
        order = sorted(coins, key=lambda c: base_tr["per"].get(c, {"pnl": 0.0})["pnl"])
        line = {"train_days": tr_d, "worst": order[:3], "cells": []}
        for k in range(0, 4):
            keep = [c for c in coins if c not in set(order[:k])]
            te = F.run(keep, window=TE, frac=a.frac)
            line["cells"].append((k, te["ret_pct"], te["avg"], te["n"]))
        rows.append(line)

    print("  {:<12}{:<26}{:>12}{:>12}{:>12}{:>12}".format(
        "train days", "worst 3 on train", "keep all", "drop 1", "drop 2", "drop 3"))
    for r in rows:
        cells = "".join("{:>12}".format("{:+.1f}%".format(c[1])) for c in r["cells"])
        print("  {:<12}{:<26}{}".format(
            r["train_days"], ",".join(r["worst"])[:25], cells))

    deltas = [c[1] - r["cells"][0][1] for r in rows for c in r["cells"][1:]]
    if deltas:
        better = sum(1 for d in deltas if d > 0)
        print("\n  dropping helped in {}/{} split-and-k combinations "
              "(mean change {:+.2f}pp)".format(better, len(deltas), st.mean(deltas)))

    # ── persistence of the per-coin ranking ──
    print("\n── DOES THE RANKING PERSIST? (train P&L vs test P&L per coin) ──")
    for tr_d, TR, TE in splits:
        tr = F.run(coins, window=TR, frac=a.frac)
        te = F.run(coins, window=TE, frac=a.frac)
        common = [c for c in coins
                  if tr["per"].get(c, {}).get("n", 0) >= 3
                  and te["per"].get(c, {}).get("n", 0) >= 3]
        if len(common) < 4:
            continue
        xs = [tr["per"][c]["pnl"] for c in common]
        ys = [te["per"][c]["pnl"] for c in common]
        r = pcorr(xs, ys)
        worst_tr = min(common, key=lambda c: tr["per"][c]["pnl"])
        print("  train {}d: r={:+.3f} over {} coins | worst on train {} "
              "(${:+.2f}) -> test ${:+.2f}".format(
                  tr_d, r, len(common), worst_tr,
                  tr["per"][worst_tr]["pnl"], te["per"][worst_tr]["pnl"]))

    # ── property fits: is unfitness structural? ──
    print("\n── STRUCTURAL FIT — property on TRAIN vs performance on TEST ──")
    tr_d, TR, TE = splits[0]
    te = F.run(coins, window=TE, frac=a.frac)
    props = {c: coin_props(c, TR) for c in coins}
    have = [c for c in coins if props[c] and te["per"].get(c, {}).get("n", 0) >= 3]
    if len(have) >= 5:
        ys = [st.mean(te["per"][c]["nets"]) for c in have]
        print("  {:<22}{:>9}   interpretation".format("property (train)", "r vs test"))
        for key in ("vol_4h_pct", "median_bar_range_pct", "range_over_trail",
                    "stop_over_vol", "trail_pct", "arm_delay_min"):
            xs = [props[c][key] for c in have]
            r = pcorr(xs, ys)
            verdict = "no relationship" if abs(r) < 0.4 else (
                "weak" if abs(r) < 0.6 else "worth a look")
            print("  {:<22}{:>+9.3f}   {}".format(key, r, verdict))
        print("  (n={} coins — with this few, |r| under about 0.6 is not "
              "distinguishable from chance)".format(len(have)))
    else:
        print("  too few coins with test activity to fit a property")


if __name__ == "__main__":
    main()
