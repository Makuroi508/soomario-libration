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
HL_ACCOUNT_ADDRESS = (os.getenv("HL_ACCOUNT_ADDRESS") or os.getenv("HL_VAULT_ADDRESS") or "").strip()
HL_PRIVATE_KEY     = os.getenv("HL_PRIVATE_KEY", "").strip()
HL_IS_VAULT        = _b("HL_IS_VAULT")
HL_API_URL         = "https://api.hyperliquid.xyz/info"

# ─── Target exchange ────────────────────────────────────────────
# "hyperliquid" (default — unchanged behaviour) or "propr". app.py builds the
# matching client; everything downstream is venue-agnostic. Market data always
# comes from HL's public info endpoint, since Propr settles on Hyperliquid and
# exposes no candle/price endpoints of its own.
EXCHANGE = os.getenv("EXCHANGE", "hyperliquid").strip().lower()

# Foxify / Kitsune — only read when EXCHANGE=foxify. The signal account IS the
# funded account; there is no account-id indirection to get wrong. Kitsune is a
# signal relay: no order ids, no standalone stops, no trailing. The native `sl`
# rides with the entry as the disaster backstop; the 0.55% trail stays
# bot-managed and exits through market_close().
FOXIFY_API_BASE        = os.getenv("FOXIFY_API_BASE", "https://kitsunedev.foxify.trade/api").strip()
FOXIFY_SIGNAL_ID       = os.getenv("FOXIFY_SIGNAL_ID", "").strip()
FOXIFY_PASSPHRASE      = os.getenv("FOXIFY_PASSPHRASE", "").strip()
FOXIFY_LEVERAGE        = float(os.getenv("FOXIFY_LEVERAGE", "2") or 2)
FOXIFY_MAX_DD_PCT      = float(os.getenv("FOXIFY_MAX_DD_PCT", "20") or 20)
FOXIFY_MIN_NOTIONAL    = float(os.getenv("FOXIFY_MIN_NOTIONAL", "10") or 10)
FOXIFY_EQUITY_ADD_UPNL = os.getenv("FOXIFY_EQUITY_ADD_UPNL", "0").strip() in ("1", "true", "yes")

# Propr — only read when EXCHANGE=propr. PROPR_ACCOUNT_ID is required and is
# never auto-discovered: with more than one active challenge attempt, discovery
# can silently route orders to the wrong funded account.
PROPR_API_KEY    = os.getenv("PROPR_API_KEY", "").strip()
PROPR_ACCOUNT_ID = os.getenv("PROPR_ACCOUNT_ID", "").strip()
PROPR_ATTEMPT_ID = os.getenv("PROPR_ATTEMPT_ID", "").strip()

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
# HL alt volumes run far lower than CEXes; this is a thin-dust sanity floor,
# NOT the real selector. Inclusion is decided by the per-coin backtest edge.
MIN_ATR_PCT       = _f("MIN_ATR_PCT", 3.0)            # avg daily TR% floor (volatile alts only)
MIN_DAILY_VOL_USD = _f("MIN_DAILY_VOL_USD", 2_000_000)
UNIVERSE_AUTOFILTER = _b("UNIVERSE_AUTOFILTER")       # off: log report only, never silently drop

# ─── Frozen strategy params (walk-forward validated) ────────────
RSI_LEN       = _i("RSI_LEN", 14)
RSI_TF        = os.getenv("RSI_TF", "4h")
LONG_LEVEL    = _f("LONG_LEVEL", 50)     # crossover up  -> long
SHORT_LEVEL   = _f("SHORT_LEVEL", 40)    # crossunder down -> short
TRAIL_PCT     = _f("TRAIL_PCT", 0.55)    # activate +0.55%, trail 0.55% behind peak
# How many FULL bars after entry before the trail may arm. The TradingView
# strategies this book mirrors cannot arm on the entry bar (strategy.exit sits
# inside `if strategy.position_size != 0`, which is still 0 when the entry
# bar's script body runs), so every position gets one bar to breathe. The
# soomario-elite A/B tested arming-immediately against this on real 1m/1s
# data: immediate arming LOST in all 12 coin x venue combinations -- e.g.
# HYPE +84% vs -21% at measured fees. A 0.55% trail sits inside ordinary bar
# noise; armed at once, it converts normal adverse excursion into exits.
# The 10% hard stop is NOT delayed -- it rests from entry as before. That is
# a deliberate safety deviation from Pine (which has no stop on the entry bar
# either): a 10% stop almost never fires inside one 4h bar in backtest, and
# live it is the only protection against a crash while the trail is unarmed.
TRAIL_ARM_DELAY_BARS = _i("TRAIL_ARM_DELAY_BARS", 1)
_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
               "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800, "1d": 86400}
BAR_SECONDS = _TF_SECONDS.get(RSI_TF, 14400)
TRAIL_ARM_DELAY_SEC = TRAIL_ARM_DELAY_BARS * BAR_SECONDS
HARD_STOP_PCT = _f("HARD_STOP_PCT", 10)  # native resting trigger; never moved against
DAILY_DD_PCT  = _f("DAILY_DD_PCT", 5)    # halt new entries once down 5% on the UTC day
# Halting only stops digging — open positions keep bleeding toward the venue's
# hard daily limit. On a prop account that is what ends the challenge, so the
# guard must also FLATTEN. Default off: Hyperliquid has no breach rule and
# flattening there would change the walk-forward-validated strategy.
DAILY_FLATTEN = _b("DAILY_FLATTEN")      # 1 -> close the book at the halt, then pause

# Take over positions found on the exchange but missing from our book. Safe on a
# dedicated bot account; leave OFF where you also trade manually.
ADOPT_ORPHANS = _b("ADOPT_ORPHANS")

# ─── Venue max-drawdown guard (prop accounts) ───────────────────
# The limit that actually ends a challenge. STATIC anchors to the starting
# balance; TRAILING anchors to the venue's high-water mark and ratchets up.
# We flatten DD_GUARD_MARGIN points ABOVE the real floor so a gap can't take us
# through it. 0 disables (Hyperliquid has no such rule).
MAX_DD_PCT      = _f("MAX_DD_PCT", 0)              # e.g. 8 for a 2-Step
DD_TYPE         = os.getenv("DD_TYPE", "static").strip().lower()   # static | trailing
DD_GUARD_MARGIN = _f("DD_GUARD_MARGIN", 1.5)       # points of buffer

# ─── Sizing / leverage / concurrency ────────────────────────────
LEVERAGE      = _f("LEVERAGE", 2)        # launch at 2x (no liquidation risk vs 10% stop)
NOTIONAL_FRAC = _f("NOTIONAL_FRAC", 0.20)  # 20% of equity notional per position
# Concurrency cap. Default = leverage / notional_frac (2x / 20% -> 10), the lever
# that turns ~56% fill rate (1x) into ~85% (2x).
#
# Overridable because a prop account has to decouple the two: at NOTIONAL_FRAC
# 0.08 the formula yields 25, and 25 x 8% x 11.3% = 22.6% of equity if a
# correlated cluster all hard-stops at once — instant breach of a 6% floor.
# LEAVE UNSET on the Hyperliquid service to keep today's computed 10.
MAX_CONCURRENT = _i("MAX_CONCURRENT", int(LEVERAGE / NOTIONAL_FRAC + 1e-9))

# ─── Universe (volatile alt perps; main DEX only — no HIP-3) ────
_DEFAULT_COINS = "SOL,AVAX,LINK,NEAR,ADA,DOGE,BCH,LTC,DOT,ATOM"
_CORE_COINS = [c.strip().upper() for c in os.getenv("COINS", _DEFAULT_COINS).split(",") if c.strip()]

# WATCH: fragile / unproven names that still trade live, but at a reduced size so a
# wrong call costs less. They earn full size by proving themselves on real fills
# (shadow.py + realized-per-trade tracking). The traded universe is core ∪ watch.
WATCH_SET = set(c.strip().upper() for c in os.getenv("WATCH", "").split(",") if c.strip())
WATCH_SIZE_MULT = _f("WATCH_SIZE_MULT", 0.5)         # notional multiplier for WATCH coins
COINS = _CORE_COINS + [w for w in WATCH_SET if w not in _CORE_COINS]


def size_mult(coin: str) -> float:
    """Per-coin notional multiplier: WATCH coins trade reduced, everything else 1.0."""
    return WATCH_SIZE_MULT if coin.upper() in WATCH_SET else 1.0

# ─── Timing ─────────────────────────────────────────────────────
POLL_SECONDS           = _i("POLL_SECONDS", 120)    # trailing-manage + price poll cadence
RECONCILE_INTERVAL_SEC = _i("RECONCILE_INTERVAL_SEC", 300)
CANDLE_LIMIT           = _i("CANDLE_LIMIT", 200)    # 4h candles to pull per coin for RSI

# ─── Dashboard / logging ────────────────────────────────────────
DASHBOARD_PORT = _i("PORT", 8080)
LOG_LEVEL      = os.getenv("LOG_LEVEL", "INFO")

# Optional Telegram (no-ops if unset) — reused by utils.tg_notify
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()


# ─── HL symbol / DEX helpers (swing universe is all main-perp) ──
# Kept API-compatible with Aphelion's hl_client so that module reuses cleanly.
def asset_dex(asset: str) -> str:
    # Swing universe is main-perp only; no HIP-3 routing.
    return ""


def hl_symbol(asset: str) -> str:
    asset = asset.upper()
    sym = asset.split(":", 1)[1] if ":" in asset else asset
    return _HL_K_TOKENS.get(sym, sym)


# HL denominates some low-unit-price tokens in thousands with a CASE-SENSITIVE
# lowercase 'k' prefix (kPEPE, not KPEPE). Internally we uppercase everything;
# this maps back to the exact HL symbol at the API boundary (orders, candles,
# prices, asset_meta keys). Extend if a new k-token enters the universe.
_HL_K_TOKENS = {
    "KPEPE": "kPEPE", "KSHIB": "kSHIB", "KBONK": "kBONK", "KFLOKI": "kFLOKI",
    "KLUNC": "kLUNC", "KDOGS": "kDOGS", "KNEIRO": "kNEIRO", "KAPU": "kAPU",
}


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
        "exchange": EXCHANGE,
        "coins": COINS,
        "watch": sorted(WATCH_SET),
        "watch_size_mult": WATCH_SIZE_MULT,
        "leverage": LEVERAGE,
        "notional_frac": NOTIONAL_FRAC,
        "max_concurrent": MAX_CONCURRENT,
        "rsi": f"{RSI_LEN}@{RSI_TF}",
        "levels": f"L{LONG_LEVEL:.0f}/S{SHORT_LEVEL:.0f}",
        "trail_pct": TRAIL_PCT,
        "hard_stop_pct": HARD_STOP_PCT,
        "daily_dd_pct": DAILY_DD_PCT,
        "max_dd": f"{MAX_DD_PCT}% {DD_TYPE}" if MAX_DD_PCT else "off",
        "mode": "PAPER" if PAPER else ("DRY_RUN" if DRY_RUN else "LIVE"),
        "db": str(DB_PATH),
    }
