"""
Soomario Libration-Solana - dashboard API (venue-parameterized)
===============================================================
Same surface as the single-venue api.py, but every endpoint takes ?venue= and
reads that venue's own isolated state: its status.json + equity_log.jsonl (under
STATE_DIR/<venue>/) and its own SQLite DB. Curve event markers are derived from
the per-venue trade ledger (clean per-venue) rather than a shared trade log.
rsi_state for the Universe heat comes from the SHARED signal DB, since the signal
is computed once and fanned.

Read-only: the worker owns all writes; the API opens its own cached read handles
(one per venue) serialized by a lock (WAL lets them coexist with the worker).
"""
import logging
import os
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory

import config
from config import BASE_DIR, STATE_DIR, summary as config_summary
from utils import tail_jsonl, load_json, iso

logger = logging.getLogger("api_solana")
app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")

_DBS = {}
_LOCK = threading.RLock()


def enabled_venues():
    env = [v.strip().lower() for v in os.getenv("ENABLED_VENUES", "").split(",") if v.strip()]
    if env:
        return env
    found = []
    try:
        for d in sorted(STATE_DIR.iterdir()):
            if d.is_dir() and d.name != "signals" and (d / "libration.db").exists():
                found.append(d.name)
    except FileNotFoundError:
        pass
    return found or ["pacifica"]


def _venue():
    vs = enabled_venues()
    v = (request.args.get("venue") or vs[0]).lower()
    return v if v in vs else vs[0]


def _venue_dir(v):
    return STATE_DIR / v


def _db(v):
    with _LOCK:
        if v not in _DBS:
            from db import DB
            _DBS[v] = DB(path=str(_venue_dir(v) / "libration.db"))
        return _DBS[v]


def _signal_db():
    with _LOCK:
        if "__signals__" not in _DBS:
            from db import DB
            _DBS["__signals__"] = DB(path=str(STATE_DIR / "signals" / "signals.db"))
        return _DBS["__signals__"]


@app.before_request
def _acquire():
    _LOCK.acquire()


@app.teardown_request
def _release(exc=None):
    try:
        _LOCK.release()
    except RuntimeError:
        pass


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/")
def dashboard():
    return send_from_directory(str(BASE_DIR), "dashboard.html")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "ts": iso(), "venues": enabled_venues()}), 200


@app.route("/api/venues")
def api_venues():
    vs = enabled_venues()
    labels = {"pacifica": "Pacifica (live)", "bulk": "Bulk (testnet)"}
    return jsonify({"venues": vs, "default": vs[0],
                    "labels": {v: labels.get(v, v.title()) for v in vs}})


@app.route("/api/status")
def api_status():
    v = _venue()
    st = load_json(_venue_dir(v) / "status.json", default=None)
    if not st:
        return jsonify({"ts": iso(), "venue": v, "name": f"{config.NAME} - {v}",
                        "config": config_summary(), "equity": 0, "total_upnl": 0,
                        "open_positions": 0, "daily_halt": False, "positions": [],
                        "warming_up": True})
    return jsonify(st)


@app.route("/api/positions")
def api_positions():
    v = _venue()
    st = load_json(_venue_dir(v) / "status.json", default=None)
    if st and "positions" in st:
        return jsonify({"ts": st.get("ts"), "venue": v, "positions": st["positions"]})
    return jsonify({"ts": iso(), "venue": v, "positions": _db(v).open_positions()})


@app.route("/api/trades")
def api_trades():
    v = _venue()
    n = int(request.args.get("n", 50))
    return jsonify({"ts": iso(), "venue": v, "trades": _db(v).recent_trades(n)})


@app.route("/api/stats")
def api_stats():
    v = _venue()
    db = _db(v)
    closed = db.recent_trades(2000)
    n = len(closed)
    wins = sum(1 for t in closed if (t.get("net_pct") or 0) > 0)
    avg_net = (sum(t.get("net_pct") or 0 for t in closed) / n) if n else 0.0
    realized = db.realized_pnl()
    open_n = len(db.open_positions())
    n_miss = db._conn.execute("SELECT COUNT(*) FROM misses").fetchone()[0]
    miss_breakdown = {r: c for r, c in db._conn.execute(
        "SELECT reason, COUNT(*) FROM misses GROUP BY reason").fetchall()}
    entries = n + open_n
    signals_seen = entries + n_miss
    fill_rate = (entries / signals_seen * 100) if signals_seen else None
    st = load_json(_venue_dir(v) / "status.json", default={}) or {}
    fric_avg, fric_n = db.measured_friction()
    return jsonify({
        "ts": iso(), "venue": v,
        "equity": st.get("equity"), "total_upnl": st.get("total_upnl"),
        "open_positions": open_n, "max_concurrent": config.MAX_CONCURRENT,
        "daily_halt": st.get("daily_halt", False), "closed_trades": n,
        "win_rate": round(wins / n * 100, 2) if n else None,
        "avg_net_pct": round(avg_net, 4), "realized_pnl": round(realized, 2),
        "realized_friction_pct": fric_avg, "friction_n": fric_n,
        "fill_rate": round(fill_rate, 2) if fill_rate is not None else None,
        "signals_seen": signals_seen, "misses": n_miss,
        "miss_breakdown": miss_breakdown, "config": config_summary(),
    })


@app.route("/api/universe")
def api_universe():
    v = _venue()
    HOT_BAND, WARM_BAND = 1.5, 3.0

    def heat(rsi):
        if rsi is None:
            return "dormant"
        for d in (config.LONG_LEVEL - rsi, rsi - config.SHORT_LEVEL):
            if 0 < d <= HOT_BAND:
                return "hot"
        for d in (config.LONG_LEVEL - rsi, rsi - config.SHORT_LEVEL):
            if 0 < d <= WARM_BAND:
                return "warming"
        return "dormant"

    rsi_map = {r["coin"]: r["last_rsi"] for r in _signal_db().all_rsi_state()}
    active = {p["coin"] for p in _db(v).open_positions()}
    st = load_json(_venue_dir(v) / "status.json", default={}) or {}
    coins_list = st.get("coins") or [c.upper() for c in config.COINS]
    coins = []
    for sym in coins_list:
        sym = sym.upper()
        coins.append({"coin": sym,
                      "state": "active" if sym in active else heat(rsi_map.get(sym)),
                      "in_position": sym in active,
                      "watch": sym in config.WATCH_SET})
    return jsonify({"ts": iso(), "venue": v, "coins": coins})


@app.route("/api/shadow")
def api_shadow():
    v = _venue()
    db = _db(v)

    def _median(xs):
        xs = sorted(xs); k = len(xs)
        return None if k == 0 else (xs[k // 2] if k % 2 else (xs[k // 2 - 1] + xs[k // 2]) / 2)

    live_nets = [t.get("net_pct") or 0 for t in db.recent_trades(2000)]
    live = {"trail_pct": config.TRAIL_PCT, "n": len(live_nets),
            "median_net_pct": round(_median(live_nets), 4) if live_nets else None,
            "win_rate": round(sum(1 for x in live_nets if x > 0) / len(live_nets) * 100, 2) if live_nets else None}
    return jsonify({"ts": iso(), "venue": v, "live": live, "shadows": [],
                    "note": "Per-venue live trail stats. Shadow A/B is HL-only for now."})


@app.route("/api/equity")
def api_equity():
    v = _venue()
    n = int(request.args.get("n", 5000))
    tf = (request.args.get("tf") or "all").lower()
    tf_config = {
        "1d": {"window_secs": 86400, "bucket_secs": 3600},
        "1w": {"window_secs": 86400 * 7, "bucket_secs": 14400},
        "1m": {"window_secs": 86400 * 30, "bucket_secs": 86400},
        "all": {"window_secs": None, "bucket_secs": 86400},
    }.get(tf, {"window_secs": None, "bucket_secs": 86400})
    bucket_secs, window_secs = tf_config["bucket_secs"], tf_config["window_secs"]

    SNAPSHOT_SECS = config.POLL_SECONDS
    n = max(n, int(window_secs / SNAPSHOT_SECS) + 100) if window_secs else max(n, 200000)
    now_dt = datetime.now(timezone.utc)
    window_start_dt = (datetime.fromtimestamp(now_dt.timestamp() - window_secs, tz=timezone.utc)
                       if window_secs is not None else None)
    cutoff_iso = window_start_dt.isoformat() if window_start_dt else None

    points_raw = tail_jsonl(_venue_dir(v) / "equity_log.jsonl", n=n) or []
    points_in_window = [p for p in points_raw if (p.get("ts") or "") >= cutoff_iso] if cutoff_iso else points_raw

    def _ts_epoch(p):
        try:
            return datetime.fromisoformat(str(p.get("ts", "")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return 0.0

    by_bucket = {}
    for p in points_in_window:
        ts = _ts_epoch(p)
        if ts <= 0:
            continue
        b = int(ts // bucket_secs)
        cur = by_bucket.get(b)
        if cur is None or _ts_epoch(cur) <= ts:
            by_bucket[b] = p
    points = [by_bucket[k] for k in sorted(by_bucket.keys())]

    # events from the per-venue ledger: ENTRY (opened_at) + EXIT (closed_at)
    events = []
    db = _db(v)
    for t in db.recent_trades(n):
        oa, ca = t.get("opened_at"), t.get("closed_at")
        asset = t.get("coin") or ""
        if oa and (not cutoff_iso or oa >= cutoff_iso):
            events.append({"ts": oa, "type": "ENTRY", "asset": asset, "label": f"ENTRY {asset}"})
        if ca and (not cutoff_iso or ca >= cutoff_iso):
            reason = (t.get("exit_reason") or "EXIT").upper()
            etype = "TRAIL" if "TRAIL" in reason else ("HARD_STOP" if "STOP" in reason else "EXIT")
            pnl = t.get("net_pct")
            try:
                label = f"{etype} {asset} {pnl:+.2f}%"
            except (TypeError, ValueError):
                label = f"{etype} {asset}"
            events.append({"ts": ca, "type": etype, "asset": asset, "label": label})
    for p in db.open_positions():
        oa = p.get("opened_at")
        if oa and (not cutoff_iso or oa >= cutoff_iso):
            events.append({"ts": oa, "type": "ENTRY", "asset": p.get("coin", ""),
                           "label": f"ENTRY {p.get('coin','')}"})

    window_start_out = (window_start_dt.isoformat() if window_start_dt
                        else (points[0].get("ts") if points else now_dt.isoformat()))
    return jsonify({"tf": tf, "venue": v, "bucket_seconds": bucket_secs,
                    "window_start": window_start_out, "window_end": now_dt.isoformat(),
                    "empty": len(points) < 2, "points": points, "events": events})
