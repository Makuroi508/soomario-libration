"""
propr_compare.py — Libration book vs HYPE-only, judged as a PROP account.
═════════════════════════════════════════════════════════════════════════
THE OBJECTIVE IS NOT RETURN
On the Hyperliquid vault the question is "what compounds fastest". On a Propr
challenge it is "what reaches the profit target without ever touching the
trailing drawdown floor", because touching it ends the account permanently.
A strategy that returns more but breaches once scores zero, so the two are
ranked here on survival first and return second.

THE ACTUAL CONSTRAINT (from the live Propr env)
  MAX_DD_PCT=8, DD_TYPE=trailing   -> floor sits 8% below the venue high-water
                                      mark and RATCHETS UP as equity makes highs
  DD_GUARD_MARGIN=1.5              -> the bot flattens at 6.5% below the HWM,
                                      1.5 points early, so a gap cannot carry
                                      it through the real floor
  DAILY_DD_PCT=3                   -> new entries stop for the UTC day at -3%
  NOTIONAL_FRAC=0.08, LEVERAGE=2   -> a position is 8% of equity notional, so a
                                      10% hard stop costs 0.8% of equity
  MAX_CONCURRENT=6                 -> capped BELOW leverage/frac (=25), so gross
                                      exposure tops out near 48%, not 200%

WHY A SINGLE HISTORICAL PATH IS NOT ENOUGH
Whether a given run breaches depends on the ORDER trades arrived in. One
ordering that survived says little about a strategy whose losses cluster. Both
books are therefore bootstrapped: resample the trade sequence many times and
count how often the floor is touched. That frequency, not the historical
maximum drawdown, is the number a prop account should be chosen on.
"""
import argparse
import random
import statistics as st
from collections import defaultdict
from datetime import datetime

# Live Propr environment
MAX_DD_PCT = 8.0
DD_GUARD_MARGIN = 1.5
GUARD_AT = MAX_DD_PCT - DD_GUARD_MARGIN      # 6.5% — where the bot flattens
DAILY_DD_PCT = 3.0
NOTIONAL_FRAC = 0.08
MAX_CONCURRENT = 6
WATCH_MULT = 0.5
START_BALANCE = 5000.0
WATCH = {"FARTCOIN", "JTO", "PENGU", "XMR"}


def equity_impact(net_pct, coin=None, frac=NOTIONAL_FRAC):
    """A trade's effect on ACCOUNT equity, in percent.

    P&L = notional * net_pct, and notional = frac * equity, so the equity move
    is net_pct * frac. WATCH names trade at half size.
    """
    mult = WATCH_MULT if (coin and coin.upper() in WATCH) else 1.0
    return net_pct * frac * mult


def walk(seq, frac=NOTIONAL_FRAC, start=START_BALANCE, daily_halt=True):
    """Run a trade sequence through the prop rules.

    seq: list of (close_datetime_or_None, net_pct, coin). Returns the path and
    the first rule that fired, if any.
    """
    eq = start
    hwm = start
    day = None
    day_start = start
    halted_today = False
    curve = []
    guard_hit = None
    dead_hit = None

    for i, (when, net, coin) in enumerate(seq):
        if daily_halt and when is not None:
            d = when.date()
            if d != day:
                day, day_start, halted_today = d, eq, False
            if halted_today:
                continue                     # entries stop; open trades already closed

        eq *= (1.0 + equity_impact(net, coin, frac) / 100.0)
        hwm = max(hwm, eq)
        dd = (eq - hwm) / hwm * 100.0
        curve.append(eq)

        if daily_halt and when is not None and day_start > 0:
            if (eq - day_start) / day_start * 100.0 <= -DAILY_DD_PCT:
                halted_today = True

        if dead_hit is None and dd <= -MAX_DD_PCT:
            dead_hit = i
        if guard_hit is None and dd <= -GUARD_AT:
            guard_hit = i
            break                            # bot flattens and stays flat

    peak, mdd = start, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100.0)
    return {"end": eq, "ret_pct": (eq / start - 1) * 100.0, "mdd": mdd,
            "guard_hit": guard_hit, "dead_hit": dead_hit, "n": len(curve),
            "curve": curve}


def bootstrap(trades, frac=NOTIONAL_FRAC, runs=5000, seed=11, block=5):
    """Resample the trade sequence to estimate how often the floor is touched.

    Sampled in BLOCKS rather than independently: this strategy's losses cluster
    (stops are driven by market-wide moves), and independent resampling would
    break exactly the dependence that causes a breach.
    """
    rnd = random.Random(seed)
    n = len(trades)
    if n < 20:
        return None
    guard = dead = 0
    rets, mdds = [], []
    for _ in range(runs):
        seq = []
        while len(seq) < n:
            i = rnd.randrange(0, n)
            seq.extend(trades[i:i + block])
        seq = seq[:n]
        r = walk([(None, t[1], t[2]) for t in seq], frac=frac, daily_halt=False)
        guard += r["guard_hit"] is not None
        dead += r["dead_hit"] is not None
        rets.append(r["ret_pct"])
        mdds.append(r["mdd"])
    rets.sort(); mdds.sort()
    return {"runs": runs,
            "p_guard": guard / runs, "p_dead": dead / runs,
            "ret_median": rets[runs // 2],
            "ret_p05": rets[int(runs * .05)], "ret_p95": rets[int(runs * .95)],
            "mdd_median": mdds[runs // 2], "mdd_p05": mdds[int(runs * .05)]}
