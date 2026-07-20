"""
backtest.py — Libration strategy replay with the SHARED-ACCOUNT model.
═══════════════════════════════════════════════════════════════════════
Why this exists: a per-coin backtest overstates the edge because it ignores
the real constraint — ONE account, 10 concurrent slots, 20% notional each.
When many coins signal at once they compete for slots; the live bot can only
hold 10. This harness replays the frozen strategy across ALL coins on a single
shared timeline with that cap enforced, so "would more coins help?" is answered
under the same physics the live bot runs under.

Strategy replayed (frozen — see config.py / README):
  • RSI(14) Wilder on completed 4h closes; long = cross up 50, short = cross down 40
  • trailing stop 0.55% (arms +0.55%, trails behind peak), hard stop 10%
  • 20% notional/position, 2x leverage, max 10 concurrent, 1 position/coin
  • 5% daily-DD halt (UTC day), round-trip friction assumption (default 0.5%)

Usage (run from repo root, after backtest/pull_candles.py has written candles/):
  python backtest/backtest.py                      # current live universe
  python backtest/backtest.py --coins SOL,AVAX,INJ,SEI,TIA
  python backtest/backtest.py --notional 0.10      # thinner sizing -> 20 slots
  python backtest/backtest.py --compare            # A/B: current vs +candidates, and sizing sweep

INTRABAR FILL MODEL (documented approximation): the live bot polls every 120s;
the backtest only has 4h OHLC. Per bar we (1) update the peak with the bar's
high/low, (2) arm/advance the trail, then (3) check whether the effective stop
was touched using the bar's low (long) / high (short), filling at the stop level.
Hard stop is checked against the same extreme. Entries fill at the signal bar's
CLOSE (matching the live "enter on completed-bar close" rule). This is slightly
optimistic on trail exits and slightly pessimistic on gaps; validate against the
live fills (--validate) to calibrate before trusting absolute numbers.
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import wilder_rsi, entry_signal  # noqa: E402  (reuse the LIVE signal code)

CANDLE_DIR = Path(__file__).resolve().parent / "candles"

# Frozen defaults (mirror config.py so the backtest == live mechanics)
RSI_LEN, LONG_LEVEL, SHORT_LEVEL = 14, 50.0, 40.0
TRAIL_PCT, HARD_STOP_PCT, DAILY_DD_PCT = 0.55, 10.0, 5.0
LEVERAGE, NOTIONAL_FRAC = 2.0, 0.20
FRICTION_PCT = 0.5           # round-trip friction assumption (handoff §2)
START_EQUITY = 1000.0

LIVE_CORE = ["DOT", "WLD", "LINK", "TAO", "JTO", "HYPE", "kPEPE", "SUI", "SOL",
             "NEAR", "ADA", "ENA", "BCH", "AVAX", "ZEC"]
LIVE_WATCH = ["ATOM", "XMR", "AAVE", "ONDO", "FARTCOIN", "PENGU"]


def load_candles(coin: str) -> list:
    path = CANDLE_DIR / f"{coin.upper()}_4h.csv"
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append({"t": int(r["t"]), "open": float(r["open"]), "high": float(r["high"]),
                         "low": float(r["low"]), "close": float(r["close"])})
    return sorted(rows, key=lambda x: x["t"])


class Position:
    __slots__ = ("coin", "side", "entry", "qty", "notional", "peak", "hard_stop",
                 "trail_active", "trail_stop", "opened_t")

    def __init__(self, coin, side, entry, qty, notional, t):
        self.coin, self.side, self.entry, self.qty = coin, side, entry, qty
        self.notional, self.opened_t = notional, t
        self.peak = entry
        self.trail_active, self.trail_stop = False, None
        hs = HARD_STOP_PCT / 100.0
        self.hard_stop = entry * (1 - hs) if side == "long" else entry * (1 + hs)


def run(coins, notional_frac=NOTIONAL_FRAC, leverage=LEVERAGE, friction_pct=FRICTION_PCT,
        trail_pct=TRAIL_PCT, hard_stop_pct=HARD_STOP_PCT, verbose=False):
    """Replay the shared-account strategy. Returns (stats, trades)."""
    max_concurrent = int(leverage / notional_frac + 1e-9)

    # Load candles + precompute RSI per coin, indexed by bar-open timestamp.
    series = {}
    all_ts = set()
    for coin in coins:
        candles = load_candles(coin)
        if len(candles) < RSI_LEN + 3:
            continue
        closes = [c["close"] for c in candles]
        rsi = wilder_rsi(closes, RSI_LEN)
        by_t = {c["t"]: (c, rsi[i], rsi[i - 1] if i > 0 else None)
                for i, c in enumerate(candles)}
        series[coin] = by_t
        all_ts.update(by_t.keys())
    if not series:
        raise SystemExit("No candle CSVs found. Run backtest/pull_candles.py first.")

    timeline = sorted(all_ts)
    equity = START_EQUITY
    realized = 0.0
    open_pos = {}                 # coin -> Position
    trades = []
    misses = {"concurrency_full": 0, "daily_halt": 0}
    slot_usage = []               # open count sampled each bar (for saturation stats)
    day_baseline = equity
    cur_day = None
    halted = False

    for t in timeline:
        day = t // (24 * 60 * 60 * 1000)
        if day != cur_day:        # UTC-day rollover: reset the daily-DD halt
            cur_day, day_baseline, halted = day, equity, False

        # ── 1. manage open positions on this bar (trail + stops) ──
        for coin in list(open_pos.keys()):
            if coin not in series or t not in series[coin]:
                continue
            c = series[coin][t][0]
            p = open_pos[coin]
            is_long = p.side == "long"
            # update peak with the bar extreme
            p.peak = max(p.peak, c["high"]) if is_long else min(p.peak, c["low"])
            # arm trail
            if not p.trail_active:
                armed = (c["high"] >= p.entry * (1 + trail_pct / 100)) if is_long \
                    else (c["low"] <= p.entry * (1 - trail_pct / 100))
                if armed:
                    p.trail_active = True
            # effective stop = max(hard, trail) long / min(hard, trail) short
            band = p.entry * (trail_pct / 100)
            if p.trail_active:
                p.trail_stop = (p.peak - band) if is_long else (p.peak + band)
                eff = max(p.trail_stop, p.hard_stop) if is_long else min(p.trail_stop, p.hard_stop)
            else:
                eff = p.hard_stop
            # did the bar touch the stop?
            hit = (c["low"] <= eff) if is_long else (c["high"] >= eff)
            if hit:
                _book(trades, p, eff, friction_pct, t, "TRAIL" if p.trail_active else "HARD_STOP")
                realized += trades[-1]["pnl"]
                equity = START_EQUITY + realized
                del open_pos[coin]

        # ── 2. daily-DD halt check ──
        if not halted and day_baseline > 0:
            dd = (day_baseline - equity) / day_baseline * 100
            if dd >= DAILY_DD_PCT:
                halted = True

        # ── 3. entries (evaluate the cross on this freshly-closed bar) ──
        for coin in coins:
            if coin not in series or t not in series[coin]:
                continue
            if coin in open_pos:
                continue                      # pyramiding 1
            c, rsi_now, rsi_prev = series[coin][t]
            sig = entry_signal(rsi_prev, rsi_now, LONG_LEVEL, SHORT_LEVEL)
            if not sig:
                continue
            if halted:
                misses["daily_halt"] += 1; continue
            if len(open_pos) >= max_concurrent:
                misses["concurrency_full"] += 1; continue
            notional = notional_frac * equity
            entry_px = c["close"]
            qty = notional / entry_px
            open_pos[coin] = Position(coin, sig, entry_px, qty, notional, t)

        slot_usage.append(len(open_pos))

    # close out any still-open at last close (mark-out, no friction double-count)
    for coin, p in open_pos.items():
        last_t = max(series[coin])
        _book(trades, p, series[coin][last_t][0]["close"], friction_pct, last_t, "MARKOUT")
        realized += trades[-1]["pnl"]

    stats = _summarize(trades, realized, misses, slot_usage, max_concurrent, coins)
    return stats, trades


def _book(trades, p, fill_px, friction_pct, t, reason):
    is_long = p.side == "long"
    move = (fill_px - p.entry) if is_long else (p.entry - fill_px)
    ret_pct = move / p.entry * 100
    net_pct = ret_pct - friction_pct              # round-trip friction
    pnl = p.notional * net_pct / 100
    trades.append({"coin": p.coin, "side": p.side, "entry": p.entry, "exit": fill_px,
                   "ret_pct": ret_pct, "net_pct": net_pct, "pnl": pnl,
                   "reason": reason, "opened_t": p.opened_t, "closed_t": t})


def _summarize(trades, realized, misses, slot_usage, cap, coins):
    n = len(trades)
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    hard = sum(1 for t in trades if t["reason"] == "HARD_STOP")
    per_coin = {}
    for t in trades:
        d = per_coin.setdefault(t["coin"], {"n": 0, "pnl": 0.0, "wins": 0, "hard": 0})
        d["n"] += 1; d["pnl"] += t["pnl"]
        d["wins"] += t["net_pct"] > 0; d["hard"] += t["reason"] == "HARD_STOP"
    avg_slots = sum(slot_usage) / len(slot_usage) if slot_usage else 0
    full_pct = 100 * sum(1 for s in slot_usage if s >= cap) / len(slot_usage) if slot_usage else 0
    return {
        "coins": len(coins), "trades": n, "win_rate": 100 * wins / n if n else 0,
        "realized": realized, "avg_pnl": realized / n if n else 0,
        "hard_stops": hard, "final_equity": START_EQUITY + realized,
        "return_pct": realized / START_EQUITY * 100,
        "misses": misses, "avg_slots": avg_slots, "cap": cap, "full_pct": full_pct,
        "per_coin": per_coin,
    }


def _print_stats(label, s):
    print(f"\n─── {label} ───")
    print(f"  coins={s['coins']}  slots(cap)={s['cap']}  trades={s['trades']}  "
          f"win={s['win_rate']:.1f}%  hard_stops={s['hard_stops']}")
    print(f"  realized=${s['realized']:.2f} ({s['return_pct']:+.1f}%)  "
          f"avg=${s['avg_pnl']:.3f}/trade  final=${s['final_equity']:.2f}")
    print(f"  slot use: mean {s['avg_slots']:.1f}/{s['cap']}, full {s['full_pct']:.0f}% of bars  "
          f"| misses: {s['misses']}")


def _print_per_coin(s):
    print(f"  {'coin':<10}{'trades':>7}{'win%':>6}{'hard':>5}{'pnl':>9}{'avg':>8}")
    for c, d in sorted(s["per_coin"].items(), key=lambda kv: -kv[1]["pnl"]):
        print(f"  {c:<10}{d['n']:>7}{100*d['wins']/d['n']:>5.0f}%{d['hard']:>5}"
              f"{d['pnl']:>9.2f}{d['pnl']/d['n']:>8.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", help="comma-separated; default = live core+watch")
    ap.add_argument("--notional", type=float, default=NOTIONAL_FRAC)
    ap.add_argument("--friction", type=float, default=FRICTION_PCT)
    ap.add_argument("--compare", action="store_true", help="run the diversification A/B sweep")
    ap.add_argument("--per-coin", action="store_true")
    args = ap.parse_args()

    live = LIVE_CORE + LIVE_WATCH
    if args.compare:
        # Which candle CSVs actually exist?
        have = {p.stem.replace("_4h", "").upper() for p in CANDLE_DIR.glob("*_4h.csv")}
        live_have = [c for c in live if c.upper() in have]
        extra = sorted(have - {c.upper() for c in live})
        print(f"Candles available: {len(have)} coins. Live set present: {len(live_have)}/{len(live)}.")
        print(f"Candidate (non-live) coins present: {extra}")

        base, _ = run(live_have, notional_frac=0.20, friction_pct=args.friction)
        _print_stats("CURRENT live universe @ 20% notional (10 slots)", base)

        wide, _ = run(live_have + extra, notional_frac=0.20, friction_pct=args.friction)
        _print_stats("WIDER pool (live + candidates) @ 20% notional (10 slots)", wide)

        thin, _ = run(live_have + extra, notional_frac=0.10, friction_pct=args.friction)
        _print_stats("WIDER pool @ 10% notional (20 slots) — true diversification", thin)

        nolosers = [c for c in live_have if c.upper() != "TAO"]
        cut, _ = run(nolosers + extra, notional_frac=0.10, friction_pct=args.friction)
        _print_stats("WIDER pool minus TAO @ 10% notional (20 slots)", cut)
        return

    coins = [c.strip() for c in args.coins.split(",")] if args.coins else live
    s, trades = run(coins, notional_frac=args.notional, friction_pct=args.friction)
    _print_stats(f"universe ({len(coins)} coins) @ {args.notional:.0%} notional", s)
    if args.per_coin:
        _print_per_coin(s)


if __name__ == "__main__":
    main()
