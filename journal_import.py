"""
Soomario Libration — one-shot import of another bot's closed trades
═══════════════════════════════════════════════════════════════════
Kitsune (Foxify) exposes no trade history, so when an account moves from
soomario-prop to Libration its past round trips exist only in soomario-prop's
own journal. Importing them keeps three things honest:

  * the dashboard's stats and equity curve start at the account's real start,
  * account.equity (= inception + realized) matches the venue wallet, so sizing
    is not computed off a balance the account no longer has,
  * a pinned FOXIFY_START_BALANCE and the ledger agree on where the account is.

Two source shapes are accepted, both served by soomario-prop:

  /api/performance            public. trades[]: close, entry, qty, side, pnl,
                              hold_sec. pnl is the WALLET delta per trade, fees
                              and funding included, so it is exact; the exit
                              price is derived from it and is therefore an
                              effective price, not the fill.
  /api/journal?secret=...     raw records: asset, side, qty, entry_px, exit_px,
                              balance_before/after, pnl_est, opened_at, closed_at.
                              Carries the real exit fill; the fee is backed out
                              so realized PnL still equals the wallet delta.

The source may also be a local file path holding either JSON body.

Idempotent: a trade whose (coin, closed_at) is already in the ledger is skipped.
"""
import json
import logging
from datetime import datetime, timedelta

import requests

logger = logging.getLogger("journal_import")


def _load(src: str):
    if src.startswith(("http://", "https://")):
        r = requests.get(src, timeout=20)
        r.raise_for_status()
        return r.json()
    with open(src, encoding="utf-8") as f:
        return json.load(f)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opened(closed_at, hold_sec):
    if not closed_at or hold_sec is None:
        return None
    try:
        return (datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
                - timedelta(seconds=float(hold_sec))).isoformat()
    except (TypeError, ValueError):
        return None


def _from_performance(body: dict, coin: str) -> list:
    out = []
    for t in body.get("trades") or []:
        entry, qty, pnl = _f(t.get("entry")), _f(t.get("qty")), _f(t.get("pnl"))
        side = str(t.get("side") or "").lower()
        if not entry or not qty or pnl is None or side not in ("long", "short"):
            continue
        sign = 1.0 if side == "long" else -1.0
        exit_ = entry + sign * pnl / qty          # effective: reproduces pnl exactly
        out.append(dict(coin=coin, side=side, entry=entry, exit=exit_, qty=qty, fee=0.0,
                        opened_at=_opened(t.get("close"), t.get("hold_sec")),
                        closed_at=t.get("close"), pnl=pnl))
    return out


def _from_journal(body: dict, coin: str) -> list:
    out = []
    for r in body.get("records") or []:
        entry, qty, exit_ = _f(r.get("entry_px")), _f(r.get("qty")), _f(r.get("exit_px"))
        side = str(r.get("side") or "").lower()
        if not entry or not qty or not exit_ or side not in ("long", "short"):
            continue
        sign = 1.0 if side == "long" else -1.0
        gross = (exit_ - entry) * sign * qty
        b0, b1 = _f(r.get("balance_before")), _f(r.get("balance_after"))
        pnl = (b1 - b0) if (b0 is not None and b1 is not None) else _f(r.get("pnl_est"))
        if pnl is None:
            pnl = gross
        out.append(dict(coin=(r.get("asset") or coin), side=side, entry=entry, exit=exit_,
                        qty=qty, fee=gross - pnl, opened_at=r.get("opened_at"),
                        closed_at=r.get("closed_at"), pnl=pnl))
    return out


def parse(body, coin: str = "HYPE") -> list:
    if isinstance(body, dict) and "records" in body:
        rows = _from_journal(body, coin)
    elif isinstance(body, dict) and "trades" in body:
        rows = _from_performance(body, coin)
    else:
        raise ValueError("unrecognised source: expected soomario-prop /api/performance "
                         "or /api/journal JSON")
    rows.sort(key=lambda x: str(x["closed_at"] or ""))
    return rows


def run(db, src: str, coin: str = "HYPE") -> int:
    """Book every not-yet-present trade. Returns the number added."""
    body = _load(src)
    rows = parse(body, coin.upper())
    have = {(r[0], r[1]) for r in db._conn.execute("SELECT coin, closed_at FROM trades")}
    added = 0
    for t in rows:
        key = (str(t["coin"]).upper(), t["closed_at"])
        if key in have:
            continue
        notional = t["entry"] * t["qty"]
        sign = 1.0 if t["side"] == "long" else -1.0
        ret_pct = (t["exit"] - t["entry"]) * sign / t["entry"] * 100
        net_pct = ret_pct - (t["fee"] / notional * 100 if notional else 0.0)
        db._conn.execute(
            "INSERT INTO trades (coin, side, entry, exit, qty, ret_pct, net_pct, "
            "friction_pct, fee, exit_reason, opened_at, closed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (key[0], t["side"], t["entry"], round(t["exit"], 8), t["qty"],
             round(ret_pct, 4), round(net_pct, 4), None, round(t["fee"], 6),
             "IMPORTED", t["opened_at"], t["closed_at"]))
        have.add(key)
        added += 1
    db._conn.commit()

    # Start the account's history where the imported history starts.
    if rows:
        first = min((t["opened_at"] or t["closed_at"]) for t in rows)
        acct = db.account()
        if first and (not acct.get("inception_ts") or first < acct["inception_ts"]):
            db.set_account(inception_ts=first)
    total = sum(t["pnl"] for t in rows)
    logger.info(f"📥 journal import: {len(rows)} trade(s) in source, {added} added, "
                f"source realized ${total:+.2f}")
    return added
