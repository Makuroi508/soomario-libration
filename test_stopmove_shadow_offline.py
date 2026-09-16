"""Offline cover for two changes - no network, no venue keys.

  1. _move_stop only records trail_stop when the venue CONFIRMS the move, and
     still records it on venues where the software backstop owns the trail.
  2. Shadows can carry an ARMING DELAY, width shadows inherit the live delay,
     and the summary keeps each rule in its own bucket.
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

TMP = os.path.join(tempfile.gettempdir(), "stopmove_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ.update(STATE_PATH=TMP, PAPER="0", DRY_RUN="0", TG_ENABLED="0",
                  COINS="SOL,LINK", SHADOW_TRAILS="0.4",
                  SHADOW_ARM_DELAYS="0,0.55:0", COIN_PARAMS='{"LINK":{"trail_pct":0.75}}')

import config                                  # noqa: E402
from db import DB                              # noqa: E402
from exit_manager import ExitManager           # noqa: E402
from shadow import ShadowTracker               # noqa: E402

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
    """modify_stop behaviour is switchable to cover every venue convention."""
    moves_native_stop = True

    def __init__(self, mode="ok"):
        self.mode, self.calls, self.n = mode, [], 0

    def modify_stop(self, coin, is_long, qty, old_oid, new_stop):
        self.calls.append((coin, new_stop))
        if self.mode == "ok":
            self.n += 1
            return f"oid{self.n}"
        if self.mode == "kept_old":           # HL / Propr / Bulk: placement failed
            return str(old_oid)
        if self.mode == "none":               # nothing came back at all
            return None
        if self.mode == "backstop":           # software backstop owns the exit
            return "backstop"
        raise AssertionError(self.mode)

    def place_stop_market(self, *a, **k):
        return "oid0"

    def get_positions(self):
        return []

    def market_close(self, *a, **k):
        return {"filled": True, "avg_price": 1.0}

    def cancel_order(self, *a, **k):
        return True


def pos(stop_id="oid0"):
    return dict(coin="SOL", side="long", entry=100.0, qty=1.0, peak=101.0,
                hard_stop=90.0, hard_stop_id=stop_id, trail_active=1,
                trail_stop=99.0, opened_at="2026-01-01T00:00:00+00:00")


print("[1] _move_stop records ONLY a confirmed move")
db = DB(path=os.path.join(TMP, "a.db"))
for mode, expect_recorded, label in (
        ("ok", True, "venue moved it -> record new level and new id"),
        ("kept_old", False, "venue kept the OLD trigger -> do not record"),
        ("none", False, "venue returned nothing -> do not record"),
        ("backstop", True, "software backstop owns the trail -> MUST record")):
    em = ExitManager(Client(mode), db)
    up = {}
    em._move_stop(pos(), 1.0, 99.5, up)
    check(f"{mode}: {label}", ("trail_stop" in up) is expect_recorded)

em = ExitManager(Client("ok"), db)
up = {}
em._move_stop(pos(), 1.0, 99.5, up)
check("confirmed move stores the NEW oid", up.get("hard_stop_id") == "oid1")

c = Client("kept_old")
em = ExitManager(c, db)
for _ in range(3):
    em._move_stop(pos(), 1.0, 99.5, {})
check("a stuck stop keeps RETRYING every tick", len(c.calls) == 3)
check("and is counted so it can be alarmed on", em._stop_stuck.get("SOL") == 3)

c = Client("ok")
em = ExitManager(c, db)
em._stop_stuck["SOL"] = 7
em._move_stop(pos(), 1.0, 99.5, {})
check("a successful move clears the stuck streak", "SOL" not in em._stop_stuck)


class NoNativeClient(Client):
    moves_native_stop = False


em = ExitManager(NoNativeClient("kept_old"), db)
up = {}
em._move_stop(pos(), 1.0, 99.5, up)
check("moves_native_stop=False (Foxify) still records the trail",
      up.get("trail_stop") == 99.5)

import foxify_client                             # noqa: E402
check("foxify_client declares moves_native_stop=False",
      foxify_client.FoxifyClient.moves_native_stop is False)

print("\n[2] shadows carry an arming delay")
check("SHADOW_ARM_DELAYS parses '0,0.55:0'",
      config.SHADOW_ARM_DELAYS == [(None, 0.0), (0.55, 0.0)])

db2 = DB(path=os.path.join(TMP, "b.db"))
sh = ShadowTracker(db2, friction_fn=lambda: 0.0)
now = datetime.now(timezone.utc)
sh.on_open(dict(coin="LINK", side="long", entry=100.0, qty=1.0, hard_stop=90.0,
                opened_at=now.isoformat()))
rows = db2.get_open_shadows("LINK")
check("one shadow per rule (1 width + 2 arm-delay)", len(rows) == 3)
widths = sorted(r["trail_pct"] for r in rows)
check("width shadow at 0.4, arm shadows at the live 0.75 and at 0.55",
      widths == [0.4, 0.55, 0.75])
live = config.arm_delay_sec("LINK") / 3600
check(f"the WIDTH shadow inherits the live delay ({live:g}h), not zero",
      any(r["trail_pct"] == 0.4 and r["arm_delay_h"] == live for r in rows))
check("the arm-delay shadows carry delay 0",
      all(r["arm_delay_h"] == 0.0 for r in rows if r["trail_pct"] in (0.55, 0.75)))

# a delayed shadow must not arm inside its window, and must still honour the
# hard stop while it waits
db3 = DB(path=os.path.join(TMP, "c.db"))
db3.open_shadow("SOL", 0.55, "long", 100.0, 1.0, 90.0, now.isoformat(), arm_delay_h=4.0)
s3 = ShadowTracker(db3, trails=[], arm_delays=[], friction_fn=lambda: 0.0)
s3.on_price("SOL", 102.0)
r = db3.get_open_shadows("SOL")[0]
check("inside the delay it does NOT arm even at +2%", not r["active"])
check("inside the delay the peak is pinned to price", r["peak"] == 102.0)
s3.on_price("SOL", 89.0)
check("the hard stop still fires inside the delay", not db3.get_open_shadows("SOL"))

db4 = DB(path=os.path.join(TMP, "d.db"))
aged = (now - timedelta(hours=5)).isoformat()
db4.open_shadow("SOL", 0.55, "long", 100.0, 1.0, 90.0, aged, arm_delay_h=4.0)
s4 = ShadowTracker(db4, trails=[], arm_delays=[], friction_fn=lambda: 0.0)
s4.on_price("SOL", 101.0)
check("past the delay it arms normally", bool(db4.get_open_shadows("SOL")[0]["active"]))

print("\n[3] the summary keeps each rule apart")
db5 = DB(path=os.path.join(TMP, "e.db"))
for trail, stale, arm in ((0.55, None, None), (0.55, None, 0.0), (0.4, None, 4.0),
                          (0.55, 24.0, 4.0)):
    db5.open_shadow("SOL", trail, "long", 100.0, 1.0, 90.0, now.isoformat(),
                    stale_h=stale, arm_delay_h=arm)
for row in db5.get_open_shadows("SOL"):
    db5.close_shadow(row["id"], 101.0, 1.0, 1.0, "TRAIL", row["opened_at"])
keys = set(db5.shadow_summary())
check("legacy (no stale, no arm) stays a bare float key", 0.55 in keys)
check("arm-delay rule gets its own bucket", "0.55+arm0h" in keys)
check("width+delay rule gets its own bucket", "0.4+arm4h" in keys)
check("stale+delay rule gets its own bucket", "0.55+stale24h+arm4h" in keys)
check("four rules -> four buckets, nothing pooled", len(keys) == 4)
check("every bucket kept its one trade",
      all(v["n"] == 1 for v in db5.shadow_summary().values()))

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
