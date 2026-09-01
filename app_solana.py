"""
Soomario Libration-Solana - entry point (fan-out worker)
========================================================
Runs ONE worker that computes the canonical Libration RSI signal per tick from
Hyperliquid candles, then fans it to isolated per-venue books:
  - Pacifica (LIVE, native stops)
  - Bulk     (TESTNET paper, backstop-only stops)

Deploy as a separate Railway service ("Libration-Solana") with its own volume.
The live Hyperliquid Libration bot is a different deployment and is untouched.

Key env:
  ENABLED_VENUES   default "pacifica,bulk"
  PACIFICA_COINS   per-venue universe (pruned to what Pacifica lists at boot)
  BULK_COINS       per-venue universe (pruned to what Bulk lists at boot)
  WATCH            reduced-size coins (applies size_mult across venues)
  plus each adapter's own creds (see pacifica_client.py / bulk_client.py headers)
"""
import os
import sys
import traceback

print("=" * 60, flush=True)
print("LIBRATION-SOLANA - BOOT TRACE", flush=True)
print(f"  python: {sys.version}", flush=True)
print("=" * 60, flush=True)

try:
    import logging
    import threading
    import time

    from utils import setup_logging, iso, load_json, tail_jsonl, tg_notify
    setup_logging()
    logger = logging.getLogger("app_solana")

    import config
    from config import POLL_SECONDS, DASHBOARD_PORT, STATE_DIR
    from db import DB
    from exchange import make_client, SignalEngine, VenueBook

    from flask import Flask, jsonify, request
    print("[boot] modules OK", flush=True)
except Exception as e:
    print(f"[boot] FATAL IMPORT ERROR: {type(e).__name__}: {e}", flush=True)
    traceback.print_exc()
    raise


# Venues trade THE BOOK by default -- config.COINS, the same eleven-coin list
# every other Elite deployment runs, with per-coin params and probation sizing.
# The old hardcoded per-venue defaults predated the elite findings and quietly
# resurrected verified-dead coins (AAVE, ADA, AVAX, BCH, ENA, NEAR, WLD, ZEC)
# on Pacifica even when the COINS env was set correctly, because _venue_coins
# never consulted it. A venue-specific env (PACIFICA_COINS / BULK_COINS) is
# still honoured for deliberate divergence -- e.g. Bulk testnet experiments.
# Coins a venue does not list are dropped at boot against live market_info.
DEFAULT_PACIFICA = ",".join(config.COINS)
DEFAULT_BULK = ",".join(config.COINS)

_BOOKS = {}   # name -> VenueBook (shared with the Flask reader)


def _venue_coins(name: str):
    if name == "pacifica":
        raw = os.getenv("PACIFICA_COINS", DEFAULT_PACIFICA)
    elif name == "bulk":
        raw = os.getenv("BULK_COINS", DEFAULT_BULK)
    else:
        raw = os.getenv(f"{name.upper()}_COINS", "")
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


def run_worker():
    time.sleep(3)  # let Flask bind first
    logger.info("=" * 60)
    logger.info(f"  LIBRATION-SOLANA WORKER STARTING - {config.summary()}")
    logger.info("=" * 60)

    # Canonical signal + mark source: Hyperliquid, read-only (no keys needed for
    # public candles/prices). asset_meta is loaded from the public /info meta.
    hl = make_client("hyperliquid")
    try:
        from hl_client import build_asset_meta
        hl.asset_meta = build_asset_meta()
        logger.info(f"HL candle/mark source ready ({len(hl.asset_meta)} symbols)")
    except Exception as e:
        logger.warning(f"HL asset_meta load skipped: {e}")

    signal_db = DB(path=str(STATE_DIR / "signals" / "signals.db"))

    enabled = [v.strip().lower() for v in os.getenv("ENABLED_VENUES", "pacifica,bulk").split(",") if v.strip()]
    for name in enabled:
        try:
            client = make_client(name)
        except ValueError as e:
            logger.error(f"skip {name}: {e}"); continue
        if not client.init_sdk():
            logger.error(f"[{name}] init_sdk failed - venue disabled this run.")
            continue
        book = VenueBook(name, client, _venue_coins(name), STATE_DIR)
        book.boot()
        # Bulk borrows HL marks if its native price feed path needs tuning.
        if name == "bulk" and hasattr(client, "set_price_fallback"):
            client.set_price_fallback(lambda extra=None: hl.get_all_prices(extra=extra))
        _BOOKS[name] = book
        tg_notify(f"Libration-Solana [{name}] STARTED - equity ${book.pm.equity():.2f}, "
                  f"{len(book.coins)} coins", level="info")

    if not _BOOKS:
        logger.error("no venues initialized - worker idle. Fix creds/env and redeploy.")
        return

    signal_coins = sorted(set().union(*[b.coins for b in _BOOKS.values()]))
    engine = SignalEngine(hl, signal_db, signal_coins)
    logger.info(f"signal universe ({len(signal_coins)}): {signal_coins}")

    while True:
        try:
            t0 = time.time()
            tick(engine, hl)
            time.sleep(max(1.0, POLL_SECONDS - (time.time() - t0)))
        except KeyboardInterrupt:
            logger.info("worker interrupted"); break
        except Exception as e:
            logger.error(f"tick crashed: {e}", exc_info=True)
            time.sleep(POLL_SECONDS)


def tick(engine, hl):
    now_ms = int(time.time() * 1000)
    # one HL mark poll covering the signal universe + every open coin across books
    open_coins = set()
    for b in _BOOKS.values():
        open_coins |= {p["coin"] for p in b.db.open_positions()}
    hl_marks = hl.get_all_prices(extra=list(open_coins | set(engine.coins))) or {}

    fired = engine.compute(now_ms)        # canonical cross, computed ONCE
    for name, book in _BOOKS.items():
        try:
            book.step(fired, hl_marks)    # isolated per-venue execution + accounting
        except Exception as e:
            logger.error(f"[{name}] step crashed: {e}", exc_info=True)


# The dashboard + full per-venue API (?venue=) live in api_solana. The worker
# writes each venue's state to disk; the API reads it, so there's no shared
# in-process state between them.


def main():
    logger.info("=" * 60)
    logger.info(f"  LIBRATION-SOLANA launching - ts={iso()}")
    logger.info("=" * 60)
    t = threading.Thread(target=run_worker, daemon=True, name="libration-solana-worker")
    t.start()
    from api_solana import app as flask_app
    logger.info(f"dashboard: 0.0.0.0:{DASHBOARD_PORT}")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    flask_app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
