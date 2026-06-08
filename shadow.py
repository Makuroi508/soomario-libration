"""
Soomario Libration — Shadow trail A/B
══════════════════════════════════════
The live bot trades TRAIL_PCT (0.55%). In parallel, for each value in
SHADOW_TRAILS (e.g. 0.3, 0.4), this tracks what that tighter trail WOULD have
done on the SAME real entry and the SAME observed prices — without risking a
cent. Backtest says 0.3% beats 0.55%, but 0.3% sits at the noise floor where
real fills diverge most, so live data settles it (spec §7b).

A shadow shares the real position's entry AND its 10% hard stop, differing only
in the trail rule. When a shadow trail (or the hard stop) would trigger, we log
the counterfactual exit and charge it the bot's MEASURED real round-trip
friction so the comparison is fair — a hypothetical exit is otherwise
slippage-free and looks artificially good.

State lives in SQLite (shadow_state) so it survives redeploys. Tighter trails
almost always close BEFORE the real 0.55% position; reap() cleans up the rare
leftover when a real position closes first (e.g. a gap straight to the hard
stop). This compares EXIT RULES on identical entries — the right first-order
question — not a full divergent-path re-backtest.
"""
import logging

import config
from utils import iso

logger = logging.getLogger("shadow")
EPS = 1e-9


class ShadowTracker:
    def __init__(self, db, trails=None, friction_fn=None):
        self.db = db
        self.trails = trails if trails is not None else config.SHADOW_TRAILS
        # measured real round-trip friction (%). Placeholder until live fills
        # populate it; override by passing a callable that reads the KPI.
        self.friction_fn = friction_fn or (lambda: config.MEASURED_FRICTION_PCT)

    def on_open(self, pos: dict):
        """Seed one shadow per trail value off the real entry + hard stop."""
        for tp in self.trails:
            self.db.open_shadow(pos["coin"], tp, pos["side"], pos["entry"],
                                pos["qty"], pos["hard_stop"], pos.get("opened_at") or iso())

    def on_price(self, coin: str, price: float):
        """Advance every open shadow for `coin` and book any that would trigger."""
        if price is None or price <= 0:
            return
        for s in self.db.get_open_shadows(coin):
            self._step(s, price)

    def reap(self, open_coins: set, marks: dict):
        """Close orphan shadows whose real position has already closed, valued at
        the current mark, so no in-flight state leaks across the trial."""
        open_coins = {c.upper() for c in open_coins}
        for s in self.db.get_open_shadows():
            if s["coin"] not in open_coins:
                px = marks.get(s["coin"]) or s["entry"]
                self._book(s, px, "ORPHAN")

    # ── internals ──────────────────────────────────────────────
    def _step(self, s, price):
        is_long = s["side"] == "long"
        peak = max(s["peak"], price) if is_long else min(s["peak"], price)
        active = bool(s["active"])
        band = s["entry"] * (s["trail_pct"] / 100)

        if not active:
            armed = price >= s["entry"] * (1 + s["trail_pct"] / 100) if is_long \
                else price <= s["entry"] * (1 - s["trail_pct"] / 100)
            if armed:
                active = True

        # effective stop = hard stop until the trail arms, then the tighter of the two
        if is_long:
            eff = max(peak - band, s["hard_stop"]) if active else s["hard_stop"]
            hit = price <= eff
        else:
            eff = min(peak + band, s["hard_stop"]) if active else s["hard_stop"]
            hit = price >= eff

        if hit:
            reason = "TRAIL" if active and (
                (is_long and eff > s["hard_stop"] + EPS) or
                (not is_long and eff < s["hard_stop"] - EPS)) else "HARD_STOP"
            self._book(s, eff, reason)
        elif peak != s["peak"] or active != bool(s["active"]):
            self.db.update_shadow(s["id"], peak=peak, active=active)

    def _book(self, s, exit_px, reason):
        is_long = s["side"] == "long"
        move = (exit_px - s["entry"]) if is_long else (s["entry"] - exit_px)
        ret_pct = move / s["entry"] * 100 if s["entry"] else 0.0
        net_pct = ret_pct - self.friction_fn()   # fair-comparison: charge measured friction
        self.db.close_shadow(s["id"], round(exit_px, 8), ret_pct, net_pct, reason, s["opened_at"])
        logger.info(f"    ◌ shadow {s['coin']} @{s['trail_pct']}% {reason} "
                    f"ret {ret_pct:+.2f}% net {net_pct:+.2f}% (friction {self.friction_fn():.2f}%)")
