"""
Mechanical Protocol (Long Only) — Streamlit Dashboard
Port of a Pine Script v6 intraday breakout strategy: PDH/PDL + true-gap filter,
10 SMA, Parabolic SAR, RSI(14), MACD(12,26,9), one trade/day, 1:1 R exit,
time-based close.

Run: streamlit run app.py
Open: http://localhost:8501
"""
import os
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from dotenv import find_dotenv, load_dotenv
from openalgo import api

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # workspace/indicators/
from mechanical_protocol_core import run_symbol  # noqa: E402

load_dotenv(find_dotenv(), override=True)

st.set_page_config(page_title="Mechanical Protocol - OpenAlgo", layout="wide")
st.markdown(
    """
    <style>
        .stApp { background-color: #0e1117; }
        .stMetric label { color: #fafafa; }
        .stMetric [data-testid="stMetricValue"] { color: #fafafa; }
    </style>
    """,
    unsafe_allow_html=True,
)

client = api(
    api_key=os.getenv("OPENALGO_API_KEY"),
    host=os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"),
)

INTERVAL_DAYS = {"1m": 2, "5m": 15, "15m": 45, "1h": 120, "D": 365}


def badge(label, value, color):
    st.markdown(
        f"""<div style="background-color:{color}22;border:1px solid {color};
        border-radius:6px;padding:8px 12px;margin-bottom:8px;">
        <span style="color:{color};font-weight:600;">{label}: {value}</span></div>""",
        unsafe_allow_html=True,
    )


# --- Sidebar ---
st.sidebar.title("Mechanical Protocol")
symbol = st.sidebar.text_input("Symbol", value="TITAN")
exchange = st.sidebar.selectbox("Exchange", ["NSE", "BSE", "NFO", "NSE_INDEX"], index=0)
interval = st.sidebar.selectbox("Interval", ["1m", "5m", "15m", "1h"], index=2)

st.sidebar.subheader("Backtest Range")
_default_start = datetime.now().date() - timedelta(days=INTERVAL_DAYS.get(interval, 45))
backtest_start = st.sidebar.date_input("Start date", value=_default_start)
backtest_end = st.sidebar.date_input("End date", value=datetime.now().date())

st.sidebar.subheader("Chart Display")
chart_days = st.sidebar.slider("Show last N trading days in chart", min_value=1, max_value=30, value=5)

st.sidebar.subheader("Strategy Filters")
sma_len = st.sidebar.number_input("SMA Length", min_value=1, value=10)
rsi_len = st.sidebar.number_input("RSI Length", min_value=1, value=14)
rsi_threshold = st.sidebar.number_input("RSI Bullish Threshold", min_value=1, max_value=100, value=60)
max_gap_filter = st.sidebar.number_input("Max Gap Down Allowed (%)", min_value=0.0, value=1.0, step=0.1)

st.sidebar.subheader("Session Window (IST)")
entry_start = st.sidebar.time_input("Entry window start", value=dtime(9, 15))
entry_end = st.sidebar.time_input("Entry window end", value=dtime(14, 15))
time_exit = st.sidebar.time_input("Time-based exit", value=dtime(14, 30))

auto_refresh = st.sidebar.checkbox("Auto-refresh", value=False)
refresh_sec = st.sidebar.slider("Refresh interval (sec)", 15, 120, 30, disabled=not auto_refresh)

# --- Fetch + Compute (shared core) ---
if backtest_start >= backtest_end:
    st.error("Backtest start date must be before the end date.")
    st.stop()

try:
    result = run_symbol(
        client, symbol, exchange, interval, backtest_start, backtest_end,
        sma_len, rsi_len, rsi_threshold, max_gap_filter,
        entry_start, entry_end, time_exit,
    )
except Exception as e:
    st.error(f"Error fetching data: {e}")
    st.stop()

df = result["df"]
sma, rsi, psar = result["ind"]["sma"], result["ind"]["rsi"], result["ind"]["psar"]
macd_line, macd_signal, macd_hist = result["ind"]["macd_line"], result["ind"]["macd_signal"], result["ind"]["macd_hist"]
days_sorted = result["days_sorted"]
long_condition = result["long_condition"]
trades = result["trades"]
position, pending = result["state"]["position"], result["state"]["pending"]
signal_locked, trade_taken_today = result["state"]["signal_locked"], result["state"]["trade_taken_today"]

trades_df = pd.DataFrame([{k: v for k, v in t.items() if k not in ("entry_idx", "exit_idx")} for t in trades])

# --- Header / Stats ---
st.title(f"{symbol} - Mechanical Protocol (Long Only)")

last = df.iloc[-1]
# Chart spans multiple days by default, so intraday labels need the date too
# (bare "09:15" would be ambiguous across day boundaries).
fmt = "%m-%d %H:%M" if interval != "D" else "%Y-%m-%d"

try:
    quote = client.quotes(symbol=symbol, exchange=exchange)
    d = quote.get("data", {})
    ltp = d.get("ltp", last["close"])
except Exception:
    ltp = last["close"]

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("LTP", f"{ltp:,.2f}")
c2.metric(f"SMA({sma_len})", f"{sma.iloc[-1]:,.2f}", "Above" if ltp > sma.iloc[-1] else "Below")
c3.metric("Parabolic SAR", f"{psar.iloc[-1]:,.2f}", "Bullish" if ltp > psar.iloc[-1] else "Bearish")
c4.metric(f"RSI({rsi_len})", f"{rsi.iloc[-1]:.1f}")
c5.metric("MACD", "Bullish" if macd_line.iloc[-1] > macd_signal.iloc[-1] else "Bearish")

# --- HUD (mirrors the Pine dashboard table) ---
st.subheader("Live Protocol HUD")
h1, h2 = st.columns(2)
with h1:
    candle_pct = (last["high"] - last["low"]) / last["low"] * 100
    is_green_hud = last["close"] >= last["open"]
    badge("Candle Size", f"{'+' if is_green_hud else '-'}{candle_pct:.2f}%",
          "#22c55e" if is_green_hud else "#ef4444")

    rsi_last = rsi.iloc[-1]
    rsi_color = "#ef4444" if rsi_last >= 70 else ("#22c55e" if rsi_last <= 30 else "#9ca3af")
    badge("RSI", f"{rsi_last:.2f}", rsi_color)

    hist_pos = macd_hist.iloc[-1] >= 0
    badge("MACD Hist", "Above 0" if hist_pos else "Below 0", "#22c55e" if hist_pos else "#ef4444")

with h2:
    macd_bull_hud = macd_line.iloc[-1] > macd_signal.iloc[-1]
    badge("MACD Line", "Bullish (Above Sig)" if macd_bull_hud else "Bearish (Below Sig)",
          "#22c55e" if macd_bull_hud else "#ef4444")

    pdh_last, pdl_last = last["pdh"], last["pdl"]
    if pd.isna(pdh_last):
        badge("Price vs PDH/PDL", "No prior day yet", "#9ca3af")
    elif last["close"] > pdh_last:
        badge("Price vs PDH/PDL", "Above PDH", "#22c55e")
    elif last["close"] < pdl_last:
        badge("Price vs PDH/PDL", "Below PDL", "#ef4444")
    else:
        badge("Price vs PDH/PDL", "Inside Range", "#9ca3af")

    gap_last = last["true_gap_pct"]
    if gap_last > 0:
        badge("True Gap", f"Up +{gap_last:.2f}%", "#22c55e")
    elif gap_last < 0:
        badge("True Gap", f"Down {gap_last:.2f}%", "#ef4444")
    else:
        badge("True Gap", "Filled / None", "#9ca3af")

# --- Current signal status ---
st.subheader("Current Status")
if position is not None:
    unrealized_r = ((last["close"] - position["entry"]) / (position["entry"] - position["sl"])) if position["entry"] != position["sl"] else 0
    st.success(
        f"OPEN POSITION - Entry {position['entry']:.2f} at {position['entry_time']} | "
        f"SL {position['sl']:.2f} | TP {position['tp']:.2f} | Unrealized: {unrealized_r:+.2f}R"
    )
elif pending is not None:
    st.info(
        f"RESTING BUY STOP - at {pending['entry']:.2f}, signal candle at {pending['entry_time']}. "
        f"SL/TP will be set from the fill price once it triggers."
    )
elif signal_locked and trade_taken_today:
    st.write("Today's one trade has already been taken and closed. No further signals today.")
elif signal_locked:
    st.write("Today's signal candle formed but its order was cancelled at day rollover. No further signals today.")
else:
    st.write("No signal candle yet today.")

# --- Chart ---
# Slice to the last N trading days for display -- the simulation above still
# ran over the full backtest range, only the figure itself is windowed. With
# 15m bars, showing the full backtest range by default meant 800+ candles
# squeezed into one chart width (unreadable); this keeps it legible while
# the trade log / stats stay computed over the complete range.
chart_day_set = set(days_sorted[-min(chart_days, len(days_sorted)):])
df_chart = df[df["day"].isin(chart_day_set)]
x_labels_c = df_chart.index.strftime(fmt)
sma_c, rsi_c = sma.loc[df_chart.index], rsi.loc[df_chart.index]
macd_line_c = macd_line.loc[df_chart.index]
macd_signal_c = macd_signal.loc[df_chart.index]
macd_hist_c = macd_hist.loc[df_chart.index]
psar_c = psar.loc[df_chart.index]

n_rows = 4  # price, RSI, MACD, Volume
fig = make_subplots(
    rows=n_rows, cols=1, shared_xaxes=True,
    row_heights=[0.5, 0.17, 0.17, 0.16], vertical_spacing=0.03,
    subplot_titles=["Price + SMA + PSAR + PDH/PDL", f"RSI({rsi_len})", "MACD(12,26,9)", "Volume"],
)

fig.add_trace(go.Candlestick(
    x=x_labels_c, open=df_chart["open"], high=df_chart["high"],
    low=df_chart["low"], close=df_chart["close"],
    name="Price", showlegend=False,
), row=1, col=1)
fig.add_trace(go.Scatter(x=x_labels_c, y=sma_c, name=f"SMA({sma_len})",
                         line=dict(color="#3b82f6", width=1)), row=1, col=1)
fig.add_trace(go.Scatter(x=x_labels_c, y=psar_c, mode="markers", name="Parabolic SAR",
                         marker=dict(symbol="circle", size=3, color="#2962FF")), row=1, col=1)
fig.add_trace(go.Scatter(x=x_labels_c, y=df_chart["pdh"], name="PDH", mode="lines",
                         line=dict(color="#22c55e", width=1, dash="dot", shape="hv")), row=1, col=1)
fig.add_trace(go.Scatter(x=x_labels_c, y=df_chart["pdl"], name="PDL", mode="lines",
                         line=dict(color="#ef4444", width=1, dash="dot", shape="hv")), row=1, col=1)

chart_trades = [t for t in trades if t["Entry Time"].date() in chart_day_set]
if chart_trades:
    entry_pos = [df_chart.index.get_loc(t["Entry Time"]) for t in chart_trades]
    exit_pos = [df_chart.index.get_loc(t["Exit Time"]) for t in chart_trades]
    entry_x = [x_labels_c[p] for p in entry_pos]
    exit_x = [x_labels_c[p] for p in exit_pos]
    entry_y = [t["Entry"] for t in chart_trades]
    exit_y = [t["Exit"] for t in chart_trades]
    exit_colors = ["#22c55e" if t["Reason"] == "Target" else ("#ef4444" if t["Reason"] == "Stop Loss" else "#9ca3af") for t in chart_trades]
    fig.add_trace(go.Scatter(x=entry_x, y=entry_y, mode="markers", name="Entry",
                             marker=dict(symbol="triangle-up", size=11, color="#22c55e")), row=1, col=1)
    fig.add_trace(go.Scatter(x=exit_x, y=exit_y, mode="markers", name="Exit",
                             marker=dict(symbol="triangle-down", size=11, color=exit_colors)), row=1, col=1)

fig.add_trace(go.Scatter(x=x_labels_c, y=rsi_c, name=f"RSI({rsi_len})",
                         line=dict(color="yellow", width=1)), row=2, col=1)
fig.add_hline(y=rsi_threshold, line_dash="dash", line_color="#22c55e", row=2, col=1)
fig.add_hline(y=70, line_dash="dot", line_color="red", opacity=0.4, row=2, col=1)
fig.add_hline(y=30, line_dash="dot", line_color="green", opacity=0.4, row=2, col=1)

fig.add_trace(go.Scatter(x=x_labels_c, y=macd_line_c, name="MACD",
                         line=dict(color="cyan", width=1)), row=3, col=1)
fig.add_trace(go.Scatter(x=x_labels_c, y=macd_signal_c, name="Signal",
                         line=dict(color="orange", width=1)), row=3, col=1)
hist_colors = ["#22c55e" if v >= 0 else "#ef4444" for v in macd_hist_c]
fig.add_trace(go.Bar(x=x_labels_c, y=macd_hist_c, name="Hist",
                     marker_color=hist_colors, opacity=0.6), row=3, col=1)

vol_colors = ["#22c55e" if c >= o else "#ef4444" for c, o in zip(df_chart["close"], df_chart["open"])]
fig.add_trace(go.Bar(x=x_labels_c, y=df_chart["volume"], name="Volume",
                     marker_color=vol_colors, opacity=0.5), row=4, col=1)

fig.update_layout(template="plotly_dark", height=1100, xaxis_rangeslider_visible=False)
for r in range(1, n_rows + 1):
    fig.update_xaxes(type="category", row=r, col=1)
    fig.update_yaxes(side="right", row=r, col=1)

st.caption(f"Chart showing last {len(chart_day_set)} trading day(s) of {len(days_sorted)} in the backtest range.")
st.plotly_chart(fig, use_container_width=True)

# --- Trade Log ---
st.subheader("Simulated Trade Log")
if trades_df.empty:
    st.info("No completed trades in the fetched history window yet. Widen the interval/history or relax filters.")
else:
    wins = (trades_df["R"] > 0).sum()
    total = len(trades_df)
    win_rate = wins / total * 100 if total else 0
    total_r = trades_df["R"].sum()

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Trades", total)
    m2.metric("Win Rate", f"{win_rate:.1f}%")
    m3.metric("Total R", f"{total_r:+.2f}R")

    st.dataframe(trades_df.sort_values("Entry Time", ascending=False), use_container_width=True)

st.caption(
    "This is an approximate replay of the Pine Script v6 strategy: one resting buy-stop "
    "signal per day (never re-defined or cancelled until it fills or the day ends), "
    "SL/TP computed from the actual fill price and the prior bar's low, SL/TP checked "
    "against the same bar's range. It is not a tick-accurate backtest engine."
)

# --- Auto-refresh ---
if auto_refresh:
    import time
    time.sleep(refresh_sec)
    st.rerun()
