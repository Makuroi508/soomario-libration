"""
Soomario Libration — Ledger repair (one-shot, operator-run)
═══════════════════════════════════════════════════════════
Reconciles the local trade ledger against the VENUE's own trade history and
equity, then rebuilds the dashboard curve from the corrected ledger.

WHY THIS EXISTS
On Propr, get_user_fills() sent `?limit=200` to /accounts/{id}/trades. That
endpoint rejects any query string with a bare 400 — the same quirk /positions
already documents. So the call failed on every attempt, silently: reconcile()
read "the fill never indexed" instead of "the fills endpoint is broken", and
fell through to its stop-estimate path. Consequences in the 25-29 Jul run:

  • 42 of 45 closes booked at an ESTIMATE, not a real fill
  • fee = 0.00 on every trade (0.075%/side never charged → ~$50 unaccounted)
  • friction_pct — the entire go/no-go metric — measured nothing
  • 8 positions that never armed a trail booked at exactly -10.00%, the hard
    stop, while the observed market return was between +0.37% and -5.93%

Net effect: the ledger showed -$465.74 realized while Propr's ledger showed
-$138.95. The dashboard was reporting a loss roughly 3.3x the real one.

USAGE (run with the same env as the service, so it can reach the venue and the
volume; on Railway this is a one-off shell on the service):

    python repair.py --inspect     # dump the raw /trades payload. DO THIS FIRST.
    python repair.py --report      # reconcile and print what would change
    python repair.py --apply       # write the corrections + rebuild the curve

--inspect exists because no one has ever seen this payload: get_user_fills()
never returned a row, so its field mapping (executedAt / price / quantity /
fee / realizedPnl / positionSide / type) is inferred from prose, not observed.
Verify the names against --inspect output before trusting --report.

Nothing is written without --apply. A timestamped .bak copy of the DB is taken
before the first write.
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

# How far from a local trade's closed_at a venue fill may sit and still be
# considered the same close. Generous: reconcile only books after an 8-tick
# debounce (~15 min), and the fabricated rows were booked ~14 min after entry.
MATCH_WINDOW = timedelta(hours=3)
# Size agreement required to call it the same close.
SIZE_TOL = 0.02


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
# Candidate query params, tried one at a time. `limit` is known to 400 — it is
# included so the output shows the contrast rather than assuming it.
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
    are demonstrably wrong about this endpoint (limit -> bare 400)."""
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
                mark = " ★ MORE" if base_n is not None and len(rows) > base_n else ""
                print(f"  {label:<38} -> 200  {len(rows):>4} row(s){span}{mark}")
            except ValueError:
                print(f"  {label:<38} -> 200  (non-JSON)")
        else:
            msg = ""
            try:
                msg = str(r.json().get("message", ""))[:50]
            except ValueError:
                msg = r.text[:50]
            print(f"  {label:<38} -> {r.status_code}  {msg}")
    print("  ★ = returned more than the default page; use that param to page.\n")


# ── --inspect ───────────────────────────────────────────────────
def inspect(client):
    """Dump the raw venue trade payload so the field mapping can be verified."""
    if not hasattr(client, "_req") or not getattr(client, "account_id", None):
        # Only Propr has the unverified mapping this is here to check.
        rows = client.get_user_fills()
        print(f"{config.EXCHANGE}: get_user_fills() returned {len(rows)} row(s)")
        for r in rows[:5]:
            print(json.dumps(r, indent=2, default=str))
        return
    raw = client._req("GET", f"/accounts/{client.account_id}/trades")
    if raw is None:
        print("✗ /trades returned no data. If this is a 400, the endpoint is "
              "still being sent query params somewhere.")
        return
    rows = raw.get("data", raw if isinstance(raw, list) else [])
    print(f"✓ /trades returned {len(rows)} row(s)")

    # The default page is small (10 observed) and covers only the last few
    # hours, so a historical repair needs paging. Nothing is documented and
    # `limit` 400s, so probe: show the envelope, then try candidate params and
    # report which the server actually accepts.
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
    for r in rows[:5]:
        print(json.dumps(r, indent=2, default=str))
    mapped = client.get_user_fills()
    print(f"\nget_user_fills() mapped {len(mapped)} row(s)")
    closes = [f for f in mapped if str(f.get("dir", "")).startswith("Close")]
    print(f"  of which closing fills: {len(closes)}")
    if mapped and not closes:
        print("  ⚠ no row mapped to a Close — check the `type` values above "
              "against the ('reduce','close','liquidation') test in get_user_fills().")


# ── reconciliation ──────────────────────────────────────────────
def _venue_closes(client):
    """Closing fills grouped by coin, newest first."""
    by_coin = {}
    for f in client.get_user_fills():
        if not str(f.get("dir", "")).startswith("Close"):
            continue
        coin = str(f.get("coin", "")).upper()
        if coin:
            by_coin.setdefault(coin, []).append(f)
    for v in by_coin.values():
        v.sort(key=lambda f: _ts(f.get("time")) or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    return by_coin


def _match(trade, fills):
    """Find the venue fill(s) that closed this local trade.

    Fills are grouped by orderId FIRST. One logical close routinely arrives as
    several trade rows sharing an order (0.493 + 1.196 + 1.779 for a single
    3.468 BCH close), so comparing individual rows against the booked quantity
    finds nothing and would condemn a perfectly real trade as fabricated.
    Returns (avg_px, fee, realized_pnl, n_fills) or None.
    """
    closed = _ts(trade["closed_at"])
    qty = float(trade["qty"] or 0)
    if not closed or qty <= 0:
        return None
    groups = {}
    for f in fills:
        ft = _ts(f.get("time"))
        if not ft or abs(ft - closed) > MATCH_WINDOW:
            continue
        try:
            sz, px = abs(float(f.get("sz") or 0)), float(f.get("px") or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0:
            continue
        g = groups.setdefault(f.get("oid") or f"_{ft.isoformat()}",
                              {"sz": 0.0, "notional": 0.0, "fee": 0.0,
                               "pnl": 0.0, "n": 0, "dt": abs(ft - closed)})
        g["sz"] += sz
        g["notional"] += px * sz
        g["fee"] += float(f.get("fee") or 0)
        g["pnl"] += float(f.get("closedPnl") or 0)
        g["n"] += 1
        g["dt"] = min(g["dt"], abs(ft - closed))
    # Prefer the size-consistent group closest in time to the booked close.
    cands = [g for g in groups.values() if abs(g["sz"] - qty) / qty <= SIZE_TOL]
    if not cands:
        return None
    g = min(cands, key=lambda x: x["dt"])
    return g["notional"] / g["sz"], g["fee"], g["pnl"], g["n"]


def reconcile(db, client):
    """Compare every local trade against the venue. Returns (fixes, orphans)."""
    by_coin = _venue_closes(client)
    rows = db._conn.execute(
        "SELECT id, coin, side, entry, exit, qty, fee, net_pct, exit_reason, "
        "opened_at, closed_at FROM trades ORDER BY id ASC").fetchall()
    fixes, orphans = [], []
    for t in rows:
        t = dict(t)
        m = _match(t, by_coin.get(t["coin"].upper(), []))
        if m is None:
            orphans.append(t)
            continue
        avg_px, fee, pnl, n = m
        if (abs(avg_px - float(t["exit"])) / float(t["exit"]) > 1e-6
                or abs(fee - float(t["fee"] or 0)) > 1e-6):
            fixes.append((t, avg_px, fee, n))
    return fixes, orphans


def _pnl(side, entry, exit_, qty, fee):
    sign = 1.0 if side == "long" else -1.0
    return (float(exit_) - float(entry)) * sign * float(qty) - float(fee or 0.0)


def report(db, client, apply=False):
    fixes, orphans = reconcile(db, client)
    acct = db.account()
    before = db.realized_pnl()

    print(f"\n{'═' * 72}")
    print(f"  LEDGER RECONCILIATION — {config.EXCHANGE.upper()}")
    print(f"{'═' * 72}")
    print(f"  local trades          {db.trade_count()}")
    print(f"  matched to venue      {len(fixes)} need correction")
    print(f"  NO venue fill found   {len(orphans)}")

    if fixes:
        print(f"\n  ── corrections (exit / fee from the real fill) ──")
        for t, px, fee, n in fixes:
            old = _pnl(t["side"], t["entry"], t["exit"], t["qty"], t["fee"])
            new = _pnl(t["side"], t["entry"], px, t["qty"], fee)
            print(f"   {t['closed_at'][:19]} {t['coin']:9} {t['side']:5} "
                  f"exit {float(t['exit']):>11.6f} -> {px:>11.6f}  "
                  f"fee {float(t['fee'] or 0):>6.2f} -> {fee:>6.2f}  "
                  f"pnl {old:>+9.2f} -> {new:>+9.2f}  ({n} fill{'s' if n > 1 else ''})")

    if orphans:
        print(f"\n  ── no matching venue fill — candidates to VOID ──")
        print(f"     (a local 'close' the venue has no record of: the position")
        print(f"      never existed, so its PnL was invented)")
        for t in orphans:
            p = _pnl(t["side"], t["entry"], t["exit"], t["qty"], t["fee"])
            flag = "  ← fabricated -10% signature" if (t["exit_reason"] == "HARD_STOP"
                                                       and (t["net_pct"] or 0) <= -9.0) else ""
            print(f"   {(t['closed_at'] or '')[:19]} {t['coin']:9} {t['side']:5} "
                  f"{t['exit_reason'] or '':12} net {float(t['net_pct'] or 0):>7.2f}% "
                  f"pnl {p:>+9.2f}{flag}")

    # What the ledger becomes.
    after = before
    for t, px, fee, _ in fixes:
        after += _pnl(t["side"], t["entry"], px, t["qty"], fee) - \
                 _pnl(t["side"], t["entry"], t["exit"], t["qty"], t["fee"])
    for t in orphans:
        after -= _pnl(t["side"], t["entry"], t["exit"], t["qty"], t["fee"])

    venue_eq = client.get_equity() or 0.0
    initial = getattr(client, "_initial_balance", None) or acct.get("inception") or 0.0
    venue_realized = venue_eq - initial if initial else None

    print(f"\n  ── realized PnL ──")
    print(f"   ledger now            ${before:>+10.2f}")
    print(f"   ledger after repair   ${after:>+10.2f}")
    if venue_realized is not None:
        print(f"   venue truth           ${venue_realized:>+10.2f}   "
              f"(equity ${venue_eq:,.2f} − initial ${initial:,.2f})")
        print(f"   residual gap          ${after - venue_realized:>+10.2f}")
    print(f"{'═' * 72}\n")

    if not apply:
        print("Dry run — nothing written. Re-run with --apply to commit.\n")
        return

    if not (fixes or orphans):
        print("Nothing to change.\n")
        return

    bak = f"{db.path}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak"
    shutil.copy2(db.path, bak)
    print(f"→ backup written: {bak}")

    for t, px, fee, _ in fixes:
        sign = 1.0 if t["side"] == "long" else -1.0
        move = (px - float(t["entry"])) * sign
        ret = move / float(t["entry"]) * 100
        fee_pct = fee / (float(t["entry"]) * float(t["qty"])) * 100
        db._conn.execute(
            "UPDATE trades SET exit=?, fee=?, ret_pct=?, net_pct=? WHERE id=?",
            (px, fee, round(ret, 4), round(ret - fee_pct, 4), t["id"]))
    for t in orphans:
        db._conn.execute("DELETE FROM trades WHERE id=?", (t["id"],))
    db._conn.commit()
    print(f"→ {len(fixes)} corrected, {len(orphans)} voided")

    # Re-pin the equity accumulator to the repaired ledger, then anchor the
    # inception so the curve tip lands on the venue's number. On a challenge
    # account there are no deposits/withdrawals, so the venue ledger IS the
    # flow-neutral truth and the two must agree.
    realized = db.realized_pnl()
    if venue_realized is not None and abs(realized - venue_realized) > 0.01:
        drift = venue_realized - realized
        print(f"→ residual ${drift:+.2f} unexplained by trade records "
              f"(un-booked fees, partial fills, or closes outside the match window)")
    inception = venue_eq - realized if venue_realized is not None else (acct.get("inception") or 0.0)
    db.set_account(equity=round(inception + realized, 4), inception=round(inception, 4),
                   inception_ts=acct.get("inception_ts") or iso())
    print(f"→ account resynced: inception ${inception:,.2f}, "
          f"realized ${realized:+,.2f}, equity ${inception + realized:,.2f}")

    from app import _rebuild_logs_from_ledger
    _rebuild_logs_from_ledger(db)
    print("→ equity curve + trade log rebuilt from the repaired ledger\n")


def main():
    ap = argparse.ArgumentParser(description="Reconcile the local ledger against the venue.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--inspect", action="store_true", help="dump the raw /trades payload")
    g.add_argument("--report", action="store_true", help="show what would change")
    g.add_argument("--apply", action="store_true", help="commit the corrections")
    a = ap.parse_args()

    client = _client()
    if a.inspect:
        inspect(client)
        return
    db = DB()
    # Guard against the obvious footgun: running --report/--apply from a laptop.
    # The real ledger lives on the service's mounted volume, so DB() here opens a
    # brand-new empty file and the run would silently "reconcile" nothing.
    # `railway run` does not help — it injects env vars but still runs locally,
    # with no volume. Use RECONCILE_LEDGER=report|apply on the service instead.
    if db.trade_count() == 0:
        sys.exit(f"{db.path} has no trades — this is not the live ledger.\n"
                 f"The real DB is on the service's volume. Set the env var "
                 f"RECONCILE_LEDGER=report (then =apply) on Railway and redeploy;\n"
                 f"the repair runs at boot and prints to the deploy logs.")
    report(db, client, apply=a.apply)


if __name__ == "__main__":
    main()
