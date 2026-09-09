"""Driver: which book, at what size, passes which Propr challenge."""
import csv
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import challenge_sim as C  # noqa: E402
import coin_fitness as F   # noqa: E402

TV = ("C:/Users/santo/Downloads/"
      "Soomario_v3_(WunderTrading_Ready)_BYBIT_HYPEUSDT.P_2026-09-09.csv")
FRACS = (0.02, 0.04, 0.06, 0.08, 0.12, 0.16, 0.24)


def tv_trades(since=None):
    rows = list(csv.DictReader(open(TV, encoding="utf-8-sig")))
    by = defaultdict(list)
    for r in rows:
        by[int(r["Trade number"])].append(r)
    out = []
    for _, legs in sorted(by.items()):
        ext = next((l for l in legs if l["Type"].startswith("Exit")), None)
        if not ext:
            continue
        when = datetime.strptime(ext["Date and time"], "%Y-%m-%d %H:%M")
        if since and when < since:
            continue
        out.append((when, float(ext["Return %"]), "HYPE"))
    out.sort(key=lambda x: x[0])
    return out


def harness_trades(coins, window=None):
    """Chronological (close_datetime, net_pct, coin) from the validated harness."""
    run = F.run(coins, window=window, frac=0.20)
    return [(datetime.fromtimestamp(t["closed_t"] / 1000, timezone.utc).replace(tzinfo=None),
             t["net_pct"], t["coin"]) for t in run["trades"]]


def main():
    coins = F.load(F.LIVE_BOOK, verbose=False)
    lo, hi = F.span(coins)
    print("harness data: {} .. {} ({:.0f} days, {} coins)".format(
        datetime.fromtimestamp(lo / 1000, timezone.utc).date(),
        datetime.fromtimestamp(hi / 1000, timezone.utc).date(),
        (hi - lo) / 86400000, len(coins)))

    book = harness_trades(coins)
    hype_h = harness_trades(["HYPE"])
    tv = tv_trades()
    tv12 = tv_trades(since=datetime(2025, 9, 9))
    print("  Libration book : {} trades".format(len(book)))
    print("  HYPE (harness) : {} trades".format(len(hype_h)))
    print("  HYPE (TradingView 21mo): {} · last 12mo: {}".format(len(tv), len(tv12)))

    books = [("Libration book", book), ("HYPE (harness)", hype_h),
             ("HYPE (TV 12mo)", tv12)]

    for key, ch in C.CHALLENGES.items():
        print()
        print("{:=^94}".format(
            "  {}  ${:,} | target +{}% | {} DD {}% | daily {}% | ${}  ".format(
                ch["label"], ch["size"], ch["target"], ch["type"].upper(),
                ch["dd"], ch["daily"], ch["fee"])))
        usable = max(ch["dd"] - 1.5, 0.25)
        print("  bot flattens at -{:.1f}% (guard margin 1.5 of the {}% limit)"
              .format(usable, ch["dd"]))
        print("  {:<18}{:>9}{:>9}{:>9}{:>11}{:>10}{:>13}".format(
            "book", "notional", "P(pass)", "P(fail)", "undecided", "med days",
            "med DD/HWM"))
        for label, seq in books:
            best = None
            for frac in FRACS:
                e = C.evaluate(seq, ch, frac, runs=1200)
                if not e:
                    continue
                # prefer the size that maximises P(pass); break ties on speed
                key = (e["p_pass"], -(e["median_days"] or 9e9))
                if best is None or key > (best[1]["p_pass"], -(best[1]["median_days"] or 9e9)):
                    best = (frac, e)
                print("  {:<18}{:>9.0%}{:>9.1%}{:>9.1%}{:>11.1%}{:>10}{:>12.2f}%"
                      .format(label, frac, e["p_pass"], e["p_floor"],
                              e["p_undecided"],
                              int(e["median_days"]) if e["median_days"] is not None else "-",
                              e["median_worst_dd"]))
            if best:
                print("  {:<18}-> best {:.0%} notional: P(pass) {:.1%}, "
                      "median {} days".format(
                          "", best[0], best[1]["p_pass"],
                          int(best[1]["median_days"]) if best[1]["median_days"] else "-"))
            print()


if __name__ == "__main__":
    main()
