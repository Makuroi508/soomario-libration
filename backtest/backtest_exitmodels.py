"""
backtest_exitmodels.py — A/B/C comparison of EXIT rules on one price series.
════════════════════════════════════════════════════════════════════════════
THE QUESTION
Today the bot trails a 0.55% stop against a 120s poll -- an INTRABAR rule.
Would exiting only on completed 4h closes do better? There is also a
measurement argument for close-only: a close-only rule reads nothing but
closes, so 4h candles simulate it EXACTLY -- no fill model, no assumptions.
An intrabar trailing rule is path-dependent and can only be approximated.

THE THREE MODELS (identical entries, identical account mechanics)
  A intrabar    trailing stop evaluated on 1m bars + intrabar hard stop.
                Faithful simulation of the CURRENT live strategy.
  B close_trail trail evaluated ONLY on completed 4h closes (peak = max of
                closes, exit at the close price); hard stop still INTRABAR,
                because it is a native resting trigger on the venue and fires
                whenever touched. A fixed price level is path-independent, so
                checking it against the bar low/high on OHLC is exact.
  C close_only  everything on 4h closes, including the hard stop. Models a bot
                with NO resting order -- worst-case slippage past the stop.
  D threshold   NO TRAILING AT ALL. Enter with the 10% hard stop resting on the
                venue (fires intrabar); at each subsequent 4h close, exit iff the
                position is >= TP_PCT in profit, else hold and re-check next bar.
                A trade therefore ends exactly two ways: a small win booked at a
                4h close, or -10%.

                Two things to watch in D's output, not just its win rate:
                (a) friction. At 0.5% round-trip a +0.55% gross exit nets +0.05%.
                    It only pays because the fill is at whatever the close IS,
                    and 4h bars move 1.5-3%, so a qualifying close often clears
                    the threshold by a wide margin. `avg_win%` reports this.
                (b) slot starvation. With no trail, an underwater trade sits
                    until it recovers or stops out, holding a slot the whole
                    time. Watch trades/`concurrency_full` misses/avg_hold.

Entries are identical across all three: RSI(14) cross on a completed 4h close,
filled at that close, via the live signals.py. Shared-account model throughout
(slots = LEVERAGE/NOTIONAL_FRAC, 1 position/coin, 5% daily-DD halt, friction).

Prices come from backtest/candles_bn (Binance perps; 4h is resampled from the
same 1m series, so entry and exit timeframes cannot disagree).

  python backtest/backtest_exitmodels.py                     # all 3, 180d
  python backtest/backtest_exitmodels.py --per-coin
  python backtest/backtest_exitmodels.py --window live       # the live 42d window
  python backtest/backtest_exitmodels.py --model close_trail --notional 0.10
"""
import argparse
import csv
import gzip
import statistics as st
import sys
from bisect import insort
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import wilder_rsi, entry_signal  # noqa: E402  (the LIVE signal code)

BN = Path(__file__).resolve().parent / "candles_bn"
BAR_4H_MS = 4 * 60 * 60 * 1000

RSI_LEN, LONG_LEVEL, SHORT_LEVEL = 14, 50.0, 40.0
TRAIL_PCT, HARD_STOP_PCT, DAILY_DD_PCT = 0.55, 10.0, 5.0
LEVERAGE, NOTIONAL_FRAC, FRICTION_PCT = 2.0, 0.20, 0.5
START_EQUITY = 1000.0
MODELS = ("intrabar", "close_trail", "close_only", "threshold")
TP_PCT = 0.55                # Model D: profit threshold checked at each 4h close

LIVE = ["DOT", "WLD", "LINK", "TAO", "JTO", "HYPE", "kPEPE", "SUI", "SOL", "NEAR",
        "ADA", "ENA", "BCH", "AVAX", "ZEC", "ATOM", "XMR", "AAVE", "ONDO",
        "FARTCOIN", "PENGU"]
# The live fill history runs 2026-06-08 .. 2026-08-10 (63.0 days), per
# trade_history (3).csv: 1070 fills, 527 round-trips, +$232.35, 77.8% win,
# 20 hard stops. Extend this when a newer export lands.
LIVE_WINDOW = (int(datetime(2026, 6, 8).timestamp() * 1000),
               int(datetime(2026, 8, 11).timestamp() * 1000))


def _read(path):
    if not path.exists():
        return None
    with gzip.open(path, "rt", newline="") as f:
        rd = csv.reader(f); next(rd, None)
        return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in rd]


def load(coin, lo=None, hi=None):
    """Return (bars4h list, 1m arrays) filtered to [lo,hi], or None."""
    b4 = _read(BN / f"{coin.upper()}_4h.csv.gz")
    b1 = _read(BN / f"{coin.upper()}_1m.csv.gz")
    if not b4 or not b1:
        return None
    if lo is not None:
        b4 = [r for r in b4 if lo <= r[0] <= hi]
        b1 = [r for r in b1 if lo <= r[0] <= hi]
    if len(b4) < RSI_LEN + 3 or len(b1) < 100:
        return None
    a = np.array(b1, dtype=np.float64)
    return b4, (a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4])


def exit_intrabar(m1, entry_t, entry_px, side, trail_pct, hard_pct):
    """Model A: 1m-resolution trail + intrabar hard stop (current live strategy)."""
    ts, op, hi, lo, cl = m1
    i = int(np.searchsorted(ts, entry_t, side="left"))
    n = len(ts)
    if i >= n:
        return None
    is_long = side == "long"
    band = entry_px * (trail_pct / 100.0)
    hs = entry_px * (1 - hard_pct / 100.0) if is_long else entry_px * (1 + hard_pct / 100.0)
    arm = entry_px * (1 + trail_pct / 100.0) if is_long else entry_px * (1 - trail_pct / 100.0)
    peak, armed = entry_px, False
    while i < n:
        o, h, l = float(op[i]), float(hi[i]), float(lo[i])
        if armed:
            tstop = (peak - band) if is_long else (peak + band)
            eff = max(tstop, hs) if is_long else min(tstop, hs)
        else:
            eff = hs
        if (l <= eff) if is_long else (h >= eff):
            fill = min(o, eff) if is_long else max(o, eff)   # gap-through
            return int(ts[i]), fill, ("TRAIL" if armed else "HARD_STOP")
        peak = max(peak, h) if is_long else min(peak, l)
        if not armed and ((h >= arm) if is_long else (l <= arm)):
            armed = True
        i += 1
    return int(ts[n - 1]), float(cl[n - 1]), "MARKOUT"


def exit_on_close(b4, start_idx, entry_px, side, trail_pct, hard_pct, intrabar_hard):
    """Models B and C: trail evaluated only on completed 4h closes.

    intrabar_hard=True  -> hard stop is a resting trigger, fires on bar low/high (B)
    intrabar_hard=False -> hard stop only checked at the close, filled there (C)
    """
    is_long = side == "long"
    band = entry_px * (trail_pct / 100.0)
    hs = entry_px * (1 - hard_pct / 100.0) if is_long else entry_px * (1 + hard_pct / 100.0)
    arm = entry_px * (1 + trail_pct / 100.0) if is_long else entry_px * (1 - trail_pct / 100.0)
    peak, armed = entry_px, False
    for j in range(start_idx, len(b4)):
        t, o, h, l, c = b4[j]
        close_t = t + BAR_4H_MS
        if intrabar_hard and ((l <= hs) if is_long else (h >= hs)):
            fill = min(o, hs) if is_long else max(o, hs)     # gap-through
            return close_t, fill, "HARD_STOP"
        if not intrabar_hard and ((c <= hs) if is_long else (c >= hs)):
            return close_t, c, "HARD_STOP"                   # slipped past, fill at close
        peak = max(peak, c) if is_long else min(peak, c)     # peak tracks CLOSES only
        if not armed and ((c >= arm) if is_long else (c <= arm)):
            armed = True
        if armed:
            tstop = (peak - band) if is_long else (peak + band)
            if (c <= tstop) if is_long else (c >= tstop):
                return close_t, c, "TRAIL"
    t, _, _, _, c = b4[-1]
    return t + BAR_4H_MS, c, "MARKOUT"


def exit_threshold(b4, start_idx, entry_px, side, tp_pct, hard_pct):
    """Model D: hold until a 4h CLOSE is >= tp_pct in profit, or the 10% hard stop.

    No trailing and no peak tracking -- the only state is the entry price, so
    this rule reads nothing but closes (plus a fixed stop level) and is exactly
    simulatable on 4h OHLC. The hard stop rests on the venue and fires intrabar.
    """
    is_long = side == "long"
    hs = entry_px * (1 - hard_pct / 100.0) if is_long else entry_px * (1 + hard_pct / 100.0)
    tp = entry_px * (1 + tp_pct / 100.0) if is_long else entry_px * (1 - tp_pct / 100.0)
    for j in range(start_idx, len(b4)):
        t, o, h, l, c = b4[j]
        close_t = t + BAR_4H_MS
        if (l <= hs) if is_long else (h >= hs):
            fill = min(o, hs) if is_long else max(o, hs)     # gap-through
            return close_t, fill, "HARD_STOP"
        if (c >= tp) if is_long else (c <= tp):
            return close_t, c, "TAKE_PROFIT"
    t, _, _, _, c = b4[-1]
    return t + BAR_4H_MS, c, "MARKOUT"


def run(coins, model="intrabar", notional_frac=NOTIONAL_FRAC, leverage=LEVERAGE,
        friction_pct=FRICTION_PCT, window=None, quiet=True, tp_pct=TP_PCT):
    cap = int(leverage / notional_frac + 1e-9)
    lo, hi = window if window else (None, None)

    data, skipped = {}, []
    for c in coins:
        d = load(c, lo, hi)
        if d is None:
            skipped.append(c); continue
        b4, m1 = d
        rsi = wilder_rsi([r[4] for r in b4], RSI_LEN)
        data[c] = {"b4": b4, "m1": m1, "rsi": rsi,
                   "idx": {r[0] + BAR_4H_MS: i for i, r in enumerate(b4)}}
    if not data:
        raise SystemExit("No candles in backtest/candles_bn. Run pull_binance.py first.")
    if skipped and not quiet:
        print(f"  (skipped: {', '.join(skipped)})")

    timeline = sorted({t for d in data.values() for t in d["idx"]})
    equity = realized = 0.0
    equity = START_EQUITY
    open_pos, pending, trades = {}, [], []
    misses = {"concurrency_full": 0, "daily_halt": 0}
    slots = []
    state = {"day": None, "base": equity, "halted": False}

    def advance(ts):
        d = ts // 86_400_000
        if d != state["day"]:
            state["day"], state["base"], state["halted"] = d, equity, False

    def flush(until):
        nonlocal realized, equity
        while pending and pending[0][0] <= until:
            ex_t, coin = pending.pop(0)
            p = open_pos.pop(coin)
            advance(ex_t)
            is_long = p["side"] == "long"
            move = (p["exit_px"] - p["entry"]) if is_long else (p["entry"] - p["exit_px"])
            ret = move / p["entry"] * 100
            net = ret - friction_pct
            pnl = p["ntl"] * net / 100
            realized += pnl
            equity = START_EQUITY + realized
            trades.append({"coin": coin, "side": p["side"], "ret_pct": ret, "net_pct": net,
                           "pnl": pnl, "reason": p["reason"], "opened_t": p["t"],
                           "closed_t": ex_t,
                           "bars_held": max(1, (ex_t - p["t"]) // BAR_4H_MS)})
            if state["base"] > 0 and not state["halted"]:
                if (state["base"] - equity) / state["base"] * 100 >= DAILY_DD_PCT:
                    state["halted"] = True

    for t in timeline:
        flush(t)
        advance(t)
        for coin, d in data.items():
            j = d["idx"].get(t)
            if j is None or coin in open_pos:
                continue
            sig = entry_signal(d["rsi"][j - 1] if j else None, d["rsi"][j],
                               LONG_LEVEL, SHORT_LEVEL)
            if not sig:
                continue
            if state["halted"]:
                misses["daily_halt"] += 1; continue
            if len(open_pos) >= cap:
                misses["concurrency_full"] += 1; continue
            px = d["b4"][j][4]
            if model == "intrabar":
                res = exit_intrabar(d["m1"], t, px, sig, TRAIL_PCT, HARD_STOP_PCT)
            elif model == "threshold":
                res = exit_threshold(d["b4"], j + 1, px, sig, tp_pct, HARD_STOP_PCT)
            else:
                res = exit_on_close(d["b4"], j + 1, px, sig, TRAIL_PCT, HARD_STOP_PCT,
                                    intrabar_hard=(model == "close_trail"))
            if res is None:
                continue
            ex_t, ex_px, reason = res
            open_pos[coin] = {"side": sig, "entry": px, "ntl": notional_frac * equity,
                              "exit_px": ex_px, "reason": reason, "t": t}
            insort(pending, (ex_t, coin))
        slots.append(len(open_pos))

    flush(float("inf"))
    return _stats(trades, realized, misses, slots, cap), trades


def _stats(trades, realized, misses, slots, cap):
    n = len(trades) or 1
    rets = [t["net_pct"] for t in trades]
    per = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "hard": 0})
    for t in trades:
        d = per[t["coin"]]
        d["n"] += 1; d["pnl"] += t["pnl"]
        d["w"] += t["net_pct"] > 0; d["hard"] += t["reason"] == "HARD_STOP"
    sd = st.pstdev(rets) if len(rets) > 1 else 0
    w = [r for r in rets if r > 0]
    l = [r for r in rets if r <= 0]
    return {"avg_win": st.mean(w) if w else 0, "avg_loss": st.mean(l) if l else 0,
            "reasons": dict(Counter(t["reason"] for t in trades)),
            "max_hold": max((t["bars_held"] for t in trades), default=0),
            "trades": len(trades), "win": 100 * sum(1 for r in rets if r > 0) / n,
            "realized": realized, "avg_pct": st.mean(rets) if rets else 0, "sd": sd,
            "t_stat": (st.mean(rets) / (sd / len(rets) ** 0.5)) if sd and rets else 0,
            "hard": sum(1 for t in trades if t["reason"] == "HARD_STOP"),
            "ret_pct": realized / START_EQUITY * 100,
            "avg_bars": st.mean([t["bars_held"] for t in trades]) if trades else 0,
            "mean_slots": st.mean(slots) if slots else 0, "cap": cap,
            "full_pct": 100 * sum(1 for s in slots if s >= cap) / len(slots) if slots else 0,
            "misses": misses, "per": per}


def show(label, s):
    print(f"\n─── {label} ───")
    print(f"  trades={s['trades']}  win={s['win']:.1f}%  hard_stops={s['hard']}  "
          f"avg_hold={s['avg_bars']:.1f} bars ({s['avg_bars']*4:.0f}h)  "
          f"max_hold={s['max_hold']} bars")
    print(f"  realized=${s['realized']:+.2f} ({s['ret_pct']:+.1f}%)  "
          f"avg={s['avg_pct']:+.3f}%/trade  sd={s['sd']:.2f}%  t={s['t_stat']:.2f}")
    print(f"  avg_win={s['avg_win']:+.2f}%  avg_loss={s['avg_loss']:+.2f}%  "
          f"exits={s['reasons']}")
    print(f"  slots mean {s['mean_slots']:.1f}/{s['cap']}, full {s['full_pct']:.0f}%  "
          f"misses={s['misses']}")


def per_coin(s):
    print(f"  {'coin':<10}{'n':>5}{'win%':>6}{'hard':>5}{'pnl $':>10}{'avg $':>8}")
    for c, d in sorted(s["per"].items(), key=lambda kv: -kv[1]["pnl"]):
        print(f"  {c:<10}{d['n']:>5}{100*d['w']/d['n']:>5.0f}%{d['hard']:>5}"
              f"{d['pnl']:>10.2f}{d['pnl']/d['n']:>8.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins")
    ap.add_argument("--model", choices=MODELS)
    ap.add_argument("--notional", type=float, default=NOTIONAL_FRAC)
    ap.add_argument("--friction", type=float, default=FRICTION_PCT)
    ap.add_argument("--window", choices=("live", "full"), default="full")
    ap.add_argument("--per-coin", action="store_true")
    ap.add_argument("--tp", type=float, default=TP_PCT, help="Model D profit threshold %%")
    ap.add_argument("--tp-sweep", action="store_true",
                    help="sweep Model D's threshold to test friction sensitivity")
    a = ap.parse_args()

    coins = [c.strip() for c in a.coins.split(",")] if a.coins else LIVE
    win = LIVE_WINDOW if a.window == "live" else None
    tag = "live 42d window" if a.window == "live" else "full 180d"

    if a.tp_sweep:
        print(f"Model D threshold sweep | {len(coins)} coins @ {a.notional:.0%} | {tag}")
        print(f"(friction {a.friction}% round-trip — a +{a.friction}% gross exit nets zero)")
        print(f"\n  {'tp%':>6}{'trades':>8}{'win%':>7}{'hard':>6}{'avg_win%':>10}"
              f"{'avg%/trade':>12}{'net $':>10}{'t':>7}{'avg_hold':>10}")
        for tp in (0.55, 1.0, 1.5, 2.0, 3.0):
            s, _ = run(coins, model="threshold", notional_frac=a.notional,
                       friction_pct=a.friction, window=win, tp_pct=tp)
            print(f"  {tp:>6.2f}{s['trades']:>8}{s['win']:>6.1f}%{s['hard']:>6}"
                  f"{s['avg_win']:>10.2f}{s['avg_pct']:>12.3f}{s['realized']:>10.2f}"
                  f"{s['t_stat']:>7.2f}{s['avg_bars']:>9.1f}b")
        return

    models = [a.model] if a.model else list(MODELS)
    for m in models:
        s, _ = run(coins, model=m, notional_frac=a.notional, friction_pct=a.friction,
                   window=win, quiet=False, tp_pct=a.tp)
        lbl = f"{m.upper():<12} | {len(coins)} coins @ {a.notional:.0%} | {tag}"
        show(lbl + (f" | tp={a.tp}%" if m == "threshold" else ""), s)
        if a.per_coin:
            per_coin(s)


if __name__ == "__main__":
    main()
