#!/usr/bin/env python3
"""
fetch_candles.py — pull the candle history the Libration-on-stocks backtest needs.

Run this from a machine with outbound internet (the Claude Code web sandbox is
egress-restricted and cannot reach api.hyperliquid.xyz). It writes one JSON file
per symbol into --out, which libration_bt.py then reads offline.

    python3 backtest/fetch_candles.py --days 180 --out backtest/data

Two series are pulled per symbol, because the strategy needs both:

  4h  — entry signals. RSI(14) is evaluated only on completed 4h closes.
  15m — exit simulation. The trailing stop is 0.55%; a 4h bar on these names
        routinely ranges 1.5%+, so resolving the trail on 4h OHLC alone would be
        meaningless. 15m is the finest granularity that keeps the download sane.

Hyperliquid lists the Rotation universe as stock perps under an `xyz:` prefix.
That is the right venue to test: unlike NYSE, HL stock perps trade continuously,
so the 4h bar grid and the 0.55% trail behave the way they do on the crypto book
Libration already runs. Names HL does not list are reported and skipped.
"""
import argparse
import json
import os
import time

import requests

HL_API = "https://api.hyperliquid.xyz/info"

# Rotation's tradeable universe: rotation_scorer.CORE_HOLD | MAG_UNIVERSE.
UNIVERSE = [
    "TSLA",                                   # CORE_HOLD
    "NVDA", "PLTR", "GOOGL", "MU", "TSM",
    "MSFT", "AMZN", "AAPL",
    "CRCL", "MSTR",
    "MRVL", "ARM", "AVGO",
    "SPCX",
    "AMD",
]

INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
# HL caps candleSnapshot at 5000 candles per response; page below that.
PAGE = 4000


def fetch_page(coin, interval, start_ms, end_ms, retries=4):
    body = {"type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": start_ms, "endTime": end_ms}}
    for attempt in range(retries):
        try:
            r = requests.post(HL_API, json=body, timeout=30)
            if r.status_code == 200:
                raw = r.json()
                return raw if isinstance(raw, list) else []
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return []
        except Exception:
            time.sleep(2 ** attempt)
    return []


def fetch_series(coin, interval, days):
    """Page backwards over `days` of history, oldest-first, de-duplicated."""
    step = INTERVAL_MS[interval]
    now = int(time.time() * 1000)
    start = now - days * 86_400_000
    out, cursor = {}, start
    while cursor < now:
        end = min(cursor + PAGE * step, now)
        page = fetch_page(coin, interval, cursor, end)
        if not page:
            # A gap (delisted stretch, or no history that far back) is not fatal;
            # step past it rather than spinning.
            cursor = end
            continue
        for c in page:
            try:
                out[int(c["t"])] = {"t": int(c["t"]), "T": int(c["T"]),
                                    "o": float(c["o"]), "h": float(c["h"]),
                                    "l": float(c["l"]), "c": float(c["c"]),
                                    "v": float(c.get("v", 0.0))}
            except (KeyError, TypeError, ValueError):
                continue
        cursor = max(int(page[-1]["t"]) + step, end)
        time.sleep(0.15)          # be polite to the public endpoint
    return [out[k] for k in sorted(out)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--out", default="backtest/data")
    ap.add_argument("--symbols", default=",".join(UNIVERSE))
    ap.add_argument("--prefix", default="xyz:",
                    help="HL coin prefix. 'xyz:' for stock perps; pass --prefix '' "
                         "to pull Libration's own crypto coins for the control run.")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    missing, wrote = [], []

    for sym in symbols:
        coin = f"{args.prefix}{sym}"
        sig = fetch_series(coin, "4h", args.days)
        if len(sig) < 60:
            missing.append(sym)
            print(f"  {sym:6} SKIP — HL returned {len(sig)} 4h candles for {coin} "
                  f"(not listed?)")
            continue
        exe = fetch_series(coin, "15m", args.days)
        path = os.path.join(args.out, f"{sym}.json")
        with open(path, "w") as fh:
            json.dump({"symbol": sym, "hl_coin": coin,
                       "bars_4h": sig, "bars_15m": exe}, fh)
        wrote.append(sym)
        print(f"  {sym:6} ok — {len(sig):5} x 4h, {len(exe):6} x 15m "
              f"({sig[0]['t']} .. {sig[-1]['t']})")

    print(f"\nwrote {len(wrote)} symbols to {args.out}")
    if missing:
        print(f"not on Hyperliquid, no data: {', '.join(missing)}")
        print("Those need a different source (yfinance 1h, resampled) if you want them in.")


if __name__ == "__main__":
    main()
