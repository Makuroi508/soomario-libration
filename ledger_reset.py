"""
Soomario Libration — start the ledger over when the VENUE resets the account
════════════════════════════════════════════════════════════════════════════
A prop venue can hand the same credentials a fresh account: Foxify re-funds a
breached challenge back to its starting balance under the same signalId. The
bot keeps trading straight through, which is fine, but its ledger still holds
the previous account's trades. Every figure built on it is then wrong at once:
realized equity (and so position sizing) carries the old losses, the dashboard
shows the old drawdown, and the report measures a dead account.

LEDGER_RESET_AT=<ISO time between the last old trade and the first new one>

  * backs the database up with sqlite's online backup (WAL-safe),
  * MOVES every trade closed before that time into trades_archive (nothing is
    deleted; the old account stays inspectable),
  * sets inception_ts to the reset time; inception itself comes from the pinned
    venue start balance when there is one (FOXIFY_START_BALANCE), else stays.

The caller then resyncs equity and rebuilds the curve. Idempotent: a second run
finds nothing left to move.
"""
import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger("ledger_reset")


def _parse(ts: str) -> str:
    d = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


def run(db, reset_at: str, start_balance=None) -> dict:
    cut = _parse(reset_at)
    conn = db._conn
    # Guard against a cut placed inside a live round trip on the new account:
    # an open position opened before the cut belongs to the OLD account.
    stale_open = [p["coin"] for p in db.open_positions()
                  if p.get("opened_at") and str(p["opened_at"]) < cut]
    if stale_open:
        raise RuntimeError(f"positions opened before {cut} are still in the book "
                           f"({', '.join(stale_open)}) - pick a later reset time")

    n_old = conn.execute("SELECT COUNT(*) FROM trades WHERE closed_at < ?", (cut,)).fetchone()[0]
    result = {"cut": cut, "archived": 0, "kept": 0, "backup": None}
    if n_old:
        bak = f"{db.path}.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.pre-reset.bak"
        dst = sqlite3.connect(bak)
        with dst:
            conn.backup(dst)
        dst.close()
        result["backup"] = bak
        cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)")]
        conn.execute(f"CREATE TABLE IF NOT EXISTS trades_archive AS SELECT * FROM trades WHERE 0")
        have = {r[1] for r in conn.execute("PRAGMA table_info(trades_archive)")}
        for c in cols:
            if c not in have:
                conn.execute(f"ALTER TABLE trades_archive ADD COLUMN {c}")
        cl = ", ".join(cols)
        with conn:
            conn.execute(f"INSERT INTO trades_archive ({cl}) SELECT {cl} FROM trades "
                         f"WHERE closed_at < ?", (cut,))
            conn.execute("DELETE FROM trades WHERE closed_at < ?", (cut,))
        result["archived"] = n_old

    fields = {"inception_ts": cut}
    if start_balance:
        fields["inception"] = float(start_balance)
    db.set_account(**fields)
    result["kept"] = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    result["realized_after"] = db.realized_pnl()
    logger.warning(f"🧹 ledger reset at {cut}: {result['archived']} old-account trade(s) "
                   f"archived, {result['kept']} kept, realized now ${result['realized_after']:+.2f}"
                   + (f" (backup {result['backup']})" if result["backup"] else ""))
    return result
