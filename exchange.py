"""
Soomario Libration-Solana — fan-out core
════════════════════════════════════════
Three small pieces that let ONE Hyperliquid-derived signal drive many isolated
venue books, reusing the single-venue strategy modules (signals / PositionManager
/ ExitManager / DB) completely unchanged:

  • make_client(name)  — factory: "hyperliquid" | "pacifica" | "bulk".
  • SignalEngine       — computes the canonical RSI(14)/4h cross ONCE per tick
                         from HL candles and returns {coin: 'long'|'short'}. The
                         rsi_state / new-bar guard lives in ONE shared signal DB,
                         so a cross is detected once and never double-fires — even
                         though it is then fanned to every venue.
  • VenueBook          — a per-venue bundle (its OWN SQLite DB, PositionManager,
                         ExitManager, equity, inception baseline, reconcile). It
                         executes the fanned signal in isolation, sizing off its
                         own equity and filling at its OWN mark, and writes its own
                         status.json + equity_log.jsonl under STATE_DIR/<venue>/.

The strategy parameters (leverage, notional fraction, RSI levels, trail, hard
stop, daily-DD halt) come straight from config.py — the SAME frozen values the
live Hyperliquid Libration bot uses. Nothing here re-tunes them.
"""
import logging
from pathlib import Path

import config
import signals
from utils import iso, save_json, append_jsonl
from db import DB
from position_manager import PositionManager
from exit_manager import ExitManager

logger = logging.getLogger("exchange")


# ═══════════════════════════════════════════════════════════════════
#  Client factory
# ═══════════════════════════════════════════════════════════════════
def make_client(name: str):
    """Return a venue client that satisfies the hl_client contract the strategy
    layer drives. Raises ValueError for an unknown venue so the worker can skip
    it cleanly. init_sdk() is the caller's responsibility (the worker calls it
    for the trading venues; the read-only HL feed doesn't need it)."""
    key = (name or "").strip().lower()
    if key in ("hyperliquid", "hl"):
        from hl_client import HLClient
        return HLClient()
    if key == "pacifica":
        from pacifica_client import PacificaClient
        return PacificaClient()
    if key == "bulk":
        from bulk_client import BulkClient
        return BulkClient()
    raise ValueError(f"unknown venue '{name}'")


# ═══════════════════════════════════════════════════════════════════
#  Signal engine — one canonical HL cross per tick
# ═══════════════════════════════════════════════════════════════════
class SignalEngine:
    """Computes the Libration entry signal exactly as the single-venue worker
    does (see app.tick), but only DETECTS the cross — the VenueBooks execute it.

    State (last closed 4h ts + last RSI per coin) is persisted in a SHARED signal
    DB, so `compute()` fires each cross exactly once regardless of how many venues
    consume it. Uses Hyperliquid candles as the canonical superset feed."""

    def __init__(self, hl, signal_db: DB, coins):
        self.hl = hl
        self.db = signal_db
        self.coins = [c.upper() for c in coins]

    def compute(self, now_ms: int) -> dict:
        """Return {coin: 'long'|'short'} for coins whose RSI crossed a level on a
        FRESHLY closed 4h bar this tick. Cold-start bars are primed but not traded
        (their close price is stale), matching the backtest's close fills."""
        fired = {}
        for coin in self.coins:
            try:
                candles = self.hl.fetch_candles(coin, config.RSI_TF, config.CANDLE_LIMIT)
                closed = signals.closed_candles(candles, now_ms)
                if len(closed) < config.RSI_LEN + 2:
                    continue
                last_ts = closed[-1]["t"]
                st = self.db.get_rsi_state(coin)
                seen = int(st["last_closed_4h_ts"]) if st and st.get("last_closed_4h_ts") else None
                closes = [c["c"] for c in closed]
                rsi = signals.wilder_rsi(closes, config.RSI_LEN)
                # Persist the newest RSI + bar ts in the shared DB (the double-fire
                # guard). This is the ONLY writer of rsi_state, so the cross can
                # never be re-evaluated by a second venue on the same bar.
                self.db.set_rsi_state(coin, last_ts, rsi[-1])
                if seen is None:
                    logger.info(f"    · primed {coin} — no cold-start entry on pre-existing bar")
                    continue
                if not signals.new_closed_bar(seen, last_ts):
                    continue  # already evaluated this bar
                # Per-coin levels (COIN_PARAMS) -- the whole point of the merge:
                # a fanned venue trades the same book with the same numbers,
                # including CC's 65-above-50 straddle wherever it is listed.
                sig = signals.entry_signal(rsi[-2], rsi[-1],
                                           config.long_level(coin), config.short_level(coin))
                if sig:
                    fired[coin] = sig
                    logger.info(f"    ⚡ SIGNAL {sig.upper()} {coin} "
                                f"(rsi {rsi[-2]:.1f} → {rsi[-1]:.1f})")
            except Exception as e:
                logger.warning(f"signal eval {coin} failed: {e}")
        return fired


# ═══════════════════════════════════════════════════════════════════
#  Venue book — isolated per-venue execution + accounting
# ═══════════════════════════════════════════════════════════════════
class VenueBook:
    """One venue's isolated world: its own DB (equity, inception, positions,
    trades, misses), its own PositionManager / ExitManager, and its own
    status.json + equity_log.jsonl under STATE_DIR/<venue>/. The accounting
    invariants therefore hold per venue with the core modules reused unchanged."""

    def __init__(self, name: str, client, coins, state_dir):
        self.name = name
        self.client = client
        self.coins = [c.upper() for c in coins]
        self.dir = Path(state_dir) / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = DB(path=str(self.dir / "libration.db"))
        self.pm = PositionManager(client, self.db)
        self.em = ExitManager(client, self.db, self.pm)
        self.status_file = self.dir / "status.json"
        self.equity_log = self.dir / "equity_log.jsonl"

    def boot(self):
        """Seed the flow-neutral baseline once and re-adopt any open exchange
        position the DB has lost (self-heal), exactly like the single-venue
        worker's startup, but scoped to this venue's client + DB."""
        self.pm.ensure_seeded()
        self.pm.adopt_unmanaged()
        logger.info(f"[{self.name}] booted — equity ${self.pm.equity():.2f}, "
                    f"{len(self.coins)} coins, {len(self.db.open_positions())} open")

    def step(self, fired: dict, hl_marks: dict):
        """Execute one tick for this venue: enter any fanned signal in THIS
        venue's universe, reconcile against the exchange, trail-manage open
        positions, check the daily-DD halt, then snapshot state to disk. All
        priced off this venue's OWN mark (HL marks are only a fallback)."""
        self.pm.maybe_reset_daily()
        marks = self._marks(hl_marks)

        # ── entries: only coins this venue trades, priced at its own mark ──
        for coin, sig in fired.items():
            if coin not in self.coins:
                continue
            self.pm.maybe_enter(coin, sig, marks.get(coin))

        # ── reconcile FIRST (book anything the exchange already closed), then
        #    trail-manage / backstop the still-open set ──
        self.em.reconcile()
        for p in self.db.open_positions():
            price = marks.get(p["coin"])
            if price:
                self.em.manage(p, price)

        self.pm.check_daily_dd()
        self._snapshot(marks)

    def _marks(self, hl_marks: dict) -> dict:
        """This venue's marks, with HL marks as a fallback for any symbol the
        venue feed didn't return. The venue's own price WINS for execution."""
        out = dict(hl_marks or {})
        try:
            own = self.client.get_all_prices(extra=self.coins) or {}
            out.update(own)
        except Exception as e:
            logger.warning(f"[{self.name}] price poll failed, using HL marks: {e}")
        return out

    def _snapshot(self, marks: dict):
        """Write this venue's status.json (dashboard reads it) and append one
        equity point to its equity_log.jsonl. Mirrors app._write_status but
        scoped to the venue dir; the dashboard API reads exactly these files."""
        db, pm = self.db, self.pm
        equity = pm.equity()
        total_upnl = 0.0
        positions = []
        for p in db.open_positions():
            mark = marks.get(p["coin"])
            upnl = None
            if mark:
                move = (mark - p["entry"]) if p["side"] == "long" else (p["entry"] - mark)
                upnl = round(move * p["qty"], 4)
                total_upnl += move * p["qty"]
            eff_stop = p["trail_stop"] if p["trail_stop"] is not None else p["hard_stop"]
            positions.append({
                "coin": p["coin"], "side": p["side"], "entry": p["entry"], "qty": p["qty"],
                "notional": p["notional"], "margin": p["margin"], "mark": mark, "upnl": upnl,
                "peak": p["peak"], "stop": eff_stop, "hard_stop": p["hard_stop"],
                "trail_active": bool(p["trail_active"]), "opened_at": p["opened_at"],
                "reduced": p["coin"].upper() in config.WATCH_SET,
            })
        acct = db.account()
        inception = acct.get("inception") or equity or 0.0
        realized_pnl = db.realized_pnl()
        total_pnl = realized_pnl + total_upnl
        save_json(self.status_file, {
            "ts": iso(),
            "venue": self.name,
            "name": f"{config.NAME} — {self.name}",
            "config": config.summary(),
            "equity": round(equity, 2),
            "total_upnl": round(total_upnl, 2),
            "realized_pnl": round(realized_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / inception * 100, 2) if inception else 0.0,
            "inception": round(inception, 2),
            "open_positions": len(positions),
            "max_concurrent": pm.max_concurrent,
            "daily_halt": bool(acct["daily_halt"]),
            "daily_baseline": acct["daily_baseline"],
            "coins": self.coins,
            "positions": positions,
        })
        append_jsonl(self.equity_log, {
            "ts": iso(), "equity": round(equity, 4),
            "active_positions": len(positions), "total_upnl": round(total_upnl, 4),
        })
