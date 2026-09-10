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


def orders(fake):
    """The /order POSTs only. market_open now also reads /account to confirm the
    fill on-venue, so posts[-1] is no longer necessarily the order."""
    return [(u, b) for u, b in fake.posts if u.endswith("/order")]


def acts_of(fake):
    return [a for _, b in orders(fake) for a in (b.get("actions") or [])]


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
        self.open_orders = []
        self.fills = True               # does an accepted order actually cross?
        self.n = 0
        self.oracle_offset = 0.0
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
                return FakeResp({"symbol": s, "markPrice": px, "oraclePrice": px + self.oracle_offset,
                                 "lastPrice": px + 0.01})
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
                "positions": self.positions, "openOrders": self.open_orders}}])
        self._apply(json)
        self.n += 1
        # real Bulk order ids are base58 hashes; the library validates base58 on cancel
        oid = base58.b58encode(bytes([self.n]) + os.urandom(31)).decode()
        return FakeResp({"status": "ok", "statuses": [{"resting": {"oid": oid}}]})

    def _apply(self, body):
        """Move the venue's position book the way a real fill would. market_open
        now CONFIRMS the position on the venue before reporting a fill, so an
        acceptance that never moves the book is (correctly) not a fill."""
        if not self.fills:
            return
        for act in (body or {}).get("actions") or []:
            leg = act.get("m") or act.get("l")
            if not leg:
                continue
            sym = leg.get("c")
            sz = float(leg.get("sz") or 0)
            px = float(leg.get("p") or 0) or self.marks.get(sym, 0)
            self.positions = [q for q in self.positions if q.get("symbol") != sym]
            if not leg.get("r"):
                self.positions.append({"symbol": sym, "size": sz if leg.get("b") else -sz,
                                       "price": px})


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
url, body = orders(fake)[-1]
act = body["actions"][0]
check("market_open hits /order", url.endswith("/order"))
check("encodes as market 'm'", "m" in act)
check("m.b True (buy), r False, c SOL-USD", act["m"]["b"] is True and act["m"]["r"] is False and act["m"]["c"] == "SOL-USD")
check("size floored to lot (1.0)", abs(act["m"]["sz"] - 1.0) < 1e-9)
check("envelope account=MASTER signer=AGENT + nonce/signature",
      body["account"] == MASTER and body["signer"] == AGENT and body["nonce"] and body["signature"])
check("market_open returns filled", res and res["filled"] and res["total_size"] == 1.0)
cli.exec_slip_pct = 1.0; fake.posts.clear()
cli.market_open("SOL", is_buy=True, notional_usd=200.0, current_price=200.0)
act_l = acts_of(fake)[-1]
check("BULK_EXEC_SLIP_PCT>0 -> IOC LIMIT entry (bounded sweep), not market", "l" in act_l and "m" not in act_l)
cli.exec_slip_pct = 0.0

fake.posts.clear()
cli.market_close("SOL", size=1.0, is_long=True, current_price=210.0)
act = acts_of(fake)[0]
check("close: m.b False (sell), r True", act["m"]["b"] is False and act["m"]["r"] is True)

fake.posts.clear()
ok = cli.cancel_order("SOL", "AbRyZTwsAprF8XAy88c7diKAGAUbmhAggmAWx24jm8nB")
act = acts_of(fake)[-1]
check("cancel encodes as 'cx'", ok and "cx" in act and act["cx"]["c"] == "SOL-USD")

print("[3] NATIVE stops (mainnet) with backstop fallback")
fake.posts.clear()
sid = cli.place_stop_market("SOL", True, 1.0, 180.0)
act = acts_of(fake)[-1]
check("stop encodes as 'st' closing side (sell for long)",
      "st" in act and act["st"]["c"] == "SOL-USD" and act["st"]["d"] is False)
check("stop trigger price + size", abs(act["st"]["tr"] - 180.0) < 1e-9 and abs(act["st"]["sz"] - 1.0) < 1e-9)
check("stop-limit cap: lim = trigger*(1-1.5%) on a long close",
      abs((act["st"].get("lim") or 0) - 180.0 * (1 - 0.015)) < 1e-6)
fake.posts.clear(); cli.place_stop_market("SOL", False, 1.0, 220.0)
act_s = acts_of(fake)[-1]
check("stop-limit cap: lim = trigger*(1+1.5%) on a short close",
      act_s["st"]["d"] is True and abs((act_s["st"].get("lim") or 0) - 220.0 * 1.015) < 1e-6)
cli.stop_slip_pct = 0.0
fake.posts.clear(); cli.place_stop_market("SOL", True, 1.0, 180.0)
_lim = acts_of(fake)[-1]["st"].get("lim")
check("BULK_STOP_SLIP_PCT=0 -> pure market stop (no limit)", _lim is None or _lim != _lim)
cli.stop_slip_pct = 1.5
check("returns non-numeric id (exit_manager won't cancel it)", sid and sid != bc.BACKSTOP_SENTINEL and not str(sid).isdigit())
fake.posts.clear()
sid2 = cli.modify_stop("SOL", True, 1.0, sid, 185.0)
acts = acts_of(fake)
check("modify = cancel old THEN place new (never two live stops)",
      len(acts) == 2 and "cx" in acts[0] and acts[0]["cx"]["oid"] == sid and "st" in acts[1]
      and abs(acts[1]["st"]["tr"] - 185.0) < 1e-9)
check("modify returns a new id", sid2 and sid2 != sid and sid2 != bc.BACKSTOP_SENTINEL)
fake.posts.clear()
cli.market_close("SOL", size=1.0, is_long=True, current_price=190.0)
acts = acts_of(fake)
check("close pulls the resting native stop (cx after reduce-only close)",
      len(acts) == 2 and "m" in acts[0] and "cx" in acts[1] and acts[1]["cx"]["oid"] == sid2)
cli.native_stops = False
check("native_stops=0 -> backstop sentinel", cli.place_stop_market("SOL", True, 1.0, 180.0) == bc.BACKSTOP_SENTINEL)
cli.native_stops = True

print("[4] reads: v1.0.19 fullAccount shape, None/[] discipline, prices")
check("get_price SOL from /ticker/SOL-USD", cli.get_price("SOL") == 200.0)
cli._px_cache.clear(); fake.oracle_offset = -1.0
check("marks prefer oraclePrice (BULK_MARK_SOURCE=oracle, default)", cli.get_price("SOL") == 199.0)
cli.mark_source = "mark"; cli._px_cache.clear()
check("BULK_MARK_SOURCE=mark -> markPrice", cli.get_price("SOL") == 200.0)
cli.mark_source = "oracle"; fake.oracle_offset = 0.0; cli._px_cache.clear()
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
      and any("st" in a for a in acts_of(fake)))
fake.posts.clear()
em.manage(db.open_positions()[0], 150.0)           # gap through stop -> software backstop
check("backstop closed position", not db.has_open_position("SOL"))
check("reduce-only market close sent", any(a.get("m", {}).get("r") is True for a in acts_of(fake)))
check("trade booked to ledger", db.trade_count() == 1)

print("[6] acceptance is not a fill; triggers never outlive the position")
# 10 Sep 2026: two Bulk entries were ACCEPTED, never crossed, and were booked as
# filled. Each wrote a position the venue did not have and parked a protective
# stop on a flat account; reconcile then voided one (DOGE) and booked a
# fabricated close for the other (SOL), leaving both conditional orders resting.
fake.fills = False; fake.positions = []; cli._px_cache.clear()
cli.fill_confirm_sec = 1.0
check("accepted-but-unfilled entry is NOT reported as a fill",
      cli.market_open("SOL", is_buy=False, notional_usd=200.0, current_price=200.0) is None)
check("miss reason names the venue truth", "no position on venue" in (cli.last_open_error or ""))
fake.fills = True
res = cli.market_open("SOL", is_buy=False, notional_usd=200.0, current_price=200.0)
check("a real fill still books, at the VENUE's entry and size",
      bool(res and res["filled"]) and res["avg_price"] == 200.0
      and abs(res["total_size"] - 1.0) < 1e-9)
fake.account_fail = True
check("unreadable account stays optimistic (never drop a live position)",
      bool((cli.market_open("SOL", is_buy=False, notional_usd=200.0,
                            current_price=200.0) or {}).get("filled")))
fake.account_fail = False
fake.positions = []

# Bulk keeps a reduce-only stop resting after its position is gone, so anything
# that drops a position must pull it, and anything already stranded (by a crash,
# a redeploy, or the pre-fix paths) must be swept off the book.
check("client declares that its triggers outlive the position",
      cli.stops_outlive_position is True)
fake.open_orders = [
    {"symbol": "SOL-USD", "orderId": "RPHANSTPz", "triggerPrice": 220.0, "reduceOnly": True},
    {"symbol": "ETH-USD", "orderId": "HELDSTPz", "triggerPrice": 2700.0, "reduceOnly": True},
    {"symbol": "ZEC-USD", "orderId": "PLANLMTz", "price": 45.0},
]
cli._last_sweep = 0.0; fake.posts.clear()
n = cli.sweep_orphan_stops(keep_coins={"ETH"})
def _cancels(f):
    return [a["cx"]["oid"] for a in acts_of(f) if "cx" in a]

_cx = _cancels(fake)
check("sweeps the stranded trigger on a flat coin", n == 1 and _cx == ["RPHANSTPz"])
check("leaves a coin the book still holds, and a non-protective order, alone",
      "HELDSTPz" not in _cx and "PLANLMTz" not in _cx)
cli._last_sweep = 0.0; fake.posts.clear()
fake.positions = [{"symbol": "SOL-USD", "size": -1.0, "price": 200.0}]
cli.sweep_orphan_stops(set())
check("a coin the VENUE still holds keeps its trigger",
      "RPHANSTPz" not in _cancels(fake))
fake.open_orders = []; fake.positions = []

db.insert_position(dict(coin="SOL", side="short", entry=200.0, qty=1.0, notional=200.0,
                        margin=100.0, peak=200.0, hard_stop=220.0, hard_stop_id="STPSLz",
                        trail_active=0, trail_stop=None, intended_entry=200.0,
                        opened_at="2026-09-10T08:00:36+00:00"))
fake.posts.clear()
em.close_position(dict(db.open_positions()[0]), fill_px=199.0, reason="RECONCILED")
check("a reconcile-booked close pulls the trigger (the 10 Sep SOL leak)",
      _cancels(fake) == ["STPSLz"])
db.insert_position(dict(coin="DOGE", side="short", entry=0.086, qty=1000.0, notional=86.0,
                        margin=43.0, peak=0.086, hard_stop=0.0947, hard_stop_id="STPDGEz",
                        trail_active=0, trail_stop=None, intended_entry=0.086,
                        opened_at="2026-09-10T00:00:30+00:00"))
fake.posts.clear()
em._void_position(dict(db.open_positions()[0]), 30)
check("a VOIDed position pulls its trigger too (the 10 Sep DOGE leak)",
      _cancels(fake) == ["STPDGEz"])

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
