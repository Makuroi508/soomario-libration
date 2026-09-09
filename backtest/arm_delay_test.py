"""
arm_delay_test.py — should the trail arm immediately, or wait a bar?
════════════════════════════════════════════════════════════════════
Two candidate rules for when the 0.55% trailing stop becomes live:

  IMMEDIATE   the trail arms the moment price touches +0.55%, whenever that
              happens, possibly minutes after entry.
  DELAYED     the trail cannot arm until TRAIL_ARM_DELAY_SEC has elapsed
              (default one full 4h bar). This is what runs live, and it mirrors
              Pine, where strategy.exit sits inside `if position_size != 0` and
              so cannot act on the entry bar.

config.py already claims delayed wins ("immediate arming LOST in all 12 coin x
venue combinations -- e.g. HYPE +84% vs -21%"). That is a strong claim from a
grid this module did not run, so it is re-tested here from scratch on 12 months
of Binance 1m data through the validated harness, rather than assumed.

THE MECHANISM UNDER TEST
A 0.55% trail is narrower than ordinary intrabar noise on these coins (median
4h bar range is 1.7-3.2%, i.e. 3-6 trail widths). Armed instantly, a position
that ticks +0.55% and comes straight back gets exited at roughly breakeven --
which after ~0.16% friction is a small loss. Delaying arming lets the position
establish direction before the trail can act. If that mechanism is real, the
delay should raise average trade quality and cut the count of tiny scratch
exits, and the effect should be strongest on the widest-ranging coins.

  python backtest/arm_delay_test.py
"""
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import coin_fitness as F  # noqa: E402

D_MS = 86_400_000
# minutes; None means "leave each coin on its live setting"
VARIANTS = [("immediate", 0), ("15m", 15), ("30m", 30), ("45m", 45),
            ("1h", 60), ("2h", 120), ("4h = 1 bar", 240), ("6h", 360),
            ("8h = 2 bars", 480), ("12h", 720), ("1d = 6 bars", 1440),
            ("LIVE (per-coin)", None)]
# delays worth testing per coin; excludes LIVE, which is a mixture
GRID = [0, 15, 30, 45, 60, 120, 240, 360, 480, 720, 1440]


def with_delay(minutes):
    """Force one arming delay across every coin, keeping all other overrides."""
    base = F.params_for

    def patched(coin):
        p = dict(base(coin))
        if minutes is not None:
            p["arm_delay_sec"] = minutes * 60.0
        return p
    return patched


def run_variant(coins, minutes, window=None, frac=0.20):
    orig = F.params_for
    F.params_for = with_delay(minutes)
    try:
        return F.run(coins, window=window, frac=frac)
    finally:
        F.params_for = orig


def scratch_rate(run, band=0.35):
    """Share of exits landing within +/-band% net — the 'stopped out on noise'
    signature the delay is supposed to prevent."""
    nets = [t["net_pct"] for t in run["trades"]]
    if not nets:
        return 0.0
    return 100.0 * sum(1 for x in nets if abs(x) <= band) / len(nets)


def main():
    coins = F.load(F.LIVE_BOOK, verbose=False)
    lo, hi = F.span(coins)
    total = (hi - lo) / D_MS
    split = lo + int(total * 0.65 * D_MS)
    print("data {} .. {} ({:.0f} days, {} coins) | friction {}%".format(
        datetime.fromtimestamp(lo / 1000, timezone.utc).date(),
        datetime.fromtimestamp(hi / 1000, timezone.utc).date(),
        total, len(coins), F.FRICTION))
    print("live default: TRAIL_ARM_DELAY_BARS={} on {} = {:.0f} min; "
          "FARTCOIN/JTO/PENGU/XMR override to 30 min".format(
              config.TRAIL_ARM_DELAY_BARS, config.RSI_TF,
              config.TRAIL_ARM_DELAY_SEC / 60))

    print("\n─── FULL 12 MONTHS ───")
    print("  {:<18}{:>8}{:>9}{:>7}{:>10}{:>9}{:>9}{:>10}".format(
        "arming", "trades", "avg%", "win%", "hard", "net%", "DD%", "scratch%"))
    full = {}
    for label, m in VARIANTS:
        r = run_variant(coins, m)
        full[label] = r
        print("  {:<18}{:>8}{:>9.3f}{:>6.1f}%{:>10}{:>8.1f}%{:>8.1f}%{:>9.1f}%".format(
            label, r["n"], r["avg"], r["win"], r["hard"], r["ret_pct"], r["dd"],
            scratch_rate(r)))

    print("\n─── TRAIN / TEST (65 / 35) ───")
    print("  {:<18}{:>12}{:>12}{:>12}{:>12}".format(
        "arming", "train avg%", "train net%", "test avg%", "test net%"))
    for label, m in VARIANTS:
        tr = run_variant(coins, m, window=(lo, split))
        te = run_variant(coins, m, window=(split, hi))
        print("  {:<18}{:>12.3f}{:>11.1f}%{:>12.3f}{:>11.1f}%".format(
            label, tr["avg"], tr["ret_pct"], te["avg"], te["ret_pct"]))

    print("\n─── PER COIN: immediate vs live setting (avg % per trade) ───")
    imm = full["immediate"]
    live = full["LIVE (per-coin)"]
    print("  {:<10}{:>10}{:>12}{:>10}{:>9}{:>11}".format(
        "coin", "live delay", "immediate", "delayed", "diff", "bar range"))
    rows = []
    for c in coins:
        a = imm["per"].get(c)
        b = live["per"].get(c)
        if not a or not b or a["n"] < 5 or b["n"] < 5:
            continue
        ia, ba = st.mean(a["nets"]), st.mean(b["nets"])
        bars = F._DATA[c]["b4"]
        rng = st.median([(x[2] - x[3]) / x[3] * 100 for x in bars if x[3]])
        rows.append((c, F.params_for(c)["arm_delay_sec"] / 60, ia, ba, ba - ia, rng))
    rows.sort(key=lambda r: -r[4])
    for c, d, ia, ba, diff, rng in rows:
        print("  {:<10}{:>9.0f}m{:>12.3f}{:>10.3f}{:>+9.3f}{:>10.2f}%".format(
            c, d, ia, ba, diff, rng))
    wins = sum(1 for r in rows if r[4] > 0)
    print("  delay beats immediate on {}/{} coins".format(wins, len(rows)))

    # ── does a per-coin optimum survive out of sample? ──
    print()
    print("─── PER-COIN OPTIMUM: chosen on TRAIN, scored on TEST ───")
    print("  Sweeping 11 delays x 11 coins is 121 cells; the best cell is expected")
    print("  to look good by chance. Only the TEST column carries any claim.")
    tr_runs = {m: run_variant(coins, m, window=(lo, split)) for m in GRID}
    te_runs = {m: run_variant(coins, m, window=(split, hi)) for m in GRID}
    print("  {:<10}{:>12}{:>13}{:>13}{:>13}".format(
        "coin", "best(train)", "train avg%", "test avg%", "live avg%"))
    chosen = {}
    for c in coins:
        cand = [(m, tr_runs[m]["per"].get(c)) for m in GRID]
        cand = [(m, st.mean(d["nets"])) for m, d in cand if d and d["n"] >= 5]
        if not cand:
            continue
        bm, bv = max(cand, key=lambda x: x[1])
        chosen[c] = bm
        te = te_runs[bm]["per"].get(c)
        lv = live["per"].get(c)
        print("  {:<10}{:>11.0f}m{:>13.3f}{:>13}{:>13}".format(
            c, bm, bv,
            "{:.3f}".format(st.mean(te["nets"])) if te and te["n"] >= 3 else "-",
            "{:.3f}".format(st.mean(lv["nets"])) if lv and lv["n"] >= 3 else "-"))

    # ── aggregate: does per-coin tuning beat one uniform number? ──
    print()
    print("─── UNIFORM vs PER-COIN TUNED, on TEST ───")
    best_uniform = max(GRID, key=lambda m: te_runs[m]["avg"])
    best_uniform_tr = max(GRID, key=lambda m: tr_runs[m]["avg"])
    print("  best uniform picked on TRAIN : {:.0f}m -> test avg {:+.3f}%, net {:+.1f}%"
          .format(best_uniform_tr, te_runs[best_uniform_tr]["avg"],
                  te_runs[best_uniform_tr]["ret_pct"]))
    print("  best uniform in hindsight    : {:.0f}m -> test avg {:+.3f}%  (not actionable)"
          .format(best_uniform, te_runs[best_uniform]["avg"]))
    live_te = run_variant(coins, None, window=(split, hi))
    print("  LIVE per-coin config         : test avg {:+.3f}%, net {:+.1f}%"
          .format(live_te["avg"], live_te["ret_pct"]))


if __name__ == "__main__":
    main()
