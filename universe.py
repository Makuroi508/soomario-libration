"""
Soomario Libration — Universe filter
══════════════════════════════════════
Codifies the selection rule (spec §2): keep liquid, volatile alt perps; drop
slow large-caps where the thin edge gets eaten by friction. A coin is INCLUDED
only if BOTH hold:
  • avg daily true-range % over the last ~30d  >= MIN_ATR_PCT   (volatility)
  • 24h notional volume                         >= MIN_DAILY_VOL_USD (liquidity)

The volatile set carries the slippage cushion; the slow names do not. Re-run
quarterly:  python universe.py

This is a maintenance tool, not a per-tick gate. The default COINS list is
already curated, so UNIVERSE_AUTOFILTER is OFF by default — at boot the report
is logged for visibility but the live set is NOT silently changed (a quiet-day
volume dip must never drop a validated coin mid-trial).
"""
import logging

import requests

import config
from config import HL_API_URL, MIN_ATR_PCT, MIN_DAILY_VOL_USD, hl_symbol

logger = logging.getLogger("universe")

EXCLUDE = {"BTC", "ETH", "BNB", "XRP", "XLM", "TRX"}  # slow large-caps (spec §2)


def avg_daily_atr_pct(candles: list, period: int = 30) -> float:
    """Mean daily true-range as % of close over the last `period` daily candles."""
    if len(candles) < 2:
        return 0.0
    rows = candles[-(period + 1):]
    trs = []
    for i in range(1, len(rows)):
        h, l, pc, c = rows[i]["h"], rows[i]["l"], rows[i - 1]["c"], rows[i]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if c:
            trs.append(tr / c * 100)
    return sum(trs) / len(trs) if trs else 0.0


def _day_volume_map() -> dict:
    """24h notional volume per main-perp coin via metaAndAssetCtxs (no signing)."""
    try:
        resp = requests.post(HL_API_URL, json={"type": "metaAndAssetCtxs"}, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"metaAndAssetCtxs HTTP {resp.status_code}")
            return {}
        meta, ctxs = resp.json()
        names = [a["name"] for a in meta.get("universe", [])]
        out = {}
        for name, ctx in zip(names, ctxs):
            try:
                out[name.upper()] = float(ctx.get("dayNtlVlm", 0) or 0)
            except (TypeError, ValueError):
                out[name.upper()] = 0.0
        return out
    except Exception as e:
        logger.warning(f"day-volume fetch failed: {e}")
        return {}


def evaluate(client, candidates=None, min_atr_pct=None, min_vol_usd=None):
    """Return (included, report). `report` lists each candidate with its ATR%,
    24h volume, and pass/fail reasons. Pure read-only."""
    candidates = candidates or config.COINS
    min_atr_pct = MIN_ATR_PCT if min_atr_pct is None else min_atr_pct
    min_vol_usd = MIN_DAILY_VOL_USD if min_vol_usd is None else min_vol_usd

    vol_map = _day_volume_map()
    included, report = [], []
    for coin in candidates:
        sym = hl_symbol(coin).upper()
        reasons = []
        if sym in EXCLUDE:
            reasons.append("excluded large-cap")
        candles = client.fetch_candles(coin, "1d", 35)
        atr = avg_daily_atr_pct(candles, 30)
        vol = vol_map.get(sym, 0.0)
        if atr < min_atr_pct:
            reasons.append(f"ATR% {atr:.2f} < {min_atr_pct}")
        if vol < min_vol_usd:
            reasons.append(f"vol ${vol/1e6:.1f}M < ${min_vol_usd/1e6:.0f}M")
        ok = not reasons
        if ok:
            included.append(coin)
        report.append({"coin": sym, "atr_pct": round(atr, 2),
                       "day_vol_usd": round(vol, 0), "include": ok,
                       "reasons": reasons})
    return included, report


def log_report(report):
    logger.info("─" * 56)
    logger.info(f"  UNIVERSE  (ATR% >= {MIN_ATR_PCT}, vol >= ${MIN_DAILY_VOL_USD/1e6:.0f}M)")
    for r in report:
        flag = "✓" if r["include"] else "✗"
        why = "" if r["include"] else "  — " + "; ".join(r["reasons"])
        logger.info(f"   {flag} {r['coin']:<6} ATR {r['atr_pct']:>5.2f}%  "
                    f"vol ${r['day_vol_usd']/1e6:>7.1f}M{why}")
    logger.info("─" * 56)


if __name__ == "__main__":
    from utils import setup_logging
    from hl_client import HLClient
    setup_logging()
    inc, rep = evaluate(HLClient())
    log_report(rep)
    print("\nINCLUDED:", ",".join(hl_symbol(c).upper() for c in inc))
    dropped = [r["coin"] for r in rep if not r["include"]]
    if dropped:
        print("DROPPED :", ",".join(dropped))
