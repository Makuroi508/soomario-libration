"""
signals.py — RSI(14) on 4h closes, crossover/crossunder entry detection.

Frozen strategy (do not re-optimize):
  long  = crossover(RSI, 50)   RSI crosses UP   through 50 on a completed 4h close
  short = crossunder(RSI, 40)  RSI crosses DOWN through 40 on a completed 4h close

RSI is Wilder's RSI (RMA smoothing), identical to TradingView ta.rsi.
Entries are evaluated ONLY on the close of a completed 4h candle. The forming
candle is never used for entries. Callers track the last closed 4h timestamp
per coin (see new_closed_bar) so an intra-bar poll cannot double-fire a signal.
"""


def wilder_rsi(closes, length=14):
    """Wilder's RSI. Returns a list aligned to `closes`; leading entries are None
    until enough data exists. Matches TradingView ta.rsi(close, length)."""
    if len(closes) < length + 1:
        return [None] * len(closes)
    rsis = [None] * len(closes)
    gains = losses = 0.0
    for i in range(1, length + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / length, losses / length
    rsis[length] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(length + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (length - 1) + max(ch, 0.0)) / length
        al = (al * (length - 1) + max(-ch, 0.0)) / length
        rsis[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return rsis


def entry_signal(rsi_prev, rsi_now, long_level=50.0, short_level=40.0):
    """Return 'long', 'short', or None. Evaluate on the close of a completed 4h bar.

    long  : rsi_prev < 50 <= rsi_now   (crossover up through 50)
    short : rsi_prev > 40 >= rsi_now   (crossunder down through 40)
    """
    if rsi_prev is None or rsi_now is None:
        return None
    if rsi_prev < long_level <= rsi_now:
        return "long"
    if rsi_prev > short_level >= rsi_now:
        return "short"
    return None


def rsi_at(rsi_candles, rsi_values, ts_ms):
    """The RSI as TradingView would read it at `ts_ms`: the value of the last
    RSI-timeframe bar that had CLOSED by then. None if there is no such bar."""
    val = None
    for c, r in zip(rsi_candles, rsi_values):
        if c.get("T") is not None and c["T"] <= ts_ms and r is not None:
            val = r
        elif c.get("T") is not None and c["T"] > ts_ms:
            break
    return val


def cross_on_chart(rsi_candles, rsi_values, chart_closed,
                   long_level=50.0, short_level=40.0):
    """Entry signal evaluated on the CHART's last two closed bars.

    TradingView runs the script once per chart bar and reads the higher- (or
    lower-) timeframe RSI through request.security, so the cross it sees is
    between the RSI as of one chart close and the RSI as of the next. When the
    two timeframes are equal this is exactly entry_signal(rsi[-2], rsi[-1]).
    """
    if len(chart_closed) < 2:
        return None, None, None
    now_ts, prev_ts = chart_closed[-1]["T"], chart_closed[-2]["T"]
    now = rsi_at(rsi_candles, rsi_values, now_ts)
    prev = rsi_at(rsi_candles, rsi_values, prev_ts)
    return entry_signal(prev, now, long_level, short_level), prev, now


def new_closed_bar(last_seen_ts, latest_closed_ts):
    """True only when a new 4h candle has closed since we last evaluated.
    Guards against double-firing on intra-bar polls.

    last_seen_ts / latest_closed_ts are the candle OPEN timestamps (ms) of the
    last bar we already processed and the most recently CLOSED bar respectively.
    """
    if latest_closed_ts is None:
        return False
    if last_seen_ts is None:
        return True
    return latest_closed_ts > last_seen_ts


def closed_candles(candles, now_ms):
    """Given raw candles (each a dict with 't' open-ms and 'T' close-ms), return
    only the candles that have fully closed at `now_ms`. The forming candle is
    dropped so it can never feed an entry decision."""
    return [c for c in candles if c.get("T") is not None and c["T"] <= now_ms]
