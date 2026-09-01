"""
coin_fitness.py — should any coin be dropped from the live book?
════════════════════════════════════════════════════════════════
THE QUESTION, SPLIT IN TWO
The intuition "the biggest losers don't fit this configuration" contains two
very different claims, and they have opposite answers in this project's history:

  A  "coin X lost money, so drop coin X"  — an IN-SAMPLE rank. Already tested
     against the live record and it does not survive: split-half correlation of
     per-coin P&L is r=0.177 and a worst coin as bad as the observed one arises
     by chance with p=0.086. Ranking on realized P&L is ranking on noise.

  B  "some coins have a PROPERTY that makes them a poor match for a trailing
     stop of this width" — testable, generalizable, and the honest version of
     the intuition. A property is measured on the TRAIN window and its
     predictive power judged on TEST.

This module tests both, and reports A's out-of-sample result rather than its
flattering in-sample one.

FIDELITY — WHY THIS IS NOT backtest_exitmodels.py
The live book is no longer one configuration. config.COIN_PARAMS carries
per-coin trail widths, CC's 50/65 straddle with a 10.5% stop, and a per-coin
trail ARMING DELAY (Pine parity: exit orders go live one chart bar after
entry). Simulating every coin at 0.55%/immediate-arm would misprice precisely
the coins being judged, so this module reads the live overrides and models the
arming delay directly.

  python backtest/coin_fitness.py
  python backtest/coin_fitness.py --train 140 --splits 3
"""
import argparse
import random
import statistics as st
import sys
from bisect import insort
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from signals import wilder_rsi, entry_signal  # noqa: E402
import backtest_exitmodels as X  # noqa: E402

D_MS = 86_400_000
BAR_4H = 4 * 60 * 60 * 1000
FRICTION = 0.16          # measured TRAIL-exit friction from live fills (n=29)
START_EQ = 1000.0

# The live book as the worker reports it at boot.
LIVE_BOOK = ["HYPE", "CC", "SOL", "ONDO", "KPEPE", "SUI", "LINK",
             "FARTCOIN", "JTO", "PENGU", "XMR"]
WATCH = {"FARTCOIN", "JTO", "PENGU", "XMR"}      # traded at WATCH_SIZE_MULT

_DATA = {}
_EXITS = {}


def params_for(coin):
    """Resolve the live per-coin overrides, falling back to the frozen set."""
    ov = config.COIN_PARAMS.get(coin.upper(), {})
    return {
        "long_level": float(ov.get("long_level", config.LONG_LEVEL)),
        "short_level": float(ov.get("short_level", config.SHORT_LEVEL)),
        "trail_pct": float(ov.get("trail_pct", config.TRAIL_PCT)),
        "hard_stop_pct": float(ov.get("hard_stop_pct", config.HARD_STOP_PCT)),
        "arm_delay_sec": (float(ov["arm_delay_min"]) * 60
                          if ov.get("arm_delay_min") is not None
                          else config.TRAIL_ARM_DELAY_SEC),
    }


def load(coins, verbose=True):
    out = []
    for c in coins:
        if c in _DATA:
            out.append(c); continue
        d = X.load(c)
        if d is None:
            if verbose:
                print("  no candles for {} — excluded".format(c))
            continue
        b4, m1 = d
        _DATA[c] = {"b4": b4, "m1": m1,
                    "rsi": wilder_rsi([r[4] for r in b4], X.RSI_LEN)}
        out.append(c)
    return out


def exit_path(m1, entry_t, entry_px, side, trail_pct, hard_pct, arm_delay_sec):
    """1m exit walk with the live trail ARMING DELAY modelled.

    Until arm_delay_sec has elapsed the trail cannot arm, so only the hard stop
    is live — which is exactly what Pine does when exit orders go live one chart
    bar after entry. Omitting this makes every coin look like it exits earlier
    and smaller than it really does.
    """
    ts, op, hi, lo, cl = m1
    i = int(np.searchsorted(ts, entry_t, side="left"))
    n = len(ts)
    if i >= n:
        return None
    is_long = side == "long"
    band = entry_px * (trail_pct / 100.0)
    hs = entry_px * (1 - hard_pct / 100.0) if is_long else entry_px * (1 + hard_pct / 100.0)
    arm_px = entry_px * (1 + trail_pct / 100.0) if is_long \
        else entry_px * (1 - trail_pct / 100.0)
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
            fill = min(o, eff) if is_long else max(o, eff)     # gap-through
            return t, fill, ("TRAIL" if armed else "HARD_STOP")
        peak = max(peak, h) if is_long else min(peak, l)
        if not armed and t >= arm_after:
            if (h >= arm_px) if is_long else (l <= arm_px):
                armed = True
        i += 1
    return int(ts[n - 1]), float(cl[n - 1]), "MARKOUT"


def cached_exit(coin, j, side, p):
    key = (coin, j, side, p["trail_pct"], p["hard_stop_pct"], p["arm_delay_sec"])
    hit = _EXITS.get(key)
    if hit is not None:
        return hit
    d = _DATA[coin]
    px = d["b4"][j][4]
    res = exit_path(d["m1"], d["b4"][j][0] + BAR_4H, px, side,
                    p["trail_pct"], p["hard_stop_pct"], p["arm_delay_sec"])
    _EXITS[key] = res
    return res


def run(coins, window=None, frac=0.20, leverage=2.0, friction=FRICTION,
        watch_mult=0.5):
    """Shared-account replay honouring per-coin params, arming delay and WATCH sizing."""
    cap = int(leverage / frac + 1e-9)
    lo_w, hi_w = window if window else (None, None)
    P = {c: params_for(c) for c in coins}

    ev = []
    for c in coins:
        d = _DATA.get(c)
        if d is None:
            continue
        for j, r in enumerate(d["b4"]):
            ct = r[0] + BAR_4H
            if lo_w is not None and not (lo_w <= ct <= hi_w):
                continue
            ev.append((ct, c, j))
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
                           "reason": p["reason"], "closed_t": ex_t})
            if state["base"] > 0 and not state["halted"]:
                if (state["base"] - equity) / state["base"] * 100 >= config.DAILY_DD_PCT:
                    state["halted"] = True

    for ct, c, j in ev:
        flush(ct)
        advance(ct)
        if c in open_pos:
            continue
        d, p = _DATA[c], P[c]
        sig = entry_signal(d["rsi"][j - 1] if j else None, d["rsi"][j],
                           p["long_level"], p["short_level"])
        if not sig:
            continue
        if state["halted"]:
            continue
        if len(open_pos) >= cap:
            blocked += 1
            continue
        res = cached_exit(c, j, sig, p)
        if res is None:
            continue
        ex_t, ex_px, reason = res
        mult = watch_mult if c in WATCH else 1.0
        open_pos[c] = {"side": sig, "entry": d["b4"][j][4],
                       "ntl": frac * equity * mult, "exit_px": ex_px,
                       "reason": reason, "t": ct}
        insort(pending, (ex_t, c))
    flush(float("inf"))

    per = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "hard": 0, "nets": []})
    for t in trades:
        dd = per[t["coin"]]
        dd["n"] += 1; dd["pnl"] += t["pnl"]; dd["w"] += t["net_pct"] > 0
        dd["hard"] += t["reason"] == "HARD_STOP"; dd["nets"].append(t["net_pct"])
    nets = [t["net_pct"] for t in trades]
    eq, peak, dd_ = START_EQ, START_EQ, 0.0
    for t in sorted(trades, key=lambda z: z["closed_t"]):
        eq += t["pnl"]; peak = max(peak, eq); dd_ = min(dd_, (eq - peak) / peak * 100)
    return {"n": len(trades), "pnl": realized, "ret_pct": realized / START_EQ * 100,
            "avg": st.mean(nets) if nets else 0.0,
            "win": 100 * sum(1 for x in nets if x > 0) / len(nets) if nets else 0,
            "hard": sum(1 for t in trades if t["reason"] == "HARD_STOP"),
            "dd": dd_, "blocked": blocked, "per": per, "nets": nets}


def span(coins):
    lo = min(_DATA[c]["b4"][0][0] for c in coins)
    hi = max(_DATA[c]["b4"][-1][0] for c in coins)
    return lo, hi
