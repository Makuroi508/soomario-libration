"""
Offline validation for bulk_client.py (MAINNET + AGENT MODE) - no network.
REAL bulk-keychain signing (agent key signing for a master account), mocked
HTTP session with the v1.0.19 response shapes, real PositionManager/ExitManager
for the lifecycle with NATIVE stops.
"""
import os, sys, shutil

os.environ["STATE_PATH"] = "/tmp/bulk_test"
os.environ["PAPER"] = "0"
os.environ["DRY_RUN"] = "0"
os.environ.setdefault("COINS", "SOL,ETH,ZEC,SUI,FARTCOIN,BNB,DOGE")
os.environ["BULK_NETWORK"] = "mainnet"
os.environ["BULK_AGENT"] = "1"

import base58
import nacl.signing
import bulk_keychain as bk

_master = bk.Keypair()                    # the funded wallet (key stays OFFLINE in prod)
_agent = bk.Keypair()                     # the key the bot actually holds
MASTER = str(_master.pubkey)
AGENT = str(_agent.pubkey)
os.environ["BULK_ACCOUNT_ADDRESS"] = MASTER
os.environ["BULK_PRIVATE_KEY"] = _agent.to_base58()

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
        self.account_shape = None       # override raw /account response
        self.positions = []
        self.n = 0
        self.marks = {"SOL-USD": 200.0, "ETH-USD": 3000.0, "ZEC-USD": 50.0, "SUI-USD": 3.0,
                      "FARTCOIN-USD": 1.2, "BNB-USD": 600.0, "DOGE-USD": 0.2}
    def get(self, url, params=None, timeout=None, headers=None):
        if url.endswith("/exchangeInfo"):
            return FakeResp([
                {"symbol": s, "lotSize": 1e-08, "tickSize": 1e-08, "minNotional": 50.0,
                 "maxLeverage": 10, "orderTypes": ["LIMIT", "MARKET", "STOP", "STOP_LIMIT", "TRIGGER"]}
                for s in self.marks])
        for s, px in self.marks.items():
            if url.endswith(f"/ticker/{s}"):
                return FakeResp({"symbol": s, "markPrice": px, "lastPrice": px + 0.01})
        return FakeResp({"error": "not found"}, status=404)
    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append((url, json))
        if url.endswith("/account"):
            if self.account_fail:
                return FakeResp({}, status=503)
            if self.account_shape is not None:
                return FakeResp(self.account_shape)
            return FakeResp([{"fullAccount": {
                "kind": "MasterEOA", "parent": None,
                "margin": {"totalMargin": 600.001, "availableMargin": 600.001, "marginUsed": 0.0},
                "positions": self.positions, "openOrders": []}}])
        self.n += 1
        # real Bulk order ids are base58 hashes; the library validates base58 on cancel
        oid = base58.b58encode(bytes([self.n]) + os.urandom(31)).decode()
        return FakeResp({"status": "ok", "statuses": [{"resting": {"oid": oid}}]})


print("[1] init + agent signing (mainnet domain)")
cli = bc.BulkClient()
fake = FakeSession(); cli._sess = fake
check("init_sdk succeeds", cli.init_sdk())
check("meta loaded (7 symbols)", len(cli.asset_meta) == 7)
check("SOL lists, symbol maps to SOL-USD", cli.lists("SOL") and bc.bulk_symbol("SOL") == "SOL-USD")
check("native STOP capability detected", cli._stop_supported("SOL"))
check("agent mode: account is MASTER, signer is AGENT",
      cli.account_address == MASTER and cli._signer_pub == AGENT and cli.is_agent)
check("signing_works (real bulk-keychain, agent for master)", cli.signing_works())

order = {"type": "order", "symbol": "SOL-USD", "is_buy": True, "price": 0, "size": 1.0,
         "reduce_only": False, "iso": False, "order_type": {"type": "market", "is_market": True}}
prepared = bk.prepare_order(order, cli.network, MASTER, signer=AGENT, nonce=None)
signed = cli._signer.sign_prepared(prepared)
msg = bytes(prepared["message_bytes"])
check("preimage embeds MASTER pubkey (not agent)",
      base58.b58decode(MASTER) in msg and base58.b58decode(AGENT) not in msg)
check("preimage ends with mainnet domain byte (1)", msg[-1] == 1)
try:
    nacl.signing.VerifyKey(base58.b58decode(AGENT)).verify(msg, base58.b58decode(signed["signature"]))
    ok = True
except Exception:
    ok = False
check("signature verifies under AGENT pubkey", ok)
check("signed envelope: account=master signer=agent",
      signed["account"] == MASTER and signed["signer"] == AGENT)

print("[2] order encodings (captured POST)")
fake.posts.clear()
res = cli.market_open("SOL", is_buy=True, notional_usd=200.0, current_price=200.0)
url, body = fake.posts[-1]
act = body["actions"][0]
check("market_open hits /order", url.endswith("/order"))
check("encodes as market 'm'", "m" in act)
check("m.b True (buy), r False, c SOL-USD", act["m"]["b"] is True and act["m"]["r"] is False and act["m"]["c"] == "SOL-USD")
check("size floored to lot (1.0)", abs(act["m"]["sz"] - 1.0) < 1e-9)
check("envelope account=MASTER signer=AGENT + nonce/signature",
      body["account"] == MASTER and body["signer"] == AGENT and body["nonce"] and body["signature"])
check("market_open returns filled", res and res["filled"] and res["total_size"] == 1.0)

fake.posts.clear()
cli.market_close("SOL", size=1.0, is_long=True, current_price=210.0)
act = fake.posts[0][1]["actions"][0]
check("close: m.b False (sell), r True", act["m"]["b"] is False and act["m"]["r"] is True)

fake.posts.clear()
ok = cli.cancel_order("SOL", "AbRyZTwsAprF8XAy88c7diKAGAUbmhAggmAWx24jm8nB")
act = fake.posts[-1][1]["actions"][0]
check("cancel encodes as 'cx'", ok and "cx" in act and act["cx"]["c"] == "SOL-USD")

print("[3] NATIVE stops (mainnet) with backstop fallback")
fake.posts.clear()
sid = cli.place_stop_market("SOL", True, 1.0, 180.0)
act = fake.posts[-1][1]["actions"][0]
check("stop encodes as 'st' closing side (sell for long)",
      "st" in act and act["st"]["c"] == "SOL-USD" and act["st"]["d"] is False)
check("stop trigger price + size", abs(act["st"]["tr"] - 180.0) < 1e-9 and abs(act["st"]["sz"] - 1.0) < 1e-9)
check("returns non-numeric id (exit_manager won't cancel it)", sid and sid != bc.BACKSTOP_SENTINEL and not str(sid).isdigit())
fake.posts.clear()
sid2 = cli.modify_stop("SOL", True, 1.0, sid, 185.0)
acts = [p[1]["actions"][0] for p in fake.posts]
check("modify = cancel old THEN place new (never two live stops)",
      len(acts) == 2 and "cx" in acts[0] and acts[0]["cx"]["oid"] == sid and "st" in acts[1]
      and abs(acts[1]["st"]["tr"] - 185.0) < 1e-9)
check("modify returns a new id", sid2 and sid2 != sid and sid2 != bc.BACKSTOP_SENTINEL)
fake.posts.clear()
cli.market_close("SOL", size=1.0, is_long=True, current_price=190.0)
acts = [p[1]["actions"][0] for p in fake.posts]
check("close pulls the resting native stop (cx after reduce-only close)",
      len(acts) == 2 and "m" in acts[0] and "cx" in acts[1] and acts[1]["cx"]["oid"] == sid2)
cli.native_stops = False
check("native_stops=0 -> backstop sentinel", cli.place_stop_market("SOL", True, 1.0, 180.0) == bc.BACKSTOP_SENTINEL)
cli.native_stops = True

print("[4] reads: v1.0.19 fullAccount shape, None/[] discipline, prices")
check("get_price SOL from /ticker/SOL-USD markPrice", cli.get_price("SOL") == 200.0)
fake.positions = [{"symbol": "SOL-USD", "size": 5.0, "price": 199.0, "fairPrice": 200.0}]
live = cli.get_positions()
check("positions long (size +) -> +szi, entry from price", live and live[0]["szi"] == 5.0
      and live[0]["coin"] == "SOL" and live[0]["entryPx"] == 199.0)
fake.positions = [{"symbol": "ETH-USD", "size": -2.0, "price": 3000.0}]
check("positions short (size -) -> -szi", cli.get_positions()[0]["szi"] == -2.0)
fake.positions = []
check("confirmed empty -> [] (trusted)", cli.get_positions() == [])
fake.account_fail = True
check("failed read (503) -> None", cli.get_positions() is None)
fake.account_fail = False
fake.account_shape = {"error": "Account Not Found"}
check("unrecognized/404-style body -> None (NEVER [])", cli.get_positions() is None)
fake.account_shape = None
check("get_equity from margin.totalMargin", abs(cli.get_equity() - 600.001) < 1e-9)
cli._px_cache.clear()
cli2 = bc.BulkClient(); cli2._sess = fake; cli2.init_sdk()
cli2.price_path = "/nope"
cli2.set_price_fallback(lambda extra=None: {"SOL": 201.5, "ETH": 3010.0})
check("price fallback (HL) fills coins the ticker can't", cli2.get_all_prices(extra=["SOL"]).get("SOL") == 201.5)

print("[5] integration: entry -> native stop -> trail -> backstop close via real managers")
shutil.rmtree("/tmp/bulk_test", ignore_errors=True); os.makedirs("/tmp/bulk_test", exist_ok=True)
from db import DB
from position_manager import PositionManager
from exit_manager import ExitManager
db = DB(path="/tmp/bulk_test/lib.db")
db.set_account(equity=1000.0, daily_baseline=1000.0, daily_halt=0,
               inception=1000.0, inception_ts="2026-01-01T00:00:00+00:00", last_reset="2026-01-01")
pm = PositionManager(cli, db); em = ExitManager(cli, db, pm)
fake.positions = []; cli._px_cache.clear()
pos = pm.maybe_enter("SOL", "long", 200.0)
check("entry booked with a NATIVE stop id", pos is not None and pos["hard_stop_id"]
      and pos["hard_stop_id"] != bc.BACKSTOP_SENTINEL and not str(pos["hard_stop_id"]).isdigit())
fake.positions = [{"symbol": "SOL-USD", "size": pos["qty"], "price": pos["entry"]}]
# Pine-parity arming delay (newer base): a fresh position must not arm even at
# +2%; age it past the delay first. Older base (no delay) arms immediately.
import config
from datetime import datetime, timezone, timedelta
delay = float(getattr(config, "TRAIL_ARM_DELAY_SEC", 0) or 0)
if delay > 0:
    em.manage(db.open_positions()[0], 200.0 * 1.02)
    if db.open_positions()[0]["trail_active"]:
        raise SystemExit("fresh position armed despite arm delay")
    aged = (datetime.now(timezone.utc) - timedelta(seconds=delay + 60)).isoformat()
    db.update_position("SOL", opened_at=aged)
fake.posts.clear()
em.manage(db.open_positions()[0], 200.0 * 1.02)   # arm trail -> stop ratchets (cx + st)
p = db.open_positions()[0]
check("trail armed and native stop moved", bool(p["trail_active"]) and p["trail_stop"] is not None
      and any("st" in x[1]["actions"][0] for x in fake.posts))
fake.posts.clear()
em.manage(db.open_positions()[0], 150.0)           # gap through stop -> software backstop
check("backstop closed position", not db.has_open_position("SOL"))
check("reduce-only market close sent", any(x[1]["actions"][0].get("m", {}).get("r") is True for x in fake.posts))
check("trade booked to ledger", db.trade_count() == 1)

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
