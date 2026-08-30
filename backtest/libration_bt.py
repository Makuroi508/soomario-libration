#!/usr/bin/env python3
"""
libration_bt.py — run Libration's frozen strategy over the Rotation stock universe.

The point of this harness is to answer one question honestly: do Libration's
settings, which were validated on 24/7 altcoin perps, still carry an edge on the
equities Rotation trades? So the rules are ported verbatim rather than
re-tuned, and the signal code is IMPORTED from the live bot (`signals.py`)
rather than reimplemented — if the strategy changes, this backtest changes with
it and cannot silently drift out of parity.

Frozen rules (config.py defaults, mirrored in Params below):
    RSI(14) Wilder on completed 4h closes
    long   = RSI crosses UP through 50
    short  = RSI crosses DOWN through 40
    trail  = 0.55% — arms at +0.55%, then rides 0.55% behind the peak
    arm delay = 1 bar (no peak tracking or arming on the entry bar)
    hard stop = 10% from entry, never moved against
    notional  = 20% of live (mark-to-market) equity per position
    leverage  = 2x, max 10 concurrent, pyramiding 1
    daily halt = no new entries once down 5% on the UTC day

Two modelling choices worth knowing, both deliberately conservative:

  Exits resolve on 15m bars, not 4h. A 0.55% trail cannot be simulated on 4h
  OHLC — these names range well over 1% in four hours, so a 4h-only fill model
  would invent whatever answer you wanted. Within each 15m bar the stop is
  checked BEFORE the peak is advanced, so the trail never ratchets using
  information from later in the same bar.

  Gaps fill at the open, not at the stop. If a bar opens through the stop the
  fill is booked at that open. This is the mechanic that punishes a 0.55% trail
  on anything that stops trading overnight, and hiding it would defeat the
  purpose of the test.
"""
import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from signals import entry_signal, wilder_rsi   # noqa: E402  (live strategy code)


@dataclass
class Params:
    rsi_len: int = 14
    long_level: float = 50.0
    short_level: float = 40.0
    trail_pct: float = 0.55
    arm_delay_bars: int = 1
    bar_seconds: int = 4 * 3600
    hard_stop_pct: float = 10.0
    notional_frac: float = 0.20
    leverage: float = 2.0
    max_concurrent: int = 10
    daily_dd_pct: float = 5.0
    # 4.36 bps round trip is what the live book actually paid over 611 trades
    # (fee / entry notional, from /api/trades). Split evenly across the two legs.
    fee_bps_per_side: float = 2.18
    slippage_bps: float = 0.0
    start_equity: float = 1697.02       # Libration's real inception equity
    allow_shorts: bool = True


@dataclass
class Position:
    coin: str
    side: str
    entry: float
    qty: float
    notional: float
    opened_ms: int
    peak: float
    hard_stop: float
    trail_stop: float = None
    trail_active: bool = False


@dataclass
class Trade:
    coin: str
    side: str
    entry: float
    exit: float
    qty: float
    notional: float
    opened_ms: int
    closed_ms: int
    reason: str
    fee: float
    gross: float = 0.0
    net: float = 0.0
    ret_pct: float = 0.0
    net_pct: float = 0.0


@dataclass
class Result:
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    misses: dict = field(default_factory=dict)
    entries: int = 0
    still_open: list = field(default_factory=list)


def _sign(side):
    return 1.0 if side == "long" else -1.0


def build_signals(bars_4h, p: Params):
    """Map 4h bar CLOSE ms -> ('long'|'short', close_price).

    Only completed bars produce signals, matching signals.closed_candles: the
    decision for bar i is taken at bar i's close using RSI[i-1] -> RSI[i].
    """
    closes = [b["c"] for b in bars_4h]
    rsi = wilder_rsi(closes, p.rsi_len)
    out = {}
    for i in range(1, len(bars_4h)):
        sig = entry_signal(rsi[i - 1], rsi[i], p.long_level, p.short_level)
        if sig and (p.allow_shorts or sig == "long"):
            out[bars_4h[i]["T"]] = (sig, bars_4h[i]["c"])
    return out


def run(data, p: Params):
    """data: {symbol: {"bars_4h": [...], "bars_15m": [...]}} -> Result."""
    sigs = {s: build_signals(d["bars_4h"], p) for s, d in data.items()}
    execs = {s: {b["T"]: b for b in d["bars_15m"]} for s, d in data.items()}

    timeline = sorted({t for s in data for t in execs[s]})
    if not timeline:
        return Result()

    cash = p.start_equity
    open_pos, res = {}, Result()
    arm_delay_ms = p.arm_delay_bars * p.bar_seconds * 1000
    fee_rate = p.fee_bps_per_side / 10_000.0
    slip = p.slippage_bps / 10_000.0

    day = None
    baseline = cash
    halted = False

    def equity_at(ts):
        """Mark-to-market equity. A symbol with no bar at ts (halted feed) marks
        at its entry, which is neutral rather than optimistic."""
        eq = cash
        for c, pos in open_pos.items():
            b = execs[c].get(ts)
            mk = b["c"] if b else pos.entry
            eq += _sign(pos.side) * (mk - pos.entry) * pos.qty
        return eq

    def close_pos(pos, price, ts, reason):
        nonlocal cash
        # Adverse slippage on the exit leg.
        fill = price * (1 - slip) if pos.side == "long" else price * (1 + slip)
        gross = _sign(pos.side) * (fill - pos.entry) * pos.qty
        fee = pos.notional * fee_rate + abs(fill * pos.qty) * fee_rate
        cash += gross - fee
        t = Trade(coin=pos.coin, side=pos.side, entry=pos.entry, exit=fill,
                  qty=pos.qty, notional=pos.notional, opened_ms=pos.opened_ms,
                  closed_ms=ts, reason=reason, fee=fee,
                  gross=gross, net=gross - fee,
                  ret_pct=_sign(pos.side) * (fill - pos.entry) / pos.entry * 100,
                  net_pct=((gross - fee) / pos.notional * 100) if pos.notional else 0.0)
        res.trades.append(t)
        del open_pos[pos.coin]

    for ts in timeline:
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        if d != day:
            day, baseline, halted = d, equity_at(ts), False

        # ── 1. manage open positions on this 15m bar ──────────────
        for coin in list(open_pos):
            pos = open_pos[coin]
            b = execs[coin].get(ts)
            if not b:
                continue
            is_long = pos.side == "long"
            in_delay = ts <= pos.opened_ms + arm_delay_ms

            stop = pos.hard_stop if (in_delay or not pos.trail_active) else pos.trail_stop
            hit = (b["l"] <= stop) if is_long else (b["h"] >= stop)
            gapped = (b["o"] <= stop) if is_long else (b["o"] >= stop)
            if hit:
                # A bar that OPENS through the stop fills at the open — the gap
                # is the loss, not the stop level.
                fill = b["o"] if gapped else stop
                close_pos(pos, fill, ts,
                          "TRAIL" if pos.trail_active and not in_delay else "HARD_STOP")
                continue

            if in_delay:
                # Pine parity: peak is pinned to current price, trail cannot arm.
                pos.peak = b["c"]
                continue

            pos.peak = max(pos.peak, b["h"]) if is_long else min(pos.peak, b["l"])
            if not pos.trail_active:
                armed = (b["h"] >= pos.entry * (1 + p.trail_pct / 100)) if is_long \
                    else (b["l"] <= pos.entry * (1 - p.trail_pct / 100))
                if armed:
                    pos.trail_active = True
            if pos.trail_active:
                band = pos.entry * (p.trail_pct / 100)
                lvl = pos.peak - band if is_long else pos.peak + band
                new = max(lvl, pos.hard_stop) if is_long else min(lvl, pos.hard_stop)
                if pos.trail_stop is None:
                    pos.trail_stop = new
                elif (new > pos.trail_stop) if is_long else (new < pos.trail_stop):
                    pos.trail_stop = new

        # ── 2. daily drawdown halt ────────────────────────────────
        eq = equity_at(ts)
        if not halted and baseline > 0 and eq <= baseline * (1 - p.daily_dd_pct / 100):
            halted = True

        # ── 3. entries, only on a completed 4h close ──────────────
        for coin in sorted(data):
            hit = sigs[coin].get(ts)
            if not hit:
                continue
            side, px = hit
            if coin in open_pos:
                res.misses["already_open"] = res.misses.get("already_open", 0) + 1
                continue
            if halted:
                res.misses["daily_dd_halt"] = res.misses.get("daily_dd_halt", 0) + 1
                continue
            if len(open_pos) >= p.max_concurrent:
                res.misses["concurrency_full"] = res.misses.get("concurrency_full", 0) + 1
                continue
            notional = p.notional_frac * eq
            used = sum(x.notional for x in open_pos.values()) / p.leverage
            if used + notional / p.leverage > eq:
                res.misses["margin_full"] = res.misses.get("margin_full", 0) + 1
                continue
            fill = px * (1 + slip) if side == "long" else px * (1 - slip)
            if fill <= 0:
                continue
            qty = notional / fill
            hard = fill * (1 - p.hard_stop_pct / 100) if side == "long" \
                else fill * (1 + p.hard_stop_pct / 100)
            open_pos[coin] = Position(coin=coin, side=side, entry=fill, qty=qty,
                                      notional=notional, opened_ms=ts, peak=fill,
                                      hard_stop=hard)
            res.entries += 1

        res.equity_curve.append((ts, eq))

    res.still_open = [(c, x.side, x.entry) for c, x in open_pos.items()]
    return res


def report(res: Result, p: Params, label=""):
    import statistics as st
    tr = res.trades
    if not tr:
        print(f"{label}: no trades")
        return
    net = sum(t.net for t in tr)
    wins = [t for t in tr if t.net > 0]
    eq = [e for _, e in res.equity_curve]
    peak, mdd = -1e18, 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak if peak > 0 else 0)
    days = (res.equity_curve[-1][0] - res.equity_curve[0][0]) / 86_400_000
    final = p.start_equity + net
    ret = (final / p.start_equity - 1) * 100
    ann = ((final / p.start_equity) ** (365 / days) - 1) * 100 if days >= 1 else float("nan")
    hs = [t for t in tr if t.reason == "HARD_STOP"]

    print(f"\n{'=' * 68}\n{label}\n{'=' * 68}")
    print(f"  window            {days:.0f} days")
    print(f"  trades            {len(tr)}   ({len(tr)/max(days,1):.2f}/day)")
    print(f"  win rate          {len(wins)/len(tr)*100:.1f}%")
    print(f"  avg net / trade   {st.mean([t.net_pct for t in tr]):+.4f}% of notional")
    print(f"  net P&L           ${net:+,.2f}   on ${p.start_equity:,.2f} start")
    print(f"  total return      {ret:+.2f}%   (annualized {ann:+.1f}%)")
    print(f"  max drawdown      {mdd*100:.2f}%")
    print(f"  fees paid         ${sum(t.fee for t in tr):,.2f}")
    print(f"  hard stops        {len(hs)} ({len(hs)/len(tr)*100:.1f}%), "
          f"P&L ${sum(t.net for t in hs):+,.2f}")
    if res.misses:
        print(f"  misses            {dict(sorted(res.misses.items()))}")

    per = {}
    for t in tr:
        a = per.setdefault(t.coin, [0, 0.0, 0])
        a[0] += 1
        a[1] += t.net
        a[2] += 1 if t.net > 0 else 0
    print(f"\n  {'sym':6} {'n':>4} {'net$':>10} {'win%':>7}")
    for c, (n, pnl, w) in sorted(per.items(), key=lambda kv: -kv[1][1]):
        print(f"  {c:6} {n:4} {pnl:10,.2f} {w/n*100:6.1f}%")


def load(path):
    data = {}
    for f in sorted(glob.glob(os.path.join(path, "*.json"))):
        d = json.load(open(f))
        if d.get("bars_4h") and d.get("bars_15m"):
            data[d["symbol"]] = d
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="backtest/data")
    ap.add_argument("--fee-bps-per-side", type=float, default=2.18)
    ap.add_argument("--slippage-bps", type=float, default=0.0)
    ap.add_argument("--start-equity", type=float, default=1697.02)
    ap.add_argument("--longs-only", action="store_true")
    args = ap.parse_args()

    data = load(args.data)
    if not data:
        print(f"no data in {args.data} — run backtest/fetch_candles.py first")
        return 1
    print(f"loaded {len(data)} symbols: {', '.join(sorted(data))}")

    p = Params(fee_bps_per_side=args.fee_bps_per_side,
               slippage_bps=args.slippage_bps,
               start_equity=args.start_equity,
               allow_shorts=not args.longs_only)
    report(run(data, p), p, "Libration rules on Rotation universe")

    # Fee sensitivity: the live edge is ~0.17%/trade gross, so cost assumptions
    # are not a footnote here — they can flip the sign.
    for bps, name in [(4.5, "HL taker 4.5bps/side"), (10.0, "10bps/side stress")]:
        p2 = Params(fee_bps_per_side=bps, slippage_bps=args.slippage_bps,
                    start_equity=args.start_equity, allow_shorts=not args.longs_only)
        report(run(data, p2), p2, f"Sensitivity — {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
