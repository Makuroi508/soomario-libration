"""
pull_candles_1m.py — fetch Hyperliquid 1m candles for exit simulation.
══════════════════════════════════════════════════════════════════════
WHY THIS EXISTS
The 4h candles pulled by pull_candles.py are fine for the RSI(14) ENTRY
signal (the live bot evaluates entries only on completed 4h closes), but
they cannot simulate EXITS. The live bot polls every 120s and trails a
0.55% stop; the median 4h bar range is 1.7-3.2%, i.e. 3-6x the trail band,
and ~100% of 4h bars exceed it. So on 4h data the exit is decided by an
intrabar path the OHLC does not contain, and the harness lets winners run
to the bar extreme -- measured at +1.50% mean trail exit vs ~+0.74% gross
live. The error scales with volatility, which flatters exactly the
high-vol coins (TAO) the study exists to catch.

1m is the closest available grid to the live 120s poll (HL offers no 2m).

  python backtest/pull_candles_1m.py                  # live universe + candidates
  python backtest/pull_candles_1m.py --days 180
  python backtest/pull_candles_1m.py SOL TAO          # explicit coins

Writes gzipped CSV per coin to backtest/candles_1m/<COIN>_1m.csv.gz with
columns t,open,high,low,close (t = bar OPEN ms UTC, oldest first).
Gzip because 180d of 1m across 40 coins is ~480MB raw, ~150MB compressed.

Resumable: a coin whose file already exists and covers the requested span
is skipped, so an interrupted run can simply be re-run.
"""
import csv
import gzip
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from config import HL_API_URL, hl_symbol  # noqa: E402

from pull_candles import DIVERSIFICATION_CANDIDATES  # noqa: E402

CANDLE_DIR = Path(__file__).resolve().parent / "candles_1m"
INTERVAL = "1m"
PAGE_SLEEP = 0.10          # be polite; HL pages ~5000 bars (~3.5d) per response
MAX_RETRIES = 4


def _post(req):
    """POST with bounded retry/backoff — a 2000-request run will hit transients."""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(HL_API_URL, json=req, timeout=30)
            r.raise_for_status()
            return r.json() or []
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(0.5 * (2 ** attempt))        # 0.5s, 1s, 2s, 4s
    raise last


def fetch_1m(coin: str, days: int) -> list:
    """Page forward through 1m candles. Returns oldest-first, deduped."""
    sym = hl_symbol(coin)
    end = int(time.time() * 1000)
    start = end - days * 24 * 60 * 60 * 1000
    out, cursor = [], start
    while cursor < end:
        batch = _post({"type": "candleSnapshot",
                       "req": {"coin": sym, "interval": INTERVAL,
                               "startTime": cursor, "endTime": end}})
        if not batch:
            break
        out.extend(batch)
        last_t = int(batch[-1]["t"])
        if last_t <= cursor:                        # no forward progress -> done
            break
        cursor = last_t + 1
        time.sleep(PAGE_SLEEP)
    seen, uniq = set(), []
    for c in sorted(out, key=lambda x: int(x["t"])):
        t = int(c["t"])
        if t not in seen:
            seen.add(t)
            uniq.append({"t": t, "open": float(c["o"]), "high": float(c["h"]),
                         "low": float(c["l"]), "close": float(c["c"])})
    return uniq


def _covers(path: Path, days: int) -> bool:
    """True if an existing file already spans ~the requested window."""
    if not path.exists():
        return False
    try:
        with gzip.open(path, "rt", newline="") as f:
            rows = list(csv.DictReader(f))
        if len(rows) < 1000:
            return False
        span_d = (int(rows[-1]["t"]) - int(rows[0]["t"])) / 86_400_000
        return span_d >= days * 0.95
    except Exception:                               # noqa: BLE001
        return False


def main():
    argv = sys.argv[1:]
    days, args, i = 180, [], 0
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

    coins = [a.upper() for a in args] if args else \
        list(dict.fromkeys(config.COINS + DIVERSIFICATION_CANDIDATES))

    CANDLE_DIR.mkdir(exist_ok=True, parents=True)
    print(f"Pulling {days}d of {INTERVAL} candles for {len(coins)} coins -> {CANDLE_DIR}")
    print(f"(~{days * 1440 // 5000 + 1} requests/coin; resumable — rerun to fill gaps)\n")
    ok = skip = fail = 0
    t0 = time.time()
    for n, coin in enumerate(coins, 1):
        path = CANDLE_DIR / f"{coin.upper()}_1m.csv.gz"
        if _covers(path, days):
            print(f"  [{n:>2}/{len(coins)}] {coin:<10} already complete — skipped")
            skip += 1
            continue
        try:
            rows = fetch_1m(coin, days)
            if not rows:
                print(f"  [{n:>2}/{len(coins)}] {coin:<10} — no data")
                fail += 1
                continue
            tmp = path.with_suffix(".gz.tmp")       # atomic: never leave a half file
            with gzip.open(tmp, "wt", newline="", compresslevel=6) as f:
                w = csv.DictWriter(f, fieldnames=["t", "open", "high", "low", "close"])
                w.writeheader()
                w.writerows(rows)
            tmp.replace(path)
            span = (rows[-1]["t"] - rows[0]["t"]) / 86_400_000
            print(f"  [{n:>2}/{len(coins)}] {coin:<10} {len(rows):>7} bars  "
                  f"{span:>5.1f}d  {path.stat().st_size/1e6:>5.1f}MB  "
                  f"[{time.time()-t0:>5.0f}s]")
            ok += 1
        except Exception as e:                      # noqa: BLE001
            print(f"  [{n:>2}/{len(coins)}] {coin:<10} — FAILED: {e}")
            fail += 1
    print(f"\nDone in {time.time()-t0:.0f}s. {ok} pulled, {skip} skipped, {fail} failed.")


if __name__ == "__main__":
    main()
