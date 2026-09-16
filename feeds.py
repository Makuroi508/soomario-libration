"""
Soomario Libration — per-coin SIGNAL feed
══════════════════════════════════════════
A backtest is tied to the price series it was run on. The Soomario v3 runs this
book reproduces were charted on three different venues — Binance for most, Bybit
for HYPE and SOL, OKX for CRV — so a coin's signal must be computed from the
same venue's candles, not from whichever feed happens to be canonical.

Hyperliquid and Binance agree on the RSI(14) 50/40 cross 96.1% of the time
(measured, 10,665 signals, PnL difference indistinguishable from zero), so for
the untuned coins the venue is close to immaterial. It is NOT known to be
immaterial for the retuned ones — XLM on RSI(5) 20/55, CRV on 50/25, AVAX on
RSI(13) 71/32 — whose crosses sit in places where a few basis points of price
difference move the signal. Matching the venue removes the question instead of
assuming the answer.

All four sources are PUBLIC read-only candle endpoints; none needs a key. Every
fetcher returns the same shape the strategy already consumes:
    {"t": open_ms, "T": close_ms, "o","h","l","c","v": float}   oldest first
with the still-forming final bar left in — signals.closed_candles() drops it.
"""
import logging
import time

import requests

logger = logging.getLogger("feeds")

TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
         "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
         "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000}

# Coins whose ticker differs from "<COIN>USDT" on a given venue.
_SYMBOL_OVERRIDE = {
    "binance": {"KPEPE": "1000PEPEUSDT"},
    "bybit": {"KPEPE": "1000PEPEUSDT"},
    "okx": {"KPEPE": "1000PEPE-USDT-SWAP"},
}
HTTP_TIMEOUT = 12


def _sym(venue: str, coin: str) -> str:
    c = str(coin).split(":", 1)[-1].upper()
    ov = _SYMBOL_OVERRIDE.get(venue, {}).get(c)
    if ov:
        return ov
    return f"{c}-USDT-SWAP" if venue == "okx" else f"{c}USDT"


def _get(url, params):
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
    return r.json()


def _binance(coin, interval, limit):
    rows = _get("https://fapi.binance.com/fapi/v1/klines",
                {"symbol": _sym("binance", coin), "interval": interval,
                 "limit": min(limit, 1500)})
    return [{"t": int(k[0]), "T": int(k[6]) + 1, "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in rows]


_BYBIT_TF = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
             "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
             "1d": "D"}


def _bybit(coin, interval, limit):
    iv = _BYBIT_TF.get(interval)
    if iv is None:
        raise RuntimeError(f"bybit has no {interval} interval")
    d = _get("https://api.bybit.com/v5/market/kline",
             {"category": "linear", "symbol": _sym("bybit", coin),
              "interval": iv, "limit": min(limit, 1000)})
    if str(d.get("retCode")) != "0":
        raise RuntimeError(f"bybit retCode {d.get('retCode')}: {d.get('retMsg')}")
    step = TF_MS[interval]
    out = [{"t": int(k[0]), "T": int(k[0]) + step, "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
           for k in d["result"]["list"]]
    out.sort(key=lambda x: x["t"])            # bybit returns newest-first
    return out


_OKX_TF = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
           "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H", "1d": "1D"}


def _okx(coin, interval, limit):
    iv = _OKX_TF.get(interval)
    if iv is None:
        raise RuntimeError(f"okx has no {interval} interval")
    d = _get("https://www.okx.com/api/v5/market/history-candles",
             {"instId": _sym("okx", coin), "bar": iv, "limit": min(limit, 100)})
    if str(d.get("code")) != "0":
        raise RuntimeError(f"okx code {d.get('code')}: {d.get('msg')}")
    step = TF_MS[interval]
    out = [{"t": int(k[0]), "T": int(k[0]) + step, "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
           for k in d["data"]]
    out.sort(key=lambda x: x["t"])            # okx returns newest-first
    return out


_FETCH = {"binance": _binance, "bybit": _bybit, "okx": _okx}
# One failure should not silently poison the signal: remember the last good
# series per (venue, coin, interval) and serve it if a later poll fails, rather
# than returning [] and making the coin look like it has no history.
_LAST_GOOD = {}


def fetch_candles(venue: str, coin: str, interval: str, limit: int, hl_client=None):
    """Candles for `coin` from `venue`. 'hyperliquid' (or an unknown venue)
    falls through to the Hyperliquid client the worker already holds."""
    v = (venue or "hyperliquid").strip().lower()
    fn = _FETCH.get(v)
    if fn is None:
        if hl_client is None:
            raise RuntimeError(f"no client for signal venue {venue!r}")
        return hl_client.fetch_candles(coin, interval, limit)
    key = (v, str(coin).upper(), interval)
    for attempt in (1, 2):
        try:
            out = fn(coin, interval, limit)
            if out:
                _LAST_GOOD[key] = out
                return out
            raise RuntimeError("empty candle list")
        except Exception as e:                            # noqa: BLE001
            if attempt == 2:
                stale = _LAST_GOOD.get(key)
                logger.warning(f"{v} candles for {coin} {interval} failed: {e}"
                               + (f" — serving the last good {len(stale)} bars"
                                  if stale else " — no cached series to fall back on"))
                return stale or []
            time.sleep(1.0)
