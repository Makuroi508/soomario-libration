# Soomario Libration

A standalone Python executor that runs a walk-forward-validated **RSI swing strategy**
across a basket of volatile altcoin perps from **one shared account** on Hyperliquid,
managing concurrency, position sizing, leverage, and a daily-drawdown halt centrally.

Libration is Aphelion's sibling: it reuses the same Hyperliquid client, the Imperial
Treasury dashboard system, and the canonical equity-curve spec. Where Aphelion is
signal-driven DCA with defensive limit buys, Libration is a one-position-per-coin swing
engine with a trailing stop and a hard stop.

> **Status:** validated in backtest + walk-forward. The open question is whether real
> slippage keeps expectancy positive — that's the entire point of the Phase-1 live trial.
> Launch in `PAPER`, then a small `LIVE` trial, and read the go/no-go off the dashboard.

---

## The strategy (frozen — do not re-optimize without re-running walk-forward)

| Parameter | Value |
|---|---|
| RSI | 14, on 4h closes (Wilder / `ta.rsi`) |
| Long entry | RSI crosses **up** through 50 |
| Short entry | RSI crosses **down** through 40 |
| Trailing stop | 0.55% — arms at +0.55%, trails 0.55% behind peak |
| Hard stop | 10% — native resting trigger, never moved against |
| Daily-DD halt | 5% — no new entries the rest of the UTC day |
| Sizing | 20% notional per position, of current equity |
| Leverage | 2x (launch) |
| Max concurrent | `leverage / notional_frac` = **10** at 2x/20% |
| Pyramiding | 1 (one position per coin) |

Entries are evaluated **only on the close of a completed 4h bar** (a per-coin guard
prevents intra-bar double-firing). At 2x with a 10% stop there is no liquidation risk —
the stop fires long before margin is gone.

---

## Architecture

```
app.py              main loop: scheduler-free daemon tick + Flask on the main thread
config.py           all params from env, validated defaults
universe.py         ATR% + liquidity filter (quarterly tool; writes universe.json)
signals.py          Wilder RSI, crossover/crossunder, closed-bar guard
position_manager.py SHARED-ACCOUNT core: sizing, concurrency, leverage, daily-DD halt
exit_manager.py     trailing (0.55%) + hard stop (10%), live reconcile, userFills booking
shadow.py           counterfactual trail A/B (records, never trades)
hl_client.py        Hyperliquid client (reused from Aphelion) + candles/stops/userFills
db.py               SQLite operational state (+ WAL); JSONL logs for the dashboard
api.py              Flask routes: dashboard + /api/* + /healthz
dashboard.html      Imperial Treasury monitor (5 tabs)
Procfile            web: python app.py
requirements.txt
```

**Data model.** SQLite (`libration.db`) is the operational source of truth the bot reads
and writes each tick. Append-only `equity_log.jsonl` / `trade_log.jsonl` feed the
dashboard charts. `status.json` is a per-tick snapshot the API serves. `universe.json`
holds the latest ATR%/volume evaluation.

**Flow-neutral equity.** The curve plots `starting baseline + cumulative realized PnL +
open unrealized PnL` — never raw wallet value — so deposits and withdrawals can't show up
as gains or losses. During the own-account trial this equals wallet value; it stays
correct the moment Libration is wrapped as a vault.

---

## Deployment (Railway)

1. **Create a persistent volume and mount it at `/data`.** This is required — without it,
   every redeploy wipes the equity curve, trade history, and the daily baseline.
2. Set `STATE_PATH=/data`. That one variable places the DB, all JSONL logs, `status.json`,
   and shadow state on the volume together.
3. Set the Hyperliquid credentials (below). Public networking port must match `PORT` (8080).
4. `Procfile` is `web: python app.py` (Railpack, Python 3.13 — no Dockerfile/gunicorn).
5. **Start in `PAPER=1`.** Watch a few real 4h bars flow through and confirm the dashboard
   populates before risking capital. Then switch to live.
6. If the venue geoblocks the Railway region, route accordingly (check your egress IP).

### Run modes

| Mode | Env | Behavior |
|---|---|---|
| **PAPER** | `PAPER=1` | Simulates fills against live prices, tracks its own equity, never sends orders. The multi-week dry run. |
| **DRY_RUN** | `DRY_RUN=1` | Reads live; the SDK skips signing so orders are no-ops. A quick "does the loop run" check, not a multi-day trial. |
| **LIVE** | both unset | Real orders, real stops, real fills. |

---

## Configuration (env)

**Credentials**
| Var | Default | Notes |
|---|---|---|
| `HL_ACCOUNT_ADDRESS` | — | account / vault address |
| `HL_PRIVATE_KEY` | — | API wallet key; never commit |
| `HL_IS_VAULT` | `0` | `1` if trading a vault |

**State / deploy**
| Var | Default |
|---|---|
| `STATE_PATH` | `./state` (set `/data` on Railway) |
| `DB_PATH` | `<STATE_PATH>/libration.db` |
| `PORT` | `8080` |
| `LOG_LEVEL` | `INFO` |

**Strategy (frozen)**
| Var | Default |
|---|---|
| `RSI_LEN` / `RSI_TF` | `14` / `4h` |
| `LONG_LEVEL` / `SHORT_LEVEL` | `50` / `40` |
| `TRAIL_PCT` | `0.55` |
| `HARD_STOP_PCT` | `10` |
| `DAILY_DD_PCT` | `5` |
| `LEVERAGE` / `NOTIONAL_FRAC` | `2` / `0.20` (→ 10 concurrent) |
| `COINS` | `SOL,AVAX,LINK,NEAR,ADA,DOGE,BCH,LTC,DOT,ATOM` |

**Timing / toggles**
| Var | Default |
|---|---|
| `POLL_SECONDS` | `120` |
| `CANDLE_LIMIT` | `200` |
| `ENTRIES_ENABLED` / `TRAIL_ENABLED` | `1` / `1` |

**Paper accounting**
| Var | Default |
|---|---|
| `PAPER_START_EQUITY` | `1000` |
| `PAPER_SLIPPAGE_PCT` | `0.0` |

**Shadow trail A/B**
| Var | Default |
|---|---|
| `SHADOW_TRAILS` | `0.3,0.4` |
| `MEASURED_FRICTION_PCT` | `0.5` (placeholder until live fills measure it) |

**Universe filter**
| Var | Default |
|---|---|
| `MIN_ATR_PCT` | `3.0` |
| `MIN_DAILY_VOL_USD` | `10000000` |
| `UNIVERSE_AUTOFILTER` | `0` (report only; never silently drops a validated coin) |

**Telegram (optional)** — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

---

## Smoke tests (run on a machine with venue network access)

The bot reaches `api.hyperliquid.xyz`, which sandboxes/CI often can't. Verify on your box:

```bash
# 1. candles return oldest-first OHLC
python -c "from hl_client import HLClient; print(HLClient().fetch_candles('SOL','4h',5))"

# 2. a reduce-only stop trigger rests (do this on a tiny live position; check the HL UI)
#    DRY_RUN=1 first to see the computed trigger price, then live.

# 3. the dashboard + equity route respond
curl -s localhost:8080/healthz
curl -s 'localhost:8080/api/equity?tf=all' | python -m json.tool | head -20
for tf in 1d 1w 1m all; do
  curl -s "localhost:8080/api/equity?tf=$tf" \
    | python -c "import json,sys;d=json.load(sys.stdin);print(tf:=d['tf'],'bucket',d['bucket_seconds'],'pts',len(d['points']))"
done
# expect buckets 1d=3600 / 1w=14400 / 1m=86400 / all=86400
```

---

## Dashboard

`https://<app>.up.railway.app/` — five tabs:

- **Overview** — mode banner, performance-equity hero with the `X/10` concurrency gauge,
  the equity curve (flow-neutral), the validation KPI row, and the universe pulse.
- **Positions** — open positions with mark, uPnL, move %, current stop (TRAIL/HARD),
  distance-to-stop, and age.
- **Trades** — closed trades with the go/no-go header: win rate, **avg net/trade vs the
  +0.5% backtest**, best/worst, TRAIL-vs-HARD-STOP split.
- **Trail A/B** — live 0.55% vs each shadow trail, median net after measured friction,
  matched-trade progress toward ~50, and a switch-or-hold verdict.
- **Universe** — per coin: heat state, daily ATR%, 24h volume, included/excluded + reason.

**Universe pulse** shows a coarse 3-state heat (dormant / warming / hot) computed
server-side — the raw RSI never reaches the browser and no direction is shown, so the
edge can't be cleanly copied. Open positions show as `active`.

API: `/api/status`, `/api/positions`, `/api/trades`, `/api/stats`, `/api/shadow`,
`/api/universe`, `/api/equity`, `/healthz`.

---

## Watch list (reduced sizing for unproven names)

Not every coin earns full size on day one. `WATCH` is a second list of fragile or
short-history names that still trade live, but at `WATCH_SIZE_MULT` of normal notional
(default 0.5×) — so a wrong call on one costs half. They're added to the traded universe
alongside `COINS`; the only difference is size.

```
COINS=DOT,WLD,LINK,TAO,JTO,HYPE,kPEPE,SUI,SOL,NEAR,ADA,ENA,BCH,AVAX,ZEC
WATCH=ATOM,XMR,AAVE,ONDO,FARTCOIN,PENGU
WATCH_SIZE_MULT=0.5
```

The asymmetry is the point: you can always promote a coin to full size once live fills
confirm the edge (move it from `WATCH` to `COINS`), but you can't un-take the losses from
launching a bad one at full weight. Use the realized-per-trade tracking and `shadow.py` to
decide promotions on evidence, not backtest optimism. Reduced positions are flagged `½×`
on the dashboard (position cards and the Universe tab).

---

## How realized friction is measured

When a stop fills on the exchange, `reconcile()` reads the **actual fill** from Hyperliquid's
`userFills` and books the close at that price (net of fees), not at the stop estimate.
Per trade it records **round-trip friction** = the planned return (signal entry → intended
stop, no fees) minus the realized net return. This is the single number the go/no-go hinges
on; it also becomes the friction charged to the shadow A/B for a fair comparison (the
placeholder is used until ≥5 live trades exist). In paper mode friction is left null —
fills are idealized, so there's nothing real to measure.

---

## Phase-1 live validation (the actual go/no-go)

Run 4–6 weeks at 2x, small account, 4–5 volatile alts (e.g. SOL, AVAX, LINK, NEAR, INJ),
with the shadow A/B on from day one.

**Targets:** net expectancy **> ~+0.2%/trade** after real friction (backtest was ~+0.5%);
fill rate ~85%; realized win rate ~65%.

- **GO** (scale up / add coins / consider 3x): realized net stays clearly positive after
  friction, fill rate ~80%+, no execution surprises.
- **NO-GO** (stop, debug): realized expectancy ≤ 0. Most likely cause is trailing-exit
  slippage exceeding the edge — fix by widening the trail or restricting to the most
  liquid/volatile names, then re-validate.
- **Trail decision:** after ~50 matched trades, switch `TRAIL_PCT` to 0.3% only if it
  beats 0.55% by a clear margin *after measured friction*. If the gap closes once real
  friction is applied (the likely outcome), 0.55% stays.

---

## Operations

- **Daily-DD halt** blocks new entries once down 5% on the UTC day; open positions keep
  riding their stops. Resets at the UTC rollover.
- **Reconcile** trusts the exchange as the source of truth each tick: a stop that filled
  out-of-band is detected and booked (the Farms orphan-position lesson).
- **Isolated margin** per position so one coin's stop can't cascade into others.
- **Universe maintenance:** run `python universe.py` quarterly to re-evaluate the list.
  `UNIVERSE_AUTOFILTER` stays off by default so a quiet-volume day never drops a validated
  coin mid-trial.
- **Leverage:** launch at 2x. Step to 3x only after live data confirms slippage and you've
  watched it through a real drawdown. The real cost of leverage here is *correlated*
  drawdown when alts sell off together; the daily-DD halt caps the bleed.
