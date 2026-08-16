"""
Offline validation for bulk_client.py - no network. REAL bulk-keychain signing,
mocked HTTP session, real PositionManager/ExitManager for the lifecycle.
"""
import os, sys, shutil

os.environ["STATE_PATH"] = "/tmp/bulk_test"
os.environ["PAPER"] = "0"
os.environ["DRY_RUN"] = "0"
os.environ.setdefault("COINS", "SOL,ETH,ZEC,SUI,FARTCOIN,BNB,DOGE")

import bulk_keychain as bk
_kp = bk.Keypair()
os.environ["BULK_PRIVATE_KEY"] = _kp.to_base58()
os.environ["BULK_ACCOUNT_ADDRESS"] = _kp.pubkey

import bulk_client as bc

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}")


class FakeResp:
    def __init__(self, body, status=200): self._b, self.status_code = body, status
    def json(self): return self._b
    def raise_for_status(self):
        if self.status_code >= 400: raise Exception(f"HTTP {self.status_code}")

class FakeSession:
    def __init__(self):
        self.posts = []
        self.account_fail = False
        self.positions = []
        self.prices = [{"symbol": s, "markPrice": p} for s, p in
                       [("SOL-USD","200.0"),("ETH-USD","3000.0"),("ZEC-USD","50.0"),
                        ("SUI-USD","3.0"),("FARTCOIN-USD","1.2"),("BNB-USD","600.0"),
                        ("DOGE-USD","0.2")]]
    def get(self, url, params=None, timeout=None, headers=None):
        if url.endswith("/exchangeInfo"):
            return FakeResp([
                {"symbol":"SOL-USD","lotSize":0.0001,"tickSize":0.001,"minNotional":50.0},
                {"symbol":"ETH-USD","lotSize":0.0001,"tickSize":0.001,"minNotional":50.0},
                {"symbol":"ZEC-USD","lotSize":0.001,"tickSize":0.01,"minNotional":40.0},
                {"symbol":"SUI-USD","lotSize":0.1,"tickSize":0.0001,"minNotional":40.0},
                {"symbol":"FARTCOIN-USD","lotSize":1,"tickSize":0.0001,"minNotional":25.0},
                {"symbol":"BNB-USD","lotSize":0.001,"tickSize":0.00001,"minNotional":50.0},
                {"symbol":"DOGE-USD","lotSize":1,"tickSize":0.00001,"minNotional":25.0},
            ])
        if url.endswith("/ticker"):
            return FakeResp(self.prices)
        return FakeResp([])
    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append((url, json))
        if url.endswith("/account"):
            if self.account_fail:
                return FakeResp({}, status=503)
            return FakeResp({"data": {"positions": self.positions,
                                      "marginSummary": {"accountValue": "1000.0"}}})
        return FakeResp({"success": True})


print("[1] init + signing")
cli = bc.BulkClient()
fake = FakeSession(); cli._sess = fake
check("init_sdk succeeds", cli.init_sdk())
check("meta loaded (7 symbols)", len(cli.asset_meta) == 7)
check("SOL lists, symbol maps to SOL-USD", cli.lists("SOL") and bc.bulk_symbol("SOL") == "SOL-USD")
check("signing_works (real bulk-keychain)", cli.signing_works())

print("[2] order encodings (real signer, captured POST)")
fake.posts.clear()
res = cli.market_open("SOL", is_buy=True, notional_usd=200.0, current_price=200.0)
url, body = fake.posts[-1]
act = body["actions"][0]
check("market_open hits /order", url.endswith("/order"))
check("encodes as market 'm'", "m" in act)
check("m.b True (buy), r False, c SOL-USD", act["m"]["b"] is True and act["m"]["r"] is False and act["m"]["c"] == "SOL-USD")
check("size floored to lot (1.0)", abs(act["m"]["sz"] - 1.0) < 1e-9)
check("envelope has nonce+signature+account", all(k in body for k in ("nonce","signature","account","signer")))
check("market_open returns filled", res and res["filled"] and res["total_size"] == 1.0)

fake.posts.clear()
cli.market_close("SOL", size=1.0, is_long=True, current_price=210.0)
act = fake.posts[-1][1]["actions"][0]
check("close: m.b False (sell), r True", act["m"]["b"] is False and act["m"]["r"] is True)

fake.posts.clear()
ok = cli.cancel_order("SOL", "AbRyZTwsAprF8XAy88c7diKAGAUbmhAggmAWx24jm8nB")
act = fake.posts[-1][1]["actions"][0]
check("cancel encodes as 'cx'", "cx" in act and act["cx"]["c"] == "SOL-USD")

print("[3] backstop-only stops")
sid = cli.place_stop_market("SOL", True, 1.0, 180.0)
check("place_stop returns sentinel (non-numeric)", sid == bc.BACKSTOP_SENTINEL and not str(sid).isdigit())
check("modify_stop also sentinel", cli.modify_stop("SOL", True, 1.0, sid, 185.0) == bc.BACKSTOP_SENTINEL)

print("[4] reads: prices, positions None/[], equity, fallback")
check("get_price SOL native", cli.get_price("SOL") == 200.0)
fake.positions = [{"symbol":"SOL-USD","size":"5.0","side":"long","entryPx":"199.0"}]
live = cli.get_positions()
check("positions long -> +szi", live and live[0]["szi"] == 5.0 and live[0]["coin"] == "SOL")
fake.positions = [{"symbol":"ETH-USD","size":"2.0","side":"short","entryPx":"3000"}]
check("positions short -> -szi", cli.get_positions()[0]["szi"] == -2.0)
fake.positions = []
check("empty -> [] (trusted)", cli.get_positions() == [])
fake.account_fail = True
check("failed read -> None", cli.get_positions() is None)
fake.account_fail = False
check("get_equity from marginSummary", cli.get_equity() == 1000.0)

# price fallback: break the native feed, inject HL-style marks
cli2 = bc.BulkClient(); cli2._sess = fake; cli2.init_sdk()
cli2.price_path = "/does-not-exist"
cli2.set_price_fallback(lambda extra=None: {"SOL": 201.5, "ETH": 3010.0})
check("price fallback used when native empty", cli2.get_all_prices().get("SOL") == 201.5)

print("[5] integration: entry -> trail -> backstop close via real managers")
shutil.rmtree("/tmp/bulk_test", ignore_errors=True); os.makedirs("/tmp/bulk_test", exist_ok=True)
from db import DB
from position_manager import PositionManager
from exit_manager import ExitManager
db = DB(path="/tmp/bulk_test/lib.db")
db.set_account(equity=1000.0, daily_baseline=1000.0, daily_halt=0,
               inception=1000.0, inception_ts="2026-01-01T00:00:00+00:00", last_reset="2026-01-01")
pm = PositionManager(cli, db); em = ExitManager(cli, db, pm)
fake.positions = []
pos = pm.maybe_enter("SOL", "long", 200.0)
check("entry booked with sentinel stop", pos is not None and pos["hard_stop_id"] == bc.BACKSTOP_SENTINEL)
fake.positions = [{"symbol":"SOL-USD","size":str(pos["qty"]),"side":"long","entryPx":str(pos["entry"])}]
# Elite's exit manager holds the trail one full bar after entry (Pine-parity
# arming delay); age the position past the delay before expecting arming.
from datetime import datetime, timezone, timedelta
_aged = (datetime.now(timezone.utc)
         - timedelta(seconds=config.TRAIL_ARM_DELAY_SEC + 60)).isoformat()
db.update_position("SOL", opened_at=_aged)
em.manage(db.open_positions()[0], 200.0 * 1.02)   # arm trail
check("trail armed", bool(db.open_positions()[0]["trail_active"]))
fake.posts.clear()
em.manage(db.open_positions()[0], 150.0)           # gap through stop -> backstop
check("backstop closed position", not db.has_open_position("SOL"))
check("reduce-only market close sent", any(u.endswith("/order")
      and b["actions"][0].get("m", {}).get("r") is True for u, b in fake.posts))
check("trade booked to ledger", db.trade_count() == 1)

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
