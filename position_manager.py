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
        hs = config.HARD_STOP_PCT / 100.0
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
            order = self.client.market_open(coin, is_long, notional, current_price=price)
            if not order or not order.get("filled"):
                reason = getattr(self.client, "last_open_error", None) or "not_filled"
                self.db.log_miss(coin, signal, f"entry_failed:{reason}"); return None
            avg = float(order["avg_price"])
            qty = float(order["total_size"])

        # ── hard stop, placed immediately (10%) ──
        stop_px = avg * (1 - config.HARD_STOP_PCT / 100) if is_long \
            else avg * (1 + config.HARD_STOP_PCT / 100)
        if not config.PAPER:
            stop_id = self.client.place_stop_market(coin, is_long, qty, stop_px)
            if stop_id is None:
                # Filled but unprotected — flatten immediately rather than ride naked.
                logger.error(f"❌ {coin}: stop placement failed after fill — flattening")
                tg_notify(f"⚠️ {coin}: STOP FAILED to place after entry fill — "
                          f"flattening the position immediately (not riding unprotected).",
                          level="warn")
                self.client.market_close(coin, qty, is_long, current_price=price)
                self.db.log_miss(coin, signal, "stop_failed_flattened"); return None

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
        self.db.set_account(daily_baseline=self.equity(), daily_halt=0,
                            last_reset=utc_date_str())
        logger.info(f"🔄 daily baseline reset to ${self.equity():.2f}")

    def check_daily_dd(self):
        acct = self.db.account()
        base = acct["daily_baseline"] or 0.0
        if base <= 0:
            return
        dd = (base - self.equity()) / base * 100
        if dd >= config.DAILY_DD_PCT and not acct["daily_halt"]:
            self.db.set_account(daily_halt=1)
            logger.warning(f"🛑 DAILY DD HALT: down {dd:.2f}% >= {config.DAILY_DD_PCT}% "
                           f"— no new entries until UTC rollover (open positions ride their stops)")
            tg_notify(f"*DAILY DD HALT* — down {dd:.2f}% on the day.\n"
                      f"No new entries until UTC rollover; open positions keep their stops.",
                      level="warn")
