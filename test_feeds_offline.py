"""
Offline checks for the candle feed's failure behaviour
═════════════════════════════════════════════════════
    python test_feeds_offline.py          (no network, no keys, no venue)

The bug these pin down: on 2026-10-01 Binance IP-banned the Railway egress
address every service shares. fetch_candles answered each failure with the last
good 200 bars, so six bots went on computing RSI(14) from candles that predated
the cross they were waiting for. Nothing errored, nothing halted, the dashboards
looked healthy — aureus and Foxify simply never saw LTC cross 50 and sat in a
short while the vault and the Propr account reversed into a long. Every retry
also renewed the ban.

So: a venue that fails is PARKED (honouring the ban's own expiry when it states
one), the signal path gets [] rather than history, and a coin whose venue is
parked is read from a stand-in venue instead of going dark.
"""
import time

import feeds

ok = fail = 0


def check(label, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label}")


def reset():
    feeds._COOLDOWN.clear()
    feeds._BACKOFF.clear()
    feeds._LAST_GOOD.clear()


BAR = [{"t": 1, "T": 2, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 0.0}]


class Feed:
    """A stand-in venue fetcher that counts calls and can be made to fail."""

    def __init__(self, err=None, bars=None):
        self.err, self.calls = err, 0
        self.bars = BAR if bars is None else bars

    def __call__(self, coin, interval, limit):
        self.calls += 1
        if self.err:
            raise RuntimeError(self.err)
        return self.bars


def install(**venues):
    for name, fn in venues.items():
        feeds._FETCH[name] = fn


_ORIG = dict(feeds._FETCH)
_ORIG_FB = list(feeds._FALLBACK)

print("\n[1] the ban's own expiry is honoured, not guessed")
reset()
feeds._FALLBACK = []
until_ms = int((time.time() + 3600) * 1000)
err = (f'HTTP 418 {{"code":-1003,"msg":"Way too many requests; '
       f'IP(208.77.246.105) banned until {until_ms}. Please use the websocket"}}')
check("'banned until' is read out of Binance's own message",
      abs(feeds._ban_until(err) - until_ms / 1000) < 1)
check("seconds-vs-milliseconds is not confused", feeds._ban_until("banned until 1790893138") ==
      1790893138)
check("an unrelated error carries no expiry", feeds._ban_until("HTTP 500 oops") is None)

b = Feed(err=err)
install(binance=b)
check("the first failure returns nothing to the signal path",
      feeds.fetch_candles("binance", "LTC", "4h", 200) == [])
check("the venue is parked until the ban it declared expires",
      abs(feeds._COOLDOWN["binance"] - until_ms / 1000) < 1)
check("one failing poll costs two attempts, not more", b.calls == 2)

print("\n[2] a parked venue is not polled again")
before = b.calls
for _ in range(25):
    feeds.fetch_candles("binance", "LTC", "4h", 200)
check("25 further ticks make zero requests while parked", b.calls == before)
check("venue_ready reports the park", feeds.venue_ready("binance") is False)
check("an unrelated venue is unaffected", feeds.venue_ready("bybit") is True)

print("\n[3] no stale candles on the signal path")
reset()
feeds._FALLBACK = []
good = Feed()
install(binance=good)
check("a healthy venue returns its bars", feeds.fetch_candles("binance", "LTC", "4h", 200) == BAR)
install(binance=Feed(err="down"))
check("the series is remembered", feeds._LAST_GOOD[("binance", "LTC", "4h")] == BAR)
check("but an outage hands the signal path [], not yesterday's bars",
      feeds.fetch_candles("binance", "LTC", "4h", 200) == [])
check("stale bars remain available to callers that ask for them",
      feeds.fetch_candles("binance", "LTC", "4h", 200, stale_ok=True) == BAR)

print("\n[4] generic failures back off, successes clear it")
reset()
feeds._FALLBACK = []
install(binance=Feed(err="connection reset"))
t0 = time.time()
feeds.fetch_candles("binance", "LTC", "4h", 200)
first = feeds._COOLDOWN["binance"] - t0
check("an outage with no stated expiry parks for a minute", 55 < first < 65)
feeds._COOLDOWN.clear()                       # let the next poll through
feeds.fetch_candles("binance", "LTC", "4h", 200)
check("a repeat failure doubles the penalty", 110 < feeds._BACKOFF["binance"] < 130)
for _ in range(12):
    feeds._COOLDOWN.clear()
    feeds.fetch_candles("binance", "LTC", "4h", 200)
check("the penalty is capped at half an hour", feeds._BACKOFF["binance"] == 1800.0)
feeds._COOLDOWN.clear()
install(binance=Feed())
feeds.fetch_candles("binance", "LTC", "4h", 200)
check("one good poll clears the penalty", "binance" not in feeds._BACKOFF)
check("and clears the park", feeds.venue_ready("binance") is True)

print("\n[5] an empty answer counts as a failure")
reset()
feeds._FALLBACK = []
install(binance=Feed(bars=[]))
check("an empty candle list is not passed off as a result",
      feeds.fetch_candles("binance", "LTC", "4h", 200) == [])
check("and parks the venue", feeds.venue_ready("binance") is False)

print("\n[6] a parked coin is read somewhere else rather than going dark")
reset()
feeds._FALLBACK = ["bybit", "okx", "binance"]
alt = Feed(bars=[dict(BAR[0], c=2.0)])
install(binance=Feed(err="HTTP 418 banned"), bybit=alt, okx=Feed(err="also down"))
feeds.fetch_candles("binance", "LTC", "4h", 200)          # parks binance
got = feeds.fetch_candles("binance", "LTC", "4h", 200)
check("the stand-in venue supplies the bars", got and got[0]["c"] == 2.0)
check("the stand-in is actually called", alt.calls >= 1)

reset()
feeds._FALLBACK = ["bybit", "okx"]
install(binance=Feed(err="HTTP 418 banned"), bybit=Feed(err="down"), okx=Feed(bars=BAR))
feeds.fetch_candles("binance", "LTC", "4h", 200)
check("a stand-in that is also down is skipped for the next one",
      feeds.fetch_candles("binance", "LTC", "4h", 200) == BAR)

reset()
feeds._FALLBACK = ["bybit"]
bb = Feed(err="down")
install(binance=Feed(err="HTTP 418 banned"), bybit=bb)
feeds.fetch_candles("binance", "LTC", "4h", 200)
feeds.fetch_candles("binance", "LTC", "4h", 200)
n = bb.calls
for _ in range(10):
    feeds.fetch_candles("binance", "LTC", "4h", 200)
check("a stand-in that fails is parked too, not hammered", bb.calls == n)
check("with every venue down the signal path still gets []",
      feeds.fetch_candles("binance", "LTC", "4h", 200) == [])

reset()
feeds._FALLBACK = ["bybit"]
install(binance=Feed(), bybit=Feed(bars=[dict(BAR[0], c=9.0)]))
got = feeds.fetch_candles("binance", "LTC", "4h", 200)
check("a healthy venue is never substituted", got[0]["c"] == 1.0)

print("\n[7] routing is unchanged")
reset()
feeds._FETCH.clear()
feeds._FETCH.update(_ORIG)
feeds._FALLBACK = _ORIG_FB


class StubHL:
    def __init__(self): self.calls = []

    def fetch_candles(self, coin, interval, limit):
        self.calls.append((coin, interval, limit))
        return BAR


hl = StubHL()
check("hyperliquid still routes to the worker's own client",
      feeds.fetch_candles("hyperliquid", "LTC", "4h", 200, hl_client=hl) == BAR)
check("and is not subject to venue parking", hl.calls == [("LTC", "4h", 200)])
try:
    feeds.fetch_candles("hyperliquid", "LTC", "4h", 200)
    check("a missing client still raises rather than trading blind", False)
except RuntimeError:
    check("a missing client still raises rather than trading blind", True)

RULE = "-" * 58
print("")
print(RULE)
print(f"  {ok} passed, {fail} failed")
print(RULE)
raise SystemExit(1 if fail else 0)
