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
from utils import iso, utc_date_str, append_jsonl
from config import TRADE_LOG

logger = logging.getLogger("position_manager")
EPS = 1e-9


class PositionManager:
    def __init__(self, client, db):
        self.client = client
        self.db = db
        self.max_concurrent = config.MAX_CONCURRENT

    # ── seeding (paper only) ───────────────────────────────────
    def ensure_seeded(self):
        """In paper mode, seed the account equity once so sizing has a base."""
        if not config.PAPER:
            return
        acct = self.db.account()
        if not acct["equity"] or acct["equity"] <= 0:
            self.db.set_account(
                equity=config.PAPER_START_EQUITY,
                daily_baseline=config.PAPER_START_EQUITY,
                daily_halt=0, last_reset=utc_date_str(),
            )
            logger.info(f"📓 paper equity seeded at ${config.PAPER_START_EQUITY:.2f}")

    # ── equity (source of truth per tick) ──────────────────────
    def equity(self) -> float:
        """LIVE/DRY_RUN: exchange mark-to-market. PAPER: realized cash + unrealized
        of open positions valued at live prices."""
        if config.PAPER:
            return self._paper_equity()
        eq = self.client.get_equity()
        return float(eq) if eq and eq > 0 else 0.0

    def _paper_equity(self) -> float:
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
        notional = config.NOTIONAL_FRAC * eq
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
                self.client.market_close(coin, qty, is_long, current_price=price)
                self.db.log_miss(coin, signal, "stop_failed_flattened"); return None

        # ── persist + dashboard ENTRY event ──
        pos = dict(
            coin=coin.upper(), side=signal, entry=avg, qty=qty, notional=notional,
            margin=margin, peak=avg, hard_stop=stop_px, hard_stop_id=stop_id,
            trail_active=0, trail_stop=None, opened_at=iso(),
        )
        self.db.insert_position(pos)
        append_jsonl(TRADE_LOG, {
            "action": "ENTRY", "asset": coin.upper(), "side": signal,
            "entry": round(avg, 6), "qty": round(qty, 8),
            "label": f"ENTRY {coin.upper()}",
        })
        logger.info(f"    ✓ ENTRY {signal.upper()} {coin} {qty:.6f} @ ${avg:.6f} "
                    f"(notional ${notional:.2f}, margin ${margin:.2f}, stop ${stop_px:.6f})")
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
