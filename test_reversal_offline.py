"""Offline cover for REVERSE_ON_SIGNAL - no network, no venue keys.

TradingView's strategy.entry reverses on an opposite signal: the position is
closed and the other side opened in one go. The bot used to hold the old
position and skip the signal, which is how four accounts ended up long HYPE
from 09-25 while TradingView had been short since 09-28 04:00.
"""
import os
import shutil
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "reversal_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ.update(STATE_PATH=TMP, PAPER="0", DRY_RUN="0", TG_ENABLED="0",
                  EXCHANGE="hyperliquid", COINS="HYPE,SOL", NOTIONAL_FRAC="0.4",
                  WATCH="SOL", WATCH_SIZE_MULT="0.15", LEVERAGE="2",
                  MAX_CONCURRENT="14", COIN_PARAMS='{"HYPE":{"size_mult":0.5}}')

import config                                    # noqa: E402
from db import DB                                # noqa: E402
from exit_manager import ExitManager             # noqa: E402
from position_manager import PositionManager     # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


class Client:
    def __init__(self, price=90.0, close_ok=True):
        self.price, self.close_ok = price, close_ok
        self.opened, self.closed, self.cancels = [], [], []

    def get_equity(self):
        return 10_000.0

    def get_price(self, coin):
        return self.price

    def get_positions(self):
        return []

    def set_leverage(self, coin, lev):
        return True

    def market_open(self, coin, is_buy, notional, current_price=None):
        self.opened.append((coin, "long" if is_buy else "short", notional))
        return {"filled": True, "avg_price": self.price, "total_size": notional / self.price}

    def market_close(self, coin, qty, is_long, current_price=None):
        if not self.close_ok:
            return None
        self.closed.append((coin, qty, "long" if is_long else "short"))
        return {"filled": True, "avg_price": self.price, "total_size": qty}

    def place_stop_market(self, *a, **k):
        return "oid1"

    def cancel_order(self, coin, oid):
        self.cancels.append((coin, oid))
        return True

    def get_user_fills(self, start_ms=None):
        return []


def book(name, client=None, equity=10_000.0):
    db = DB(os.path.join(TMP, name))
    db.set_account(equity=equity, daily_baseline=equity, inception=equity,
                   inception_ts="2026-09-01T00:00:00+00:00")
    c = client or Client()
    pm = PositionManager(c, db)
    pm.exit_manager = ExitManager(c, db, pm)
    pm.ensure_seeded()
    return db, pm, c


def long_hype(db, entry=93.549):
    db.insert_position(dict(coin="HYPE", side="long", entry=entry, qty=20.29,
                            notional=entry * 20.29, margin=entry * 20.29 / 2,
                            peak=entry, hard_stop=entry * 0.9, hard_stop_id="oid0",
                            trail_active=0, trail_stop=None, intended_entry=entry,
                            opened_at="2026-09-25T12:00:00+00:00"))


print("\n1. the live case: long held, short signal arrives")
db, pm, c = book("rev.db")
long_hype(db)
pos = pm.maybe_enter("HYPE", "short", 90.0)
check("the old long was closed on the venue", c.closed and c.closed[0][0] == "HYPE")
check("  its resting stop was cancelled", c.cancels and c.cancels[0][1] == "oid0")
check("  booked as REVERSAL", db.recent_trades(5)[0]["exit_reason"] == "REVERSAL")
check("  with the loss recorded", db.recent_trades(5)[0]["net_pct"] < 0)
check("the new short is open", pos and pos["side"] == "short")
check("  and it is the only HYPE position", len(db.open_positions()) == 1)
check("  sized off equity, not the old position",
      abs(c.opened[0][2] - pm.sizing_equity() * 0.4 * 0.5) < 1.0)

print("\n2. a signal the same way round changes nothing")
db2, pm2, c2 = book("same.db")
long_hype(db2)
check("no second entry", pm2.maybe_enter("HYPE", "long", 90.0) is None)
check("  nothing closed", not c2.closed)
check("  the position is untouched", db2.open_positions()[0]["entry"] == 93.549)

print("\n3. REVERSE_ON_SIGNAL=0 keeps the old behaviour")
config.REVERSE_ON_SIGNAL = False
db3, pm3, c3 = book("off.db")
long_hype(db3)
check("signal skipped", pm3.maybe_enter("HYPE", "short", 90.0) is None)
check("  position kept", db3.open_positions()[0]["side"] == "long")
check("  and the miss is logged, not silent",
      db3._conn.execute("SELECT reason FROM misses ORDER BY id DESC LIMIT 1").fetchone()[0]
      == "opposite_position_held")
config.REVERSE_ON_SIGNAL = True

print("\n4. a venue that will not close keeps the position")
db4, pm4, c4 = book("fail.db", Client(close_ok=False))
long_hype(db4)
check("no new position", pm4.maybe_enter("HYPE", "short", 90.0) is None)
check("  the long is still held", db4.open_positions()[0]["side"] == "long")
check("  nothing was booked", db4.trade_count() == 0)
check("  the reason is recorded for the next tick",
      db4._conn.execute("SELECT reason FROM misses ORDER BY id DESC LIMIT 1").fetchone()[0]
      == "reversal_close_failed")

print("\n5. guards still come first")
db5, pm5, c5 = book("halt.db")
long_hype(db5)
db5.set_account(daily_halt=1)
check("a daily halt blocks the reversal too", pm5.maybe_enter("HYPE", "short", 90.0) is None)
check("  and the long is left alone", db5.open_positions()[0]["side"] == "long")

print("\n6. flatten_all still works through the shared close")
db6, pm6, c6 = book("flat.db")
long_hype(db6)
db6.insert_position(dict(coin="SOL", side="short", entry=200.0, qty=3.0, notional=600.0,
                         margin=300.0, peak=200.0, hard_stop=220.0, hard_stop_id="oid2",
                         trail_active=0, trail_stop=None, intended_entry=200.0,
                         opened_at="2026-09-27T00:00:00+00:00"))
n = pm6.flatten_all(reason="DAILY_GUARD")
check("both positions closed", n == 2 and not db6.open_positions())
check("  booked with the guard's reason",
      all(t["exit_reason"] == "DAILY_GUARD" for t in db6.recent_trades(5)))

print("\n7. close_one is what FORCE_CLOSE uses")
db7, pm7, c7 = book("force.db")
long_hype(db7)
fill = pm7.close_one(db7.get_position("HYPE"), reason="FORCE_CLOSE")
check("closed at the venue", fill == 90.0 and bool(c7.closed))
check("  booked as FORCE_CLOSE", db7.recent_trades(2)[0]["exit_reason"] == "FORCE_CLOSE")
check("  the stop was cancelled", bool(c7.cancels) and c7.cancels[0][1] == "oid0")
check("  the book is flat", not db7.open_positions())

db8, pm8, c8 = book("force_fail.db", Client(close_ok=False))
long_hype(db8)
check("a venue that will not fill leaves it open",
      pm8.close_one(db8.get_position("HYPE"), reason="FORCE_CLOSE") is None
      and db8.open_positions()[0]["side"] == "long")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
