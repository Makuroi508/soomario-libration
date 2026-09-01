"""
Soomario Libration — API routes
═════════════════════════════════
  GET /                 — dashboard HTML
  GET /healthz          — Railway liveness
  GET /api/status       — live snapshot (reads status.json the worker writes)
  GET /api/positions    — open positions w/ live mark + uPnL
  GET /api/trades       — recent closed trades (?n=50)
  GET /api/report       — financial report (?period=&format=json|md|html|zip)
  GET /api/equity       — equity curve + event markers (?tf=1d|1w|1m|all)  [SPEC v1.2]
  GET /api/stats        — KPIs: win rate, avg net %, fill rate, realized PnL

The worker owns the live HLClient + DB writes. The API only READS: status.json
for the live snapshot, the JSONL logs for the curve, and a separate read-only
SQLite handle (WAL) for closed-trade KPIs.
"""
import logging
import threading
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, send_from_directory

import config
from config import BASE_DIR, EQUITY_LOG, TRADE_LOG, STATUS_FILE, summary as config_summary
from utils import tail_jsonl, load_json, iso, next_utc_reset, now_utc

import report as report_mod

logger = logging.getLogger("api")
app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")

# One shared read connection for the API, serialized by a lock. The worker owns
# its own separate connection for writes (WAL lets them coexist). Flask serves
# requests on per-request threads; rather than open a new SQLite connection for
# every request (churny, and noisy in the logs), all API reads share one handle
# and a lock holds for the duration of each request so no two threads execute on
# it at once. Low-traffic dashboard reads make the lock essentially free.
_DB = None
_DB_LOCK = threading.RLock()


def attach_state(db=None):
    """Liveness marker only — the API never shares the worker's connection."""
    logger.info("api: worker attached")


def _db():
    global _DB
    if _DB is None:
        from db import DB
        _DB = DB()
    return _DB


@app.before_request
def _acquire_db():
    _DB_LOCK.acquire()


@app.teardown_request
def _release_db(exc=None):
    try:
        _DB_LOCK.release()
    except RuntimeError:
        pass  # lock wasn't held (e.g. before_request never ran) — ignore


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
    return jsonify({"status": "ok", "ts": iso(), "name": config.NAME}), 200


@app.route("/api/status")
def api_status():
    st = load_json(STATUS_FILE, default=None)
    if not st:
        return jsonify({
            "ts": iso(), "name": config.NAME, "config": config_summary(),
            "equity": 0, "total_upnl": 0, "open_positions": 0,
            "daily_halt": False, "positions": [], "warming_up": True,
        })
    return jsonify(st)


@app.route("/api/positions")
def api_positions():
    st = load_json(STATUS_FILE, default=None)
    if st and "positions" in st:
        return jsonify({"ts": st.get("ts"), "positions": st["positions"]})
    return jsonify({"ts": iso(), "positions": _db().open_positions()})


@app.route("/api/trades")
def api_trades():
    n = int(request.args.get("n", 50))
    return jsonify({"ts": iso(), "trades": _db().recent_trades(n)})


@app.route("/api/stats")
def api_stats():
    db = _db()
    closed = db.recent_trades(2000)
    n = len(closed)
    wins = sum(1 for t in closed if (t.get("net_pct") or 0) > 0)
    avg_net = (sum(t.get("net_pct") or 0 for t in closed) / n) if n else 0.0
    realized = db.realized_pnl()   # durable, fee-aware, single source of truth

    open_n = len(db.open_positions())
    n_miss = db._conn.execute("SELECT COUNT(*) FROM misses").fetchone()[0]
    miss_breakdown = {
        r: c for r, c in db._conn.execute(
            "SELECT reason, COUNT(*) FROM misses GROUP BY reason").fetchall()
    }
    entries = n + open_n           # every position ever opened
    signals_seen = entries + n_miss
    fill_rate = (entries / signals_seen * 100) if signals_seen else None

    st = load_json(STATUS_FILE, default={}) or {}
    fric_avg, fric_n = db.measured_friction()
    return jsonify({
        "ts": iso(),
        "equity": st.get("equity"),
        "total_upnl": st.get("total_upnl"),
        "open_positions": open_n,
        "max_concurrent": config.MAX_CONCURRENT,
        "daily_halt": st.get("daily_halt", False),
        # Computed here rather than read from the status file so the countdown
        # stays correct even if the writer is wedged — a halted bot that has
        # stopped ticking is exactly when you most want to know the reset time.
        "daily_reset_at": next_utc_reset().isoformat(),
        "seconds_to_reset": max(0, int((next_utc_reset() - now_utc()).total_seconds())),
        "closed_trades": n,
        "win_rate": round(wins / n * 100, 2) if n else None,
        "avg_net_pct": round(avg_net, 4),
        "realized_pnl": round(realized, 2),
        "realized_friction_pct": fric_avg,
        "friction_n": fric_n,
        "fill_rate": round(fill_rate, 2) if fill_rate is not None else None,
        "signals_seen": signals_seen,
        "misses": n_miss,
        "miss_breakdown": miss_breakdown,
        "config": config_summary(),
    })


@app.route("/api/universe")
def api_universe():
    """Per-coin pulse: a COARSE 3-state heat (dormant / warming / hot) computed
    server-side from RSI proximity to a trigger boundary. The raw RSI never
    leaves the server, and no direction is shown — 'hot' spans enough range that
    it can't be cleanly front-run. Coins with an open position show 'active'.
    ATR%/volume/include are merged from universe.json when the worker has written
    it (Universe tab); absent on a fresh deploy."""
    HOT_BAND, WARM_BAND = 1.5, 3.0  # RSI points to a boundary (presentation tuning)

    def heat(rsi, coin=None):
        if rsi is None:
            return "dormant"
        # distance below the long level (long brewing) or above the short
        # level (short brewing) -- per-coin, so CC's 65 straddle reads right
        ll, sl = config.long_level(coin), config.short_level(coin)
        for d in (ll - rsi, rsi - sl):
            if 0 < d <= HOT_BAND:
                return "hot"
        for d in (ll - rsi, rsi - sl):
            if 0 < d <= WARM_BAND:
                return "warming"
        return "dormant"

    db = _db()
    rsi_map = {r["coin"]: r["last_rsi"] for r in db.all_rsi_state()}
    active = {p["coin"] for p in db.open_positions()}
    from config import STATE_DIR
    meta = load_json(STATE_DIR / "universe.json", default={}) or {}
    meta_map = {m["coin"]: m for m in meta.get("report", [])}

    coins = []
    for c in config.COINS:
        sym = c.upper()
        m = meta_map.get(sym, {})
        coins.append({
            "coin": sym,
            "state": "active" if sym in active else heat(rsi_map.get(sym), sym),
            "in_position": sym in active,
            "atr_pct": m.get("atr_pct"),
            "day_vol_usd": m.get("day_vol_usd"),
            "include": m.get("include"),
            "reasons": m.get("reasons", []),
            "watch": sym in config.WATCH_SET,
        })
    return jsonify({
        "ts": iso(), "coins": coins,
        "universe_evaluated_at": meta.get("ts"),
        "min_atr_pct": config.MIN_ATR_PCT,
        "min_vol_usd": config.MIN_DAILY_VOL_USD,
    })


@app.route("/api/shadow")
def api_shadow():
    """Live TRAIL_PCT vs shadow trails — median net %/trade after measured friction.
    The Phase-1 trail A/B decision (spec §7b). Live side = real closed trades at
    TRAIL_PCT; shadow side = counterfactual exits charged the measured friction."""
    db = _db()

    def _median(xs):
        xs = sorted(xs); n = len(xs)
        return None if n == 0 else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)

    live_nets = [t.get("net_pct") or 0 for t in db.recent_trades(2000)]
    live = {
        "trail_pct": config.TRAIL_PCT,
        "n": len(live_nets),
        "median_net_pct": round(_median(live_nets), 4) if live_nets else None,
        "win_rate": round(sum(1 for x in live_nets if x > 0) / len(live_nets) * 100, 2) if live_nets else None,
    }
    shadows = []
    # Keys are floats for plain trail-width shadows and strings for rule
    # shadows ("0.55+stale24h"), so sort on the string form - sorting the mixed
    # set directly raises TypeError.
    for key, sm in sorted(db.shadow_summary().items(), key=lambda kv: str(kv[0])):
        shadows.append({"label": str(key),
                        "trail_pct": sm.get("trail_pct", key),
                        "stale_h": sm.get("stale_h"),
                        "n": sm["n"],
                        "median_net_pct": round(sm["median_net_pct"], 4)
                        if sm["median_net_pct"] is not None else None,
                        "win_rate": sm["win_rate"]})
    fric_avg, fric_n = db.measured_friction()
    is_meas = fric_avg is not None and fric_n >= 5
    return jsonify({
        "ts": iso(),
        "measured_friction_pct": fric_avg if is_meas else config.MEASURED_FRICTION_PCT,
        "friction_is_measured": is_meas,
        "friction_n": fric_n,
        "live": live,
        "shadows": shadows,
        "note": "Decide the trail only after ~50 matched trades; shadow exits are "
                "charged measured friction for fairness (spec §7b).",
    })


# ═══════════════════════════════════════════════════════════════
#  Financial report — see report.py for the metric definitions
# ═══════════════════════════════════════════════════════════════
@app.route("/api/report")
def api_report():
    """Financial report bundle.

      ?period = lifetime | ytd | 90d | 30d | 7d      (default lifetime)
      ?format = json | md | html | marketing | marketing_html | zip

    Read-only: builds from the SQLite trade ledger with no network calls, so it
    cannot perturb the trading worker sharing this process. Gated on
    REPORT_TOKEN when that is set — the payload is full account financials.
    """
    want = (config.REPORT_TOKEN or "").strip()
    if want:
        got = (request.args.get("token") or
               request.headers.get("X-Report-Token") or "").strip()
        if got != want:
            return jsonify({"error": "unauthorized"}), 401

    period = (request.args.get("period") or "lifetime").lower()
    if period not in report_mod.PERIODS:
        return jsonify({"error": "bad period",
                        "allowed": sorted(report_mod.PERIODS)}), 400
    fmt = (request.args.get("format") or "json").lower()

    try:
        st = load_json(STATUS_FILE, default={}) or {}
        rep = report_mod.build(_db(), period=period, status=st)
    except Exception as e:                                    # noqa: BLE001
        logger.exception("report build failed")
        return jsonify({"error": "report build failed", "detail": str(e)}), 500

    stamp = rep["provenance"]["window_end"][:10]
    name = f"libration_report_{period}_{stamp}"
    if fmt == "json":
        return jsonify(rep)
    if fmt == "md":
        return Response(report_mod.render_markdown(rep),
                        mimetype="text/markdown; charset=utf-8")
    if fmt == "html":
        return Response(report_mod.render_html(rep), mimetype="text/html; charset=utf-8")
    if fmt == "marketing":
        return Response(report_mod.render_marketing_markdown(rep),
                        mimetype="text/markdown; charset=utf-8")
    if fmt == "marketing_html":
        return Response(report_mod.render_html(rep, marketing=True),
                        mimetype="text/html; charset=utf-8")
    if fmt == "zip":
        return Response(report_mod.bundle_zip(rep), mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})
    return jsonify({"error": "bad format",
                    "allowed": ["json", "md", "html", "marketing",
                                "marketing_html", "zip"]}), 400


# ═══════════════════════════════════════════════════════════════
#  Equity curve — conforms to EQUITY_CURVE_SPEC.md v1.2 (§7 verbatim)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/equity")
def api_equity():
    n = int(request.args.get("n", 5000))
    tf = (request.args.get("tf") or "all").lower()

    tf_config = {
        "1d":  {"window_secs": 86400,       "bucket_secs": 3600},
        "1w":  {"window_secs": 86400 * 7,   "bucket_secs": 14400},
        "1m":  {"window_secs": 86400 * 30,  "bucket_secs": 86400},
        "all": {"window_secs": None,        "bucket_secs": 86400},
    }.get(tf, {"window_secs": None, "bucket_secs": 86400})
    bucket_secs = tf_config["bucket_secs"]
    window_secs = tf_config["window_secs"]

    # window-aware safety cap so wide TFs don't truncate (spec §13)
    SNAPSHOT_SECS = config.POLL_SECONDS
    if window_secs is not None:
        n = max(n, int(window_secs / SNAPSHOT_SECS) + 100)
    else:
        n = max(n, 200000)

    now_dt = datetime.now(timezone.utc)
    window_start_dt = (datetime.fromtimestamp(now_dt.timestamp() - window_secs, tz=timezone.utc)
                       if window_secs is not None else None)
    cutoff_iso = window_start_dt.isoformat() if window_start_dt else None

    points_raw = tail_jsonl(EQUITY_LOG, n=n) or []
    points_in_window = [p for p in points_raw if (p.get("ts") or "") >= cutoff_iso] \
        if cutoff_iso else points_raw

    def _ts_epoch(p):
        try:
            return datetime.fromisoformat(str(p.get("ts", "")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return 0.0

    by_bucket: dict[int, dict] = {}
    for p in points_in_window:
        ts = _ts_epoch(p)
        if ts <= 0:
            continue
        bucket = int(ts // bucket_secs)
        cur = by_bucket.get(bucket)
        if cur is None or _ts_epoch(cur) <= ts:
            by_bucket[bucket] = p
    points = [by_bucket[k] for k in sorted(by_bucket.keys())]

    # Events: Libration emits ENTRY + EXIT(type=TRAIL|HARD_STOP) in trade_log.
    trade_events_raw = tail_jsonl(TRADE_LOG, n=n) or []
    events = []
    for ev in trade_events_raw:
        ts = ev.get("ts") or ev.get("processed_at")
        if not ts or (cutoff_iso and ts < cutoff_iso):
            continue
        action = ev.get("action") or ""
        asset = ev.get("asset") or ""
        if action == "ENTRY":
            events.append({"ts": ts, "type": "ENTRY", "asset": asset,
                           "label": ev.get("label") or f"ENTRY {asset}"})
        elif action == "EXIT":
            sub = ev.get("type") or "EXIT"
            pnl = ev.get("net_pct")
            try:
                label = ev.get("label") or f"{sub} {asset} {pnl:+.2f}%"
            except (TypeError, ValueError):
                label = ev.get("label") or f"{sub} {asset}"
            etype = "TRAIL" if sub == "TRAIL" else ("HARD_STOP" if sub == "HARD_STOP" else "EXIT")
            events.append({"ts": ts, "type": etype, "asset": asset, "label": label})

    if window_start_dt:
        window_start_out = window_start_dt.isoformat()
    elif points:
        window_start_out = points[0].get("ts")
    else:
        window_start_out = now_dt.isoformat()

    return jsonify({
        "tf": tf,
        "bucket_seconds": bucket_secs,
        "window_start": window_start_out,
        "window_end": now_dt.isoformat(),
        "empty": len(points) < 2,
        "points": points,
        "events": events,
    })
