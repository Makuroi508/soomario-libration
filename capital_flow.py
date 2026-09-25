"""
Soomario Libration — record a deposit or withdrawal
═══════════════════════════════════════════════════
Performance equity is flow-neutral by construction: account.equity is the
starting baseline plus realized PnL, never the wallet. That is what keeps a
deposit from being read as profit — but it also means money added to the
account is invisible. The bot then sizes every position off the old, smaller
equity, and the drawdown floor stays anchored to a balance that no longer
exists. On aureus a $1,130 deposit left the book trading a quarter of the
size it should have.

CAPITAL_FLOW=<amount>   +1130.52 deposit, -250 withdrawal
CAPITAL_FLOW=auto       size the flow from the venue: whatever the wallet holds
                        that the ledger cannot explain

Both raise (or lower) BOTH ends of the identity, so realized PnL, the trade
ledger and the equity curve are untouched:

    inception      += flow      (the drawdown anchor moves with the capital)
    account.equity += flow      (sizing sees the new capital)
    daily_baseline += flow      (today's daily guard is not tripped by it)

`auto` must be run when the book is FLAT, and never after a loss you want the
record to keep: it cannot tell a deposit from PnL the ledger missed, so it
would silently absorb the difference. The amount form is exact and always
safe. Either way the change is logged with both numbers so it can be audited.
"""
import logging

logger = logging.getLogger("capital_flow")


def resolve(db, raw: str, venue_equity=None, open_upnl: float = 0.0) -> float:
    """The flow to apply, from an explicit amount or from the venue."""
    s = (raw or "").strip().lower()
    if s in ("auto", "sync"):
        if not venue_equity or venue_equity <= 0:
            raise RuntimeError("CAPITAL_FLOW=auto needs a venue equity read, got "
                               f"{venue_equity!r}")
        booked = (db.account().get("equity") or 0.0) + open_upnl
        return round(float(venue_equity) - booked, 4)
    flow = float(s)
    if flow == 0:
        raise ValueError("CAPITAL_FLOW=0 does nothing")
    return flow


def apply(db, flow: float) -> dict:
    """Move inception, the equity accumulator and today's daily baseline."""
    a = db.account()
    before = {k: (a.get(k) or 0.0) for k in ("inception", "equity", "daily_baseline")}
    if before["inception"] + flow <= 0 or before["equity"] + flow <= 0:
        raise RuntimeError(f"flow ${flow:,.2f} would take the account to zero or "
                           f"below (inception ${before['inception']:,.2f}, "
                           f"equity ${before['equity']:,.2f})")
    db.set_account(inception=round(before["inception"] + flow, 4),
                   equity=round(before["equity"] + flow, 4),
                   daily_baseline=round(before["daily_baseline"] + flow, 4))
    after = db.account()
    word = "deposit" if flow > 0 else "withdrawal"
    logger.warning(
        f"💵 {word} of ${abs(flow):,.2f} recorded: equity ${before['equity']:,.2f} "
        f"-> ${after['equity']:,.2f}, drawdown anchor ${before['inception']:,.2f} "
        f"-> ${after['inception']:,.2f}. Realized PnL and the trade ledger are "
        f"unchanged - REMOVE CAPITAL_FLOW now.")
    return {"flow": flow, "before": before,
            "after": {k: after[k] for k in ("inception", "equity", "daily_baseline")}}
