# Libration rules on the Rotation stock universe

Does the frozen RSI-swing strategy still carry an edge on the equities Rotation
trades? This harness answers that without re-tuning anything: `libration_bt.py`
imports `signals.py` from the live bot, so the entry logic under test is the
entry logic in production.

## Run it

The Claude Code web sandbox cannot reach `api.hyperliquid.xyz`, so the fetch step
has to run somewhere with open egress.

```bash
pip install requests

# 1. control run FIRST — Libration's own coins, the book we can check against
python3 backtest/fetch_candles.py --prefix '' --days 180 --out backtest/data_crypto \
    --symbols HYPE,CC,SOL,ONDO,KPEPE,SUI,LINK,FARTCOIN,JTO,PENGU,XMR
python3 backtest/libration_bt.py --data backtest/data_crypto

# 2. the actual question — Rotation's universe as HL stock perps
python3 backtest/fetch_candles.py --days 180 --out backtest/data
python3 backtest/libration_bt.py --data backtest/data
```

**Do step 1 first and read the result before trusting step 2.** Over roughly
Jun 8 – Aug 30 the live book did +16.2% on 611 closed trades at an 82.7% win rate
and a 12.67% max drawdown. If the control run does not land near that, the
harness is wrong and the stock numbers mean nothing. Treat that comparison as
the gate, not a formality.

## Why Hyperliquid stock perps and not NYSE data

Rotation's universe is listed on HL under an `xyz:` prefix (`xyz:TSLA`,
`xyz:NVDA`, …), and those perps trade continuously. That matters more than it
sounds. The strategy's exit is a 0.55% trailing stop; on a market that closes
overnight and at weekends, price routinely gaps several times that distance
while the stop cannot fire, so every exit lands far from its trigger. Testing on
NYSE bars would mostly measure gap risk, and testing on NYSE bars *without*
modelling the gaps would manufacture an edge that does not exist.

The continuous HL series is also the venue Libration would actually execute on,
which makes the test decision-relevant rather than academic.

Four names in Rotation's universe (MRVL, ARM, AVGO, SPCX) are not on HL stock
perps — the live bot already falls back to yfinance for their prices. The fetch
script reports and skips them. Read any result as covering the twelve that are
listed.

## What the model does and does not do

Ported verbatim from `config.py`: RSI(14) on 4h closes, long on a cross up
through 50, short on a cross down through 40, 0.55% trail with a 1-bar arming
delay, 10% hard stop, 20% of mark-to-market equity per position, 2x, 10
concurrent, one position per symbol, 5% daily-drawdown halt.

Deliberate modelling choices:

- **Exits resolve on 15m bars.** A 0.55% trail cannot be simulated on 4h OHLC —
  these names range well over 1% in four hours, so a 4h fill model returns
  whatever you want it to. Within a bar the stop is checked *before* the peak
  advances, so the trail never ratchets on information from later in that bar.
  This is conservative: real Libration polls every ~60s and would ratchet faster.
- **Gaps fill at the open.** A bar that opens through the stop books the open,
  not the stop level.
- **Costs default to 4.36 bps round trip**, which is what the live book actually
  paid across 611 trades (`fee / entry notional` from `/api/trades`), split
  evenly across the legs. `libration_bt.py` also prints 4.5 bps/side (HL taker)
  and 10 bps/side stress cases automatically, because the live gross edge is
  only ~0.17%/trade — cost assumptions can flip the sign, so they are reported
  rather than buried.

Not modelled: funding on perps, borrow, partial fills, and depth. On thin stock
perps the last two are real; treat a marginal positive result as unproven.

## Files

| | |
|---|---|
| `fetch_candles.py` | pulls 4h + 15m OHLCV from HL's public `candleSnapshot` |
| `libration_bt.py` | the engine; imports `signals.py` from the live bot |
| `selftest.py` | synthetic fixtures pinning entry, arming delay, trail ratchet, hard stop, gap fill |

`python3 backtest/selftest.py` must print `all mechanics verified` before a run
is worth reading.
