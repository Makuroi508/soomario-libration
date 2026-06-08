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
from datetime import datetime

import config
from utils import iso, append_jsonl, tg_notify
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

    # ── booking a close (paper idealized, or live actual fill) ─
    def close_position(self, pos: dict, fill_px: float, reason: str,
                       fee: float = 0.0, intended_exit: float = None):
        is_long = pos["side"] == "long"
        entry = pos["entry"]            # actual entry fill
        qty = pos["qty"]
        move = (fill_px - entry) if is_long else (entry - fill_px)
        ret_pct = move / entry * 100 if entry else 0.0
        pnl = move * qty
        fee_pct = (fee / (entry * qty) * 100) if (entry and qty) else 0.0
        net_pct = ret_pct - fee_pct
        net_pnl = pnl - (fee or 0.0)

        # Round-trip friction = the PLAN (signal entry -> intended stop, no fees)
        # minus what ACTUALLY happened (real fills, net of fees). Positive = cost.
        # Null in paper (idealized fills) so it never pollutes the measured number.
        intended_entry = pos.get("intended_entry") or entry
        i_exit = intended_exit if intended_exit is not None else fill_px
        imove = (i_exit - intended_entry) if is_long else (intended_entry - i_exit)
        intended_ret = imove / intended_entry * 100 if intended_entry else 0.0
        friction_pct = None if config.PAPER else round(intended_ret - net_pct, 4)

        # Realized PnL (net of fees) accrues to the flow-neutral baseline (both modes).
        acct = self.db.account()
        self.db.set_account(equity=(acct["equity"] or 0.0) + net_pnl)

        self.db.book_trade(dict(
            coin=pos["coin"], side=pos["side"], entry=entry, exit=fill_px, qty=qty,
            ret_pct=round(ret_pct, 4), net_pct=round(net_pct, 4),
            friction_pct=friction_pct, fee=round(fee or 0.0, 6),
            exit_reason=reason, opened_at=pos.get("opened_at"), closed_at=iso(),
        ))
        self.db.delete_position(pos["coin"])
        fr = "" if friction_pct is None else f" friction {friction_pct:+.2f}%"
        logger.info(f"    ◼ CLOSE {reason} {pos['coin']} @ ${fill_px:.6f} "
                    f"(net {net_pct:+.2f}%, pnl ${net_pnl:+.2f}{fr})")
        tg_notify(f"CLOSE *{reason}* {pos['coin']} @ ${fill_px:.4f}\n"
                  f"net {net_pct:+.2f}% · pnl ${net_pnl:+.2f}{fr}",
                  level="trail" if reason == "TRAIL" else "trade")

    def _resolve_exit_fill(self, pos):
        """Return (avg_fill_px, total_fee) from the position's actual closing
        fills, or None if none are found yet (caller falls back to the estimate)."""
        try:
            open_ms = int(datetime.fromisoformat(
                str(pos["opened_at"]).replace("Z", "+00:00")).timestamp() * 1000) - 1000
        except (ValueError, TypeError, KeyError):
            open_ms = None
        fills = self.client.get_user_fills(start_ms=open_ms)
        coin = pos["coin"].upper()
        closes = [f for f in fills
                  if short_name(str(f.get("coin", ""))).upper() == coin
                  and str(f.get("dir", "")).startswith("Close")]
        tot_sz = sum(abs(float(f.get("sz", 0) or 0)) for f in closes)
        if tot_sz <= 0:
            return None
        avg_px = sum(float(f["px"]) * abs(float(f["sz"])) for f in closes) / tot_sz
        fee = sum(float(f.get("fee", 0) or 0) for f in closes)
        return avg_px, fee

    # ── live reconcile — exchange is source of truth ───────────
    def reconcile(self):
        """LIVE: if a position is open in the DB but gone on the exchange, its
        stop filled out-of-band — book it at the ACTUAL fill from userFills so
        realized friction (intended stop vs real fill) is measured, not guessed.
        Falls back to the stop-level estimate if fills aren't indexed yet."""
        if config.PAPER:
            return
        try:
            live = self.client.get_positions() or []
        except Exception as e:
            logger.warning(f"reconcile: could not read exchange positions: {e}")
            return
        live_coins = {short_name(p.get("coin", p.get("symbol", ""))) for p in live}
        for pos in self.db.open_positions():
            if pos["coin"].upper() in live_coins:
                continue
            stop = pos["trail_stop"] if pos["trail_stop"] is not None else pos["hard_stop"]
            reason = "TRAIL" if pos["trail_active"] else "HARD_STOP"
            resolved = self._resolve_exit_fill(pos)
            if resolved:
                fill_px, fee = resolved
                logger.info(f"reconcile: {pos['coin']} closed on exchange — actual fill ${fill_px:.6f}")
                self.close_position(pos, fill_px=fill_px, reason=reason, fee=fee, intended_exit=stop)
            else:
                logger.info(f"reconcile: {pos['coin']} gone, fills not indexed yet — estimate ${stop:.6f}")
                self.close_position(pos, fill_px=stop, reason=reason, intended_exit=stop)
