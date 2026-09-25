"""Offline cover for CAPITAL_FLOW - no network, no venue keys.

A deposit must move the sizing base and the drawdown anchor together, and must
leave realized PnL and the trade ledger alone. Modelled on the real aureus
case: ledger equity $338.33 against a $1,468.85 wallet after a deposit.
"""
import os
import shutil
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "capflow_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ.update(STATE_PATH=TMP, PAPER="0", DRY_RUN="0", TG_ENABLED="0",
                  EXCHANGE="hyperliquid", COINS="HYPE,SOL", NOTIONAL_FRAC="0.8",
                  WATCH="SOL", WATCH_SIZE_MULT="0.15", MAX_DD_PCT="20",
                  DD_TYPE="static", DD_GUARD_MARGIN="0",
                  COIN_PARAMS='{"HYPE":{"size_mult":0.5}}')

import capital_flow                              # noqa: E402
import config                                    # noqa: E402
from db import DB                                # noqa: E402
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
    """Wallet holds more than the ledger knows about: a deposit landed."""

    def __init__(self, equity=1468.85, price=80.0):
        self.eq, self.price = equity, price

    def get_equity(self):
        return self.eq

    def get_price(self, coin):
        return self.price

    def get_positions(self):
        return []


def book(db, coin, entry, exit_, qty, closed):
    db.book_trade(dict(coin=coin, side="long", entry=entry, exit=exit_, qty=qty, fee=0.0,
                       exit_reason="TRAIL", opened_at=closed, closed_at=closed))


def seeded(name, inception=381.22, realized_trade=(100.0, 57.11, 1.0)):
    """Account at aureus's numbers: $381.22 in, one losing trade, $338.33 left."""
    db = DB(os.path.join(TMP, name))
    db.set_account(equity=inception, daily_baseline=inception, inception=inception,
                   inception_ts="2026-07-01T00:00:00+00:00")
    e, x, q = realized_trade
    book(db, "SOL", e, x, q, "2026-09-01T00:00:00+00:00")
    pm = PositionManager(Client(), db)
    pm.ensure_seeded()
    return db, pm


print("\n1. an explicit deposit")
db, pm = seeded("dep.db")
check("before: ledger equity is the pre-deposit number", abs(db.account()["equity"] - 338.33) < 0.01)
check("  the wallet says otherwise", pm.client.get_equity() == 1468.85)
res = capital_flow.apply(db, capital_flow.resolve(db, "1130.52"))
a = db.account()
check("equity now matches the wallet", abs(a["equity"] - 1468.85) < 0.01)
check("drawdown anchor moved by the same amount", abs(a["inception"] - 1511.74) < 0.01)
check("  so the 20% floor is now $1,209.39", abs(a["inception"] * 0.8 - 1209.39) < 0.01)
check("today's daily baseline moved too (no phantom daily loss)",
      abs(a["daily_baseline"] - 1511.74) < 0.01)
check("realized PnL untouched", abs(db.realized_pnl() - (-42.89)) < 0.01)
check("trade ledger untouched", db.trade_count() == 1)
check("a later resync does not undo it", (pm.ensure_seeded(), abs(db.account()["equity"] - 1468.85) < 0.01)[1])
check("sizing follows: HYPE at 0.8 x 0.5 of the new equity",
      abs(pm.equity() * config.NOTIONAL_FRAC * config.size_mult("HYPE") - 587.54) < 0.5)

print("\n2. auto, from the venue")
db2, pm2 = seeded("auto.db")
flow = capital_flow.resolve(db2, "auto", venue_equity=1468.85, open_upnl=0.0)
check("auto sizes the flow from the wallet", abs(flow - 1130.52) < 0.01)
capital_flow.apply(db2, flow)
check("  and lands on the wallet figure", abs(db2.account()["equity"] - 1468.85) < 0.01)

db3, _ = seeded("auto_upnl.db")
check("auto does not count an open position's unrealised PnL as a deposit",
      abs(capital_flow.resolve(db3, "auto", venue_equity=1468.85, open_upnl=25.0)
          - 1105.52) < 0.01)

print("\n3. withdrawals and refusals")
db4, _ = seeded("wd.db")
capital_flow.apply(db4, capital_flow.resolve(db4, "-100"))
a = db4.account()
check("a withdrawal lowers both ends", abs(a["equity"] - 238.33) < 0.01
      and abs(a["inception"] - 281.22) < 0.01)

for raw, why in (("0", "zero"), ("", "empty"), ("abc", "not a number")):
    try:
        capital_flow.resolve(db4, raw)
        check(f"{why} is rejected", False)
    except (ValueError, RuntimeError):
        check(f"{why} is rejected", True)

try:
    capital_flow.resolve(db4, "auto", venue_equity=0.0)
    check("auto with no venue read is rejected", False)
except RuntimeError:
    check("auto with no venue read is rejected", True)

db5, _ = seeded("zero.db")
try:
    capital_flow.apply(db5, -5000.0)
    check("a flow that would empty the account is refused", False)
except RuntimeError:
    check("a flow that would empty the account is refused", True)
check("  and nothing moved", abs(db5.account()["equity"] - 338.33) < 0.01)

print("\n4. the drawdown guard uses the new anchor")
db6, pm6 = seeded("guard.db")
capital_flow.apply(db6, 1130.52)
pm6.check_max_dd()
check("no halt while above the new floor ($1,209.39 on $1,468.85)", not db6.max_dd_halt())
book(db6, "SOL", 100.0, 68.0, 10.0, "2026-09-20T00:00:00+00:00")   # -$320 realized
pm6.ensure_seeded()
check("  equity now $1,148.85", abs(pm6.equity() - 1148.85) < 0.01)
pm6.check_max_dd()
check("halts below it", db6.max_dd_halt())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
