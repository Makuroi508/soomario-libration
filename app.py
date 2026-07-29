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
import os
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
    from datetime import datetime, timezone

    from utils import setup_logging, iso, save_json, tg_notify
    setup_logging()
    logger = logging.getLogger("app")
    print("[boot] utils + logging OK", flush=True)

    import config
    from config import POLL_SECONDS, DASHBOARD_PORT, short_name, EQUITY_LOG, TRADE_LOG, summary as config_summary
    print("[boot] config OK", flush=True)

    from hl_client import HLClient, build_asset_meta
    from db import DB
    from position_manager import PositionManager
    from exit_manager import ExitManager
    from shadow import ShadowTracker
    import universe
    import signals
    from api import app as flask_app, attach_state
    print("[boot] modules OK", flush=True)
except Exception as e:
    print("=" * 60, flush=True)
    print(f"[boot] ❌ FATAL IMPORT ERROR: {type(e).__name__}: {e}", flush=True)
    traceback.print_exc()
    sys.stdout.flush(); sys.stderr.flush()
    raise


def _challenge_snapshot(pm, db):
    """The three numbers that decide a prop challenge: progress to target, room
    to today's floor, room to the PERMANENT floor. Mirrors the venue's own header
    (used / allowed) so the two can be cross-checked at a glance — if they ever
    disagree, the venue's ledger is the one that counts."""
    if config.EXCHANGE not in ("propr", "foxify"):
        return None
    c = pm.client
    try:
        eq = pm._risk_equity()
        rules = pm.venue_rules()
        hwm = c.high_water_mark() if hasattr(c, "high_water_mark") else None
    except Exception as e:                                   # never break the tick
        logger.warning(f"challenge snapshot failed: {e}")
        return None
    if not eq or eq <= 0:
        return None
    acct = db.account()
    init = getattr(c, "_initial_balance", None) or acct.get("inception") or eq
    day_base = acct.get("daily_baseline") or eq
    max_dd, dd_type = pm.dd_limit()
    v_daily = rules.get("daily_loss_pct") or config.DAILY_DD_PCT
    anchor = (hwm or init) if dd_type == "trailing" else init

    daily_used = max(0.0, day_base - eq)
    daily_allow = day_base * v_daily / 100
    dd_used_pct = max(0.0, (anchor - eq) / anchor * 100) if anchor else 0.0
    target = rules.get("target_pct")
    prog = (eq - init) / init * 100 if init else 0.0
    return {
        "venue": "PROPR",
        "name": rules.get("challenge"),
        "phase": rules.get("phase_order"),
        "phases_total": rules.get("phases_total"),
        "venue_equity": round(eq, 2),
        "initial": round(init, 2),
        "hwm": round(hwm, 2) if hwm else None,
        "target_pct": target,
        "progress_pct": round(prog, 3),
        "daily_used": round(daily_used, 2),
        "daily_allowed": round(daily_allow, 2),
        "daily_limit_pct": v_daily,
        "daily_guard_pct": round(pm.daily_limit_pct(), 2),
        "daily_floor": round(day_base * (1 - v_daily / 100), 2),
        "dd_used_pct": round(dd_used_pct, 3),
        "dd_limit_pct": max_dd,
        "dd_type": dd_type,
        "dd_anchor": round(anchor, 2) if anchor else None,
        "dd_floor": round(anchor * (1 - max_dd / 100), 2) if anchor else None,
        "dd_guard_floor": round(anchor * (1 - max(max_dd - config.DD_GUARD_MARGIN, 0.25) / 100), 2) if anchor else None,
    }


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
            "notional": p["notional"], "margin": p["margin"], "mark": mark, "upnl": upnl,
            "peak": p["peak"], "stop": eff_stop, "hard_stop": p["hard_stop"],
            "trail_active": bool(p["trail_active"]), "opened_at": p["opened_at"],
            "reduced": p["coin"].upper() in config.WATCH_SET,
        })
    acct = db.account()
    # inception is a fixed baseline (backfilled at boot). NEVER fall back to
    # daily_baseline — that resets every UTC day and would zero out returns.
    inception = acct.get("inception") or equity or 0.0
    # Realized comes from the durable trade ledger, not the mutable accumulator,
    # so a baseline reset can't wipe it. Total $ = realized + open unrealized,
    # which is inception-independent and therefore robust across redeploys.
    realized_pnl = db.realized_pnl()
    total_pnl = realized_pnl + (total_upnl or 0.0)
    day = None
    if acct.get("inception_ts"):
        try:
            d0 = datetime.fromisoformat(acct["inception_ts"])
            day = (datetime.now(timezone.utc) - d0).days + 1
        except (ValueError, TypeError):
            day = None
    save_json(config.STATUS_FILE, {
        "ts": iso(),
        "name": config.NAME,
        "config": config_summary(),
        "equity": round(equity, 2),
        "total_upnl": round(total_upnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / inception * 100, 2) if inception else 0.0,
        "inception": round(inception, 2),
        "day": day,
        "open_positions": len(positions),
        "max_concurrent": pm.max_concurrent,
        "daily_halt": bool(acct["daily_halt"]),
        "daily_baseline": acct["daily_baseline"],
        "positions": positions,
        "challenge": _challenge_snapshot(pm, db),
    })


def _universe_refresh(hl):
    """Evaluate ATR%/liquidity and write universe.json for the Universe tab.
    Informational by default; only narrows the live set if UNIVERSE_AUTOFILTER=1."""
    try:
        inc, rep = universe.evaluate(hl)
        universe.log_report(rep)
        save_json(config.STATE_DIR / "universe.json", {"ts": iso(), "report": rep})
        if config.UNIVERSE_AUTOFILTER and inc:
            config.COINS = [c for c in config.COINS if c in inc]
            logger.info(f"universe autofilter ON -> trading {config.COINS}")
    except Exception as e:
        logger.warning(f"universe eval skipped: {e}")


def _repair_phantom_closes(hl, db):
    """One-time recovery for phantom -10% closes booked during an API outage.
    Removes only provably-fabricated closes: coin still open on the exchange at
    the same entry, booked at the hard-stop level. REPAIR_PHANTOM_COINS may name
    extra coins to clear (e.g. one that has since genuinely flattened)."""
    live = hl.get_positions()
    if live is None:
        logger.warning("repair: exchange read failed — skipping phantom-close repair.")
        return
    live_by_coin = {}
    for p in live:
        coin = short_name(str(p.get("coin", ""))).upper()
        try:
            entry = float(p.get("entryPx") or 0)
        except (TypeError, ValueError):
            entry = 0.0
        if coin and entry > 0:
            live_by_coin[coin] = entry
    forced = [c.strip().upper() for c in os.getenv("REPAIR_PHANTOM_COINS", "").split(",") if c.strip()]
    removed = db.delete_phantom_closes(live_by_coin, forced_coins=forced)
    if removed:
        for c, ex, net in removed:
            logger.warning(f"🩺 repair: removed phantom close {c} exit ${ex} net {net:+.2f}%")
        tg_notify(f"🩺 Repaired {len(removed)} phantom close(s) wrongly booked during the API "
                  f"outage; realized P&L corrected.", level="info")
    else:
        logger.info("repair: no phantom closes matched.")


def _write_jsonl(path, lines):
    """Atomically (re)write a JSONL file from scratch."""
    import json as _json
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        for e in lines:
            e = {**e, "ts": e.get("ts") or iso()}
            f.write(_json.dumps(e, default=str) + "\n")
    os.replace(tmp, path)


def _rebuild_logs_from_ledger(db):
    """Rebuild the equity-curve logs from the durable trade ledger. Because the
    phantom trades were already removed from the ledger, the rebuilt curve simply
    excludes them — no dip, no phantom hard-stop marker. The equity line becomes
    inception + cumulative realized PnL at each close; live ticks append the
    current (realized + unrealized) tip afterward."""
    acct = db.account()
    inception = acct.get("inception") or 0.0
    inception_ts = acct.get("inception_ts") or iso()
    trades = db._conn.execute(
        "SELECT coin, side, entry, exit, qty, fee, net_pct, exit_reason, opened_at, closed_at "
        "FROM trades ORDER BY COALESCE(closed_at, '') ASC, id ASC").fetchall()

    eq_lines = [{"ts": inception_ts, "equity": round(inception, 4), "active_positions": 0}]
    cum = 0.0
    for t in trades:
        sign = 1.0 if t["side"] == "long" else -1.0
        try:
            net_pnl = (float(t["exit"]) - float(t["entry"])) * sign * float(t["qty"] or 0) - float(t["fee"] or 0)
        except (TypeError, ValueError):
            net_pnl = 0.0
        cum += net_pnl
        eq_lines.append({"ts": t["closed_at"] or inception_ts,
                         "equity": round(inception + cum, 4), "active_positions": 0})

    ev_lines = []
    for t in trades:
        coin = t["coin"].upper()
        ev_lines.append({"ts": t["opened_at"] or inception_ts, "action": "ENTRY",
                         "asset": coin, "side": t["side"], "entry": t["entry"],
                         "qty": t["qty"], "label": f"ENTRY {coin}"})
        net = t["net_pct"] or 0.0
        ev_lines.append({"ts": t["closed_at"] or inception_ts, "action": "EXIT",
                         "type": t["exit_reason"] or "EXIT", "asset": coin, "side": t["side"],
                         "net_pct": net, "label": f"EXIT {coin} {net:+.2f}%"})
    ev_lines.sort(key=lambda e: e.get("ts") or "")

    _write_jsonl(EQUITY_LOG, eq_lines)
    _write_jsonl(TRADE_LOG, ev_lines)
    logger.warning(f"🩺 rebuilt curve from ledger: {len(trades)} trades -> {len(eq_lines)} "
                   f"equity points, {len(ev_lines)} events. Realized-equity ${inception + cum:.2f}")
    tg_notify(f"🩺 Equity curve rebuilt from the trade ledger ({len(trades)} trades) — "
              f"the phantom dip is gone.", level="info")


def run_worker():
    time.sleep(3)  # let Flask bind first
    logger.info("═" * 60)
    logger.info(f"  LIBRATION WORKER STARTING — {config_summary()}")
    logger.info("═" * 60)

    if config.EXCHANGE == "propr":
        from propr_client import ProprClient
        hl = ProprClient()
    elif config.EXCHANGE == "foxify":
        from foxify_client import FoxifyClient
        hl = FoxifyClient()
    else:
        hl = HLClient()
    if not config.PAPER:
        if not hl.init_sdk():
            logger.error(f"🛑 {config.EXCHANGE} init failed — worker will not start. "
                         f"Fix creds and redeploy.")
            return
        # ProprClient/FoxifyClient already seed this in init_sdk(); rebuilding
        # here doubles the meta fan-out at boot for no gain.
        hl.asset_meta = getattr(hl, "asset_meta", None) or build_asset_meta()
        if config.EXCHANGE in ("propr", "foxify") and hasattr(hl, "equity_breakdown"):
            logger.info(f"💰 equity components: {hl.equity_breakdown()}")
            if hasattr(hl, "challenge_rules"):
                _r = hl.challenge_rules()
                logger.info(f"📋 venue rules: {_r}")
                _vd, _vx = _r.get("daily_loss_pct"), _r.get("max_dd_pct")
                if _vd and config.DAILY_DD_PCT >= _vd:
                    logger.error(f"⚠️  DAILY_DD_PCT={config.DAILY_DD_PCT} is NOT tighter than the "
                                 f"venue's {_vd}% daily limit — the guard would fire too late")
                if _vx and config.MAX_DD_PCT and config.MAX_DD_PCT > _vx:
                    logger.error(f"⚠️  MAX_DD_PCT={config.MAX_DD_PCT} exceeds the venue's {_vx}% "
                                 f"— the guard floor sits BELOW the real breach point")
        logger.info(f"📐 asset_meta loaded for {len(hl.asset_meta)} symbols")
    db = DB()
    pm = PositionManager(hl, db)
    em = ExitManager(hl, db, pm)
    shadow = ShadowTracker(db)
    pm.ensure_seeded()
    # One-time incident repair: remove phantom closes (booked when a flaky API
    # read made live positions look closed). Gated by env REPAIR_PHANTOM_CLOSES=1;
    # only deletes a "closed" trade when that coin is STILL OPEN on the exchange,
    # which proves the close was fabricated. Remove the env var after one run.
    if os.getenv("REPAIR_PHANTOM_CLOSES") == "1":
        _repair_phantom_closes(hl, db)
    if os.getenv("REBUILD_CURVE") == "1":
        _rebuild_logs_from_ledger(db)
    # Self-heal: re-adopt any open exchange position the DB has lost, so the bot
    # manages it instead of abandoning it (or doubling it). Runs every boot.
    pm.adopt_unmanaged()
    attach_state(db)  # let the API read from this DB handle's file

    # Universe evaluation (informational). Off by default; only filters if asked.
    _universe_refresh(hl)

    tg_notify(f"Libration worker STARTED — equity ${pm.equity():.2f}, "
              f"{len(config.COINS)} coins, mode {config_summary()['mode']}", level="info")

    last_uni = time.time()
    while True:
        try:
            t0 = time.time()
            tick(hl, db, pm, em, shadow)
            if time.time() - last_uni > 3600:   # refresh universe hourly
                _universe_refresh(hl)
                last_uni = time.time()
            time.sleep(max(1.0, POLL_SECONDS - (time.time() - t0)))
        except KeyboardInterrupt:
            logger.info("🛑 worker interrupted"); break
        except Exception as e:
            logger.error(f"❌ tick crashed: {e}", exc_info=True)
            time.sleep(POLL_SECONDS)


# Bar length in ms, for gating the candle pull to the bar boundary (see tick()).
_INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


def tick(hl, db, pm, em, shadow):
    pm.maybe_reset_daily()
    now_ms = int(__import__("time").time() * 1000)

    # one cheap price poll for trailing + marks. Include any open-position coin
    # that's no longer in COINS (dropped from the tracked set after the position
    # was opened) so it still gets a live mark — and therefore still gets
    # trailed, backstopped, and marked on the dashboard until it exits.
    open_coins = [p["coin"] for p in db.open_positions()]
    marks = hl.get_all_prices(extra=open_coins) or {}

    # ── entries: evaluate the RSI cross only on a freshly closed 4h bar ──
    #
    # The candle pull is GATED on the bar boundary. RSI is computed from closed
    # bars only, so between two 4h closes there is no new information — pulling
    # 200 candles per coin every tick just to re-read the same timestamp was the
    # single largest source of Hyperliquid rate-limit pressure, and with several
    # services sharing one egress IP it is what produces the 429s that zero out
    # the universe screen (every coin reads vol $0.0M and nothing can trade).
    #
    # At 20 coins and POLL_SECONDS=45 the old path was ~27 candle calls/minute
    # per service. Gated, it is 20 calls per 4h bar — a >300x reduction — and
    # the entry logic is bit-identical because a mid-bar fetch could never have
    # produced a new signal anyway.
    _bar_ms = _INTERVAL_MS.get(config.RSI_TF, 4 * 3_600_000)
    _expect_last_close = (now_ms // _bar_ms) * _bar_ms - _bar_ms

    for coin in config.COINS:
        try:
            _st = db.get_rsi_state(coin)
            _seen = int(_st["last_closed_4h_ts"]) if _st and _st.get("last_closed_4h_ts") else None
            # Already hold the newest closed bar for this coin → nothing to learn.
            # Open positions are unaffected: trailing and the stop backstop run
            # off get_all_prices() above, which still polls every tick.
            if _seen is not None and _seen >= _expect_last_close:
                continue
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
            if seen is None:
                # Cold start: prime state but do NOT trade a cross that completed
                # before we were watching — its close price is stale (the bar may
                # have closed up to ~4h ago, and we'd enter at the current, often
                # run-up price). Trade only crosses observed live, so entries match
                # the backtest's close fills. After this, state persists on the
                # volume so reboots don't re-prime.
                logger.info(f"    · primed {coin} — no cold-start entry on pre-existing bar")
                continue
            if not signals.new_closed_bar(seen, last_ts):
                continue  # already evaluated this bar — no double-fire
            sig = signals.entry_signal(rsi[-2], rsi[-1], config.LONG_LEVEL, config.SHORT_LEVEL)
            if sig:
                price = marks.get(coin) or closes[-1]
                pos = pm.maybe_enter(coin, sig, price)
                if pos:
                    shadow.on_open(pos)   # start the counterfactual A/B on this entry
        except Exception as e:
            logger.warning(f"entry eval {coin} failed: {e}")

    # ── reconcile FIRST: book any position the exchange already closed (native
    # trigger filled) and drop it from the open set, so the trailing/backstop
    # pass below never acts on a position that's already flat. ──
    em.reconcile()

    # ── trailing management on (still) open positions + shadow advancement ──
    for p in db.open_positions():
        price = marks.get(p["coin"])
        if price:
            em.manage(p, price)
    for coin, price in marks.items():
        shadow.on_price(coin, price)   # advance/close any open shadows on observed price

    # ── shadow reap + daily-DD + snapshots ──
    open_coins = {p["coin"] for p in db.open_positions()}
    shadow.reap(open_coins, marks)
    pm.check_max_dd(em)      # permanent limit first — it outranks the daily one
    pm.check_daily_dd(em)
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
