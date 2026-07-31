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
  8. Fees: the docs say 0.075%, but live /trades rows report feeRate 0.00045 —
     0.045% taker, the same as HL. Observed, not assumed.

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
# How long to wait for a filled entry to surface as a queryable position before
# giving up on attaching its stop. See _position_id().
POSITION_ID_WAIT = float(os.getenv("POSITION_ID_WAIT", "20"))
# /trades `type` values, per the docs: open | increase | reduce | close | flip
# | liquidation. Only these four realise PnL. `increase` adds to a position and
# must NOT count as closing, or an average-up would book a phantom exit.
_CLOSING_TYPES = ("reduce", "close", "flip", "liquidation")
# /trades paging. 100 is the accepted cap — 200 is a bare 400. See get_user_fills().
_FILLS_PAGE = 100
_FILLS_MAX_ROWS = 2000     # backstop so a paging bug can never loop forever
_POSITIONS_MAX_ROWS = 1000 # same backstop for the positions pager


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
        self.last_error = None
        self._close_shape = None
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
                try:
                    self.last_error = r.json()
                except ValueError:
                    self.last_error = {"message": r.text[:200]}
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

        # ORDER MATTERS. On Propr's account schema:
        #   balance / crossWalletBalance = WALLET, excludes unrealised PnL
        #   marginBalance               = wallet + unrealised = EQUITY
        # Using `balance` would make the daily guard blind to open-position
        # drawdown — it would sit flat while positions bled, which is exactly
        # the failure the guard exists to prevent. Propr breaches on equity.
        if acct.get("marginBalance") is not None:
            return _f(acct["marginBalance"])
        for k in ("equity", "currentEquity", "totalEquity", "accountValue"):
            if acct.get(k) is not None:
                return _f(acct[k])
        wallet = acct.get("balance", acct.get("crossWalletBalance"))
        if wallet is not None:
            upnl = _f(acct.get("totalUnrealizedPnl", acct.get("crossUnrealizedPnl")))
            return _f(wallet) + upnl
        logger.error("equity: no recognised balance field — see [shape:account] above")
        return 0.0

    def high_water_mark(self) -> Optional[float]:
        """Propr's OWN high-water mark — the anchor its trailing drawdown uses.
        Never reconstruct this locally: if our HWM lags theirs by a spike we
        didn't sample, our floor sits below the real one and the guard fires
        too late."""
        a = self._attempt() or {}
        hwm = (a.get("account") or {}).get("highWaterMark")
        return _f(hwm) if hwm is not None else None

    def challenge_rules(self) -> dict:
        """Phase rules as Propr states them. Field names are still unconfirmed,
        so this logs [shape:phase] and returns whatever it can resolve; the
        guard falls back to env config for anything missing."""
        a = self._attempt() or {}
        ch = a.get("challenge") or {}
        # The ATTEMPT's phases track progress (startingBalance, order, status).
        # The RULES live on the CHALLENGE's phases. Join them on phaseId.
        att_phases = [p for p in (a.get("phases") or []) if isinstance(p, dict)]
        ch_phases = [p for p in (ch.get("phases") or []) if isinstance(p, dict)]
        if att_phases:
            self._log_unmapped("attempt_phase", att_phases[0])
        if ch_phases:
            self._log_unmapped("challenge_phase", ch_phases[0])
        cur_att = next((p for p in att_phases
                        if a.get("currentPhaseId") in (p.get("attemptPhaseId"),
                                                       p.get("phaseId"), p.get("id"))), None)
        cur_att = cur_att or (att_phases[0] if att_phases else {})
        want = cur_att.get("phaseId")
        cur = next((p for p in ch_phases
                    if p.get("phaseId") == want or p.get("id") == want), None)
        cur = cur or (ch_phases[0] if ch_phases else {})
        cur_id = a.get("currentPhaseId")
        def pick(*keys):
            for k in keys:
                if cur.get(k) is not None:
                    return _f(cur[k])
            return None
        return {
            "daily_loss_pct": pick("maxDailyLossPercent", "maxDailyLoss",
                                   "dailyLossLimit", "dailyLossPercent"),
            "max_dd_pct": pick("maxDrawdownPercent", "maxDrawdown", "drawdownPercent"),
            "dd_type": str(cur.get("drawdownType") or cur.get("maxDrawdownType") or "").lower(),
            "target_pct": pick("profitTargetPercent", "profitTarget", "targetPercent"),
            "challenge": ch.get("name"),
            "phases_total": len(ch_phases) or len(att_phases) or None,
            "phase_order": cur_att.get("order"),
            "phase_id": cur_id,
        }

    def equity_breakdown(self) -> dict:
        """Diagnostic: the components behind get_equity(), for the boot log."""
        a = self._attempt() or {}
        acct = a.get("account") or {}
        return {k: acct.get(k) for k in
                ("marginBalance", "balance", "availableBalance", "crossWalletBalance",
                 "totalUnrealizedPnl", "highWaterMark", "marginLevel")}

    # ── positions ───────────────────────────────────────────────
    def _raw_positions(self) -> Optional[list]:
        """Open positions, PAGED.

        This used to send no params at all and read a single default page. That
        page is small and newest-first, so any position older than it was
        INVISIBLE to the bot — and invisible is indistinguishable from closed
        everywhere downstream. It is the reason DOT (opened 27 Jul) and PENGU
        sat live and unmanaged for days while the ledger showed them stopped
        out: reconcile() saw them absent and booked a close, _check_orphans()
        iterates this same list so it could never flag them, and nothing was
        left watching their risk.

        The old comment here claimed "the server rejects status/limit with a
        bare 400". That was the original misdiagnosis and it is wrong: the docs
        list `status` (open/closed/liquidated), `limit` and `offset` as
        supported on this endpoint. What actually 400s is limit ABOVE the cap —
        the same mistake as `?limit=200` on /trades. Ask for status=open and
        page properly.
        """
        rows, offset = [], 0
        while offset < _POSITIONS_MAX_ROWS:
            d = self._req("GET", f"/accounts/{self.account_id}/positions",
                          params={"status": "open", "limit": _FILLS_PAGE,
                                  "offset": offset})
            if d is None:
                # A failed read must never look flat (the Farms lesson). If we
                # already have pages, return them; otherwise signal failure.
                return rows or None
            page = d.get("data", [])
            rows.extend(page)
            if len(page) < _FILLS_PAGE:
                break
            offset += _FILLS_PAGE
        keep = [p for p in rows
                if _f(p.get("quantity")) != 0
                and str(p.get("status", "open")).lower() == "open"]
        # Surface a status vocabulary we don't recognise rather than silently
        # filtering live positions out of existence.
        seen = {str(p.get("status", "")).lower() for p in rows if p.get("status")}
        unknown = seen - {"open", "closed", "liquidated", ""}
        if unknown and "pos_status" not in self._logged_shapes:
            self._logged_shapes.add("pos_status")
            logger.warning(f"positions: unrecognised status values {sorted(unknown)} "
                           f"— verify none of these mean OPEN")
        return keep

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
        """Conditional orders need this. Only exists once the entry has filled.

        The fill and the position record are not atomic, and the observed lag on
        Propr is routinely longer than the ~3s this used to wait. Losing the race
        is expensive: place_stop_market() returns None, maybe_enter() tries to
        flatten (often also rejected, because the position isn't queryable yet
        either), and the entry ends up recorded with hard_stop_id=None. Wait
        with backoff up to POSITION_ID_WAIT seconds instead — a slow entry costs
        seconds, a missed stop costs the position.
        """
        target, want = short_name(symbol), "long" if is_long else "short"
        deadline = time.time() + POSITION_ID_WAIT
        attempt, delay = 0, 0.4
        while True:
            for p in (self._raw_positions() or []):
                if (short_name(str(p.get("asset", ""))) == target
                        and str(p.get("positionSide", "")).lower() == want
                        and _f(p.get("quantity")) != 0):
                    if attempt:
                        logger.info(f"    ↳ {target}: positionId resolved after "
                                    f"{attempt + 1} tries")
                    return p.get("positionId")
            attempt += 1
            if time.time() + delay >= deadline:
                break
            time.sleep(delay)
            delay = min(delay * 1.6, 3.0)
        logger.error(f"{target}: positionId never appeared after {attempt} tries "
                     f"(~{POSITION_ID_WAIT:.0f}s)")
        return None

    # ── orders ──────────────────────────────────────────────────
    def _order(self, body: dict) -> Optional[dict]:
        body.setdefault("intentId", _ulid())
        body.setdefault("accountId", self.account_id)
        body.setdefault("exchange", "hyperliquid")
        body.setdefault("productType", "perp")
        # Reference SDK: IOC for market, GTC otherwise. We previously forced GTC
        # on everything, which diverges from every working example.
        body.setdefault("timeInForce", "IOC" if body.get("type") == "market" else "GTC")
        body.setdefault("closePosition", False)
        d = self._req("POST", f"/accounts/{self.account_id}/orders",
                      json={"orders": [body]})
        if not d:
            return None
        rows = d.get("data", [])
        return rows[0] if rows else None

    # Propr rejects sell+long with 13096 (order_side_must_align_with_position_side_
    # buy_long_or_sell_short). The intuitive mapping — "closing a long is a sell on
    # the long book" — is therefore invalid here. We try the accepted shapes in
    # order and cache whichever the API takes, so this costs one 400 per process.
    # Reduce-only shape discovery.
    #
    # The docs' canonical stop is side=sell + positionSide=long on a long, but the
    # live API rejected exactly that with 13096
    # (order_side_must_align_with_position_side_BUY_LONG_or_SELL_SHORT). Omitting
    # positionSide gives a bare 400, so the field is required. The literal reading
    # of the error is that Propr wants buy<->long / sell<->short and lets
    # reduceOnly do the reducing — so that shape is tried first.
    #
    # The docs are demonstrably wrong elsewhere (status/limit query params on
    # /positions also 400), so we trust the error text over the documentation and
    # verify empirically. Every shape is tried; the winner is cached.
    _REDUCE_SHAPES = ("aligned", "docs", "omit")

    def _reduce_shapes(self, is_long: bool):
        side = "sell" if is_long else "buy"
        for shape in ([self._close_shape] if self._close_shape else list(self._REDUCE_SHAPES)):
            if shape == "aligned":
                yield shape, side, ("long" if side == "buy" else "short")
            elif shape == "docs":
                yield shape, side, ("long" if is_long else "short")
            else:
                yield shape, side, None

    def _reduce_order(self, base_body: dict, is_long: bool, what: str):
        attempts = []
        for shape, side, pside in self._reduce_shapes(is_long):
            body = dict(base_body, side=side)
            if pside is not None:
                body["positionSide"] = pside
            else:
                body.pop("positionSide", None)
            res = self._order(body)
            if res is not None:
                if self._close_shape != shape:
                    logger.info(f"    ↳ {what}: shape '{shape}' "
                                f"(side={side} positionSide={pside}) ACCEPTED — caching")
                    self._close_shape = shape
                return res
            err = (self.last_error or {})
            attempts.append(f"{shape}(side={side},positionSide={pside}) -> "
                            f"{err.get('code', '?')} {err.get('message', '')[:60]}")
            # Keep going regardless of the error code. Bailing early on a
            # non-13096 response is what stopped the fallback from ever running.
        import json as _json
        logger.error(f"❌ {what}: every reduce-only shape rejected.\n"
                     f"   attempts: {' | '.join(attempts)}\n"
                     f"   last body sent: {_json.dumps(body)}")
        return None

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

    def market_open_with_stop(self, symbol: str, is_buy: bool, notional_usd: float,
                              stop_px: float, current_price: Optional[float] = None
                              ) -> Optional[dict]:
        """Entry AND its hard stop, submitted as ONE order group.

        This is the documented second half of
        13056 CONDITIONAL_ORDER_REQUIRES_POSITION_OR_GROUP: a conditional order
        needs a positionId *or* to sit in the same group as an entry order.
        Only the positionId branch was ever used, which is inherently racy —
        the position has to exist and be queryable before the stop can be
        attached, and every failure left a filled position riding naked.

        With a group there is no window at all: the stop is accepted with the
        entry or the whole batch is rejected. Per the docs, a top-level
        `orderGroupId` (a ULID we generate) is required whenever orders.length
        > 1, and only one entry order is allowed per request.

        Returns None if the group is rejected, so the caller can fall back to
        the old two-step flow.
        """
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
        trigger = self._round_px(symbol, stop_px)
        common = {"asset": asset, "base": asset, "quote": "USDC",
                  "exchange": "hyperliquid", "productType": "perp",
                  "quantity": str(size)}
        entry = dict(common, intentId=_ulid(), type="market",
                     side="buy" if is_buy else "sell",
                     positionSide="long" if is_buy else "short",
                     reduceOnly=False, closePosition=False, timeInForce="IOC")
        # The stop leg uses the ALIGNED shape (buy<->long, sell<->short), which
        # is what the live API accepts — verified from cached shape discovery
        # ("stop kPEPE: shape 'aligned' (side=sell positionSide=short)" closing
        # a long). The docs' example uses side=sell/positionSide=long and the
        # live API rejects exactly that with 13096.
        stop = dict(common, intentId=_ulid(), type="stop_market",
                    side="sell" if is_buy else "buy",
                    positionSide="short" if is_buy else "long",
                    triggerPrice=str(trigger), reduceOnly=True,
                    closePosition=False, timeInForce="GTC")
        d = self._req("POST", f"/accounts/{self.account_id}/orders",
                      json={"orderGroupId": _ulid(), "accountId": self.account_id,
                            "orders": [entry, stop]})
        if not d:
            err = (self.last_error or {})
            self.last_open_error = (f"group rejected: {err.get('code', '?')} "
                                    f"{str(err.get('message', ''))[:80]}")
            logger.warning(f"⚠️  {asset}: entry+stop group rejected "
                           f"({self.last_open_error}) — falling back to two-step")
            return None
        rows = d.get("data", [])
        if not rows:
            self.last_open_error = "group returned no orders"
            return None
        # Match the legs back by type; order in the response is not guaranteed.
        e_row = next((r for r in rows if str(r.get("type", "")).lower() == "market"), rows[0])
        s_row = next((r for r in rows if str(r.get("type", "")).lower() == "stop_market"), None)
        fill = _f(e_row.get("averageFillPrice")) or px
        stop_oid = s_row.get("orderId") if s_row else None
        logger.info(f"✅ {asset} open {'long' if is_buy else 'short'} {size} @ ${fill:.6f} "
                    f"({e_row.get('orderId')})  🛡 stop @ ${trigger:.6f} ({stop_oid})")
        return {"filled": True, "avg_price": fill, "total_size": size, "size": size,
                "oid": e_row.get("orderId"), "stop_oid": str(stop_oid) if stop_oid else None,
                "stop_px": trigger, "raw": d}

    def market_close(self, symbol: str, size: float, is_long: bool,
                     current_price: Optional[float] = None) -> Optional[dict]:
        asset = hl_symbol(short_name(symbol))
        size = self._round(symbol, size)
        if size <= 0:
            logger.warning(f"⚠️  {asset}: close size rounded to 0")
            return None
        res = self._reduce_order({
            "type": "market", "asset": asset, "base": asset, "quote": "USDC",
            "quantity": str(size),
            "reduceOnly": True, "closePosition": True,        # never flips
        }, is_long, f"close {asset}")
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
        res = self._reduce_order({
            "positionId": pid, "type": "stop_market",
            "asset": asset, "base": asset, "quote": "USDC",
            "quantity": str(size), "triggerPrice": str(trigger), "reduceOnly": True,
        }, is_long, f"stop {asset}")
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
        """Propr /trades mapped to HL userFills shape: coin, dir, px, sz, fee.

        PAGING, THE HARD-WON VERSION. `limit=200` returns a bare 400 ("Bad
        Request Exception") and that is the whole origin of the phantom-close
        incident: every call failed, silently. _req() logged the 400 and
        returned None, we returned [], and reconcile() read "the fill never
        indexed" instead of "the fills endpoint is broken" — so every close
        fell through to the stop-estimate path, booking positions that never
        armed a trail at exactly the hard stop, a fabricated -10%.

        Probed empirically (repair.py --inspect): the cap is 100, not 200.
        `limit=100` is accepted and `offset` walks backwards through history;
        pageSize/perPage/page/startTime/from are all silently IGNORED (200 OK
        with the default 10 rows), which is worse than a 400 — they look like
        they work. Default page is only 10 rows / a few hours, so anything
        historical MUST page or it silently sees a sliver of the account.
        """
        out, offset = [], 0
        rows = []
        while offset < _FILLS_MAX_ROWS:
            d = self._req("GET", f"/accounts/{self.account_id}/trades",
                          params={"limit": _FILLS_PAGE, "offset": offset})
            if not d:
                break
            page = d.get("data", [])
            rows.extend(page)
            if len(page) < _FILLS_PAGE:
                break
            offset += _FILLS_PAGE
            # Stop early once the page is entirely older than the caller's
            # window — history is returned newest-first.
            if start_ms and page:
                oldest = str(page[-1].get("executedAt") or "")
                try:
                    from datetime import datetime
                    if int(datetime.fromisoformat(
                            oldest.replace("Z", "+00:00")).timestamp() * 1000) < start_ms:
                        break
                except (ValueError, TypeError):
                    pass
        for t in rows:
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
            # Observed `type` values: open | close | flip. `flip` reverses a
            # position, so it BOTH closes and opens — it must count as closing
            # or its realized PnL is lost. isLiquidation / a non-zero
            # realizedPnl are backstops for any type string not seen yet.
            ttype = str(t.get("type", "")).lower()
            closing = (ttype in _CLOSING_TYPES
                       or bool(t.get("isLiquidation"))
                       or _f(t.get("realizedPnl")) != 0.0)
            # positionSide on a trade row mirrors the ORDER side (buy<->long,
            # sell<->short), NOT the direction of the position being closed:
            # the ZEC close of a SHORT reports side=buy, positionSide=long. HL's
            # `dir` names the POSITION, so a close inverts it.
            order_is_buy = str(t.get("side", "")).lower() == "buy"
            if closing:
                pos_side = "Short" if order_is_buy else "Long"
            else:
                pos_side = "Long" if order_is_buy else "Short"
            out.append({
                "coin": t.get("asset", ""),
                "dir": f"{'Close' if closing else 'Open'} {pos_side}",
                "px": t.get("price"), "sz": t.get("quantity"),
                "fee": t.get("fee"), "closedPnl": t.get("realizedPnl"),
                "time": ts,
                # One logical close is frequently SPLIT across several trade
                # rows sharing an orderId (a 3.468 BCH close arrived as
                # 0.493 + 1.196 + 1.779). Anything matching fills to a booked
                # trade must aggregate by this, or it sees only fragments.
                "oid": t.get("orderId"),
                # The venue's own position lifecycle id. Grouping fills by this
                # reconstructs true round trips exactly — far more reliable than
                # matching booked trades to fills by time and size, which cannot
                # tell a long's close from the close of a short that preceded it
                # on the same coin.
                "pid": t.get("positionId"),
                "type": ttype,
            })
        return out
