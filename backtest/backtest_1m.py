"""
backtest_1m.py — dual-timeframe Libration replay (4h entries, 1m exits).
════════════════════════════════════════════════════════════════════════
WHY THIS REPLACES THE 4h-ONLY EXIT MODEL
backtest.py evaluates both entries and exits on 4h OHLC. Entries are fine
(the live bot only enters on completed 4h closes), but exits are not: the
live bot polls every 120s with a 0.55% trailing stop, while the median 4h
bar range is 1.7-3.2% and ~100% of 4h bars exceed the trail band. On 4h
data the exit is decided by an intrabar path the OHLC does not contain,
so winners run to the bar extreme. Measured on the live universe:

    4h harness : mean trail exit +1.50% gross, +0.516% net/trade, 65.6% win
    live (42d) : ~+0.74% gross, +0.24% net/trade, 83% win

i.e. the 4h model roughly DOUBLES the per-trade edge. The error scales with
volatility, so it flatters high-vol names most -- which is why TAO, the
worst line in the live book (-$80), comes out PROFITABLE on 4h data. The
kickoff doc's validation gate (TAO must reproduce as the tail) fails there.

WHAT CHANGED (exactly one thing)
  entries : IDENTICAL -- RSI(14) on completed 4h closes via live signals.py,
            filled at the signal bar's close. Untouched.
  exits   : simulated bar-by-bar on 1m candles (closest grid to the live
            120s poll) instead of 4h OHLC.
The trail-band convention (band = entry * trail_pct, per the original) and
all frozen params are preserved, so GRANULARITY IS THE ONLY VARIABLE.

FILL RULES (1m)
  * conservative intrabar order: the stop implied by the peak as of the END
    of the prior 1m bar is tested against this bar's adverse extreme, then
    the peak rolls forward. No look-ahead. (At 1m the bar range is far below
    the 0.55% band, so this is immaterial -- but it is correct.)
  * gap-through: if the bar OPENS beyond the stop, fill at the open, not the
    stop level (pessimistic, and what a resting trigger actually does).
  * hard stop is a native resting trigger live, so it fills at the level.

Usage (after backtest/pull_candles.py AND backtest/pull_candles_1m.py):
  python backtest/backtest_1m.py --per-coin
  python backtest/backtest_1m.py --coins SOL,TAO,JTO --per-coin
  python backtest/backtest_1m.py --notional 0.10
  python backtest/backtest_1m.py --compare
"""
import argparse
import csv
import gzip
import sys
from bisect import insort
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import wilder_rsi, entry_signal  # noqa: E402  (reuse the LIVE signal code)

from backtest import (CANDLE_DIR, LIVE_CORE, LIVE_WATCH, RSI_LEN, LONG_LEVEL,  # noqa: E402
                      SHORT_LEVEL, TRAIL_PCT, HARD_STOP_PCT, DAILY_DD_PCT,
                      LEVERAGE, NOTIONAL_FRAC, FRICTION_PCT, START_EQUITY,
                      load_candles, _summarize, _print_stats, _print_per_coin)

MIN_DIR = Path(__file__).resolve().parent / "candles_1m"
BAR_4H_MS = 4 * 60 * 60 * 1000


def load_1m(coin: str):
    """Return (t[int64], o, h, l, c) float32 arrays, or None if absent."""
    path = MIN_DIR / f"{coin.upper()}_1m.csv.gz"
    if not path.exists():
        return None
    ts, o, h, l, c = [], [], [], [], []
    with gzip.open(path, "rt", newline="") as f:
        for r in csv.DictReader(f):
            ts.append(int(r["t"])); o.append(r["open"]); h.append(r["high"])
            l.append(r["low"]); c.append(r["close"])
    if len(ts) < 100:
        return None
    order = np.argsort(np.array(ts, dtype=np.int64))
    return (np.array(ts, dtype=np.int64)[order],
            np.array(o, dtype=np.float32)[order], np.array(h, dtype=np.float32)[order],
            np.array(l, dtype=np.float32)[order], np.array(c, dtype=np.float32)[order])


def simulate_exit(m, entry_t, entry_px, side, trail_pct, hard_stop_pct):
    """Walk 1m bars forward from entry_t until a stop fills.

    Returns (exit_t, exit_px, reason). Reason is TRAIL / HARD_STOP / MARKOUT.
    The position's exit is independent of account state, so it can be resolved
    once at open time and queued -- equity/slots never affect the price path.
    """
    ts, op, hi, lo, cl = m
    i = int(np.searchsorted(ts, entry_t, side="left"))
    n = len(ts)
    if i >= n:
        return None
    is_long = side == "long"
    band = entry_px * (trail_pct / 100.0)
    hs = entry_px * (1 - hard_stop_pct / 100.0) if is_long \
        else entry_px * (1 + hard_stop_pct / 100.0)
    peak = entry_px
    armed = False
    arm_px = entry_px * (1 + trail_pct / 100.0) if is_long \
        else entry_px * (1 - trail_pct / 100.0)

    while i < n:
        o, h, l = float(op[i]), float(hi[i]), float(lo[i])
        # stop implied by the peak as of the END of the prior bar (no look-ahead)
        if armed:
            trail_stop = (peak - band) if is_long else (peak + band)
            eff = max(trail_stop, hs) if is_long else min(trail_stop, hs)
        else:
            eff = hs
        if (l <= eff) if is_long else (h >= eff):
            # gap-through: opening past the stop fills at the open, not the level
            fill = min(o, eff) if is_long else max(o, eff)
            return int(ts[i]), fill, ("TRAIL" if armed else "HARD_STOP")
        # survived -> roll peak forward, then arm
        peak = max(peak, h) if is_long else min(peak, l)
        if not armed and ((h >= arm_px) if is_long else (l <= arm_px)):
            armed = True
        i += 1
    return int(ts[n - 1]), float(cl[n - 1]), "MARKOUT"


def run(coins, notional_frac=NOTIONAL_FRAC, leverage=LEVERAGE, friction_pct=FRICTION_PCT,
        trail_pct=TRAIL_PCT, hard_stop_pct=HARD_STOP_PCT):
    """Shared-account replay: 4h entry decisions, 1m exit paths."""
    max_concurrent = int(leverage / notional_frac + 1e-9)

    sig, minute, skipped = {}, {}, []
    for coin in coins:
        candles = load_candles(coin)              # 4h, for the RSI entry signal
        m = load_1m(coin)                         # 1m, for the exit path
        if len(candles) < RSI_LEN + 3 or m is None:
            skipped.append(coin)
            continue
        rsi = wilder_rsi([c["close"] for c in candles], RSI_LEN)
        # entry decision lands at the bar's CLOSE time (t + 4h), filled at close px
        sig[coin] = {c["t"] + BAR_4H_MS: (c["close"], rsi[i], rsi[i - 1] if i else None)
                     for i, c in enumerate(candles)}
        minute[coin] = m
    if not sig:
        raise SystemExit("No usable candles. Run pull_candles.py and pull_candles_1m.py first.")
    if skipped:
        print(f"  (skipped, missing 4h or 1m data: {', '.join(skipped)})")

    timeline = sorted({t for d in sig.values() for t in d})
    equity, realized = START_EQUITY, 0.0
    open_pos = {}                 # coin -> dict(exit_t, exit_px, reason, entry, notional, side)
    pending = []                  # sorted [(exit_t, coin)]
    trades, slot_usage = [], []
    misses = {"concurrency_full": 0, "daily_halt": 0}
    state = {"day": None, "baseline": equity, "halted": False}

    def advance_day(ts):
        d = ts // 86_400_000
        if d != state["day"]:
            state["day"], state["baseline"], state["halted"] = d, equity, False

    def flush_exits(until_t):
        """Book every queued exit at or before until_t, in true time order."""
        nonlocal realized, equity
        while pending and pending[0][0] <= until_t:
            ex_t, coin = pending.pop(0)
            p = open_pos.pop(coin)
            advance_day(ex_t)
            is_long = p["side"] == "long"
            move = (p["exit_px"] - p["entry"]) if is_long else (p["entry"] - p["exit_px"])
            ret_pct = move / p["entry"] * 100
            net_pct = ret_pct - friction_pct
            pnl = p["notional"] * net_pct / 100
            realized += pnl
            equity = START_EQUITY + realized
            trades.append({"coin": coin, "side": p["side"], "entry": p["entry"],
                           "exit": p["exit_px"], "ret_pct": ret_pct, "net_pct": net_pct,
                           "pnl": pnl, "reason": p["reason"],
                           "opened_t": p["opened_t"], "closed_t": ex_t})
            if state["baseline"] > 0 and not state["halted"]:
                if (state["baseline"] - equity) / state["baseline"] * 100 >= DAILY_DD_PCT:
                    state["halted"] = True

    for t in timeline:
        flush_exits(t)            # exits that occurred since the last decision point
        advance_day(t)

        for coin in coins:
            if coin not in sig or t not in sig[coin] or coin in open_pos:
                continue
            close_px, rsi_now, rsi_prev = sig[coin][t]
            s = entry_signal(rsi_prev, rsi_now, LONG_LEVEL, SHORT_LEVEL)
            if not s:
                continue
            if state["halted"]:
                misses["daily_halt"] += 1; continue
            if len(open_pos) >= max_concurrent:
                misses["concurrency_full"] += 1; continue
            res = simulate_exit(minute[coin], t, close_px, s, trail_pct, hard_stop_pct)
            if res is None:                        # no 1m data past this point
                continue
            ex_t, ex_px, reason = res
            open_pos[coin] = {"side": s, "entry": close_px, "notional": notional_frac * equity,
                              "exit_t": ex_t, "exit_px": ex_px, "reason": reason, "opened_t": t}
            insort(pending, (ex_t, coin))
        slot_usage.append(len(open_pos))

    flush_exits(float("inf"))     # book anything still open
    stats = _summarize(trades, realized, misses, slot_usage, max_concurrent, list(sig))
    return stats, trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins")
    ap.add_argument("--notional", type=float, default=NOTIONAL_FRAC)
    ap.add_argument("--friction", type=float, default=FRICTION_PCT)
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--per-coin", action="store_true")
    args = ap.parse_args()

    live = LIVE_CORE + LIVE_WATCH
    if args.compare:
        have4 = {p.stem.replace("_4h", "").upper() for p in CANDLE_DIR.glob("*_4h.csv")}
        have1 = {p.name.replace("_1m.csv.gz", "").upper() for p in MIN_DIR.glob("*_1m.csv.gz")}
        have = have4 & have1
        live_have = [c for c in live if c.upper() in have]
        extra = sorted(have - {c.upper() for c in live})
        print(f"Coins with BOTH 4h and 1m candles: {len(have)}. "
              f"Live set present: {len(live_have)}/{len(live)}.")
        print(f"Candidates present: {extra}\n")

        for label, cs, nf in [
            ("CURRENT live universe @ 20% (10 slots)", live_have, 0.20),
            ("WIDER pool (live + candidates) @ 20% (10 slots)", live_have + extra, 0.20),
            ("WIDER pool @ 10% (20 slots) — true diversification", live_have + extra, 0.10),
            ("WIDER pool minus TAO @ 10% (20 slots)",
             [c for c in live_have if c.upper() != "TAO"] + extra, 0.10),
        ]:
            s, _ = run(cs, notional_frac=nf, friction_pct=args.friction)
            _print_stats(label, s)
        return

    coins = [c.strip() for c in args.coins.split(",")] if args.coins else live
    s, trades = run(coins, notional_frac=args.notional, friction_pct=args.friction)
    _print_stats(f"universe ({len(coins)} coins) @ {args.notional:.0%} notional [1m exits]", s)
    if args.per_coin:
        _print_per_coin(s)


if __name__ == "__main__":
    main()
