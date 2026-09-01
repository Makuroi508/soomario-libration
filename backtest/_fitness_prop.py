"""Does bar-range-to-trail-width predict fitness? Tested at n=52, not n=11.

The 11-coin book gave range_over_trail an r of -0.600 against out-of-sample
per-trade return, which is suggestive but sits exactly on the edge of what 11
points can distinguish from chance. If the mechanism is real -- a stop that sits
inside the typical bar's noise gets brushed regardless of whether the coin
trended -- it should hold across every liquid coin, not just the eleven that
happen to be in the book.

Each coin is run STANDALONE (its own account, no slot competition) so the
measurement is of the coin, not of what else was open at the time.
"""
import random
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coin_fitness as F  # noqa: E402
import backtest_exitmodels as X  # noqa: E402

D_MS = 86_400_000
TRAIL = 0.55        # frozen set, so every coin is judged on the same rule
HARD = 10.0
ARM = 14400         # one 4h bar


def pcorr(xs, ys):
    if len(xs) < 3:
        return 0.0
    mx, my = st.mean(xs), st.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    return num / den if den else 0.0


def perm_p(xs, ys, n=20000, seed=7):
    """Two-sided permutation p-value for the correlation."""
    obs = abs(pcorr(xs, ys))
    rnd = random.Random(seed)
    ys2 = list(ys)
    hits = 0
    for _ in range(n):
        rnd.shuffle(ys2)
        if abs(pcorr(xs, ys2)) >= obs:
            hits += 1
    return hits / n


def solo(coin, window, trail=TRAIL):
    """Every RSI cross for one coin, standalone, frozen rule."""
    d = F._DATA[coin]
    lo, hi = window
    nets = []
    for j in range(1, len(d["b4"])):
        ct = d["b4"][j][0] + F.BAR_4H
        if not (lo <= ct <= hi):
            continue
        from signals import entry_signal
        sig = entry_signal(d["rsi"][j - 1], d["rsi"][j], 50.0, 40.0)
        if not sig:
            continue
        r = F.exit_path(d["m1"], ct, d["b4"][j][4], sig, trail, HARD, ARM)
        if not r:
            continue
        _, px, _ = r
        sgn = 1 if sig == "long" else -1
        nets.append((px - d["b4"][j][4]) * sgn / d["b4"][j][4] * 100 - F.FRICTION)
    return nets


def prop(coin, window):
    d = F._DATA[coin]
    lo, hi = window
    bars = [b for b in d["b4"] if lo <= b[0] <= hi]
    if len(bars) < 30:
        return None
    rngs = [(b[2] - b[3]) / b[3] * 100 for b in bars if b[3]]
    rets = [(bars[i][4] - bars[i - 1][4]) / bars[i - 1][4] * 100
            for i in range(1, len(bars)) if bars[i - 1][4]]
    if not rngs or len(rets) < 5:
        return None
    return {"range_over_trail": st.median(rngs) / TRAIL,
            "median_range": st.median(rngs),
            "vol": st.pstdev(rets)}


def main():
    uni = sorted({p.name.replace("_4h.csv.gz", "")
                  for p in X.BN.glob("*_4h.csv.gz")})
    coins = F.load(uni, verbose=False)
    lo, hi = F.span(coins)
    split = lo + int((hi - lo) * 0.66)
    TR, TE = (lo, split), (split, hi)
    print("universe {} coins | train {:.0f}d | test {:.0f}d | frozen rule "
          "0.55% trail, 10% stop, 4h arm".format(
              len(coins), (split - lo) / D_MS, (hi - split) / D_MS))

    rows = []
    for c in coins:
        p = prop(c, TR)
        if not p:
            continue
        te = solo(c, TE)
        tr = solo(c, TR)
        if len(te) < 8 or len(tr) < 8:
            continue
        rows.append({"coin": c, "rot": p["range_over_trail"], "vol": p["vol"],
                     "train_avg": st.mean(tr), "test_avg": st.mean(te),
                     "n_te": len(te)})
    rows.sort(key=lambda r: r["rot"])
    print("\n  {:<10}{:>10}{:>10}{:>12}{:>12}{:>7}".format(
        "coin", "range/trail", "vol%", "train avg%", "test avg%", "n"))
    for r in rows:
        print("  {:<10}{:>10.1f}{:>10.2f}{:>12.3f}{:>12.3f}{:>7}".format(
            r["coin"], r["rot"], r["vol"], r["train_avg"], r["test_avg"], r["n_te"]))

    xs = [r["rot"] for r in rows]
    ys = [r["test_avg"] for r in rows]
    r_all = pcorr(xs, ys)
    p_all = perm_p(xs, ys)
    print("\n  range/trail (train) vs test avg%/trade:  r={:+.3f}  "
          "permutation p={:.4f}  n={}".format(r_all, p_all, len(rows)))
    xt = [r["train_avg"] for r in rows]
    print("  train avg% vs test avg% (does P&L rank persist?): r={:+.3f}  "
          "permutation p={:.4f}".format(pcorr(xt, ys), perm_p(xt, ys)))

    # quartile view: is there a usable cut, or just a slope?
    q = len(rows) // 4
    if q >= 3:
        lowq, highq = rows[:q], rows[-q:]
        print("\n  lowest-quartile range/trail ({} coins, ratio<={:.1f}): "
              "test avg {:+.3f}%/trade".format(q, lowq[-1]["rot"],
                                               st.mean(r["test_avg"] for r in lowq)))
        print("  highest-quartile range/trail ({} coins, ratio>={:.1f}): "
              "test avg {:+.3f}%/trade".format(q, highq[0]["rot"],
                                               st.mean(r["test_avg"] for r in highq)))


if __name__ == "__main__":
    main()
