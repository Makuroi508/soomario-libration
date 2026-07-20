# Libration — Coin Diversification Kickoff

*Hand this to the next Claude Code session (the one running locally with control of the
cloned repo folder). It is self-contained: findings, tooling, and an exact runbook. The
prior session ran in the cloud and could NOT reach `api.hyperliquid.xyz` or the Railway
dashboard (network policy 403s), so it built the tooling but could not pull candles. The
local session CAN — that's the whole point of moving to your machine.*

**Branch:** `claude/libration-coin-diversification-bn03to` (all tooling below is committed
there — check it out after cloning). **Never touch `main`/live until a change is decided.**
Diversification is **env-only** on Railway (`COINS` / `WATCH` / `NOTIONAL_FRAC`) — no code
deploy, instant rollback.

---

## 1. The question we're answering

"Diversify the signal to more coins." The operator's intuition: more coins helps, but
spreads capital thinner between assets. We're validating that with the real trade history
and a proper backtest before changing anything live.

## 2. What the live data already told us (42 days, 762 fills, since Jun 8 2026)

- **381 closed round-trips, 83% win rate, +$0.855 avg/trade, +$326 realized.** The strategy
  works. Live equity ≈ $1,948, inception ≈ $1,697.
- **The risk is a thin tail, not the win rate.** ~83% small trailing-stop wins fund a few
  catastrophic −10% hard stops. **11 hard-stop hits = −$379.** The entire net profit is
  what survives those 11 bombs. *This strategy lives or dies on how many −10% hard stops it
  eats — not win rate.*
- **7 of 23 coins are net-negative (−$134 drag). TAO alone is −$80** (15 of 21 trades won,
  but three −10% stops of −$35/−$35/−$40 erased it). TAO is high-price + high-vol, so each
  10% stop is a full ~2%-of-equity gouge. It is the single most damaging line in the book.

  | Carrying it | P&L | | Dead weight | P&L |
  |---|---|---|---|---|
  | ENA +$61 · AVAX +$51 · JTO +$50 | | | ZEC ~$1 · DOT −$8 · HYPE −$9 | |
  | WLD +$47 · LINK +$45 · SUI +$44 | | | AAVE −$10 · kPEPE −$11 · NEAR −$12 | |
  | SOL +$39 · BCH +$26 · PENGU +$22 | | | **TAO −$80** | |

- **Concurrency is NOT saturated.** From a week of tick logs: **mean 7/10 slots in use,
  full (10/10) only ~12% of the time**, 2+ free slots ~76% of the time.

## 3. What that means for "diversify to more coins" (the precise mechanics)

`MAX_CONCURRENT = int(LEVERAGE / NOTIONAL_FRAC) = int(2/0.20) = 10`. This is a hard cap.

- **Widening `COINS` ≠ thinning per-position size.** Because the account isn't slot-bound
  (mean 7/10), adding coins mostly fills idle slots with *more trades* at the same 20% size.
  It does **not** "decrease funds between assets."
- **The lever that actually spreads capital thinner is `NOTIONAL_FRAC`.** Lower it (0.20 →
  0.10) → 20 slots, each 10%, same 200% gross. Every −10% bomb shrinks from ~2% of equity
  to ~1%. Since we're not slot-bound, this is **pure risk reduction** (won't add trades) —
  smaller bombs *and* smaller wins, slower compounding. That's the honest tradeoff.
- **More coins only helps if the new coins have positive edge.** The data proves adding
  coins isn't automatically good — 7 current coins lose money. Naive widening = more TAOs.

**Working thesis (validate, don't assume):** the win is mostly in *selection and tail
control*, not raw coin count. Likely best combination, in priority order:
1. **Fix the tail first** — cut TAO, or give high-vol/high-price names a tighter hard stop
   and/or 0.5× WATCH size. Highest ROI, env-only.
2. **Thin `NOTIONAL_FRAC`** for genuine diversification / risk reduction.
3. **Then** widen the pool to use idle capacity — only with coins that backtest positive.
4. Rank-slots-by-edge barely matters yet (12% saturation); revisit if we widen *and* thin.

## 4. Tooling committed on the branch (`backtest/`)

- **`backtest/pull_candles.py`** — fetches HL 4h candles (public endpoint, no keys) to
  `backtest/candles/<COIN>_4h.csv`. Includes a `DIVERSIFICATION_CANDIDATES` list to edit.
- **`backtest/backtest.py`** — replays the frozen strategy with the **shared-account model**
  (10 slots, 20% sizing, daily-DD, 1/coin) by reusing the live `signals.py`. A per-coin
  backtest overstates the edge by ignoring slot competition; this doesn't. Has a `--compare`
  A/B sweep (current vs wider vs thinner vs minus-TAO). Intrabar fill model is documented in
  the file header (4h OHLC approximation of the live 120s poll) — **validate before trusting
  absolute numbers.**

## 5. Runbook for the local session (do these in order)

```bash
# 0. you're on the branch with candle access
git checkout claude/libration-coin-diversification-bn03to

# 1. pull candles for the live set + candidates (deep history)
python backtest/pull_candles.py --days 180

# 2. FIRST validate the harness against reality: run the current live universe and
#    compare per-coin PnL / trade counts / hard-stop counts to the live fills in
#    trade_history_1.csv (see §2). They won't match to the dollar (idealized fills,
#    friction assumption), but per-coin RANK and the TAO tail MUST reproduce. If TAO
#    isn't the worst line, the intrabar fill model needs calibration — fix that before
#    trusting anything else.
python backtest/backtest.py --coins DOT,WLD,LINK,TAO,JTO,HYPE,kPEPE,SUI,SOL,NEAR,ADA,ENA,BCH,AVAX,ZEC,ATOM,XMR,AAVE,ONDO,FARTCOIN,PENGU --per-coin

# 3. run the diversification sweep
python backtest/backtest.py --compare

# 4. sizing sweep to price the "thin per-position" tradeoff
for f in 0.20 0.15 0.10; do python backtest/backtest.py --notional $f; done
```

Then produce a recommended `COINS` / `WATCH` / `NOTIONAL_FRAC` string, backed by numbers,
for the operator to set on Railway.

## 6. Open decisions for the operator

- **Candle source** confirmed = HL public API via `pull_candles.py` (local session can reach it).
- **Diversification style:** operator leaned toward *thin size + widen pool + rank slots by
  edge*, driven by backtest evidence. Confirm after the sweep.
- **Trial mode:** recommended = **env-only on Railway** (change `COINS`/`NOTIONAL_FRAC`,
  roll back instantly). Code changes (edge-ranked slots, per-coin stops) only if the
  backtest shows they're worth it.
- **Non-negotiable:** the frozen strategy params (RSI 14/4h, 50/40, 0.55% trail, 10% stop,
  5% DD) are walk-forward validated — **do not re-optimize.** The universe + sizing are the
  only knobs. (See `LIBRATION_HANDOFF.md` §2, §9.)

## 7. Watch-outs (from the handoff — don't regress these)

- Deploy interdependent files together; Railway auto-deploys on push to `main`.
- `get_positions()`: `None` = failed read, `[]` = genuinely empty — never collapse them.
- Reconcile/accounting are the delicate subsystems (`LIBRATION_HANDOFF.md` §4–§5). The
  diversification work touches none of that — it's `config`/env only.
- Operator is on Windows/PowerShell: use `Invoke-RestMethod` or `curl.exe`, not bare `curl`.

---
*Prior session: cloud, read-only vs the venue. Findings above are from the operator-supplied
`trade_history_1.csv`, `logs.json`, and `LIBRATION_HANDOFF.md`. Next session: local, full
access — pull candles, validate the harness, run the sweep, recommend a config.*
