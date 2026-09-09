"""
hype_vs_book.py — HYPE alone vs the frozen 11-coin book, over 3/6/12/21 months.
══════════════════════════════════════════════════════════════════════════════
Three things this settles:

  1. Do my simulated HYPE exits match TradingView's? If they diverge, nothing
     downstream is trustworthy.
  2. Over matched windows, does HYPE alone really beat the book on the HYPE
     settings (frozen)?
  3. Does the answer change with the length of the window?

FEES ARE NOT COMPARABLE OUT OF THE BOX
TradingView charges 0.4008% per round-trip on this export. The friction
measured from real fills on this account is 0.16% (fees plus slippage on both
legs). TradingView is therefore ~2.5x too harsh, which makes its HYPE results
PESSIMISTIC relative to the harness. Every comparison below restates both sides
at 0.16% so the two are on the same footing; the raw TradingView numbers are
shown alongside so the adjustment is visible rather than hidden.

COMPOUNDING
Both sides compound per trade. TradingView's position value grows from $9,990
to $81,637 over the record (8.2x) because the strategy sizes off equity, and
the harness recomputes notional as NOTIONAL_FRAC x current equity every time a
position opens. So a percentage return here is a rate applied to a growing
base, not a fixed dollar amount repeated.
"""
import csv
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coin_fitness as F  # noqa: E402
import overfit_test as O  # noqa: E402

TV_FILE = ("C:/Users/santo/Downloads/"
           "Soomario_v3_(WunderTrading_Ready)_BYBIT_HYPEUSDT.P_2026-09-10.csv")
TV_FEE = 0.4008          # what TradingView charged, per round-trip
OUR_FEE = 0.16           # measured from this account's own fills
START = 2000.0
D_MS = 86_400_000


def tv_trips():
    """(exit_datetime, return% restated at OUR_FEE, raw return%)"""
    rows = list(csv.DictReader(open(TV_FILE, encoding="utf-8-sig")))
    by = defaultdict(list)
    for r in rows:
        by[int(r["Trade number"])].append(r)
    out = []
    for _, legs in sorted(by.items()):
        x = next((l for l in legs if l["Type"].startswith("Exit")), None)
        if not x:
            continue
        raw = float(x["Return %"])
        out.append((datetime.strptime(x["Date and time"], "%Y-%m-%d %H:%M"),
                    raw + (TV_FEE - OUR_FEE), raw))
    out.sort(key=lambda t: t[0])
    return out


def compound(rets, frac=1.0, start=START):
    """Equity after applying each trade's return to the running balance."""
    eq, peak, mdd = start, start, 0.0
    for r in rets:
        eq *= (1.0 + r * frac / 100.0)
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak * 100.0)
    return eq, mdd


def book_rets(coins, window, mode="frozen"):
    """Per-trade equity impact for the book, already scaled for WATCH sizing."""
    r = O.run_as(coins, mode, window=window)
    out = []
    for t in sorted(r["trades"], key=lambda z: z["closed_t"]):
        mult = 0.5 if t["coin"].upper() in F.WATCH else 1.0
        out.append(t["net_pct"] * mult)
    return out, r


def main():
    F._ORIG_PARAMS = F.params_for
    coins = F.load(F.LIVE_BOOK, verbose=False)
    lo, hi = F.span(coins)
    tv = tv_trips()
    end_dt = datetime.fromtimestamp(hi / 1000, timezone.utc).replace(tzinfo=None)

    # ── 1. do the exits match? ──
    print("═══ 1. DO MY SIMULATED HYPE EXITS MATCH TRADINGVIEW? ═══")
    w12 = (hi - 365 * D_MS, hi)
    hs = O.run_as(coins, "frozen", window=w12)
    hh = [t for t in hs["trades"] if t["coin"].upper() == "HYPE"]
    tv12 = [t for t in tv if t[0] >= end_dt - timedelta(days=365)]
    hr = [t["net_pct"] for t in hh]
    tr = [t[1] for t in tv12]
    print("  last 12 months, HYPE only, both at {}% friction:".format(OUR_FEE))
    print("    {:<22}{:>9}{:>11}{:>10}{:>10}{:>11}".format(
        "source", "trades", "per day", "win%", "avg%", "median%"))
    for lbl, s, days in (("my harness (Binance)", hr, 365),
                         ("TradingView (Bybit)", tr, 365)):
        print("    {:<22}{:>9}{:>11.2f}{:>9.1f}%{:>10.3f}{:>11.3f}".format(
            lbl, len(s), len(s) / days, 100 * sum(1 for x in s if x > 0) / len(s),
            st.mean(s), st.median(s)))
    print("    trade counts within {:.0f}%; if these diverged, nothing below would hold"
          .format(abs(len(hr) - len(tr)) / max(len(tr), 1) * 100))

    # ── 2. head to head over matched windows ──
    print()
    print("═══ 2. HYPE ALONE vs THE FROZEN BOOK ═══")
    print("  both compounding from ${:,.0f}, both at {}% friction".format(START, OUR_FEE))
    print("  HYPE sized at 20% of equity, same as one book position")
    print()
    print("  {:<12}{:>9}{:>11}{:>11}{:>10}{:>11}{:>11}{:>10}".format(
        "window", "HYPE n", "HYPE ends", "HYPE DD", "book n", "book ends",
        "book DD", "winner"))
    for label, days in (("3 months", 91), ("6 months", 182),
                        ("12 months", 365), ("21 months", 638)):
        cut_ms = hi - days * D_MS
        cut_dt = end_dt - timedelta(days=days)
        t_r = [t[1] for t in tv if t[0] >= cut_dt]
        if len(t_r) < 5:
            continue
        he, hd = compound(t_r, frac=0.20)
        if cut_ms >= lo:
            b_r, br = book_rets(coins, (cut_ms, hi))
            be, bd = compound(b_r, frac=0.20)
            win = "HYPE" if he > be else "book"
            print("  {:<12}{:>9}{:>11,.0f}{:>10.1f}%{:>10}{:>11,.0f}{:>10.1f}%{:>10}"
                  .format(label, len(t_r), he, hd, len(b_r), be, bd, win))
        else:
            print("  {:<12}{:>9}{:>11,.0f}{:>10.1f}%{:>10}{:>11}{:>10}{:>10}"
                  .format(label, len(t_r), he, hd, "-", "no data", "-", "HYPE only"))
    print()
    print("  The book has only 12 months of candles: CC, PENGU and FARTCOIN did")
    print("  not exist for most of the 21-month window, so a 21-month book number")
    print("  cannot be produced honestly.")

    # ── 3. per-window detail ──
    print()
    print("═══ 3. WHY — PER-TRADE QUALITY AND FREQUENCY ═══")
    print("  {:<12}{:>12}{:>12}{:>12}{:>12}".format(
        "window", "HYPE avg%", "book avg%", "HYPE/day", "book/day"))
    for label, days in (("3 months", 91), ("6 months", 182), ("12 months", 365)):
        cut_ms = hi - days * D_MS
        cut_dt = end_dt - timedelta(days=days)
        t_r = [t[1] for t in tv if t[0] >= cut_dt]
        if cut_ms < lo or len(t_r) < 5:
            continue
        b_r, _ = book_rets(coins, (cut_ms, hi))
        print("  {:<12}{:>12.3f}{:>12.3f}{:>12.2f}{:>12.2f}".format(
            label, st.mean(t_r), st.mean(b_r), len(t_r) / days, len(b_r) / days))


if __name__ == "__main__":
    main()
