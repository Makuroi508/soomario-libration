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
| `DD_GUARD_MARGIN` | `0` | `0` | `0` | `0` |
| `DAILY_DD_PCT` | `3` | `3` | `3` | `4` |
| `MAX_CONCURRENT` | `8` | `8` | `8` | `8` |
| *simulated P(pass)* | *92%* | *93%* | *94%* | *100%* |
| *median days to target* | *90* | *79* | *61* | *27* |

`DD_TYPE` is the one people get wrong: **1-Step floors are STATIC**, fixed at
`start × (1 − dd)`, so profit becomes permanent cushion. Only the 2-Step
Explorer trails the high-water mark. Running a 1-Step under `trailing` throws
away the product's main advantage.

`DD_GUARD_MARGIN` is how far *above* the real floor the bot flattens — and it
should be **`0`**. Measured across 1,500 simulated attempts per setting, the
margin never once improved P(pass); it only ever cost it, because a flatten is
not a pause. `check_max_dd()` keeps the book empty until a human intervenes, so
an early flatten converts "might recover" into "definitely done":

| Challenge | margin 0 | margin 1.5 | cost |
|---|---:|---:|---:|
| Explorer 8% | **100%** | 91.3% | -8.7pp |
| Pro 5% | **92.9%** | 87.8% | -5.1pp |
| Turbo 3% | **92.2%** | 74.1% | **-18.1pp** |
| Classic 6% | **94.1%** | 92.2% | -1.9pp |

The Turbo is worst because 1.5 points of a 3% allowance is half the room.

**What the simulation cannot see:** the margin exists to stop a price gap
carrying equity through the floor between polls. Equity here moves trade by
trade, so there is no gap to model and this test structurally cannot show the
benefit the margin was designed for. The real protection against that is
`MAX_CONCURRENT`, not the margin — see below.

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
MAX_CONCURRENT=8            # 6 if you want a provably un-killable worst case
MAX_DD_PCT=5                # see the dial
DD_TYPE=static              # see the dial
DD_GUARD_MARGIN=0           # never flatten early — it only costs pass rate
DAILY_DD_PCT=3
DAILY_FLATTEN=0             # off: flattening a drawdown locks in losses that recover
```

**`MAX_CONCURRENT`** — leave it set. Unset it and the formula gives
`int(LEVERAGE/NOTIONAL_FRAC)` = 16 at 0.12, far more correlated exposure than a
prop floor tolerates.

It is also the real defence against a mass stop-out, which is the scenario the
guard margin was meant to cover. A 10% hard stop costs `frac x 10%` of equity,
so the worst case if every open position stops at once is `cap x frac x 10%`:

| | cap 4 | cap 6 | cap 8 | cap 10 |
|---|---:|---:|---:|---:|
| frac 0.08 | 3.2% | 4.8% | 6.4% | 8.0% ⚠ |
| frac 0.12 | 4.8% | **7.2%** | 9.6% ⚠ | 12.0% ⚠ |
| frac 0.16 | 6.4% | 9.6% ⚠ | 12.8% ⚠ | 16.0% ⚠ |

At **0.12 x cap 6 = 7.2%** the account cannot be killed by a simultaneous
stop-out, because the worst case sits inside the 8% floor. At cap 8 it is 9.6%
and it can. The price of that safety is real but small — P(pass) at margin 0
drops from 100% to 93.0% on the Explorer and 92.9% to 89.5% on the Pro.

For context, the live Hyperliquid book's worst observed day was **2** hard stops
(11 stops across 8 distinct days), which at 0.12 is 2.4% — nowhere near either
bound. Cap 8 is the higher-expectancy choice; cap 6 is the one that cannot lose
the account to a single correlated event.

**`DAILY_FLATTEN=0`** — keep it off. The config comment argues for `1` on prop
accounts, but live experience on this account contradicts it: flattening during
a drawdown closed positions that subsequently recovered, and the realized loss
was worse than riding them. Since the trailing stop and the resting hard stop
already bound every position, the flatten adds forced selling at the worst
moment without adding protection.

The cost of leaving it off is that open positions can keep bleeding past the
venue's daily limit after the halt has stopped new entries. `DAILY_DD_PCT` is
therefore set *below* the venue limit (4 against Explorer's 5) so the halt fires
early and leaves the open book a point of room to resolve on its own.

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

## Ready to paste — Explorer 2-Step, fresh account ($10k, +5%, 8% trailing)

```
EXCHANGE=propr
PROPR_API_KEY=<your key>
PROPR_ACCOUNT_ID=<new urn>
PROPR_API_BASE=https://api.propr.xyz/v1
STATE_PATH=/data

NOTIONAL_FRAC=0.12
LEVERAGE=2
MAX_CONCURRENT=8
MAX_DD_PCT=8
DD_TYPE=trailing
DD_GUARD_MARGIN=0
DAILY_DD_PCT=4
DAILY_FLATTEN=0

COINS=HYPE,CC,SOL,ONDO,kPEPE,SUI,LINK,FARTCOIN,JTO,PENGU,XMR
WATCH=FARTCOIN,JTO,PENGU,XMR
WATCH_SIZE_MULT=0.5
UNIVERSE_AUTOFILTER=0

POLL_SECONDS=120
RECONCILE_INTERVAL_SEC=300
ADOPT_ORPHANS=1
MEASURED_FRICTION_PCT=0.16
```

Simulated P(pass) 100%, median 27 days. Strategy variables are omitted on
purpose — unset means the frozen validated defaults.

Swap `MAX_CONCURRENT=6` if you would rather the account be un-killable by a
single correlated stop-out; that costs about 7 points of pass rate.

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
