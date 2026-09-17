"""Offline cover for the Foxify move from soomario-prop - no network, no keys.

  1. Two-stage confirmation: nothing is booked as opened or closed until
     /signals/trade-status or /signals/positions proves it. Replays the
     2026-08-10 (pending entry never filled) and 2026-07-31 (close reported
     while the venue still held it) incidents.
  2. FOXIFY_START_BALANCE pins the static-drawdown anchor to the funded
     balance, while today's daily baseline stays at live equity.
  3. journal_import books soomario-prop's closed trades so realized PnL equals
     the wallet history, and is idempotent.
"""
import json
import os
import shutil
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "foxify_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ.update(STATE_PATH=TMP, PAPER="0", DRY_RUN="0", TG_ENABLED="0", EXCHANGE="foxify",
                  COINS="HYPE", FOXIFY_SIGNAL_ID="t", FOXIFY_PASSPHRASE="t",
                  FOXIFY_STATUS_SEC="0.05", FOXIFY_CONFIRM_SEC="0.05",
                  MAX_DD_PCT="20", DD_TYPE="static", DD_GUARD_MARGIN="0")

import config                                  # noqa: E402
import foxify_client                           # noqa: E402
import journal_import                          # noqa: E402
from db import DB                              # noqa: E402
from position_manager import PositionManager   # noqa: E402

foxify_client.time.sleep = lambda s: None      # deadlines still run on the clock

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


class HL:
    def get_price(self, s):
        return 80.0


class Kitsune:
    """Scripted venue. status: what trade-status reports; positions: a list of
    what successive /signals/positions reads return (last one repeats)."""

    def __init__(self, status="completed", positions=None, equity=484.21):
        self.status, self.positions, self.equity = status, positions or [[]], equity
        self.sent, self.reads = [], 0

    def __call__(self, path, body):
        if path == "/signals/trade":
            self.sent.append(body)
            return {"accepted": True, "async": True, "requestId": "r1"}
        if path == "/signals/trade-status":
            return {"status": self.status, "error": "All VAs failed",
                    "results": [{"success": False, "vaName": "va", "statusCode": 500}]}
        if path == "/signals/positions":
            i = min(self.reads, len(self.positions) - 1)
            self.reads += 1
            return self.positions[i]
        if path == "/signals/balance":
            return {"currentBalance": self.equity, "availableBalance": self.equity}
        raise AssertionError(path)


LONG = [{"symbol": "PERP_HYPE_USDC", "direction": "long", "size": 145.2,
         "entryPrice": 80.2, "unrealizedPnl": 0.1, "id": 1}]


def client(venue, start=None):
    if start is None:
        os.environ.pop("FOXIFY_START_BALANCE", None)
    else:
        os.environ["FOXIFY_START_BALANCE"] = str(start)
    c = foxify_client.FoxifyClient(hl_reader=HL())
    c._post = venue
    c.asset_meta = {}
    c._round = lambda sym, sz: round(sz, 2)
    return c


print("\n1. opens")
v = Kitsune("completed", [LONG])
r = client(v).market_open("HYPE", True, 145.2, current_price=80.0)
check("completed + visible -> booked at the venue entry", r and r["avg_price"] == 80.2)
check("  size read back from the venue", r and abs(r["total_size"] - 145.2 / 80.2) < 1e-9)
check("  hard stop rides with the entry", v.sent[0].get("sl") == config.HARD_STOP_PCT)
check("  leverage defaults to 1", v.sent[0].get("leverage") == 1.0)

c = client(Kitsune("failed", [[]]))
check("failed downstream -> not booked", c.market_open("HYPE", True, 145.2, 80.0) is None)
check("  reason recorded", "execution failed" in (c.last_open_error or ""))

r = client(Kitsune("pending", [[], [], LONG])).market_open("HYPE", True, 145.2, 80.0)
check("pending, then position appears -> booked", r and r["avg_price"] == 80.2)

c = client(Kitsune("pending", [[]]))
check("2026-08-10: pending and never fills -> NOT booked",
      c.market_open("HYPE", True, 145.2, 80.0) is None)
check("  reason says unconfirmed", "UNCONFIRMED" in (c.last_open_error or ""))

c = client(Kitsune("pending", [None]))
check("pending and positions unreadable -> NOT booked",
      c.market_open("HYPE", True, 145.2, 80.0) is None)

r = client(Kitsune("completed", [[]])).market_open("HYPE", True, 145.2, 80.0)
check("completed but not yet visible -> booked at mark (execution not in doubt)",
      r and r["avg_price"] == 80.0)

print("\n2. closes")
v = Kitsune("completed", [[]])
r = client(v).market_close("HYPE", 1.81, True, current_price=80.0)
check("completed -> filled", r and r["filled"])
check("  sent reduce-only, opposite side", v.sent[0]["reduceOnly"] and v.sent[0]["action"] == "sell")

check("failed -> not booked",
      client(Kitsune("failed", [LONG])).market_close("HYPE", 1.81, True, 80.0) is None)
check("2026-07-31: pending while the venue still holds it -> NOT booked",
      client(Kitsune("pending", [LONG])).market_close("HYPE", 1.81, True, 80.0) is None)
r = client(Kitsune("pending", [LONG, []])).market_close("HYPE", 1.81, True, 80.0)
check("pending, then position disappears -> filled", r and r["filled"])
check("pending and positions unreadable -> NOT booked",
      client(Kitsune("pending", [None])).market_close("HYPE", 1.81, True, 80.0) is None)

print("\n3. pinned start balance")


def fresh_db(name):
    return DB(os.path.join(TMP, name))


c = client(Kitsune(equity=484.21), start=500)
check("FOXIFY_START_BALANCE exposed to the dashboard anchor", c._initial_balance == 500.0)
db = fresh_db("pin.db")
pm = PositionManager(c, db)
pm.ensure_seeded()
a = db.account()
check("fresh DB: inception = funded balance, not boot equity", a["inception"] == 500.0)
check("  daily baseline = live equity (no phantom day-one loss)", a["daily_baseline"] == 484.21)

db2 = fresh_db("nopin.db")
PositionManager(client(Kitsune(equity=484.21)), db2).ensure_seeded()
check("no pin: behaviour unchanged (inception = live equity)",
      db2.account()["inception"] == 484.21)

PositionManager(client(Kitsune(equity=484.21), start=500), db2).ensure_seeded()
a = db2.account()
check("existing DB anchored at boot equity is re-pinned", a["inception"] == 500.0)
check("  equity accumulator resynced to the pin", abs(a["equity"] - 500.0) < 1e-6)

for eq, halts in ((400.5, False), (399.5, True)):
    d = fresh_db(f"guard{eq}.db")
    k = Kitsune(equity=484.21)
    p = PositionManager(client(k, start=500), d)
    p.ensure_seeded()
    k.equity = eq
    p.check_max_dd()
    check(f"static guard at venue equity ${eq}: halt={halts} (floor $400)",
          d.max_dd_halt() == halts)

print("\n4. journal import")
perf = {"start_balance": 500.0, "trades": [
    {"close": "2026-09-13T09:26:16+00:00", "entry": 78.9965, "qty": 1.85, "side": "short",
     "pnl": 2.366349, "hold_sec": 19568.6},
    {"close": "2026-08-15T16:33:54+00:00", "entry": 56.481, "qty": 5.33, "side": "long",
     "pnl": -17.494518, "hold_sec": None},
    {"close": "2026-07-31T19:03:47+00:00", "entry": 55.8675, "qty": 5.8, "side": "long",
     "pnl": -17.886025, "hold_sec": 68603.2},
]}
src = os.path.join(TMP, "perf.json")
json.dump(perf, open(src, "w"))
k = Kitsune(equity=467.0)
db3 = fresh_db("imp.db")
pm3 = PositionManager(client(k, start=500), db3)
pm3.ensure_seeded()
added = journal_import.run(db3, src, "HYPE")
pm3.ensure_seeded()
want = 2.366349 - 17.494518 - 17.886025
check("all trades added", added == 3)
check("realized PnL equals the wallet deltas exactly", abs(db3.realized_pnl() - want) < 1e-6)
a = db3.account()
check("account equity = start + imported history", abs(a["equity"] - (500 + want)) < 1e-4)
check("inception still the funded balance", a["inception"] == 500.0)
check("history starts at the first imported open",
      a["inception_ts"].startswith("2026-07-31T00:00"))
rows = {r["closed_at"]: r for r in db3.recent_trades(10)}
check("short exit derived on the right side of entry",
      rows["2026-09-13T09:26:16+00:00"]["exit"] < 78.9965)
check("unknown hold time -> no invented open time",
      rows["2026-08-15T16:33:54+00:00"]["opened_at"] is None)
check("tagged IMPORTED", all(r["exit_reason"] == "IMPORTED" for r in rows.values()))
check("second run is idempotent", journal_import.run(db3, src, "HYPE") == 0)

jr = {"records": [{"asset": "HYPE", "side": "long", "qty": 2.0, "entry_px": 50.0,
                   "exit_px": 51.0, "balance_before": 500.0, "balance_after": 501.85,
                   "opened_at": "2026-08-01T00:00:00+00:00",
                   "closed_at": "2026-08-01T04:00:00+00:00"}]}
db4 = fresh_db("jr.db")
t = journal_import.parse(jr)[0]
check("raw journal: real exit fill kept", t["exit"] == 51.0)
check("  fee backed out so PnL = balance delta", abs(t["fee"] - 0.15) < 1e-9)
db4.set_account(equity=500.0, inception=500.0)
jsrc = os.path.join(TMP, "jr.json")
json.dump(jr, open(jsrc, "w"))
journal_import.run(db4, jsrc, "HYPE")
check("  booked realized equals the balance delta", abs(db4.realized_pnl() - 1.85) < 1e-9)
try:
    journal_import.parse({"nope": 1})
    check("unknown shape rejected", False)
except ValueError:
    check("unknown shape rejected", True)

print("\n5. ledger reset after a venue re-fund")
import ledger_reset                            # noqa: E402

db5 = fresh_db("reset.db")
pm5 = PositionManager(client(Kitsune(equity=500.0), start=500), db5)
pm5.ensure_seeded()


def book(coin, entry, exit_, qty, closed):
    db5.book_trade(dict(coin=coin, side="long", entry=entry, exit=exit_, qty=qty, fee=0.0,
                        exit_reason="TRAIL", opened_at=closed, closed_at=closed))


book("TAO", 100.0, 90.0, 5.0, "2026-08-09T00:23:05+00:00")      # old account: -50
book("DOT", 1.0, 0.9, 100.0, "2026-08-10T08:23:14+00:00")       # old account: -10
book("XMR", 10.0, 11.0, 10.0, "2026-08-22T03:06:35+00:00")      # new account: +10
book("SOL", 100.0, 101.0, 2.0, "2026-09-17T09:56:59+00:00")     # new account: +2
pm5.ensure_seeded()
check("before: old losses sit in realized", abs(db5.realized_pnl() - (-48.0)) < 1e-9)
r = ledger_reset.run(db5, "2026-08-15T00:00:00Z", 500.0)
pm5.ensure_seeded()
a = db5.account()
check("old-account trades archived, not deleted",
      r["archived"] == 2
      and db5._conn.execute("SELECT COUNT(*) FROM trades_archive").fetchone()[0] == 2)
check("new-account trades kept", r["kept"] == 2)
check("realized = new account only", abs(db5.realized_pnl() - 12.0) < 1e-9)
check("equity resynced to start + new realized", abs(a["equity"] - 512.0) < 1e-6)
check("inception stays the funded balance", a["inception"] == 500.0)
check("history starts at the reset", a["inception_ts"].startswith("2026-08-15T00:00:00"))
check("backup written", bool(r["backup"]) and os.path.exists(r["backup"]))
check("second run is a no-op",
      ledger_reset.run(db5, "2026-08-15T00:00:00Z", 500.0)["archived"] == 0)
db5.insert_position(dict(coin="HYPE", side="long", entry=80.0, qty=1.0, notional=80.0,
                         margin=40.0, peak=80.0, hard_stop=72.0, hard_stop_id="n",
                         trail_active=0, trail_stop=None, intended_entry=80.0,
                         opened_at="2026-08-14T00:00:00+00:00"))
try:
    ledger_reset.run(db5, "2026-08-15T00:00:00Z", 500.0)
    check("refuses while an old-account position is still booked", False)
except RuntimeError:
    check("refuses while an old-account position is still booked", True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
