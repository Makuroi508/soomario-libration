"""
pull_candles.py — fetch Hyperliquid 4h candles for the backtest.
════════════════════════════════════════════════════════════════
Run this on a machine that can reach api.hyperliquid.xyz (the cloud
session that built this could NOT — its network policy blocks the venue).

  python backtest/pull_candles.py                      # default coin set + WATCH candidates
  python backtest/pull_candles.py SOL AVAX INJ SEI     # explicit coins
  python backtest/pull_candles.py --days 180           # deeper history (default 180)

Writes one CSV per coin to backtest/candles/<COIN>_4h.csv with columns:
    t,open,high,low,close,volume
where t is the candle OPEN time in ms (UTC), oldest first. backtest.py reads these.

No credentials needed — candleSnapshot is a public (unsigned) endpoint.
"""
import csv
import sys
import time
from pathlib import Path

import requests

# Reuse the exact k-token casing map + universe the live bot uses, so the
# symbols we pull match what the bot actually trades.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from config import HL_API_URL, hl_symbol  # noqa: E402

CANDLE_DIR = Path(__file__).resolve().parent / "candles"
INTERVAL = "4h"

# Candidate coins to evaluate for diversification, on TOP of the current live set.
# Edit this freely — the whole point is to test names that aren't trading yet.
DIVERSIFICATION_CANDIDATES = [
    "INJ", "SEI", "TIA", "ARB", "OP", "APT", "RUNE", "AAVE", "CRV", "LDO",
    "PENDLE", "JUP", "WIF", "kBONK", "kSHIB", "PYTH", "STX", "FIL", "RENDER", "GALA",
]


def fetch_candles(coin: str, days: int) -> list:
    """Pull `days` of 4h candles for `coin`. Returns oldest-first list of dicts."""
    sym = hl_symbol(coin)  # exact HL casing (e.g. kPEPE, not KPEPE)
    end = int(time.time() * 1000)
    start = end - days * 24 * 60 * 60 * 1000
    out = []
    # HL caps candles per response; page backward by shrinking the window if needed.
    cursor = start
    while cursor < end:
        req = {"type": "candleSnapshot",
               "req": {"coin": sym, "interval": INTERVAL, "startTime": cursor, "endTime": end}}
        r = requests.post(HL_API_URL, json=req, timeout=20)
        r.raise_for_status()
        batch = r.json() or []
        if not batch:
            break
        for c in batch:
            out.append({"t": int(c["t"]), "open": float(c["o"]), "high": float(c["h"]),
                        "low": float(c["l"]), "close": float(c["c"]), "volume": float(c["v"])})
        last_t = int(batch[-1]["t"])
        if last_t <= cursor:
            break
        cursor = last_t + 1
        time.sleep(0.15)  # be polite to the API
    # dedupe + sort by open time
    seen, uniq = set(), []
    for c in sorted(out, key=lambda x: x["t"]):
        if c["t"] not in seen:
            seen.add(c["t"]); uniq.append(c)
    return uniq


def main():
    # Strip flags AND their values, so `--days 180` doesn't leave "180"
    # behind as a positional and get fetched as a coin named 180.
    argv = sys.argv[1:]
    days = 180
    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--days":
            days = int(argv[i + 1]); i += 2
        elif a.startswith("--days="):
            days = int(a.split("=", 1)[1]); i += 1
        elif a.startswith("--"):
            i += 1
        else:
            args.append(a); i += 1

    if args:
        coins = [a.upper() for a in args]
    else:
        # current live universe (core + watch) + the diversification candidates
        coins = list(dict.fromkeys(config.COINS + DIVERSIFICATION_CANDIDATES))

    CANDLE_DIR.mkdir(exist_ok=True, parents=True)
    print(f"Pulling {days}d of {INTERVAL} candles for {len(coins)} coins -> {CANDLE_DIR}")
    ok, fail = 0, 0
    for coin in coins:
        try:
            rows = fetch_candles(coin, days)
            if not rows:
                print(f"  {coin:<10} — no data (delisted / wrong symbol?)"); fail += 1; continue
            path = CANDLE_DIR / f"{coin.upper()}_4h.csv"
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["t", "open", "high", "low", "close", "volume"])
                w.writeheader(); w.writerows(rows)
            print(f"  {coin:<10} {len(rows):>5} bars  {rows[0]['t']} .. {rows[-1]['t']}")
            ok += 1
        except Exception as e:
            print(f"  {coin:<10} — FAILED: {e}"); fail += 1
    print(f"\nDone. {ok} ok, {fail} failed. Candles in {CANDLE_DIR}")


if __name__ == "__main__":
    main()
