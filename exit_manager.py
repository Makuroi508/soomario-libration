"""
Soomario Libration — Exit manager
══════════════════════════════════
Two exits per position, both relative to entry:

  1. Hard stop (10%) — a native resting trigger placed at entry, never moved
     against the position. Always-on safety net; survives disconnects.
  2. Trailing stop (0.55%) — managed here: activates once price is +0.55% in
     profit, then the stop = peak -/+ 0.55% of entry and ratchets with new peaks.
     The single resting trigger is moved (via hl_client.modify_stop, which is
     place-new-then-cancel-old) to the higher of {hard stop, trail} for longs,
     lower for shorts. There is no separate TP — the trail IS the profit exit.

LIVE: this module only MOVES the stop. The exchange fills it; reconcile() books
the close (trusting the exchange as source of truth — the Farms orphan lesson).
PAPER: there is no exchange, so manage() also detects a stop crossing and books
the close itself, idealized at the stop level.
"""
import logging

import config
from utils import iso, append_jsonl
from config import TRADE_LOG, short_name

logger = logging.getLogger("exit_manager")
EPS = 1e-9


class ExitManager:
    def __init__(self, client, db, position_manager=None):
        self.client = client
        self.db = db
        self.pm = position_manager  # for paper realized-equity bookkeeping

    # ── per-position trailing management ───────────────────────
    def manage(self, pos: dict, price: float):
        if price is None or price <= 0:
            return
        coin = pos["coin"]
        is_long = pos["side"] == "long"
        entry = pos["entry"]
        qty = pos["qty"]
        updates = {}

        # Track the favorable extreme: max price for long, min price for short.
        if is_long:
            peak = max(pos["peak"], price)
        else:
            peak = min(pos["peak"], price)
        if peak != pos["peak"]:
            updates["peak"] = peak

        trail_active = bool(pos["trail_active"])
        if config.TRAIL_ENABLED and not trail_active:
            armed = price >= entry * (1 + config.TRAIL_PCT / 100) if is_long \
                else price <= entry * (1 - config.TRAIL_PCT / 100)
            if armed:
                trail_active = True
                updates["trail_active"] = 1
                logger.info(f"    ▲ trail armed {coin} at ${price:.6f} (entry ${entry:.6f})")

        # Once armed, compute the trail level and ratchet the resting stop.
        if trail_active:
            band = entry * (config.TRAIL_PCT / 100)
            if is_long:
                trail = peak - band
                new_stop = max(trail, pos["hard_stop"])
                cur = pos["trail_stop"] if pos["trail_stop"] is not None else pos["hard_stop"]
                if new_stop > cur + EPS:
                    self._move_stop(pos, qty, new_stop, updates)
            else:
                trail = peak + band
                new_stop = min(trail, pos["hard_stop"])
                cur = pos["trail_stop"] if pos["trail_stop"] is not None else pos["hard_stop"]
                if new_stop < cur - EPS:
                    self._move_stop(pos, qty, new_stop, updates)

        if updates:
            # keep the in-memory pos consistent for the paper stop check below
            pos = {**pos, **updates}
            self.db.update_position(coin, **updates)

        # PAPER: no exchange to fill the stop, so detect the crossing here.
        if config.PAPER:
            self._paper_check_stop(pos, price)

    def _move_stop(self, pos, qty, new_stop, updates):
        new_stop = round(new_stop, 8)
        if config.PAPER:
            updates["trail_stop"] = new_stop
            return
        new_oid = self.client.modify_stop(
            pos["coin"], pos["side"] == "long", qty, pos["hard_stop_id"], new_stop)
        updates["trail_stop"] = new_stop
        if new_oid:
            updates["hard_stop_id"] = str(new_oid)
        logger.info(f"    ↗ trail stop {pos['coin']} -> ${new_stop:.6f}")

    def _paper_check_stop(self, pos, price):
        is_long = pos["side"] == "long"
        stop = pos["trail_stop"] if pos["trail_stop"] is not None else pos["hard_stop"]
        hit = (price <= stop) if is_long else (price >= stop)
        if hit:
            reason = "TRAIL" if pos["trail_active"] else "HARD_STOP"
            self.close_position(pos, fill_px=stop, reason=reason)

    # ── booking a close (paper fill, or live reconcile) ────────
    def close_position(self, pos: dict, fill_px: float, reason: str):
        is_long = pos["side"] == "long"
        entry = pos["entry"]
        qty = pos["qty"]
        move = (fill_px - entry) if is_long else (entry - fill_px)
        ret_pct = move / entry * 100 if entry else 0.0
        pnl = move * qty

        # Realized PnL accrues to the flow-neutral baseline in BOTH modes, so the
        # equity curve is driven purely by trade outcomes (not deposits/withdrawals).
        # In live, exit_px is the stop estimate until userFills lands, after which
        # it becomes the actual fill (net of slippage). See reconcile().
        acct = self.db.account()
        self.db.set_account(equity=(acct["equity"] or 0.0) + pnl)

        # net_pct == ret_pct here; live realized friction is measured separately
        # from actual fills (see note in reconcile()).
        self.db.book_trade(dict(
            coin=pos["coin"], side=pos["side"], entry=entry, exit=fill_px, qty=qty,
            ret_pct=round(ret_pct, 4), net_pct=round(ret_pct, 4),
            exit_reason=reason, opened_at=pos.get("opened_at"), closed_at=iso(),
        ))
        self.db.delete_position(pos["coin"])
        logger.info(f"    ◼ CLOSE {reason} {pos['coin']} @ ${fill_px:.6f} "
                    f"({ret_pct:+.2f}%, pnl ${pnl:+.2f})")

    # ── live reconcile — exchange is source of truth ───────────
    def reconcile(self):
        """LIVE: if a position is open in the DB but gone on the exchange, its
        stop filled while we were not looking — book it. Booked at the stop
        level as a best estimate; for exact realized friction the live trial
        should hook the userFills endpoint (offered as a refinement)."""
        if config.PAPER:
            return
        try:
            live = self.client.get_positions() or []
        except Exception as e:
            logger.warning(f"reconcile: could not read exchange positions: {e}")
            return
        live_coins = {short_name(p.get("coin", p.get("symbol", ""))) for p in live}
        for pos in self.db.open_positions():
            if pos["coin"].upper() not in live_coins:
                stop = pos["trail_stop"] if pos["trail_stop"] is not None else pos["hard_stop"]
                reason = "TRAIL" if pos["trail_active"] else "HARD_STOP"
                logger.info(f"reconcile: {pos['coin']} gone on exchange — booking at ~${stop:.6f}")
                self.close_position(pos, fill_px=stop, reason=reason)
