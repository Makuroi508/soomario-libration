"""
Offline validation for pacifica_client.py - no network, no keys, no live orders.
Mirrors the handoff's offline testing pattern: mock the HTTP session, feed
documented response shapes, drive the REAL PositionManager/ExitManager.
"""
import json
import os
import sys

os.environ["STATE_PATH"] = "/tmp/pac_test"
os.environ["PAPER"] = "0"      # exercise the LIVE order path (mocked HTTP)
os.environ["DRY_RUN"] = "0"
os.environ["PACIFICA_ACCOUNT_ADDRESS"] = ""   # filled after keygen below
os.environ.setdefault("COINS", "SOL,ETH,ZEC,SUI,FARTCOIN,KPEPE")

import base58
from solders.keypair import Keypair
import nacl.signing

# a throwaway signer for the test (never used against mainnet)
_kp = Keypair()
os.environ["PACIFICA_PRIVATE_KEY"] = base58.b58encode(bytes(_kp)).decode()
os.environ["PACIFICA_ACCOUNT_ADDRESS"] = str(_kp.pubkey())

import pacifica_client as pc

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}")


# ---- fake HTTP session -------------------------------------------------
class FakeResp:
    def __init__(self, body, status=200):
        self._body = body; self.status_code = status
    def json(self): return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

class FakeSession:
    def __init__(self):
        self.posts = []          # captured (url, json) for assertions
        self.positions = []      # what /positions returns
        self.positions_fail = False
        self.prices = {"SOL": "200.0", "ETH": "3000.0", "ZEC": "50.0",
                       "SUI": "3.0", "FARTCOIN": "1.2", "kPEPE": "0.02"}
    def get(self, url, params=None, timeout=None, headers=None):
        if url.endswith("/info"):
            return FakeResp({"success": True, "data": [
                {"symbol": "SOL", "tick_size": "0.001", "lot_size": "0.01",
                 "max_leverage": 50, "min_order_size": "10"},
                {"symbol": "ETH", "tick_size": "0.1", "lot_size": "0.0001",
                 "max_leverage": 50, "min_order_size": "10"},
                {"symbol": "ZEC", "tick_size": "0.01", "lot_size": "0.001",
                 "max_leverage": 40, "min_order_size": "10"},
                {"symbol": "SUI", "tick_size": "0.0001", "lot_size": "0.1",
                 "max_leverage": 40, "min_order_size": "10"},
                {"symbol": "FARTCOIN", "tick_size": "0.0001", "lot_size": "1",
                 "max_leverage": 25, "min_order_size": "10"},
                {"symbol": "kPEPE", "tick_size": "0.000001", "lot_size": "1000",
                 "max_leverage": 20, "min_order_size": "10"},
            ]})
        if url.endswith("/info/prices"):
            return FakeResp({"success": True, "data": [
                {"symbol": k, "mark": v} for k, v in self.prices.items()]})
        if url.endswith("/positions"):
            if self.positions_fail:
                return FakeResp({}, status=503)
            return FakeResp({"success": True, "data": self.positions})
        if url.endswith("/account"):
            return FakeResp({"success": True, "data": {"account_equity": "1000.0"}})
        if url.endswith("/trades/history"):   # moved from /account/trades (404 since ~Aug 2026)
            return FakeResp({"success": True, "data": [
                {"symbol": "SOL", "side": "close_long", "price": "210.0",
                 "amount": "5.0", "fee": "0.1", "created_at": 111}]})
        return FakeResp({"success": True, "data": []})
    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append((url, json))
        return FakeResp({"success": True, "data": {"order_id": 12345}})


# ---- 1. signing correctness -------------------------------------------
print("[1] signing")
cli = pc.PacificaClient()
fake = FakeSession()
cli._sess = fake
ok = cli.init_sdk()
check("init_sdk succeeds", ok)
check("market meta loaded (6 symbols)", len(cli.asset_meta) == 6)
check("universe prune: SOL lists", cli.lists("SOL"))
check("kPEPE maps + lists", cli.lists("KPEPE") and pc.pac_symbol("KPEPE") == "kPEPE")

# verify the signed message matches the reference construction + verifies
hdr = {"timestamp": 1700000000000, "expiry_window": 5000, "type": "create_market_order"}
payload = {"symbol": "SOL", "reduce_only": False, "amount": "1.0", "side": "bid",
           "slippage_percent": "0.5", "client_order_id": "fixed"}
msg = pc._prepare_message(hdr, payload)
expected = ('{"data":{"amount":"1.0","client_order_id":"fixed","reduce_only":false,'
            '"side":"bid","slippage_percent":"0.5","symbol":"SOL"},'
            '"expiry_window":5000,"timestamp":1700000000000,"type":"create_market_order"}')
check("canonical message exact", msg == expected)
sig = cli._keypair.sign_message(msg.encode())
vk = nacl.signing.VerifyKey(bytes(_kp.pubkey()))
try:
    vk.verify(msg.encode(), bytes(sig)); verified = True
except Exception:
    verified = False
check("signature verifies under account pubkey", verified)

# ---- 2. read parsing ---------------------------------------------------
print("[2] reads")
check("get_price SOL", cli.get_price("SOL") == 200.0)
check("get_all_prices count", len(cli.get_all_prices()) == 6)
check("get_equity", cli.get_equity() == 1000.0)

fake.positions = [{"symbol": "SOL", "side": "bid", "amount": "5.0",
                   "entry_price": "199.0", "isolated": True}]
live = cli.get_positions()
check("positions long -> +szi", live and live[0]["szi"] == 5.0 and live[0]["coin"] == "SOL")
fake.positions = [{"symbol": "ETH", "side": "ask", "amount": "2.0", "entry_price": "3000"}]
check("positions short -> -szi", cli.get_positions()[0]["szi"] == -2.0)
fake.positions = []
check("empty read -> [] (trusted)", cli.get_positions() == [])
fake.positions_fail = True
check("failed read -> None (never [])", cli.get_positions() is None)
fake.positions_fail = False

fills = cli.get_user_fills(start_ms=0)
check("fills map close_long -> 'Close Long'", fills and fills[0]["dir"] == "Close Long"
      and fills[0]["px"] == 210.0 and fills[0]["sz"] == 5.0)

# ---- 3. order payload construction ------------------------------------
print("[3] order payloads")
fake.posts.clear()
res = cli.market_open("SOL", is_buy=True, notional_usd=200.0, current_price=200.0)
url, body = fake.posts[-1]
check("market_open hits create_market", url.endswith("/orders/create_market"))
check("market_open side=bid reduce_only=False", body["side"] == "bid" and body["reduce_only"] is False)
check("market_open amount rounded to lot 0.01 (1.0)", body["amount"] == "1")
check("market_open envelope has account+signature", "account" in body and "signature" in body)
check("market_open filled result", res and res["filled"] and res["total_size"] == 1.0)

fake.posts.clear()
cli.market_close("SOL", size=1.0, is_long=True, current_price=210.0)
_, body = fake.posts[-1]
check("market_close side=ask reduce_only=True", body["side"] == "ask" and body["reduce_only"] is True)

fake.posts.clear()
sid = cli.place_stop_market("SOL", is_long=True, size=1.0, stop_px=179.5)
url, body = fake.posts[-1]
check("stop hits positions/tpsl", url.endswith("/positions/tpsl"))
check("stop closing side=ask for long", body["side"] == "ask")
check("stop sets stop_loss.stop_price", body["stop_loss"]["stop_price"] == "179.5")
check("place_stop returns non-numeric id (backstop-safe)", sid and not str(sid).isdigit())

fake.posts.clear()
sid2 = cli.modify_stop("SOL", is_long=True, size=1.0, old_id=sid, new_stop=185.0)
_, body = fake.posts[-1]
check("modify_stop replaces via tpsl", float(body["stop_loss"]["stop_price"]) == 185.0 and sid2 != sid)

fake.posts.clear()
cli.set_leverage("SOL", 2)
url, body = fake.posts[-1]
check("set_leverage payload", url.endswith("/account/leverage") and body["leverage"] == 2)

# ---- 4. full entry -> trail -> close via REAL managers ----------------
print("[4] integration through PositionManager / ExitManager")
import shutil
shutil.rmtree("/tmp/pac_test", ignore_errors=True)
os.makedirs("/tmp/pac_test", exist_ok=True)
from db import DB
from position_manager import PositionManager
from exit_manager import ExitManager
import config

db = DB(path="/tmp/pac_test/lib.db")
db.set_account(equity=1000.0, daily_baseline=1000.0, daily_halt=0,
               inception=1000.0, inception_ts="2026-01-01T00:00:00+00:00",
               last_reset="2026-01-01")
pm = PositionManager(cli, db)
em = ExitManager(cli, db, pm)

fake.positions = []
pos = pm.maybe_enter("SOL", "long", 200.0)
check("entry booked a position", pos is not None and db.has_open_position("SOL"))
# reflect the open on the exchange for subsequent management
fake.positions = [{"symbol": "SOL", "side": "bid", "amount": str(pos["qty"]),
                   "entry_price": str(pos["entry"])}]

# Elite's exit manager holds the trail for one full bar after entry (the
# Pine-parity arming delay -- immediate arming lost 12/12 in the elite A/B).
# A freshly opened position must therefore NOT arm, even +2% in profit:
p = db.open_positions()[0]
em.manage(p, 200.0 * 1.02)
p = db.open_positions()[0]
check("fresh position stays unarmed (arm delay)", not p["trail_active"])

# Age the position past the delay, then the same tick arms and ratchets.
from datetime import datetime, timezone, timedelta
aged = (datetime.now(timezone.utc)
        - timedelta(seconds=config.TRAIL_ARM_DELAY_SEC + 60)).isoformat()
db.update_position("SOL", opened_at=aged)
p = db.open_positions()[0]
em.manage(p, 200.0 * 1.02)     # +2% -> arms trail, ratchets the native stop
p = db.open_positions()[0]
check("trail armed after +2%", bool(p["trail_active"]))
check("native stop moved up (trail_stop set)", p["trail_stop"] is not None)

# price collapses through the stop while still 'open' -> software backstop closes
fake.posts.clear()
em.manage(db.open_positions()[0], 150.0)
closed = not db.has_open_position("SOL")
check("backstop closed the position", closed)
check("a reduce-only market_close was sent", any(u.endswith("/orders/create_market")
      and b.get("reduce_only") is True for u, b in fake.posts))
check("realized PnL booked to ledger", db.trade_count() == 1)

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
