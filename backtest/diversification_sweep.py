"""
diversification_sweep.py — does widening the pool and/or thinning size help?
═══════════════════════════════════════════════════════════════════════════
Runs the VALIDATED strategy (model A: 1m intrabar trail + intrabar hard stop,
which reproduces the live account to within a few percent once friction is set
to the measured 0.09% round-trip) across universe x sizing combinations.

THE SIZING IDENTITY THAT MATTERS
    MAX_CONCURRENT = int(LEVERAGE / NOTIONAL_FRAC)
so slots x frac == LEVERAGE always. Going 10 slots @ 20% -> 20 slots @ 10%
does NOT reduce gross exposure at saturation (both are 200%); it only divides
the same 2x into finer pieces. Real exposure drops only while the account is
SIGNAL-bound rather than SLOT-bound. Getting 20 slots at 20% each instead
means LEVERAGE=4 -- a genuinely different risk profile, reported separately
and clearly marked, since it is NOT an env-only change.

WHY EVERY ROW REPORTS BLOCK STABILITY
A previous round of this study found close-based exit variants that looked
2.5x better on total return but were positive in only 2 of 4 45-day blocks --
all their gains came from the first half. Total return alone is not evidence.
A config is only interesting if it is positive across blocks AND its bootstrap
CI on mean %/trade is meaningfully better, not merely larger.

  python backtest/diversification_sweep.py
  python backtest/diversification_sweep.py --blocks 4 --boot 10000
"""
import argparse
import random
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_exitmodels as X  # noqa: E402

D_MS = 86_400_000
FRICTION = 0.09                  # measured from live fills: 0.045%/side


def max_drawdown(trades):
    """Peak-to-trough drawdown (%) of the realized equity curve."""
    if not trades:
        return 0.0
    eq = X.START_EQUITY
    peak, worst = eq, 0.0
    for t in sorted(trades, key=lambda x: x["closed_t"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        worst = min(worst, (eq - peak) / peak * 100)
    return worst


def evaluate(coins, frac, leverage, window=None, boot=0):
    s, tr = X.run(coins, model="intrabar", notional_frac=frac, leverage=leverage,
                  friction_pct=FRICTION, window=window)
    s["dd"] = max_drawdown(tr)
    if boot and tr:
        rets = [t["net_pct"] for t in tr]
        bs = sorted(st.mean(random.choices(rets, k=len(rets))) for _ in range(boot))
        s["ci"] = (bs[int(boot * .025)], bs[int(boot * .975)])
    return s, tr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=4)
    ap.add_argument("--boot", type=int, default=10000)
    ap.add_argument("--universe", help="file with comma-separated full universe")
    a = ap.parse_args()
    random.seed(11)

    uni_path = a.universe or (Path(__file__).resolve().parent / "universe_top50.txt")
    full = [c.strip() for c in open(uni_path).read().split(",") if c.strip()]
    live = X.LIVE
    have = {p.name.replace("_4h.csv.gz", "").upper()
            for p in X.BN.glob("*_4h.csv.gz")}
    full = [c for c in full if c.upper() in have]
    live = [c for c in live if c.upper() in have]
    print(f"Universes — current: {len(live)} coins | widened: {len(full)} coins "
          f"(+{len(full)-len(live)} new)\n")

    CONFIGS = [
        ("CURRENT   21 coins @20% (10 slots)", live, 0.20, 2.0, True),
        ("THIN      21 coins @10% (20 slots)", live, 0.10, 2.0, True),
        ("WIDE      top50  @20% (10 slots)", full, 0.20, 2.0, True),
        ("WIDE+THIN top50  @10% (20 slots)", full, 0.10, 2.0, True),
        ("LEVERED   top50  @20% (20 slots, 4x!)", full, 0.20, 4.0, False),
    ]

    end = int(time.time() * 1000)
    start = end - 180 * D_MS
    bw = 180 // a.blocks
    blocks = [(start + i * bw * D_MS, start + (i + 1) * bw * D_MS)
              for i in range(a.blocks)]

    print(f"{'config':<40}{'trades':>7}{'win%':>6}{'hard':>5}{'net%':>8}"
          f"{'avg%':>8}{'t':>6}{'maxDD%':>8}{'slots':>11}{'blocked':>8}")
    rows = []
    for label, coins, frac, lev, env_only in CONFIGS:
        s, _ = evaluate(coins, frac, lev, boot=a.boot)
        rows.append((label, coins, frac, lev, s, env_only))
        print(f"{label:<40}{s['trades']:>7}{s['win']:>5.1f}%{s['hard']:>5}"
              f"{s['ret_pct']:>7.1f}%{s['avg_pct']:>8.3f}{s['t_stat']:>6.2f}"
              f"{s['dd']:>8.1f}{s['mean_slots']:>6.1f}/{s['cap']:<4}"
              f"{s['misses']['concurrency_full']:>8}")

    print(f"\nBOOTSTRAP 95% CI on mean %/trade ({a.boot} resamples)")
    for label, _, _, _, s, _ in rows:
        lo, hi = s.get("ci", (0, 0))
        verdict = "EXCLUDES 0" if lo > 0 else "includes 0"
        print(f"  {label:<40} {s['avg_pct']:+.3f}%  [{lo:+.3f}, {hi:+.3f}]  {verdict}")

    print(f"\nSTABILITY — net % per {bw}-day block (a config must earn its headline)")
    print(f"  {'config':<40}" + "".join(f"{'B'+str(i+1):>9}" for i in range(a.blocks))
          + f"{'pos':>6}")
    for label, coins, frac, lev, _, _ in rows:
        cells, pos = [], 0
        for w in blocks:
            sb, _ = evaluate(coins, frac, lev, window=w)
            cells.append(sb["ret_pct"])
            pos += sb["ret_pct"] > 0
        print(f"  {label:<40}" + "".join(f"{c:>8.1f}%" for c in cells)
              + f"{pos:>4}/{a.blocks}")

    print("\nNOTE: LEVERED is NOT env-only — it doubles gross exposure to 4x and "
          "is shown for contrast only.")


if __name__ == "__main__":
    main()
