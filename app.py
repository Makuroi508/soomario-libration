"""
Soomario Libration — Entry point
═════════════════════════════════
Flask dashboard on the main thread; a daemon worker that ticks every
POLL_SECONDS. Same failure posture as Aphelion:
  • boot trace prints before anything can die silently (Railway logs)
  • worker tick crash → logged, loop continues next interval
  • Flask crash → Railway restarts the container

Each tick the worker:
  1. rolls the daily baseline at UTC midnight
  2. per coin: pull 4h candles, drop the forming bar, compute RSI, and only on
     a NEWLY closed bar evaluate the cross (50 long / 40 short) → maybe_enter
  3. manage trailing stops on open positions against a single live price poll
  4. reconcile against the exchange (book any stop that filled out-of-band)
  5. check the 5% daily-DD halt
  6. snapshot equity (dashboard curve) + write status.json (dashboard reads it)
"""
import sys
import traceback

print("=" * 60, flush=True)
print("LIBRATION — BOOT TRACE", flush=True)
print(f"  python: {sys.version}", flush=True)
print("=" * 60, flush=True)

try:
    import logging
    import threading
    import time

    from utils import setup_logging, iso, save_json, tg_notify
    setup_logging()
    logger = logging.getLogger("app")
    print("[boot] utils + logging OK", flush=True)

    import config
    from config import POLL_SECONDS, DASHBOARD_PORT, summary as config_summary
    print("[boot] config OK", flush=True)

    from hl_client import HLClient
    from db import DB
    from position_manager import PositionManager
    from exit_manager import ExitManager
    import signals
    from api import app as flask_app, attach_state
    print("[boot] modules OK", flush=True)
except Exception as e:
    print("=" * 60, flush=True)
    print(f"[boot] ❌ FATAL IMPORT ERROR: {type(e).__name__}: {e}", flush=True)
    traceback.print_exc()
    sys.stdout.flush(); sys.stderr.flush()
    raise


def _write_status(db, pm, equity, total_upnl, marks):
    positions = []
    for p in db.open_positions():
        mark = marks.get(p["coin"])
        upnl = None
        if mark:
            move = (mark - p["entry"]) if p["side"] == "long" else (p["entry"] - mark)
            upnl = round(move * p["qty"], 4)
        eff_stop = p["trail_stop"] if p["trail_stop"] is not None else p["hard_stop"]
        positions.append({
            "coin": p["coin"], "side": p["side"], "entry": p["entry"], "qty": p["qty"],
            "notional": p["notional"], "mark": mark, "upnl": upnl,
            "peak": p["peak"], "stop": eff_stop, "hard_stop": p["hard_stop"],
            "trail_active": bool(p["trail_active"]), "opened_at": p["opened_at"],
        })
    acct = db.account()
    save_json(config.STATUS_FILE, {
        "ts": iso(),
        "name": config.NAME,
        "config": config_summary(),
        "equity": round(equity, 2),
        "total_upnl": round(total_upnl, 2),
        "open_positions": len(positions),
        "max_concurrent": pm.max_concurrent,
        "daily_halt": bool(acct["daily_halt"]),
        "daily_baseline": acct["daily_baseline"],
        "positions": positions,
    })


def run_worker():
    time.sleep(3)  # let Flask bind first
    logger.info("═" * 60)
    logger.info(f"  LIBRATION WORKER STARTING — {config_summary()}")
    logger.info("═" * 60)

    hl = HLClient()
    if not config.PAPER:
        if not hl.init_sdk():
            logger.error("🛑 HL SDK init failed — worker will not start. Fix creds and redeploy.")
            return
        hl.build_asset_meta()
    db = DB()
    pm = PositionManager(hl, db)
    em = ExitManager(hl, db, pm)
    pm.ensure_seeded()
    attach_state(db)  # let the API read from this DB handle's file

    tg_notify(f"Libration worker STARTED — equity ${pm.equity():.2f}, "
              f"{len(config.COINS)} coins, mode {config_summary()['mode']}", level="info")

    while True:
        try:
            t0 = time.time()
            tick(hl, db, pm, em)
            time.sleep(max(1.0, POLL_SECONDS - (time.time() - t0)))
        except KeyboardInterrupt:
            logger.info("🛑 worker interrupted"); break
        except Exception as e:
            logger.error(f"❌ tick crashed: {e}", exc_info=True)
            time.sleep(POLL_SECONDS)


def tick(hl, db, pm, em):
    pm.maybe_reset_daily()
    now_ms = int(__import__("time").time() * 1000)

    # one cheap price poll for trailing + marks
    marks = hl.get_all_prices() or {}

    # ── entries: evaluate the RSI cross only on a freshly closed 4h bar ──
    for coin in config.COINS:
        try:
            candles = hl.fetch_candles(coin, config.RSI_TF, config.CANDLE_LIMIT)
            closed = signals.closed_candles(candles, now_ms)
            if len(closed) < config.RSI_LEN + 2:
                continue
            last_ts = closed[-1]["t"]
            st = db.get_rsi_state(coin)
            seen = int(st["last_closed_4h_ts"]) if st and st.get("last_closed_4h_ts") else None
            closes = [c["c"] for c in closed]
            rsi = signals.wilder_rsi(closes, config.RSI_LEN)
            db.set_rsi_state(coin, last_ts, rsi[-1])
            if not signals.new_closed_bar(seen, last_ts):
                continue  # already evaluated this bar — no double-fire
            sig = signals.entry_signal(rsi[-2], rsi[-1], config.LONG_LEVEL, config.SHORT_LEVEL)
            if sig:
                price = marks.get(coin) or closes[-1]
                pm.maybe_enter(coin, sig, price)
        except Exception as e:
            logger.warning(f"entry eval {coin} failed: {e}")

    # ── trailing management on open positions ──
    for p in db.open_positions():
        price = marks.get(p["coin"])
        if price:
            em.manage(p, price)

    # ── live reconcile + daily-DD + snapshots ──
    em.reconcile()
    pm.check_daily_dd()
    equity = pm.equity()
    total_upnl = 0.0
    for p in db.open_positions():
        mk = marks.get(p["coin"])
        if mk:
            move = (mk - p["entry"]) if p["side"] == "long" else (p["entry"] - mk)
            total_upnl += move * p["qty"]
    db.snapshot_equity(equity, extra={"total_upnl": round(total_upnl, 4)})
    _write_status(db, pm, equity, total_upnl, marks)
    logger.info(f"tick done — equity ${equity:.2f} upnl ${total_upnl:+.2f} "
                f"open {len(db.open_positions())}/{pm.max_concurrent}")


def main():
    logger.info("═" * 60)
    logger.info(f"  LIBRATION launching — ts={iso()}")
    logger.info("═" * 60)
    t = threading.Thread(target=run_worker, daemon=True, name="libration-worker")
    t.start()
    logger.info("🛰  worker thread launched")
    logger.info(f"🌐 dashboard: 0.0.0.0:{DASHBOARD_PORT}")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    flask_app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
