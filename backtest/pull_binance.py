"""
pull_binance.py — deep 1m history from Binance (fallback Bybit), resampled to 4h.
═════════════════════════════════════════════════════════════════════════════════
WHY NOT HYPERLIQUID
HL's candleSnapshot serves only the most recent ~5000 sub-4h bars and returns
EMPTY for historical sub-4h windows -- 1m history caps at ~3.5 days, which is
far too little to simulate a 0.55% trailing stop over a meaningful span.
Binance and Bybit DO honor a historical startTime at 1m, so full 180d of 1m
is reachable by paging. Verified 2026-07-20.

CAVEAT (matters for interpretation): these are Binance/Bybit perp prices, not
Hyperliquid's. Basis and microstructure differ, so exact stop-trigger instants
will differ from live HL fills. For a STRATEGY comparison (close-only exits vs
intrabar trailing) that is fine -- both variants are priced off the same series,
so the comparison is apples-to-apples even if absolute levels shift slightly.

The 4h series is RESAMPLED FROM THE SAME 1m BARS rather than fetched separately,
so the entry timeframe and the exit timeframe can never disagree.

  python backtest/pull_binance.py --days 180
  python backtest/pull_binance.py SOL TAO --days 180

Writes backtest/candles_bn/<COIN>_1m.csv.gz and <COIN>_4h.csv.gz
(columns t,open,high,low,close,volume; t = bar OPEN ms UTC, oldest first).
Resumable: complete coins are skipped, so an interrupted run can be re-run.
"""
import csv
import gzip
import sys
import time
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "candles_bn"
BINANCE = "https://fapi.binance.com/fapi/v1/klines"
BYBIT = "https://api.bybit.com/v5/market/kline"
MINUTE_MS = 60_000
BAR_4H_MS = 4 * 60 * 60 * 1000
PAGE = 1000
MAX_RETRIES = 5

LIVE = ["DOT", "WLD", "LINK", "TAO", "JTO", "HYPE", "kPEPE", "SUI", "SOL", "NEAR",
        "ADA", "ENA", "BCH", "AVAX", "ZEC", "ATOM", "XMR", "AAVE", "ONDO",
        "FARTCOIN", "PENGU"]


def symbol(coin: str) -> str:
    """HL k-prefix (kPEPE = 1000 PEPE) maps to Binance/Bybit 1000-prefix."""
    c = coin.strip()
    return ("1000" + c[1:].upper() + "USDT") if c.startswith("k") else c.upper() + "USDT"


def _get(url, params):
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code in (418, 429):            # rate limited -> back off hard
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:                          # noqa: BLE001
            last = e
            time.sleep(0.4 * (2 ** attempt))
    raise last


def fetch_binance(sym, start, end):
    out, cursor = [], start
    while cursor < end:
        d = _get(BINANCE, {"symbol": sym, "interval": "1m",
                           "startTime": cursor, "endTime": end, "limit": PAGE})
        if not d:
            break
        for k in d:
            out.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                        float(k[4]), float(k[5])))
        nxt = int(d[-1][0]) + MINUTE_MS
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.06)
    return out


def fetch_bybit(sym, start, end):
    out, cursor = [], start
    while cursor < end:
        d = _get(BYBIT, {"category": "linear", "symbol": sym, "interval": "1",
                         "start": cursor, "end": end, "limit": PAGE})
        rows = (d.get("result") or {}).get("list") or []
        if not rows:
            break
        rows = sorted(rows, key=lambda r: int(r[0]))    # bybit returns newest-first
        for k in rows:
            out.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                        float(k[4]), float(k[5])))
        nxt = int(rows[-1][0]) + MINUTE_MS
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.06)
    return out


def dedupe(rows):
    seen, out = set(), []
    for r in sorted(rows, key=lambda x: x[0]):
        if r[0] not in seen:
            seen.add(r[0]); out.append(r)
    return out


def resample_4h(m1):
    """Aggregate 1m bars into 4h bars aligned to the UTC 4h grid (00:00,04:00,...).

    o = first open, h = max high, l = min low, c = last close, v = sum.
    Built from the SAME 1m series the exits are simulated on, so the entry
    timeframe and exit timeframe are guaranteed consistent.
    """
    buckets = {}
    for t, o, h, l, c, v in m1:
        b = (t // BAR_4H_MS) * BAR_4H_MS
        g = buckets.get(b)
        if g is None:
            buckets[b] = [t, o, h, l, c, v, t]          # +last_t tracker
        else:
            if t < g[0]:
                g[0], g[1] = t, o
            g[2] = max(g[2], h)
            g[3] = min(g[3], l)
            if t >= g[6]:
                g[4], g[6] = c, t
            g[5] += v
    return [(b, g[1], g[2], g[3], g[4], g[5]) for b, g in sorted(buckets.items())]


def write(path, rows):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", newline="", compresslevel=6) as f:
        w = csv.writer(f)
        w.writerow(["t", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    tmp.replace(path)


def complete(path, days, interval_ms):
    if not path.exists():
        return False
    try:
        with gzip.open(path, "rt", newline="") as f:
            rows = list(csv.reader(f))[1:]
        if len(rows) < 100:
            return False
        span = (int(rows[-1][0]) - int(rows[0][0])) / 86_400_000
        return span >= days * 0.95
    except Exception:                                   # noqa: BLE001
        return False


def last_bar(path):
    """Open-ms of the final 1m bar already stored, or None."""
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", newline="") as f:
            rows = list(csv.reader(f))[1:]
        return int(rows[-1][0]) if rows else None
    except Exception:                                   # noqa: BLE001
        return None


def update(coin, out_1m, out_4h):
    """Append only bars newer than what is stored. Cheap way to bring a stale
    cache current -- a session that resumes days later would otherwise be
    backtesting against data that ends before the trades it is validating."""
    prev = last_bar(out_1m)
    if prev is None:
        return None
    with gzip.open(out_1m, "rt", newline="") as f:
        old = [tuple(r) for r in list(csv.reader(f))[1:]]
    old = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
           for r in old]
    end = int(time.time() * 1000)
    if end - prev < 2 * MINUTE_MS:
        return 0
    sym = symbol(coin)
    fresh = []
    for fn in (fetch_binance, fetch_bybit):
        try:
            fresh = fn(sym, prev + MINUTE_MS, end)
            if fresh:
                break
        except Exception:                               # noqa: BLE001
            continue
    if not fresh:
        return 0
    merged = dedupe(old + fresh)
    write(out_1m, merged)
    write(out_4h, resample_4h(merged))
    return len(merged) - len(old)


def main():
    argv = sys.argv[1:]
    days, args, i, do_update = 180, [], 0, False
    while i < len(argv):
        a = argv[i]
        if a == "--days":
            days = int(argv[i + 1]); i += 2
        elif a.startswith("--days="):
            days = int(a.split("=", 1)[1]); i += 1
        elif a == "--update":
            do_update = True; i += 1
        elif a.startswith("--"):
            i += 1
        else:
            args.append(a); i += 1
    coins = args or LIVE

    if do_update:
        OUT.mkdir(parents=True, exist_ok=True)
        have = sorted({p.name.replace("_1m.csv.gz", "")
                       for p in OUT.glob("*_1m.csv.gz")})
        coins = args or have
        print(f"Updating {len(coins)} cached coins to now\n")
        t0 = time.time()
        for n, c in enumerate(coins, 1):
            p1 = OUT / f"{c.upper()}_1m.csv.gz"
            p4 = OUT / f"{c.upper()}_4h.csv.gz"
            added = update(c, p1, p4)
            if added is None:
                print(f"  [{n:>2}/{len(coins)}] {c:<10} no cache — use a full pull")
            else:
                span = (last_bar(p1) - 0)
                print(f"  [{n:>2}/{len(coins)}] {c:<10} +{added:>6} bars  "
                      f"through {time.strftime('%Y-%m-%d %H:%M', time.gmtime(span/1000))}"
                      f"  [{time.time()-t0:>4.0f}s]")
        print(f"\nUpdate done in {time.time()-t0:.0f}s.")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    print(f"Pulling {days}d of 1m from Binance (Bybit fallback) for {len(coins)} coins")
    print(f"-> {OUT}   (~{days*1440//PAGE} requests/coin; resumable)\n")

    t0, ok, skip, fail = time.time(), 0, 0, 0
    for n, coin in enumerate(coins, 1):
        p1 = OUT / f"{coin.upper()}_1m.csv.gz"
        p4 = OUT / f"{coin.upper()}_4h.csv.gz"
        if complete(p1, days, MINUTE_MS) and p4.exists():
            print(f"  [{n:>2}/{len(coins)}] {coin:<10} complete — skipped"); skip += 1
            continue
        sym = symbol(coin)
        rows, src = [], ""
        for name, fn in (("binance", fetch_binance), ("bybit", fetch_bybit)):
            try:
                rows = dedupe(fn(sym, start, end))
                if len(rows) > 1000:
                    src = name
                    break
            except Exception as e:                      # noqa: BLE001
                print(f"      {coin} {name} failed: {str(e)[:60]}")
        if not rows:
            print(f"  [{n:>2}/{len(coins)}] {coin:<10} — NO DATA"); fail += 1
            continue
        bars4 = resample_4h(rows)
        write(p1, rows); write(p4, bars4)
        span = (rows[-1][0] - rows[0][0]) / 86_400_000
        print(f"  [{n:>2}/{len(coins)}] {coin:<10} {len(rows):>7} 1m + {len(bars4):>5} 4h  "
              f"{span:>5.1f}d  {src:<8} {p1.stat().st_size/1e6:>5.1f}MB  [{time.time()-t0:>5.0f}s]")
        ok += 1
    print(f"\nDone in {time.time()-t0:.0f}s. {ok} pulled, {skip} skipped, {fail} failed.")


if __name__ == "__main__":
    main()
