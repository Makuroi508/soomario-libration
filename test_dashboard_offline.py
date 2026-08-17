"""
Offline test for api_solana.py - verifies every endpoint scopes to ?venue=
and that Pacifica / Bulk state stays fully isolated. No network.
"""
import os, sys, shutil, json

os.environ["STATE_PATH"] = "/tmp/dash_test"
os.environ["ENABLED_VENUES"] = "pacifica,bulk"
os.environ["COINS"] = "SOL,ZEC,ETH,DOGE"
shutil.rmtree("/tmp/dash_test", ignore_errors=True)

import config
from config import STATE_DIR
from db import DB
from utils import save_json, append_jsonl, iso
import api_solana

PASS = FAIL = 0
def check(n, c):
    global PASS, FAIL
    (globals().__setitem__('PASS', PASS+1) if c else globals().__setitem__('FAIL', FAIL+1))
    print(("  PASS  " if c else "  FAIL  ") + n)


def seed(venue, equity, open_coin, closed_coin, net_pct):
    d = STATE_DIR / venue
    d.mkdir(parents=True, exist_ok=True)
    db = DB(path=str(d / "libration.db"))
    db.set_account(equity=equity, daily_baseline=equity, daily_halt=0,
                   inception=1000.0, inception_ts="2026-01-01T00:00:00+00:00",
                   last_reset="2026-01-01")
    db.insert_position({"coin": open_coin, "side": "long", "entry": 100.0, "qty": 1.0,
                        "notional": 100.0, "margin": 50.0, "peak": 100.0, "hard_stop": 90.0,
                        "hard_stop_id": "x", "trail_active": 0, "trail_stop": None,
                        "intended_entry": 100.0, "opened_at": "2026-02-01T00:00:00+00:00"})
    db.book_trade({"coin": closed_coin, "side": "long", "entry": 100.0, "exit": 100+net_pct,
                   "qty": 1.0, "ret_pct": net_pct, "net_pct": net_pct, "friction_pct": 0.1,
                   "fee": 0.05, "exit_reason": "TRAIL", "opened_at": "2026-02-01T00:00:00+00:00",
                   "closed_at": "2026-02-02T00:00:00+00:00"})
    save_json(d / "status.json", {"ts": iso(), "venue": venue, "equity": equity,
                                  "config": config.summary(), "total_upnl": 0,
                                  "open_positions": 1, "daily_halt": False,
                                  "coins": [open_coin, closed_coin],
                                  "positions": [{"coin": open_coin, "side": "long",
                                                 "entry": 100.0, "qty": 1.0}]})
    append_jsonl(d / "equity_log.jsonl", {"ts": "2026-02-01T00:00:00+00:00", "equity": 1000.0, "active_positions": 0})
    append_jsonl(d / "equity_log.jsonl", {"ts": iso(), "equity": equity, "active_positions": 1})

seed("pacifica", 1200.0, "SOL", "ZEC", 3.0)
seed("bulk", 980.0, "ETH", "DOGE", -1.0)
# shared signal db (for /api/universe heat)
sdb = DB(path=str(STATE_DIR / "signals" / "signals.db"))
sdb.set_rsi_state("SOL", 1, 49.2)

cl = api_solana.app.test_client()
def get(path):
    return json.loads(cl.get(path).data)

print("[1] venue discovery")
v = get("/api/venues")
check("lists both venues", set(v["venues"]) == {"pacifica", "bulk"})
check("default is first (pacifica)", v["default"] == "pacifica")
check("has human labels", v["labels"]["bulk"].lower().startswith("bulk"))

print("[2] status scoping")
check("pacifica status equity 1200", get("/api/status?venue=pacifica")["equity"] == 1200.0)
check("bulk status equity 980", get("/api/status?venue=bulk")["equity"] == 980.0)
check("no venue -> defaults to pacifica", get("/api/status")["venue"] == "pacifica")

print("[3] positions + trades isolation")
pac_pos = get("/api/positions?venue=pacifica")["positions"]
bulk_pos = get("/api/positions?venue=bulk")["positions"]
check("pacifica holds SOL", pac_pos[0]["coin"] == "SOL")
check("bulk holds ETH", bulk_pos[0]["coin"] == "ETH")
pac_tr = get("/api/trades?venue=pacifica")["trades"]
bulk_tr = get("/api/trades?venue=bulk")["trades"]
check("pacifica trade is ZEC, not DOGE", pac_tr[0]["coin"] == "ZEC" and all(t["coin"] != "DOGE" for t in pac_tr))
check("bulk trade is DOGE, not ZEC", bulk_tr[0]["coin"] == "DOGE" and all(t["coin"] != "ZEC" for t in bulk_tr))

print("[4] stats + equity per venue")
ps = get("/api/stats?venue=pacifica")
check("pacifica stats: 1 closed, realized>0", ps["closed_trades"] == 1 and ps["realized_pnl"] > 0)
bs = get("/api/stats?venue=bulk")
check("bulk stats: realized<0", bs["realized_pnl"] < 0)
pe = get("/api/equity?venue=pacifica&tf=all")
check("pacifica equity has points", len(pe["points"]) >= 1 and pe["venue"] == "pacifica")
check("pacifica events reference ZEC/SOL only",
      all(ev["asset"] in ("ZEC", "SOL") for ev in pe["events"]))
be = get("/api/equity?venue=bulk&tf=all")
check("bulk events reference DOGE/ETH only",
      all(ev["asset"] in ("DOGE", "ETH") for ev in be["events"]))

print("[5] universe uses shared signal rsi + per-venue active")
u = get("/api/universe?venue=pacifica")
sol = next((c for c in u["coins"] if c["coin"] == "SOL"), None)
check("SOL active (open on pacifica)", sol and sol["in_position"] is True)
zec = next((c for c in u["coins"] if c["coin"] == "ZEC"), None)
check("universe coins come from venue status list", zec is not None)

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
