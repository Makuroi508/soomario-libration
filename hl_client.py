"""
Soomario Libration — Hyperliquid client (reused from Aphelion) (dual-DEX aware)
═══════════════════════════════════════════════
Thin wrapper around the HL Python SDK with:
  • Retry/backoff for 429/500/502/503 + timeouts
  • Per-asset DEX routing (main perps + HIP-3 DEXes like xyz)
  • Auto-discovered szDecimals/priceDecimals from each DEX's /info meta
  • Dual-strategy price fetch: allMids for main + l2Book per-asset for HIP-3
    (HIP-3 stock prices are NOT in allMids — Rotation discovered this)
  • Aggregated equity across all DEXes the user has positions on
  • Aggressive-limit "market" orders (HL has no true market for vaults)
"""
import logging
import math
import time
from typing import Optional

import requests

from config import (
    HL_API_URL, HL_ACCOUNT_ADDRESS, HL_PRIVATE_KEY, HL_IS_VAULT,
    DRY_RUN, ASSET_META_FILE, COINS,
    hl_symbol, short_name, asset_dex, active_dexes, is_cross_for,
)
from utils import save_json, load_json

logger = logging.getLogger("hl_client")

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_BACKOFF_SEC = 1.5

ENTRY_SLIPPAGE = 0.005   # 0.5% for market opens
CLOSE_SLIPPAGE = 0.01    # 1.0% for closes — wider to guarantee fill

# Hyperliquid rejects any order whose notional value is below this floor.
# round_size() floors the coin size to the asset's szDecimals, which on
# high-priced perps (e.g. stock perps at $150-400) can push the actual
# notional below this even when the target notional was above it — one
# size increment can be worth several dollars. market_open() bumps the
# size up one increment when the floored size would breach this.
HL_MIN_ORDER_USD = 10.0

# Price-fetch retries per HIP-3 stock (Rotation pattern)
L2BOOK_RETRIES = 3
L2BOOK_RETRY_SLEEP = 0.3
L2BOOK_GAP = 0.05


# ═══════════════════════════════════════════════════════════════
#  Retry wrapper for SDK calls
# ═══════════════════════════════════════════════════════════════

def hl_retry(fn, *args, _what: str = "hl_call", **kwargs):
    """Retry an HL SDK call on transient errors. Returns None on repeated failure.

    On failure, attaches the last exception to `hl_retry.last_error` so callers
    can surface the actual SDK error instead of a bare None. Also uses
    logger.exception() (with traceback) on terminal failures — important for
    diagnosing vault auth issues where the SDK swallows context.
    """
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = fn(*args, **kwargs)
            if result is None:
                raise RuntimeError(f"{_what}: SDK returned None")
            if isinstance(result, dict) and result.get("status") == "err":
                raise RuntimeError(f"{_what}: SDK error: {result}")
            return result
        except Exception as e:
            last_exc = e
            msg = str(e)
            transient = (
                "429" in msg or "500" in msg or "502" in msg or "503" in msg
                or "504" in msg or "timeout" in msg.lower()
                or "returned None" in msg
            )
            if not transient or attempt == MAX_RETRIES:
                # Use logger.exception so the stack trace is captured —
                # the SDK often swallows the original cause and this is our
                # only window into what HL actually said.
                logger.exception(f"❌ {_what} failed (attempt {attempt}): {e}")
                hl_retry.last_error = e
                return None
            backoff = BASE_BACKOFF_SEC * (2 ** (attempt - 1))
            logger.warning(f"⚠️  {_what} transient (attempt {attempt}): {e} — backoff {backoff:.1f}s")
            time.sleep(backoff)
    logger.exception(f"❌ {_what} exhausted retries: {last_exc}")
    hl_retry.last_error = last_exc
    return None


# Initialize the attribute so callers can safely read it
hl_retry.last_error = None


# ═══════════════════════════════════════════════════════════════
#  Meta fetch — one call per DEX, merged into unified asset_meta
# ═══════════════════════════════════════════════════════════════

def fetch_hl_meta(dex: str = "") -> Optional[dict]:
    """
    Fetch /info meta for a given DEX.

    IMPORTANT: The plain `meta` endpoint silently ignores the `dex` parameter
    and always returns the main-perp universe. For HIP-3 DEXes, we must use
    `metaAndAssetCtxs` which explicitly supports `dex`. We use it for all
    calls (empty dex works too and returns the main DEX meta).

    Response shape of metaAndAssetCtxs: [meta_dict, asset_ctxs_array].
    We extract [0] for the universe.
    """
    try:
        body = {"type": "metaAndAssetCtxs"}
        if dex:
            body["dex"] = dex
        resp = requests.post(HL_API_URL, json=body, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"Meta fetch HTTP {resp.status_code} for dex='{dex}': {resp.text[:200]}")
            return None
        data = resp.json()
        # metaAndAssetCtxs returns [meta_dict, ctxs_array]
        if isinstance(data, list) and len(data) >= 1 and isinstance(data[0], dict):
            return data[0]
        logger.warning(f"Meta fetch unexpected shape for dex='{dex}': "
                       f"{type(data).__name__}, len={len(data) if hasattr(data,'__len__') else '?'}")
        return None
    except Exception as e:
        logger.warning(f"Meta fetch error (dex='{dex}'): {e}")
        return None


def build_asset_meta() -> dict:
    """
    Build {full_hl_symbol: {szDecimals, priceDecimals, maxLeverage, dex}} by
    fanning out to every DEX the user's COINS touch.

    Keys use full HL symbols:
      • 'SOL'        for main-perp assets
      • 'xyz:MSTR'   for HIP-3 xyz assets
    """
    dexes = active_dexes(COINS)
    out = {}
    for dex in dexes:
        meta = fetch_hl_meta(dex=dex)
        if not meta or "universe" not in meta:
            logger.warning(f"⚠️  Could not fetch meta for dex='{dex or '(main)'}'")
            continue
        count = 0
        names_preview = []
        for entry in meta.get("universe", []):
            raw_name = entry.get("name")
            if not raw_name:
                continue
            # HL returns HIP-3 asset names already prefixed (e.g. 'xyz:TSLA').
            # Normalize to canonical form: lowercase dex + UPPERCASE asset.
            if ":" in raw_name:
                prefix, _, asset_part = raw_name.partition(":")
                hl_name = f"{prefix.lower()}:{asset_part.upper()}"
            else:
                hl_name = f"{dex}:{raw_name.upper()}" if dex else raw_name.upper()
            sz_dec = int(entry.get("szDecimals", 2))
            price_dec = max(0, 6 - sz_dec)
            out[hl_name] = {
                "szDecimals": sz_dec,
                "priceDecimals": price_dec,
                "maxLeverage": int(entry.get("maxLeverage", 3)),
                "dex": dex,
            }
            count += 1
            if len(names_preview) < 10:
                names_preview.append(raw_name)
        logger.info(f"📐 Loaded {count} assets from dex='{dex or '(main)'}' "
                    f"(first: {names_preview})")

    if not out:
        logger.warning("⚠️  All meta fetches failed — falling back to cached")
        return load_json(ASSET_META_FILE, default={})

    save_json(ASSET_META_FILE, out)
    return out


def validate_symbols(required_short: list[str], asset_meta: dict) -> tuple[list, list]:
    """Returns (available, missing) in short-name form."""
    available, missing = [], []
    for s in required_short:
        if hl_symbol(s) in asset_meta:
            available.append(s.upper())
        else:
            missing.append(s.upper())
    return available, missing


# ═══════════════════════════════════════════════════════════════
#  Rounding helpers
# ═══════════════════════════════════════════════════════════════

def _meta_key(symbol: str) -> str:
    return symbol.upper() if ":" in symbol else hl_symbol(symbol)


def round_size(asset_meta: dict, symbol: str, size: float) -> float:
    key = _meta_key(symbol)
    dec = asset_meta.get(key, {}).get("szDecimals", 2)
    factor = 10 ** dec
    return math.floor(size * factor) / factor


def round_price(asset_meta: dict, symbol: str, price: float) -> float:
    key = _meta_key(symbol)
    dec = asset_meta.get(key, {}).get("priceDecimals", 2)
    if price <= 0:
        return price
    sig_figs = 5
    magnitude = math.floor(math.log10(price))
    sig_dec = max(0, sig_figs - 1 - magnitude)
    eff_dec = min(dec, sig_dec)
    return round(price, max(0, eff_dec))


# ═══════════════════════════════════════════════════════════════
#  HL client
# ═══════════════════════════════════════════════════════════════

class HLClient:
    """Dual-DEX-aware Hyperliquid client."""

    def __init__(self, asset_meta: Optional[dict] = None):
        self.exchange = None
        self.info = None
        self.asset_meta = asset_meta or {}
        self.account_address = HL_ACCOUNT_ADDRESS
        self.is_vault = HL_IS_VAULT
        # Last failure reason from market_open (None when the last open
        # succeeded). The engine surfaces this in the ORDER_FAILED signal-log
        # entry so the dashboard shows a real reason instead of "None".
        self.last_open_error = None

    # ── SDK initialization ────────────────────────────────────

    def init_sdk(self) -> bool:
        if DRY_RUN:
            logger.info("🧪 DRY_RUN=1 — skipping SDK init (reads still work)")
            return True
        if self.exchange is not None:
            return True
        if not HL_PRIVATE_KEY:
            logger.error("❌ HL_PRIVATE_KEY not set")
            return False
        if not HL_ACCOUNT_ADDRESS:
            logger.error("❌ HL_ACCOUNT_ADDRESS not set")
            return False
        try:
            import eth_account
            from hyperliquid.info import Info
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants

            # The SDK builds coin_to_asset / name_to_coin from the dexes
            # passed via perp_dexs. Without HIP-3 dexes loaded, calling
            # exchange.order(name="xyz:MSTR") raises KeyError because the
            # main universe doesn't contain that prefixed name. We always
            # include "" (main) so the main pool keeps working, then add
            # every HIP-3 dex our universe touches.
            perp_dexs = sorted({""} | {asset_dex(a) for a in COINS})

            account = eth_account.Account.from_key(HL_PRIVATE_KEY)
            self.info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=perp_dexs)
            if self.is_vault:
                self.exchange = Exchange(
                    account, constants.MAINNET_API_URL,
                    vault_address=HL_ACCOUNT_ADDRESS,
                    perp_dexs=perp_dexs,
                )
            else:
                self.exchange = Exchange(
                    account, constants.MAINNET_API_URL,
                    perp_dexs=perp_dexs,
                )
            logger.info(f"🔐 HL SDK initialized — signer={account.address[:10]}..., "
                        f"target={HL_ACCOUNT_ADDRESS[:10]}..., vault={self.is_vault}, "
                        f"perp_dexs={perp_dexs}")
            return True
        except Exception as e:
            logger.error(f"❌ HL SDK init failed: {e}", exc_info=True)
            return False

    # ── Market data (REST) ────────────────────────────────────

    def get_all_prices(self) -> dict[str, float]:
        """
        Returns {short_name: price} across all DEXes in COINS.
        Main perps via allMids (one call), HIP-3 stocks via per-asset l2Book.
        """
        out: dict[str, float] = {}
        main_assets = [a for a in COINS if not asset_dex(a)]
        hip3_assets = [a for a in COINS if asset_dex(a)]

        # 1. allMids for main perps
        if main_assets:
            try:
                resp = requests.post(HL_API_URL, json={"type": "allMids"}, timeout=10)
                if resp.status_code == 200:
                    mids = resp.json()
                    if isinstance(mids, dict):
                        for a in main_assets:
                            v = mids.get(a.upper())
                            if v is not None:
                                try:
                                    out[a.upper()] = float(v)
                                except (TypeError, ValueError):
                                    pass
            except Exception as e:
                logger.debug(f"allMids error: {e}")

        # 2. l2Book per HIP-3 asset (prices NOT in allMids)
        for a in hip3_assets:
            hl_sym = hl_symbol(a)
            for attempt in range(L2BOOK_RETRIES):
                try:
                    resp = requests.post(HL_API_URL, json={
                        "type": "l2Book", "coin": hl_sym,
                    }, timeout=10)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    levels = data.get("levels")
                    if not levels or len(levels) < 2:
                        continue
                    bids, asks = levels[0], levels[1]
                    if not bids or not asks:
                        continue
                    mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
                    if mid > 0:
                        out[a.upper()] = mid
                        break
                except Exception:
                    pass
                time.sleep(L2BOOK_RETRY_SLEEP)
            time.sleep(L2BOOK_GAP)

        return out

    def get_price(self, symbol: str) -> Optional[float]:
        """Accepts short name. Targeted single-symbol fetch."""
        key = short_name(symbol) if ":" in symbol else symbol.upper()
        dex = asset_dex(key)
        try:
            if dex:
                resp = requests.post(HL_API_URL, json={
                    "type": "l2Book", "coin": f"{dex}:{key}",
                }, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    levels = data.get("levels")
                    if levels and len(levels) >= 2 and levels[0] and levels[1]:
                        return (float(levels[0][0]["px"]) + float(levels[1][0]["px"])) / 2
            else:
                resp = requests.post(HL_API_URL, json={"type": "allMids"}, timeout=10)
                if resp.status_code == 200:
                    v = resp.json().get(key)
                    if v is not None:
                        return float(v)
        except Exception as e:
            logger.debug(f"get_price({symbol}) error: {e}")
        return None

    # ── Account state (fan out across DEXes) ──────────────────

    def _fetch_clearinghouse(self, dex: str) -> dict:
        try:
            body = {"type": "clearinghouseState", "user": self.account_address}
            if dex:
                body["dex"] = dex
            resp = requests.post(HL_API_URL, json=body, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug(f"clearinghouseState(dex={dex!r}) error: {e}")
        return {}

    def get_positions(self) -> list[dict]:
        """
        All open positions across every DEX in COINS.
        HIP-3 positions have prefixed coin names ('xyz:MSTR'); use
        config.short_name() to normalize for matching.
        """
        out = []
        for dex in active_dexes(COINS):
            state = self._fetch_clearinghouse(dex)
            for pw in state.get("assetPositions", []) or []:
                if not isinstance(pw, dict):
                    continue
                pos = pw.get("position")
                if not isinstance(pos, dict):
                    continue
                try:
                    szi = float(pos.get("szi", 0) or 0)
                    if szi != 0:
                        out.append(pos)
                except (TypeError, ValueError):
                    continue
        return out

    def get_position(self, symbol: str) -> Optional[dict]:
        target = short_name(symbol) if ":" in symbol else symbol.upper()
        for p in self.get_positions():
            if short_name(p.get("coin", "")) == target:
                return p
        return None

    def get_equity(self) -> float:
        """
        Total account equity, matching HL UI exactly.

        Personal accounts (HL_IS_VAULT=0):
          Under HL's unified-account model, spot USDC IS the wallet —
          its `total` balance equals HL UI's "USDC Value" / "Portfolio
          Value" / "Total Balance". Perp positions are funded from this
          pool, with the locked portion sitting in the spot `hold`.
          Adding xyz / main perp accountValue or isolated wallet equity
          on top would double-count the same capital.

        Vault accounts (HL_IS_VAULT=1):
          Vaults have no spot leg; their wallet is the main perp
          account. `marginSummary.accountValue` from `clearinghouseState`
          for the vault address is the canonical wallet value.
        """
        if self.is_vault:
            state = self._fetch_clearinghouse("")
            try:
                ms = state.get("marginSummary", {}) or {}
                return float(ms.get("accountValue", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        # Personal: spot USDC total = wallet (matches HL UI Balances tab)
        return self._get_spot_usdc_breakdown()["total"]

    def get_perp_equity(self) -> float:
        """Main perps accountValue only. Used by breakdown for display."""
        state = self._fetch_clearinghouse("")
        try:
            ms = state.get("marginSummary", {}) or {}
            return float(ms.get("accountValue", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def get_spot_usdc(self) -> float:
        """Spot USDC balance. Under unified account, this is typically 0
        because USDC is in the unified pool reflected by perp queries."""
        try:
            resp = requests.post(HL_API_URL, json={
                "type": "spotClearinghouseState",
                "user": self.account_address,
            }, timeout=10)
            if resp.status_code != 200:
                return 0.0
            data = resp.json()
            balances = data.get("balances", []) or []
            for b in balances:
                if b.get("coin") == "USDC":
                    try:
                        return float(b.get("total", 0) or 0)
                    except (TypeError, ValueError):
                        return 0.0
            return 0.0
        except Exception as e:
            logger.debug(f"spotClearinghouseState error: {e}")
            return 0.0

    def get_equity_breakdown(self) -> dict:
        """Detailed breakdown for the dashboard and /debug/equity."""
        main_state = self._fetch_clearinghouse("")
        try:
            ms = main_state.get("marginSummary", {}) or {}
            main_av = float(ms.get("accountValue", 0) or 0)
        except (TypeError, ValueError):
            main_av = 0.0

        hip3_positions = []
        hip3_isolated_total = 0.0
        for dex in active_dexes(COINS):
            if not dex:
                continue
            state = self._fetch_clearinghouse(dex)
            for ap in state.get("assetPositions", []) or []:
                pos = ap.get("position", {}) or {}
                lev = pos.get("leverage", {}) or {}
                if lev.get("type") == "isolated":
                    try:
                        margin_used = float(pos.get("marginUsed", 0) or 0)
                        upnl = float(pos.get("unrealizedPnl", 0) or 0)
                        contribution = margin_used + upnl
                        hip3_isolated_total += contribution
                        hip3_positions.append({
                            "coin": pos.get("coin"),
                            "dex": dex,
                            "marginUsed": margin_used,
                            "unrealizedPnl": upnl,
                            "contribution": contribution,
                        })
                    except (TypeError, ValueError):
                        pass

        spot = self.get_spot_usdc()
        total = main_av + hip3_isolated_total + spot

        return {
            "main_perps_accountValue": main_av,
            "hip3_isolated_margin_plus_upnl": hip3_isolated_total,
            "hip3_positions": hip3_positions,
            "spot_usdc": spot,
            "total": total,
        }

    def get_withdrawable(self) -> float:
        total = 0.0
        for dex in active_dexes(COINS):
            state = self._fetch_clearinghouse(dex)
            try:
                v = float(state.get("withdrawable", 0) or 0)
                if v > 0:
                    total += v
            except (TypeError, ValueError):
                pass
        return total

    def get_available_margin(self, dex: Optional[str] = None) -> float:
        """
        Margin actually deployable for new positions, matching HL UI's
        "Available Balance" / "Available to Trade" number exactly.

        Personal accounts (HL_IS_VAULT=0):
          Spot USDC `total - hold` = HL UI Available Balance. Under
          unified account the perp positions reserve their margin in
          the spot `hold` field, so the free portion is what's left to
          deploy. Verified live ($1,664 total, $1,504 hold → $160 free,
          matches HL UI Balances tab).

        Vault accounts (HL_IS_VAULT=1):
          `marginSummary.accountValue - marginSummary.totalMarginUsed`
          from the main clearinghouseState. Vaults have no spot leg.

        DEX ARGUMENT — kept for backward compat, ignored.

        Returns 0.0 on any fetch error rather than raising — the engine's
        margin check `if margin_needed > available * 0.95: reject` then
        correctly rejects until the next tick fetches fresh data.
        """
        del dex  # explicitly ignored — see docstring
        if self.is_vault:
            state = self._fetch_clearinghouse("")
            ms = state.get("marginSummary", {}) or {}
            try:
                av   = float(ms.get("accountValue", 0) or 0)
                used = float(ms.get("totalMarginUsed", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
            return max(0.0, av - used)
        # Personal: spot USDC free = HL UI Available Balance
        return self._get_spot_usdc_breakdown()["free"]

    def _get_spot_usdc_free(self) -> float:
        """Free USDC = total - hold. Internal helper. 0.0 on fetch failure."""
        bd = self._get_spot_usdc_breakdown()
        return bd["free"]

    def _get_spot_usdc_breakdown(self) -> dict:
        """Return {'total': X, 'hold': Y, 'free': X-Y} for the user's spot
        USDC. Under HL's unified-account model, spot USDC IS the wallet
        value: 'total' matches HL UI's USDC Total Balance, 'free' matches
        Available Balance. Returns zeros on fetch failure.
        """
        try:
            resp = requests.post(HL_API_URL, json={
                "type": "spotClearinghouseState",
                "user": self.account_address,
            }, timeout=10)
            if resp.status_code != 200:
                return {"total": 0.0, "hold": 0.0, "free": 0.0}
            data = resp.json()
            for bal in data.get("balances", []) or []:
                if bal.get("coin") == "USDC":
                    total = float(bal.get("total", 0) or 0)
                    hold  = float(bal.get("hold", 0) or 0)
                    return {
                        "total": total,
                        "hold":  hold,
                        "free":  max(0.0, total - hold),
                    }
        except Exception as e:
            logger.debug(f"spot USDC breakdown fetch error: {e}")
        return {"total": 0.0, "hold": 0.0, "free": 0.0}

    def get_collateral_breakdown(self) -> dict:
        """
        Where the user's capital sits, matching HL UI exactly.

        Personal accounts (unified): the spot USDC pool IS the wallet —
        `total` matches HL UI's "USDC Total Balance" / "Portfolio Value",
        `free` matches "Available Balance" / "Available to Trade", and
        `hold` is the portion locked supporting open perp positions.
        We do NOT break out main_perp_cross / xyz_perp_cross / isolated
        as separate pools because under unified those are derivative
        claims on the same spot pool — listing them alongside spot
        double-counts the same money (verified live: bot showed $3,122
        portfolio value vs HL UI $1,664; the delta was exactly the
        perp-pool double-count).

        Vault accounts have no spot leg; we use main marginSummary
        accountValue as the wallet, with deployable = accountValue
        - totalMarginUsed.

        Returns (personal):
          {
            "mode":             "unified",
            "deployable":       160.51,   # spot free, matches HL UI
            "portfolio_value":  1664.94,  # spot total,  matches HL UI
            "by_pool": {
              "spot_usdc_free": 160.51,
              "spot_usdc_hold": 1504.43,  # locked supporting perp positions
            },
            "isolated_position_margin": 0.00,  # unused under unified
          }

        Returns (vault):
          {
            "mode":             "vault",
            "deployable":       4354.44,
            "portfolio_value":  4354.44,
            "by_pool": {"main_perp_value": 4354.44},
            "isolated_position_margin": 0.00,
          }
        """
        if self.is_vault:
            state = self._fetch_clearinghouse("")
            ms = state.get("marginSummary", {}) or {}
            try:
                av   = float(ms.get("accountValue", 0) or 0)
                used = float(ms.get("totalMarginUsed", 0) or 0)
            except (TypeError, ValueError):
                av, used = 0.0, 0.0
            deployable = max(0.0, av - used)
            return {
                "mode":                     "vault",
                "deployable":               round(deployable, 2),
                "portfolio_value":          round(av, 2),
                "by_pool":                  {"main_perp_value": round(av, 2)},
                "isolated_position_margin": 0.0,
            }

        # Personal under unified-account: spot USDC is the wallet
        bd = self._get_spot_usdc_breakdown()
        return {
            "mode":             "unified",
            "deployable":       round(bd["free"], 2),
            "portfolio_value":  round(bd["total"], 2),
            "by_pool": {
                "spot_usdc_free": round(bd["free"], 2),
                "spot_usdc_hold": round(bd["hold"], 2),
            },
            "isolated_position_margin": 0.0,
        }

    # ─── Backward-compat shim — old name, new behavior ───
    # The dashboard's /api/status reads `available_margin_by_dex`. Under
    # the new model, "available per dex" is no longer a meaningful concept,
    # but the API response shape needs to keep working until the dashboard
    # is updated in the next pass. We return the same unified number under
    # both 'main' and 'xyz' keys so callers see a consistent number, then
    # the dashboard refactor can switch to the new collateral_breakdown.
    def get_available_margin_breakdown(self) -> dict:
        """DEPRECATED — superseded by get_collateral_breakdown().

        Kept temporarily so the existing /api/status response shape doesn't
        break the deployed dashboard. The numbers under 'by_dex' are now
        the unified deployable amount (same under both keys), not per-pool
        cross available. Spot free USDC is reported truthfully.

        Remove this method once the dashboard reads /api/collateral instead.
        """
        from config import COINS, active_dexes

        deployable = self.get_available_margin()
        spot_free = self._get_spot_usdc_free()

        by_dex = {}
        for dex in active_dexes(COINS):
            label = dex if dex else "main"
            by_dex[label] = round(deployable, 2)

        return {
            "by_dex":         by_dex,
            "spot_usdc_free": round(spot_free, 2),
            "total_perp":     round(deployable, 2),
        }

    def get_unrealized_pnl_total(self) -> float:
        total = 0.0
        for p in self.get_positions():
            try:
                total += float(p.get("unrealizedPnl", 0) or 0)
            except (TypeError, ValueError):
                pass
        return total

    def get_open_orders(self) -> list[dict]:
        out = []
        for dex in active_dexes(COINS):
            try:
                body = {"type": "openOrders", "user": self.account_address}
                if dex:
                    body["dex"] = dex
                resp = requests.post(HL_API_URL, json=body, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        out.extend(data)
            except Exception as e:
                logger.debug(f"openOrders(dex={dex!r}) error: {e}")
        return out

    def get_portfolio_history(self, period: str = "allTime") -> list[tuple[int, float]]:
        """Fetch HL's per-period mark-to-market NAV history.

        HL exposes `{type: "portfolio", user}` which returns periods like
        day / week / month / allTime / perpDay / perpWeek / perpMonth /
        perpAllTime. Each carries an `accountValueHistory` of
        [[ts_ms, value_str], ...]. This is the canonical data source for
        equity-curve reconstruction — matches HL UI's own portfolio chart.

        Returns: list of (ts_ms, value_float). Empty on error.
        """
        try:
            body = {"type": "portfolio", "user": self.account_address}
            resp = requests.post(HL_API_URL, json=body, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"portfolio HTTP {resp.status_code}: {resp.text[:200]}")
                return []
            data = resp.json()
            if not isinstance(data, list):
                logger.warning(f"portfolio: unexpected shape {type(data).__name__}")
                return []
            for entry in data:
                if not isinstance(entry, list) or len(entry) < 2:
                    continue
                if entry[0] != period:
                    continue
                payload = entry[1] if isinstance(entry[1], dict) else {}
                series = payload.get("accountValueHistory") or []
                out = []
                for pair in series:
                    try:
                        ts_ms = int(pair[0])
                        value = float(pair[1])
                        out.append((ts_ms, value))
                    except (TypeError, ValueError, IndexError):
                        continue
                return out
            logger.warning(f"portfolio: period '{period}' not found in response")
            return []
        except Exception as e:
            logger.warning(f"portfolio fetch error: {e}")
            return []

    # ── Order placement ───────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """symbol: short name. Margin mode auto-routes per asset."""
        short = symbol.upper().split(":", 1)[-1]
        hl_sym = hl_symbol(short)
        is_cross = is_cross_for(short)
        if DRY_RUN or self.exchange is None:
            logger.info(f"    [DRY] set_leverage({hl_sym}, {leverage}x, cross={is_cross})")
            return True
        result = hl_retry(
            self.exchange.update_leverage,
            leverage, hl_sym, is_cross,
            _what=f"set_leverage({hl_sym},{leverage}x)",
        )
        if result and result.get("status") == "ok":
            logger.info(f"    ✅ leverage {hl_sym}={leverage}x ({'cross' if is_cross else 'iso'})")
            return True
        # Surface the underlying exception captured by hl_retry so the user
        # can actually diagnose vault auth failures, dex permission issues, etc.
        err = getattr(hl_retry, "last_error", None)
        logger.warning(f"    ⚠️  leverage set failed: result={result}  underlying_error={err!r}")
        return False

    def market_open(self, symbol: str, is_buy: bool, notional_usd: float,
                    current_price: Optional[float] = None) -> Optional[dict]:
        short = symbol.upper().split(":", 1)[-1]
        hl_sym = hl_symbol(short)
        if current_price is None or current_price <= 0:
            current_price = self.get_price(short) or 0
        if current_price <= 0:
            logger.error(f"❌ {hl_sym}: no price available")
            return None

        size = notional_usd / current_price
        size = round_size(self.asset_meta, hl_sym, size)
        if size <= 0:
            logger.warning(f"⚠️  {hl_sym}: size rounded to 0 "
                           f"(notional=${notional_usd:.2f}, price=${current_price:.4f})")
            self.last_open_error = (f"size rounds to 0 at ${current_price:.2f} "
                                    f"(notional ${notional_usd:.2f})")
            return None

        # HL enforces a $10 minimum order value. round_size() floored the
        # coin size to the asset's precision, which on high-priced perps can
        # drop the notional below $10 even when notional_usd cleared it. Round
        # the size UP to the smallest increment whose notional clears the min.
        # (Integer-unit math avoids the float trap where 0.06 + 0.01 == 0.0699…
        # floors straight back to 0.06.)
        if size * current_price < HL_MIN_ORDER_USD:
            key = _meta_key(hl_sym)
            dec = self.asset_meta.get(key, {}).get("szDecimals", 2)
            factor = 10 ** dec
            min_size = math.ceil((HL_MIN_ORDER_USD / current_price) * factor) / factor
            if min_size > size and min_size * current_price >= HL_MIN_ORDER_USD:
                logger.info(f"    ↑ {hl_sym}: size {size} → {min_size} to clear "
                            f"${HL_MIN_ORDER_USD:.0f} min "
                            f"(notional ${size*current_price:.2f} → ${min_size*current_price:.2f})")
                size = min_size
            else:
                logger.warning(f"⚠️  {hl_sym}: cannot clear ${HL_MIN_ORDER_USD:.0f} min order "
                               f"(notional=${notional_usd:.2f}, price=${current_price:.4f}) — skipping")
                self.last_open_error = (f"below ${HL_MIN_ORDER_USD:.0f} min order even after "
                                        f"size bump (price ${current_price:.2f})")
                return None

        limit_price = current_price * (1 + ENTRY_SLIPPAGE) if is_buy else current_price * (1 - ENTRY_SLIPPAGE)
        limit_price = round_price(self.asset_meta, hl_sym, limit_price)

        side = "BUY" if is_buy else "SELL"
        logger.info(f"    ↗ market_open {side} {size} {hl_sym} @ ${limit_price} "
                    f"(ref=${current_price:.4f}, notional=${size*current_price:.2f})")

        if DRY_RUN or self.exchange is None:
            logger.info(f"    [DRY] would market_open")
            self.last_open_error = None
            return {
                "status": "filled", "filled": True, "dry": True,
                "avg_price": current_price, "total_size": size, "oid": None,
            }

        result = hl_retry(
            self.exchange.order,
            hl_sym, is_buy, size, limit_price, {"limit": {"tif": "Gtc"}},
            _what=f"market_open({hl_sym})",
        )
        parsed = self._parse_order_result(result, fallback_price=limit_price, fallback_size=size)
        if parsed is None:
            # Prefer the in-band order rejection (e.g. "minimum value of $10")
            # over hl_retry.last_error, which can be STALE — it may hold an
            # error from an earlier call in this same entry flow (e.g. a
            # set_leverage failure), masking the real order rejection.
            in_band = self._extract_order_error(result)
            if in_band:
                logger.error(f"    ❌ market_open({hl_sym}) rejected: {in_band}")
                self.last_open_error = str(in_band)
            else:
                err = getattr(hl_retry, "last_error", None)
                if err is not None:
                    logger.error(f"    ❌ market_open({hl_sym}) underlying error: {err!r}")
                self.last_open_error = str(err) if err is not None else "order not filled"
        else:
            self.last_open_error = None
        return parsed

    def market_close(self, symbol: str, size: float, is_long: bool,
                     current_price: Optional[float] = None) -> Optional[dict]:
        short = symbol.upper().split(":", 1)[-1]
        hl_sym = hl_symbol(short)
        if current_price is None or current_price <= 0:
            current_price = self.get_price(short) or 0
        if current_price <= 0:
            logger.error(f"❌ {hl_sym}: no price for close")
            return None

        size = round_size(self.asset_meta, hl_sym, size)
        if size <= 0:
            logger.warning(f"⚠️  {hl_sym}: close size rounded to 0")
            return None

        is_buy = not is_long
        limit_price = current_price * (1 - CLOSE_SLIPPAGE) if is_long else current_price * (1 + CLOSE_SLIPPAGE)
        limit_price = round_price(self.asset_meta, hl_sym, limit_price)

        side = "SELL" if is_long else "BUY"
        logger.info(f"    ↘ market_close {side} {size} {hl_sym} @ ${limit_price} (reduce_only)")

        if DRY_RUN or self.exchange is None:
            logger.info(f"    [DRY] would market_close")
            return {
                "status": "filled", "filled": True, "dry": True,
                "avg_price": current_price, "total_size": size, "oid": None,
            }

        result = hl_retry(
            self.exchange.order,
            hl_sym, is_buy, size, limit_price, {"limit": {"tif": "Gtc"}},
            reduce_only=True,
            _what=f"market_close({hl_sym})",
        )
        return self._parse_order_result(result, fallback_price=limit_price, fallback_size=size)

    def place_limit_buy(self, symbol: str, size: float, price: float) -> Optional[dict]:
        """Place a PASSIVE resting limit BUY at the given price.

        Used by engine for defense orders that should sit on the book and
        fill only when price drops to the trigger level. Unlike market_open,
        which uses ENTRY_SLIPPAGE for aggressive limit-as-market, this places
        at the exact price with no padding.

        Returns: {"oid": int, "status": "resting"|"filled", ...} on success
                 None on failure
        """
        short = symbol.upper().split(":", 1)[-1]
        hl_sym = hl_symbol(short)

        size = round_size(self.asset_meta, hl_sym, size)
        if size <= 0:
            logger.warning(f"⚠️  {hl_sym}: place_limit_buy size rounded to 0 "
                           f"(price=${price:.4f})")
            return None
        price = round_price(self.asset_meta, hl_sym, price)

        notional = size * price
        logger.info(f"    📌 place_limit_buy {size} {hl_sym} @ ${price} "
                    f"(notional ${notional:.2f})")

        if DRY_RUN or self.exchange is None:
            fake_oid = int(time.time() * 1000) % 1_000_000_000
            logger.info(f"    [DRY] would place limit_buy, fake oid={fake_oid}")
            return {
                "status": "resting", "filled": False, "dry": True,
                "oid": fake_oid, "size": size, "price": price,
            }

        # Tif=Gtc means good-till-canceled. Will fill immediately if price has
        # already dropped past trigger; otherwise rests on book.
        result = hl_retry(
            self.exchange.order,
            hl_sym, True, size, price, {"limit": {"tif": "Gtc"}},
            _what=f"place_limit_buy({hl_sym}@${price})",
        )

        # Try _parse_order_result first (handles the standard fields)
        parsed = self._parse_order_result(result, fallback_price=price, fallback_size=size)
        if parsed and parsed.get("oid"):
            return {
                "oid": parsed["oid"],
                "status": "filled" if parsed.get("filled") else "resting",
                "filled": parsed.get("filled", False),
                "size": size,
                "price": price,
                "avg_price": parsed.get("avg_price", price),
                "total_size": parsed.get("total_size", 0),
            }

        # Fallback: dig oid out of HL's "resting" status structure
        try:
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            for s in statuses:
                if isinstance(s, dict) and "resting" in s:
                    return {
                        "oid": s["resting"]["oid"], "status": "resting", "filled": False,
                        "size": size, "price": price,
                    }
                if isinstance(s, dict) and "filled" in s:
                    return {
                        "oid": s["filled"]["oid"], "status": "filled", "filled": True,
                        "size": float(s["filled"].get("totalSz", size)),
                        "price": price,
                        "avg_price": float(s["filled"].get("avgPx", price)),
                    }
        except Exception:
            pass

        logger.error(f"❌ {hl_sym}: place_limit_buy returned no parseable oid: {result}")
        return None

    def cancel_order(self, symbol: str, oid: int) -> bool:
        hl_sym = hl_symbol(symbol)
        if DRY_RUN or self.exchange is None:
            logger.info(f"    [DRY] cancel_order({hl_sym}, oid={oid})")
            return True
        result = hl_retry(self.exchange.cancel, hl_sym, oid, _what=f"cancel({hl_sym},{oid})")
        return bool(result and result.get("status") == "ok")

    # ── Candles (4h OHLC for RSI) ──────────────────────────────

    _INTERVAL_MS = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }

    def fetch_candles(self, symbol: str, interval: str = "4h", limit: int = 200) -> list[dict]:
        """Public candleSnapshot read (no signing). Returns oldest-first list of
        {"t": open_ms, "T": close_ms, "o","h","l","c","v": float}. The caller is
        responsible for dropping the still-forming final candle before computing
        entry signals (signals.closed_candles)."""
        short = short_name(symbol)
        hl_sym = hl_symbol(short)
        step = self._INTERVAL_MS.get(interval)
        if not step:
            logger.error(f"❌ unsupported interval {interval!r}")
            return []
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (limit + 2) * step
        try:
            resp = requests.post(HL_API_URL, json={
                "type": "candleSnapshot",
                "req": {"coin": hl_sym, "interval": interval,
                        "startTime": start_ms, "endTime": now_ms},
            }, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"candleSnapshot HTTP {resp.status_code} for {hl_sym}: {resp.text[:160]}")
                return []
            raw = resp.json()
            if not isinstance(raw, list):
                logger.warning(f"candleSnapshot unexpected shape for {hl_sym}: {type(raw).__name__}")
                return []
            out = []
            for c in raw:
                try:
                    out.append({
                        "t": int(c["t"]), "T": int(c["T"]),
                        "o": float(c["o"]), "h": float(c["h"]),
                        "l": float(c["l"]), "c": float(c["c"]),
                        "v": float(c.get("v", 0.0)),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            out.sort(key=lambda x: x["t"])
            return out[-(limit + 2):]
        except Exception as e:
            logger.warning(f"fetch_candles({hl_sym}) error: {e}")
            return []

    # ── User fills (actual execution prices → realized friction) ──

    def get_user_fills(self, start_ms: Optional[int] = None) -> list[dict]:
        """Recent fills for the account (no signing). With start_ms uses
        userFillsByTime to bound the window; else the latest userFills snapshot.
        Each fill: coin, px, sz, side, dir ('Close Long' etc.), closedPnl, fee,
        time, oid. Used to book closes at the ACTUAL fill so realized friction
        (intended stop vs real fill) becomes a measured number, not an estimate."""
        if not HL_ACCOUNT_ADDRESS:
            return []
        if start_ms is not None:
            body = {"type": "userFillsByTime", "user": HL_ACCOUNT_ADDRESS, "startTime": int(start_ms)}
        else:
            body = {"type": "userFills", "user": HL_ACCOUNT_ADDRESS}
        try:
            resp = requests.post(HL_API_URL, json=body, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"userFills HTTP {resp.status_code}: {resp.text[:160]}")
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"get_user_fills error: {e}")
            return []

    # ── Stop (trigger) orders — the always-resting safety net ──

    def place_stop_market(self, symbol: str, is_long: bool, size: float,
                          stop_px: float) -> Optional[str]:
        """Place a reduce-only stop-market (trigger) order that closes the
        position when price crosses stop_px. Returns the resting order id as a
        string, or None on failure. is_long describes the OPEN position; the
        close side is the opposite. HL has no in-place modify for triggers, so
        the trail uses modify_stop (place-new-then-cancel-old)."""
        short = symbol.upper().split(":", 1)[-1]
        hl_sym = hl_symbol(short)
        size = round_size(self.asset_meta, hl_sym, size)
        if size <= 0:
            logger.warning(f"⚠️  {hl_sym}: stop size rounded to 0")
            return None
        trigger_px = round_price(self.asset_meta, hl_sym, stop_px)
        # Closing a long => SELL; closing a short => BUY. The post-trigger limit
        # is padded in the fill direction so the market close always clears.
        is_buy_close = not is_long
        limit_px = trigger_px * (1 + CLOSE_SLIPPAGE) if is_buy_close else trigger_px * (1 - CLOSE_SLIPPAGE)
        limit_px = round_price(self.asset_meta, hl_sym, limit_px)
        order_type = {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "sl"}}

        side = "BUY" if is_buy_close else "SELL"
        logger.info(f"    🛑 place_stop {side} {size} {hl_sym} trigger=${trigger_px} (reduce_only)")

        if DRY_RUN or self.exchange is None:
            fake_oid = int(time.time() * 1000) % 1_000_000_000
            logger.info(f"    [DRY] would place stop, fake oid={fake_oid}")
            return str(fake_oid)

        result = hl_retry(
            self.exchange.order,
            hl_sym, is_buy_close, size, limit_px, order_type,
            reduce_only=True,
            _what=f"place_stop({hl_sym}@{trigger_px})",
        )
        oid = self._extract_resting_oid(result)
        if oid is None:
            in_band = self._extract_order_error(result)
            err = in_band or getattr(hl_retry, "last_error", None)
            logger.error(f"❌ {hl_sym}: place_stop_market got no oid: {err if err else result}")
            return None
        return str(oid)

    def modify_stop(self, symbol: str, is_long: bool, size: float,
                    old_oid, new_stop_px: float) -> Optional[str]:
        """Ratchet a stop to a new trigger. Places the NEW stop first, then
        cancels the OLD one, so the position is never left unprotected during
        the swap. Returns the new oid, or the old oid unchanged if the new
        placement failed (caller keeps protecting with the old stop)."""
        new_oid = self.place_stop_market(symbol, is_long, size, new_stop_px)
        if new_oid is None:
            logger.warning(f"⚠️  {symbol}: modify_stop kept old stop (new placement failed)")
            return str(old_oid) if old_oid is not None else None
        if old_oid is not None and str(old_oid) != str(new_oid):
            try:
                self.cancel_order(symbol, int(old_oid))
            except (TypeError, ValueError):
                logger.warning(f"⚠️  {symbol}: could not cancel old stop oid={old_oid!r}")
        return new_oid

    @staticmethod
    def _extract_resting_oid(result: Optional[dict]):
        """Pull a resting/filled oid out of an SDK order result, or None."""
        try:
            statuses = (result or {}).get("response", {}).get("data", {}).get("statuses", [])
            for s in statuses:
                if isinstance(s, dict):
                    if "resting" in s:
                        return s["resting"].get("oid")
                    if "filled" in s:
                        return s["filled"].get("oid")
        except (AttributeError, TypeError):
            pass
        return None

    # ── Internal helpers ──────────────────────────────────────

    @staticmethod
    def _extract_order_error(result: Optional[dict]) -> Optional[str]:
        """Pull the in-band order rejection message (e.g. 'Order must have
        minimum value of $10') from an SDK order result, if present. Returns
        None if there's no in-band error. Used to surface the real rejection
        instead of a stale hl_retry.last_error from an earlier call."""
        try:
            statuses = (result or {}).get("response", {}).get("data", {}).get("statuses", [])
            if statuses and isinstance(statuses[0], dict) and "error" in statuses[0]:
                return statuses[0]["error"]
        except (AttributeError, IndexError, TypeError):
            pass
        return None

    @staticmethod
    def _parse_order_result(result: Optional[dict], fallback_price: float, fallback_size: float) -> Optional[dict]:
        if not result:
            return None
        if result.get("status") != "ok":
            logger.warning(f"    ⚠️  order non-ok: {result}")
            return None
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            return None
        s = statuses[0]
        if "filled" in s:
            fill = s["filled"]
            return {
                "status": "filled", "filled": True,
                "avg_price": float(fill.get("avgPx", fallback_price)),
                "total_size": float(fill.get("totalSz", fallback_size)),
                "oid": fill.get("oid"),
            }
        if "resting" in s:
            return {
                "status": "resting", "filled": False,
                "avg_price": fallback_price, "total_size": fallback_size,
                "oid": s["resting"].get("oid"),
            }
        if "error" in s:
            logger.error(f"    ❌ order error: {s['error']}")
            return None
        return None

    # ── Pre-flight signer check ───────────────────────────────

    def signing_works(self) -> bool:
        if DRY_RUN:
            return True
        if self.exchange is None:
            return False
        if not self.is_vault:
            try:
                signer = self.exchange.wallet.address
                if signer.lower() != self.account_address.lower():
                    logger.error(
                        f"❌ Signer/account mismatch: key signs for {signer}, "
                        f"but HL_ACCOUNT_ADDRESS={self.account_address}. "
                        f"If this is intentional, set HL_IS_VAULT=1."
                    )
                    return False
            except Exception as e:
                logger.warning(f"Could not verify signer address: {e}")
        return True
