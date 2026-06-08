"""
Soomario Libration — State
═══════════════════════════════
SQLite is the operational source of truth the bot reads/writes each tick:
  account     — single row: equity, daily baseline, daily-DD halt flag
  positions   — one open position per coin (pyramiding 1)
  trades      — closed trades (live KPI + dashboard stats)
  misses      — missed signals + reason (fill-rate KPI)
  rsi_state   — last closed 4h ts + last RSI per coin (double-fire guard)

Append-only JSONL logs (equity_log.jsonl, trade_log.jsonl) are written
alongside for the dashboard, conforming to EQUITY_CURVE_SPEC.md v1.2. SQLite
drives decisions; JSONL drives charts. The exchange is the ultimate source of
truth for fills — reconcile() trusts it and books any stop that fired while we
were not looking (the Farms orphan-position lesson).
"""
import logging
import sqlite3
from typing import Optional

from config import DB_PATH, EQUITY_LOG, TRADE_LOG
from utils import append_jsonl, iso, utc_date_str

logger = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  equity REAL, daily_baseline REAL, daily_halt INTEGER DEFAULT 0, last_reset TEXT
);
CREATE TABLE IF NOT EXISTS positions (
  coin TEXT PRIMARY KEY, side TEXT, entry REAL, qty REAL, notional REAL, margin REAL,
  peak REAL, hard_stop REAL, hard_stop_id TEXT, trail_active INTEGER DEFAULT 0,
  trail_stop REAL, opened_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT, side TEXT, entry REAL, exit REAL,
  qty REAL, ret_pct REAL, net_pct REAL, exit_reason TEXT, opened_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS misses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT, signal TEXT, reason TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS rsi_state (
  coin TEXT PRIMARY KEY, last_closed_4h_ts TEXT, last_rsi REAL
);
CREATE TABLE IF NOT EXISTS shadow_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT, trail_pct REAL, side TEXT,
  entry REAL, qty REAL, peak REAL, active INTEGER DEFAULT 0, hard_stop REAL, opened_at TEXT
);
CREATE TABLE IF NOT EXISTS shadow_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT, side TEXT, trail_pct REAL,
  entry REAL, shadow_exit REAL, shadow_ret_pct REAL, shadow_net_pct REAL,
  exit_reason TEXT, opened_at TEXT, shadow_closed_at TEXT
);
"""


class DB:
    def __init__(self, path=None):
        self.path = str(path or DB_PATH)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # Ensure the singleton account row exists.
        if self._conn.execute("SELECT 1 FROM account WHERE id=1").fetchone() is None:
            self._conn.execute(
                "INSERT INTO account(id, equity, daily_baseline, daily_halt, last_reset) "
                "VALUES (1, 0, 0, 0, ?)", (utc_date_str(),)
            )
            self._conn.commit()
        logger.info(f"🗄️  DB ready at {self.path}")

    # ── account ────────────────────────────────────────────────
    def account(self) -> dict:
        return dict(self._conn.execute("SELECT * FROM account WHERE id=1").fetchone())

    def set_account(self, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE account SET {cols} WHERE id=1", tuple(fields.values()))
        self._conn.commit()

    def daily_halt(self) -> bool:
        return bool(self.account()["daily_halt"])

    # ── positions ──────────────────────────────────────────────
    def open_positions(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM positions").fetchall()]

    def get_position(self, coin: str) -> Optional[dict]:
        r = self._conn.execute("SELECT * FROM positions WHERE coin=?", (coin.upper(),)).fetchone()
        return dict(r) if r else None

    def has_open_position(self, coin: str) -> bool:
        return self.get_position(coin) is not None

    def insert_position(self, p: dict):
        self._conn.execute(
            "INSERT OR REPLACE INTO positions "
            "(coin, side, entry, qty, notional, margin, peak, hard_stop, hard_stop_id, "
            " trail_active, trail_stop, opened_at) "
            "VALUES (:coin,:side,:entry,:qty,:notional,:margin,:peak,:hard_stop,"
            ":hard_stop_id,:trail_active,:trail_stop,:opened_at)",
            {
                "coin": p["coin"].upper(), "side": p["side"], "entry": p["entry"],
                "qty": p["qty"], "notional": p["notional"], "margin": p["margin"],
                "peak": p["peak"], "hard_stop": p["hard_stop"],
                "hard_stop_id": p.get("hard_stop_id"),
                "trail_active": int(p.get("trail_active", 0)),
                "trail_stop": p.get("trail_stop"), "opened_at": p.get("opened_at") or iso(),
            },
        )
        self._conn.commit()

    def update_position(self, coin: str, **fields):
        if not fields:
            return
        fields = {k: (int(v) if k == "trail_active" else v) for k, v in fields.items()}
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE positions SET {cols} WHERE coin=?",
                           tuple(fields.values()) + (coin.upper(),))
        self._conn.commit()

    def delete_position(self, coin: str):
        self._conn.execute("DELETE FROM positions WHERE coin=?", (coin.upper(),))
        self._conn.commit()

    # ── closed trades (SQLite KPI + JSONL for dashboard) ───────
    def book_trade(self, t: dict):
        """Record a closed trade in SQLite and append a close event to trade_log.jsonl."""
        self._conn.execute(
            "INSERT INTO trades "
            "(coin, side, entry, exit, qty, ret_pct, net_pct, exit_reason, opened_at, closed_at) "
            "VALUES (:coin,:side,:entry,:exit,:qty,:ret_pct,:net_pct,:exit_reason,:opened_at,:closed_at)",
            {
                "coin": t["coin"].upper(), "side": t["side"], "entry": t["entry"],
                "exit": t["exit"], "qty": t["qty"], "ret_pct": t.get("ret_pct"),
                "net_pct": t.get("net_pct"), "exit_reason": t.get("exit_reason"),
                "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at") or iso(),
            },
        )
        self._conn.commit()
        # Dashboard event marker. EXIT type with reason in the label.
        pnl = t.get("net_pct")
        try:
            label = f"EXIT {t['coin'].upper()} {pnl:+.2f}%"
        except (TypeError, ValueError):
            label = f"EXIT {t['coin'].upper()}"
        append_jsonl(TRADE_LOG, {
            "action": "EXIT", "type": t.get("exit_reason", "EXIT"),
            "asset": t["coin"].upper(), "side": t["side"],
            "net_pct": pnl, "label": label,
        })

    def recent_trades(self, n: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── misses (fill-rate KPI) ─────────────────────────────────
    def log_miss(self, coin: str, signal: str, reason: str):
        self._conn.execute(
            "INSERT INTO misses (coin, signal, reason, ts) VALUES (?,?,?,?)",
            (coin.upper(), signal, reason, iso()),
        )
        self._conn.commit()
        logger.info(f"    ✗ miss {coin} {signal}: {reason}")

    # ── rsi_state (double-fire guard) ──────────────────────────
    def get_rsi_state(self, coin: str) -> Optional[dict]:
        r = self._conn.execute("SELECT * FROM rsi_state WHERE coin=?", (coin.upper(),)).fetchone()
        return dict(r) if r else None

    def set_rsi_state(self, coin: str, last_closed_4h_ts, last_rsi):
        self._conn.execute(
            "INSERT OR REPLACE INTO rsi_state (coin, last_closed_4h_ts, last_rsi) VALUES (?,?,?)",
            (coin.upper(), str(last_closed_4h_ts) if last_closed_4h_ts is not None else None, last_rsi),
        )
        self._conn.commit()

    def all_rsi_state(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM rsi_state").fetchall()]

    # ── shadow trail A/B (counterfactual; never trades) ────────
    def open_shadow(self, coin, trail_pct, side, entry, qty, hard_stop, opened_at):
        self._conn.execute(
            "INSERT INTO shadow_state (coin, trail_pct, side, entry, qty, peak, active, hard_stop, opened_at) "
            "VALUES (?,?,?,?,?,?,0,?,?)",
            (coin.upper(), trail_pct, side, entry, qty, entry, hard_stop, opened_at),
        )
        self._conn.commit()

    def get_open_shadows(self, coin=None):
        if coin:
            rows = self._conn.execute("SELECT * FROM shadow_state WHERE coin=?", (coin.upper(),)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM shadow_state").fetchall()
        return [dict(r) for r in rows]

    def update_shadow(self, sid, **fields):
        if not fields:
            return
        fields = {k: (int(v) if k == "active" else v) for k, v in fields.items()}
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE shadow_state SET {cols} WHERE id=?",
                           tuple(fields.values()) + (sid,))
        self._conn.commit()

    def close_shadow(self, sid, shadow_exit, ret_pct, net_pct, reason, opened_at):
        row = self._conn.execute("SELECT * FROM shadow_state WHERE id=?", (sid,)).fetchone()
        if row is None:
            return
        self._conn.execute(
            "INSERT INTO shadow_trades (coin, side, trail_pct, entry, shadow_exit, "
            "shadow_ret_pct, shadow_net_pct, exit_reason, opened_at, shadow_closed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row["coin"], row["side"], row["trail_pct"], row["entry"], shadow_exit,
             round(ret_pct, 4), round(net_pct, 4), reason, opened_at, iso()),
        )
        self._conn.execute("DELETE FROM shadow_state WHERE id=?", (sid,))
        self._conn.commit()

    def shadow_summary(self):
        """Median net %/trade per shadow trail, for the dashboard A/B panel."""
        def _median(xs):
            xs = sorted(xs)
            n = len(xs)
            if n == 0:
                return None
            return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
        out = {}
        for tp, in self._conn.execute("SELECT DISTINCT trail_pct FROM shadow_trades").fetchall():
            nets = [r[0] for r in self._conn.execute(
                "SELECT shadow_net_pct FROM shadow_trades WHERE trail_pct=?", (tp,)).fetchall()]
            out[tp] = {"n": len(nets), "median_net_pct": _median(nets),
                       "win_rate": round(sum(1 for x in nets if x > 0) / len(nets) * 100, 2) if nets else None}
        return out

    def snapshot_equity(self, equity: float, extra: Optional[dict] = None):
        """Append one equity point. Cadence = poll tick (EQUITY_CURVE_SPEC §2)."""
        entry = {"equity": round(float(equity), 4),
                 "active_positions": len(self.open_positions())}
        if extra:
            entry.update(extra)
        append_jsonl(EQUITY_LOG, entry)

    def close(self):
        self._conn.close()
