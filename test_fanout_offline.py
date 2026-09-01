"""
Offline validation for the fan-out core (exchange.py) — no network, no keys.
Proves the central invariant: ONE canonical HL cross is computed once (rsi_state
in a single shared signal DB, no double-fire) and fanned to TWO fully isolated
venue books, each with its own DB / equity / positions, honoring each venue's
own universe. Uses lightweight fake clients so the focus stays on the fan-out
plumbing; real venue signing/exec is covered by test_pacifica/bulk_offline.
"""
import os, sys, shutil

os.environ["STATE_PATH"] = "/tmp/fanout_test"
os.environ["PAPER"] = "0"            # LIVE path (fake clients, no network)
os.environ["DRY_RUN"] = "0"
os.environ["COINS"] = "SOL,ETH"
shutil.rmtree("/tmp/fanout_test", ignore_errors=True)

import config
import signals
from config import STATE_DIR
from db import DB
from exchange import SignalEngine, VenueBook

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}")


# ── candle construction: a series whose Wilder RSI crosses UP through 50 on its
#    final bar (a long entry). Deterministic — searched, not random. ──
BAR = 14_400_000                       # 4h in ms
BASE_T = 1_700_000_000_000
NOW_MS = BASE_T + 500 * BAR            # far future -> every bar is closed

def _long_cross_closes():
    base = [100.0 - i for i in range(40)]              # downtrend -> RSI floor
    rally = [base[-1] + i * 3 for i in range(1, 40)]   # sharp rally lifts RSI
    closes = base + rally
    rsi = signals.wilder_rsi(closes, config.RSI_LEN)
    for i in range(1, len(closes)):
        if rsi[i - 1] is not None and rsi[i - 1] < 50 <= rsi[i]:
            return closes[:i + 1]      # last bar IS the crossover bar
    raise RuntimeError("failed to construct a long cross")

def candles_from_closes(closes):
    out = []
    for i, c in enumerate(closes):
        t = BASE_T + i * BAR
        out.append({"t": t, "T": t + BAR, "o": c, "h": c, "l": c, "c": c, "v": 1.0})
    return out

_FULL = _long_cross_closes()           # ...ends on the cross bar
_PRE = _FULL[:-1]                       # ...one bar before the cross (for priming)


class FakeHL:
    """Canonical signal + mark source. Serves per-coin candle sets that can be
    advanced from 'pre-cross' to 'cross' between ticks, like real time passing."""
    def __init__(self):
        self.sets = {"SOL": _PRE, "ETH": _PRE}
    def fetch_candles(self, coin, interval="4h", limit=200):
        return candles_from_closes(self.sets.get(coin.upper(), []))
    def get_all_prices(self, extra=None):
        return {"SOL": 200.0, "ETH": 3000.0}


class FakeVenue:
    """Minimal client satisfying the strategy contract on the LIVE path."""
    def __init__(self, equity):
        self._equity = equity
        self._pos = {}                 # coin -> {szi, entryPx}
        self.prices = {"SOL": 200.0, "ETH": 3000.0}
        self.last_open_error = None
    def init_sdk(self): return True
    def get_equity(self): return self._equity
    def get_all_prices(self, extra=None): return dict(self.prices)
    def get_price(self, s): return self.prices.get(str(s).upper())
    def get_positions(self):
        return [{"coin": c, "szi": p["szi"], "entryPx": p["entryPx"], "symbol": c}
                for c, p in self._pos.items()]
    def set_leverage(self, s, lev): return True
    def market_open(self, coin, is_buy, notional, current_price=None):
        px = current_price or self.get_price(coin)
        qty = notional / px
        self._pos[coin.upper()] = {"szi": qty if is_buy else -qty, "entryPx": px}
        return {"filled": True, "avg_price": px, "total_size": qty}
    def market_close(self, coin, size, is_long, current_price=None):
        self._pos.pop(coin.upper(), None)
        return {"filled": True, "avg_price": current_price or self.get_price(coin)}
    def place_stop_market(self, coin, is_long, size, stop_px): return f"stop-{coin}"
    def modify_stop(self, coin, is_long, size, old_id, new_stop): return f"stop-{coin}"
    def cancel_order(self, coin, oid): return True
    def get_user_fills(self, start_ms=None): return []


hl = FakeHL()
signal_db = DB(path=str(STATE_DIR / "signals" / "signals.db"))
engine = SignalEngine(hl, signal_db, ["SOL", "ETH"])

# two isolated books: pacifica trades SOL+ETH, bulk trades SOL only
pac = VenueBook("pacifica", FakeVenue(1200.0), ["SOL", "ETH"], STATE_DIR)
bulk = VenueBook("bulk", FakeVenue(800.0), ["SOL"], STATE_DIR)
pac.boot(); bulk.boot()

print("[1] canonical signal - computed once, primed then fired")
fired0 = engine.compute(NOW_MS)
check("cold start: first observation primes, fires nothing", fired0 == {})
check("rsi_state primed in the shared signal DB", signal_db.get_rsi_state("SOL") is not None)

# a new 4h bar closes -> the cross bar is now the freshly-closed bar
hl.sets = {"SOL": _FULL, "ETH": _FULL}
fired = engine.compute(NOW_MS)
check("fresh bar fires SOL long", fired.get("SOL") == "long")
check("fresh bar fires ETH long", fired.get("ETH") == "long")
fired_again = engine.compute(NOW_MS)
check("no double-fire on the same bar (shared rsi_state)", fired_again == {})

print("[2] fan-out to two isolated books")
pac.step(fired, hl.get_all_prices())
bulk.step(fired, hl.get_all_prices())
pac_open = {p["coin"] for p in pac.db.open_positions()}
bulk_open = {p["coin"] for p in bulk.db.open_positions()}
check("SOL opened on pacifica", "SOL" in pac_open)
check("SOL opened on bulk (one signal -> two books)", "SOL" in bulk_open)
check("ETH opened on pacifica (in its universe)", "ETH" in pac_open)
check("ETH NOT opened on bulk (per-venue universe scoping)", "ETH" not in bulk_open)
check("books hold isolated position sets", pac_open == {"SOL", "ETH"} and bulk_open == {"SOL"})

print("[3] isolated accounting + per-venue state files")
check("equity isolated per venue (1200 vs 800 seed)",
      abs(pac.pm.equity() - 1200.0) < 1e-6 and abs(bulk.pm.equity() - 800.0) < 1e-6)
from utils import load_json, tail_jsonl
pac_st = load_json(STATE_DIR / "pacifica" / "status.json", default={})
bulk_st = load_json(STATE_DIR / "bulk" / "status.json", default={})
check("per-venue status.json written, scoped",
      pac_st.get("venue") == "pacifica" and bulk_st.get("venue") == "bulk"
      and set(bulk_st.get("coins", [])) == {"SOL"})
check("per-venue equity_log.jsonl written",
      len(tail_jsonl(STATE_DIR / "pacifica" / "equity_log.jsonl")) >= 1
      and len(tail_jsonl(STATE_DIR / "bulk" / "equity_log.jsonl")) >= 1)

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
