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
        self._gone_streak = {}      # coin -> consecutive 'absent w/o fill' reconcile passes

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

        # Stop enforcement. PAPER: no exchange, so detect the crossing here and
        # book at the stop. LIVE: the resting trigger should fill, but if price
        # has crossed the stop and we're STILL open (trigger missing/failed/
        # gapped), force a reduce-only market close — the safety net.
        if config.PAPER:
            self._paper_check_stop(pos, price)
        else:
            self._live_stop_backstop(pos, price)

    def _live_stop_backstop(self, pos, price):
        """LIVE safety net: never let a position survive past its stop just
        because the resting trigger order isn't on the book. If price has
        crossed the effective stop and the position is still open, force a
        reduce-only market close now and book it. Reduce-only means it can
        never flip into a new position, and if the resting trigger already
        filled it's a harmless no-op."""
        is_long = pos["side"] == "long"
        stop = pos["trail_stop"] if pos["trail_stop"] is not None else pos["hard_stop"]
        if stop is None:
            return
        crossed = (price <= stop) if is_long else (price >= stop)
        if not crossed:
            return
        reason = "TRAIL" if pos["trail_active"] else "HARD_STOP"
        # Log-only: the backstop firing is expected as long as native triggers
        # aren't surviving on the exchange (e.g. a shared vault). The routine
        # close is reported by the normal CLOSE notification below — no need to
        # alarm on every exit. We only escalate to Telegram if the close can't
        # be confirmed (a position may still be open — that IS worth a ping).
        logger.warning(f"🛟 {pos['coin']} ${price:.6f} crossed stop ${stop:.6f} but still open "
                       f"— forcing reduce-only market close (resting trigger missing/failed)")
        res = self.client.market_close(pos["coin"], pos["qty"], is_long, current_price=price)
        if res and res.get("filled"):
            sid = pos.get("hard_stop_id")
            if sid is not None and str(sid).isdigit():
                try:
                    self.client.cancel_order(pos["coin"], int(sid))   # clear any stale trigger
                except (TypeError, ValueError):
                    pass
            fill = res.get("avg_price") or stop
            self.close_position(pos, fill_px=fill, reason=reason, intended_exit=stop)
        else:
            # The close didn't confirm. Usually benign: a native trigger already
            # closed this position and the DB hasn't caught up yet, so HL rejects
            # the reduce-only with "would increase position" on a flat book.
            # reconcile books it. Only alarm if it's genuinely STILL open.
            if self._still_open_on_exchange(pos["coin"]):
                logger.error(f"❌ {pos['coin']} safety market_close did not confirm and the "
                             f"position is STILL OPEN — will retry next tick.")
                tg_notify(f"⚠️ {pos['coin']}: stop crossed at ${price:.4f} but the close did "
                          f"NOT confirm and the position is still OPEN. Check the exchange and "
                          f"set a manual stop.", level="warn")
            else:
                logger.info(f"🛟 {pos['coin']} already closed on the exchange (native trigger "
                            f"filled) — reconcile will book it. No action needed.")

    def _still_open_on_exchange(self, coin):
        """True if the exchange still shows an open position in `coin`. On any
        read failure (None / exception), assume True — safer to warn than to
        wrongly treat a failed read as 'already closed'."""
        try:
            live = self.client.get_positions()
        except Exception:
            return True
        if live is None:
            return True
        live_coins = {short_name(str(p.get("coin", p.get("symbol", "")))).upper() for p in live}
        return coin.upper() in live_coins

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
        """LIVE: book a position as closed ONLY when the exchange confirms it AND
        a real closing fill exists. A position can only leave the exchange via a
        stop fill, market close, or liquidation — all of which produce a
        userFills record. If a position is missing from the exchange but NO fill
        is found, that's an unreliable read (HL was timing out), NOT a close:
        we never fabricate an exit. This is the guard against the incident where
        a flaky API read booked live positions as phantom -10% hard stops."""
        if config.PAPER:
            return
        live = self.client.get_positions()
        if live is None:
            logger.warning("reconcile: exchange read failed — skipping this pass.")
            return
        open_db = self.db.open_positions()
        if not open_db:
            return
        # Sanity gate: an empty exchange while the DB holds positions is almost
        # always a partial/failed read, not a simultaneous mass-close. Skip.
        if not live:
            logger.warning(f"reconcile: exchange shows 0 positions but DB has "
                           f"{len(open_db)} open — unreliable read, skipping.")
            return
        live_coins = {short_name(str(p.get("coin", p.get("symbol", "")))).upper() for p in live}
        for pos in open_db:
            coin = pos["coin"].upper()
            if coin in live_coins:
                self._gone_streak.pop(coin, None)
                continue
            stop = pos["trail_stop"] if pos["trail_stop"] is not None else pos["hard_stop"]
            reason = "TRAIL" if pos["trail_active"] else "HARD_STOP"
            resolved = self._resolve_exit_fill(pos)
            if resolved:
                fill_px, fee = resolved
                logger.info(f"reconcile: {coin} closed on exchange — actual fill ${fill_px:.6f}")
                self.close_position(pos, fill_px=fill_px, reason=reason, fee=fee, intended_exit=stop)
                self._gone_streak.pop(coin, None)
            else:
                # Absent but no confirming fill: do NOT book. Likely a stale read.
                n = self._gone_streak.get(coin, 0) + 1
                self._gone_streak[coin] = n
                logger.warning(f"reconcile: {coin} absent from exchange but NO closing fill found "
                               f"(streak {n}) — NOT booking a phantom exit; rechecking next tick.")
                if n == 5:
                    tg_notify(f"⚠️ {coin}: the bot still holds this in its book but it's absent "
                              f"from the exchange with no fill on record. Please check it manually "
                              f"— the bot will NOT auto-close without a confirmed fill.", level="warn")
