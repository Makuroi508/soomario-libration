"""
optimize_params.py — RSI level / trail / TP sweep with OUT-OF-SAMPLE discipline.
════════════════════════════════════════════════════════════════════════════════
READ THIS BEFORE TRUSTING ANY NUMBER THIS PRINTS.

Sweeping a grid over 180 days of one asset class is the single easiest way to
manufacture a fake edge. With ~35 parameter combinations, the best-looking cell
is expected to look good BY CHANCE even if every combination is worthless. This
project has already seen it twice: a close-only exit variant that showed +32.9%
(2.5x the incumbent) was positive in only 2 of 4 45-day blocks, and a take-profit
rule was profitable at tp=0.55% while losing money at 1.0/1.5/2.0/3.0.

So this script does NOT report "the best parameters". It reports:
  1. TRAIN  — the first `--train` days. Parameters are chosen here and ONLY here.
  2. TEST   — the remaining days, never consulted during selection. The winner's
              test performance is the only number with any predictive claim.
  3. The INCUMBENT (50/40) evaluated on the SAME test window, as the benchmark
     the challenger has to beat.
  4. Block stability and a bootstrap CI on the difference.

A challenger is only interesting if it beats the incumbent OUT OF SAMPLE and is
stable across blocks. Anything else is curve-fitting.

The exits are model "intrabar" (1m trail + intrabar hard stop), the only
configuration validated against the live account (see backtest_exitmodels.py).
Friction is the measured 0.09% round-trip, NOT the 0.5% the original harness
assumed -- that error alone inverted the sign of every early result.

  python backtest/optimize_params.py --sweep rsi
  python backtest/optimize_params.py --sweep trail
  python backtest/optimize_params.py --sweep tp
"""
import argparse
import random
import statistics as st
import sys
import time
from bisect import insort
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import wilder_rsi, entry_signal  # noqa: E402

import backtest_exitmodels as X  # noqa: E402

D_MS = 86_400_000
BAR_4H = 4 * 60 * 60 * 1000
FRICTION = 0.09
START_EQ = 1000.0

_DATA = {}          # coin -> {b4, m1, rsi, idx}
_EXIT_CACHE = {}    # (coin, bar_idx, side, trail, hard, tp) -> (t, px, reason)


def data_span():
    """First/last bar time ACROSS THE LOADED CANDLES.

    Windows must be anchored to the data, never to time.time(). A session that
    resumes days after the candles were pulled would otherwise slide every
    window forward into empty space -- silently reporting a final block that is
    mostly past the end of the data. That bug turned a 4/4-positive config into
    an apparent 2/4 here, so the anchor is computed, not assumed.
    """
    lo = min(d["b4"][0][0] for d in _DATA.values())
    hi = max(d["b4"][-1][0] for d in _DATA.values())
    return lo, hi


def load_all(coins, verbose=True):
    """Load candles + RSI once. RSI does not depend on the entry LEVELS, so it
    is computed a single time and reused across the whole sweep."""
    t0 = time.time()
    for c in coins:
        if c in _DATA:
            continue
        d = X.load(c)
        if d is None:
            continue
        b4, m1 = d
        _DATA[c] = {"b4": b4, "m1": m1,
                    "rsi": wilder_rsi([r[4] for r in b4], X.RSI_LEN)}
    if verbose:
        print(f"loaded {len(_DATA)} coins in {time.time()-t0:.0f}s")
    return list(_DATA)


def get_exit(coin, j, side, trail, hard, tp):
    """Cached exit resolution. The exit path depends only on (coin, entry bar,
    side, exit params) -- never on account state -- so it is computed once and
    reused across every parameter combination that produces the same entry."""
    key = (coin, j, side, trail, hard, tp)
    hit = _EXIT_CACHE.get(key)
    if hit is not None:
        return hit
    d = _DATA[coin]
    px = d["b4"][j][4]
    t = d["b4"][j][0] + BAR_4H
    if tp is not None:
        res = X.exit_threshold(d["b4"], j + 1, px, side, tp, hard)
    else:
        res = X.exit_intrabar(d["m1"], t, px, side, trail, hard)
    _EXIT_CACHE[key] = res
    return res


def run(coins, long_level=50.0, short_level=40.0, trail=0.55, hard=10.0, tp=None,
        frac=0.20, leverage=2.0, window=None, friction=FRICTION):
    """Shared-account replay with parameterised entry levels."""
    cap = int(leverage / frac + 1e-9)
    lo, hi = window if window else (None, None)

    ev = []     # (close_time, coin, bar_idx) entry decision points
    for c in coins:
        d = _DATA.get(c)
        if d is None:
            continue
        for j, r in enumerate(d["b4"]):
            t = r[0] + BAR_4H
            if lo is not None and not (lo <= t <= hi):
                continue
            ev.append((t, c, j))
    ev.sort()

    equity, realized = START_EQ, 0.0
    open_pos, pending, trades = {}, [], []
    blocked = 0
    state = {"day": None, "base": equity, "halted": False}

    def advance(ts):
        dd = ts // D_MS
        if dd != state["day"]:
            state["day"], state["base"], state["halted"] = dd, equity, False

    def flush(until):
        nonlocal realized, equity
        while pending and pending[0][0] <= until:
            ex_t, coin = pending.pop(0)
            p = open_pos.pop(coin)
            advance(ex_t)
            sgn = 1 if p["side"] == "long" else -1
            ret = (p["exit_px"] - p["entry"]) * sgn / p["entry"] * 100
            net = ret - friction
            pnl = p["ntl"] * net / 100
            realized += pnl
            equity = START_EQ + realized
            trades.append({"coin": coin, "net_pct": net, "pnl": pnl,
                           "reason": p["reason"], "closed_t": ex_t,
                           "bars": max(1, (ex_t - p["t"]) // BAR_4H)})
            if state["base"] > 0 and not state["halted"]:
                if (state["base"] - equity) / state["base"] * 100 >= X.DAILY_DD_PCT:
                    state["halted"] = True

    for t, c, j in ev:
        flush(t)
        advance(t)
        if c in open_pos:
            continue
        d = _DATA[c]
        sig = entry_signal(d["rsi"][j - 1] if j else None, d["rsi"][j],
                           long_level, short_level)
        if not sig:
            continue
        if state["halted"]:
            continue
        if len(open_pos) >= cap:
            blocked += 1
            continue
        res = get_exit(c, j, sig, trail, hard, tp)
        if res is None:          # entry lands past the end of the 1m series
            continue
        ex_t, ex_px, reason = res
        open_pos[c] = {"side": sig, "entry": d["b4"][j][4], "ntl": frac * equity,
                       "exit_px": ex_px, "reason": reason, "t": t}
        insort(pending, (ex_t, c))
    flush(float("inf"))

    rets = [x["net_pct"] for x in trades]
    n = len(trades) or 1
    eq, peak, dd = START_EQ, START_EQ, 0.0
    for x in sorted(trades, key=lambda z: z["closed_t"]):
        eq += x["pnl"]; peak = max(peak, eq); dd = min(dd, (eq - peak) / peak * 100)
    sd = st.pstdev(rets) if len(rets) > 1 else 0
    return {"n": len(trades), "win": 100 * sum(1 for r in rets if r > 0) / n,
            "ret_pct": realized / START_EQ * 100,
            "avg": st.mean(rets) if rets else 0, "sd": sd,
            "t": (st.mean(rets) / (sd / len(rets) ** 0.5)) if sd and rets else 0,
            "hard": sum(1 for x in trades if x["reason"] == "HARD_STOP"),
            "dd": dd, "blocked": blocked, "rets": rets}


def blocks_for(coins, params, start, end, nblocks=4):
    w = (end - start) // nblocks
    out = []
    for i in range(nblocks):
        s = run(coins, window=(start + i * w, start + (i + 1) * w), **params)
        out.append(s["ret_pct"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", choices=("rsi", "trail", "tp"), default="rsi")
    ap.add_argument("--train", type=int, default=120, help="training days")
    ap.add_argument("--coins", help="comma list; default = live 21")
    ap.add_argument("--frac", type=float, default=0.20)
    ap.add_argument("--boot", type=int, default=10000)
    a = ap.parse_args()
    random.seed(17)

    coins = [c.strip() for c in a.coins.split(",")] if a.coins else X.LIVE
    coins = load_all(coins)

    start, end = data_span()                 # anchored to DATA, not wall clock
    total_d = (end - start) / D_MS
    train_d = min(a.train, total_d * 0.75)
    split = start + int(train_d * D_MS)
    TRAIN, TEST = (start, split), (split, end)
    print(f"data spans {total_d:.1f}d "
          f"({time.strftime('%Y-%m-%d', time.gmtime(start/1000))} .. "
          f"{time.strftime('%Y-%m-%d', time.gmtime(end/1000))})")
    print(f"TRAIN {train_d:.0f}d | TEST {total_d-train_d:.0f}d | {len(coins)} coins "
          f"@ {a.frac:.0%} | friction {FRICTION}%\n")

    base = {"long_level": 50.0, "short_level": 40.0, "trail": 0.55, "tp": None,
            "frac": a.frac}

    if a.sweep == "rsi":
        grid = [{"long_level": L, "short_level": S}
                for L in (30, 35, 40, 45, 50, 55, 60) for S in (30, 35, 40, 45, 50)
                if S <= L]
        name = lambda p: f"L{p['long_level']:.0f}/S{p['short_level']:.0f}"       # noqa: E731
    elif a.sweep == "trail":
        grid = [{"trail": v} for v in (0.3, 0.4, 0.55, 0.75, 1.0, 1.5, 2.0)]
        name = lambda p: f"trail {p['trail']}%"                                   # noqa: E731
    else:
        grid = [{"tp": v} for v in (0.55, 0.8, 1.0, 1.5, 2.0, 3.0)]
        name = lambda p: f"tp {p['tp']}%"                                         # noqa: E731

    print(f"── TRAIN ({a.train}d) — {len(grid)} combinations. "
          f"The best cell here is EXPECTED to look good by chance. ──")
    print(f"  {'params':<16}{'n':>6}{'win%':>7}{'hard':>6}{'net%':>8}{'avg%':>8}"
          f"{'t':>6}{'DD%':>7}")
    results = []
    for g in grid:
        p = {**base, **g}
        s = run(coins, window=TRAIN, **p)
        results.append((name(p), p, s))
        print(f"  {name(p):<16}{s['n']:>6}{s['win']:>6.1f}%{s['hard']:>6}"
              f"{s['ret_pct']:>7.1f}%{s['avg']:>8.3f}{s['t']:>6.2f}{s['dd']:>7.1f}")

    ranked = sorted(results, key=lambda r: -r[2]["avg"])
    print(f"\n  best on train: {ranked[0][0]}  (avg {ranked[0][2]['avg']:+.3f}%/trade)")
    inc = [r for r in results if r[0] in ("L50/S40", "trail 0.55%", "tp 0.55%")]
    if inc:
        print(f"  incumbent    : {inc[0][0]}  (avg {inc[0][2]['avg']:+.3f}%/trade)")

    print(f"\n── TEST ({180-a.train}d, never used for selection) ──")
    print(f"  {'params':<16}{'n':>6}{'win%':>7}{'net%':>8}{'avg%':>8}{'t':>6}{'DD%':>7}")
    top = ranked[:3] + [r for r in inc if r not in ranked[:3]]
    test_res = {}
    for nm, p, _ in top:
        s = run(coins, window=TEST, **p)
        test_res[nm] = s
        tag = "  <- incumbent" if nm in ("L50/S40", "trail 0.55%", "tp 0.55%") else ""
        print(f"  {nm:<16}{s['n']:>6}{s['win']:>6.1f}%{s['ret_pct']:>7.1f}%"
              f"{s['avg']:>8.3f}{s['t']:>6.2f}{s['dd']:>7.1f}{tag}")

    inc_name = inc[0][0] if inc else None
    if inc_name and inc_name in test_res:
        ri = test_res[inc_name]["rets"]
        print(f"\n── CHALLENGER vs INCUMBENT on TEST (bootstrap {a.boot}) ──")
        for nm in [n for n in test_res if n != inc_name]:
            rc = test_res[nm]["rets"]
            if not rc or not ri:
                continue
            diff = sorted(st.mean(random.choices(rc, k=len(rc)))
                          - st.mean(random.choices(ri, k=len(ri)))
                          for _ in range(a.boot))
            p_worse = sum(1 for d in diff if d < 0) / len(diff)
            print(f"  {nm:<16} diff {st.mean(rc)-st.mean(ri):+.3f}%/trade  "
                  f"CI [{diff[int(a.boot*.025)]:+.3f}, {diff[int(a.boot*.975)]:+.3f}]  "
                  f"P(worse)={p_worse:.2f}")

    bw = (end - start) / D_MS / 4
    print(f"\n── FULL-PERIOD BLOCK STABILITY (4 x {bw:.0f}d, data-anchored) ──")
    for nm, p, _ in top:
        b = blocks_for(coins, p, start, end)
        print(f"  {nm:<16}" + "".join(f"{x:>8.1f}%" for x in b)
              + f"   {sum(1 for x in b if x>0)}/4 positive")


if __name__ == "__main__":
    main()
