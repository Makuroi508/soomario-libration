"""
report.py — financial report bundle for Libration.
═══════════════════════════════════════════════════
Builds the same *kind* of artefact as the Accumulator report (operator report +
marketing pack + machine-readable JSON), but with metrics that fit THIS
instrument. Accumulator accumulates unrealized positions funded by
contributions, so its whole apparatus is time-weighted return to strip cash
flows. Libration is one flow-neutral account turning over realized round-trips,
so the interesting numbers are the exit distribution and the tail — not TWR.

FOUR RULES CARRIED OVER FROM THE REFERENCE, DELIBERATELY
  1. A figure is a RECORD, not a number: value + n + window + basis + status +
     warnings. The basis line is what stops a figure being quoted wrong.
  2. Every figure lands in a LEDGER as publishable / annualized-short-window /
     insufficient-data. Nothing is silently omitted.
  3. DO_NOT_PUBLISH names metrics that are wrong or unstable, WITH the reason.
     Marketing rendering refuses to emit them.
  4. Reports RECONCILE and say so. `reconciliation.ok` is part of the payload.

RETURNS ARE ANCHORED ON THE TRADE LEDGER, NOT ON EQUITY
`db.realized_pnl()` rebuilds P&L from the trades table, so it is immune to
deposits and withdrawals. Libration has NO flows table, so an equity-delta
return would book a deposit as profit. Everything here therefore derives from
per-trade P&L: pnl = (exit - entry) * side_sign * qty - fee.

NO NETWORK AT REPORT TIME. Everything comes from SQLite. Charts are hand-rolled
inline SVG (the venv has no matplotlib and this must print to PDF with no
external assets).
"""
from __future__ import annotations

import io
import json
import math
import random
import statistics as st
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import config

# ── figure status ────────────────────────────────────────────────
OK = "OK"
ANNUALIZED_SHORT_WINDOW = "ANNUALIZED_SHORT_WINDOW"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

MIN_N_FOR_RATIO = 30          # below this, ratio stats are noise
YEAR_DAYS = 365               # crypto trades every day

ANNUALIZED_WARNING = (
    "Annualized from {days} days of data — a projection, not an achieved annual "
    "return. Short windows exaggerate: small differences in the underlying period "
    "become large differences once annualized."
)

# Figures that ARE published but must not travel alone. The reference report's
# rule: the caveat is part of the quotable sentence, so the number cannot be
# lifted out of context without also lifting the qualification.
CONTEXT_REQUIRED = {
    "win_rate": (
        "Win rate is not a performance figure for this strategy on its own. The "
        "live account posted its HIGHEST win rate (82%) during a LOSING three-week "
        "period, because nine hard stops clustered. Always quote it beside tail "
        "concentration and the hard-stop rate."
    ),
    "profit_factor": (
        "A ratio of sums, so a handful of outliers can dominate it at this sample "
        "size. Read it beside 'Net P&L excluding largest winner' — if that figure "
        "moves materially, the profit factor rests on one or two trades."
    ),
    "cagr": (
        "Annualized from under a year. Quote the cumulative return as the headline "
        "and CAGR only with its window stated."
    ),
}

# Metrics that must never reach a marketing surface, and why. Each entry is a
# finding, not an opinion — see the reason string.
DO_NOT_PUBLISH = {
    "per_coin_ranking": (
        "Per-coin P&L does not persist: split-half correlation r=0.177 across the "
        "live record, and a worst-coin as bad as the observed one arises by chance "
        "with p=0.086. Publishing a best/worst coin publishes noise."
    ),
    "sharpe_from_trade_returns": (
        "Sharpe computed on per-trade returns and annualized as if daily is an "
        "artifact — at ~9 trades/day it inflates by roughly sqrt(9). Use `sharpe`, "
        "which is computed on DAILY equity returns, and quote its confidence interval."
    ),
}


def _fig(key, label, value, unit="", n=None, days=None, basis="", status=OK,
         warnings=None, detail=None, publishable=True):
    """One figure record. Mirrors the reference schema field-for-field so the
    two report families can share tooling."""
    return {"key": key, "label": label, "value": value, "unit": unit, "n": n,
            "window_days": days, "basis": basis, "status": status,
            "warnings": warnings or [], "detail": detail or {},
            "publishable": publishable}


def _annualized(fig, days):
    """Stamp a figure as a short-window projection. The reference's single most
    useful habit: the caveat travels WITH the number, not in a footnote."""
    if days < YEAR_DAYS:
        fig["status"] = ANNUALIZED_SHORT_WINDOW
        fig["warnings"].append(ANNUALIZED_WARNING.format(days=days))
    return fig


# ── primitives ───────────────────────────────────────────────────
def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def trade_pnl(t):
    """Net P&L for one trade, reconstructed exactly as db.realized_pnl() does so
    the two can never disagree."""
    e, x, q = t.get("entry"), t.get("exit"), t.get("qty")
    if e is None or x is None or q is None:
        return None
    sign = 1.0 if t.get("side") == "long" else -1.0
    return (float(x) - float(e)) * sign * float(q) - float(t.get("fee") or 0.0)


def _daily_equity(trades, inception):
    """Realized-equity series, one point per calendar day with activity.

    Built from the trade ledger rather than the equity log so a deposit cannot
    masquerade as a gain. Returns [(date, equity)] ascending.
    """
    by_day = defaultdict(float)
    for t in trades:
        d = _parse(t.get("closed_at"))
        p = trade_pnl(t)
        if d is None or p is None:
            continue
        by_day[d.date()] += p
    if not by_day:
        return []
    lo, hi = min(by_day), max(by_day)
    out, eq, day = [], float(inception or 0.0), lo
    while day <= hi:
        eq += by_day.get(day, 0.0)
        out.append((day, eq))
        day += timedelta(days=1)
    return out


def _max_drawdown(series):
    """Peak-to-trough on the realized-equity curve. Returns (pct, peak_date, trough_date)."""
    if not series:
        return 0.0, None, None
    peak, peak_d = series[0][1], series[0][0]
    worst, pd_, td_ = 0.0, None, None
    for d, v in series:
        if v > peak:
            peak, peak_d = v, d
        if peak > 0:
            dd = (v - peak) / peak * 100
            if dd < worst:
                worst, pd_, td_ = dd, peak_d, d
    return worst, pd_, td_


# ── section 1: account ───────────────────────────────────────────
def _account_section(trades, inception, days, upnl):
    figs, series = {}, _daily_equity(trades, inception)
    pnls = [p for p in (trade_pnl(t) for t in trades) if p is not None]
    realized = sum(pnls)
    end_eq = (inception or 0.0) + realized

    figs["realized_pnl"] = _fig(
        "realized_pnl", "Realized P&L", round(realized, 2), "USD", len(pnls), days,
        "Sum of (exit-entry)*side*qty - fee over every closed round-trip. Rebuilt "
        "from the trade ledger, so deposits and withdrawals cannot inflate it.")

    if inception:
        figs["return_on_inception"] = _fig(
            "return_on_inception", "Return on starting capital",
            round(realized / inception * 100, 2), "%", len(pnls), days,
            "Realized P&L / ${:,.2f} starting equity. Realized only — open "
            "positions are excluded.".format(inception))

    # Daily returns drive every ratio statistic. Per-trade returns would inflate
    # them by ~sqrt(trades per day) — see DO_NOT_PUBLISH.sharpe_from_trade_returns.
    rets = []
    for i in range(1, len(series)):
        prev = series[i - 1][1]
        if prev > 0:
            rets.append((series[i][1] - prev) / prev)

    if len(rets) >= 2:
        sd = st.pstdev(rets)
        figs["volatility"] = _annualized(_fig(
            "volatility", "Realized volatility (annualized)",
            round(sd * math.sqrt(YEAR_DAYS) * 100, 2), "%", len(rets), days,
            "std-dev of DAILY realized-equity returns x sqrt(365)"), days)

        mean_d = st.mean(rets)
        if sd > 0:
            sharpe = mean_d / sd * math.sqrt(YEAR_DAYS)
            se = math.sqrt((1 + sharpe ** 2 / 2) / len(rets))   # Lo (2002) approximation
            f = _fig("sharpe", "Sharpe ratio", round(sharpe, 2), "", len(rets), days,
                     "mean/std of daily realized-equity returns x sqrt(365), rf=0",
                     detail={"ci95_low": round(sharpe - 1.96 * se, 2),
                             "ci95_high": round(sharpe + 1.96 * se, 2)})
            f["warnings"].append(
                "95% confidence interval {} to {} — wide intervals are normal at "
                "this sample size.".format(f["detail"]["ci95_low"],
                                           f["detail"]["ci95_high"]))
            figs["sharpe"] = _annualized(f, days)

        down = [r for r in rets if r < 0]
        if down:
            dsd = math.sqrt(sum(r * r for r in down) / len(rets))
            if dsd > 0:
                figs["sortino"] = _annualized(_fig(
                    "sortino", "Sortino ratio",
                    round(mean_d / dsd * math.sqrt(YEAR_DAYS), 2), "", len(rets), days,
                    "downside deviation, MAR = 0%"), days)

    dd, pk, tr = _max_drawdown(series)
    f = _fig("max_drawdown", "Maximum drawdown", round(dd, 2), "%", len(series), days,
             "peak-to-trough on the realized-equity curve",
             detail={"peak_date": str(pk) if pk else None,
                     "trough_date": str(tr) if tr else None})
    f["warnings"].append(
        "A running extremum: it can only grow with a longer sample, so this is a "
        "lower bound on observed history, not an estimate of future risk.")
    figs["max_drawdown"] = f

    if series:
        peak, uw = series[0][1], 0
        for _, v in series:
            peak = max(peak, v)
            if v < peak:
                uw += 1
        figs["time_under_water"] = _fig(
            "time_under_water", "Time under water", round(uw / len(series) * 100, 1),
            "%", len(series), days,
            "share of days below the running equity high-water mark")

    calendar, figs = _investor_metrics(series, trades, inception, days, figs)

    return {"label": "Account", "days": days, "calendar": calendar,
            "capital": {"inception": round(inception or 0.0, 2),
                        "realized_pnl": round(realized, 2),
                        "unrealized_pnl": round(upnl or 0.0, 2),
                        "end_equity_realized": round(end_eq, 2),
                        "end_equity_incl_open": round(end_eq + (upnl or 0.0), 2),
                        "note": "Realized equity is the durable figure; unrealized "
                                "moves with open marks and is shown separately."},
            "figures": figs,
            "curve": [{"d": str(d), "v": round(v, 2)} for d, v in series]}


# ── section 1b: investor metrics ─────────────────────────────────
def _investor_metrics(series, trades, inception, days, figs):
    """The figures an investor or trader expects to see, added to `figs`.

    Two are here under protest and carry it in their warnings: CAGR annualizes a
    sub-year window, and profit factor is a ratio of sums that a couple of
    outliers can dominate. Both are standard, both are requested, and both are
    reported WITH the caveat rather than omitted — the reference report's rule is
    that the caveat travels with the number, not that awkward numbers disappear.
    """
    if not series:
        return {}, {}

    start_eq = inception if inception else series[0][1]
    end_eq = series[-1][1]
    yrs = max(days, 1) / YEAR_DAYS

    # ── CAGR ──
    if start_eq > 0 and end_eq > 0:
        cagr = ((end_eq / start_eq) ** (1 / yrs) - 1) * 100
        f = _fig("cagr", "CAGR", round(cagr, 2), "%", len(series), days,
                 "({:,.2f} / {:,.2f}) ^ (365/{}) - 1, on realized equity".format(
                     end_eq, start_eq, days))
        if days < YEAR_DAYS:
            f["status"] = ANNUALIZED_SHORT_WINDOW
            f["warnings"].append(ANNUALIZED_WARNING.format(days=days))
            f["warnings"].append(CONTEXT_REQUIRED["cagr"])
            f["warnings"].append(
                "Compounding {} days to a full year magnifies the window: a period "
                "that happened to run well projects to an annual figure the "
                "strategy has never achieved. Quote the cumulative return "
                "alongside it.".format(days))
        figs["cagr"] = f

    # ── drawdown from the top (where the account sits right now) ──
    peak = max(v for _, v in series)
    cur_dd = (end_eq - peak) / peak * 100 if peak > 0 else 0.0
    peak_d = next((str(d) for d, v in series if v == peak), None)
    figs["drawdown_from_top"] = _fig(
        "drawdown_from_top", "Drawdown from the top", round(cur_dd, 2), "%",
        len(series), days,
        "current realized equity {:,.2f} against the all-time peak {:,.2f} "
        "(set {})".format(end_eq, peak, peak_d),
        detail={"peak": round(peak, 2), "peak_date": peak_d,
                "current": round(end_eq, 2)})

    # ── per-month drawdown + calendar returns ──
    by_month = defaultdict(list)
    for d, v in series:
        by_month[d.strftime("%Y-%m")].append((d, v))
    monthly = []
    for m in sorted(by_month):
        pts = by_month[m]
        base = pts[0][1]
        mpeak, mworst = base, 0.0
        for _, v in pts:
            mpeak = max(mpeak, v)
            if mpeak > 0:
                mworst = min(mworst, (v - mpeak) / mpeak * 100)
        ret = (pts[-1][1] / base - 1) * 100 if base > 0 else 0.0
        monthly.append({"month": m, "return_pct": round(ret, 2),
                        "max_drawdown_pct": round(mworst, 2), "days": len(pts)})
    if monthly:
        worst_m = min(monthly, key=lambda x: x["max_drawdown_pct"])
        figs["monthly_max_drawdown"] = _fig(
            "monthly_max_drawdown", "Worst monthly drawdown",
            worst_m["max_drawdown_pct"], "%", len(monthly), days,
            "deepest peak-to-trough WITHIN a single calendar month ({}). Measured "
            "inside each month, so it is not the same as the all-time drawdown."
            .format(worst_m["month"]),
            detail={"month": worst_m["month"]})
        best = max(monthly, key=lambda x: x["return_pct"])
        worst = min(monthly, key=lambda x: x["return_pct"])
        figs["best_month"] = _fig(
            "best_month", "Best month", best["return_pct"], "%", len(monthly), days,
            "calendar-month return on realized equity ({})".format(best["month"]))
        figs["worst_month"] = _fig(
            "worst_month", "Worst month", worst["return_pct"], "%", len(monthly), days,
            "calendar-month return on realized equity ({})".format(worst["month"]))
        if len(monthly) < 3:
            for k in ("best_month", "worst_month", "monthly_max_drawdown"):
                figs[k]["warnings"].append(
                    "Only {} calendar months in the sample, and the first and last "
                    "are partial.".format(len(monthly)))

    # ── risk-adjusted on drawdown ──
    mdd = figs.get("max_drawdown", {}).get("value")
    if mdd and mdd < 0:
        if "cagr" in figs:
            figs["calmar"] = _annualized(_fig(
                "calmar", "Calmar ratio", round(figs["cagr"]["value"] / abs(mdd), 2),
                "", len(series), days, "CAGR / |maximum drawdown|"), days)
        net = end_eq - start_eq
        dd_usd = abs(mdd) / 100 * max(v for _, v in series)
        if dd_usd > 0:
            figs["recovery_factor"] = _fig(
                "recovery_factor", "Recovery factor", round(net / dd_usd, 2), "",
                len(series), days,
                "net profit {:,.2f} / peak drawdown {:,.2f} in dollars".format(
                    net, dd_usd))

    # ── trade-shape figures ──
    pnls = [p for p in (trade_pnl(t) for t in trades) if p is not None]
    if pnls:
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        if wins and losses and st.mean(losses) != 0:
            figs["win_loss_ratio"] = _fig(
                "win_loss_ratio", "Average win / average loss",
                round(st.mean(wins) / abs(st.mean(losses)), 2), "", len(pnls), days,
                "mean winning trade {} / mean losing trade {}".format(
                    _money(st.mean(wins)), _money(abs(st.mean(losses)))))
        figs["largest_win"] = _fig(
            "largest_win", "Largest single win", round(max(pnls), 2), "USD",
            len(pnls), days, "best round-trip in the window")
        figs["largest_loss"] = _fig(
            "largest_loss", "Largest single loss", round(min(pnls), 2), "USD",
            len(pnls), days, "worst round-trip in the window")
        figs["trades_per_month"] = _fig(
            "trades_per_month", "Trades per month",
            round(len(pnls) / max(days / 30.44, 0.1), 1), "", len(pnls), days,
            "{} closed round-trips over {} days".format(len(pnls), days))

    holds = []
    for t in trades:
        o, x = _parse(t.get("opened_at")), _parse(t.get("closed_at"))
        if o and x:
            holds.append((x - o).total_seconds() / 3600)
    if holds:
        figs["avg_hold_hours"] = _fig(
            "avg_hold_hours", "Average holding period", round(st.mean(holds), 1),
            "hours", len(holds), days,
            "entry to exit, mean; median {:.1f}h".format(st.median(holds)),
            detail={"median_hours": round(st.median(holds), 1),
                    "max_hours": round(max(holds), 1)})

    # ── daily-return tail risk ──
    rets = []
    for i in range(1, len(series)):
        prev = series[i - 1][1]
        if prev > 0:
            rets.append((series[i][1] - prev) / prev * 100)
    if len(rets) >= 20:
        srt = sorted(rets)
        var5 = srt[max(0, int(len(srt) * 0.05) - 1)]
        tail = [r for r in srt if r <= var5]
        figs["var_95_daily"] = _fig(
            "var_95_daily", "Daily VaR (95%)", round(var5, 2), "%", len(rets), days,
            "5th percentile of daily realized-equity returns: one day in twenty "
            "was worse than this")
        if tail:
            figs["cvar_95_daily"] = _fig(
                "cvar_95_daily", "Daily CVaR (95%)", round(st.mean(tail), 2), "%",
                len(tail), days, "mean of the worst 5% of days")
        figs["best_day"] = _fig("best_day", "Best day", round(max(rets), 2), "%",
                                len(rets), days, "best single realized-equity day")
        figs["worst_day"] = _fig("worst_day", "Worst day", round(min(rets), 2), "%",
                                 len(rets), days, "worst single realized-equity day")

    calendar = {"months": monthly,
                "note": "Monthly returns are computed on the realized-equity curve, "
                        "so open positions cannot flatter a month. The first and "
                        "last months are partial."}
    return calendar, figs


# ── section 2: the tail (Libration's defining risk) ──────────────
def _tail_section(trades, days):
    """The section with no Accumulator analogue, and the one that matters most.

    This strategy's P&L is not governed by win rate but by how much of the gross
    winnings the hard-stop tail eats. The live account posted its HIGHEST win
    rate (82%) during a LOSING three-week period, because nine stops clustered.
    """
    rows = [(t, trade_pnl(t)) for t in trades]
    rows = [(t, p) for t, p in rows if p is not None]
    if not rows:
        return {"label": "Tail & exits", "figures": {}, "by_reason": {}}

    figs = {}
    wins = [p for _, p in rows if p > 0]
    losses = [p for _, p in rows if p <= 0]
    gross_win = sum(wins)
    hard = [(t, p) for t, p in rows if (t.get("exit_reason") or "") == "HARD_STOP"]
    hard_pnl = sum(p for _, p in hard)

    by_reason = {}
    for reason in sorted({(t.get("exit_reason") or "UNKNOWN") for t, _ in rows}):
        sub = [p for t, p in rows if (t.get("exit_reason") or "UNKNOWN") == reason]
        by_reason[reason] = {"n": len(sub), "total_pnl": round(sum(sub), 2),
                             "avg_pnl": round(st.mean(sub), 2),
                             "share_of_trades_pct": round(len(sub) / len(rows) * 100, 1)}

    if gross_win > 0:
        figs["tail_concentration"] = _fig(
            "tail_concentration", "Tail concentration",
            round(abs(hard_pnl) / gross_win * 100, 1), "%", len(rows), days,
            "Absolute hard-stop losses / gross winnings. {} of {} won is "
            "consumed by {} hard stops.".format(_money(abs(hard_pnl)), _money(gross_win), len(hard)),
            detail={"hard_stop_pnl": round(hard_pnl, 2),
                    "gross_winnings": round(gross_win, 2)})
        figs["tail_concentration"]["warnings"].append(
            "This is the strategy's defining risk metric. Net profit is whatever "
            "survives the hard-stop tail; win rate does not describe it.")

    figs["hard_stop_rate"] = _fig(
        "hard_stop_rate", "Hard-stop rate", round(len(hard) / len(rows) * 100, 2), "%",
        len(rows), days, "share of round-trips exiting at the 10% hard stop")

    figs["win_rate"] = _fig(
        "win_rate", "Win rate", round(len(wins) / len(rows) * 100, 2), "%", len(rows),
        days, "share of round-trips with net P&L > 0")
    figs["win_rate"]["warnings"].append(CONTEXT_REQUIRED["win_rate"])

    if wins and losses:
        aw, al = st.mean(wins), st.mean(losses)
        figs["expectancy"] = _fig(
            "expectancy", "Expectancy per trade",
            round(st.mean([p for _, p in rows]), 3), "USD", len(rows), days,
            "mean net P&L per round-trip. avg win {} x {}, avg loss "
            "{} x {}.".format(_money(aw), len(wins), _money(al), len(losses)),
            detail={"avg_win": round(aw, 2), "avg_loss": round(al, 2)})
        if sum(losses) != 0:
            figs["profit_factor"] = _fig(
                "profit_factor", "Profit factor",
                round(gross_win / abs(sum(losses)), 2), "", len(rows), days,
                "gross winnings {} / gross losses {}".format(
                    _money(gross_win), _money(abs(sum(losses)))))
            figs["profit_factor"]["warnings"].append(CONTEXT_REQUIRED["profit_factor"])

    # Robustness: does a single trade carry the whole record?
    allp = [p for _, p in rows]
    if len(allp) > 2:
        figs["pnl_excl_largest_win"] = _fig(
            "pnl_excl_largest_win", "Net P&L excluding largest winner",
            round(sum(allp) - max(allp), 2), "USD", len(allp) - 1, days,
            "total minus the single best trade ({}). If this flips the sign, "
            "the record rests on one trade.".format(_money(max(allp))))
        figs["pnl_excl_largest_loss"] = _fig(
            "pnl_excl_largest_loss", "Net P&L excluding largest loser",
            round(sum(allp) - min(allp), 2), "USD", len(allp) - 1, days,
            "total minus the single worst trade ({}).".format(_money(min(allp))))

    # Clustering — stops arrive together, which is why more coins cannot diversify them.
    stop_days = Counter()
    for t, _ in hard:
        d = _parse(t.get("closed_at"))
        if d:
            stop_days[d.date()] += 1
    if stop_days:
        figs["stop_clustering"] = _fig(
            "stop_clustering", "Hard stops per affected day",
            round(len(hard) / len(stop_days), 2), "", len(hard), days,
            "{} hard stops fell on {} distinct days (worst day: {}).".format(
                len(hard), len(stop_days), max(stop_days.values())),
            detail={"distinct_days": len(stop_days),
                    "worst_day": max(stop_days.values()),
                    "worst_date": str(max(stop_days, key=stop_days.get))})
        figs["stop_clustering"]["warnings"].append(
            "Stops cluster in time rather than across coins — they are driven by "
            "market-wide moves, so widening the coin pool does not diversify them.")

    _EPOCH = datetime.min.replace(tzinfo=timezone.utc)
    worst_streak = cur = 0
    for _, p in sorted(rows, key=lambda r: _parse(r[0].get("closed_at")) or _EPOCH):
        cur = cur + 1 if p <= 0 else 0
        worst_streak = max(worst_streak, cur)
    figs["max_consecutive_losses"] = _fig(
        "max_consecutive_losses", "Longest losing streak", worst_streak, "trades",
        len(rows), days, "consecutive round-trips with net P&L <= 0")

    return {"label": "Tail & exits", "figures": figs, "by_reason": by_reason}


# ── section 3: execution quality ─────────────────────────────────
def _execution_section(db, trades, days):
    """What the bot cost to run, versus what it planned.

    friction_pct is the bot's own plan-vs-actual measure (intended return minus
    realized net), so it captures fees plus slippage on BOTH legs. It is split by
    exit reason because hard stops fill in fast markets and slip several times
    more than trail exits — a blended number describes neither.
    """
    figs = {}
    fr = defaultdict(list)
    for t in trades:
        v = t.get("friction_pct")
        if v is not None:
            fr[(t.get("exit_reason") or "UNKNOWN")].append(float(v))
    allfr = [v for vs in fr.values() for v in vs]

    if allfr:
        figs["friction_all"] = _fig(
            "friction_all", "Realized round-trip friction (all exits)",
            round(st.mean(allfr), 3), "%", len(allfr), days,
            "mean of intended-return minus realized-net per trade: fees + entry "
            "slippage + exit slippage",
            detail={"median": round(st.median(allfr), 3)})
        for reason, vs in sorted(fr.items()):
            if len(vs) >= 3:
                figs["friction_" + reason.lower()] = _fig(
                    "friction_" + reason.lower(),
                    "Friction — {} exits".format(reason), round(st.mean(vs), 3), "%",
                    len(vs), days, "mean plan-vs-actual cost on {} exits".format(reason),
                    detail={"median": round(st.median(vs), 3)})

    # Fill rate: signals the bot saw versus signals it could act on. A low fill
    # rate means the slot cap, not the signal, is deciding what gets traded.
    try:
        n_miss = db._conn.execute("SELECT COUNT(*) FROM misses").fetchone()[0]
        breakdown = {r: c for r, c in db._conn.execute(
            "SELECT reason, COUNT(*) FROM misses GROUP BY reason").fetchall()}
    except Exception:                                        # noqa: BLE001
        n_miss, breakdown = 0, {}
    entries = len(trades) + len(db.open_positions())
    seen = entries + n_miss
    if seen:
        figs["fill_rate"] = _fig(
            "fill_rate", "Signal fill rate", round(entries / seen * 100, 2), "%",
            seen, days,
            "{} positions opened out of {} signals seen; {} missed".format(
                entries, seen, n_miss),
            detail={"signals_seen": seen, "entries": entries, "misses": n_miss,
                    "breakdown": breakdown})
        if n_miss and n_miss / seen > 0.25:
            figs["fill_rate"]["warnings"].append(
                "Over a quarter of signals were not taken. When the slot cap binds, "
                "which signals get traded becomes arbitrary rather than selective.")
    return {"label": "Execution", "figures": figs}


# ── section 4: per coin ──────────────────────────────────────────
def _per_coin_section(trades, inception):
    """Per-coin attribution, shipped with its own refutation.

    Testing on the live record gives a split-half correlation of r=0.177 and a
    label-shuffle p=0.086 for the worst coin — per-coin P&L does not persist.
    The table is included because people always ask for it; the caveat is
    attached because acting on it would be fitting noise.
    """
    agg = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0, "hard": 0})
    for t in trades:
        p = trade_pnl(t)
        if p is None:
            continue
        d = agg[(t.get("coin") or "?").upper()]
        d["n"] += 1
        d["pnl"] += p
        d["wins"] += p > 0
        d["hard"] += (t.get("exit_reason") or "") == "HARD_STOP"
    rows = []
    for coin, d in sorted(agg.items(), key=lambda kv: -kv[1]["pnl"]):
        rows.append({"coin": coin, "trips": d["n"],
                     "win_rate_pct": round(d["wins"] / d["n"] * 100, 1),
                     "hard_stops": d["hard"],
                     "net_pnl": round(d["pnl"], 2),
                     "avg_pnl": round(d["pnl"] / d["n"], 3),
                     "contribution_pp": round(d["pnl"] / inception * 100, 2)
                     if inception else None})
    return {"label": "Per coin", "rows": rows, "publishable": False,
            "caveat": DO_NOT_PUBLISH["per_coin_ranking"]}


# ── section 5: shadow trail A/B ──────────────────────────────────
def _shadow_section(db, days):
    """Live trail versus the counterfactual tighter trails.

    The comparison is only fair if the hypothetical exits are charged the SAME
    friction the real ones paid. config.MEASURED_FRICTION_PCT is a placeholder
    (0.5%) roughly 3x the measured trail-exit cost, so this section reports the
    measured value alongside and flags the gap when it is material.
    """
    try:
        summary = db.shadow_summary()
        fric_avg, fric_n = db.measured_friction()
    except Exception:                                        # noqa: BLE001
        return {"label": "Trail A/B", "rows": [], "note": "shadow data unavailable"}

    charged = config.MEASURED_FRICTION_PCT
    rows = [{"trail_pct": tp, "n": s["n"],
             "median_net_pct": round(s["median_net_pct"], 4)
             if s.get("median_net_pct") is not None else None,
             "win_rate": s.get("win_rate")}
            for tp, s in sorted(summary.items())]
    out = {"label": "Trail A/B", "rows": rows,
           "live_trail_pct": config.TRAIL_PCT,
           "friction_charged_pct": charged,
           "friction_measured_pct": fric_avg, "friction_n": fric_n,
           "decision_ready": all(r["n"] >= 50 for r in rows) if rows else False,
           "note": "Decide the trail only after ~50 matched trades per arm."}
    if fric_avg is not None and fric_n >= 5 and abs(charged - fric_avg) > 0.05:
        out["warning"] = (
            "Shadow exits are charged {:.2f}% friction but the measured cost is "
            "{:.3f}% over {} trades. The A/B is biased against tighter trails "
            "until MEASURED_FRICTION_PCT is corrected.".format(charged, fric_avg, fric_n))
    return out


# ── reconciliation ───────────────────────────────────────────────
def _reconciliation(db, trades, is_lifetime, all_trades):
    """Prove the report adds up.

    Two independent paths to the same number: this module's per-trade sum, and
    db.realized_pnl(). They can only be asserted equal over the LIFETIME window
    — a 30-day report sums a subset, so comparing it to the lifetime ledger
    would always look like a mismatch. For a windowed report the lifetime total
    is still shown, but as context rather than as the pass/fail test.
    """
    mine = sum(p for p in (trade_pnl(t) for t in trades) if p is not None)
    lifetime_mine = sum(p for p in (trade_pnl(t) for t in all_trades) if p is not None)
    try:
        ledger = db.realized_pnl()
    except Exception:                                        # noqa: BLE001
        ledger = None
    unpriced = sum(1 for t in trades if trade_pnl(t) is None)
    delta = None if ledger is None else round(lifetime_mine - ledger, 4)
    ok = (delta is not None and abs(delta) < 0.01 and unpriced == 0)
    out = {"scope": "lifetime" if is_lifetime else "windowed",
           "window_realized_pnl": round(mine, 4),
           "lifetime_recomputed_pnl": round(lifetime_mine, 4),
           "db_realized_pnl": ledger,
           "delta": delta,
           "trades_in_window": len(trades),
           "trades_lifetime": len(all_trades),
           "trades_unpriced": unpriced,
           "ok": ok,
           "note": "The pass/fail test recomputes LIFETIME P&L from the trade rows "
                   "and compares it to the durable ledger total; a non-zero delta "
                   "means rows are missing entry/exit/qty. The window figure is the "
                   "subset this report covers."}
    if not is_lifetime:
        out["note"] += (" This is a windowed report, so window_realized_pnl is "
                        "expected to differ from the lifetime total.")
    return out


# ── benchmarks ───────────────────────────────────────────────────
def _series_returns(prices, dates):
    """Daily simple returns for one coin over `dates`, skipping gaps."""
    out, prev = [], None
    for d in dates:
        px = prices.get(d)
        if px is None or px <= 0:
            continue
        if prev is not None:
            out.append(px / prev - 1.0)
        prev = px
    return out


def _compound(rets):
    eq = 1.0
    for r in rets:
        eq *= (1.0 + r)
    return (eq - 1.0) * 100


def _dd_of(rets):
    eq, peak, worst = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        worst = min(worst, (eq - peak) / peak * 100)
    return worst


def _benchmarks(db, trades, start, end, strat_return_pct, strat_dd_pct,
                strat_mean_net_pct=None):
    """What the same window would have paid without the signal.

    Everything here is priced off the persisted daily_prices table, so the report
    stays offline. Daily closes cannot reproduce intraday stop fills, so these
    rows answer 'was the TIMING worth anything' — not 'what would the bot have
    done'. That distinction is stated in the payload, not buried here.

    B1  Equal-weight buy-and-hold across the coins actually traded. Difference
        vs the strategy isolates whether entering and exiting beat sitting still.
    B2  Randomized-entry timing null: the same coins, the same NUMBER of trades
        and the same holding durations, but entry dates drawn at random from the
        window. Difference isolates TIMING — the core claim of a signal bot.
    B3  BTC buy-and-hold, as market context only.
    """
    coins = sorted({(t.get("coin") or "").upper() for t in trades if t.get("coin")})
    s_date, e_date = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    px = db.price_series(coins + ["BTC"], s_date, e_date)
    cov = db.price_coverage()

    usable = [c for c in coins if len(px.get(c, {})) >= 10]
    if not usable:
        return {"status": INSUFFICIENT_DATA, "coverage": cov,
                "note": "No daily price history yet for the traded coins. The bot "
                        "persists a daily close for every coin it screens, so this "
                        "section fills in on its own — the first pass after deploy "
                        "backfills roughly 33 days from the candles already pulled "
                        "for RSI. Benchmarks appear once at least 10 days exist."}

    all_dates = sorted({d for c in usable for d in px[c]})
    rows = []

    # B1 — equal-weight buy and hold
    per_day = []
    for i in range(1, len(all_dates)):
        d0, d1 = all_dates[i - 1], all_dates[i]
        legs = []
        for c in usable:
            a, b = px[c].get(d0), px[c].get(d1)
            if a and b and a > 0:
                legs.append(b / a - 1.0)
        if legs:
            per_day.append(sum(legs) / len(legs))
    rows.append({"key": "B1", "label": "Equal-weight buy & hold, same coins",
                 "return_pct": round(_compound(per_day), 2),
                 "max_dd_pct": round(_dd_of(per_day), 2),
                 "isolates": "Whether trading at all beat sitting still in the same coins."})

    # B2 — randomized-entry timing null, matched on trade count and duration
    holds, counts = defaultdict(list), Counter()
    for t in trades:
        c = (t.get("coin") or "").upper()
        o, x = _parse(t.get("opened_at")), _parse(t.get("closed_at"))
        if c in px and o and x:
            counts[c] += 1
            holds[c].append(max(1, (x - o).days))
    rnd = random.Random(20260901)          # fixed seed: a report must reproduce
    sims = []
    for _ in range(200):
        legs = []
        for c in usable:
            n = counts.get(c, 0)
            if not n:
                continue
            dates = sorted(px[c])
            for _ in range(n):
                hold = rnd.choice(holds[c]) if holds[c] else 1
                if len(dates) - hold - 1 <= 0:
                    continue
                i = rnd.randrange(0, len(dates) - hold - 1)
                a, b = px[c][dates[i]], px[c][dates[min(i + hold, len(dates) - 1)]]
                if a and a > 0:
                    legs.append((b / a - 1.0) * 100)
        if legs:
            sims.append(st.mean(legs))
    if sims:
        sims.sort()
        rows.append({"key": "B2", "label": "Randomized entry timing, matched trades",
                     "return_pct": round(st.mean(sims), 3),
                     "max_dd_pct": None,
                     "detail": {"runs": len(sims), "unit": "mean % per trade",
                                "p05": round(sims[int(len(sims) * .05)], 3),
                                "p95": round(sims[int(len(sims) * .95)], 3)},
                     "isolates": "TIMING. Same coins, same trade count, same hold "
                                 "durations, entry dates drawn at random."})

    # B3 — BTC context
    btc = px.get("BTC", {})
    if len(btc) >= 10:
        br = _series_returns(btc, sorted(btc))
        rows.append({"key": "B3", "label": "BTC buy & hold",
                     "return_pct": round(_compound(br), 2),
                     "max_dd_pct": round(_dd_of(br), 2),
                     "isolates": "Market context only — a different risk profile, "
                                 "not a like-for-like claim."})

    # Adjudicate B2 rather than leaving the reader to eyeball it: if the strategy's
    # mean return per trade falls inside the randomized-timing band, the entry
    # timing is not distinguishable from chance over this window.
    verdict = None
    b2 = next((r for r in rows if r["key"] == "B2"), None)
    if b2 and strat_mean_net_pct is not None:
        lo, hi = b2["detail"]["p05"], b2["detail"]["p95"]
        inside = lo <= strat_mean_net_pct <= hi
        verdict = {
            "strategy_mean_net_pct_per_trade": round(strat_mean_net_pct, 3),
            "random_band_p05_p95": [lo, hi],
            "random_mean": b2["return_pct"],
            "timing_beats_chance": not inside and strat_mean_net_pct > hi,
            "statement": (
                "The strategy averaged {:+.3f}% net per trade. Random entry timing "
                "over the same coins, trade count and hold durations produced "
                "{:+.3f}% on average, with a 5th-95th percentile band of {:+.3f}% "
                "to {:+.3f}%. The strategy's result falls {} that band, so over "
                "this window the entry timing {}."
            ).format(strat_mean_net_pct, b2["return_pct"], lo, hi,
                     "INSIDE" if inside else ("ABOVE" if strat_mean_net_pct > hi else "BELOW"),
                     "is not distinguishable from chance" if inside else
                     ("beat chance" if strat_mean_net_pct > hi else "underperformed chance")),
        }

    return {"status": OK, "coverage": cov, "coins_priced": len(usable),
            "timing_verdict": verdict,
            "window": {"start": s_date, "end": e_date},
            "strategy": {"return_pct": strat_return_pct, "max_dd_pct": strat_dd_pct},
            "rows": rows,
            "methodology": "Priced from the persisted daily_prices table so the "
                           "report performs no network calls. Daily closes cannot "
                           "reproduce intraday stop fills, so these rows test "
                           "whether the timing added value — they do not simulate "
                           "the bot.",
            "material_notes": [
                "B1 and B3 are unlevered buy-and-hold; the strategy runs 2x "
                "leverage with a 10% stop, so risk profiles differ and the return "
                "columns are not directly comparable without that in mind.",
                "B2 is the load-bearing comparison: if the strategy's mean return "
                "per trade sits inside B2's p05-p95 band, the entry timing is not "
                "distinguishable from chance over this window.",
            ]}


# ── periods ──────────────────────────────────────────────────────
PERIODS = {"lifetime": None, "ytd": "ytd", "90d": 90, "30d": 30, "7d": 7}


def _window(period, trades):
    """Resolve a period name to (start, end, label). Anchored to the DATA, never
    to wall clock alone — a stale DB must not silently report an empty window."""
    closes = [d for d in (_parse(t.get("closed_at")) for t in trades) if d]
    if not closes:
        now = datetime.now(timezone.utc)
        return now, now, period
    lo, hi = min(closes), max(closes)
    spec = PERIODS.get(period, None)
    if spec is None:
        return lo, hi, "Lifetime"
    if spec == "ytd":
        return max(lo, datetime(hi.year, 1, 1, tzinfo=timezone.utc)), hi, "Year to date"
    return max(lo, hi - timedelta(days=spec)), hi, "Last {} days".format(spec)


def build(db, period="lifetime", status=None):
    """Assemble the full report structure. Pure: reads SQLite, no network."""
    all_trades = db.recent_trades(1000000)
    start, end, plabel = _window(period, all_trades)
    trades = [t for t in all_trades
              if (_parse(t.get("closed_at")) or end) >= start]
    days = max(1, (end - start).days)

    acct = db.account() or {}
    inception = acct.get("inception") or 0.0
    st_json = status or {}

    sec_account = _account_section(trades, inception, days, st_json.get("total_upnl"))
    sec_tail = _tail_section(trades, days)
    sec_exec = _execution_section(db, trades, days)
    sec_coin = _per_coin_section(trades, inception)
    sec_shadow = _shadow_section(db, days)
    _roi = sec_account["figures"].get("return_on_inception")
    _dd = sec_account["figures"].get("max_drawdown")
    try:
        _nets = [t.get("net_pct") for t in trades if t.get("net_pct") is not None]
        bench = _benchmarks(db, trades, start, end,
                            _roi["value"] if _roi else None,
                            _dd["value"] if _dd else None,
                            st.mean(_nets) if _nets else None)
    except Exception as e:                                   # noqa: BLE001
        bench = {"status": INSUFFICIENT_DATA,
                 "note": "benchmark computation failed: {}".format(e)}

    # Ledger: every figure classified, nothing silently dropped.
    figures = {}
    for s in (sec_account, sec_tail, sec_exec):
        figures.update(s.get("figures", {}))
    ledger = {"generated_figures": len(figures),
              "publishable": sorted(k for k, f in figures.items()
                                    if f["publishable"] and f["status"] == OK),
              "annualized_short_window": sorted(
                  k for k, f in figures.items() if f["status"] == ANNUALIZED_SHORT_WINDOW),
              "not_publishable": sorted(k for k, f in figures.items() if not f["publishable"]),
              "insufficient_data": sorted(k for k, f in figures.items()
                                          if f["status"] == INSUFFICIENT_DATA)}

    facts = {"as_of": end.isoformat(), "period": plabel, "days": days,
             "closed_trades": len(trades)}
    for k in ("realized_pnl", "return_on_inception", "cagr", "max_drawdown",
              "drawdown_from_top", "monthly_max_drawdown", "sharpe", "sortino",
              "calmar", "volatility", "win_rate", "profit_factor",
              "tail_concentration", "hard_stop_rate", "expectancy",
              "win_loss_ratio", "avg_hold_hours", "best_month", "worst_month"):
        if k in figures:
            facts[k] = figures[k]["value"]

    return {
        "provenance": {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "period": period, "period_label": plabel,
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "days": days,
            "source": "SQLite trades ledger (no network at report time)",
            "returns_basis": "Realized P&L rebuilt per trade from the ledger. "
                             "Libration has no capital-flows table, so equity deltas "
                             "would book a deposit as profit; the ledger cannot.",
            "strategy": config.summary(),
            "trades_in_scope": len(trades),
            "trades_lifetime": len(all_trades),
        },
        "account": sec_account,
        "tail": sec_tail,
        "execution": sec_exec,
        "per_coin": sec_coin,
        "shadow": sec_shadow,
        "benchmarks": bench,
        "ledger": ledger,
        "reconciliation": _reconciliation(db, trades, period == "lifetime", all_trades),
        "facts": facts,
        "do_not_publish": DO_NOT_PUBLISH,
        "context_required": CONTEXT_REQUIRED,
        "disclosures": [
            "Past performance is not necessarily indicative of future results.",
            "Figures are realized round-trip results from the bot's own ledger; open "
            "positions are excluded from realized P&L and shown separately.",
            "Figures marked ANNUALIZED_SHORT_WINDOW are projected from less than one "
            "year of data and are not achieved annual returns.",
            "This is a leveraged perpetual-futures strategy. A 10% hard stop at 20% "
            "notional is roughly a 2% equity loss per stopped position, and stops "
            "cluster in time.",
        ],
    }


# ── inline SVG charts (no matplotlib in the venv; must print to PDF) ──
def _svg_line(points, w=760, h=220, pad=34, label=""):
    if len(points) < 2:
        return '<p class="muted">Not enough data to chart.</p>'
    ys = [p[1] for p in points]
    lo, hi = min(ys), max(ys)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    n = len(points)

    def X(i):
        return pad + i * (w - 2 * pad) / (n - 1)

    def Y(v):
        return h - pad - (v - lo) * (h - 2 * pad) / (hi - lo)

    line = " ".join("{:.1f},{:.1f}".format(X(i), Y(v)) for i, (_, v) in enumerate(points))
    area = "{:.1f},{:.1f} ".format(pad, h - pad) + line + " {:.1f},{:.1f}".format(w - pad, h - pad)
    base = Y(points[0][1])
    return (
        '<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{lab}">'
        '<polyline points="{area}" fill="rgba(56,132,255,.12)" stroke="none"/>'
        '<line x1="{pad}" y1="{base:.1f}" x2="{w2}" y2="{base:.1f}" '
        'stroke="currentColor" stroke-dasharray="3 3" opacity=".35"/>'
        '<polyline points="{line}" fill="none" stroke="#3884ff" stroke-width="2"/>'
        '<text x="{pad}" y="16" font-size="11" fill="currentColor" opacity=".7">'
        '{hi:,.2f}</text>'
        '<text x="{pad}" y="{hy}" font-size="11" fill="currentColor" opacity=".7">'
        '{lo:,.2f}</text></svg>'
    ).format(w=w, h=h, pad=pad, w2=w - pad, area=area, line=line, base=base,
             hi=hi, lo=lo, hy=h - pad + 14, lab=label or "chart")


def _svg_bars(pairs, w=760, h=220, pad=34, label=""):
    if not pairs:
        return '<p class="muted">No data.</p>'
    vals = [v for _, v in pairs]
    lo, hi = min(0.0, min(vals)), max(0.0, max(vals))
    if hi - lo < 1e-9:
        hi = lo + 1.0
    n = len(pairs)
    bw = (w - 2 * pad) / n * 0.72

    def Y(v):
        return h - pad - (v - lo) * (h - 2 * pad) / (hi - lo)

    zero = Y(0.0)
    out = []
    for i, (name, v) in enumerate(pairs):
        x = pad + i * (w - 2 * pad) / n + ((w - 2 * pad) / n - bw) / 2
        y, hgt = (Y(v), zero - Y(v)) if v >= 0 else (zero, Y(v) - zero)
        col = "#2fa36b" if v >= 0 else "#d0483f"
        out.append('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" '
                   'fill="{}" rx="2"/>'.format(x, y, bw, max(abs(hgt), 0.6), col))
        out.append('<text x="{:.1f}" y="{}" font-size="9" text-anchor="middle" '
                   'fill="currentColor" opacity=".75">{}</text>'.format(
                       x + bw / 2, h - pad + 12, name[:8]))
    out.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="currentColor" '
               'opacity=".35"/>'.format(pad, zero, w - pad, zero))
    return ('<svg viewBox="0 0 {} {}" class="chart" role="img" aria-label="{}">{}'
            '</svg>').format(w, h, label or "chart", "".join(out))


# ── renderers ────────────────────────────────────────────────────
def _money(v):
    """-$6.10, never $-6.10."""
    return ("-$" if v < 0 else "$") + "{:,.2f}".format(abs(v))


def _fmt(f):
    v = f["value"]
    u = f["unit"]
    if u == "USD" and isinstance(v, (int, float)):
        return _money(v)
    if isinstance(v, float):
        s = "{:,.2f}".format(v) if abs(v) < 1e6 else "{:,.0f}".format(v)
    else:
        s = str(v)
    if u == "%":
        return s + "%"
    return s + ((" " + u) if u else "")


def _md(text):
    """Escape pipes so a basis or caveat string cannot break a markdown table row."""
    return str(text).replace("|", r"\|")


def _fig_rows(figs, keys=None):
    return [figs[k] for k in (keys or figs) if k in figs]


def render_markdown(r):
    """Operator report. Every figure carries its basis; every caveat is inline."""
    p, out = r["provenance"], []
    A = out.append
    A("# Libration — Financial Report\n")
    A("*Generated {} · {} · {} to {} ({} days)*\n".format(
        p["generated_utc"][:19], p["period_label"], p["window_start"][:10],
        p["window_end"][:10], p["days"]))

    cap = r["account"]["capital"]
    A("\n## Account\n")
    A("- Starting capital: **${:,.2f}**".format(cap["inception"]))
    A("- Realized P&L: **${:,.2f}**".format(cap["realized_pnl"]))
    A("- Unrealized (open positions): ${:,.2f}".format(cap["unrealized_pnl"]))
    A("- Equity, realized basis: **${:,.2f}**  ·  including open marks: ${:,.2f}\n".format(
        cap["end_equity_realized"], cap["end_equity_incl_open"]))

    def table(figs):
        if not figs:
            return
        A("\n| Figure | Value | n | Basis |")
        A("|---|---:|---:|---|")
        for f in figs:
            flag = " ⚠️ *annualized*" if f["status"] == ANNUALIZED_SHORT_WINDOW else ""
            nop = " 🚫 *not for publication*" if not f["publishable"] else ""
            A("| {} | **{}**{}{} | {} | {} |".format(
                _md(f["label"]), _fmt(f), flag, nop, f["n"] or "—", _md(f["basis"])))

    table(_fig_rows(r["account"]["figures"]))

    A("\n## Tail & exits\n")
    A("*The defining risk section for this strategy. Net profit is whatever "
      "survives the hard-stop tail — win rate does not describe it.*\n")
    table(_fig_rows(r["tail"]["figures"]))
    if r["tail"]["by_reason"]:
        A("\n| Exit reason | Trades | Share | Total P&L | Avg P&L |")
        A("|---|---:|---:|---:|---:|")
        for k, v in r["tail"]["by_reason"].items():
            A("| {} | {} | {}% | ${:,.2f} | ${:,.2f} |".format(
                k, v["n"], v["share_of_trades_pct"], v["total_pnl"], v["avg_pnl"]))

    A("\n## Execution\n")
    table(_fig_rows(r["execution"]["figures"]))

    A("\n## Per coin\n")
    A("> ⚠️ **{}**\n".format(r["per_coin"]["caveat"]))
    if r["per_coin"]["rows"]:
        A("| Coin | Trips | Win% | Hard stops | Net P&L | Avg/trip | Contribution |")
        A("|---|---:|---:|---:|---:|---:|---:|")
        for x in r["per_coin"]["rows"]:
            A("| {} | {} | {}% | {} | ${:,.2f} | ${:,.3f} | {} |".format(
                x["coin"], x["trips"], x["win_rate_pct"], x["hard_stops"],
                x["net_pnl"], x["avg_pnl"],
                "{:+.2f}pp".format(x["contribution_pp"])
                if x["contribution_pp"] is not None else "—"))

    sh = r["shadow"]
    A("\n## Trail A/B\n")
    if sh.get("warning"):
        A("> ⚠️ **{}**\n".format(sh["warning"]))
    if sh.get("rows"):
        A("| Trail | n | Median net%/trade | Win rate |")
        A("|---|---:|---:|---:|")
        A("| **{}% (live)** | — | — | — |".format(sh.get("live_trail_pct")))
        for x in sh["rows"]:
            A("| {}% (shadow) | {} | {} | {} |".format(
                x["trail_pct"], x["n"],
                x["median_net_pct"] if x["median_net_pct"] is not None else "—",
                x["win_rate"] if x["win_rate"] is not None else "—"))
        A("\n*{}*".format(sh.get("note", "")))

    cal = r["account"].get("calendar") or {}
    if cal.get("months"):
        A("\n## Calendar returns\n")
        A("| Month | Return | Max drawdown in month | Days |")
        A("|---|---:|---:|---:|")
        for m in cal["months"]:
            A("| {} | {:+.2f}% | {:.2f}% | {} |".format(
                m["month"], m["return_pct"], m["max_drawdown_pct"], m["days"]))
        A("\n*{}*".format(cal.get("note", "")))

    b = r["benchmarks"]
    A("\n## Benchmarks\n")
    if b.get("status") != OK:
        A("*{}*".format(b.get("note", "")))
    else:
        A("*{}*\n".format(b.get("methodology", "")))
        A("| Row | Return | Max DD | Isolates |")
        A("|---|---:|---:|---|")
        stg = b.get("strategy") or {}
        A("| **Strategy (live signals)** | **{}** | **{}** | actual realized results |".format(
            "{:+.2f}%".format(stg["return_pct"]) if stg.get("return_pct") is not None else "-",
            "{:.2f}%".format(stg["max_dd_pct"]) if stg.get("max_dd_pct") is not None else "-"))
        for row in b.get("rows", []):
            rp = row.get("return_pct")
            unit = (row.get("detail") or {}).get("unit")
            if rp is None:
                val = "-"
            elif unit:
                val = "{:+.3f}% /trade".format(rp)
            else:
                val = "{:+.2f}%".format(rp)
            A("| {} - {} | {} | {} | {} |".format(
                row["key"], _md(row["label"]), val,
                "{:.2f}%".format(row["max_dd_pct"]) if row.get("max_dd_pct") is not None else "-",
                _md(row.get("isolates", ""))))
        v = b.get("timing_verdict")
        if v:
            A("\n> **Timing test.** {}\n".format(_md(v["statement"])))
        for n in b.get("material_notes", []):
            A("- {}".format(_md(n)))
        cov = b.get("coverage") or {}
        A("\n*Priced from {} daily closes across {} coins, {} to {}.*".format(
            cov.get("rows"), cov.get("coins"), cov.get("first"), cov.get("last")))

    rec = r["reconciliation"]
    A("\n## Reconciliation\n")
    A("- Scope: **{}** · this window ${:,.2f} over {} trades".format(
        rec["scope"], rec["window_realized_pnl"], rec["trades_in_window"]))
    A("- Lifetime recomputed ${:,.4f} vs ledger {} · delta {}".format(
        rec["lifetime_recomputed_pnl"],
        "${:,.4f}".format(rec["db_realized_pnl"]) if rec["db_realized_pnl"] is not None else "—",
        rec["delta"]))
    A("- Unpriced rows {} · **{}**".format(
        rec["trades_unpriced"], "OK" if rec["ok"] else "MISMATCH"))

    A("\n## Figure ledger\n")
    L = r["ledger"]
    A("- Publishable: {}".format(", ".join(L["publishable"]) or "—"))
    A("- Annualized from a short window: {}".format(
        ", ".join(L["annualized_short_window"]) or "—"))
    A("- Not for publication: {}".format(", ".join(L["not_publishable"]) or "—"))

    A("\n## Do not publish\n")
    for k, why in r["do_not_publish"].items():
        A("- **{}** — {}".format(k, why))

    A("\n## Disclosures\n")
    for d in r["disclosures"]:
        A("- {}".format(d))
    return "\n".join(out)


def render_marketing_markdown(r):
    """Marketing pack: publishable figures only, each caveat welded into the
    quotable sentence so the number cannot travel without it."""
    p, out = r["provenance"], []
    A = out.append
    A("# Libration — Approved Figures\n")
    A("*{} · {} to {} ({} days). Copy whole sentences, not bare numbers.*\n".format(
        p["period_label"], p["window_start"][:10], p["window_end"][:10], p["days"]))

    figs = {}
    for s in ("account", "tail", "execution"):
        figs.update(r[s].get("figures", {}))

    approved = [f for f in figs.values() if f["publishable"]]
    if not approved:
        A("\nNo figures currently clear the publication bar.")
    for f in sorted(approved, key=lambda f: f["key"]):
        sentence = "**{}: {}** over {} days ({} observations), measured as {}".format(
            f["label"], _fmt(f), p["days"], f["n"] or "n/a", f["basis"].rstrip("."))
        if f["status"] == ANNUALIZED_SHORT_WINDOW:
            sentence += " — annualized from under a year of data and therefore a projection, not an achieved annual return"
        A("\n- " + sentence + ".")
        for w in f["warnings"]:
            A("  - *{}*".format(w))

    A("\n## Withheld from publication\n")
    for k, why in r["do_not_publish"].items():
        A("- **{}** — {}".format(k, why))
    A("\n## Disclosures\n")
    for d in r["disclosures"]:
        A("- {}".format(d))
    return "\n".join(out)


_CSS = """
:root{--bg:#fff;--fg:#15181d;--muted:#5b6472;--line:#e3e7ee;--accent:#3884ff;
--good:#2fa36b;--bad:#d0483f;--warn:#8a6100;--warnbg:#fff7e0}
@media(prefers-color-scheme:dark){:root{--bg:#0f1216;--fg:#e6e9ef;--muted:#98a2b3;
--line:#252b34;--warnbg:#2a2412;--warn:#e0b155}}
*{box-sizing:border-box}
body{margin:0;padding:32px;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px} h2{font-size:19px;margin:34px 0 10px;
padding-bottom:6px;border-bottom:1px solid var(--line)}
.muted{color:var(--muted);font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}
.kpi{border:1px solid var(--line);border-radius:9px;padding:11px 13px}
.kpi .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.kpi .v{font-size:21px;font-weight:640;margin-top:3px}
.pos{color:var(--good)} .neg{color:var(--bad)}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:13.5px;display:block;overflow-x:auto}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
td.n,th.n{text-align:right} td.basis{white-space:normal;color:var(--muted);font-size:12px}
.note{background:var(--warnbg);border-left:3px solid var(--warn);padding:10px 13px;
border-radius:0 6px 6px 0;margin:12px 0;font-size:13px}
.chart{width:100%;height:auto;margin:10px 0;color:var(--fg)}
.tag{font-size:10px;padding:1px 6px;border-radius:9px;border:1px solid var(--line);
color:var(--muted);margin-left:5px;white-space:nowrap}
ul{padding-left:19px} li{margin:5px 0}
@media print{body{padding:0} .chart{page-break-inside:avoid} h2{page-break-after:avoid}}
"""


def render_html(r, marketing=False):
    p = r["provenance"]
    H = ['<!doctype html><html><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         "<title>Libration — {}</title><style>{}</style></head><body><div class='wrap'>".format(
             "Approved Figures" if marketing else "Financial Report", _CSS)]
    A = H.append
    A("<h1>Libration — {}</h1>".format("Approved Figures" if marketing else "Financial Report"))
    A('<p class="muted">Generated {} · {} · {} → {} ({} days) · {} closed trades</p>'.format(
        p["generated_utc"][:19], p["period_label"], p["window_start"][:10],
        p["window_end"][:10], p["days"], p["trades_in_scope"]))

    f = r["facts"]

    def kpi(label, val, cls=""):
        return '<div class="kpi"><div class="l">{}</div><div class="v {}">{}</div></div>'.format(
            label, cls, val)

    cap = r["account"]["capital"]
    k = [kpi("Realized P&L", "${:,.2f}".format(cap["realized_pnl"]),
             "pos" if cap["realized_pnl"] >= 0 else "neg")]
    if "return_on_inception" in r["account"]["figures"]:
        v = r["account"]["figures"]["return_on_inception"]["value"]
        k.append(kpi("Return on capital", "{:+.2f}%".format(v), "pos" if v >= 0 else "neg"))
    if "max_drawdown" in r["account"]["figures"]:
        k.append(kpi("Max drawdown", "{:.2f}%".format(
            r["account"]["figures"]["max_drawdown"]["value"]), "neg"))
    if "tail_concentration" in r["tail"]["figures"]:
        k.append(kpi("Tail concentration", "{:.1f}%".format(
            r["tail"]["figures"]["tail_concentration"]["value"])))
    k.append(kpi("Closed trades", "{:,}".format(f.get("closed_trades", 0))))
    A('<div class="kpis">' + "".join(k) + "</div>")

    curve = [(x["d"], x["v"]) for x in r["account"].get("curve", [])]
    if len(curve) > 1:
        A("<h2>Realized equity</h2>")
        A(_svg_line(curve, label="realized equity curve"))
        A('<p class="muted">Rebuilt from the trade ledger, so deposits and '
          "withdrawals cannot distort it.</p>")

    def ftable(figs, show_unpub=True):
        rows = [x for x in figs if show_unpub or x["publishable"]]
        if not rows:
            return
        A('<table><tr><th>Figure</th><th class="n">Value</th><th class="n">n</th>'
          "<th>Basis</th></tr>")
        for x in rows:
            tags = ""
            if x["status"] == ANNUALIZED_SHORT_WINDOW:
                tags += '<span class="tag">annualized</span>'
            if not x["publishable"]:
                tags += '<span class="tag">not for publication</span>'
            A('<tr><td>{}{}</td><td class="n"><b>{}</b></td><td class="n">{}</td>'
              '<td class="basis">{}</td></tr>'.format(
                  x["label"], tags, _fmt(x), x["n"] or "—", x["basis"]))
        A("</table>")
        for x in rows:
            for w in x["warnings"]:
                A('<div class="note"><b>{}</b> — {}</div>'.format(x["label"], w))

    if not marketing:
        A("<h2>Account</h2>")
        ftable(_fig_rows(r["account"]["figures"]))

        A("<h2>Tail &amp; exits</h2>")
        A('<p class="muted">The defining risk section for this strategy: net profit '
          "is whatever survives the hard-stop tail.</p>")
        ftable(_fig_rows(r["tail"]["figures"]))
        br = r["tail"]["by_reason"]
        if br:
            A('<table><tr><th>Exit reason</th><th class="n">Trades</th><th class="n">Share</th>'
              '<th class="n">Total P&amp;L</th><th class="n">Avg P&amp;L</th></tr>')
            for kk, v in br.items():
                A('<tr><td>{}</td><td class="n">{}</td><td class="n">{}%</td>'
                  '<td class="n {}">${:,.2f}</td><td class="n">${:,.2f}</td></tr>'.format(
                      kk, v["n"], v["share_of_trades_pct"],
                      "pos" if v["total_pnl"] >= 0 else "neg", v["total_pnl"], v["avg_pnl"]))
            A("</table>")

        A("<h2>Execution</h2>")
        ftable(_fig_rows(r["execution"]["figures"]))

        A("<h2>Per coin</h2>")
        A('<div class="note">{}</div>'.format(r["per_coin"]["caveat"]))
        rows = r["per_coin"]["rows"]
        if rows:
            A(_svg_bars([(x["coin"], x["net_pnl"]) for x in rows], label="net P&L by coin"))
            A('<table><tr><th>Coin</th><th class="n">Trips</th><th class="n">Win%</th>'
              '<th class="n">Hard stops</th><th class="n">Net P&amp;L</th>'
              '<th class="n">Avg/trip</th><th class="n">Contribution</th></tr>')
            for x in rows:
                A('<tr><td>{}</td><td class="n">{}</td><td class="n">{}%</td>'
                  '<td class="n">{}</td><td class="n {}">${:,.2f}</td>'
                  '<td class="n">${:,.3f}</td><td class="n">{}</td></tr>'.format(
                      x["coin"], x["trips"], x["win_rate_pct"], x["hard_stops"],
                      "pos" if x["net_pnl"] >= 0 else "neg", x["net_pnl"], x["avg_pnl"],
                      "{:+.2f}pp".format(x["contribution_pp"])
                      if x["contribution_pp"] is not None else "—"))
            A("</table>")

        sh = r["shadow"]
        A("<h2>Trail A/B</h2>")
        if sh.get("warning"):
            A('<div class="note">{}</div>'.format(sh["warning"]))
        if sh.get("rows"):
            A('<table><tr><th>Trail</th><th class="n">n</th>'
              '<th class="n">Median net%/trade</th><th class="n">Win rate</th></tr>')
            for x in sh["rows"]:
                A('<tr><td>{}% (shadow)</td><td class="n">{}</td><td class="n">{}</td>'
                  '<td class="n">{}</td></tr>'.format(
                      x["trail_pct"], x["n"],
                      x["median_net_pct"] if x["median_net_pct"] is not None else "—",
                      x["win_rate"] if x["win_rate"] is not None else "—"))
            A("</table>")
            A('<p class="muted">{}</p>'.format(sh.get("note", "")))

        cal = r["account"].get("calendar") or {}
        if cal.get("months"):
            A("<h2>Calendar returns</h2>")
            A(_svg_bars([(m["month"][5:], m["return_pct"]) for m in cal["months"]],
                        label="monthly returns"))
            A('<table><tr><th>Month</th><th class="n">Return</th>'
              '<th class="n">Max DD in month</th><th class="n">Days</th></tr>')
            for m in cal["months"]:
                A('<tr><td>{}</td><td class="n {}">{:+.2f}%</td>'
                  '<td class="n neg">{:.2f}%</td><td class="n">{}</td></tr>'.format(
                      m["month"], "pos" if m["return_pct"] >= 0 else "neg",
                      m["return_pct"], m["max_drawdown_pct"], m["days"]))
            A("</table>")
            A('<p class="muted">{}</p>'.format(cal.get("note", "")))

        b = r["benchmarks"]
        A("<h2>Benchmarks</h2>")
        if b.get("status") != OK:
            A('<div class="note">{}</div>'.format(b.get("note", "")))
        else:
            A('<p class="muted">{}</p>'.format(b.get("methodology", "")))
            A('<table><tr><th>Row</th><th class="n">Return</th>'
              '<th class="n">Max DD</th><th>Isolates</th></tr>')
            stg = b.get("strategy") or {}
            A('<tr><td><b>Strategy (live signals)</b></td><td class="n"><b>{}</b></td>'
              '<td class="n">{}</td><td class="basis">actual realized results</td></tr>'.format(
                  "{:+.2f}%".format(stg["return_pct"]) if stg.get("return_pct") is not None else "—",
                  "{:.2f}%".format(stg["max_dd_pct"]) if stg.get("max_dd_pct") is not None else "—"))
            for row in b.get("rows", []):
                rp = row.get("return_pct")
                unit = (row.get("detail") or {}).get("unit")
                if rp is None:
                    val = "—"
                elif unit:
                    val = "{:+.3f}% /trade".format(rp)
                else:
                    val = "{:+.2f}%".format(rp)
                A('<tr><td>{} · {}</td><td class="n">{}</td><td class="n">{}</td>'
                  '<td class="basis">{}</td></tr>'.format(
                      row["key"], row["label"], val,
                      "{:.2f}%".format(row["max_dd_pct"])
                      if row.get("max_dd_pct") is not None else "—",
                      row.get("isolates", "")))
            A("</table>")
            v = b.get("timing_verdict")
            if v:
                A('<div class="note"><b>Timing test.</b> {}</div>'.format(v["statement"]))
            for n in b.get("material_notes", []):
                A('<p class="muted">{}</p>'.format(n))
            cov = b.get("coverage") or {}
            A('<p class="muted">Priced from {} daily closes across {} coins, {} to {}.</p>'
              .format(cov.get("rows"), cov.get("coins"), cov.get("first"), cov.get("last")))

        rec = r["reconciliation"]
        A("<h2>Reconciliation</h2>")
        A('<p>Scope <b>{}</b> · this window ${:,.2f} over {} trades. Lifetime '
          "recomputed ${:,.4f} vs ledger {} · delta {} · {} unpriced — <b>{}</b></p>".format(
              rec["scope"], rec["window_realized_pnl"], rec["trades_in_window"],
              rec["lifetime_recomputed_pnl"],
              "${:,.4f}".format(rec["db_realized_pnl"])
              if rec["db_realized_pnl"] is not None else "—",
              rec["delta"], rec["trades_unpriced"],
              "OK" if rec["ok"] else "MISMATCH"))
    else:
        A("<h2>Approved figures</h2>")
        A('<p class="muted">Each figure is quotable only with its caveat. Copy whole '
          "sentences.</p>")
        allf = {}
        for s in ("account", "tail", "execution"):
            allf.update(r[s].get("figures", {}))
        ftable([x for x in allf.values() if x["publishable"]], show_unpub=False)

    if r.get("context_required"):
        A("<h2>Quote only with context</h2><ul>")
        for kk, why in r["context_required"].items():
            A("<li><b>{}</b> — {}</li>".format(kk, why))
        A("</ul>")
    A("<h2>Do not publish</h2><ul>")
    for kk, why in r["do_not_publish"].items():
        A("<li><b>{}</b> — {}</li>".format(kk, why))
    A("</ul><h2>Disclosures</h2><ul>")
    for d in r["disclosures"]:
        A("<li>{}</li>".format(d))
    A("</ul></div></body></html>")
    return "".join(H)


def bundle_zip(r):
    """The downloadable pack: same shape as the Accumulator bundle."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.json", json.dumps(r, indent=2, default=str))
        z.writestr("report.md", render_markdown(r))
        z.writestr("report.html", render_html(r))
        z.writestr("marketing.md", render_marketing_markdown(r))
        z.writestr("marketing.html", render_html(r, marketing=True))
        z.writestr("facts.json", json.dumps(
            {**r["facts"], "RETIRED_do_not_publish": r["do_not_publish"]},
            indent=2, default=str))
        z.writestr("README.md", (
            "# Libration report bundle\n\n"
            "Generated {} · {} · {} to {} ({} days)\n\n"
            "| File | Use it for |\n|---|---|\n"
            "| `report.md` | Pasting into a chat or document. |\n"
            "| `report.html` | Viewing in a browser. Ctrl+P / Cmd+P then \"Save as PDF\". |\n"
            "| `marketing.md` | Approved figures only, caveats welded in. |\n"
            "| `marketing.html` | Same, for a browser. |\n"
            "| `report.json` | Every figure with its n, window, basis and status. |\n"
            "| `facts.json` | Headline numbers plus the do-not-publish list. |\n\n"
            "## Before publishing anything\n\n"
            "Every figure carries its caveat, and the caveat is part of the quotable "
            "sentence on purpose — copy the whole sentence, not the number alone.\n\n"
            "Reconciliation: **{}**\n".format(
                r["provenance"]["generated_utc"][:19], r["provenance"]["period_label"],
                r["provenance"]["window_start"][:10], r["provenance"]["window_end"][:10],
                r["provenance"]["days"],
                "OK" if r["reconciliation"]["ok"] else "MISMATCH")))
    return buf.getvalue()
