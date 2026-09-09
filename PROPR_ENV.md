# Propr environment — full variable set, in order

Every variable the bot reads on a Propr deployment, grouped so the numbers you
actually change sit together at the top. Values are from the 12-month sizing
study in `backtest/challenge_sim.py` (2026-09-09).

---

## ⚠ Two variables in your current config do nothing

| You have | Status | Fix |
|---|---|---|
| `PROPR_BASE_URL="https://api.propr.xyz/v1"` | **Never read.** The code reads `PROPR_API_BASE` (`propr_client.py:50`). | Rename to `PROPR_API_BASE`, or delete — the default is already that URL. |
| `PROPR_START_BALANCE="5000"` | **Never read anywhere.** Account inception comes from the venue. | Delete. |

Neither is breaking anything today, because the base URL default matches what
you set and the start balance is read from Propr. But if you ever point at a
different endpoint, `PROPR_BASE_URL` will be silently ignored.

---

## 1 — The dial: change these per challenge

These four decide pass/fail. Everything below this section stays the same.

| Variable | Turbo 3% | Pro 5% | Classic 6% | Explorer 8% |
|---|---|---|---|---|
| `NOTIONAL_FRAC` | `0.08` | `0.12` | `0.12` | `0.12` |
| `MAX_DD_PCT` | `3` | `5` | `6` | `8` |
| `DD_TYPE` | `static` | `static` | `static` | `trailing` |
| `DD_GUARD_MARGIN` | `0.5` | `0.75` | `1.0` | `1.5` |
| `DAILY_DD_PCT` | `3` | `3` | `3` | `5` |
| *simulated P(pass)* | *77%* | *92%* | *~95%* | *100%* |
| *median days to target* | *91* | *82* | *~70* | *28* |

`DD_TYPE` is the one people get wrong: **1-Step floors are STATIC**, fixed at
`start × (1 − dd)`, so profit becomes permanent cushion. Only the 2-Step
Explorer trails the high-water mark. Running a 1-Step under `trailing` throws
away the product's main advantage.

`DD_GUARD_MARGIN` is how far *above* the real floor the bot flattens. At the
default `1.5` on a 3% challenge you surrender **half your allowance** to the
buffer.

---

## 2 — Venue and identity

```
EXCHANGE=propr
PROPR_API_KEY=<secret>
PROPR_ACCOUNT_ID=urn:prp-account:vxM6PScH7jV7
PROPR_API_BASE=https://api.propr.xyz/v1
PROPR_ATTEMPT_ID=
STATE_PATH=/data
```

`PROPR_ATTEMPT_ID` is optional and self-heals — the client always takes the id
from the API and only warns if yours disagrees. Leave it blank.

`STATE_PATH=/data` needs a Railway volume; one per service.

---

## 3 — Risk and sizing

```
NOTIONAL_FRAC=0.12          # see the dial above
LEVERAGE=2
MAX_CONCURRENT=8            # was 6; 6 blocked 101 entries over 12 months
MAX_DD_PCT=5                # see the dial
DD_TYPE=static              # see the dial
DD_GUARD_MARGIN=0.75        # see the dial
DAILY_DD_PCT=3
DAILY_FLATTEN=1             # was 0 — see note
```

**`MAX_CONCURRENT`** — leave it set. Unset it and the formula gives
`int(LEVERAGE/NOTIONAL_FRAC)` = 16 at 0.12, which is far more correlated
exposure than a prop floor tolerates. Measured over 12 months: cap 6 blocks 101
entries, cap 8 blocks 8, cap 10 blocks 0. Going 6 → 8 lifts P(pass) on Pro from
89.0% to 90.8% and on Turbo from 73.0% to 78.0%, with no increase in drawdown.
Past 8 there is no further gain.

**`DAILY_FLATTEN=1`** — your current `0` is the Hyperliquid default and the
config's own comment argues against it here: *"Halting only stops digging —
open positions keep bleeding toward the venue's hard daily limit. On a prop
account that is what ends the challenge, so the guard must also FLATTEN."*
Halting alone stops new entries but lets open positions keep running toward the
3% daily limit. I have **not** simulated the flatten behaviour, so treat this as
following the code author's stated intent rather than a tested result.

---

## 4 — The book

```
COINS=HYPE,CC,SOL,ONDO,kPEPE,SUI,LINK,FARTCOIN,JTO,PENGU,XMR
WATCH=FARTCOIN,JTO,PENGU,XMR
WATCH_SIZE_MULT=0.5
UNIVERSE_AUTOFILTER=0
```

Keep all eleven. Dropping the worst performers was tested and made things worse
out of sample — the per-coin ranking is anti-persistent.

`UNIVERSE_AUTOFILTER=0` keeps the ATR/volume screen as a logged report only; it
never silently drops a coin. `MIN_ATR_PCT` and `MIN_DAILY_VOL_USD` are inert
while this is `0`.

---

## 5 — Strategy — frozen, do not tune

```
RSI_LEN=14
RSI_TF=4h
LONG_LEVEL=50
SHORT_LEVEL=40
TRAIL_PCT=0.55
HARD_STOP_PCT=10
TRAIL_ENABLED=1
TRAIL_ARM_DELAY_BARS=1
COIN_PARAMS=<leave unset — the built-in default is the tuned set>
```

Every one of these is walk-forward validated, and each alternative tested here
(RSI levels 30/35/40/50, trail widths, take-profit rules, 15m/30m/1h
timeframes) lost out of sample. Leave `COIN_PARAMS` unset so the code's own
default applies — it carries CC's 50/65 straddle, the per-coin trail widths and
the 30-minute arming delays.

---

## 6 — Operations

```
POLL_SECONDS=120
RECONCILE_INTERVAL_SEC=300
CANDLE_LIMIT=200
ADOPT_ORPHANS=1
LOG_LEVEL=INFO
PORT=8080
```

`ADOPT_ORPHANS=1` is right on a dedicated bot account and wrong anywhere you
also trade by hand.

---

## 7 — Optional

```
MEASURED_FRICTION_PCT=0.16   # default 0.5 is a stale placeholder
SHADOW_TRAILS=0.3,0.4
SHADOW_STALE_HOURS=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_CHANNEL_ID=
REPORT_TOKEN=<set this if the dashboard is reachable>
ENTRIES_ENABLED=1
DRY_RUN=0
PAPER=0
```

`MEASURED_FRICTION_PCT` cannot affect live trading — it only scores the shadow
trail A/B. The default 0.5 is ~3× the measured 0.16% and biases that comparison
against tighter trails.

`REPORT_TOKEN` gates `/api/report`, which serves full account financials.

---

## Ready to paste — Bronze 1-Step Pro ($25k, +12%, 5% static)

```
EXCHANGE=propr
PROPR_API_KEY=<secret>
PROPR_ACCOUNT_ID=<urn>
PROPR_API_BASE=https://api.propr.xyz/v1
STATE_PATH=/data

NOTIONAL_FRAC=0.12
LEVERAGE=2
MAX_CONCURRENT=8
MAX_DD_PCT=5
DD_TYPE=static
DD_GUARD_MARGIN=0.75
DAILY_DD_PCT=3
DAILY_FLATTEN=1

COINS=HYPE,CC,SOL,ONDO,kPEPE,SUI,LINK,FARTCOIN,JTO,PENGU,XMR
WATCH=FARTCOIN,JTO,PENGU,XMR
WATCH_SIZE_MULT=0.5
UNIVERSE_AUTOFILTER=0

POLL_SECONDS=120
RECONCILE_INTERVAL_SEC=300
ADOPT_ORPHANS=1
MEASURED_FRICTION_PCT=0.16
```

Strategy variables are omitted on purpose — unset means the frozen validated
defaults, which is what you want.

---

## A caution on the numbers

P(pass) figures come from 12 months of Binance data replayed through the
harness, starting each attempt at a random point in the real trade sequence.
Most of that window is **in-sample for `COIN_PARAMS`** (grid dated 2026-08-16),
so the book's absolute results flatter it. The *relative* ordering — book over
HYPE-only, and larger size lowering P(pass) once the floor is in reach — is the
robust part.

Your live Explorer account sits at −5.8% against an 8% trailing floor, i.e.
close to the 6.5% guard, while this study says P(pass) 100% at 12%. Live
evidence beats a backtest: **size down on that account, not up.**
