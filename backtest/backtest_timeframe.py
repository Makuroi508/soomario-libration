"""
backtest_timeframe.py — does the RSI signal work on 15m / 30m / 1h instead of 4h?
═════════════════════════════════════════════════════════════════════════════════
`RSI_TF` is env-configurable, so this is an env-only change like COINS. Entries
move to the chosen timeframe; EXITS are unchanged (1m intrabar trail + resting
hard stop), so the timeframe is the only variable.

WHAT TO WATCH — this is not simply "more trades"
A shorter bar multiplies SIGNALS but not CAPACITY. Slots are fixed at
LEVERAGE/NOTIONAL_FRAC = 10, one position per coin, and the average hold is set
by the 0.55% trail (~11h), not by the entry timeframe. So throughput is capped
near (10 slots / 11h) ~= 22 trades/day no matter how fast signals arrive. Past
that point extra signals do not become extra trades — they become BLOCKED
entries, and the strategy silently degrades from "take the RSI cross" to "take
whichever cross happens to find a free slot". Read `blocked` and `mean slots`
before reading net%.

Second effect: friction is per-trade. At 0.09% round-trip a strategy whose
average gross win is ~0.57% keeps most of it; one that trades 3x as often for
smaller moves may not.

Bars are resampled from the SAME 1m series used for exits, so entry and exit
data can never disagree. Windows are anchored to the data (see
backtest-window-anchoring), never to wall clock.

  python backtest/backtest_timeframe.py
  python backtest/backtest_timeframe.py --tfs 15,30,60,240 --train 140
"""
import argparse
import random
import statistics as st
import sys
import time
from bisect import insort
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import wilder_rsi, entry_signal  # noqa: E402

import backtest_exitmodels as X  # noqa: E402

D_MS = 86_400_000
MIN_MS = 60_000
FRICTION = 0.09
START_EQ = 1000.0
RSI_LEN = 14

_M1 = {}                 # coin -> (ts, o, h, l, c) 1m arrays
_BARS = {}               # (coin, tf) -> (t, o, h, l, c) resampled arrays
_RSI = {}                # (coin, tf) -> list
_EXITS = {}              # (coin, tf, j, side) -> (t, px, reason)


def load_1m(coins, verbose=True):
    t0 = time.time()
    out = []
    for c in coins:
        if c in _M1:
            out.append(c); continue
        d = X.load(c)
        if d is None:
            continue
        _M1[c] = d[1]
        out.append(c)
    if verbose:
        print(f"loaded {len(out)} coins ({time.time()-t0:.0f}s)")
    return out


def resample(coin, tf_min):
    """Aggregate 1m bars into tf_min bars aligned to the UTC grid.

    reduceat over run boundaries — the 1m series can have gaps (venue outages),
    so this groups by bucket id rather than assuming a fixed stride.
    """
    key = (coin, tf_min)
    if key in _BARS:
        return _BARS[key]
    ts, op, hi, lo, cl = _M1[coin]
    step = tf_min * MIN_MS
    b = ts // step
    starts = np.concatenate(([0], np.flatnonzero(np.diff(b)) + 1))
    ends = np.concatenate((starts[1:], [len(ts)]))
    bars = (b[starts] * step,
            op[starts],
            np.maximum.reduceat(hi, starts),
            np.minimum.reduceat(lo, starts),
            cl[ends - 1])
    _BARS[key] = bars
    return bars


def rsi_for(coin, tf_min):
    key = (coin, tf_min)
    if key not in _RSI:
        _RSI[key] = wilder_rsi(list(resample(coin, tf_min)[4]), RSI_LEN)
    return _RSI[key]


def exit_for(coin, tf_min, j, side, trail, hard):
    key = (coin, tf_min, j, side)
    hit = _EXITS.get(key)
    if hit is not None:
        return hit
    t, o, h, l, c = resample(coin, tf_min)
    entry_t = int(t[j]) + tf_min * MIN_MS      # decision lands at the bar CLOSE
    res = X.exit_intrabar(_M1[coin], entry_t, float(c[j]), side, trail, hard)
    _EXITS[key] = res
    return res


def run(coins, tf_min, long_level=50.0, short_level=40.0, trail=0.55, hard=10.0,
        frac=0.20, leverage=2.0, window=None, friction=FRICTION):
    cap = int(leverage / frac + 1e-9)
    lo_w, hi_w = window if window else (None, None)
    step = tf_min * MIN_MS

    ev = []
    for c in coins:
        t = resample(c, tf_min)[0]
        r = rsi_for(c, tf_min)
        for j in range(1, len(t)):
            if r[j] is None or r[j - 1] is None:
                continue
            ct = int(t[j]) + step
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
                           "reason": p["reason"], "closed_t": ex_t,
                           "hold_h": (ex_t - p["t"]) / 3_600_000})
            if state["base"] > 0 and not state["halted"]:
                if (state["base"] - equity) / state["base"] * 100 >= X.DAILY_DD_PCT:
                    state["halted"] = True

    for ct, c, j in ev:
        flush(ct)
        advance(ct)
        if c in open_pos:
            continue
        r = rsi_for(c, tf_min)
        sig = entry_signal(r[j - 1], r[j], long_level, short_level)
        if not sig:
            continue
        if state["halted"]:
            continue
        if len(open_pos) >= cap:
            blocked += 1
            continue
        res = exit_for(c, tf_min, j, sig, trail, hard)
        if res is None:
            continue
        ex_t, ex_px, reason = res
        open_pos[c] = {"side": sig, "entry": float(resample(c, tf_min)[4][j]),
                       "ntl": frac * equity, "exit_px": ex_px,
                       "reason": reason, "t": ct}
        insort(pending, (ex_t, c))
    flush(float("inf"))

    rets = [x["net_pct"] for x in trades]
    n = len(trades) or 1
    eq, peak, dd = START_EQ, START_EQ, 0.0
    for x in sorted(trades, key=lambda z: z["closed_t"]):
        eq += x["pnl"]; peak = max(peak, eq); dd = min(dd, (eq - peak) / peak * 100)
    sd = st.pstdev(rets) if len(rets) > 1 else 0
    days = (max(x["closed_t"] for x in trades) - min(x["closed_t"] for x in trades)) \
        / D_MS if trades else 1
    return {"n": len(trades), "win": 100 * sum(1 for r in rets if r > 0) / n,
            "ret_pct": realized / START_EQ * 100, "avg": st.mean(rets) if rets else 0,
            "t": (st.mean(rets) / (sd / len(rets) ** 0.5)) if sd and rets else 0,
            "hard": sum(1 for x in trades if x["reason"] == "HARD_STOP"),
            "dd": dd, "blocked": blocked, "rets": rets,
            "hold": st.mean([x["hold_h"] for x in trades]) if trades else 0,
            "per_day": len(trades) / max(days, 1)}


def span(coins, tf=240):
    lo = min(int(resample(c, tf)[0][0]) for c in coins)
    hi = max(int(resample(c, tf)[0][-1]) for c in coins)
    return lo, hi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tfs", default="15,30,60,240")
    ap.add_argument("--train", type=int, default=140)
    ap.add_argument("--coins")
    ap.add_argument("--frac", type=float, default=0.20)
    ap.add_argument("--boot", type=int, default=10000)
    a = ap.parse_args()
    random.seed(23)

    coins = [c.strip() for c in a.coins.split(",")] if a.coins else X.LIVE
    coins = load_1m(coins)
    tfs = [int(x) for x in a.tfs.split(",")]

    start, end = span(coins)
    total_d = (end - start) / D_MS
    train_d = min(a.train, total_d * 0.75)
    split = start + int(train_d * D_MS)
    print(f"data {total_d:.1f}d  ({time.strftime('%Y-%m-%d', time.gmtime(start/1000))} .. "
          f"{time.strftime('%Y-%m-%d', time.gmtime(end/1000))})")
    print(f"TRAIN {train_d:.0f}d | TEST {total_d-train_d:.0f}d | {len(coins)} coins "
          f"@ {a.frac:.0%} | L50/S40 | trail 0.55% | friction {FRICTION}%\n")

    name = {15: "15m", 30: "30m", 60: "1h", 240: "4h (live)"}
    for phase, win in (("TRAIN", (start, split)), ("TEST", (split, end))):
        print(f"── {phase} ──")
        print(f"  {'tf':<11}{'trades':>8}{'/day':>7}{'win%':>7}{'hard':>6}{'net%':>8}"
              f"{'avg%':>8}{'t':>6}{'DD%':>7}{'hold_h':>8}{'blocked':>9}")
        for tf in tfs:
            s = run(coins, tf, window=win, frac=a.frac)
            print(f"  {name.get(tf, str(tf)):<11}{s['n']:>8}{s['per_day']:>7.1f}"
                  f"{s['win']:>6.1f}%{s['hard']:>6}{s['ret_pct']:>7.1f}%{s['avg']:>8.3f}"
                  f"{s['t']:>6.2f}{s['dd']:>7.1f}{s['hold']:>8.1f}{s['blocked']:>9}")
        print()

    print("── CHALLENGER vs 4h INCUMBENT on TEST ──")
    base = run(coins, 240, window=(split, end), frac=a.frac)
    for tf in [t for t in tfs if t != 240]:
        s = run(coins, tf, window=(split, end), frac=a.frac)
        if not s["rets"] or not base["rets"]:
            continue
        diff = sorted(st.mean(random.choices(s["rets"], k=len(s["rets"])))
                      - st.mean(random.choices(base["rets"], k=len(base["rets"])))
                      for _ in range(a.boot))
        print(f"  {name.get(tf):<11} diff {s['avg']-base['avg']:+.3f}%/trade  "
              f"CI [{diff[int(a.boot*.025)]:+.3f}, {diff[int(a.boot*.975)]:+.3f}]  "
              f"P(worse)={sum(1 for d in diff if d<0)/len(diff):.2f}")

    bw = (end - start) / D_MS / 4
    print(f"\n── BLOCK STABILITY (4 x {bw:.0f}d, data-anchored) ──")
    for tf in tfs:
        cells = []
        for i in range(4):
            w = (start + int(i * bw * D_MS), start + int((i + 1) * bw * D_MS))
            cells.append(run(coins, tf, window=w, frac=a.frac)["ret_pct"])
        print(f"  {name.get(tf, str(tf)):<11}" + "".join(f"{c:>9.1f}%" for c in cells)
              + f"   {sum(1 for c in cells if c>0)}/4")


if __name__ == "__main__":
    main()
