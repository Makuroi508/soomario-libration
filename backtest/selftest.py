#!/usr/bin/env python3
"""
selftest.py — synthetic fixtures that pin the backtest's mechanics.

These are not strategy tests; they check that libration_bt reproduces the four
behaviours that decide whether a Libration-on-stocks result is believable at
all: the entry trigger, the arming delay, the trailing ratchet, and the gap
fill. Run with `python3 backtest/selftest.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libration_bt import Params, run   # noqa: E402

H4 = 4 * 3600 * 1000
M15 = 15 * 60 * 1000
T0 = 1_700_000_000_000 // H4 * H4      # aligned 4h boundary


def bars_4h(closes, t0=T0):
    """Flat-bodied 4h candles at `closes` — only the close feeds RSI."""
    return [{"t": t0 + i * H4, "T": t0 + (i + 1) * H4,
             "o": c, "h": c, "l": c, "c": c, "v": 1.0}
            for i, c in enumerate(closes)]


def bars_15m(path, t_first_close):
    """path: list of (open, high, low, close) -> consecutive 15m candles, the
    first of which CLOSES at t_first_close. Real 15m and 4h grids share close
    timestamps, and the engine keys entries off the 4h close, so the fixture
    has to line up the same way or no entry ever fires."""
    return [{"t": t_first_close + (i - 1) * M15, "T": t_first_close + i * M15,
             "o": o, "h": h, "l": lo, "c": c, "v": 1.0}
            for i, (o, h, lo, c) in enumerate(path)]


def rsi_cross_up_closes():
    """20 down closes (RSI far below 50) then strong ups to cross 50."""
    closes = [100.0]
    for _ in range(20):
        closes.append(closes[-1] * 0.99)
    for _ in range(12):
        closes.append(closes[-1] * 1.03)
    return closes


def find_entry(res):
    assert res.trades or True
    return res.trades


def case(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'  — ' + detail if detail else ''}")
    return ok


def main():
    p = Params(fee_bps_per_side=0.0, slippage_bps=0.0,
               start_equity=10_000.0, allow_shorts=False)
    ok = True

    # ── 1. entry fires on the 4h close where RSI crosses up through 50 ──
    closes = rsi_cross_up_closes()
    b4 = bars_4h(closes)
    from signals import wilder_rsi
    r = wilder_rsi(closes, 14)
    xi = next(i for i in range(1, len(closes))
              if r[i - 1] is not None and r[i - 1] < 50 <= r[i])
    entry_px = closes[xi]

    # After entry: drift flat for 2 full 4h (arm delay + margin), then rally.
    start = b4[xi]["T"]
    flat = [(entry_px, entry_px * 1.004, entry_px, entry_px)] * 32   # 8h, under +0.55%
    res = run({"X": {"bars_4h": b4, "bars_15m": bars_15m(flat, start)}}, p)
    ok &= case("entry fires once on the RSI-50 crossover close",
               res.entries == 1 and res.still_open
               and abs(res.still_open[0][2] - entry_px) < 1e-9,
               f"entered long at {entry_px:.4f}; no exit while under +0.55%")

    # ── 2. arming delay: a +2% spike INSIDE the entry bar must not arm ──
    spike = [(entry_px, entry_px * 1.02, entry_px, entry_px)] * 16   # first 4h only
    calm = [(entry_px, entry_px * 1.001, entry_px * 0.999, entry_px)] * 16
    res = run({"X": {"bars_4h": b4, "bars_15m": bars_15m(spike + calm, start)}}, p)
    ok &= case("trail cannot arm during the 1-bar delay",
               not res.trades, "spike to +2% in the entry bar booked no exit")

    # ── 3. trail arms after the delay and exits at peak − 0.55% ──
    # 17 calm bars so the rally lands strictly AFTER the 4h arming delay, and
    # the drop is a separate bar: the stop is checked before the peak advances,
    # so a bar can arm the trail or fire it, never both.
    calm17 = calm + calm[:1]
    rally = [(entry_px, entry_px * 1.03, entry_px, entry_px * 1.03)]
    drop = [(entry_px * 1.03, entry_px * 1.03, entry_px * 0.98, entry_px * 0.98)]
    path = calm17 + rally + drop
    res = run({"X": {"bars_4h": b4, "bars_15m": bars_15m(path, start)}}, p)
    want = entry_px * 1.03 - entry_px * 0.0055
    got = res.trades[0].exit if res.trades else None
    ok &= case("trail exit at peak − 0.55% of entry",
               got is not None and abs(got - want) < 1e-6,
               f"want {want:.4f}, got {got if got is None else round(got, 4)}")
    ok &= case("exit reason is TRAIL",
               bool(res.trades) and res.trades[0].reason == "TRAIL")

    # ── 4. hard stop at −10% when the trail never arms ──
    crash = [(entry_px, entry_px, entry_px * 0.85, entry_px * 0.85)]
    res = run({"X": {"bars_4h": b4, "bars_15m": bars_15m(calm + crash, start)}}, p)
    want = entry_px * 0.90
    got = res.trades[0].exit if res.trades else None
    ok &= case("hard stop fills at −10%",
               got is not None and abs(got - want) < 1e-6,
               f"want {want:.4f}, got {got if got is None else round(got, 4)}")
    ok &= case("exit reason is HARD_STOP",
               bool(res.trades) and res.trades[0].reason == "HARD_STOP")

    # ── 5. a bar that OPENS through the stop fills at the open, not the stop ──
    gap = [(entry_px * 0.80, entry_px * 0.80, entry_px * 0.78, entry_px * 0.79)]
    res = run({"X": {"bars_4h": b4, "bars_15m": bars_15m(calm + gap, start)}}, p)
    got = res.trades[0].exit if res.trades else None
    ok &= case("gap-through fills at the open (not the stop)",
               got is not None and abs(got - entry_px * 0.80) < 1e-6,
               f"want {entry_px*0.80:.4f} (open), got {got if got is None else round(got,4)}; "
               f"stop was {entry_px*0.90:.4f}")

    print("\n" + ("all mechanics verified" if ok else "SELFTEST FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
