"""
Soomario Elite — Fleet dashboard
════════════════════════════════
One page over every deployment. A tiny read-only aggregator that polls each
book's EXISTING /api endpoints server-side (no CORS, no changes to the running
services), caches the results, and serves a single combined view.

Why server-side pull rather than a client-side mashup: the fleet's most
important job is noticing a book that has gone QUIET — the recurring lesson of
this project is that a silent failure looks exactly like a calm market. A
browser page fetching cross-origin can't tell "CORS blocked" from "down";
this service can, and renders DOWN/STALE as loudly as a drawdown.

Deploy as its own Railway service from this repo:
    start command:  python app_fleet.py
    FLEET_SOURCES   "HL Vault=https://...;Propr 10k=https://...;Solana=https://..."
                    Labels are free text. Solana fan-out sources are detected
                    via /api/venues and expanded into one entry per venue.
    FLEET_POLL_SEC  refresh interval (default 30)
    PORT            default 8080

No database, no state, no keys. Read-only by construction.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

from utils import setup_logging

setup_logging()
logger = logging.getLogger("fleet")

POLL_SEC = float(os.getenv("FLEET_POLL_SEC", "30"))
TIMEOUT = float(os.getenv("FLEET_TIMEOUT_SEC", "8"))
# A book whose own ts is older than this is STALE even if its API answers —
# the Flask thread can outlive a wedged worker.
STALE_SEC = float(os.getenv("FLEET_STALE_SEC", "600"))

_LOCK = threading.Lock()
_CACHE = {"sources": [], "polled_at": None}


def _parse_sources():
    raw = os.getenv("FLEET_SOURCES", "").strip()
    out = []
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        label, url = part.split("=", 1)
        out.append((label.strip(), url.strip().rstrip("/")))
    return out


def _get(url, path, params=None):
    r = requests.get(url + path, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _iso_age_sec(iso):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except (ValueError, TypeError):
        return None


def _book_entry(label, status, stats):
    """Normalize one book's status+stats into a fleet card."""
    cfg = status.get("config") or {}
    positions = []
    for p in status.get("positions") or []:
        try:
            entry, mark, qty = float(p["entry"]), float(p.get("mark") or 0), float(p["qty"])
            upnl = (mark - entry) * qty * (1 if p.get("side") == "long" else -1) if mark else None
        except (KeyError, TypeError, ValueError):
            upnl = None
        positions.append({"coin": p.get("coin"), "side": p.get("side"),
                          "entry": p.get("entry"), "mark": p.get("mark"),
                          "upnl": round(upnl, 2) if upnl is not None else None,
                          "trail_active": bool(p.get("trail_active"))})
    equity = status.get("equity")
    baseline = status.get("daily_baseline")
    day_pnl = (equity - baseline) if (equity is not None and baseline) else None
    ts_age = _iso_age_sec(status.get("ts"))
    state = "ok"
    if ts_age is not None and ts_age > STALE_SEC:
        state = "stale"
    if status.get("daily_halt"):
        state = "halted"
    if status.get("max_dd_halt"):
        state = "dd-halted"
    return {
        "label": label, "state": state, "ts_age_sec": ts_age,
        "exchange": cfg.get("exchange"), "mode": cfg.get("mode"),
        "equity": equity, "inception": status.get("inception"),
        "total_pnl": status.get("total_pnl"), "total_pnl_pct": status.get("total_pnl_pct"),
        "day_pnl": round(day_pnl, 2) if day_pnl is not None else None,
        "total_upnl": status.get("total_upnl"),
        "open": status.get("open_positions"), "max_concurrent": status.get("max_concurrent"),
        "positions": positions,
        "closed_trades": (stats or {}).get("closed_trades"),
        "realized_friction_pct": (stats or {}).get("realized_friction_pct"),
        "challenge": status.get("challenge"),
    }


def _poll_source(label, url):
    """One deployment -> one or more fleet cards. Never raises."""
    try:
        # A fan-out service exposes /api/venues; expand into one card per
        # venue. Live shape is a dict {"venues": [...], "labels": {...},
        # "default": ...}; older builds returned a bare list. Accept both.
        venues, vlabels = None, {}
        try:
            v = _get(url, "/api/venues")
            if isinstance(v, list) and v:
                venues = [str(x) for x in v]
            elif isinstance(v, dict) and v.get("venues"):
                venues = [str(x) for x in v["venues"]]
                vlabels = v.get("labels") or {}
        except Exception:
            venues = None

        if venues:
            out = []
            for ven in venues:
                st = _get(url, "/api/status", {"venue": ven})
                try:
                    sx = _get(url, "/api/stats", {"venue": ven})
                except Exception:
                    sx = {}
                out.append(_book_entry(
                    "%s · %s" % (label, vlabels.get(ven, ven)), st, sx))
            return out

        st = _get(url, "/api/status")
        try:
            sx = _get(url, "/api/stats")
        except Exception:
            sx = {}
        return [_book_entry(label, st, sx)]
    except Exception as e:
        logger.warning("fleet: %s unreachable: %s", label, e)
        return [{"label": label, "state": "down", "error": str(e)[:160],
                 "equity": None, "day_pnl": None, "open": None, "positions": []}]


def _poller():
    sources = _parse_sources()
    if not sources:
        logger.error("FLEET_SOURCES is empty — nothing to aggregate. "
                     'Set e.g. FLEET_SOURCES="HL Vault=https://...;Propr 10k=https://..."')
    while True:
        t0 = time.time()
        books = []
        for label, url in sources:
            books.extend(_poll_source(label, url))
        with _LOCK:
            _CACHE["sources"] = books
            _CACHE["polled_at"] = datetime.now(timezone.utc).isoformat()
        time.sleep(max(5.0, POLL_SEC - (time.time() - t0)))


app = Flask(__name__)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/api/fleet")
def api_fleet():
    with _LOCK:
        books = list(_CACHE["sources"])
        polled_at = _CACHE["polled_at"]
    eq = [b["equity"] for b in books if isinstance(b.get("equity"), (int, float))]
    dp = [b["day_pnl"] for b in books if isinstance(b.get("day_pnl"), (int, float))]
    tp = [b["total_pnl"] for b in books if isinstance(b.get("total_pnl"), (int, float))]
    op = [b["open"] for b in books if isinstance(b.get("open"), int)]
    attention = [b["label"] for b in books if b.get("state") not in ("ok", None)]
    return jsonify({
        "polled_at": polled_at,
        "books": books,
        "totals": {
            "equity": round(sum(eq), 2) if eq else None,
            "day_pnl": round(sum(dp), 2) if dp else None,
            "total_pnl": round(sum(tp), 2) if tp else None,
            "open": sum(op) if op else 0,
            "books_ok": sum(1 for b in books if b.get("state") == "ok"),
            "books_total": len(books),
            "attention": attention,
        },
    })


@app.route("/")
def index():
    import config
    path = os.path.join(str(config.BASE_DIR), "fleet.html")
    with open(path, encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}


def main():
    threading.Thread(target=_poller, daemon=True, name="fleet-poller").start()
    port = int(os.getenv("PORT", "8080"))
    logger.info("fleet dashboard on 0.0.0.0:%d (%d sources, poll %.0fs)",
                port, len(_parse_sources()), POLL_SEC)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
