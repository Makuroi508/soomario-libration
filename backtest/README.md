# backtest/ — diversification research tooling (dev-only, not deployed)

Not part of the live bot (`Procfile` runs `app.py`; nothing here is imported by it).
Purpose: decide the coin universe + per-position sizing from evidence, under the *real*
shared-account constraints (10 slots, 20% notional, 1 position/coin, daily-DD).

**Run from the repo root, on a machine that can reach `api.hyperliquid.xyz`.**

```bash
python backtest/pull_candles.py --days 180     # writes backtest/candles/<COIN>_4h.csv
python backtest/backtest.py --compare          # current vs wider vs thinner vs minus-TAO
python backtest/backtest.py --per-coin         # per-coin expectancy for the live set
python backtest/backtest.py --notional 0.10    # price the "thin per-position" tradeoff
```

- `pull_candles.py` — HL 4h candle fetcher (public endpoint, no keys). Edit
  `DIVERSIFICATION_CANDIDATES` to test new names.
- `backtest.py` — strategy replay reusing the live `signals.py`, with the shared-account
  concurrency model. Read the file header for the intrabar fill-model assumptions and
  **validate against the live `trade_history_1.csv` before trusting absolute numbers**
  (per-coin rank + the TAO tail must reproduce).

`candles/` is git-ignored (data, not code). See `../DIVERSIFICATION_KICKOFF.md` for the
full findings and runbook.
