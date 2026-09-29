"""Offline cover for the per-coin CHART timeframe - no network, no keys.

TradingView runs the script once per CHART bar and reads the RSI of whatever
timeframe the input names. The bot used to evaluate on the RSI close instead,
which is identical when the two match and wrong when they do not:

  * TAO: 1D chart, 6h RSI. TradingView looks once a day, so a cross that
    happens and unwinds inside a day is never traded. The bot traded them -
    it was long TAO on 2026-09-29 while TradingView had been flat since 09-18.
  * FARTCOIN: 30m chart, 4h RSI. The 4h value is re-read every 30m, so the
    cross is acted on within 30m of the 4h close.
"""
import os
import shutil
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "charttf_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ.update(STATE_PATH=TMP, PAPER="1", TG_ENABLED="0", COINS="HYPE,TAO,FARTCOIN,SUI",
                  RSI_TF="4h", TRAIL_ARM_DELAY_BARS="1",
                  COIN_PARAMS='{"TAO":{"rsi_tf":"6h","chart_tf":"1d"},'
                              '"FARTCOIN":{"chart_tf":"30m"},'
                              '"SUI":{"rsi_tf":"6h"}}')

import config                                    # noqa: E402
import signals                                   # noqa: E402

PASS = FAIL = 0
H = 3_600_000


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def bars(step_h, n, t0=0):
    """n closed bars of `step_h` hours, oldest first."""
    return [{"t": t0 + i * step_h * H, "T": t0 + (i + 1) * step_h * H,
             "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0} for i in range(n)]


print("\n1. config")
check("chart_tf defaults to the RSI timeframe", config.chart_tf("HYPE") == "4h")
check("TAO reads 6h RSI on a 1D chart",
      config.rsi_tf("TAO") == "6h" and config.chart_tf("TAO") == "1d")
check("FARTCOIN reads 4h RSI on a 30m chart",
      config.rsi_tf("FARTCOIN") == "4h" and config.chart_tf("FARTCOIN") == "30m")
check("the arming delay is one CHART bar", config.arm_delay_sec("TAO") == 86_400)
check("  and one 30m bar for FARTCOIN", config.arm_delay_sec("FARTCOIN") == 1_800)
check("  unchanged where the two match", config.arm_delay_sec("HYPE") == 14_400)

print("\n2. equal timeframes behave exactly as before")
rsi_bars = bars(4, 6)
vals = [45.0, 46.0, 47.0, 48.0, 49.0, 51.0]           # crosses 50 on the last bar
sig, prev, now = signals.cross_on_chart(rsi_bars, vals, rsi_bars, 50.0, 40.0)
check("long fires on the cross", sig == "long" and prev == 49.0 and now == 51.0)
check("  same as the old two-value call",
      sig == signals.entry_signal(vals[-2], vals[-1], 50.0, 40.0))
sig2, _, _ = signals.cross_on_chart(rsi_bars, [45, 46, 47, 48, 51, 52], rsi_bars, 50.0, 40.0)
check("no repeat once already above the level", sig2 is None)

print("\n3. TAO: a cross that unwinds inside the day is never traded")
six = bars(6, 8)                                      # 2 days of 6h bars
# crosses up at 12:00 on day 2 and falls back before the daily close
vals = [45.0, 46.0, 44.0, 43.0, 45.0, 52.0, 47.0, 46.0]
day = [{"t": 0, "T": 24 * H, "c": 1.0}, {"t": 24 * H, "T": 48 * H, "c": 1.0}]
sig, prev, now = signals.cross_on_chart(six, vals, day, 50.0, 40.0)
check("daily sampling reads 46.0 at the close, not the 52.0 spike", now == 46.0)
check("  so nothing fires", sig is None)
check("the 6h view WOULD have fired (the old behaviour)",
      signals.entry_signal(vals[4], vals[5], 50.0, 40.0) == "long")

print("\n4. TAO: a cross that survives to the daily close does fire")
vals = [45.0, 46.0, 44.0, 43.0, 45.0, 52.0, 53.0, 54.0]
sig, prev, now = signals.cross_on_chart(six, vals, day, 50.0, 40.0)
check("fires at the daily close", sig == "long" and prev == 43.0 and now == 54.0)

print("\n5. FARTCOIN: the 4h RSI is re-read every 30m")
four = bars(4, 3)                                      # closes at 4h, 8h, 12h
vals = [45.0, 49.0, 51.0]
half = bars(0.5, 24)                                   # 12h of 30m bars
sig, prev, now = signals.cross_on_chart(four, vals, half[:17], 50.0, 40.0)
check("nothing before the 4h close", sig is None and now == 49.0)
sig, prev, now = signals.cross_on_chart(four, vals, half[:25], 50.0, 40.0)
check("fires on the first 30m bar after it", sig == "long" and prev == 49.0 and now == 51.0)

print("\n6. sampling rules")
check("the value is the last RSI bar CLOSED by then",
      signals.rsi_at(four, vals, 8 * H) == 49.0)
check("  a bar closing later is not used", signals.rsi_at(four, vals, 8 * H - 1) == 45.0)
check("  None before any bar has closed", signals.rsi_at(four, vals, 0) is None)
check("too few chart bars is not a signal",
      signals.cross_on_chart(four, vals, half[:1], 50.0, 40.0) == (None, None, None))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
