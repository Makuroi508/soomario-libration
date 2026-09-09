"""
overfit_test.py — are the per-coin overrides real, or were they overfitted?
═══════════════════════════════════════════════════════════════════════════
Compares two whole configurations across the same data:

  LIVE     config.COIN_PARAMS as deployed — CC's 50/65 straddle at 0.20 trail
           and a 10.5% stop, kPEPE 0.40, LINK/FARTCOIN/JTO 0.75,
           SUI/PENGU/XMR 1.00, plus 30-minute arming on the four WATCH names.
  FROZEN   every coin on the HYPE settings, i.e. the walk-forward validated
           defaults with COIN_PARAMS emptied: L50/S40, 0.55% trail, 10% stop,
           one-bar (240m) arming. HYPE is already on these — it sits in the
           "unchanged, omitted" list — so this is literally "run the HYPE bot's
           settings on all eleven coins".

THE DATE THAT MAKES THIS ANSWERABLE
The overrides came from a grid dated 2026-08-16. Any window ending before that
is in-sample for them and will flatter LIVE no matter how overfitted it is; a
65/35 split of a 12-month record still puts most of its "test" window inside
the fitting period. So three windows are reported and they are NOT equivalent:

  FULL     12 months            — entirely in-sample for the overrides
  65/35    conventional split   — test window still mostly in-sample
  POST-FIT strictly after 08-16 — the only genuinely clean comparison

If the overrides are real, LIVE should keep its edge in POST-FIT. If they were
fitted noise, LIVE and FROZEN should converge there, or FROZEN should win.
The POST-FIT window is short, so it settles direction, not magnitude.

  python backtest/overfit_test.py
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
GRID_FIT = datetime(2026, 8, 16, tzinfo=timezone.utc)
TRAILS = [0.30, 0.40, 0.55, 0.75, 1.00, 1.50]
DELAYS = [0, 30, 60, 240, 480]

FROZEN = {"long_level": 50.0, "short_level": 40.0, "trail_pct": 0.55,
          "hard_stop_pct": 10.0, "arm_delay_sec": 14400.0}


def frozen_params(coin):
    """Every coin on the HYPE settings."""
    return dict(FROZEN)


def live_params(coin):
    return F._ORIG_PARAMS(coin)


def run_as(coins, mode, window=None, trail=None, delay=None, frac=0.20):
    """mode: 'live' | 'frozen'. trail/delay optionally override both."""
    orig = F.params_for

    def patched(coin):
        p = dict(FROZEN) if mode == "frozen" else dict(F._ORIG_PARAMS(coin))
        if trail is not None:
            p["trail_pct"] = trail
        if delay is not None:
            p["arm_delay_sec"] = delay * 60.0
        return p
    F.params_for = patched
    try:
        return F.run(coins, window=window, frac=frac)
    finally:
        F.params_for = orig


def line(label, r):
    return ("  {:<22}{:>8}{:>9.3f}{:>7.1f}%{:>7}{:>9.1f}%{:>8.1f}%".format(
        label, r["n"], r["avg"], r["win"], r["hard"], r["ret_pct"], r["dd"]))


def main():
    F._ORIG_PARAMS = F.params_for
    coins = F.load(F.LIVE_BOOK, verbose=False)
    lo, hi = F.span(coins)
    total = (hi - lo) / D_MS
    split = lo + int(total * 0.65 * D_MS)
    fit_ms = int(GRID_FIT.timestamp() * 1000)

    print("data {} .. {} ({:.0f} days) | friction {}% | COIN_PARAMS grid dated {}"
          .format(datetime.fromtimestamp(lo / 1000, timezone.utc).date(),
                  datetime.fromtimestamp(hi / 1000, timezone.utc).date(),
                  total, F.FRICTION, GRID_FIT.date()))
    print("FROZEN = HYPE's settings on every coin: L50/S40, 0.55% trail, "
          "10% stop, 240m arming")

    windows = [
        ("FULL 12 months (in-sample)", None),
        ("TRAIN (first 65%)", (lo, split)),
        ("TEST (last 35%, still mostly in-sample)", (split, hi)),
        ("POST-FIT (after 2026-08-16, clean)", (fit_ms, hi)),
    ]

    print()
    print("═══ HEAD TO HEAD ═══")
    print("  {:<22}{:>8}{:>9}{:>8}{:>7}{:>10}{:>8}".format(
        "config", "trades", "avg%", "win%", "hard", "net%", "DD%"))
    for wl, w in windows:
        days = total if w is None else (w[1] - w[0]) / D_MS
        print("\n  {} — {:.0f} days".format(wl, days))
        rl = run_as(coins, "live", window=w)
        rf = run_as(coins, "frozen", window=w)
        print(line("LIVE (per-coin)", rl))
        print(line("FROZEN (HYPE settings)", rf))
        d = rl["avg"] - rf["avg"]
        print("  {:<22}{:>8}{:>+9.3f}{:>7}{:>7}{:>+9.1f}%".format(
            "  difference", "", d, "", "", rl["ret_pct"] - rf["ret_pct"]))

    # ── per-coin: does each override earn its keep after the fit date? ──
    print()
    print("═══ PER-COIN: does each override beat frozen AFTER the grid date? ═══")
    pl = run_as(coins, "live", window=(fit_ms, hi))
    pf = run_as(coins, "frozen", window=(fit_ms, hi))
    tl = run_as(coins, "live")
    tf = run_as(coins, "frozen")
    print("  {:<10}{:>26}{:>13}{:>13}{:>13}{:>13}".format(
        "coin", "override", "full LIVE", "full FROZ", "post LIVE", "post FROZ"))
    better_full = better_post = tested = 0
    for c in coins:
        ov = config.COIN_PARAMS.get(c.upper(), {})
        desc = ", ".join("{}={}".format(k, v) for k, v in ov.items()) if ov else "(none - frozen)"
        a, b = tl["per"].get(c), tf["per"].get(c)
        x, y = pl["per"].get(c), pf["per"].get(c)
        fa = st.mean(a["nets"]) if a and a["n"] >= 5 else None
        fb = st.mean(b["nets"]) if b and b["n"] >= 5 else None
        pa = st.mean(x["nets"]) if x and x["n"] >= 2 else None
        pb = st.mean(y["nets"]) if y and y["n"] >= 2 else None
        if ov and fa is not None and fb is not None:
            tested += 1
            better_full += fa > fb
            if pa is not None and pb is not None:
                better_post += pa > pb
        f = lambda v: "{:+.3f}".format(v) if v is not None else "-"      # noqa: E731
        print("  {:<10}{:>26}{:>13}{:>13}{:>13}{:>13}".format(
            c, desc[:25], f(fa), f(fb), f(pa), f(pb)))
    print("  overrides beating frozen: {}/{} on the full record, "
          "{}/{} after the grid date".format(better_full, tested, better_post, tested))

    # ── do the sweeps look different under frozen? ──
    print()
    print("═══ TRAIL SWEEP UNDER BOTH CONFIGS (test avg%/trade) ═══")
    print("  A per-coin scheme that merely re-labels one uniform width would show")
    print("  the same curve twice. Divergence means the overrides do something.")
    print("  {:>8}{:>16}{:>16}".format("trail", "on LIVE base", "on FROZEN base"))
    for t in TRAILS:
        a = run_as(coins, "live", window=(split, hi), trail=t)
        b = run_as(coins, "frozen", window=(split, hi), trail=t)
        print("  {:>7.2f}%{:>16.3f}{:>16.3f}".format(t, a["avg"], b["avg"]))

    print()
    print("═══ ARMING SWEEP UNDER BOTH CONFIGS (test avg%/trade) ═══")
    print("  {:>8}{:>16}{:>16}".format("delay", "on LIVE base", "on FROZEN base"))
    for m in DELAYS:
        a = run_as(coins, "live", window=(split, hi), delay=m)
        b = run_as(coins, "frozen", window=(split, hi), delay=m)
        print("  {:>7.0f}m{:>16.3f}{:>16.3f}".format(m, a["avg"], b["avg"]))


if __name__ == "__main__":
    main()
