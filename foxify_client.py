"""
Foxify (Kitsune Signals) client — drop-in for ProprClient / HLClient.

Kitsune is a SIGNAL relay, not an order API. Three POST endpoints, all authed
by signalId + passphrase:

    /signals/trade      send a trade intent
    /signals/balance    currentBalance (= equity), availableBalance (= idle margin)
    /signals/positions  open positions, sized in USD

Consequences that shape this file:

  * No order IDs. /signals/trade returns {accepted: true} and nothing else, so
    there is no oid to cancel or modify. Reconciliation happens off
    /signals/positions, never off an order response.

  * No standalone stop orders. The hard stop rides along with the entry as the
    native `sl` percentage field. place_stop_market() therefore performs NO
    network call — the stop is already attached by the time it is called. It
    returns a synthetic id so position_manager's "no stop -> flatten" guard
    stays satisfied, because on this venue a missing stop is not possible: it
    either went in with the entry or the entry itself was rejected.

  * No trailing stops. Kitsune has tp1/tp2/sl/moveSlToEntry but nothing that
    trails. Libration's 0.55% trail stays bot-managed and exits arrive as
    ordinary close signals. modify_stop() is therefore a local no-op: the
    native sl stays parked at the hard 10% as a disaster backstop and never
    ratchets. Do NOT "fix" this by re-sending a trade with a tighter sl — that
    would be read as a DCA and would add exposure.

  * Positions are denominated in USD, not tokens. get_positions() converts back
    to a token quantity via entryPrice so position_manager needs no changes.

  * No candles or prices. Same as Propr: market data is read from Hyperliquid's
    public API through hl_reader. Execution venue and data venue are separate.

  * SIZE IS ALWAYS SENT AS EXPLICIT USD NOTIONAL. Never as a percentage string.
    Kitsune computes percentages against availableBalance (idle margin), which
    shrinks as the book fills — the tenth concurrent position would be sized
    off a much smaller base than the first, silently breaking equal weighting.
"""

import logging
import os
import time
from typing import Optional

import requests

import config
from config import hl_symbol, short_name

logger = logging.getLogger(__name__)

BASE = os.getenv("FOXIFY_API_BASE", "https://kitsunedev.foxify.trade/api").rstrip("/")
_TIMEOUT = 15


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class FoxifyClient:
    def __init__(self, hl_reader=None):
        self.signal_id = os.getenv("FOXIFY_SIGNAL_ID", "").strip()
        self.passphrase = os.getenv("FOXIFY_PASSPHRASE", "").strip()
        self.leverage = _f(os.getenv("FOXIFY_LEVERAGE", "2"), 2.0)
        self.min_notional = _f(os.getenv("FOXIFY_MIN_NOTIONAL", "10"), 10.0)
        # Tier max drawdown, as the venue states it. Surfaced through
        # challenge_rules() so an env typo can never loosen the guard.
        self.max_dd_pct = _f(os.getenv("FOXIFY_MAX_DD_PCT", "20"), 20.0)
        # See equity_breakdown(). Kitsune's docs contradict themselves on
        # whether currentBalance includes unrealised PnL; this is the escape
        # hatch if it turns out it does not.
        self.add_upnl = os.getenv("FOXIFY_EQUITY_ADD_UPNL", "0").strip() in ("1", "true", "yes")

        self.asset_meta = {}
        self.last_open_error = None
        self.last_error = None
        self._initial_balance = None

        # Market data from HL, read-only. Imported lazily so a missing HL key
        # can never take the Foxify service down at import time.
        if hl_reader is None:
            from hl_client import HLClient
            hl_reader = HLClient()
        self.hl = hl_reader

        if not self.signal_id:
            raise SystemExit("FOXIFY_SIGNAL_ID is not set")
        if not self.passphrase:
            raise SystemExit("FOXIFY_PASSPHRASE is not set")

    # ── HTTP ────────────────────────────────────────────────────
    def _post(self, path: str, body: dict) -> Optional[dict]:
        """Returns the decoded body, or None on failure.

        None means "we do not know", never "nothing there". Callers must not
        treat a failed read as an empty account.
        """
        url = f"{BASE}{path}"
        payload = dict(body, signalId=self.signal_id, passphrase=self.passphrase)
        for attempt in range(4):
            try:
                r = requests.post(url, json=payload, timeout=_TIMEOUT)
                if r.status_code == 200:
                    d = r.json()
                    # {accepted: false} = bad signalId or passphrase mismatch.
                    # Loud, because it is a config error, not a market event.
                    if isinstance(d, dict) and d.get("accepted") is False:
                        self.last_error = "rejected: unknown signalId or bad passphrase"
                        logger.error(f"❌ {path}: {self.last_error}")
                        return None
                    return d
                if r.status_code == 400:
                    self.last_error = f"400 {r.text[:200]}"
                    logger.error(f"❌ {path}: {self.last_error}")
                    return None            # malformed payload — retrying won't help
                logger.warning(f"{path}: HTTP {r.status_code} (attempt {attempt + 1}/4)")
            except requests.RequestException as e:
                self.last_error = str(e)
                logger.warning(f"{path}: {e} (attempt {attempt + 1}/4)")
            time.sleep(0.6 * (attempt + 1))
        return None

    # ── lifecycle ───────────────────────────────────────────────
    def init_sdk(self) -> bool:
        """Confirm the signal account answers, and seed asset meta from HL."""
        bal = self._post("/signals/balance", {})
        if bal is None:
            logger.error("Foxify: /signals/balance did not answer — refusing to start")
            return False
        self._initial_balance = _f(bal.get("currentBalance"))
        logger.info(
            f"✅ Foxify signal {self.signal_id} | balance ${self._initial_balance:.2f} "
            f"| available ${_f(bal.get('availableBalance')):.2f} "
            f"| open interest ${_f(bal.get('openInterest')):.2f}"
        )
        from hl_client import build_asset_meta
        self.asset_meta = build_asset_meta() or {}
        return True

    def signing_works(self) -> bool:
        return self._post("/signals/balance", {}) is not None

    # ── market data (Hyperliquid, public) ───────────────────────
    def get_all_prices(self, extra=None) -> dict:
        return self.hl.get_all_prices(extra)

    def get_price(self, symbol: str) -> Optional[float]:
        return self.hl.get_price(symbol)

    def fetch_candles(self, symbol: str, interval: str = "4h", limit: int = 200) -> list:
        return self.hl.fetch_candles(symbol, interval, limit)

    # ── equity ──────────────────────────────────────────────────
    def get_equity(self) -> float:
        """The number Foxify liquidates against — so the number the guard must
        measure. availableBalance is idle margin and is NOT equity; using it
        would make the guard read low and fire constantly as the book fills.

        See equity_breakdown() for why FOXIFY_EQUITY_ADD_UPNL exists. Default is
        off, trusting the /signals/balance docs that currentBalance already
        includes unrealised PnL. Verify that on day one with a losing position
        open before you trust the guard.
        """
        d = self._post("/signals/balance", {})
        if d is None:
            return 0.0
        eq = _f(d.get("currentBalance"))
        if self.add_upnl:
            for p in (self.get_positions() or []):
                eq += _f(p.get("unrealizedPnl"))
        return eq

    def available_balance(self) -> float:
        d = self._post("/signals/balance", {})
        return _f(d.get("availableBalance")) if d else 0.0

    def equity_breakdown(self) -> dict:
        """Every balance component the venue reports, plus our own unrealised
        sum from /signals/positions.

        This exists to settle a CONTRADICTION IN KITSUNE'S OWN DOCS. The
        /signals/balance section documents currentBalance as "balance +
        unrealized PnL", but the risk-based-sizing section says currentBalance
        is "the full account balance (not including unrealized PnL)". Those
        cannot both be true, and the difference is exactly the Propr
        balance-vs-marginBalance trap: if currentBalance excludes unrealised
        PnL, the drawdown guard sits flat while open positions bleed.

        Compare `current` against `current_plus_upnl` while a position is open
        and losing. If `current` already moved with the position, the docs'
        balance section is right and nothing needs changing. If it did not, set
        FOXIFY_EQUITY_ADD_UPNL=1 so get_equity() reads true equity.
        """
        d = self._post("/signals/balance", {}) or {}
        upnl = 0.0
        for p in (self.get_positions() or []):
            upnl += _f(p.get("unrealizedPnl"))
        current = _f(d.get("currentBalance"))
        return {
            "current": round(current, 4),
            "available": round(_f(d.get("availableBalance")), 4),
            "open_interest": round(_f(d.get("openInterest")), 4),
            "tilt": round(_f(d.get("tilt")), 4),
            "upnl_from_positions": round(upnl, 4),
            "current_plus_upnl": round(current + upnl, 4),
            "equity_used": round(current + (upnl if self.add_upnl else 0.0), 4),
            "add_upnl": self.add_upnl,
        }

    def high_water_mark(self) -> Optional[float]:
        """Foxify exposes no HWM, and needs none: max drawdown is measured from
        the INITIAL FUNDED BALANCE and never ratchets. Returning None keeps the
        static-floor path in the guard and prevents a trailing anchor being
        invented locally."""
        return None

    def challenge_rules(self) -> dict:
        """Limits as the venue states them.

        daily_loss_pct is deliberately absent: Foxify imposes no daily limit and
        no trailing drawdown. Max drawdown is the ONLY thing that ends the
        account — and it is terminal, taking the collateral with it. Any daily
        guard on this venue is self-imposed via DAILY_DD_PCT, which is exactly
        what we want, but it must not be presented as a venue rule.
        """
        return {"max_dd_pct": self.max_dd_pct, "dd_type": "static"}

    # ── positions ───────────────────────────────────────────────
    def _round(self, symbol: str, size: float) -> float:
        from hl_client import round_size
        return round_size(self.asset_meta, hl_symbol(short_name(symbol)), size)

    def get_positions(self):
        """HL-shaped dicts so position_manager needs no changes.

        Returns None on a failed read, never [] — an outage must not look flat,
        or the bot will happily re-enter on top of positions it already holds.
        """
        d = self._post("/signals/positions", {})
        if d is None:
            logger.warning("get_positions: Foxify read failed — returning None")
            return None
        if not isinstance(d, list):
            logger.warning(f"get_positions: unexpected shape {type(d).__name__} — returning None")
            return None

        out = []
        for p in d:
            entry = _f(p.get("entryPrice"))
            usd = abs(_f(p.get("size")))
            if entry <= 0 or usd <= 0:
                continue
            # Kitsune reports position size in USD; the rest of the bot thinks
            # in token quantity. Convert once, here, at the API boundary.
            qty = usd / entry
            is_long = str(p.get("direction", "")).lower() == "long"
            out.append({
                "coin": short_name(str(p.get("symbol", "")).split("-")[0]),
                "szi": qty if is_long else -qty,
                "entryPx": entry,
                "unrealizedPnl": p.get("unrealizedPnl"),
                "positionId": p.get("id"),
                "leverage": {"value": self.leverage},
            })
        return out

    def get_position(self, symbol: str) -> Optional[dict]:
        target = short_name(symbol)
        for p in (self.get_positions() or []):
            if short_name(str(p.get("coin", ""))) == target:
                return p
        return None

    # ── orders ──────────────────────────────────────────────────
    def market_open(self, symbol: str, is_buy: bool, notional_usd: float,
                    current_price: Optional[float] = None) -> Optional[dict]:
        """Open with the hard stop attached in the same payload.

        The stop goes in HERE rather than in a follow-up call because Kitsune
        has no standalone stop endpoint. Sending it with the entry also means
        the position is never unprotected, even for the moment between fill and
        stop placement that the Propr path has to defend against.
        """
        asset = short_name(symbol)
        px = current_price if (current_price or 0) > 0 else (self.get_price(asset) or 0)
        if px <= 0:
            self.last_open_error = "no price"
            return None
        if notional_usd < self.min_notional:
            self.last_open_error = (f"notional ${notional_usd:.2f} below the "
                                    f"${self.min_notional:.2f} minimum")
            logger.warning(f"⚠️  {asset}: {self.last_open_error}")
            return None

        res = self._post("/signals/trade", {
            "symbol": asset,
            "action": "buy" if is_buy else "sell",
            "size": round(notional_usd, 2),      # explicit USD notional — never a percentage
            "leverage": self.leverage,
            "sl": config.HARD_STOP_PCT,          # native server-side disaster stop
            "reduceOnly": False,
            "closeExistingFirst": False,         # isolated per-coin; we never flip in place
        })
        if not res:
            self.last_open_error = self.last_error or "trade rejected"
            return None

        # Kitsune does not return a fill price. Mark price at send is the best
        # estimate available; the authoritative entry arrives on the next
        # /signals/positions read and reconciliation will correct it.
        size = self._round(symbol, notional_usd / px)
        logger.info(f"✅ {asset} open {'long' if is_buy else 'short'} "
                    f"${notional_usd:.2f} (~{size}) @ ~${px:.6f}")
        return {"filled": True, "avg_price": px, "total_size": size,
                "size": size, "oid": None, "raw": res}

    def market_close(self, symbol: str, size: float, is_long: bool,
                     current_price: Optional[float] = None) -> Optional[dict]:
        """Close by sending the opposing action with reduceOnly.

        `size` arrives in TOKENS from position_manager and has to go out as USD
        notional. reduceOnly is set explicitly rather than relying on Kitsune's
        auto-detection, so a stale position read can never flip us short.
        """
        asset = short_name(symbol)
        px = current_price if (current_price or 0) > 0 else (self.get_price(asset) or 0)
        if px <= 0:
            logger.warning(f"⚠️  {asset}: no price — cannot size the close")
            return None
        notional = abs(size) * px
        if notional <= 0:
            logger.warning(f"⚠️  {asset}: close notional rounded to 0")
            return None

        res = self._post("/signals/trade", {
            "symbol": asset,
            "action": "sell" if is_long else "buy",
            "size": round(notional, 2),
            "reduceOnly": True,
        })
        if not res:
            return None
        logger.info(f"✅ {asset} closed ${notional:.2f} (~{size}) @ ~${px:.6f}")
        return {"filled": True, "avg_price": px, "total_size": size,
                "size": size, "oid": None, "raw": res}

    def place_stop_market(self, symbol: str, is_long: bool, size: float,
                          stop_px: float) -> Optional[str]:
        """No-op by design — the stop was attached to the entry as `sl`.

        Returns a synthetic id so position_manager's "no stop id -> flatten"
        rule stays satisfied. That rule exists to catch a naked position on a
        venue where the stop is a separate order that can fail on its own.
        Here it cannot: either the entry carried its sl, or there is no entry.
        """
        return f"native:{short_name(symbol)}:{'long' if is_long else 'short'}"

    def modify_stop(self, symbol: str, is_long: bool, size: float,
                    old_oid, new_stop_px: float) -> Optional[str]:
        """Local no-op. The native sl stays parked at the hard stop.

        The trail is bot-managed and exits through market_close(). Re-sending a
        trade to tighten sl would be interpreted as a DCA and ADD exposure —
        the opposite of the intent. See the module docstring.
        """
        return str(old_oid) if old_oid is not None else self.place_stop_market(
            symbol, is_long, size, new_stop_px)

    def cancel_order(self, symbol: str, oid) -> bool:
        """Nothing to cancel: no standalone orders exist on this venue. True so
        callers treating False as an error do not log spurious failures."""
        return True

    # ── margin ──────────────────────────────────────────────────
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Leverage is per-trade on Kitsune, sent in the trade payload. Record
        it so market_open uses the value the caller intended."""
        if leverage and leverage > 0:
            self.leverage = float(leverage)
        return True

    def max_leverage(self, asset: str) -> int:
        return 50            # Kitsune accepts 1-50x

    def get_user_fills(self, start_ms: Optional[int] = None) -> list:
        """Not exposed by Kitsune. Empty list, not None — callers iterate it."""
        return []
