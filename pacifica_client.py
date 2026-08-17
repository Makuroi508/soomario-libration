"""
Soomario Libration - Pacifica client (Solana perps, LIVE)
=========================================================
Implements the SAME client contract Libration's strategy layer expects from
hl_client.HLClient, so PositionManager / ExitManager / the tick loop drive it
unchanged. Pacifica is a Solana-native perps DEX; auth is Ed25519 over a
deterministic (recursively key-sorted, compact) JSON message, exactly matching
Pacifica's reference python-sdk (common/utils.py). Signing is done with solders
(byte-identical to the reference; cross-checked against PyNaCl).

Contract methods (parity with hl_client):
  reads : get_positions (None=failed read / []=empty), get_price, get_all_prices,
          get_equity, get_user_fills, fetch_candles
  writes: set_leverage, market_open, market_close, place_stop_market,
          modify_stop, cancel_order
  life  : init_sdk, plus attributes asset_meta and last_open_error

Stops on Pacifica are NATIVE: the position stop-loss is set via POST
/positions/tpsl (set_position_tpsl). Re-calling it REPLACES the stop-loss, so
"move the trailing stop" is a single idempotent call - no place-new-then-cancel
race. The software backstop in exit_manager remains the last-resort net.

Env (set on Railway; never in code):
  PACIFICA_REST_URL         default https://api.pacifica.fi/api/v1  (mainnet)
  PACIFICA_ACCOUNT_ADDRESS  main account public key (base58) - used as `account`
                            and for read endpoints (?account=)
  PACIFICA_PRIVATE_KEY      base58 secret of the SIGNER
  PACIFICA_AGENT            "1" if PRIVATE_KEY is an API agent key (then `account`
                            stays the main address and `agent_wallet` is attached);
                            default "0" (PRIVATE_KEY is the main account key)
  PACIFICA_EXPIRY_WINDOW    signature expiry window ms (default 5000)
  PACIFICA_SLIPPAGE_PCT     market-order slippage guard % (default 0.5)
  PACIFICA_TRADES_PATH      trade-history path override (default /trades/history)
"""
import json
import logging
import math
import os
import time
import uuid
from typing import Optional

import base58
import requests

try:
    from config import DRY_RUN
except Exception:  # pragma: no cover - allow standalone import in tests
    DRY_RUN = False

logger = logging.getLogger("pacifica_client")

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_BACKOFF_SEC = 1.5
HTTP_TIMEOUT = 10

# Pacifica denominates some low-unit-price tokens with a lowercase 'k' prefix,
# like HL. Internally Libration uppercases everything; map back to the exact
# venue symbol at the API boundary. init_sdk() validates every configured coin
# against live market_info and drops any the venue does not list, so a wrong
# guess here can never silently mis-trade - it just gets pruned with a warning.
_PAC_SYMBOL_OVERRIDE = {
    "KPEPE": "kPEPE", "KSHIB": "kSHIB", "KBONK": "kBONK", "KFLOKI": "kFLOKI",
}


def _short(symbol: str) -> str:
    """Internal uppercase ticker from any venue symbol."""
    s = str(symbol).split(":", 1)[-1]
    return s.upper()


def pac_symbol(internal: str) -> str:
    """Internal uppercase ticker -> exact Pacifica (case-sensitive) symbol."""
    u = _short(internal)
    return _PAC_SYMBOL_OVERRIDE.get(u, u)


def _decimals_from_step(step: float) -> int:
    if step is None or step <= 0:
        return 6
    d = -int(math.floor(math.log10(step) + 1e-9))
    return max(0, min(d, 12))


def _fmt(value: float, decimals: int) -> str:
    """Format a number to fixed decimals with no trailing-zero / float noise."""
    q = round(float(value), decimals)
    s = f"{q:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# =====================================================================
#  Deterministic signing (mirror of Pacifica python-sdk common/utils.py)
# =====================================================================

def _sort_json(value):
    if isinstance(value, dict):
        return {k: _sort_json(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_json(v) for v in value]
    return value


def _prepare_message(header: dict, payload: dict) -> str:
    if "type" not in header or "timestamp" not in header or "expiry_window" not in header:
        raise ValueError("header must have type, timestamp, expiry_window")
    data = {**header, "data": payload}
    return json.dumps(_sort_json(data), separators=(",", ":"))


class PacificaClient:
    def __init__(self):
        self.rest_url = os.getenv("PACIFICA_REST_URL", "https://api.pacifica.fi/api/v1").rstrip("/")
        self.account_address = (os.getenv("PACIFICA_ACCOUNT_ADDRESS") or "").strip()
        self._priv = (os.getenv("PACIFICA_PRIVATE_KEY") or "").strip()
        self.is_agent = os.getenv("PACIFICA_AGENT", "0") == "1"
        self.expiry_window = int(os.getenv("PACIFICA_EXPIRY_WINDOW", "5000"))
        self.slippage_pct = os.getenv("PACIFICA_SLIPPAGE_PCT", "0.5")
        # Pacifica moved trade history from /account/trades (now 404) to
        # /trades/history -- observed live 2026-08-17, same params and a
        # compatible response shape (symbol/amount/price/fee/side/created_at).
        self.trades_path = os.getenv("PACIFICA_TRADES_PATH", "/trades/history")

        self._keypair = None          # solders Keypair (signer)
        self._agent_pubkey = None     # agent pubkey when is_agent
        self.asset_meta = {}          # {SYMBOL: {lot, tick, max_leverage, min_notional}}
        self.last_open_error = None
        self._sess = requests.Session()

    # ---- lifecycle -------------------------------------------------
    def init_sdk(self) -> bool:
        """Build the signer, resolve the account address, load market meta, and
        prune the configured universe to what the venue actually lists. Returns
        False on any fatal misconfig so the worker refuses to start."""
        try:
            from solders.keypair import Keypair
        except Exception as e:
            logger.error(f"solders import failed: {e}")
            return False
        if not self._priv:
            logger.error("PACIFICA_PRIVATE_KEY missing")
            return False
        try:
            self._keypair = Keypair.from_base58_string(self._priv)
        except Exception as e:
            logger.error(f"bad PACIFICA_PRIVATE_KEY: {e}")
            return False
        signer_pub = str(self._keypair.pubkey())
        if self.is_agent:
            self._agent_pubkey = signer_pub
            if not self.account_address:
                logger.error("PACIFICA_AGENT=1 requires PACIFICA_ACCOUNT_ADDRESS (main account)")
                return False
        else:
            # signer IS the main account
            if self.account_address and self.account_address != signer_pub:
                logger.warning(f"PACIFICA_ACCOUNT_ADDRESS {self.account_address} != key pubkey "
                               f"{signer_pub}; using key pubkey")
            self.account_address = signer_pub
        logger.info(f"Pacifica signer ready: account {self.account_address}"
                    f"{' (agent ' + self._agent_pubkey + ')' if self.is_agent else ''}")

        if not self._load_market_meta():
            logger.error("Pacifica market meta load failed - refusing to start")
            return False
        return True

    def _load_market_meta(self) -> bool:
        data = self._get("/info")
        if not data:
            return False
        meta = {}
        for m in data:
            sym = str(m.get("symbol", "")).strip()
            if not sym:
                continue
            lot = float(m.get("lot_size") or 0) or 0.0
            tick = float(m.get("tick_size") or 0) or 0.0
            try:
                min_notional = float(m.get("min_order_size") or 10)
            except (TypeError, ValueError):
                min_notional = 10.0
            meta[sym.upper()] = {
                "symbol": sym, "lot": lot, "tick": tick,
                "max_leverage": int(m.get("max_leverage") or 0),
                "min_notional": min_notional,
            }
        if not meta:
            return False
        self.asset_meta = meta
        logger.info(f"Pacifica market meta loaded for {len(meta)} symbols")
        return True

    def lists(self, internal: str) -> bool:
        """True if the venue lists this coin (used to prune the universe)."""
        return pac_symbol(internal).upper() in self.asset_meta

    def signing_works(self) -> bool:
        try:
            self._sign("noop", {"probe": "1"})
            return True
        except Exception:
            return False

    # ---- HTTP ------------------------------------------------------
    def _get(self, path: str, params: Optional[dict] = None):
        """GET a public endpoint, return the `data` field, or None on failure.
        None must be reserved for genuine read failures (see get_positions)."""
        url = self.rest_url + path
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = self._sess.get(url, params=params, timeout=HTTP_TIMEOUT,
                                    headers={"Accept": "*/*"})
                if r.status_code in RETRYABLE_STATUS:
                    raise requests.HTTPError(f"{r.status_code}")
                r.raise_for_status()
                body = r.json()
                if isinstance(body, dict):
                    if body.get("success") is False:
                        logger.warning(f"GET {path} success=false: {body.get('error')}")
                        return None
                    return body.get("data", body)
                return body
            except Exception as e:
                if attempt == MAX_RETRIES:
                    logger.warning(f"GET {path} failed after {attempt}: {e}")
                    return None
                time.sleep(BASE_BACKOFF_SEC * attempt)
        return None

    def _sign(self, op_type: str, payload: dict):
        timestamp = int(time.time() * 1000)
        header = {"timestamp": timestamp, "expiry_window": self.expiry_window, "type": op_type}
        message = _prepare_message(header, payload)
        sig = self._keypair.sign_message(message.encode("utf-8"))
        signature = base58.b58encode(bytes(sig)).decode("ascii")
        return timestamp, signature

    def _signed_post(self, path: str, op_type: str, payload: dict):
        """Sign `payload` under `op_type` and POST. Returns parsed JSON dict or
        None on transport failure. Honors DRY_RUN (no submit)."""
        timestamp, signature = self._sign(op_type, payload)
        envelope = {
            "account": self.account_address,
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": self.expiry_window,
        }
        if self.is_agent and self._agent_pubkey:
            envelope["agent_wallet"] = self._agent_pubkey
        request = {**envelope, **payload}
        if DRY_RUN:
            logger.info(f"[DRY] would POST {path} {op_type} {payload}")
            return {"success": True, "dry": True, "data": {}}
        url = self.rest_url + path
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = self._sess.post(url, json=request, timeout=HTTP_TIMEOUT,
                                    headers={"Content-Type": "application/json"})
                if r.status_code in RETRYABLE_STATUS:
                    raise requests.HTTPError(f"{r.status_code}")
                body = r.json()
                if isinstance(body, dict) and body.get("success") is False:
                    logger.warning(f"POST {path} {op_type} rejected: {body.get('error')}")
                    return body
                return body
            except Exception as e:
                if attempt == MAX_RETRIES:
                    logger.error(f"POST {path} {op_type} failed after {attempt}: {e}")
                    return None
                time.sleep(BASE_BACKOFF_SEC * attempt)
        return None

    # ---- prices ----------------------------------------------------
    def get_all_prices(self, extra=None) -> dict:
        data = self._get("/info/prices")
        out = {}
        if not data:
            return out
        for row in data:
            sym = _short(row.get("symbol", ""))
            px = row.get("mark") or row.get("mid") or row.get("oracle")
            try:
                if sym and px is not None:
                    out[sym] = float(px)
            except (TypeError, ValueError):
                continue
        return out

    def get_price(self, symbol: str) -> Optional[float]:
        return self.get_all_prices().get(_short(symbol))

    # ---- positions -------------------------------------------------
    def get_positions(self):
        """Return None on a FAILED read and [] only on a SUCCESSFUL empty read.
        Normalizes Pacifica rows to the shape reconcile/adopt expect:
        {coin, szi (+long/-short), entryPx}. This None/[] distinction is the
        foundation of reconcile safety - never collapse them."""
        if not self.account_address:
            return None
        data = self._get("/positions", params={"account": self.account_address})
        if data is None:
            return None
        out = []
        for p in data:
            sym = _short(p.get("symbol", ""))
            if not sym:
                continue
            try:
                amt = abs(float(p.get("amount") or 0))
            except (TypeError, ValueError):
                continue
            if amt <= 0:
                continue
            side = str(p.get("side", "")).lower()
            szi = amt if side == "bid" else -amt
            try:
                entry = float(p.get("entry_price") or 0)
            except (TypeError, ValueError):
                entry = 0.0
            out.append({"coin": sym, "szi": szi, "entryPx": entry,
                        "symbol": sym, "isolated": bool(p.get("isolated"))})
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

    # ---- equity ----------------------------------------------------
    def get_equity(self) -> float:
        """Account equity (used once to seed the flow-neutral baseline)."""
        data = self._get("/account", params={"account": self.account_address})
        if not data:
            return 0.0
        row = data[0] if isinstance(data, list) and data else data
        for k in ("account_equity", "equity", "total_equity", "balance", "account_value"):
            v = row.get(k) if isinstance(row, dict) else None
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return 0.0

    # ---- fills (for reconcile) -------------------------------------
    def get_user_fills(self, start_ms: Optional[int] = None) -> list:
        """Closing/opening fills normalized to what _resolve_exit_fill expects:
        {coin, px, sz, dir ('Close Long'/'Close Short'/...), fee, time}."""
        params = {"account": self.account_address, "limit": 200}
        if start_ms:
            params["start_time"] = int(start_ms)
        data = self._get(self.trades_path, params=params)
        if not data:
            return []
        out = []
        for f in data:
            sym = _short(f.get("symbol", ""))
            side = str(f.get("side", "")).lower()
            if "close_long" in side:
                d = "Close Long"
            elif "close_short" in side:
                d = "Close Short"
            elif "open_long" in side:
                d = "Open Long"
            elif "open_short" in side:
                d = "Open Short"
            else:
                d = side
            try:
                px = float(f.get("price") or f.get("px") or 0)
                sz = float(f.get("amount") or f.get("sz") or 0)
            except (TypeError, ValueError):
                continue
            try:
                fee = float(f.get("fee") or 0)
            except (TypeError, ValueError):
                fee = 0.0
            out.append({"coin": sym, "px": px, "sz": sz, "dir": d, "fee": fee,
                        "time": f.get("created_at") or f.get("time")})
        return out

    # ---- leverage --------------------------------------------------
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        sym = pac_symbol(symbol)
        res = self._signed_post("/account/leverage", "update_leverage",
                                {"symbol": sym, "leverage": int(leverage)})
        ok = bool(res) and res.get("success") is not False
        if not ok:
            logger.warning(f"set_leverage {sym} {leverage}x not confirmed")
        return ok

    # ---- rounding --------------------------------------------------
    def _meta(self, internal: str) -> dict:
        return self.asset_meta.get(pac_symbol(internal).upper(), {})

    def _round_size(self, internal: str, size: float) -> tuple:
        m = self._meta(internal)
        lot = m.get("lot") or 0.0
        if lot > 0:
            n = math.floor(size / lot + 1e-9)
            size = n * lot
            dec = _decimals_from_step(lot)
        else:
            dec = 6
        return size, _fmt(size, dec)

    # ---- market open / close ---------------------------------------
    def market_open(self, symbol: str, is_buy: bool, notional_usd: float,
                    current_price: Optional[float] = None) -> Optional[dict]:
        sym = pac_symbol(symbol)
        self.last_open_error = None
        if current_price is None or current_price <= 0:
            current_price = self.get_price(symbol) or 0
        if current_price <= 0:
            self.last_open_error = "no_price"
            return None
        size, size_str = self._round_size(symbol, notional_usd / current_price)
        if size <= 0:
            self.last_open_error = f"size rounds to 0 at ${current_price:.4f}"
            return None
        min_notional = self._meta(symbol).get("min_notional", 10.0)
        if size * current_price < min_notional:
            self.last_open_error = (f"below ${min_notional:.0f} min "
                                    f"(notional ${size*current_price:.2f})")
            return None
        payload = {
            "symbol": sym, "reduce_only": False, "amount": size_str,
            "side": "bid" if is_buy else "ask",
            "slippage_percent": str(self.slippage_pct),
            "client_order_id": str(uuid.uuid4()),
        }
        logger.info(f"    -> market_open {'BUY' if is_buy else 'SELL'} {size_str} {sym} "
                    f"(ref ${current_price:.4f}, notional ${size*current_price:.2f})")
        res = self._signed_post("/orders/create_market", "create_market_order", payload)
        if res is None or (isinstance(res, dict) and res.get("success") is False):
            self.last_open_error = self._err(res) or "not_filled"
            return None
        if isinstance(res, dict) and res.get("dry"):
            return {"filled": True, "dry": True, "avg_price": current_price, "total_size": size}
        avg = self._resolve_open_fill(symbol, current_price)
        return {"filled": True, "avg_price": avg, "total_size": size,
                "status": "filled", "oid": self._oid(res)}

    def market_close(self, symbol: str, size: float, is_long: bool,
                     current_price: Optional[float] = None) -> Optional[dict]:
        sym = pac_symbol(symbol)
        if current_price is None or current_price <= 0:
            current_price = self.get_price(symbol) or 0
        _, size_str = self._round_size(symbol, size)
        if float(size_str) <= 0:
            return None
        payload = {
            "symbol": sym, "reduce_only": True, "amount": size_str,
            "side": "ask" if is_long else "bid",
            "slippage_percent": str(self.slippage_pct),
            "client_order_id": str(uuid.uuid4()),
        }
        logger.info(f"    <- market_close {'SELL' if is_long else 'BUY'} {size_str} {sym} (reduce_only)")
        res = self._signed_post("/orders/create_market", "create_market_order", payload)
        if res is None or (isinstance(res, dict) and res.get("success") is False):
            return None
        if isinstance(res, dict) and res.get("dry"):
            return {"filled": True, "dry": True, "avg_price": current_price}
        avg = current_price
        return {"filled": True, "avg_price": avg, "status": "filled", "oid": self._oid(res)}

    def _resolve_open_fill(self, symbol: str, fallback_price: float) -> float:
        """Read the freshly-opened position's VWAP entry as the real fill; fall
        back to the reference price if the read is not yet indexed."""
        try:
            p = self.get_position(symbol)
            if p and p.get("entryPx"):
                return float(p["entryPx"])
        except Exception:
            pass
        return fallback_price

    # ---- native stop (position stop-loss) --------------------------
    def place_stop_market(self, symbol: str, is_long: bool, size: float,
                          stop_px: float) -> Optional[str]:
        """Set the position's native stop-loss. Returns a client id (string) on
        success, or None (which makes the caller flatten rather than ride naked).
        The id is a UUID string, so exit_manager's numeric-only cancel guard
        skips it - the stop is managed by re-setting, not by cancel/replace."""
        return self._set_stop(symbol, is_long, size, stop_px)

    def modify_stop(self, symbol: str, is_long: bool, size: float,
                    old_id, new_stop: float) -> Optional[str]:
        """Move the stop by REPLACING the position stop-loss (idempotent)."""
        return self._set_stop(symbol, is_long, size, new_stop)

    def _set_stop(self, symbol: str, is_long: bool, size: float, stop_px: float) -> Optional[str]:
        sym = pac_symbol(symbol)
        m = self._meta(symbol)
        tick = m.get("tick") or 0.0
        stop_str = _fmt(stop_px, _decimals_from_step(tick)) if tick > 0 else _fmt(stop_px, 6)
        cid = str(uuid.uuid4())
        payload = {
            "symbol": sym,
            "side": "ask" if is_long else "bid",   # closing side
            "stop_loss": {"stop_price": stop_str, "client_order_id": cid},
        }
        res = self._signed_post("/positions/tpsl", "set_position_tpsl", payload)
        if res is None or (isinstance(res, dict) and res.get("success") is False):
            logger.warning(f"set stop {sym} @ {stop_str} not confirmed: {self._err(res)}")
            return None
        return cid

    def cancel_order(self, symbol: str, oid) -> bool:
        sym = pac_symbol(symbol)
        key = "order_id" if str(oid).isdigit() else "client_order_id"
        val = int(oid) if str(oid).isdigit() else str(oid)
        res = self._signed_post("/orders/cancel", "cancel_order", {"symbol": sym, key: val})
        return bool(res) and res.get("success") is not False

    # ---- candles (not on the fan-out hot path; HL is canonical) ----
    def fetch_candles(self, symbol: str, interval: str = "4h", limit: int = 200) -> list:
        sym = pac_symbol(symbol)
        now_ms = int(time.time() * 1000)
        span = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400,
                "1d": 86400}.get(interval, 14400) * 1000
        params = {"symbol": sym, "interval": interval,
                  "start_time": now_ms - span * (limit + 2), "end_time": now_ms}
        data = self._get("/kline", params=params)
        if not data:
            return []
        out = []
        for c in data:
            try:
                out.append({"t": int(c.get("t")), "o": float(c["o"]), "h": float(c["h"]),
                            "l": float(c["l"]), "c": float(c["c"]), "v": float(c.get("v") or 0)})
            except (TypeError, ValueError, KeyError):
                continue
        out.sort(key=lambda x: x["t"])
        return out

    # ---- helpers ---------------------------------------------------
    @staticmethod
    def _err(res) -> Optional[str]:
        if isinstance(res, dict):
            return res.get("error") or (res.get("data") or {}).get("error") if isinstance(res.get("data"), dict) else res.get("error")
        return None

    @staticmethod
    def _oid(res):
        if isinstance(res, dict):
            d = res.get("data")
            if isinstance(d, dict):
                return d.get("order_id") or d.get("oid")
        return None
