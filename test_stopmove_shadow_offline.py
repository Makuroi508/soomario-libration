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


print("\n[4] per-coin RSI timeframe, length and signal venue")
import importlib                                # noqa: E402
import json as _json                            # noqa: E402
os.environ["COIN_PARAMS"] = _json.dumps({
    "TAO": {"rsi_tf": "6h", "arm_delay_min": 1440},
    "AVAX": {"rsi_tf": "1h", "rsi_len": 13, "long_level": 71, "short_level": 32,
             "trail_pct": 0.4, "arm_delay_min": 60},
    "XLM": {"rsi_len": 5, "long_level": 20, "short_level": 55,
            "hard_stop_pct": 6.15, "trail_pct": 0.2},
    "HYPE": {"signal_venue": "bybit"},
    "CRV": {"signal_venue": "okx", "short_level": 25, "hard_stop_pct": 10.75,
            "trail_pct": 0.15},
    "LTC": {"arm_delay_min": 60},
    "ZZZ": {"rsi_tf": "7h"},
})
importlib.reload(config)

check("per-coin rsi_tf", config.rsi_tf("TAO") == "6h" and config.rsi_tf("AVAX") == "1h")
check("rsi_tf falls back to the global when unset", config.rsi_tf("LTC") == config.RSI_TF)
check("an unknown rsi_tf falls back instead of fetching nothing",
      config.rsi_tf("ZZZ") == config.RSI_TF)
check("per-coin rsi_len", config.rsi_len("XLM") == 5 and config.rsi_len("AVAX") == 13)
check("rsi_len defaults to the global", config.rsi_len("HYPE") == config.RSI_LEN)
check("per-coin signal_venue",
      config.signal_venue("HYPE") == "bybit" and config.signal_venue("CRV") == "okx")
check("signal_venue defaults to hyperliquid", config.signal_venue("SUI") == "hyperliquid")
check("bar_seconds follows the coin's own timeframe",
      config.bar_seconds("TAO") == 21600 and config.bar_seconds("AVAX") == 3600)
check("AVAX inverted levels survive (long 71 > short 32)",
      config.long_level("AVAX") == 71 and config.short_level("AVAX") == 32)
check("XLM inverted levels survive (long 20 < short 55)",
      config.long_level("XLM") == 20 and config.short_level("XLM") == 55)
check("explicit arm_delay_min still wins",
      config.arm_delay_sec("TAO") == 1440 * 60 and config.arm_delay_sec("LTC") == 3600)

# a coin with a per-coin timeframe and NO explicit delay must inherit one bar of
# its OWN timeframe, not the global 4h
os.environ["COIN_PARAMS"] = _json.dumps({"FOO": {"rsi_tf": "1h"}})
importlib.reload(config)
check("arm delay defaults to ONE bar of the coin's own timeframe",
      config.arm_delay_sec("FOO") == config.TRAIL_ARM_DELAY_BARS * 3600)

print("\n[5] intervals the venue does not serve are rolled up")
import hl_client                                # noqa: E402
step6 = 6 * 3600 * 1000
check("6h is not native to Hyperliquid", "6h" not in hl_client._HL_NATIVE_TF)
check("2h is the largest native divisor of 6h",
      hl_client._divisor_tf(step6, hl_client.HLClient._INTERVAL_MS) == "2h")
base = 2 * 3600 * 1000
fine = [{"t": base * i, "T": base * (i + 1), "o": 10.0 + i, "h": 20.0 + i,
         "l": 5.0 - i, "c": 15.0 + i, "v": 1.0} for i in range(6)]
rolled = hl_client._roll_up(fine, step6)
check("six 2h bars roll up into two 6h bars", len(rolled) == 2)
check("buckets align to the 6h UTC boundary",
      all(b["t"] % step6 == 0 for b in rolled))
check("open is the FIRST open, close the LAST close",
      rolled[0]["o"] == fine[0]["o"] and rolled[0]["c"] == fine[2]["c"])
check("high/low span the whole bucket",
      rolled[0]["h"] == max(f["h"] for f in fine[:3])
      and rolled[0]["l"] == min(f["l"] for f in fine[:3]))
check("volume sums", rolled[0]["v"] == 3.0)

print("\n[6] the signal feed routes per coin")
import feeds                                    # noqa: E402
check("binance symbol mapping", feeds._sym("binance", "SOL") == "SOLUSDT")
check("kPEPE maps to the 1000x ticker",
      feeds._sym("binance", "KPEPE") == "1000PEPEUSDT")
check("okx uses swap instIds", feeds._sym("okx", "CRV") == "CRV-USDT-SWAP")


class _StubHL:
    def __init__(self): self.calls = []

    def fetch_candles(self, coin, interval, limit):
        self.calls.append((coin, interval, limit))
        return [{"t": 0, "T": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}]


stub = _StubHL()
feeds.fetch_candles("hyperliquid", "SOL", "4h", 200, hl_client=stub)
check("hyperliquid routes to the client the worker already holds",
      stub.calls == [("SOL", "4h", 200)])
feeds.fetch_candles("something-else", "SOL", "4h", 200, hl_client=stub)
check("an unknown venue falls back rather than failing", len(stub.calls) == 2)

feeds._LAST_GOOD[("binance", "SOL", "4h")] = [{"t": 1, "T": 2, "o": 1, "h": 1,
                                               "l": 1, "c": 1, "v": 0}]
_real = feeds._FETCH["binance"]
feeds._FETCH["binance"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
got = feeds.fetch_candles("binance", "SOL", "4h", 200)
feeds._FETCH["binance"] = _real
check("a feed outage serves the last good series, not an empty one", len(got) == 1)


print("\n[7] per-coin size_mult")
os.environ["WATCH"] = "SOL,LINK"
os.environ["WATCH_SIZE_MULT"] = "0.5"
os.environ["COIN_PARAMS"] = _json.dumps({
    "HYPE": {"size_mult": 0.5},
    "SOL": {"size_mult": 0.3},          # overrides WATCH for this coin
    "BAD": {"size_mult": 0},            # must be ignored, not honoured
    "WORSE": {"size_mult": "abc"},
})
importlib.reload(config)
check("COIN_PARAMS size_mult applies to a non-WATCH coin", config.size_mult("HYPE") == 0.5)
check("COIN_PARAMS size_mult OVERRIDES the WATCH multiplier", config.size_mult("SOL") == 0.3)
check("a WATCH coin with no override still uses WATCH_SIZE_MULT",
      config.size_mult("LINK") == 0.5)
check("an unlisted, unwatched coin is still 1.0", config.size_mult("KPEPE") == 1.0)
check("size_mult 0 is ignored, not honoured", config.size_mult("BAD") == 1.0)
check("an unparseable size_mult is ignored", config.size_mult("WORSE") == 1.0)

# the whole point: base size and the outlier move independently
os.environ["COIN_PARAMS"] = _json.dumps({"HYPE": {"size_mult": 0.5}})
os.environ["WATCH"] = ""
os.environ["NOTIONAL_FRAC"] = "0.40"
importlib.reload(config)
eq = 10_000.0
hype = config.NOTIONAL_FRAC * eq * config.size_mult("HYPE")
other = config.NOTIONAL_FRAC * eq * config.size_mult("SOL")
check("base 0.40 with HYPE at x0.5 -> HYPE 20% notional, others 40%",
      abs(hype - 2000) < 1e-6 and abs(other - 4000) < 1e-6)
check("so a HYPE 10% stop still costs 2.00% of equity",
      abs(hype * 0.10 / eq * 100 - 2.0) < 1e-9)

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
