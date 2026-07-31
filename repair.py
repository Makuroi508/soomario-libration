"""
Soomario Libration — Ledger repair (one-shot, operator-run)
═══════════════════════════════════════════════════════════
Rebuilds the local trade ledger from the VENUE's own fills, then rebuilds the
dashboard curve from it.

WHY THIS EXISTS
On Propr, get_user_fills() sent `?limit=200` to /accounts/{id}/trades. That
endpoint rejects any query string it doesn't know with a bare 400 — the same
quirk /positions already documents. The call failed on every attempt, silently:
reconcile() read "the fill never indexed" instead of "the fills endpoint is
broken", and fell through to its stop-estimate path. Consequences:

  • every close booked at an ESTIMATE, never a real fill
  • fee = 0.00 on every trade (0.045%/side never charged, ~$34 unaccounted)
  • friction_pct — the entire go/no-go metric — measured nothing
  • positions that never armed a trail booked at exactly the hard stop, -10%,
    while the observed market return was between +0.37% and -5.93%

Local ledger: -$477.72. Propr's own dashboard: -$77.15.

HOW THE REBUILD WORKS
Fills are grouped by positionId — the venue's position lifecycle id — so each
group is exactly one round trip with its true entry, exit, fees and realized
PnL. An earlier version matched booked trades to fills by time and size; that
mis-paired badly (a local BCH *long* matched the fills that closed the BCH
*short* before it on the same coin) and produced +$80 against a truth of -$75.
positionId grouping reproduces the venue total to the cent.

USAGE — set RECONCILE_LEDGER on the service and redeploy; it runs at boot,
because the SQLite file lives on the Railway volume:

    RECONCILE_LEDGER=inspect   dump the raw /trades payload + paging probe
    RECONCILE_LEDGER=report    print what would change, write nothing
    RECONCILE_LEDGER=apply     commit it (backs the DB up first)

Remove the env var after one run. Locally, `python repair.py --inspect` works
for the read-only probes; --report/--apply need the service's volume.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone

import config
from db import DB
from utils import iso

# Reuse the old ledger's exit_reason for a rebuilt trade when the same coin
# closed within this window — keeps TRAIL/HARD_STOP labels on the dashboard.
REASON_WINDOW = timedelta(hours=3)


def _client():
    if config.EXCHANGE == "propr":
        from propr_client import ProprClient
        c = ProprClient()
    elif config.EXCHANGE == "foxify":
        from foxify_client import FoxifyClient
        c = FoxifyClient()
    else:
        from hl_client import HLClient
        c = HLClient()
    if not c.init_sdk():
        sys.exit(f"{config.EXCHANGE} init failed — check credentials.")
    return c


def _ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ── paging discovery ────────────────────────────────────────────
_PAGE_PROBES = [
    {"limit": 100}, {"pageSize": 100}, {"page_size": 100}, {"perPage": 100},
    {"per_page": 100}, {"size": 100}, {"count": 100}, {"take": 100},
    {"page": 2}, {"offset": 10}, {"skip": 10},
    {"startTime": "2026-07-25T00:00:00Z"}, {"from": "2026-07-25T00:00:00Z"},
    {"startDate": "2026-07-25"}, {"since": "2026-07-25T00:00:00Z"},
    {"sort": "desc"},
]


def _probe_paging(client):
    """Report which query params /trades accepts. Empirical, because the docs
    are demonstrably wrong about this endpoint (limit=200 -> bare 400) and
    several params are silently IGNORED rather than rejected."""
    import requests
    from propr_client import BASE
    url = f"{BASE}/accounts/{client.account_id}/trades"
    headers = {"X-API-Key": client.api_key, "Content-Type": "application/json"}
    print("\n── paging probe ──")
    base_n = None
    try:
        r0 = requests.get(url, headers=headers, timeout=20)
        base_n = len(r0.json().get("data", [])) if r0.ok else None
    except requests.RequestException:
        pass
    print(f"  {'(no params)':<38} -> {base_n} row(s)  [baseline]")
    for p in _PAGE_PROBES:
        label = ", ".join(f"{k}={v}" for k, v in p.items())
        try:
            r = requests.get(url, headers=headers, params=p, timeout=20)
        except requests.RequestException as e:
            print(f"  {label:<38} -> ERR {type(e).__name__}")
            continue
        if r.status_code in (200, 201):
            try:
                rows = r.json().get("data", [])
                times = sorted(str(x.get("executedAt") or "") for x in rows)
                span = f"  {times[0][:16]}..{times[-1][:16]}" if times else ""
                mark = " * MORE" if base_n is not None and len(rows) > base_n else ""
                print(f"  {label:<38} -> 200  {len(rows):>4} row(s){span}{mark}")
            except ValueError:
                print(f"  {label:<38} -> 200  (non-JSON)")
        else:
            try:
                msg = str(r.json().get("message", ""))[:50]
            except ValueError:
                msg = r.text[:50]
            print(f"  {label:<38} -> {r.status_code}  {msg}")
    print("  * = returned more than the default page; use that param to page.\n")


# ── --inspect ───────────────────────────────────────────────────
def inspect(client):
    """Dump the raw venue trade payload so the field mapping can be verified."""
    if not hasattr(client, "_req") or not getattr(client, "account_id", None):
        rows = client.get_user_fills()
        print(f"{config.EXCHANGE}: get_user_fills() returned {len(rows)} row(s)")
        for r in rows[:5]:
            print(json.dumps(r, indent=2, default=str))
        return
    raw = client._req("GET", f"/accounts/{client.account_id}/trades")
    if raw is None:
        print("x /trades returned no data.")
        return
    rows = raw.get("data", raw if isinstance(raw, list) else [])
    print(f"/trades returned {len(rows)} row(s)")
    if isinstance(raw, dict):
        env = {k: v for k, v in raw.items() if k != "data"}
        print(f"\nenvelope (non-data keys): {json.dumps(env, indent=2, default=str)[:800]}")
    if rows:
        times = sorted(str(r.get("executedAt") or "") for r in rows)
        print(f"span: {times[0]} .. {times[-1]}")
    _probe_paging(client)
    if not rows:
        return
    print(f"\nkeys: {sorted(rows[0].keys())}\n")
    for r in rows[:3]:
        print(json.dumps(r, indent=2, default=str))


# ── venue truth ─────────────────────────────────────────────────
def venue_positions(client):
    """Reconstruct true round trips from the venue's own fills.

    Grouped by positionId, so each group is exactly one round trip: its opening
    fills, its closing fills, its fees and its realized PnL. Returns
    (closed, open_, totals), closed newest-first.
    """
    groups = {}
    for f in client.get_user_fills():
        pid = f.get("pid")
        if not pid:
            continue
        try:
            px, sz = float(f.get("px") or 0), abs(float(f.get("sz") or 0))
        except (TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0:
            continue
        g = groups.setdefault(pid, {"coin": str(f.get("coin", "")).upper(),
                                    "opens": [], "closes": [], "fee": 0.0,
                                    "pnl": 0.0, "times": []})
        g["fee"] += float(f.get("fee") or 0)
        g["pnl"] += float(f.get("closedPnl") or 0)
        ts = _ts(f.get("time"))
        if ts:
            g["times"].append(ts)
        (g["closes"] if str(f.get("dir", "")).startswith("Close")
         else g["opens"]).append((px, sz))

    closed, open_ = [], []
    tot_pnl = tot_fee = 0.0
    for pid, g in groups.items():
        tot_pnl += g["pnl"]
        tot_fee += g["fee"]
        if not g["opens"] or not g["times"]:
            continue
        oq = sum(q for _, q in g["opens"])
        entry = sum(p * q for p, q in g["opens"]) / oq
        rec = {"pid": pid, "coin": g["coin"], "qty": round(oq, 10),
               "entry": entry, "fee": g["fee"], "pnl": g["pnl"],
               "opened_at": min(g["times"]), "closed_at": max(g["times"])}
        if not g["closes"]:
            open_.append(rec)
            continue
        cq = sum(q for _, q in g["closes"])
        rec["exit"] = sum(p * q for p, q in g["closes"]) / cq
        moved_up = rec["exit"] > rec["entry"]
        rec["side"] = "long" if (g["pnl"] > 0) == moved_up else "short"
        closed.append(rec)
    closed.sort(key=lambda r: r["closed_at"], reverse=True)
    return closed, open_, {"pnl": tot_pnl, "fee": tot_fee, "net": tot_pnl - tot_fee}


def _old_reasons(db):
    """(coin, closed_at) -> exit_reason from the existing ledger, so rebuilt
    trades keep their TRAIL / HARD_STOP labels where they were trustworthy."""
    out = []
    for r in db._conn.execute(
            "SELECT coin, closed_at, exit_reason, net_pct FROM trades").fetchall():
        t = _ts(r["closed_at"])
        if t:
            out.append((r["coin"].upper(), t, r["exit_reason"], r["net_pct"] or 0.0))
    return out


def _reason_for(rec, olds):
    """Best-effort label. A fabricated -10% HARD_STOP is never carried over —
    that label was the symptom, not a fact about the trade."""
    best, best_dt = None, None
    for coin, t, reason, net in olds:
        if coin != rec["coin"]:
            continue
        dt = abs(t - rec["closed_at"])
        if dt > REASON_WINDOW:
            continue
        if reason == "HARD_STOP" and net <= -9.0:
            continue
        if best_dt is None or dt < best_dt:
            best, best_dt = reason, dt
    return best or "RECONCILED"


# ── report / apply ──────────────────────────────────────────────
def report(db, client, apply=False):
    closed, open_, tot = venue_positions(client)
    before = db.realized_pnl()
    n_before = db.trade_count()
    rebuilt_net = sum(r["pnl"] - r["fee"] for r in closed)

    print(f"\n{'=' * 72}")
    print(f"  LEDGER REBUILD FROM VENUE FILLS — {config.EXCHANGE.upper()}")
    print(f"{'=' * 72}")
    print(f"  local trades now          {n_before}")
    print(f"  venue round trips (closed) {len(closed)}")
    print(f"  venue positions still open {len(open_)}"
          + (f"  [{', '.join(r['coin'] for r in open_)}]" if open_ else ""))

    print(f"\n  -- rebuilt ledger, newest 15 --")
    for r in closed[:15]:
        print(f"   {r['closed_at'].isoformat()[:19]} {r['coin']:9} {r['side']:5} "
              f"entry {r['entry']:>11.5f} exit {r['exit']:>11.5f} "
              f"fee {r['fee']:>5.2f} net {r['pnl'] - r['fee']:>+8.2f}")
    if len(closed) > 15:
        print(f"   ... and {len(closed) - 15} more")

    print(f"\n  -- realized PnL --")
    print(f"   ledger now                ${before:>+10.2f}")
    print(f"   rebuilt from venue fills  ${rebuilt_net:>+10.2f}")
    print(f"   venue total incl. open    ${tot['net']:>+10.2f}   "
          f"(gross ${tot['pnl']:+.2f} - fees ${tot['fee']:.2f})")
    print(f"   correction                ${rebuilt_net - before:>+10.2f}")
    print(f"{'=' * 72}\n")

    if not apply:
        print("Dry run - nothing written. Set RECONCILE_LEDGER=apply to commit.\n")
        return
    if not closed:
        print("No venue round trips resolved - refusing to wipe the ledger.\n")
        return

    # HARD GUARD. The equity re-anchor below derives inception from
    # get_equity(), which on Propr is marginBalance = wallet + UNREALISED.
    # Applying with positions open folds their floating PnL permanently into
    # the baseline and skews every return figure afterwards.
    open_now = db.open_positions()
    if open_now:
        print(f"x REFUSING TO APPLY - {len(open_now)} position(s) still open: "
              f"{', '.join(p['coin'] for p in open_now)}.")
        print("  Venue equity includes their unrealised PnL, which would be baked")
        print("  into inception and skew every return from here on.")
        print("  Re-run when the book is flat (the report above is unaffected).\n")
        return

    bak = f"{db.path}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak"
    shutil.copy2(db.path, bak)
    print(f"-> backup written: {bak}")

    olds = _old_reasons(db)
    db._conn.execute("DELETE FROM trades")
    for r in sorted(closed, key=lambda x: x["closed_at"]):
        sign = 1.0 if r["side"] == "long" else -1.0
        move = (r["exit"] - r["entry"]) * sign
        ret = move / r["entry"] * 100 if r["entry"] else 0.0
        fee_pct = (r["fee"] / (r["entry"] * r["qty"]) * 100) if r["qty"] else 0.0
        db._conn.execute(
            "INSERT INTO trades (coin, side, entry, exit, qty, ret_pct, net_pct, "
            "friction_pct, fee, exit_reason, opened_at, closed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["coin"], r["side"], r["entry"], r["exit"], r["qty"],
             round(ret, 4), round(ret - fee_pct, 4), None, round(r["fee"], 6),
             _reason_for(r, olds), r["opened_at"].isoformat(),
             r["closed_at"].isoformat()))
    db._conn.commit()
    print(f"-> ledger rebuilt: {n_before} rows -> {db.trade_count()} rows")

    realized = db.realized_pnl()
    venue_eq = client.get_equity() or 0.0
    inception = venue_eq - realized
    acct = db.account()
    db.set_account(equity=round(venue_eq, 4), inception=round(inception, 4),
                   inception_ts=acct.get("inception_ts") or iso())
    print(f"-> account resynced: inception ${inception:,.2f}, "
          f"realized ${realized:+,.2f}, equity ${venue_eq:,.2f}")

    from app import _rebuild_logs_from_ledger
    _rebuild_logs_from_ledger(db)
    print("-> equity curve + trade log rebuilt from the repaired ledger\n")


def main():
    ap = argparse.ArgumentParser(description="Rebuild the local ledger from the venue.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--inspect", action="store_true", help="dump the raw /trades payload")
    g.add_argument("--report", action="store_true", help="show what would change")
    g.add_argument("--apply", action="store_true", help="commit the rebuild")
    a = ap.parse_args()

    client = _client()
    if a.inspect:
        inspect(client)
        return
    db = DB()
    if db.trade_count() == 0:
        sys.exit(f"{db.path} has no trades - this is not the live ledger.\n"
                 f"The real DB is on the service's volume. Set RECONCILE_LEDGER "
                 f"on Railway and redeploy; the repair runs at boot.")
    report(db, client, apply=a.apply)


if __name__ == "__main__":
    main()
