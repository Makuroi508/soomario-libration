"""
Soomario Libration — Propr client
══════════════════════════════════
Drop-in replacement for HLClient when EXCHANGE=propr. Implements exactly the
surface Libration calls, so position_manager / exit_manager / shadow / app
need no changes:

    get_positions, get_position, get_price, get_all_prices, get_equity,
    market_open, market_close, place_stop_market, modify_stop, cancel_order,
    set_leverage, fetch_candles, get_user_fills, init_sdk, signing_works

MARKET DATA STILL COMES FROM HYPERLIQUID.
Propr exposes no candle or price endpoints, and it settles on Hyperliquid, so
HL's public info endpoint IS the correct market data for a Propr account. We
delegate candles/prices to a credential-free HLClient. No signing, no keys.

WHAT PROPR DOES DIFFERENTLY FROM HL (and the code that handles it)
  1. accountId scopes every trading call: /accounts/{id}/...
  2. Order ids are URNs ('urn:prp-order:...'), not ints.
  3. Conditional orders need a positionId, so a stop can only be placed AFTER
     the entry has filled and the position exists. _position_id() resolves it.
  4. positionSide must agree with side: buy->long, sell->short, or 13096.
  5. reduceOnly=true is mandatory on closes — a bare sell opens a short.
  6. Cancel returns 201 on success; 400 means already filled/cancelled (fine).
  7. There is no balance endpoint. Equity comes from the challenge attempt,
     which is also the ledger Propr uses to judge a breach — so the guard and
     the breach engine read the same number.
  8. Fees are 0.075% both sides, vs 0.045% taker on HL.

FIELD NAMES MARKED `# VERIFY` are inferred from the docs' prose rather than a
sample payload. _log_unmapped() prints the raw keys on first call so the first
boot tells you what to correct.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional

import requests

import config
from config import COINS, hl_symbol, short_name

logger = logging.getLogger("propr_client")

BASE = os.getenv("PROPR_API_BASE", "https://api.propr.xyz/v1").rstrip("/")
CLOSE_SLIPPAGE = float(os.getenv("CLOSE_SLIPPAGE", "0.02"))
_TIMEOUT = 20


def _ulid() -> str:
    """ULID-shaped unique id for intentId. Crockford base32, time-ordered."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    ts, out = int(time.time() * 1000), ""
    for _ in range(10):
        ts, rem = divmod(ts, 32)
        out = alphabet[rem] + out
    rand = uuid.uuid4().int
    for _ in range(16):
        rand, rem = divmod(rand, 32)
        out += alphabet[rem]
    return out


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class ProprClient:
    def __init__(self, hl_reader=None):
        self.api_key = os.getenv("PROPR_API_KEY", "").strip()
        self.account_id = os.getenv("PROPR_ACCOUNT_ID", "").strip()
        self.attempt_id = os.getenv("PROPR_ATTEMPT_ID", "").strip()
        self.asset_meta = {}
        self.last_open_error = None
        self._margin_cfg_ids = {}
        self._logged_shapes = set()
        self._initial_balance = None

        # Market data from HL, read-only. Import here so a missing HL key can
        # never take the Propr service down at import time.
        if hl_reader is None:
            from hl_client import HLClient
            hl_reader = HLClient()
        self.hl = hl_reader

        if not self.api_key:
            raise SystemExit("PROPR_API_KEY is not set")
        if not self.account_id:
            raise SystemExit("PROPR_ACCOUNT_ID is not set — refusing to "
                             "auto-discover with multiple active attempts")

    # ── HTTP ────────────────────────────────────────────────────
    def _req(self, method: str, path: str, **kw) -> Optional[dict]:
        url = f"{BASE}{path}"
        headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
        for attempt in range(4):
            try:
                r = requests.request(method, url, headers=headers, timeout=_TIMEOUT, **kw)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code == 401:
                    # A dead key must never rot silently again.
                    from utils import tg_notify
                    tg_notify("🚨 PROPR AUTH FAILED (401). The bot cannot trade. "
                              "Regenerate the key in Settings.", level="warn")
                    logger.error("401 from Propr — API key invalid or revoked")
                    return None
                if r.status_code in (200, 201):
                    return r.json()
                if r.status_code == 400 and "/cancel" in path:
                    return {"_already_gone": True}      # filled/cancelled/expired
                logger.error(f"{method} {path} -> {r.status_code} {r.text[:300]}")
                return None
            except requests.RequestException as e:
                if attempt == 3:
                    logger.error(f"{method} {path} failed: {e}")
                    return None
                time.sleep(2 ** attempt)
        return None

    def _log_unmapped(self, tag: str, obj: dict):
        if tag not in self._logged_shapes and isinstance(obj, dict):
            self._logged_shapes.add(tag)
            logger.info(f"[shape:{tag}] keys={sorted(obj.keys())}")

    # ── boot ────────────────────────────────────────────────────
    def init_sdk(self) -> bool:
        """Verify key + account, confirm the attempt is active, seed asset meta."""
        me = self._req("GET", "/users/me")
        if not me:
            return False
        attempts = (self._req("GET", "/challenge-attempts",
                              params={"status": "active"}) or {}).get("data", [])
        mine = [a for a in attempts if a.get("accountId") == self.account_id]
        if not mine:
            logger.error(f"account {self.account_id} is not among {len(attempts)} "
                         f"active attempts — refusing to start")
            return False
        a = mine[0]
        self._log_unmapped("attempt", a)
        self._log_unmapped("attempt.account", a.get("account") or {})
        self._log_unmapped("attempt.challenge", a.get("challenge") or {})
        # ALWAYS take the id from the API. PROPR_ATTEMPT_ID is a convenience, not
        # the source of truth: a challengeId pasted in by mistake looks valid but
        # 404s on every equity read, which silently disables the daily guard.
        discovered = a.get("attemptId") or a.get("id") or ""
        if discovered and discovered != self.attempt_id:
            if self.attempt_id:
                logger.warning(f"PROPR_ATTEMPT_ID={self.attempt_id!r} does not match the "
                               f"live attempt — using {discovered!r} instead")
            self.attempt_id = discovered
        acct = a.get("account") or {}
        ch = a.get("challenge") or {}
        self._initial_balance = _f(acct.get("initialBalance")
                                   or ch.get("initialBalance")
                                   or ch.get("accountSize"), 0) or None
        logger.info(f"✅ Propr account {self.account_id} | attempt {self.attempt_id} "
                    f"| status {a.get('status')} | phase {a.get('currentPhaseId')} "
                    f"| initial ${self._initial_balance}")

        # app.py assigns asset_meta right after init_sdk(); seed it here too so
        # the client is usable standalone. build_asset_meta() reads HL's public meta.
        from hl_client import build_asset_meta
        self.asset_meta = build_asset_meta() or {}
        return True

    def signing_works(self) -> bool:
        return bool(self._req("GET", "/users/me"))

    # ── market data (Hyperliquid, public) ───────────────────────
    def get_all_prices(self, extra=None) -> dict[str, float]:
        return self.hl.get_all_prices(extra)

    def get_price(self, symbol: str) -> Optional[float]:
        return self.hl.get_price(symbol)

    def fetch_candles(self, symbol: str, interval: str = "4h", limit: int = 200) -> list[dict]:
        return self.hl.fetch_candles(symbol, interval, limit)

    # ── equity ──────────────────────────────────────────────────
    def _attempt(self) -> Optional[dict]:
        """The live attempt record. Falls back to resolving it from the active
        list by accountId, so a wrong or stale PROPR_ATTEMPT_ID self-heals
        instead of permanently zeroing out equity."""
        if self.attempt_id:
            d = self._req("GET", f"/challenge-attempts/{self.attempt_id}")
            if d:
                return d.get("data", d)
            logger.warning(f"attempt {self.attempt_id!r} not found — re-resolving")
        d = self._req("GET", "/challenge-attempts", params={"status": "active"})
        for a in (d or {}).get("data", []):
            if a.get("accountId") == self.account_id:
                aid = a.get("attemptId") or a.get("id")
                if aid and aid != self.attempt_id:
                    logger.warning(f"attempt id corrected to {aid!r}")
                    self.attempt_id = aid
                return a
        logger.error(f"no active attempt for account {self.account_id}")
        return None

    def get_equity(self) -> float:
        """Equity per Propr's own challenge ledger — the same accounting that
        decides a breach. Do NOT reconstruct this from positions: if our number
        and Propr's disagree, theirs is the one that ends the challenge."""
        a = self._attempt()
        if not a:
            return 0.0
        acct = a.get("account") or {}
        self._log_unmapped("account", acct)
        for k in ("equity", "balance", "currentBalance", "accountValue",
                  "totalEquity", "netEquity", "currentEquity"):
            if acct.get(k) is not None:
                return _f(acct[k])
        base = self._initial_balance or _f(acct.get("initialBalance"))
        pnl = None
        for k in ("totalProfitLoss", "totalPnl", "netPnl", "realizedPnl", "profitLoss"):
            if acct.get(k) is not None or a.get(k) is not None:
                pnl = _f(acct.get(k, a.get(k)))
                break
        if base and pnl is not None:
            return base + pnl
        logger.error("equity: no recognised balance field — see [shape:account] above")
        return 0.0

    # ── positions ───────────────────────────────────────────────
    def _raw_positions(self) -> Optional[list]:
        # No query params: the server rejects status/limit with a bare 400.
        # Filter client-side, which the docs recommend anyway for zero-qty rows.
        d = self._req("GET", f"/accounts/{self.account_id}/positions")
        if d is None:
            return None                      # failed read != flat (Farms lesson)
        rows = d.get("data", [])
        return [p for p in rows
                if _f(p.get("quantity")) != 0
                and str(p.get("status", "open")).lower() == "open"]

    def get_positions(self):
        """HL-shaped dicts so position_manager needs no changes.
        Returns None on a failed read, never [] — an outage must not look flat."""
        raw = self._raw_positions()
        if raw is None:
            logger.warning("get_positions: Propr read failed — returning None")
            return None
        out = []
        for p in raw:
            self._log_unmapped("position", p)
            qty = abs(_f(p.get("quantity")))
            szi = qty if str(p.get("positionSide", "")).lower() == "long" else -qty
            out.append({
                "coin": str(p.get("asset", "")),
                "szi": szi,
                "entryPx": p.get("entryPrice"),
                "unrealizedPnl": p.get("unrealizedPnl"),
                "positionId": p.get("positionId"),
                "leverage": {"value": p.get("leverage")},
            })
        return out

    def get_position(self, symbol: str) -> Optional[dict]:
        target = short_name(symbol)
        for p in (self.get_positions() or []):
            if short_name(str(p.get("coin", ""))) == target:
                return p
        return None

    def _position_id(self, symbol: str, is_long: bool) -> Optional[str]:
        """Conditional orders need this. Only exists once the entry has filled."""
        target, want = short_name(symbol), "long" if is_long else "short"
        for p in (self._raw_positions() or []):
            if (short_name(str(p.get("asset", ""))) == target
                    and str(p.get("positionSide", "")).lower() == want
                    and _f(p.get("quantity")) != 0):
                return p.get("positionId")
        return None

    # ── orders ──────────────────────────────────────────────────
    def _order(self, body: dict) -> Optional[dict]:
        body.setdefault("intentId", _ulid())
        body.setdefault("accountId", self.account_id)
        body.setdefault("exchange", "hyperliquid")
        body.setdefault("productType", "perp")
        body.setdefault("timeInForce", "GTC")
        d = self._req("POST", f"/accounts/{self.account_id}/orders",
                      json={"orders": [body]})
        if not d:
            return None
        rows = d.get("data", [])
        return rows[0] if rows else None

    def _round(self, symbol: str, size: float) -> float:
        from hl_client import round_size
        return round_size(self.asset_meta, hl_symbol(short_name(symbol)), size)

    def _round_px(self, symbol: str, px: float) -> float:
        from hl_client import round_price
        return round_price(self.asset_meta, hl_symbol(short_name(symbol)), px)

    def market_open(self, symbol: str, is_buy: bool, notional_usd: float,
                    current_price: Optional[float] = None) -> Optional[dict]:
        asset = hl_symbol(short_name(symbol))
        px = current_price if (current_price or 0) > 0 else (self.get_price(short_name(symbol)) or 0)
        if px <= 0:
            self.last_open_error = "no price"
            return None
        size = self._round(symbol, notional_usd / px)
        if size <= 0 or size * px < 10:
            self.last_open_error = (f"size {size} rounds below the $10 minimum "
                                    f"(notional ${notional_usd:.2f})")
            logger.warning(f"⚠️  {asset}: {self.last_open_error}")
            return None
        res = self._order({
            "type": "market", "side": "buy" if is_buy else "sell",
            "positionSide": "long" if is_buy else "short",   # must agree, else 13096
            "asset": asset, "base": asset, "quote": "USDC",
            "quantity": str(size), "reduceOnly": False, "closePosition": False,
        })
        if not res:
            self.last_open_error = "order rejected"
            return None
        fill = _f(res.get("averageFillPrice")) or px
        logger.info(f"✅ {asset} open {'long' if is_buy else 'short'} "
                    f"{size} @ ${fill:.6f} ({res.get('orderId')})")
        # key MUST be total_size — position_manager reads order["total_size"]
        return {"filled": True, "avg_price": fill, "total_size": size,
                "size": size, "oid": res.get("orderId"), "raw": res}

    def market_close(self, symbol: str, size: float, is_long: bool,
                     current_price: Optional[float] = None) -> Optional[dict]:
        asset = hl_symbol(short_name(symbol))
        size = self._round(symbol, size)
        if size <= 0:
            logger.warning(f"⚠️  {asset}: close size rounded to 0")
            return None
        res = self._order({
            "type": "market", "side": "sell" if is_long else "buy",
            "positionSide": "long" if is_long else "short",   # the side being CLOSED
            "asset": asset, "base": asset, "quote": "USDC",
            "quantity": str(size),
            "reduceOnly": True, "closePosition": True,        # never flips
        })
        if not res:
            return None
        fill = _f(res.get("averageFillPrice")) or current_price or self.get_price(short_name(symbol))
        logger.info(f"✅ {asset} closed {size} @ ${_f(fill):.6f}")
        return {"filled": True, "avg_price": _f(fill), "total_size": size,
                "size": size, "oid": res.get("orderId"), "raw": res}

    def place_stop_market(self, symbol: str, is_long: bool, size: float,
                          stop_px: float) -> Optional[str]:
        """Reduce-only stop-market. Needs a positionId, which only exists after
        the entry fills — call this AFTER market_open, never batched with it."""
        asset = hl_symbol(short_name(symbol))
        size = self._round(symbol, size)
        if size <= 0:
            return None
        pid = self._position_id(symbol, is_long)
        if not pid:
            logger.error(f"❌ {asset}: no open position — cannot attach a stop "
                         f"(13056 CONDITIONAL_ORDER_REQUIRES_POSITION_OR_GROUP)")
            return None
        trigger = self._round_px(symbol, stop_px)
        res = self._order({
            "positionId": pid, "type": "stop_market",
            "side": "sell" if is_long else "buy",
            "positionSide": "long" if is_long else "short",
            "asset": asset, "base": asset, "quote": "USDC",
            "quantity": str(size), "triggerPrice": str(trigger), "reduceOnly": True,
        })
        if not res:
            return None
        oid = res.get("orderId")
        logger.info(f"    🛡 {asset} stop @ ${trigger:.6f} ({oid})")
        return str(oid) if oid else None

    def modify_stop(self, symbol: str, is_long: bool, size: float,
                    old_oid, new_stop_px: float) -> Optional[str]:
        """Ratchet: place the new stop first, then cancel the old one, so the
        position is never unprotected during the swap."""
        new_oid = self.place_stop_market(symbol, is_long, size, new_stop_px)
        if new_oid is None:
            logger.warning(f"⚠️  {symbol}: modify_stop kept old stop (new placement failed)")
            return str(old_oid) if old_oid is not None else None
        if old_oid is not None and str(old_oid) != str(new_oid):
            self.cancel_order(symbol, old_oid)
        return new_oid

    def cancel_order(self, symbol: str, oid) -> bool:
        """oid is a URN string on Propr, not an int. 400 = already gone = success."""
        if oid is None:
            return False
        d = self._req("POST", f"/accounts/{self.account_id}/orders/{oid}/cancel")
        return d is not None

    # ── margin config ───────────────────────────────────────────
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        asset = hl_symbol(short_name(symbol))
        cfg_id = self._margin_cfg_ids.get(asset)
        if not cfg_id:
            d = self._req("GET", f"/accounts/{self.account_id}/margin-config/{asset}")
            if not d:
                logger.warning(f"⚠️  {asset}: not tradeable on Propr (margin-config 404)")
                return False
            cfg_id = (d.get("data", d) or {}).get("configId")
            if not cfg_id:
                return False
            self._margin_cfg_ids[asset] = cfg_id
        ok = self._req("PUT", f"/accounts/{self.account_id}/margin-config/{cfg_id}",
                       json={"exchange": "hyperliquid", "asset": asset,
                             "marginMode": config.margin_mode_for(asset),
                             "leverage": int(leverage)})
        return ok is not None

    def max_leverage(self, asset: str) -> int:
        lim = self._req("GET", "/leverage-limits/effective") or {}
        return int((lim.get("overrides") or {}).get(asset, lim.get("defaultMax", 2)))

    # ── fills (feeds friction_pct — the point of this experiment) ─
    def get_user_fills(self, start_ms: Optional[int] = None) -> list[dict]:
        """Propr /trades mapped to HL userFills shape: coin, dir, px, sz, fee."""
        d = self._req("GET", f"/accounts/{self.account_id}/trades", params={"limit": 200})
        if not d:
            return []
        out = []
        for t in d.get("data", []):
            self._log_unmapped("trade", t)
            ts = t.get("executedAt") or ""
            if start_ms:
                try:
                    from datetime import datetime
                    if int(datetime.fromisoformat(
                            ts.replace("Z", "+00:00")).timestamp() * 1000) < start_ms:
                        continue
                except (ValueError, TypeError):
                    pass
            closing = str(t.get("type", "")).lower() in ("reduce", "close", "liquidation")
            side = "Long" if str(t.get("positionSide", "")).lower() == "long" else "Short"
            out.append({
                "coin": t.get("asset", ""),
                "dir": f"{'Close' if closing else 'Open'} {side}",
                "px": t.get("price"), "sz": t.get("quantity"),
                "fee": t.get("fee"), "closedPnl": t.get("realizedPnl"),
                "time": ts,
            })
        return out
