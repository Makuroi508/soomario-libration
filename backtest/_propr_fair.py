"""Same model, same data, same window: HYPE-only vs the Libration book.

The first pass compared a 21-month TradingView record on Bybit against a
7.4-month harness run on Binance. HYPE won at every risk level, but three
confounds could produce that on their own:

  1. WINDOW   HYPE's record covers Mar/Apr 2025 (+29%, +37% months) that the
              Libration harness never sees.
  2. MODEL    TradingView's fill and commission assumptions are not this
              harness's.
  3. SELECTION HYPE is one of the eleven coins. Choosing it because its record
              looks best is the per-coin ranking this project has repeatedly
              shown does not persist.

This runs both books through the SAME harness over the SAME window, and then
asks whether HYPE's edge is stable across sub-periods of its own long record.
"""
import csv
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import propr_compare as P  # noqa: E402
import coin_fitness as F   # noqa: E402

D_MS = 86_400_000
TV = ("C:/Users/santo/Downloads/"
      "Soomario_v3_(WunderTrading_Ready)_BYBIT_HYPEUSDT.P_2026-09-09.csv")


def tv_trips():
    rows = list(csv.DictReader(open(TV, encoding="utf-8-sig")))
    by = defaultdict(list)
    for r in rows:
        by[int(r["Trade number"])].append(r)
    out = []
    for _, legs in sorted(by.items()):
        ext = next((l for l in legs if l["Type"].startswith("Exit")), None)
        if not ext:
            continue
        out.append({"close": datetime.strptime(ext["Date and time"], "%Y-%m-%d %H:%M"),
                    "ret": float(ext["Return %"]),
                    "comm": float(ext["Commission USDT"]),
                    "val": float(ext["Size (value)"])})
    out.sort(key=lambda t: t["close"])
    return out


def seq_from(run, coins=None):
    out = []
    for c, d in run["per"].items():
        if coins and c not in coins:
            continue
        for net in d["nets"]:
            out.append((None, net, c))
    return out


def main():
    tv = tv_trips()
    comm = [t["comm"] / t["val"] * 100 for t in tv if t["val"]]
    print("TradingView commission assumption: {:.4f}% per round-trip "
          "(measured live all-in friction is 0.16%)".format(st.mean(comm) * 2))
    print("  -> TradingView is {} than reality".format(
        "MORE conservative" if st.mean(comm) * 2 > 0.16 else "MORE OPTIMISTIC"))

    coins = F.load(F.LIVE_BOOK, verbose=False)
    lo, hi = F.span(coins)
    print("\nharness window: {} .. {}".format(
        datetime.fromtimestamp(lo / 1000, timezone.utc).date(),
        datetime.fromtimestamp(hi / 1000, timezone.utc).date()))

    # ── 1. same harness, same window, same rules ──
    print("\n─── 1. SAME MODEL, SAME DATA, SAME WINDOW ───")
    book = F.run(coins, frac=0.20)
    hype = F.run(["HYPE"], frac=0.20)
    for label, run in (("Libration book (11 coins)", book), ("HYPE only", hype)):
        seq = seq_from(run)
        r = P.walk(seq, daily_halt=False)
        b = P.bootstrap(seq, runs=4000)
        print("  {:<28} trades {:>5}  ret {:+7.2f}%  medDD {:>6.2f}%  "
              "P(guard) {:>5.1%}".format(
                  label, r["n"], b["ret_median"], b["mdd_median"], b["p_guard"]))

    # ── 2. same-risk on identical data ──
    print("\n─── 2. SAME-RISK, IDENTICAL DATA ───")
    print("  {:<28}{:>10}{:>12}{:>12}{:>11}".format(
        "book", "notional", "median DD", "median ret", "P(guard)"))
    for label, run in (("Libration book", book), ("HYPE only", hype)):
        for frac in (0.08, 0.16, 0.32, 0.64):
            b = P.bootstrap(seq_from(run), frac=frac, runs=2000)
            if b:
                print("  {:<28}{:>10.0%}{:>11.2f}%{:>11.2f}%{:>10.1%}".format(
                    label, frac, b["mdd_median"], b["ret_median"], b["p_guard"]))
        print()

    # ── 3. is HYPE actually the best coin, or just the one we looked at? ──
    print("─── 3. HYPE'S RANK AMONG THE ELEVEN (same window) ───")
    rows = []
    for c in coins:
        d = book["per"].get(c)
        if d and d["n"] >= 5:
            rows.append((c, d["pnl"], st.mean(d["nets"]), d["n"]))
    rows.sort(key=lambda r: -r[2])
    for i, (c, pnl, avg, n) in enumerate(rows, 1):
        mark = "  <== HYPE" if c == "HYPE" else ""
        print("  {:>2}. {:<10} avg {:+.3f}%/trade  n={:<4} net ${:>8.2f}{}".format(
            i, c, avg, n, pnl, mark))

    # ── 4. is HYPE's own edge stable across its 21 months? ──
    print("\n─── 4. HYPE STABILITY ACROSS ITS OWN 21-MONTH RECORD ───")
    n = len(tv)
    for k, label in ((3, "thirds"), (4, "quarters")):
        print("  by {}:".format(label))
        size = n // k
        for i in range(k):
            part = tv[i * size:(i + 1) * size] if i < k - 1 else tv[i * size:]
            rets = [t["ret"] for t in part]
            seq = [(None, r, "HYPE") for r in rets]
            w = P.walk(seq, daily_halt=False)
            print("    {} {} .. {}  n={:<4} avg {:+.3f}%/trade  ret {:+.2f}%  DD {:.2f}%"
                  .format(label[:-1].upper()[0] + str(i + 1),
                          part[0]["close"].date(), part[-1]["close"].date(),
                          len(part), st.mean(rets), w["ret_pct"], w["mdd"]))


if __name__ == "__main__":
    main()
