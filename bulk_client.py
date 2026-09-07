"""
Soomario Libration - Bulk Trade client (Solana perps, MAINNET)
==============================================================
Implements the SAME client contract Libration's strategy layer expects from
hl_client.HLClient, so PositionManager / ExitManager / the tick loop drive it
unchanged. Bulk is a Solana-native perps venue; auth is Ed25519 over a canonical
wincode preimage (action count + actions + nonce + ACCOUNT pubkey + network
domain byte) handled entirely by the official `bulk-keychain` library - we never
hand-roll the binary encoding.

Updated for Bulk API v1.0.19 (mainnet, 2 Sep 2026):
  * REST base  : https://mainnet-api1.bulk.trade/api/v1  (signature domain "mainnet")
  * /account   : POST {type: fullAccount, user} -> [{ "fullAccount": {...} }]
                 margin.totalMargin / availableMargin, positions[].size (signed),
                 positions[].price (entry). A read that does not yield a fullAccount
                 object is a FAILED read (None), never a trusted-empty [].
  * prices     : no all-symbol ticker on Bulk; GET /ticker/{symbol} per coin
                 (markPrice) with a short TTL cache, HL marks as fallback.
  * stops      : mainnet lists STOP order types -> NATIVE protective stops
                 ({type:"stop"}), moved by cancel-then-replace. If native placement
                 fails the software backstop (exit_manager) still covers the exit.

AGENT MODE (recommended - keeps the master wallet's key off the server):
  Bulk supports signer != account when the signer is an authorized agent wallet.
  `bulk_keychain.prepare_order(order, domain, account=MASTER, signer=AGENT)` builds
  the preimage with the MASTER pubkey; the agent Signer signs it. Authorize the
  agent ONCE from the master key with bulk_authorize_agent.py (run locally).

Env (set on Railway; never in code):
  BULK_NETWORK          "mainnet" (default) | "testnet"  -> REST default + sig domain
  BULK_REST_URL         override REST base (default per BULK_NETWORK)
  BULK_ACCOUNT_ADDRESS  trading account pubkey. In agent mode: the MASTER wallet.
  BULK_PRIVATE_KEY      base58 secret of the SIGNER (agent key in agent mode)
  BULK_AGENT            "1" if BULK_PRIVATE_KEY is an authorized agent wallet key
  BULK_NATIVE_STOPS     "1" (default) use native stop orders; "0" backstop-only
  BULK_STOP_FAIL_MODE   "backstop" (default: keep position, software backstop) |
                        "flatten" (close immediately if native stop can't be placed)
  BULK_STOP_SLIP_PCT    1.5 (default): native stops are STOP-LIMIT with the limit
                        this far past the trigger, so a thin book cannot fill a
                        protective stop tens of percent away (Bulk 6 Sep incident:
                        stops became market buys and swept $81k -> $115k). 0 = pure
                        market stop. The software backstop still covers a gap.
  BULK_MARK_SOURCE      "oracle" (default) | "mark": which ticker price drives our
                        marks. During Bulk's bootstrap phase the local mark can be
                        dislocated by a sparse book; oracle is the robust reference.
  BULK_EXEC_SLIP_PCT    0 (default) = market entries/closes. >0 = IOC LIMIT at
                        ref +/- this %, bounding how far an entry/close can sweep.
  BULK_ORDER_PATH / BULK_ACCOUNT_PATH / BULK_PRICE_PATH / BULK_KLINES_PATH  overrides
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
PRICE_TTL_SEC = 8.0          # dedupe per-symbol ticker reads within a tick

DEFAULT_REST = {
    "mainnet": "https://mainnet-api1.bulk.trade/api/v1",
    "testnet": "https://exchange-api.bulk.trade/api/v1",
    "devnet":  "https://staging-api.bulk.trade/api/v1",
}

# Sentinel returned by place_stop_market when the position is KEPT (not
# flattened) while the software backstop owns the exit. Non-numeric on purpose:
# exit_manager only attempts cancel_order when str(id).isdigit().
BACKSTOP_SENTINEL = "backstop"


def _short(symbol: str) -> str:
    s = str(symbol).split(":", 1)[-1].upper()
    return s.split("-", 1)[0] if "-USD" in s else s


def bulk_symbol(internal: str) -> str:
    """Internal uppercase ticker -> Bulk 'TICKER-USD' symbol."""
    u = str(internal).split(":", 1)[-1].upper().split("-", 1)[0]
    return f"{u}-USD"


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _find_key(obj, keys, _depth=0):
    """Depth-first search for the first of `keys` in nested dict/list JSON."""
    if _depth > 6:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = _find_key(v, keys, _depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, keys, _depth + 1)
            if r is not None:
                return r
    return None


class BulkClient:
    def __init__(self):
        self.network = (os.getenv("BULK_NETWORK", "mainnet") or "mainnet").strip().lower()
        self.rest_url = (os.getenv("BULK_REST_URL") or DEFAULT_REST.get(self.network, DEFAULT_REST["mainnet"])).rstrip("/")
        self.account_address = (os.getenv("BULK_ACCOUNT_ADDRESS") or "").strip()
        self._priv = (os.getenv("BULK_PRIVATE_KEY") or "").strip()
        self.is_agent = os.getenv("BULK_AGENT", "0") == "1"
        self.native_stops = os.getenv("BULK_NATIVE_STOPS", "1") == "1"
        self.stop_fail_mode = (os.getenv("BULK_STOP_FAIL_MODE", "backstop") or "backstop").lower()
        self.order_path = os.getenv("BULK_ORDER_PATH", "/order")
        self.account_path = os.getenv("BULK_ACCOUNT_PATH", "/account")
        self.price_path = os.getenv("BULK_PRICE_PATH", "/ticker")     # + /{SYMBOL-USD}
        self.klines_path = os.getenv("BULK_KLINES_PATH", "/klines")
        self.stop_slip_pct = _f(os.getenv("BULK_STOP_SLIP_PCT", "1.5"), 1.5) or 0.0
        self.mark_source = (os.getenv("BULK_MARK_SOURCE", "oracle") or "oracle").strip().lower()
        self.exec_slip_pct = _f(os.getenv("BULK_EXEC_SLIP_PCT", "0"), 0.0) or 0.0

        self._signer = None
        self._signer_pub = None
        self._prepare_order = None       # bulk_keychain.prepare_order (agent-capable)
        self.asset_meta = {}             # {SYMBOL: {symbol, lot, tick, min_notional, native_stop}}
        self.last_open_error = None
        self.price_fallback: Optional[Callable] = None   # e.g. HL get_all_prices
        self._px_cache = {}              # SYMBOL -> (ts, px)
        self._stop_ids = {}              # SYMBOL -> resting native stop order id
        self._sess = requests.Session()

    # ---- lifecycle -------------------------------------------------
    def set_price_fallback(self, fn: Callable):
        """Wire an external mark source (fan-out app injects HL marks)."""
        self.price_fallback = fn

    def init_sdk(self) -> bool:
        try:
            import bulk_keychain as bk
            from bulk_keychain import Keypair, Signer
        except Exception as e:
            logger.error(f"bulk_keychain import failed: {e}")
            return False
        if not self._priv:
            logger.error("BULK_PRIVATE_KEY missing")
            return False
        try:
            kp = Keypair.from_base58(self._priv)
            self._signer_pub = kp.pubkey if isinstance(kp.pubkey, str) else str(kp.pubkey)
        except Exception as e:
            logger.error(f"bad BULK_PRIVATE_KEY: {e}")
            return False
        # signature domain string: accept "mainnet" / "Mainnet" spellings
        last = None
        for dom in (self.network, self.network.capitalize(), self.network.upper()):
            try:
                self._signer = Signer(kp, dom)
                self.network = dom
                break
            except Exception as e:
                last = e
        if self._signer is None:
            logger.error(f"bulk_keychain rejected signature domain '{self.network}': {last}")
            return False
        self._prepare_order = getattr(bk, "prepare_order", None)

        if self.is_agent:
            if not self.account_address:
                logger.error("BULK_AGENT=1 requires BULK_ACCOUNT_ADDRESS (the master wallet)")
                return False
            if self._prepare_order is None:
                logger.error("bulk-keychain too old for agent signing (need prepare_order; "
                             "pin bulk-keychain>=0.1.26)")
                return False
            if self.account_address == self._signer_pub:
                logger.warning("BULK_AGENT=1 but the key IS the master account; agent mode is moot")
        else:
            if self.account_address and self.account_address != self._signer_pub:
                logger.warning(f"BULK_ACCOUNT_ADDRESS {self.account_address} != key pubkey "
                               f"{self._signer_pub}; using key pubkey (account==signer). "
                               f"Set BULK_AGENT=1 if this key is an agent wallet.")
            self.account_address = self._signer_pub
        logger.info(f"Bulk signer ready [{self.network}]: account {self.account_address}"
                    f"{' (agent ' + self._signer_pub + ')' if self.is_agent else ''}")

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
            types = {str(t).upper() for t in (m.get("orderTypes") or [])}
            meta[base] = {
                "symbol": sym,
                "lot": _f(m.get("lotSize") or m.get("lot_size"), 0.0) or 0.0,
                "tick": _f(m.get("tickSize") or m.get("tick_size"), 0.0) or 0.0,
                "min_notional": _f(m.get("minNotional") or m.get("min_notional"), 10.0) or 10.0,
                "max_leverage": _f(m.get("maxLeverage"), 0.0) or 0.0,
                "native_stop": ("STOP" in types) if types else False,
            }
        if not meta:
            return False
        self.asset_meta = meta
        n_native = sum(1 for v in meta.values() if v["native_stop"])
        logger.info(f"Bulk market meta loaded for {len(meta)} symbols "
                    f"({n_native} with native STOP; native_stops={'on' if self.native_stops else 'off'})")
        return True

    def lists(self, internal: str) -> bool:
        return _short(bulk_symbol(internal)) in self.asset_meta

    def signing_works(self) -> bool:
        try:
            self._sign({"type": "order", "symbol": "SOL-USD", "is_buy": True,
                        "price": 0, "size": 0.0, "reduce_only": False,
                        "order_type": {"type": "market", "is_market": True}})
            return True
        except Exception:
            return False

    # ---- HTTP ------------------------------------------------------
    def _get(self, path: str, params: Optional[dict] = None, retries: int = MAX_RETRIES):
        url = self.rest_url + path
        for attempt in range(1, retries + 1):
            try:
                r = self._sess.get(url, params=params, timeout=HTTP_TIMEOUT)
                if r.status_code in RETRYABLE_STATUS:
                    raise requests.HTTPError(str(r.status_code))
                r.raise_for_status()
                return r.json()
            except Exception as e:
                if attempt == retries:
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
                try:
                    return r.json()
                except ValueError:
                    return {"error": f"non-json response HTTP {r.status_code}", "http": r.status_code}
            except Exception as e:
                if attempt == MAX_RETRIES:
                    logger.error(f"POST {path} failed after {attempt}: {e}")
                    return None
                time.sleep(BASE_BACKOFF_SEC * attempt)
        return None

    # ---- signing ---------------------------------------------------
    def _sign(self, order: dict) -> dict:
        """Sign one action dict. Agent mode: preimage carries the MASTER account
        pubkey (bulk-keychain prepare_order) and the agent key signs it."""
        if self._prepare_order is not None:
            prepared = self._prepare_order(order, self.network, self.account_address,
                                           signer=self._signer_pub, nonce=None)
            return self._signer.sign_prepared(prepared)
        if self.is_agent:
            raise RuntimeError("agent signing requires bulk_keychain.prepare_order")
        return self._signer.sign(order)

    def _submit_signed(self, order: dict):
        """Sign a single action dict and POST it. Returns (response, signed)
        where response is the API dict or None. Honors DRY_RUN."""
        try:
            signed = self._sign(order)
        except Exception as e:
            logger.error(f"bulk sign failed: {e}")
            return None, None
        actions = signed.get("actions")
        if isinstance(actions, str):            # older libs returned JSON text
            import json as _json
            try:
                actions = _json.loads(actions)
            except Exception:
                pass
        body = {
            "actions": actions,
            "nonce": signed.get("nonce"),
            "account": signed.get("account") or self.account_address,
            "signer": signed.get("signer") or self._signer_pub,
            "signature": signed.get("signature"),
        }
        if DRY_RUN:
            logger.info(f"[DRY] would POST {self.order_path} {order.get('type')} {order.get('symbol')}")
            return {"status": "ok", "dry": True, "order_id": signed.get("order_id")}, signed
        res = self._post(self.order_path, body)
        if isinstance(res, dict) and signed.get("order_id"):
            res.setdefault("order_id", signed.get("order_id"))
        return res, signed

    @staticmethod
    def _ok(res) -> bool:
        if res is None:
            return False
        if isinstance(res, dict):
            if res.get("success") is False or res.get("error") or res.get("errors"):
                return False
            st = str(res.get("status", "")).lower()
            if st in ("error", "rejected", "failed"):
                return False
            # per-action rejection inside statuses[]
            for s in res.get("statuses") or []:
                if isinstance(s, dict) and (s.get("error") or "error" in s):
                    return False
        return True

    @staticmethod
    def _oid(res, signed=None) -> Optional[str]:
        if signed and signed.get("order_id"):
            return str(signed["order_id"])
        v = _find_key(res, ("oid", "order_id", "orderId")) if res is not None else None
        return str(v) if v else None

    # ---- prices ----------------------------------------------------
    def _ticker_price(self, internal: str) -> Optional[float]:
        sym = _short(internal)
        now = time.time()
        hit = self._px_cache.get(sym)
        if hit and now - hit[0] < PRICE_TTL_SEC:
            return hit[1]
        data = self._get(f"{self.price_path}/{bulk_symbol(sym)}", retries=1)
        px = None
        if isinstance(data, dict):
            # oracle-anchored by default: Bulk's local mark can be dislocated by a
            # sparse book (their 6 Sep incident); the external oracle is robust.
            order = (("oraclePrice", "markPrice", "lastPrice") if self.mark_source == "oracle"
                     else ("markPrice", "oraclePrice", "lastPrice"))
            for k in order + ("mark", "price"):
                px = _f(data.get(k))
                if px is not None and px > 0:
                    break
        if px is not None and px > 0:
            self._px_cache[sym] = (now, px)
            return px
        return None

    def get_all_prices(self, extra=None) -> dict:
        """Marks for the requested coins (`extra`, i.e. this venue's universe +
        open positions) from Bulk's per-symbol ticker; anything missing is
        filled from the injected fallback (HL marks)."""
        want = [_short(c) for c in (extra or []) if c]
        out = {}
        for sym in want:
            if sym not in self.asset_meta:
                continue
            px = self._ticker_price(sym)
            if px:
                out[sym] = px
        if self.price_fallback and (not want or len(out) < len(want)):
            try:
                fb = self.price_fallback(extra) or {}
                for k, v in fb.items():
                    k = _short(k)
                    if k not in out and v:
                        out[k] = float(v)
            except Exception as e:
                logger.warning(f"bulk price fallback failed: {e}")
        return out

    def get_price(self, symbol: str) -> Optional[float]:
        px = self._ticker_price(symbol)
        if px:
            return px
        if self.price_fallback:
            try:
                return (self.price_fallback([symbol]) or {}).get(_short(symbol))
            except Exception:
                return None
        return None

    # ---- account (positions + equity via unsigned fullAccount) -----
    def _full_account(self) -> Optional[dict]:
        """Return the fullAccount object, or None on ANY failed/unrecognized
        read. Callers must treat None as 'unknown', never as 'empty'."""
        if not self.account_address:
            return None
        acct = self._post(self.account_path, {"type": "fullAccount", "user": self.account_address})
        if acct is None:
            return None
        # documented: [{ "fullAccount": {...} }]; tolerate {data: ...} / bare object
        node = acct
        for _ in range(3):
            if isinstance(node, list):
                node = next((x for x in node if isinstance(x, dict)), None)
            if isinstance(node, dict):
                if "fullAccount" in node:
                    node = node["fullAccount"]; continue
                if "data" in node and isinstance(node["data"], (dict, list)):
                    node = node["data"]; continue
            break
        if isinstance(node, dict) and ("margin" in node or "positions" in node):
            return node
        logger.warning(f"fullAccount: unrecognized response shape ({str(acct)[:160]})")
        return None

    def get_positions(self):
        """None on FAILED read, [] on confirmed-empty. Normalizes to
        {coin, szi (+long/-short), entryPx}."""
        fa = self._full_account()
        if fa is None:
            return None
        out = []
        for p in fa.get("positions") or []:
            if not isinstance(p, dict):
                continue
            sym = _short(p.get("symbol", p.get("coin", "")))
            if not sym:
                continue
            szi = _f(p.get("size", p.get("szi", p.get("sz"))), 0.0) or 0.0
            if szi == 0:
                continue
            entry = _f(p.get("price", p.get("entryPx", p.get("entryPrice", p.get("avgPrice")))), 0.0) or 0.0
            out.append({"coin": sym, "szi": szi, "entryPx": entry, "symbol": sym,
                        "mark": _f(p.get("fairPrice")), "iso": bool(p.get("iso"))})
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
        fa = self._full_account()
        if not fa:
            return 0.0
        m = fa.get("margin") or {}
        for k in ("totalMargin", "totalBalance", "accountValue", "equity", "totalEquity"):
            v = _f(m.get(k))
            if v is not None:
                return v
        return 0.0

    def get_user_fills(self, start_ms: Optional[int] = None) -> list:
        """fullAccount carries no fill history; reconcile books at the stop
        estimate after its debounce rather than fabricating."""
        return []

    # ---- leverage (2x is under every Bulk symbol cap) --------------
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        return True

    # ---- rounding --------------------------------------------------
    def _meta(self, internal: str) -> dict:
        return self.asset_meta.get(_short(bulk_symbol(internal)), {})

    def _round_size(self, internal: str, size: float) -> float:
        lot = self._meta(internal).get("lot") or 0.0
        if lot > 0:
            size = math.floor(size / lot + 1e-9) * lot
        return round(size, 8)

    def _round_px(self, internal: str, px: float) -> float:
        tick = self._meta(internal).get("tick") or 0.0
        if tick > 0:
            px = round(px / tick) * tick
        return round(px, 8)

    # ---- market open / close ---------------------------------------
    def _exec_order(self, sym: str, is_buy: bool, size: float, reduce_only: bool,
                    ref_px: Optional[float]) -> dict:
        """Entry/close action. Default: market. With BULK_EXEC_SLIP_PCT>0: an IOC
        LIMIT at ref +/- cap, so a thin book cannot sweep us far past the mark."""
        if self.exec_slip_pct > 0 and ref_px and ref_px > 0:
            cap = self.exec_slip_pct / 100.0
            px = self._round_px(sym, ref_px * (1 + cap) if is_buy else ref_px * (1 - cap))
            return {"type": "order", "symbol": sym, "is_buy": bool(is_buy), "price": px,
                    "size": size, "reduce_only": bool(reduce_only), "iso": False,
                    "order_type": {"type": "limit", "tif": "IOC"}}
        return {"type": "order", "symbol": sym, "is_buy": bool(is_buy), "price": 0,
                "size": size, "reduce_only": bool(reduce_only), "iso": False,
                "order_type": {"type": "market", "is_market": True}}

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
        order = self._exec_order(sym, is_buy, size, False, current_price)
        logger.info(f"    -> market_open {'BUY' if is_buy else 'SELL'} {size} {sym} "
                    f"(ref ${current_price:.4f}, notional ${size*current_price:.2f})")
        res, signed = self._submit_signed(order)
        if not self._ok(res):
            self.last_open_error = self._err(res) or "not_filled"
            return None
        return {"filled": True, "avg_price": current_price, "total_size": size,
                "status": "filled", "oid": self._oid(res, signed)}

    def market_close(self, symbol: str, size: float, is_long: bool,
                     current_price: Optional[float] = None) -> Optional[dict]:
        sym = bulk_symbol(symbol)
        if current_price is None or current_price <= 0:
            current_price = self.get_price(symbol) or 0
        size = self._round_size(symbol, size)
        if size <= 0:
            return None
        order = self._exec_order(sym, not is_long, size, True, current_price)
        logger.info(f"    <- market_close {'SELL' if is_long else 'BUY'} {size} {sym} (reduce_only)")
        res, signed = self._submit_signed(order)
        if not self._ok(res):
            return None
        # Position is flat: pull any resting native stop so it can't fire later
        # and open a fresh (reverse) position. exit_manager only cancels numeric
        # ids, so this is the venue's own responsibility.
        self._cancel_tracked_stop(symbol)
        return {"filled": True, "avg_price": current_price, "status": "filled",
                "oid": self._oid(res, signed)}

    # ---- stops: NATIVE on mainnet, software backstop as the net ----
    def _stop_supported(self, symbol: str) -> bool:
        return self.native_stops and bool(self._meta(symbol).get("native_stop"))

    def _cancel_tracked_stop(self, symbol: str):
        sid = self._stop_ids.pop(_short(symbol), None)
        if sid and sid != BACKSTOP_SENTINEL:
            try:
                self.cancel_order(symbol, sid)
            except Exception as e:
                logger.warning(f"cancel resting stop {symbol} {sid} failed: {e}")

    def place_stop_market(self, symbol: str, is_long: bool, size: float,
                          stop_px: float) -> Optional[str]:
        """Place a native stop-market that closes the position (sell for a
        long, buy for a short). Returns the base58 order id (non-numeric, so
        exit_manager never tries to cancel it directly), or BACKSTOP_SENTINEL to
        keep the position under the software backstop when native placement is
        unavailable/fails (BULK_STOP_FAIL_MODE=backstop), or None to make the
        caller flatten (BULK_STOP_FAIL_MODE=flatten)."""
        coin = _short(symbol)
        if not self._stop_supported(symbol):
            return BACKSTOP_SENTINEL
        size = self._round_size(symbol, size)
        trig = self._round_px(symbol, stop_px)
        order = {"type": "stop", "symbol": bulk_symbol(symbol), "is_buy": (not is_long),
                 "size": size, "trigger_price": trig, "iso": False}
        if self.stop_slip_pct > 0:
            # STOP-LIMIT: bound the fill to trigger -/+ cap (sell below for a long
            # close, buy above for a short close). A gap past the cap leaves the
            # stop unfilled and the software backstop takes over.
            cap = self.stop_slip_pct / 100.0
            order["limit_price"] = self._round_px(symbol, trig * (1 + cap) if (not is_long) else trig * (1 - cap))
        res, signed = self._submit_signed(order)
        oid = self._oid(res, signed) if self._ok(res) else None
        if oid:
            self._stop_ids[coin] = oid
            lim = order.get("limit_price")
            logger.info(f"    [stop] native stop {coin} @ {trig} "
                        f"{'lim ' + str(lim) if lim is not None else '(market)'} id {oid}")
            return oid
        logger.warning(f"native stop {coin} @ {trig} not confirmed: {self._err(res)} "
                       f"-> {'software backstop' if self.stop_fail_mode != 'flatten' else 'FLATTEN'}")
        if self.stop_fail_mode == "flatten":
            return None
        self._stop_ids[coin] = BACKSTOP_SENTINEL
        return BACKSTOP_SENTINEL

    def modify_stop(self, symbol: str, is_long: bool, size: float,
                    old_id, new_stop: float) -> Optional[str]:
        """Move the stop: cancel the resting native stop, then place the new
        one (never two live stops at once - that could double-close and flip).
        The brief gap is covered by the software backstop."""
        coin = _short(symbol)
        if not self._stop_supported(symbol):
            return BACKSTOP_SENTINEL
        old = old_id if old_id not in (None, "", BACKSTOP_SENTINEL) else self._stop_ids.get(coin)
        if old and old != BACKSTOP_SENTINEL:
            if not self.cancel_order(symbol, old):
                logger.warning(f"modify_stop {coin}: cancel of {old} not confirmed; "
                               f"leaving it in place (no replacement to avoid a double stop)")
                return str(old)
            self._stop_ids.pop(coin, None)
        new_id = self.place_stop_market(symbol, is_long, size, new_stop)
        return new_id if new_id is not None else BACKSTOP_SENTINEL

    def cancel_order(self, symbol: str, oid) -> bool:
        if oid in (None, "", BACKSTOP_SENTINEL):
            return True
        order = {"type": "cancel", "symbol": bulk_symbol(symbol), "order_id": str(oid)}
        res, _ = self._submit_signed(order)
        return self._ok(res)

    # ---- candles (not on the fan-out hot path; HL is canonical) ----
    def fetch_candles(self, symbol: str, interval: str = "4h", limit: int = 200) -> list:
        span = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400,
                "1d": 86400}.get(interval, 14400) * 1000
        now_ms = int(time.time() * 1000)
        data = self._get(self.klines_path, params={
            "symbol": bulk_symbol(symbol), "interval": interval,
            "startTime": now_ms - span * (limit + 2), "endTime": now_ms})
        rows = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])
        out = []
        for c in rows or []:
            try:
                if isinstance(c, dict):
                    t = int(c.get("t") or c.get("time") or c.get("openTime"))
                    out.append({"t": t, "T": int(c.get("T") or c.get("closeTime") or (t + span)),
                                "o": float(c.get("o", c.get("open"))), "h": float(c.get("h", c.get("high"))),
                                "l": float(c.get("l", c.get("low"))), "c": float(c.get("c", c.get("close"))),
                                "v": float(c.get("v", c.get("volume") or 0))})
                elif isinstance(c, (list, tuple)) and len(c) >= 5:
                    t = int(c[0])
                    out.append({"t": t, "T": t + span, "o": float(c[1]), "h": float(c[2]),
                                "l": float(c[3]), "c": float(c[4]),
                                "v": float(c[5]) if len(c) > 5 else 0.0})
            except (TypeError, ValueError, KeyError):
                continue
        out.sort(key=lambda x: x["t"])
        return out

    @staticmethod
    def _err(res) -> Optional[str]:
        if isinstance(res, dict):
            e = res.get("error") or res.get("message") or res.get("msg")
            if e:
                return str(e)
            for s in res.get("statuses") or []:
                if isinstance(s, dict) and s.get("error"):
                    return str(s["error"])
        return None
