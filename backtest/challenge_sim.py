"""
challenge_sim.py — probability of PASSING a Propr challenge, per book and size.
══════════════════════════════════════════════════════════════════════════════
A challenge is a race, not a return series: reach the profit target before
touching the drawdown floor. Expected return is close to irrelevant — what
matters is P(target before floor), and that is dominated by position size, not
by which book you run.

STATIC VS TRAILING IS THE WHOLE GAME
  1-Step (Bronze Classic/Turbo/Pro) use a STATIC floor: fixed at
  start x (1 - dd). Every dollar earned becomes permanent cushion, so the
  attempt gets safer the further ahead it goes.
  2-Step Explorer uses a TRAILING floor that ratchets with the high-water mark.
  It never gets safer: a 8% fall from ANY peak ends it, including a peak set
  after the target was already in sight.

THE GUARD MARGIN MATTERS MORE ON TIGHT CHALLENGES
The bot flattens at (max_dd - DD_GUARD_MARGIN) and stays flat until a human
intervenes, so in practice the guard IS the floor. At the live default of 1.5
points that is fine on an 8% challenge (6.5% usable) but crushing on the 3%
Turbo, which leaves only 1.5% of usable room. Both are reported.

WHY RANDOM-START RATHER THAN IID BOOTSTRAP
Resampling trades independently destroys the loss clustering that actually
causes failures. Each simulated attempt instead begins at a random point in the
real trade sequence and runs forward. An attempt that reaches the end of the
data without resolving is UNDECIDED, not failed -- these challenges have no
time limit, so it would simply still be running. That answers the question
actually being asked: "if I start this challenge at an arbitrary moment, what
happens?"
"""
import random
import statistics as st
from datetime import timedelta

WATCH = {"FARTCOIN", "JTO", "PENGU", "XMR"}
WATCH_MULT = 0.5

# Propr products, from the live pricing page (2026-09-09).
CHALLENGES = {
    "1S-Turbo":   {"size": 25000, "target": 9.0,  "dd": 3.0, "type": "static",
                   "daily": 3.0, "fee": 125, "label": "Bronze 1-Step Turbo"},
    "1S-Pro":     {"size": 25000, "target": 12.0, "dd": 5.0, "type": "static",
                   "daily": 3.0, "fee": 185, "label": "Bronze 1-Step Pro"},
    "1S-Classic": {"size": 25000, "target": 10.0, "dd": 6.0, "type": "static",
                   "daily": 3.0, "fee": 275, "label": "Bronze 1-Step Classic"},
    "2S-Explorer": {"size": 10000, "target": 5.0, "dd": 8.0, "type": "trailing",
                    "daily": 5.0, "fee": 100, "label": "Explorer 2-Step (step 1)"},
}


def impact(net_pct, coin, frac):
    """Equity move in percent: notional is frac x equity, WATCH names half that."""
    m = WATCH_MULT if (coin and coin.upper() in WATCH) else 1.0
    return net_pct * frac * m


def attempt(trades, start_i, ch, frac, guard_margin=1.5):
    """One challenge attempt beginning at trades[start_i], running FORWARD only.

    No wraparound: splicing Sep-2026 onto Sep-2025 invents a price path that
    never happened and produces negative elapsed times. An attempt that reaches
    the end of the data without resolving is reported as UNDECIDED, not as a
    failure -- these challenges have no time limit, so in reality it would
    simply still be running.

    Returns (outcome, trades_used, days_elapsed, worst_dd_from_hwm).
    """
    eq = hwm = 100.0
    target = 100.0 * (1 + ch["target"] / 100.0)
    usable = max(ch["dd"] - guard_margin, 0.25)     # where the bot flattens
    day, day_start, halted = None, eq, False
    t0 = trades[start_i][0]
    worst_hwm = 0.0

    for k in range(start_i, len(trades)):
        when, net, coin = trades[k]
        if when is not None:
            d = when.date()
            if d != day:
                day, day_start, halted = d, eq, False
            if halted:
                continue

        eq *= (1.0 + impact(net, coin, frac) / 100.0)
        hwm = max(hwm, eq)
        worst_hwm = min(worst_hwm, (eq - hwm) / hwm * 100.0)

        anchor = hwm if ch["type"] == "trailing" else 100.0
        cur_dd = (eq - anchor) / anchor * 100.0

        if when is not None and day_start > 0:
            if (eq - day_start) / day_start * 100.0 <= -ch["daily"]:
                halted = True

        days = (when - t0).days if (when and t0) else (k - start_i)
        if cur_dd <= -usable:
            return "floor", k - start_i + 1, days, worst_hwm
        if eq >= target:
            return "pass", k - start_i + 1, days, worst_hwm

    last = trades[-1][0]
    days = (last - t0).days if (last and t0) else (len(trades) - start_i)
    return "undecided", len(trades) - start_i, days, worst_hwm


def evaluate(trades, ch, frac, runs=2000, guard_margin=1.5, seed=5,
             min_runway=0.35):
    """P(pass) over random start points.

    Starts are drawn from the first (1 - min_runway) of the record so every
    attempt has room to resolve; without that, late starts are structurally
    undecided and would drag the pass rate down for a reason unrelated to the
    strategy.
    """
    rnd = random.Random(seed)
    n = len(trades)
    if n < 40:
        return None
    hi = max(1, int(n * (1 - min_runway)))
    out = {"pass": 0, "floor": 0, "undecided": 0}
    days, worsts = [], []
    for _ in range(runs):
        i = rnd.randrange(0, hi)
        res, k, d, w = attempt(trades, i, ch, frac, guard_margin)
        out[res] += 1
        worsts.append(w)
        if res == "pass":
            days.append(d)
    decided = out["pass"] + out["floor"]
    return {
        "p_pass": out["pass"] / runs,
        "p_floor": out["floor"] / runs,
        "p_undecided": out["undecided"] / runs,
        "p_pass_decided": (out["pass"] / decided) if decided else None,
        "median_days": st.median(days) if days else None,
        "median_worst_dd": st.median(worsts),
    }
