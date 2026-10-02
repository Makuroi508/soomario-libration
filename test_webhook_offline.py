"""
Offline checks for the TradingView webhook
══════════════════════════════════════════
    python test_webhook_offline.py        (no network, no keys, no venue)

The engine used to re-derive TradingView's decision from candles. That is a
replica, and the replica drifted: a trail retuned on the chart but not in
COIN_PARAMS, JTO on a 4h chart here and a 30m chart there, LTC crossing 50 on
three exchanges while the chart declined the trade. SIGNAL_SOURCE=webhook ends
the guessing — the chart posts its own fills and the book mirrors them.

What must hold, because real money is behind it: an unsigned request does
nothing, a retried alert does not become a second order, a reversal closes
before it opens, a stale alert is refused rather than entered at today's price,
and the endpoint itself never places an order on a request thread.
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="wh_test_")
os.environ["STATE_PATH"] = TMP
os.environ["COINS"] = "SOL,LTC,KPEPE,HYPE"
os.environ["WEBHOOK_SECRET"] = "test-secret-value"
os.environ["TV_SYMBOL_MAP"] = json.dumps({"MYCOIN": "HYPE"})
os.environ["SIGNAL_SOURCE"] = "webhook"

import config
import api
import app as worker
from db import DB
from utils import iso

ok = fail = 0


def check(label, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label}")


SECRET = "test-secret-value"
cl = api.app.test_client()
db = DB()


def post(payload, **kw):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return cl.post("/webhook/tradingview", data=body,
                   content_type="application/json", **kw)


def alert(ticker="LTCUSDT.P", position="long", action="buy", price="68.25",
          bar="2026-10-01T20:00:00Z", secret=SECRET):
    d = {"ticker": ticker, "position": position, "action": action,
         "price": price, "bar": bar}
    if secret is not None:
        d["secret"] = secret
    return d


print("\n[1] nothing happens without the secret")
r = post(alert(secret=None))
check("an unsigned alert is refused", r.status_code == 401)
r = post(alert(secret="wrong-secret-value"))
check("a wrong secret is refused", r.status_code == 401)
check("and neither one is queued", db.recent_signals(10) == [])
r = post(alert(secret=SECRET + "x"))
check("a secret with the right prefix is still refused", r.status_code == 401)

print("\n[2] the alert is understood")
r = post(alert())
d = r.get_json()
check("a signed alert is accepted", r.status_code == 200 and d["ok"])
check("the ticker resolves to the book's coin", d["coin"] == "LTC")
check("the position is what gets mirrored", d["state"] == "long")
check("the price comes through", d["price"] == 68.25)
check("it is queued, not acted on in the request", d["queued"] is True)
check("the response says the book will act", d["acted_on"] is True)

r = post(alert(ticker="BINANCE:1000PEPEUSDT.P", position="short"))
check("an exchange-prefixed 1000x ticker maps to KPEPE", r.get_json()["coin"] == "KPEPE")
r = post(alert(ticker="MYCOIN", position="short"))
check("TV_SYMBOL_MAP covers the exceptions", r.get_json()["coin"] == "HYPE")
r = post(alert(ticker="ETHUSDT"))
check("a symbol this book does not trade is refused", r.status_code == 400)
r = post("ticker=SOLUSDT.P;position=short;price=180.5;bar=b1;secret=" + SECRET)
check("a key=value alert works too", r.get_json()["coin"] == "SOL")

print("\n[3] a retry is not a second order")
before = len(db.recent_signals(100))
r1 = post(alert(bar="2026-10-02T00:00:00Z"))
r2 = post(alert(bar="2026-10-02T00:00:00Z"))
check("the first delivery queues", r1.get_json()["queued"] is True)
check("the retry is recognised as a duplicate", r2.get_json()["duplicate"] is True)
check("and only one row exists", len(db.recent_signals(100)) == before + 1)
r3 = post(alert(bar="2026-10-02T04:00:00Z"))
check("the next bar is NOT a duplicate", r3.get_json()["queued"] is True)

print("\n[4] the reversal reads as a position, not a verb")
check("market_position wins over the order verb",
      api._alert_state({"action": "buy", "position": "short"}) == "short")
check("a bare buy still means long", api._alert_state({"action": "buy"}) == "long")
check("a close means flat", api._alert_state({"action": "close"}) == "flat")
check("flat is flat", api._alert_state({"position": "flat"}) == "flat")
check("an empty alert has no state", api._alert_state({}) is None)
check("oversized bodies are refused",
      post(json.dumps(alert()) + " " * 5000).status_code == 413)


# ── the worker side ────────────────────────────────────────────────────
class StubPM:
    def __init__(self):
        self.entered, self.closed, self.block = [], [], False

    def maybe_enter(self, coin, signal, price):
        self.entered.append((coin, signal, price))
        if self.block:
            return None
        DBH.insert_position({"coin": coin, "side": signal, "entry": price or 1.0,
                             "qty": 1.0, "notional": 10.0, "margin": 5.0,
                             "peak": price or 1.0, "hard_stop": 0.5, "hard_stop_id": "x",
                             "trail_active": 0, "trail_stop": None,
                             "intended_entry": price or 1.0, "opened_at": iso()})
        return {"coin": coin, "side": signal, "entry": price or 1.0, "qty": 1.0}

    def close_one(self, p, reason, exit_manager=None):
        self.closed.append((p["coin"], reason))
        DBH.delete_position(p["coin"])
        return 99.0


class StubShadow:
    def __init__(self):
        self.opens = []

    def on_open(self, pos):
        self.opens.append(pos["coin"])


DBH = DB()
for r in DBH.recent_signals(500):
    DBH.mark_signal(r["id"], "ignored", "test reset")
for p in DBH.open_positions():
    DBH.delete_position(p["coin"])

print("\n[5] the worker acts on what the chart sent")
pm, sh = StubPM(), StubShadow()
DBH.enqueue_signal("k1", "LTC", "long", 68.25, "b1", "tv", "{}")
n = worker.drain_signal_inbox(DBH, pm, None, sh, {"LTC": 68.30})
check("one signal, one entry", n == 1 and pm.entered == [("LTC", "long", 68.30)])
check("the live mark is preferred over the alert price", pm.entered[0][2] == 68.30)
check("the shadow A/B starts on it", sh.opens == ["LTC"])
check("the row is marked done",
      [r for r in DBH.recent_signals(5) if r["dedupe"] == "k1"][0]["status"] == "done")
check("a drained inbox is not re-run", worker.drain_signal_inbox(DBH, pm, None, sh, {}) == 0)

print("\n[6] flat closes, same-side does nothing, reversal goes through maybe_enter")
DBH.enqueue_signal("k2", "LTC", "long", 68.0, "b2", "tv", "{}")
worker.drain_signal_inbox(DBH, pm, None, sh, {"LTC": 68.0})
check("an alert for the position already held is a no-op", len(pm.entered) == 1)
DBH.enqueue_signal("k3", "LTC", "short", 67.0, "b3", "tv", "{}")
worker.drain_signal_inbox(DBH, pm, None, sh, {"LTC": 67.0})
check("the opposite side is handed to maybe_enter, which owns the reversal",
      pm.entered[-1] == ("LTC", "short", 67.0))
DBH.enqueue_signal("k4", "LTC", "flat", None, "b4", "tv", "{}")
worker.drain_signal_inbox(DBH, pm, None, sh, {"LTC": 66.0})
check("flat closes the position", pm.closed == [("LTC", "TV_EXIT")])
check("the close is booked as a TradingView exit, not a trail",
      pm.closed[-1][1] == "TV_EXIT")
DBH.enqueue_signal("k5", "LTC", "flat", None, "b5", "tv", "{}")
worker.drain_signal_inbox(DBH, pm, None, sh, {})
check("flat when already flat closes nothing twice", len(pm.closed) == 1)

print("\n[7] what must never be acted on")
DBH.enqueue_signal("k6", "LTC", "long", 68.0, "b6", "tv", "{}")
row = [r for r in DBH.recent_signals(5) if r["dedupe"] == "k6"][0]
DBH._conn.execute("UPDATE signal_inbox SET received_at = ? WHERE id = ?",
                  ((datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(), row["id"]))
DBH._conn.commit()
before = len(pm.entered)
worker.drain_signal_inbox(DBH, pm, None, sh, {"LTC": 68.0})
check("an alert older than WEBHOOK_MAX_AGE_SEC is refused", len(pm.entered) == before)
check("and says why",
      "stale" in ([r for r in DBH.recent_signals(5)
                   if r["dedupe"] == "k6"][0]["note"] or ""))

DBH.enqueue_signal("k7", "DOGE", "long", 0.4, "b7", "tv", "{}")
worker.drain_signal_inbox(DBH, pm, None, sh, {})
check("a coin this book does not trade is refused", len(pm.entered) == before)

pm.block = True
DBH.enqueue_signal("k8", "SOL", "long", 180.0, "b8", "tv", "{}")
worker.drain_signal_inbox(DBH, pm, None, sh, {"SOL": 180.0})
r = [x for x in DBH.recent_signals(5) if x["dedupe"] == "k8"][0]
check("a gate (halt, capacity, margin) still outranks the chart", r["status"] == "ignored")
check("and the signal is not retried forever", r["status"] != "pending")

print("\n[8] the endpoint records but does not act when the feed is in charge")
config.SIGNAL_SOURCE = "feed"
r = post(alert(bar="2026-10-03T00:00:00Z"))
check("the alert is still accepted, so wiring can be tested first",
      r.status_code == 200 and r.get_json()["queued"] is True)
check("but the response says it will not be acted on",
      r.get_json()["acted_on"] is False)
d = cl.get("/api/signals").get_json()
check("/api/signals shows the inbox", d["count"] > 0 and d["signal_source"] == "feed")
check("and that the webhook is configured", d["webhook_configured"] is True)
config.SIGNAL_SOURCE = "webhook"

print("\n[9] an unconfigured deployment has no open door")
_s = config.WEBHOOK_SECRET
config.WEBHOOK_SECRET = ""
check("no secret set means the endpoint is closed", post(alert()).status_code == 503)
config.WEBHOOK_SECRET = _s

shutil.rmtree(TMP, ignore_errors=True)
RULE = "-" * 58
print("")
print(RULE)
print(f"  {ok} passed, {fail} failed")
print(RULE)
sys.exit(1 if fail else 0)
