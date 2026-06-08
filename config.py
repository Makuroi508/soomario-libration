"""
Soomario Libration — Configuration
═══════════════════════════════════════
All params via env with validated defaults. The frozen strategy values
(RSI 14 / 4h, cross 50 long / 40 short, 0.55% trail, 10% hard stop, 5% daily
DD halt, 20% notional, 2x) are walk-forward validated — do not re-optimize.

Reuses Aphelion's HL plumbing conventions (HL creds, asset_dex/hl_symbol,
isolated-margin routing, JSONL state logs) so the dashboard equity curve and
the hl_client drop in with no surprises.
"""
import os
from pathlib import Path


def _f(name, default):
    return float(os.getenv(name, str(default)))


def _i(name, default):
    return int(os.getenv(name, str(default)))


def _b(name, default="0"):
    return os.getenv(name, default) == "1"


# ─── Paths / state ──────────────────────────────────────────────
NAME = "Soomario Libration"

BASE_DIR  = Path(__file__).parent
STATE_DIR = Path(os.getenv("STATE_PATH", str(BASE_DIR / "state")))
STATE_DIR.mkdir(exist_ok=True, parents=True)

LOG_FILE        = STATE_DIR / "libration.log"
DB_PATH         = Path(os.getenv("DB_PATH", str(STATE_DIR / "libration.db")))
ASSET_META_FILE = STATE_DIR / "asset_meta.json"
# Append-only logs read by the dashboard (EQUITY_CURVE_SPEC.md v1.2)
EQUITY_LOG = STATE_DIR / "equity_log.jsonl"
TRADE_LOG  = STATE_DIR / "trade_log.jsonl"
SIGNAL_LOG = STATE_DIR / "signal_log.jsonl"
STATUS_FILE = STATE_DIR / "status.json"   # worker writes each tick; API reads

# ─── Hyperliquid credentials (reused names from Aphelion) ───────
HL_ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS", "").strip()
HL_PRIVATE_KEY     = os.getenv("HL_PRIVATE_KEY", "").strip()
HL_IS_VAULT        = _b("HL_IS_VAULT")
HL_API_URL         = "https://api.hyperliquid.xyz/info"

# ─── Run modes ──────────────────────────────────────────────────
# DRY_RUN  : SDK skips signing; reads still work; orders are simulated by hl_client.
# PAPER    : the executor itself simulates fills against live prices and never
#            touches the exchange order path (build spec §14 step 7). Use PAPER
#            for a dry run on real market data before going live.
DRY_RUN         = _b("DRY_RUN")
PAPER           = _b("PAPER")
ENTRIES_ENABLED = _b("ENTRIES_ENABLED", "1")
TRAIL_ENABLED   = _b("TRAIL_ENABLED", "1")

# Paper-mode accounting (only used when PAPER=1). The executor simulates fills
# against live prices and tracks its own equity, seeded once at first run.
PAPER_START_EQUITY = _f("PAPER_START_EQUITY", 1000)
PAPER_SLIPPAGE_PCT = _f("PAPER_SLIPPAGE_PCT", 0.0)  # adverse fill padding for realism

# ─── Shadow trail A/B (counterfactual — never trades) ───────────
# Live trades TRAIL_PCT; shadow tracks what these tighter trails WOULD have done
# on the same entries/prices. Decide the trail from live data, not the backtest.
SHADOW_TRAILS = [float(x) for x in os.getenv("SHADOW_TRAILS", "0.3,0.4").split(",") if x.strip()]
# Round-trip friction charged to shadow exits so they're a FAIR comparison.
# Placeholder = backtest assumption; update from the live realized-friction KPI
# (or wire userFills) once Phase 1 produces real numbers. Spec §7b.
MEASURED_FRICTION_PCT = _f("MEASURED_FRICTION_PCT", 0.5)

# ─── Universe filter (universe.py) ──────────────────────────────
MIN_ATR_PCT       = _f("MIN_ATR_PCT", 3.0)            # avg daily TR% floor (volatile alts only)
MIN_DAILY_VOL_USD = _f("MIN_DAILY_VOL_USD", 10_000_000)
UNIVERSE_AUTOFILTER = _b("UNIVERSE_AUTOFILTER")       # off: log report only, never silently drop

# ─── Frozen strategy params (walk-forward validated) ────────────
RSI_LEN       = _i("RSI_LEN", 14)
RSI_TF        = os.getenv("RSI_TF", "4h")
LONG_LEVEL    = _f("LONG_LEVEL", 50)     # crossover up  -> long
SHORT_LEVEL   = _f("SHORT_LEVEL", 40)    # crossunder down -> short
TRAIL_PCT     = _f("TRAIL_PCT", 0.55)    # activate +0.55%, trail 0.55% behind peak
HARD_STOP_PCT = _f("HARD_STOP_PCT", 10)  # native resting trigger; never moved against
DAILY_DD_PCT  = _f("DAILY_DD_PCT", 5)    # halt new entries once down 5% on the UTC day

# ─── Sizing / leverage / concurrency ────────────────────────────
LEVERAGE      = _f("LEVERAGE", 2)        # launch at 2x (no liquidation risk vs 10% stop)
NOTIONAL_FRAC = _f("NOTIONAL_FRAC", 0.20)  # 20% of equity notional per position
# Concurrency cap = leverage / notional_frac. 2x / 20% -> 10. This is the lever
# that turns ~56% fill rate (1x) into ~85% (2x).
MAX_CONCURRENT = int(LEVERAGE / NOTIONAL_FRAC + 1e-9)

# ─── Universe (volatile alt perps; main DEX only — no HIP-3) ────
_DEFAULT_COINS = "SOL,AVAX,LINK,NEAR,ADA,DOGE,BCH,LTC,DOT,ATOM"
COINS = [c.strip().upper() for c in os.getenv("COINS", _DEFAULT_COINS).split(",") if c.strip()]

# ─── Timing ─────────────────────────────────────────────────────
POLL_SECONDS           = _i("POLL_SECONDS", 120)    # trailing-manage + price poll cadence
RECONCILE_INTERVAL_SEC = _i("RECONCILE_INTERVAL_SEC", 300)
CANDLE_LIMIT           = _i("CANDLE_LIMIT", 200)    # 4h candles to pull per coin for RSI

# ─── Dashboard / logging ────────────────────────────────────────
DASHBOARD_PORT = _i("PORT", 8080)
LOG_LEVEL      = os.getenv("LOG_LEVEL", "INFO")

# Optional Telegram (no-ops if unset) — reused by utils.tg_notify
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ─── HL symbol / DEX helpers (swing universe is all main-perp) ──
# Kept API-compatible with Aphelion's hl_client so that module reuses cleanly.
def asset_dex(asset: str) -> str:
    # Swing universe is main-perp only; no HIP-3 routing.
    return ""


def hl_symbol(asset: str) -> str:
    asset = asset.upper()
    return asset.split(":", 1)[1] if ":" in asset else asset


def short_name(coin: str) -> str:
    return coin.split(":", 1)[1].upper() if ":" in coin else coin.upper()


def active_dexes(assets: list) -> list:
    return sorted({asset_dex(a) for a in assets})


def margin_mode_for(asset: str) -> str:
    # Isolated everywhere — one coin's stop must never cascade into others.
    return "isolated"


def is_cross_for(asset: str) -> bool:
    return False


def summary() -> dict:
    return {
        "name": NAME,
        "coins": COINS,
        "leverage": LEVERAGE,
        "notional_frac": NOTIONAL_FRAC,
        "max_concurrent": MAX_CONCURRENT,
        "rsi": f"{RSI_LEN}@{RSI_TF}",
        "levels": f"L{LONG_LEVEL:.0f}/S{SHORT_LEVEL:.0f}",
        "trail_pct": TRAIL_PCT,
        "hard_stop_pct": HARD_STOP_PCT,
        "daily_dd_pct": DAILY_DD_PCT,
        "mode": "PAPER" if PAPER else ("DRY_RUN" if DRY_RUN else "LIVE"),
        "db": str(DB_PATH),
    }
