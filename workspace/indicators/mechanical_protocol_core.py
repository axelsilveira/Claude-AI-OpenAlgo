"""
Mechanical Protocol (Long Only) -- shared strategy core.

Port of a Pine Script v6 intraday breakout strategy: PDH/PDL + true-gap
filter, 10 SMA, Parabolic SAR, RSI(14), MACD(12,26,9), one signal/day,
1:1 R exit, time-based close.

Used by both the single-symbol dashboard
(dashboards/mechanical_protocol/app.py) and the watchlist scanner
(scanners/mechanical_protocol_watchlist_scan.py) so the entry/exit logic
-- already fixed a few times (ta.psar not ta.sar, tz_localize not
tz_convert, fill-price-based SL/TP) -- only lives in one place.
"""
import numpy as np
import pandas as pd
from openalgo import ta


def fetch_data(client, symbol, exchange, interval, start_date, end_date):
    df = client.history(
        symbol=symbol, exchange=exchange, interval=interval,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),
    )
    if isinstance(df, dict):
        raise RuntimeError(df.get("message", "Unknown error from OpenAlgo API"))
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
    else:
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    if df.index.tz is not None:
        # tz_convert(None) would shift to UTC before dropping the label; the
        # broker feed is already IST wall-clock time, so just drop the label.
        df.index = df.index.tz_localize(None)
    return df


def compute_indicators(df, sma_len, rsi_len):
    close, high, low = df["close"], df["high"], df["low"]
    sma = ta.sma(close, sma_len)
    rsi = ta.rsi(close, rsi_len)
    macd_line, macd_signal, macd_hist = ta.macd(close, 12, 26, 9)
    psar_arr = ta.psar(high, low, acceleration=0.02, maximum=0.2)

    def _s(x):
        return x if isinstance(x, pd.Series) else pd.Series(x, index=df.index)

    return {
        "sma": _s(sma), "rsi": _s(rsi), "psar": _s(psar_arr),
        "macd_line": _s(macd_line), "macd_signal": _s(macd_signal), "macd_hist": _s(macd_hist),
    }


def compute_pdh_pdl_gap(df):
    """Returns (days_sorted, pdh_by_day, pdl_by_day, true_gap_by_day)."""
    days_sorted = sorted(df["day"].unique())
    day_high_final = df.groupby("day")["high"].max()
    day_low_final = df.groupby("day")["low"].min()
    first_row_by_day = df.groupby("day").head(1).set_index("day")
    last_row_by_day = df.groupby("day").tail(1).set_index("day")

    pdh_by_day, pdl_by_day, true_gap_by_day = {}, {}, {}
    for idx, d in enumerate(days_sorted):
        first_high = first_row_by_day.loc[d, "high"]
        first_low = first_row_by_day.loc[d, "low"]
        if idx == 0:
            pdh_by_day[d] = np.nan
            pdl_by_day[d] = np.nan
            true_gap_by_day[d] = 0.0
            continue
        prev_day = days_sorted[idx - 1]
        pdh_by_day[d] = day_high_final[prev_day]
        pdl_by_day[d] = day_low_final[prev_day]
        yest_high = last_row_by_day.loc[prev_day, "high"]
        yest_low = last_row_by_day.loc[prev_day, "low"]
        if first_low > yest_high:
            true_gap_by_day[d] = (first_low - yest_high) / yest_high * 100
        elif first_high < yest_low:
            true_gap_by_day[d] = (first_high - yest_low) / yest_low * 100
        else:
            true_gap_by_day[d] = 0.0

    return days_sorted, pdh_by_day, pdl_by_day, true_gap_by_day


def compute_long_condition(df, ind, entry_start, entry_end, rsi_threshold, max_gap_filter):
    close, high, low, open_ = df["close"], df["high"], df["low"], df["open"]
    hour_arr, minute_arr = df.index.hour, df.index.minute
    entry_start_min = entry_start.hour * 60 + entry_start.minute
    entry_end_min = entry_end.hour * 60 + entry_end.minute
    bar_min = hour_arr * 60 + minute_arr
    valid_entry_time = (bar_min >= entry_start_min) & (bar_min <= entry_end_min)

    is_green = close > open_
    price_above_sma = close > ind["sma"]
    psar_bullish = close > ind["psar"]
    rsi_bullish = ind["rsi"] > rsi_threshold
    macd_bullish = ind["macd_line"] > ind["macd_signal"]
    valid_breakout = is_green & (open_ < df["pdh"]) & (low < df["pdh"]) & (high > df["pdh"]) & (close > df["pdh"])
    is_not_gap_up = df["true_gap_pct"] <= 0
    valid_gap_down = df["true_gap_pct"].abs() <= max_gap_filter

    return (
        valid_entry_time & is_not_gap_up & valid_gap_down & price_above_sma
        & psar_bullish & rsi_bullish & macd_bullish & valid_breakout & df["pdh"].notna()
    )


def simulate_trades(df, long_condition, time_exit, rr_ratio=1.0):
    """
    One SIGNAL per day (not one trade): the first bar that satisfies
    long_condition locks in a resting buy-stop at that bar's high. It is
    never re-defined or cancelled on later bars even if long_condition
    later turns false -- it just sits on the book until price trades back
    above that level (any bar the same day) or the day ends, whichever
    comes first. If it never fills, no further signal is sought that day.
    SL/TP are computed at FILL time, not signal time: SL is the low of the
    bar immediately before the fill bar, and TP is fill_price + risk *
    rr_ratio (default 1.0 = original 1:1), so both are measured off the
    actual fill price and a gap-through fill doesn't skew R:R.

    Returns (trades, state) where state has the end-of-data position/pending/
    signal_locked/trade_taken_today -- useful for "what's happening right now".
    """
    trades = []
    position, pending = None, None
    trade_taken_today = False
    signal_locked = False
    current_date = None
    time_exit_min = time_exit.hour * 60 + time_exit.minute

    for i in range(len(df)):
        row = df.iloc[i]
        ts = df.index[i]
        date = row["day"]

        if date != current_date:
            current_date = date
            trade_taken_today = False
            signal_locked = False
            pending = None  # any unfilled resting order is cancelled at day rollover

        # Fill check for the resting stop order, against this bar's range
        if pending is not None and position is None:
            if row["high"] >= pending["entry"]:
                fill_price = max(pending["entry"], row["open"])
                sl = df.iloc[i - 1]["low"] if i > 0 else row["low"]
                risk = fill_price - sl
                tp = fill_price + risk * rr_ratio
                position = {
                    "entry": fill_price, "sl": sl, "tp": tp,
                    "entry_time": ts, "entry_idx": i,
                }
                trade_taken_today = True
                pending = None

        # Manage an open position
        if position is not None:
            hit_sl = row["low"] <= position["sl"]
            hit_tp = row["high"] >= position["tp"]
            is_time_exit = (ts.hour * 60 + ts.minute) == time_exit_min
            if hit_sl or hit_tp or is_time_exit:
                if hit_sl:
                    exit_price, reason = position["sl"], "Stop Loss"
                elif hit_tp:
                    exit_price, reason = position["tp"], "Target"
                else:
                    exit_price, reason = row["close"], "Time Exit"
                risk = position["entry"] - position["sl"]
                r_multiple = (exit_price - position["entry"]) / risk if risk else 0.0
                trades.append({
                    "Entry Time": position["entry_time"], "Exit Time": ts,
                    "Entry": round(position["entry"], 2), "SL": round(position["sl"], 2),
                    "TP": round(position["tp"], 2), "Exit": round(exit_price, 2),
                    "Reason": reason, "R": round(r_multiple, 2),
                    "entry_idx": position["entry_idx"], "exit_idx": i,
                })
                position = None

        # Lock in the ONE signal candle for the day and place its resting stop order
        if position is None and pending is None and not signal_locked and not trade_taken_today:
            if bool(long_condition.iloc[i]):
                pending = {"entry": row["high"], "entry_time": ts}
                signal_locked = True

    state = {
        "position": position, "pending": pending,
        "signal_locked": signal_locked, "trade_taken_today": trade_taken_today,
    }
    return trades, state


def compute_metrics(trades):
    """
    Aggregate performance metrics from a list of trade dicts (as returned
    by simulate_trades). max_drawdown_r is the worst peak-to-trough decline
    in cumulative R across the trade sequence -- total_r alone hides a
    strategy that made its money back after a brutal losing streak.
    """
    if not trades:
        return {
            "num_trades": 0, "win_rate": 0.0, "total_r": 0.0, "avg_r": 0.0,
            "max_drawdown_r": 0.0, "profit_factor": 0.0,
        }
    r_values = [t["R"] for t in trades]
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r <= 0]
    total_r = sum(r_values)

    cum, peak, max_dd = 0.0, 0.0, 0.0
    for r in r_values:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0

    return {
        "num_trades": len(r_values),
        "win_rate": round(len(wins) / len(r_values) * 100, 1),
        "total_r": round(total_r, 2),
        "avg_r": round(total_r / len(r_values), 3),
        "max_drawdown_r": round(max_dd, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else profit_factor,
    }


def run_symbol(client, symbol, exchange, interval, start_date, end_date,
               sma_len, rsi_len, rsi_threshold, max_gap_filter,
               entry_start, entry_end, time_exit, rr_ratio=1.0):
    """Fetch + compute everything for one symbol in one call."""
    df = fetch_data(client, symbol, exchange, interval, start_date, end_date)
    if df.empty or len(df) < max(sma_len, rsi_len) + 5:
        raise ValueError("Not enough historical bars returned.")

    df = df.copy()
    df["day"] = df.index.date
    ind = compute_indicators(df, sma_len, rsi_len)
    days_sorted, pdh_by_day, pdl_by_day, true_gap_by_day = compute_pdh_pdl_gap(df)
    df["pdh"] = df["day"].map(pdh_by_day)
    df["pdl"] = df["day"].map(pdl_by_day)
    df["true_gap_pct"] = df["day"].map(true_gap_by_day)

    long_condition = compute_long_condition(df, ind, entry_start, entry_end, rsi_threshold, max_gap_filter)
    trades, state = simulate_trades(df, long_condition, time_exit, rr_ratio)

    return {
        "df": df, "ind": ind, "days_sorted": days_sorted,
        "long_condition": long_condition, "trades": trades, "state": state,
    }
