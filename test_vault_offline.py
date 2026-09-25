"""Offline cover for vault sizing and the flow-immune drawdown index.

  1. EQUITY_SOURCE=venue sizes from the exchange's own account value, so a
     depositor's money is used and a withdrawal is respected without anyone
     recording a flow. Modelled on the live vault: ledger $1,810 against a real
     $1,643.
  2. DD_BASIS=index measures drawdown on a unit-value curve, which deposits and
     withdrawals cannot move - and which gives TRAILING a real high-water mark
     on venues that publish none.
"""
import os
import shutil
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "vault_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ.update(STATE_PATH=TMP, PAPER="0", DRY_RUN="0", TG_ENABLED="0",
                  EXCHANGE="hyperliquid", COINS="HYPE,SOL", WATCH="SOL",
                  NOTIONAL_FRAC="0.8", WATCH_SIZE_MULT="0.15", LEVERAGE="2",
                  MAX_DD_PCT="20", DD_GUARD_MARGIN="0", HL_IS_VAULT="1",
                  COIN_PARAMS='{"HYPE":{"size_mult":0.5}}')

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


class Vault:
    """The exchange. equity is the vault's account value, depositors included."""

    def __init__(self, equity=1643.27, price=80.0, fail=False):
        self.eq, self.price, self.fail = equity, price, fail
        self.opened = []

    def get_equity(self):
        if self.fail:
            raise RuntimeError("clearinghouse timeout")
        return self.eq

    def get_price(self, coin):
        return self.price

    def get_positions(self):
        return []

    def set_leverage(self, coin, lev):
        return True

    def market_open(self, coin, is_buy, notional, current_price=None):
        self.opened.append(notional)
        return {"filled": True, "avg_price": self.price, "total_size": notional / self.price}

    def place_stop_market(self, *a, **k):
        return "oid1"

    def market_close(self, *a, **k):
        return {"filled": True, "avg_price": self.price, "total_size": 1.0}

    def cancel_order(self, *a, **k):
        return True

    def get_user_fills(self, start_ms=None):
        return []


def fresh(name, client=None, ledger=1810.21):
    db = DB(os.path.join(TMP, name))
    # inception == equity keeps _ensure_baseline's ledger resync a no-op, so the
    # fixture's number is the one under test.
    db.set_account(equity=ledger, daily_baseline=ledger, inception=ledger,
                   inception_ts="2026-07-01T00:00:00+00:00")
    pm = PositionManager(client or Vault(), db)
    return db, pm


print("\n1. config defaults")
check("a vault sizes from the venue by default", config.EQUITY_SOURCE == "venue")
check("  and measures drawdown on the index", config.DD_BASIS == "index")

print("\n2. sizing from the vault, not the ledger")
db, pm = fresh("size.db")
check("ledger still reports its own flow-neutral equity", abs(pm.equity() - 1810.21) < 0.01)
check("sizing uses the vault's real value instead", abs(pm.sizing_equity() - 1643.27) < 0.01)
pm.maybe_enter("HYPE", "long", 80.0)
check("  so a HYPE entry is 0.8 x 0.5 x $1,643.27", abs(pm.client.opened[0] - 657.31) < 0.5)

pm.client.eq = 5000.0                      # a depositor arrives, nobody tells the bot
db.delete_position("HYPE")
pm.maybe_enter("HYPE", "long", 80.0)
check("a deposit is used immediately, with no CAPITAL_FLOW", abs(pm.client.opened[1] - 2000.0) < 1)
pm.client.eq = 900.0                       # and a withdrawal
db.delete_position("HYPE")
pm.maybe_enter("HYPE", "long", 80.0)
check("a withdrawal shrinks the next position too", abs(pm.client.opened[2] - 360.0) < 1)
check("free margin follows the venue as well",
      abs(pm.free_margin() - (900.0 - 180.0)) < 1)

db2, pm2 = fresh("fallback.db", Vault(fail=True))
check("a failed venue read falls back to the ledger, never to zero",
      abs(pm2.sizing_equity() - 1810.21) < 0.01)

print("\n3. the index ignores flows")
db3, pm3 = fresh("idx.db")
em = ExitManager(pm3.client, db3, pm3)
check("starts at 1.0", db3.dd_index() == (1.0, 1.0))


def close(pm, em, db, pnl_frac, coin="SOL"):
    """Close a trade worth pnl_frac of the CURRENT sizing equity."""
    eq = pm.sizing_equity()
    qty = 1.0
    entry = 100.0
    exit_ = entry + eq * pnl_frac / qty
    db.insert_position(dict(coin=coin, side="long", entry=entry, qty=qty, notional=entry,
                            margin=entry / 2, peak=entry, hard_stop=90.0, hard_stop_id="x",
                            trail_active=0, trail_stop=None, intended_entry=entry,
                            opened_at="2026-09-01T00:00:00+00:00"))
    em.close_position(db.get_position(coin), fill_px=exit_, reason="TRAIL")


close(pm3, em, db3, 0.10)
check("a +10% trade lifts the index to 1.10", abs(db3.dd_index()[0] - 1.10) < 1e-6)
pm3.client.eq = 8000.0                      # a large depositor joins
check("  a deposit does not move the index", abs(db3.dd_index()[0] - 1.10) < 1e-6)
close(pm3, em, db3, -0.05)
check("a -5% trade takes it to 1.045", abs(db3.dd_index()[0] - 1.045) < 1e-6)
check("  the peak is remembered", abs(db3.dd_index()[1] - 1.10) < 1e-6)

print("\n4. the guard measures the index")
db4, pm4 = fresh("guard.db")
em4 = ExitManager(pm4.client, db4, pm4)
config.DD_TYPE = "trailing"
close(pm4, em4, db4, 0.25)                  # index 1.25, peak 1.25
pm4.check_max_dd()
check("trailing: no halt at the high", not db4.max_dd_halt())
close(pm4, em4, db4, -0.15)                 # 1.0625, 15% below the peak
pm4.check_max_dd()
check("  none at 15% below the peak either", not db4.max_dd_halt())
close(pm4, em4, db4, -0.07)                 # 0.988, 20.9% below
pm4.check_max_dd()
check("  halts past 20% below the peak (a real high-water mark)", db4.max_dd_halt())

db5, pm5 = fresh("static.db")
em5 = ExitManager(pm5.client, db5, pm5)
config.DD_TYPE = "static"
close(pm5, em5, db5, -0.19)
pm5.check_max_dd()
check("static: no halt at 19% below the start", not db5.max_dd_halt())
close(pm5, em5, db5, -0.02)
pm5.check_max_dd()
check("  halts past 20%", db5.max_dd_halt())

db6, pm6 = fresh("upnl.db")
config.DD_TYPE = "static"
db6.set_account(dd_index=0.85, dd_index_peak=1.2)
db6.insert_position(dict(coin="SOL", side="long", entry=100.0, qty=4.0, notional=400.0,
                         margin=200.0, peak=100.0, hard_stop=90.0, hard_stop_id="x",
                         trail_active=0, trail_stop=None, intended_entry=100.0,
                         opened_at="2026-09-01T00:00:00+00:00"))
pm6.client.price = 96.0                     # -$16 open, on ~$1,643 of capital
pm6.check_max_dd()
check("an open loss counts toward the index too", db6.dd_index()[0] == 0.85 and not db6.max_dd_halt())
pm6.client.price = 60.0                     # -$160 open: 0.85 x (1 - 0.107) = 0.759
pm6.check_max_dd()
check("  and a big enough one halts without waiting for the close", db6.max_dd_halt())

print("\n5. a fixed-capital account is untouched")
config.EQUITY_SOURCE, config.DD_BASIS, config.DD_TYPE = "ledger", "equity", "static"
db7, pm7 = fresh("ledger.db")
check("sizing uses the ledger", abs(pm7.sizing_equity() - 1810.21) < 0.01)
pm7.maybe_enter("HYPE", "long", 80.0)
check("  entry sized off it", abs(pm7.client.opened[0] - 724.08) < 0.5)
db7.delete_position("HYPE")
# a real -$510 in the ledger: 28% below the $1,810.21 anchor
db7.book_trade(dict(coin="SOL", side="long", entry=100.0, exit=49.0, qty=10.0, fee=0.0,
                    exit_reason="HARD_STOP", opened_at="2026-09-01T00:00:00+00:00",
                    closed_at="2026-09-02T00:00:00+00:00"))
pm7.ensure_seeded()
pm7.check_max_dd()
check("the dollar guard still fires on its own anchor", db7.max_dd_halt())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
