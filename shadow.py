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

ARMING-DELAY SHADOWS (SHADOW_ARM_DELAYS)
The other knob a shadow can carry is WHEN the trail is allowed to arm. The live
bot waits one 4h bar (TRAIL_ARM_DELAY_BARS, Pine parity); before 2026-08-16 it
armed immediately. Backtested on real 1m paths over 24 coins the delay is worth
about +0.01%/trade -- small, positive, and far inside the noise of any single
month, which is exactly the kind of question a shadow answers and a backtest
cannot. "0" runs the coin's live width arming at once; "0.55:0" reproduces the
whole pre-2026-08-16 config.

State lives in SQLite (shadow_state) so it survives redeploys. Tighter trails
almost always close BEFORE the real 0.55% position; reap() cleans up the rare
leftover when a real position closes first (e.g. a gap straight to the hard
stop). This compares EXIT RULES on identical entries — the right first-order
question — not a full divergent-path re-backtest.

STALE-EXIT SHADOWS (SHADOW_STALE_HOURS)
A shadow may also carry a whole exit RULE rather than just a narrower trail:
close at market after N hours if the trail never armed. That rule exists because
losing trades are a distinct population — measured over 2,018 trades at 1-minute
resolution across crypto and equity perps, NOT ONE hard stop had ever reached
+0.55%, and they sat underwater for a median 66-71 hours against 2-4 hours for a
winner. They do not go wrong; they go nowhere, slowly.

The catch, and the reason this is shadowed rather than shipped: 75-80% of the
trades still unarmed at 24 hours went on to WIN. Cutting them kills roughly three
winners per hard stop avoided, and in backtest every stale variant lost to the
plain trail on gross return, before friction. Candle backtests of a 0.55% trail
are unreliable by construction, though, which is exactly what a shadow settles —
so the rule runs here, against real fills, at no risk.
"""
import logging
from datetime import datetime, timezone

import config
from utils import iso

logger = logging.getLogger("shadow")
EPS = 1e-9


class ShadowTracker:
    def __init__(self, db, trails=None, friction_fn=None, stale_hours=None,
                 arm_delays=None):
        self.db = db
        self.trails = trails if trails is not None else config.SHADOW_TRAILS
        # Exit-RULE shadows: the live trail width plus a stale exit at N hours.
        self.stale_hours = (stale_hours if stale_hours is not None
                            else getattr(config, "SHADOW_STALE_HOURS", []))
        # Exit-RULE shadows on the ARMING DELAY: (trail_pct or None, delay_hours).
        self.arm_delays = (arm_delays if arm_delays is not None
                           else getattr(config, "SHADOW_ARM_DELAYS", []))
        # measured real round-trip friction (%). Placeholder until live fills
        # populate it; override by passing a callable that reads the KPI.
        self.friction_fn = friction_fn or (lambda: config.MEASURED_FRICTION_PCT)

    def on_open(self, pos: dict):
        """Seed one shadow per trail value, plus one per exit RULE."""
        coin = pos["coin"]
        opened = pos.get("opened_at") or iso()
        live_delay = config.arm_delay_sec(coin) / 3600.0
        live_trail = config.trail_pct(coin)
        for tp in self.trails:
            # Width shadows must carry the LIVE arming delay. Before this they
            # armed immediately while the real position waited a full bar, so
            # the width A/B was confounded by the delay it was not testing.
            self.db.open_shadow(coin, tp, pos["side"], pos["entry"],
                                pos["qty"], pos["hard_stop"], opened,
                                arm_delay_h=live_delay)
        for h in self.stale_hours:
            # Same trail AND same delay the bot actually runs, so the ONLY
            # difference from the live position is the stale exit.
            self.db.open_shadow(coin, live_trail, pos["side"],
                                pos["entry"], pos["qty"], pos["hard_stop"],
                                opened, stale_h=h, arm_delay_h=live_delay)
        for tp, d in self.arm_delays:
            # tp None = the coin's live width, so only the delay differs.
            self.db.open_shadow(coin, live_trail if tp is None else tp, pos["side"],
                                pos["entry"], pos["qty"], pos["hard_stop"],
                                opened, arm_delay_h=d)

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
    def _hours_open(self, s):
        """Hours since the shadow's entry, or None if the timestamp is unusable."""
        try:
            t0 = datetime.fromisoformat(str(s["opened_at"]).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t0).total_seconds() / 3600

    @staticmethod
    def _col(s, name):
        """Row may be a sqlite3.Row or a dict depending on the caller."""
        try:
            return s[name] if name in s.keys() else None
        except (AttributeError, TypeError):
            return s.get(name)

    def _step(self, s, price):
        is_long = s["side"] == "long"

        # Arming delay, mirroring exit_manager exactly: no peak tracking and no
        # arming until the position is old enough; the hard stop stays live, and
        # the peak is PINNED to the current price so the trail starts from where
        # the market is when it goes live, not from an extreme printed while it
        # did not exist.
        delay = self._col(s, "arm_delay_h")
        if delay:
            age = self._hours_open(s)
            if age is not None and age < delay:
                if (price <= s["hard_stop"]) if is_long else (price >= s["hard_stop"]):
                    self._book(s, s["hard_stop"], "HARD_STOP")
                elif price != s["peak"]:
                    self.db.update_shadow(s["id"], peak=price)
                return

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
            return

        # Stale exit: only for rule shadows, only while the trail never armed.
        # Booked at the live mark, because that is what a market close gets.
        stale_h = self._col(s, "stale_h")
        if stale_h and not active:
            age = self._hours_open(s)
            if age is not None and age >= stale_h:
                self._book(s, price, "STALE")
                return
        if peak != s["peak"] or active != bool(s["active"]):
            self.db.update_shadow(s["id"], peak=peak, active=active)

    def _book(self, s, exit_px, reason):
        is_long = s["side"] == "long"
        move = (exit_px - s["entry"]) if is_long else (s["entry"] - exit_px)
        ret_pct = move / s["entry"] * 100 if s["entry"] else 0.0
        net_pct = ret_pct - self.friction_fn()   # fair-comparison: charge measured friction
        self.db.close_shadow(s["id"], round(exit_px, 8), ret_pct, net_pct, reason, s["opened_at"])
        sh, ad = self._col(s, "stale_h"), self._col(s, "arm_delay_h")
        tag = (f"@{s['trail_pct']}%" + (f"+stale{sh:g}h" if sh else "")
               + (f"+arm{ad:g}h" if ad is not None else ""))
        logger.info(f"    ◌ shadow {s['coin']} {tag} {reason} "
                    f"ret {ret_pct:+.2f}% net {net_pct:+.2f}% (friction {self.friction_fn():.2f}%)")
