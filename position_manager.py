"""
Soomario Libration — Shared-account position manager
═════════════════════════════════════════════════════
The core that does not exist in the single-coin bot: one account, many coins,
central control of sizing, concurrency, leverage and the daily-drawdown halt.

Sizing: each position's notional = NOTIONAL_FRAC x live equity (mark-to-market),
recomputed at entry so it compounds. Margin = notional / LEVERAGE. Entries are
gated on BOTH capacity (count < MAX_CONCURRENT) and free margin
(equity - locked). At 2x/20% that is 10 concurrent, 10% margin each, 200%
notional fully deployed — the exposure ceiling is structural.

Pyramiding 1: one position per coin. Every missed signal is logged with a
reason — fill rate is a KPI (target ~85% at 2x).
"""
import logging
import time

import config
from utils import iso, utc_date_str, append_jsonl, tg_notify
from config import TRADE_LOG, short_name

logger = logging.getLogger("position_manager")
EPS = 1e-9


class PositionManager:
    def __init__(self, client, db):
        self.client = client
        self.db = db
        self.max_concurrent = config.MAX_CONCURRENT

    # ── baseline + perf-equity (flow-neutral) ──────────────────
    def ensure_seeded(self):
        """Public hook called at startup. Captures the starting baseline once."""
        self._ensure_baseline()

    def adopt_unmanaged(self):
        """Self-heal: re-adopt any OPEN exchange position the DB doesn't know
        about. The exchange is ground truth. This recovers positions that a bad
        reconcile/divergence dropped from the book, so the bot resumes managing
        (trailing + protecting) them instead of abandoning them — and would
        re-open them on top, doubling exposure. Runs once at startup (LIVE only).
        Skipped entirely on an unreliable read."""
        if config.PAPER:
            return
        try:
            live = self.client.get_positions()
        except Exception as e:
            logger.warning(f"adopt: could not read exchange positions: {e}")
            return
        if not live:   # None (failed read) or [] (genuinely flat) → nothing to adopt
            return
        known = {p["coin"].upper() for p in self.db.open_positions()}
        adopted = 0
        for p in live:
            coin = short_name(str(p.get("coin", ""))).upper()
            if not coin or coin in known:
                continue
            try:
                szi = float(p.get("szi", 0) or 0)
                entry = float(p.get("entryPx") or 0)
            except (TypeError, ValueError):
                continue
            qty = abs(szi)
            if szi == 0 or entry <= 0 or qty <= 0:
                continue
            side = "long" if szi > 0 else "short"
            hs = config.hard_stop_pct(coin) / 100.0
            hard = entry * (1 - hs) if side == "long" else entry * (1 + hs)
            notional = entry * qty
            self.db.insert_position(dict(
                coin=coin, side=side, entry=entry, qty=qty, notional=notional,
                margin=notional / config.LEVERAGE, peak=entry, hard_stop=hard,
                hard_stop_id=None, trail_active=0, trail_stop=None,
                intended_entry=entry, opened_at=iso()))
            # (re)place a native protective stop so it isn't left naked.
            try:
                sid = self.client.place_stop_market(coin, side == "long", qty, hard)
                if sid:
                    self.db.update_position(coin, hard_stop_id=str(sid))
            except Exception as e:
                logger.warning(f"adopt {coin}: stop placement failed: {e}")
            adopted += 1
            logger.warning(f"🩺 adopted unmanaged exchange position {coin} {side} "
                           f"{qty}@${entry:.6f} (hard stop ${hard:.6f})")
            tg_notify(f"🩺 Re-adopted {coin} {side} {qty}@${entry:.6f} from the exchange "
                      f"(it was missing from the bot's book).", level="info")
        if adopted:
            logger.warning(f"🩺 adopt: re-adopted {adopted} unmanaged position(s) from the exchange.")

    def _ensure_baseline(self):
        """Seed account.equity with starting capital ONCE. After this, account.equity
        moves only by realized PnL (booked on close) — never re-read from the
        exchange — so deposits/withdrawals can't move the curve. PAPER seeds
        PAPER_START_EQUITY; LIVE captures the wallet value at go-live."""
        acct = self.db.account()
        if acct["equity"] and acct["equity"] > 0:
            # Existing account: backfill inception ONCE if it's missing. Older
            # rows predate the inception column, so it reads NULL and the
            # dashboard used to fall back to daily_baseline — which resets every
            # UTC day and silently zeroed out "since inception" returns. The true
            # baseline is current realized-equity minus PnL booked in the ledger.
            if not acct.get("inception"):
                realized = self.db.realized_pnl()
                inception = (acct["equity"] or 0.0) - realized
                self.db.set_account(inception=inception,
                                    inception_ts=acct.get("inception_ts") or iso())
                logger.info(f"📓 inception backfilled: ${inception:.2f} "
                            f"(equity ${acct['equity']:.2f} − realized ${realized:.2f} "
                            f"from {self.db.trade_count()} trades)")
            # Pin the equity accumulator to the ledger. account.equity is meant to
            # be inception + cumulative realized; if the ledger is edited out-of-band
            # (e.g. phantom trades removed during recovery) the accumulator can be
            # left stale/drifted. Recomputing it from inception + realized_pnl()
            # makes it self-heal so the live curve point matches the rebuilt history.
            inception = self.db.account().get("inception") or 0.0
            synced = round(inception + self.db.realized_pnl(), 4)
            if abs(synced - (acct["equity"] or 0.0)) > 0.01:
                self.db.set_account(equity=synced)
                logger.info(f"📓 equity resynced to ledger: ${synced:.2f} "
                            f"(was ${acct['equity']:.2f}; drift corrected)")
            return
        base = config.PAPER_START_EQUITY if config.PAPER else (self.client.get_equity() or 0.0)
        if base > 0:
            self.db.set_account(equity=base, daily_baseline=base, daily_halt=0,
                                last_reset=utc_date_str(), inception=base, inception_ts=iso())
            tag = "paper" if config.PAPER else "live starting capital"
            logger.info(f"📓 baseline captured: ${base:.2f} ({tag})")

    def equity(self) -> float:
        """Flow-neutral performance equity = baseline + cumulative realized PnL
        (account.equity) + open unrealized PnL at live marks. This is what the
        curve plots and what sizing/DD use; it ignores deposits/withdrawals by
        construction. During the own-account trial it equals wallet value (no
        flows); it stays correct once Libration is wrapped as a vault."""
        self._ensure_baseline()
        realized = self.db.account()["equity"] or 0.0
        upnl = 0.0
        for p in self.db.open_positions():
            px = self.client.get_price(p["coin"]) or p["entry"]
            move = (px - p["entry"]) if p["side"] == "long" else (p["entry"] - px)
            upnl += move * p["qty"]
        return realized + upnl

    # ── capacity ───────────────────────────────────────────────
    def free_margin(self) -> float:
        locked = sum(p["margin"] for p in self.db.open_positions())
        return self.equity() - locked

    def has_capacity(self) -> bool:
        return len(self.db.open_positions()) < self.max_concurrent

    # ── entries ────────────────────────────────────────────────
    def maybe_enter(self, coin: str, signal: str, price: float):
        """Attempt an entry for `coin` given a fresh 'long'/'short' signal at the
        bar-close `price`. Returns the new position dict, or None (with a logged
        miss reason) if any gate blocks it."""
        if signal is None:
            return None
        if not config.ENTRIES_ENABLED:
            self.db.log_miss(coin, signal, "entries_disabled"); return None
        if self.db.max_dd_halt():
            self.db.log_miss(coin, signal, "max_dd_halt"); return None
        if self.db.daily_halt():
            self.db.log_miss(coin, signal, "daily_dd_halt"); return None
        if self.db.has_open_position(coin):
            return None  # pyramiding 1 — already in this coin
        if not self.has_capacity():
            self.db.log_miss(coin, signal, "concurrency_full"); return None

        eq = self.equity()
        if eq <= 0:
            self.db.log_miss(coin, signal, "no_equity"); return None
        mult = config.size_mult(coin)
        notional = config.NOTIONAL_FRAC * eq * mult
        margin = notional / config.LEVERAGE
        if margin > self.free_margin() + EPS:
            self.db.log_miss(coin, signal, "margin_full"); return None
        if price is None or price <= 0:
            price = self.client.get_price(coin)
            if not price or price <= 0:
                self.db.log_miss(coin, signal, "no_price"); return None

        is_long = signal == "long"

        # ── fill ──
        if config.PAPER:
            avg = self._paper_fill_price(price, is_long)
            qty = notional / avg
            stop_id = "paper"
        else:
            self.client.set_leverage(coin, int(config.LEVERAGE))  # isolated (is_cross_for=False)
            # Preferred path: submit the entry and its stop as ONE order group,
            # so there is no window in which a filled position has no stop. The
            # trigger is computed from the pre-trade price rather than the fill
            # — observed slippage is under 0.06%, far inside the 10% stop, and
            # a stop that exists is worth more than one that is exact.
            order = None
            if hasattr(self.client, "market_open_with_stop"):
                _hs = config.hard_stop_pct(coin)
                pre_stop = price * (1 - _hs / 100) if is_long \
                    else price * (1 + _hs / 100)
                order = self.client.market_open_with_stop(
                    coin, is_long, notional, pre_stop, current_price=price)
            if order is None:
                order = self.client.market_open(coin, is_long, notional, current_price=price)
            if not order or not order.get("filled"):
                reason = getattr(self.client, "last_open_error", None) or "not_filled"
                self.db.log_miss(coin, signal, f"entry_failed:{reason}"); return None
            avg = float(order["avg_price"])
            qty = float(order["total_size"])

        # ── hard stop, placed immediately (per-coin, default 10%) ──
        _hs = config.hard_stop_pct(coin)
        stop_px = avg * (1 - _hs / 100) if is_long \
            else avg * (1 + _hs / 100)
        if not config.PAPER and (order or {}).get("stop_oid"):
            # The group already attached it — no second call, no race.
            stop_id = order["stop_oid"]
            stop_px = float(order.get("stop_px") or stop_px)
        elif not config.PAPER:
            stop_id = self.client.place_stop_market(coin, is_long, qty, stop_px)
            if stop_id is None:
                # Filled but unprotected — flatten immediately rather than ride naked.
                logger.error(f"❌ {coin}: stop placement failed after fill — flattening")
                tg_notify(f"⚠️ {coin}: STOP FAILED to place after entry fill — "
                          f"flattening the position immediately (not riding unprotected).",
                          level="warn")
                closed = self.client.market_close(coin, qty, is_long, current_price=price)
                if closed and closed.get("filled"):
                    self.db.log_miss(coin, signal, "stop_failed_flattened")
                    return None
                # The flatten failed too, so the position is LIVE on the exchange.
                # Dropping it here would make it invisible to exit_manager and to
                # reconcile — an unmanaged, unstopped position nobody is watching.
                # Persist it instead (stop_id stays None); manage() retries the stop
                # each tick and the backstop can still close it.
                logger.error(f"🚨 {coin}: flatten ALSO failed — position is LIVE and "
                             f"UNPROTECTED. Recording it so it stays managed.")
                tg_notify(f"🚨 *{coin}: OPEN WITHOUT A STOP* — entry filled, stop rejected, "
                          f"and the emergency close also failed.\n"
                          f"It is now tracked and the bot will retry the stop every tick, "
                          f"but CHECK THE EXCHANGE.", level="warn")

        # ── persist + dashboard ENTRY event ──
        pos = dict(
            coin=coin.upper(), side=signal, entry=avg, qty=qty, notional=notional,
            margin=margin, peak=avg, hard_stop=stop_px, hard_stop_id=stop_id,
            trail_active=0, trail_stop=None, intended_entry=price, opened_at=iso(),
        )
        self.db.insert_position(pos)
        append_jsonl(TRADE_LOG, {
            "action": "ENTRY", "asset": coin.upper(), "side": signal,
            "entry": round(avg, 6), "qty": round(qty, 8),
            "label": f"ENTRY {coin.upper()}",
        })
        logger.info(f"    ✓ ENTRY {signal.upper()} {coin} {qty:.6f} @ ${avg:.6f} "
                    f"(notional ${notional:.2f}{' ½× WATCH' if mult < 1 else ''}, "
                    f"margin ${margin:.2f}, stop ${stop_px:.6f})")
        tg_notify(f"ENTRY *{signal.upper()}* {coin}{' (WATCH '+format(mult,'g')+'×)' if mult<1 else ''} @ ${avg:.4f}\n"
                  f"notional ${notional:.0f} · stop ${stop_px:.4f} · "
                  f"{len(self.db.open_positions())}/{self.max_concurrent} open", level="trade")
        return pos

    def _paper_fill_price(self, price: float, is_long: bool) -> float:
        slip = config.PAPER_SLIPPAGE_PCT / 100
        return price * (1 + slip) if is_long else price * (1 - slip)

    # ── daily drawdown halt ────────────────────────────────────
    def maybe_reset_daily(self):
        """Reset the daily baseline at the UTC date rollover."""
        if self.db.account()["last_reset"] != utc_date_str():
            self.reset_daily_baseline()

    def reset_daily_baseline(self):
        # STRICT, for the same reason check_daily_dd is: this baseline is what
        # the guard measures against all day. Recording it from performance
        # equity while the guard reads venue equity leaves the two on different
        # bases, and the gap between them reads as drawdown that never happened.
        base = self._risk_equity(strict=True)
        if base is None or base <= 0:
            # Deliberately do NOT stamp last_reset - maybe_reset_daily() will
            # retry on the next tick. Yesterday's halt stays in force until we
            # can set an honest baseline, which errs toward blocking entries
            # rather than measuring against a wrong number.
            logger.warning("daily reset deferred - venue equity unavailable; "
                           "baseline unchanged, retrying next tick")
            return
        self.db.set_account(daily_baseline=base, daily_halt=0,
                            last_reset=utc_date_str())
        logger.info(f"🔄 daily baseline reset to ${base:.2f}")

    # ── venue rules (authoritative over env) ───────────────────
    _RULES_TTL = 300

    def venue_rules(self) -> dict:
        """Limits as the VENUE states them. Cached briefly and refreshed, so a
        phase transition (Propr moves the target 5% -> 10%) is picked up without
        a redeploy, and an env typo can't loosen a guard below the real limit."""
        now = time.time()
        if getattr(self, "_rules_cache", None) and now - getattr(self, "_rules_ts", 0) < self._RULES_TTL:
            return self._rules_cache
        r = {}
        if hasattr(self.client, "challenge_rules"):
            try:
                r = self.client.challenge_rules() or {}
            except Exception as e:
                logger.warning(f"venue rules unavailable ({e}) — falling back to env")
        self._rules_cache, self._rules_ts = r, now
        return r

    def daily_limit_pct(self) -> float:
        """Where the guard fires on the day. Never looser than the venue allows;
        an explicitly tighter DAILY_DD_PCT still wins."""
        venue = self.venue_rules().get("daily_loss_pct")
        if venue:
            return min(config.DAILY_DD_PCT, max(venue - config.DD_GUARD_MARGIN, 0.25))
        return config.DAILY_DD_PCT

    def dd_limit(self) -> tuple:
        """(max_drawdown_pct, anchor_type) — venue first, env as fallback."""
        r = self.venue_rules()
        pct = r.get("max_dd_pct") or config.MAX_DD_PCT
        typ = (r.get("dd_type") or config.DD_TYPE or "static").lower()
        return pct, typ

    def _risk_equity(self, strict: bool = False):
        """The equity the daily guard measures against.

        On a prop venue this MUST be the venue's own ledger. If our flow-neutral
        number says we're down 1.4% while Propr's accounting says 3.1%, Propr's
        is the one that ends the challenge — so the guard and the breach engine
        have to read from the same source.

        `strict=True` returns None instead of falling back when the venue read
        fails. Risk decisions MUST use strict: daily_baseline is recorded from
        the VENUE, so substituting local performance equity compares two
        different measurement bases and manufactures a drawdown equal to
        whatever the local ledger is off by. On 31 Jul a Propr outage (500s on
        /challenge-attempts, 403s on /positions) did exactly that: the fallback
        read -4.35% against a 3.00% limit and liquidated six positions that
        were really about -1.2% down on the day. Non-strict callers (status
        display, logging) can still take the approximation.
        """
        if config.EXCHANGE in ("propr", "foxify") and not config.PAPER:
            try:
                eq = self.client.get_equity() or 0.0
            except Exception as e:
                logger.warning(f"guard: venue equity read failed ({e})")
                eq = 0.0
            if eq > 0:
                return eq
            if strict:
                return None
            logger.warning("guard: venue equity unavailable — falling back to perf equity")
        return self.equity()

    def flatten_all(self, exit_manager=None, reason: str = "DAILY_GUARD") -> int:
        """Close every open position now, clear its resting stop, and book it.

        Idempotent and self-retrying: anything that fails to close stays in the
        book and is retried on the next tick, so a transient API error can never
        leave a position silently unmanaged behind a halt flag."""
        em = exit_manager or getattr(self, "exit_manager", None)
        open_pos = self.db.open_positions()
        if not open_pos:
            return 0
        closed, failed = 0, []
        for p in open_pos:
            coin, is_long = p["coin"], p["side"] == "long"
            px = self.client.get_price(coin) or p["entry"]
            if config.PAPER:
                fill = self._paper_fill_price(px, not is_long)
            else:
                res = self.client.market_close(coin, p["qty"], is_long, current_price=px)
                if not (res and res.get("filled")):
                    failed.append(coin)
                    continue
                fill = float(res.get("avg_price") or px)
                # Close FIRST, then cancel the trigger — never leave the position
                # naked in the window between the two calls.
                sid = p.get("hard_stop_id")
                if sid and str(sid) != "paper":
                    try:
                        self.client.cancel_order(
                            coin, int(sid) if str(sid).isdigit() else sid)
                    except (TypeError, ValueError):
                        pass
            if em is not None:
                em.close_position(p, fill_px=fill, reason=reason, intended_exit=px)
            else:
                self.db.delete_position(coin)
            closed += 1
            logger.warning(f"🧯 {reason}: flattened {coin} {p['side']} "
                           f"{p['qty']:.6f} @ ${fill:.6f}")
        if failed:
            logger.error(f"❌ {reason}: could not close {', '.join(failed)} "
                         f"— will retry next tick")
            tg_notify(f"⚠️ Guard flatten could NOT close: {', '.join(failed)}.\n"
                      f"Retrying every tick — check the exchange manually.", level="warn")
        return closed

    def check_max_dd(self, exit_manager=None):
        """The limit that actually ends a challenge. The daily guard resets at
        UTC midnight; this one does not — breach it and the account is gone
        permanently, so it flattens and stays flat until a human intervenes.

        TRAILING anchors to the VENUE's high-water mark, read from the venue.
        Reconstructing an HWM locally is unsafe: if their mark caught a spike
        our 120s poll missed, our floor sits below theirs and we flatten only
        after they've already recorded the breach."""
        # An existing halt was decided on GOOD data. Keep the book empty
        # regardless of whether the venue is readable right now.
        if self.db.max_dd_halt():
            if self.db.open_positions():
                self.flatten_all(exit_manager, reason="MAX_DD_GUARD")
            return
        max_dd, dd_type = self.dd_limit()
        if max_dd <= 0:
            return
        # STRICT. Falling back to performance equity here measures the local
        # ledger against a venue-derived anchor and manufactures a drawdown
        # equal to whatever the two disagree by. On 2026-08-20T09:48 a Propr
        # outage did exactly that: perf equity $9091.40 against a reconstructed
        # $10000.00 anchor read as 9.09% and flattened the book, while the
        # venue's own numbers were $9491.58 against a $10010.89 high-water
        # mark - 5.19%, comfortably inside the 8% limit.
        eq = self._risk_equity(strict=True)
        if eq is None or eq <= 0:
            logger.error("max-dd: venue equity unavailable - SKIPPING this tick rather "
                         "than measuring the local ledger against a venue anchor. "
                         "No halt, no flatten; open positions keep their stops.")
            return
        if dd_type == "trailing":
            # Two different situations, and conflating them either disables the
            # guard or fires it on a made-up anchor:
            #   * the venue KEEPS a high-water mark (prop venues) - theirs is
            #     authoritative and a failed read means we cannot evaluate, so
            #     skip. Reconstructing max(inception, eq) here is what paired
            #     with a fallback equity to flatten the book on 08-20.
            #   * the venue has NO such concept (Hyperliquid, Pacifica, bulk) -
            #     a locally tracked mark is the only anchor available and there
            #     is no competing authority to disagree with it. Falling back is
            #     correct; skipping would silently switch the guard off.
            venue_keeps_hwm = hasattr(self.client, "high_water_mark")
            hwm = self.client.high_water_mark() if venue_keeps_hwm else None
            if not hwm or hwm <= 0:
                if venue_keeps_hwm:
                    logger.error("max-dd: venue HWM unavailable - SKIPPING this tick. "
                                 "This venue keeps its own mark, so a failed read means "
                                 "we cannot measure; it does not mean we may guess.")
                    return
                hwm = max(self.db.account().get("inception") or 0.0, eq)
            anchor = hwm
        else:
            anchor = self.db.account().get("inception") or 0.0
        if anchor <= 0:
            return
        # Flatten DD_GUARD_MARGIN points above the real floor.
        effective = max(max_dd - config.DD_GUARD_MARGIN, 0.25)
        floor = anchor * (1 - effective / 100)
        if eq > floor:
            return
        dd = (anchor - eq) / anchor * 100
        # Own flag, NOT daily_halt: reset_daily_baseline() clears daily_halt at
        # every UTC rollover, which re-armed entries under a breached max drawdown.
        self.db.set_account(max_dd_halt=1, daily_halt=1)
        logger.error(f"🚨 MAX DD GUARD: equity ${eq:.2f} <= floor ${floor:.2f} "
                     f"({dd:.2f}% below {dd_type} anchor ${anchor:.2f}; "
                     f"venue limit {max_dd}%) — flattening and pausing")
        tg_notify(f"🚨 *MAX DRAWDOWN GUARD* — {dd:.2f}% below the {dd_type} "
                  f"anchor (venue limit {max_dd}%).\n"
                  f"Flattening everything and blocking entries. This does NOT reset "
                  f"at midnight; clear it with CLEAR_MAX_DD_HALT=1 once you have "
                  f"reviewed.", level="warn")
        if self.db.open_positions():
            self.flatten_all(exit_manager, reason="MAX_DD_GUARD")

    def check_daily_dd(self, exit_manager=None):
        """Halt new entries at DAILY_DD_PCT, and when DAILY_FLATTEN is on, close
        the open book as well. Halting alone only stops digging; the positions
        already open are what carry you into a breach."""
        acct = self.db.account()
        base = acct["daily_baseline"] or 0.0
        if base <= 0:
            return
        halted = bool(acct["daily_halt"])
        limit = self.daily_limit_pct()
        eq = self._risk_equity(strict=True)
        if eq is None:
            # Cannot measure risk against the same basis as the baseline.
            # Halting here is irreversible — it liquidates the book — while
            # waiting one tick is not. Never flatten on data we do not have.
            # Positions keep their resting stops meanwhile.
            if not halted:
                logger.error("guard: venue equity unavailable — SKIPPING the daily-DD "
                             "evaluation this tick rather than measuring against a "
                             "different basis. No halt, no flatten. Stops still apply.")
                return
            # Already halted on good data: fall through so a failed flatten
            # still gets retried below.
        else:
            dd = (base - eq) / base * 100
        if eq is not None and not halted and dd >= limit:
            self.db.set_account(daily_halt=1)
            halted = True
            n = len(self.db.open_positions())
            tail = (f"; flattening {n} open position(s)" if config.DAILY_FLATTEN
                    else "; open positions ride their stops")
            logger.warning(f"🛑 DAILY DD HALT: down {dd:.2f}% >= {limit:.2f}% "
                           f"— no new entries until UTC rollover{tail}")
            tg_notify(f"*DAILY DD HALT* — down {dd:.2f}% on the day.\n"
                      + (f"Flattening {n} open position(s), then paused until UTC rollover."
                         if config.DAILY_FLATTEN else
                         "No new entries until UTC rollover; open positions keep their stops."),
                      level="warn")
        # Runs every tick while halted, so a failed close is retried until the
        # book is genuinely empty.
        if halted and config.DAILY_FLATTEN and self.db.open_positions():
            self.flatten_all(exit_manager, reason="DAILY_GUARD")
