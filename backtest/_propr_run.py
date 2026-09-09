"""Driver: Libration book vs HYPE-only under the live Propr rules."""
import csv
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import propr_compare as P  # noqa: E402
import coin_fitness as F   # noqa: E402

TV = ("C:/Users/santo/Downloads/"
      "Soomario_v3_(WunderTrading_Ready)_BYBIT_HYPEUSDT.P_2026-09-09.csv")


def tv_hype_trades():
    """Round-trips from the TradingView HYPE export, as (close, net_pct, coin).

    TradingView's Return % is on ITS position value, which is ~100% of equity.
    Propr sizes at NOTIONAL_FRAC, so the return is carried through unscaled here
    and the scaling happens once, inside equity_impact().
    """
    rows = list(csv.DictReader(open(TV, encoding="utf-8-sig")))
    by = defaultdict(list)
    for r in rows:
        by[int(r["Trade number"])].append(r)
    out = []
    for _, legs in sorted(by.items()):
        ext = next((l for l in legs if l["Type"].startswith("Exit")), None)
        if not ext:
            continue
        out.append((datetime.strptime(ext["Date and time"], "%Y-%m-%d %H:%M"),
                    float(ext["Return %"]), "HYPE"))
    out.sort(key=lambda x: x[0])
    return out


def libration_trades(window=None):
    """Round-trips from the validated harness at the live per-coin config."""
    coins = F.load(F.LIVE_BOOK, verbose=False)
    r = F.run(coins, window=window, frac=0.20)     # frac here only sets notional
    seq = []                                        # inside the harness; we rescale
    for c, d in r["per"].items():
        for net in d["nets"]:
            seq.append(net)
    return r, coins


def show(label, res, boot):
    print("\n─── {} ───".format(label))
    print("  trades {}   return {:+.2f}%   max DD {:.2f}%".format(
        res["n"], res["ret_pct"], res["mdd"]))
    if res["guard_hit"] is not None:
        print("  *** GUARD FIRED at trade {} — bot flattens at -{:.1f}% from the "
              "high-water mark ***".format(res["guard_hit"] + 1, P.GUARD_AT))
    if res["dead_hit"] is not None:
        print("  *** ACCOUNT BREACHED -{:.0f}% — challenge over ***".format(P.MAX_DD_PCT))
    if res["guard_hit"] is None and res["dead_hit"] is None:
        print("  survived: never came within {:.1f}% of the high-water mark".format(
            P.GUARD_AT))
    if boot:
        print("  bootstrap ({} resamples, block=5 to preserve loss clustering):".format(
            boot["runs"]))
        print("    P(guard fires at -{:.1f}%) = {:.1%}    P(breach -{:.0f}%) = {:.1%}".format(
            P.GUARD_AT, boot["p_guard"], P.MAX_DD_PCT, boot["p_dead"]))
        print("    return  median {:+.2f}%   p05 {:+.2f}%   p95 {:+.2f}%".format(
            boot["ret_median"], boot["ret_p05"], boot["ret_p95"]))
        print("    max DD  median {:.2f}%   p05 (worst 5%) {:.2f}%".format(
            boot["mdd_median"], boot["mdd_p05"]))


def main():
    print("PROPR RULES: {}% trailing max DD, bot flattens at {}%, {}% daily halt, "
          "{:.0%} notional, {} slots, ${:,.0f} start".format(
              P.MAX_DD_PCT, P.GUARD_AT, P.DAILY_DD_PCT, P.NOTIONAL_FRAC,
              P.MAX_CONCURRENT, P.START_BALANCE))

    # ── HYPE only, from the 21-month TradingView record ──
    hype = tv_hype_trades()
    span = (hype[-1][0] - hype[0][0]).days
    print("\nHYPE-only source: TradingView Bybit HYPEUSDT.P, {} trades over {} days "
          "({:.1f} months)".format(len(hype), span, span / 30.44))
    h_res = P.walk(hype)
    h_boot = P.bootstrap([(None, r, c) for _, r, c in hype])
    show("HYPE ONLY @ 8% notional", h_res, h_boot)

    # ── Libration book, from the validated harness ──
    r, coins = libration_trades()
    lib_seq = []
    for c, d in r["per"].items():
        for net in d["nets"]:
            lib_seq.append((None, net, c))
    print("\nLibration source: validated harness, live per-coin config, {} coins"
          .format(len(coins)))
    l_res = P.walk(lib_seq, daily_halt=False)
    l_boot = P.bootstrap(lib_seq)
    show("LIBRATION BOOK @ 8% notional", l_res, l_boot)

    # ── same-risk comparison ──
    print("\n─── SAME-RISK VIEW ───")
    print("  Sizes each book so its bootstrap MEDIAN max drawdown is the same,")
    print("  then compares what each returns for that identical risk.")
    print("  {:<22}{:>10}{:>12}{:>12}{:>14}".format(
        "book", "notional", "median DD", "median ret", "P(guard)"))
    for label, seq in (("HYPE only", [(None, r_, c_) for _, r_, c_ in hype]),
                       ("Libration book", lib_seq)):
        for frac in (0.04, 0.08, 0.16, 0.32):
            b = P.bootstrap(seq, frac=frac, runs=2000)
            if not b:
                continue
            print("  {:<22}{:>10.0%}{:>11.2f}%{:>11.2f}%{:>13.1%}".format(
                label, frac, b["mdd_median"], b["ret_median"], b["p_guard"]))
        print()


if __name__ == "__main__":
    main()
