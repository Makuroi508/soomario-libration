"""
Soomario Libration - Bulk Trade client (Solana execution layer, TESTNET/paper)
==============================================================================
Implements the SAME client contract Libration's strategy layer expects from
hl_client.HLClient, so PositionManager / ExitManager / the tick loop drive it
unchanged. Bulk is a Solana-native perps venue; auth is Ed25519 over a canonical
wincode binary message handled entirely by the official `bulk-keychain` Python
library (Keypair/Signer) - we never hand-roll the binary encoding.

Signed actions go to a single unified endpoint (POST /order) as
{actions, nonce, account, signer, signature}. `signer.sign(order)` returns
exactly that envelope plus the deterministic `order_id`.

Compact order encodings produced by the lib (verified):
  market : {'m': {'b':<is_buy>, 'c':'SOL-USD', 'i':False, 'r':<reduce_only>, 'sz':<size>}}
  cancel : {'cx':{'c':'SOL-USD', 'oid':<base58>}}

STOPS: Bulk testnet exposes only LIMIT/MARKET (per exchangeInfo), so this venue
runs BACKSTOP-ONLY: place_stop_market / modify_stop are no-ops that return a
non-numeric sentinel id, and the software backstop in exit_manager carries every
hard-stop and trailing exit (a posture the handoff already documents as
known-good on the shared vault). No native trigger signing is needed.

MARKS: get_all_prices reads Bulk's own price feed, but also accepts an injected
price_fallback (wired by the fan-out app to Hyperliquid marks) so sizing and
trailing never stall if the testnet feed path needs tuning.

Env (set on Railway; never in code):
  BULK_REST_URL        default https://staging-api.bulk.trade/api/v1  (testnet)
  BULK_ACCOUNT_ADDRESS Solana account pubkey (faucet-funded on testnet)
  BULK_PRIVATE_KEY     base58 secret of the signer for that account
  BULK_ORDER_PATH      default /order
  BULK_ACCOUNT_PATH    default /account
  BULK_PRICE_PATH      default /ticker   (all-symbol price feed; verified at boot)
  BULK_KLINES_PATH     default /klines
"""
import logging
import math
import os
import time
from typing import Optional, Callable

import requests

try:
    from config import DRY_RUN
except Exception:  # pragma: no cover
    DRY_RUN = False

logger = logging.getLogger("bulk_client")

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_BACKOFF_SEC = 1.5
HTTP_TIMEOUT = 10

# Sentinel returned by place_stop_market so the position is KEPT (not flattened)
# while the software backstop owns the exit. Non-numeric on purpose: exit_manager
# only attempts cancel_order when str(id).isdigit(), so this is never cancelled.
BACKSTOP_SENTINEL = "backstop"


def _short(symbol: str) -> str:
    s = str(symbol).split(":", 1)[-1].upper()
    return s.split("-", 1)[0] if s.endswith("-USD") or "-USD" in s else s


def bulk_symbol(internal: str) -> str:
    """Internal uppercase ticker -> Bulk 'TICKER-USD' symbol."""
    u = str(internal).split(":", 1)[-1].upper().split("-", 1)[0]
    return f"{u}-USD"


def _decimals_from_step(step: float) -> int:
    if step is None or step <= 0:
        return 6
    d = -int(math.floor(math.log10(step) + 1e-9))
    return max(0, min(d, 12))


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class BulkClient:
    def __init__(self):
        self.rest_url = os.getenv("BULK_REST_URL",
                                  "https://staging-api.bulk.trade/api/v1").rstrip("/")
        self.account_address = (os.getenv("BULK_ACCOUNT_ADDRESS") or "").strip()
        self._priv = (os.getenv("BULK_PRIVATE_KEY") or "").strip()
        self.order_path = os.getenv("BULK_ORDER_PATH", "/order")
        self.account_path = os.getenv("BULK_ACCOUNT_PATH", "/account")
        self.price_path = os.getenv("BULK_PRICE_PATH", "/ticker")
        self.klines_path = os.getenv("BULK_KLINES_PATH", "/klines")

        self._signer = None
        self.asset_meta = {}       # {SYMBOL: {lot, tick, min_notional}}
        self.last_open_error = None
        self.price_fallback: Optional[Callable] = None  # e.g. HL get_all_prices
        self._sess = requests.Session()

    # ---- lifecycle -------------------------------------------------
    def set_price_fallback(self, fn: Callable):
        """Wire an external mark source (fan-out app injects HL marks)."""
        self.price_fallback = fn

    def init_sdk(self) -> bool:
        try:
            from bulk_keychain import Keypair, Signer
        except Exception as e:
            logger.error(f"bulk_keychain import failed: {e}")
            return False
        if not self._priv:
            logger.error("BULK_PRIVATE_KEY missing")
            return False
        try:
            kp = Keypair.from_base58(self._priv)
            self._signer = Signer(kp)
            signer_pub = kp.pubkey if isinstance(kp.pubkey, str) else str(kp.pubkey)
        except Exception as e:
            logger.error(f"bad BULK_PRIVATE_KEY: {e}")
            return False
        if self.account_address and self.account_address != signer_pub:
            logger.warning(f"BULK_ACCOUNT_ADDRESS {self.account_address} != key pubkey "
                           f"{signer_pub}; using key pubkey (account==signer)")
        self.account_address = signer_pub
        logger.info(f"Bulk signer ready: account {self.account_address}")
        if not self._load_market_meta():
            logger.error("Bulk exchangeInfo load failed - refusing to start")
            return False
        return True

    def _load_market_meta(self) -> bool:
        data = self._get("/exchangeInfo")
        if not data:
            return False
        rows = data if isinstance(data, list) else data.get("symbols", data.get("data", []))
        meta = {}
        for m in rows:
            sym = str(m.get("symbol", "")).upper()
            if not sym:
                continue
            base = sym.split("-", 1)[0]
            meta[base] = {
                "symbol": sym,
                "lot": _f(m.get("lotSize") or m.get("lot_size"), 0.0) or 0.0,
                "tick": _f(m.get("tickSize") or m.get("tick_size"), 0.0) or 0.0,
                "min_notional": _f(m.get("minNotional") or m.get("min_notional"), 10.0) or 10.0,
            }
        if not meta:
            return False
        self.asset_meta = meta
        logger.info(f"Bulk market meta loaded for {len(meta)} symbols")
        return True

    def lists(self, internal: str) -> bool:
        return _short(bulk_symbol(internal)) in self.asset_meta

    def signing_works(self) -> bool:
        try:
            self._signer.sign({"type": "order", "symbol": "SOL-USD", "is_buy": True,
                               "price": 0, "size": 0.0,
                               "order_type": {"type": "market", "is_market": True}})
            return True
        except Exception:
            return False

    # ---- HTTP ------------------------------------------------------
    def _get(self, path: str, params: Optional[dict] = None):
        url = self.rest_url + path
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = self._sess.get(url, params=params, timeout=HTTP_TIMEOUT)
                if r.status_code in RETRYABLE_STATUS:
                    raise requests.HTTPError(str(r.status_code))
                r.raise_for_status()
                return r.json()
            except Exception as e:
                if attempt == MAX_RETRIES:
                    logger.warning(f"GET {path} failed after {attempt}: {e}")
                    return None
                time.sleep(BASE_BACKOFF_SEC * attempt)
        return None

    def _post(self, path: str, body: dict):
        url = self.rest_url + path
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = self._sess.post(url, json=body, timeout=HTTP_TIMEOUT,
                                    headers={"Content-Type": "application/json"})
                if r.status_code in RETRYABLE_STATUS:
                    raise requests.HTTPError(str(r.status_code))
                return r.json()
            except Exception as e:
                if attempt == MAX_RETRIES:
                    logger.error(f"POST {path} failed after {attempt}: {e}")
                    return None
                time.sleep(BASE_BACKOFF_SEC * attempt)
        return None

    def _submit_signed(self, order: dict):
        """Sign a single action dict with bulk-keychain and POST it. Returns the
        API response (dict) or None. Honors DRY_RUN."""
        try:
            signed = self._signer.sign(order)
        except Exception as e:
            logger.error(f"bulk sign failed: {e}")
            return None
        # `actions` comes back as a JSON string; the API expects the parsed array.
        actions = signed.get("actions")
        if isinstance(actions, str):
            import json as _json
            try:
                actions = _json.loads(actions.replace("'", '"'))
            except Exception:
                pass
        body = {
            "actions": actions,
            "nonce": signed.get("nonce"),
            "account": self.account_address,
            "signer": signed.get("signer"),
            "signature": signed.get("signature"),
        }
        if DRY_RUN:
            logger.info(f"[DRY] would POST {self.order_path} {order.get('type')} {order.get('symbol')}")
            return {"success": True, "dry": True, "order_id": signed.get("order_id")}
        res = self._post(self.order_path, body)
        if isinstance(res, dict):
            res.setdefault("order_id", signed.get("order_id"))
        return res

    # ---- prices ----------------------------------------------------
    def get_all_prices(self, extra=None) -> dict:
        out = {}
        data = self._get(self.price_path)
        rows = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])
        for row in rows or []:
            sym = _short(row.get("symbol", row.get("s", "")))
            px = row.get("markPrice") or row.get("mark") or row.get("price") \
                or row.get("last") or row.get("px")
            v = _f(px)
            if sym and v is not None and v > 0:
                out[sym] = v
        if not out and self.price_fallback:
            try:
                fb = self.price_fallback(extra) or {}
                out = {_short(k): float(v) for k, v in fb.items()}
            except Exception as e:
                logger.warning(f"bulk price fallback failed: {e}")
        return out

    def get_price(self, symbol: str) -> Optional[float]:
        return self.get_all_prices().get(_short(symbol))

    # ---- account (positions + equity via unsigned fullAccount) -----
    def _full_account(self):
        return self._post(self.account_path,
                          {"type": "fullAccount", "user": self.account_address})

    def get_positions(self):
        """None on FAILED read, [] on confirmed-empty. Normalizes to
        {coin, szi (+long/-short), entryPx}."""
        if not self.account_address:
            return None
        acct = self._full_account()
        if acct is None:
            return None
        data = acct.get("data", acct) if isinstance(acct, dict) else acct
        positions = None
        if isinstance(data, dict):
            for k in ("positions", "perpPositions", "openPositions", "assetPositions"):
                if isinstance(data.get(k), list):
                    positions = data[k]; break
        if positions is None:
            # a confirmed response with no positions field -> treat as empty
            return []
        out = []
        for p in positions:
            pos = p.get("position", p) if isinstance(p, dict) else {}
            sym = _short(pos.get("symbol", pos.get("coin", pos.get("c", ""))))
            if not sym:
                continue
            szi = pos.get("szi")
            if szi is None:
                size = _f(pos.get("size", pos.get("sz")), 0.0) or 0.0
                side = str(pos.get("side", pos.get("b", ""))).lower()
                is_long = side in ("long", "buy", "bid", "true", "1") or pos.get("b") is True
                szi = abs(size) if is_long else -abs(size)
            szi = _f(szi, 0.0) or 0.0
            if szi == 0:
                continue
            entry = _f(pos.get("entryPx", pos.get("entryPrice", pos.get("avgPrice", pos.get("px")))), 0.0)
            out.append({"coin": sym, "szi": szi, "entryPx": entry, "symbol": sym})
        return out

    def get_position(self, symbol: str) -> Optional[dict]:
        live = self.get_positions()
        if not live:
            return None
        u = _short(symbol)
        for p in live:
            if p["coin"] == u:
                return p
        return None

    def get_equity(self) -> float:
        acct = self._full_account()
        if not acct:
            return 0.0
        data = acct.get("data", acct) if isinstance(acct, dict) else acct
        ms = data.get("marginSummary", {}) if isinstance(data, dict) else {}
        for src in (data, ms):
            if not isinstance(src, dict):
                continue
            for k in ("equity", "accountValue", "totalEquity", "accountEquity", "balance"):
                v = _f(src.get(k))
                if v is not None:
                    return v
        return 0.0

    def get_user_fills(self, start_ms: Optional[int] = None) -> list:
        """Best-effort fills for reconcile. If the path/shape is not yet
        confirmed this returns [], and reconcile safely books at the stop
        estimate after debounce rather than fabricating."""
        acct = self._full_account()
        if not acct:
            return []
        data = acct.get("data", acct) if isinstance(acct, dict) else acct
        fills = data.get("fills", data.get("trades", [])) if isinstance(data, dict) else []
        out = []
        for f in fills or []:
            sym = _short(f.get("symbol", f.get("coin", f.get("c", ""))))
            d = str(f.get("dir", f.get("direction", ""))).strip()
            if not d:
                side = str(f.get("side", "")).lower()
                closing = str(f.get("reduceOnly", f.get("r", ""))).lower() in ("true", "1")
                d = ("Close " + ("Long" if side in ("sell", "ask") else "Short")) if closing else side
            px = _f(f.get("px", f.get("price")), 0.0)
            sz = _f(f.get("sz", f.get("size")), 0.0)
            if px is None or sz is None:
                continue
            out.append({"coin": sym, "px": px, "sz": sz, "dir": d,
                        "fee": _f(f.get("fee"), 0.0) or 0.0,
                        "time": f.get("time") or f.get("ts")})
        return out

    # ---- leverage (best-effort; 2x is under every Bulk symbol cap) --
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        # Bulk sets per-symbol max leverage via updateUserSettings; at 2x we are
        # far under every listed cap, so this is a no-op success on testnet.
        return True

    # ---- rounding --------------------------------------------------
    def _meta(self, internal: str) -> dict:
        return self.asset_meta.get(_short(bulk_symbol(internal)), {})

    def _round_size(self, internal: str, size: float) -> float:
        lot = self._meta(internal).get("lot") or 0.0
        if lot > 0:
            return math.floor(size / lot + 1e-9) * lot
        return round(size, 6)

    # ---- market open / close ---------------------------------------
    def market_open(self, symbol: str, is_buy: bool, notional_usd: float,
                    current_price: Optional[float] = None) -> Optional[dict]:
        sym = bulk_symbol(symbol)
        self.last_open_error = None
        if current_price is None or current_price <= 0:
            current_price = self.get_price(symbol) or 0
        if current_price <= 0:
            self.last_open_error = "no_price"
            return None
        size = self._round_size(symbol, notional_usd / current_price)
        if size <= 0:
            self.last_open_error = f"size rounds to 0 at ${current_price:.4f}"
            return None
        min_notional = self._meta(symbol).get("min_notional", 10.0)
        if size * current_price < min_notional:
            self.last_open_error = (f"below ${min_notional:.0f} min "
                                    f"(notional ${size*current_price:.2f})")
            return None
        order = {"type": "order", "symbol": sym, "is_buy": bool(is_buy),
                 "price": 0, "size": size, "reduce_only": False,
                 "order_type": {"type": "market", "is_market": True}}
        logger.info(f"    -> market_open {'BUY' if is_buy else 'SELL'} {size} {sym} "
                    f"(ref ${current_price:.4f}, notional ${size*current_price:.2f})")
        res = self._submit_signed(order)
        if res is None or (isinstance(res, dict) and res.get("success") is False):
            self.last_open_error = self._err(res) or "not_filled"
            return None
        return {"filled": True, "avg_price": current_price, "total_size": size,
                "status": "filled", "oid": res.get("order_id") if isinstance(res, dict) else None}

    def market_close(self, symbol: str, size: float, is_long: bool,
                     current_price: Optional[float] = None) -> Optional[dict]:
        sym = bulk_symbol(symbol)
        if current_price is None or current_price <= 0:
            current_price = self.get_price(symbol) or 0
        size = self._round_size(symbol, size)
        if size <= 0:
            return None
        order = {"type": "order", "symbol": sym, "is_buy": (not is_long),
                 "price": 0, "size": size, "reduce_only": True,
                 "order_type": {"type": "market", "is_market": True}}
        logger.info(f"    <- market_close {'SELL' if is_long else 'BUY'} {size} {sym} (reduce_only)")
        res = self._submit_signed(order)
        if res is None or (isinstance(res, dict) and res.get("success") is False):
            return None
        return {"filled": True, "avg_price": current_price, "status": "filled",
                "oid": res.get("order_id") if isinstance(res, dict) else None}

    # ---- stops: BACKSTOP-ONLY on testnet ---------------------------
    def place_stop_market(self, symbol: str, is_long: bool, size: float,
                          stop_px: float) -> Optional[str]:
        # No native trigger on Bulk testnet - keep the position and let the
        # software backstop enforce the stop. Returning a non-None, non-numeric
        # sentinel prevents position_manager from flattening as 'unprotected'.
        return BACKSTOP_SENTINEL

    def modify_stop(self, symbol: str, is_long: bool, size: float,
                    old_id, new_stop: float) -> Optional[str]:
        return BACKSTOP_SENTINEL

    def cancel_order(self, symbol: str, oid) -> bool:
        order = {"type": "cancel", "symbol": bulk_symbol(symbol), "order_id": str(oid)}
        res = self._submit_signed(order)
        return bool(res) and (not isinstance(res, dict) or res.get("success") is not False)

    # ---- candles (not on the fan-out hot path; HL is canonical) ----
    def fetch_candles(self, symbol: str, interval: str = "4h", limit: int = 200) -> list:
        data = self._get(self.klines_path,
                         params={"symbol": bulk_symbol(symbol), "interval": interval, "limit": limit})
        rows = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])
        out = []
        for c in rows or []:
            try:
                if isinstance(c, dict):
                    out.append({"t": int(c.get("t") or c.get("time") or c.get("openTime")),
                                "o": float(c.get("o", c.get("open"))), "h": float(c.get("h", c.get("high"))),
                                "l": float(c.get("l", c.get("low"))), "c": float(c.get("c", c.get("close"))),
                                "v": float(c.get("v", c.get("volume") or 0))})
                elif isinstance(c, (list, tuple)) and len(c) >= 5:
                    out.append({"t": int(c[0]), "o": float(c[1]), "h": float(c[2]),
                                "l": float(c[3]), "c": float(c[4]),
                                "v": float(c[5]) if len(c) > 5 else 0.0})
            except (TypeError, ValueError, KeyError):
                continue
        out.sort(key=lambda x: x["t"])
        return out

    @staticmethod
    def _err(res) -> Optional[str]:
        if isinstance(res, dict):
            return res.get("error") or res.get("message") or res.get("msg")
        return None
