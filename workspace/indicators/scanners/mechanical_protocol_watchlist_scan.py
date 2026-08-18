"""
Mechanical Protocol -- Watchlist Scanner
Runs the same entry logic as dashboards/mechanical_protocol/app.py across a
user-editable watchlist and flags which symbols have a signal candle today
(pending order, filled position, or already-completed trade).

Run: streamlit run app.py
Open: http://localhost:8501
"""
import os
import re
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import find_dotenv, load_dotenv
from openalgo import api

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # workspace/indicators/
from mechanical_protocol_core import run_symbol  # noqa: E402

load_dotenv(find_dotenv(), override=True)

st.set_page_config(page_title="Mechanical Protocol Scanner - OpenAlgo", layout="wide")
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

NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BPCL", "BHARTIARTL",
    "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SUNPHARMA",
    "TCS", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TECHM",
    "TITAN", "ULTRACEMCO", "UPL", "WIPRO",
]
BANKNIFTY = [
    "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "SBIN",
    "INDUSINDBK", "BANKBARODA", "FEDERALBNK", "PNB", "IDFCFIRSTB",
    "BANDHANBNK", "AUBANK",
]

STATUS_ORDER = {
    "OPEN POSITION": 0, "RESTING ORDER": 1, "TRADE DONE TODAY": 2,
    "SIGNAL CANCELLED (EOD)": 3, "NO SIGNAL": 4,
}
STATUS_COLOR = {
    "OPEN POSITION": "#22c55e", "RESTING ORDER": "#3b82f6", "TRADE DONE TODAY": "#9ca3af",
    "SIGNAL CANCELLED (EOD)": "#9ca3af", "NO SIGNAL": "#6b7280",
}

def parse_entries(text, default_exchange):
    """
    Parse comma/newline separated entries into canonical "EXCHANGE:SYMBOL"
    strings. Accepts either "NSE:RELIANCE" (exchange prefix, TradingView
    style) or a bare "RELIANCE" (falls back to default_exchange).
    """
    entries = []
    for raw in re.split(r"[,\n]", text):
        raw = raw.strip()
        if not raw:
            continue
        if ":" in raw:
            ex, sym = raw.split(":", 1)
            ex, sym = ex.strip().upper(), sym.strip().upper()
        else:
            ex, sym = default_exchange, raw.upper()
        if sym:
            entries.append(f"{ex}:{sym}")
    return entries


if "watchlist" not in st.session_state:
    st.session_state.watchlist = []

# --- Sidebar ---
st.sidebar.title("Mechanical Protocol Scanner")
exchange = st.sidebar.selectbox(
    "Default exchange", ["NSE", "BSE", "NFO", "NSE_INDEX"], index=0,
    help="Used for entries typed without an EXCHANGE: prefix (presets, single add).",
)
interval = st.sidebar.selectbox("Interval", ["1m", "5m", "15m", "1h"], index=2)
lookback_days = st.sidebar.number_input(
    "History lookback (days)", min_value=5, value=INTERVAL_DAYS.get(interval, 45),
    help="Needs at least 2 trading days for PDH/PDL, plus warmup for SMA/RSI/MACD.",
)

st.sidebar.subheader("Strategy Filters")
sma_len = st.sidebar.number_input("SMA Length", min_value=1, value=10)
rsi_len = st.sidebar.number_input("RSI Length", min_value=1, value=14)
rsi_threshold = st.sidebar.number_input("RSI Bullish Threshold", min_value=1, max_value=100, value=60)
max_gap_filter = st.sidebar.number_input("Max Gap Down Allowed (%)", min_value=0.0, value=1.0, step=0.1)

st.sidebar.subheader("Session Window (IST)")
entry_start = st.sidebar.time_input("Entry window start", value=dtime(9, 15))
entry_end = st.sidebar.time_input("Entry window end", value=dtime(14, 15))
time_exit = st.sidebar.time_input("Time-based exit", value=dtime(14, 30))

# --- Watchlist editor ---
st.title("Mechanical Protocol - Watchlist Scanner")
st.write("Which symbols currently have a signal candle -- pending order, open position, or already-traded today.")

preset_col, add_col = st.columns([1, 2])
with preset_col:
    preset = st.selectbox("Load a preset", ["-- choose --", "NIFTY 50", "BANK NIFTY"])
    if st.button("Load preset"):
        preset_list = NIFTY50 if preset == "NIFTY 50" else BANKNIFTY if preset == "BANK NIFTY" else []
        for s in preset_list:
            entry = f"NSE:{s}"
            if entry not in st.session_state.watchlist:
                st.session_state.watchlist.append(entry)
with add_col:
    with st.form("add_symbol_form", clear_on_submit=True):
        new_sym_col, submit_col = st.columns([3, 1])
        new_sym = new_sym_col.text_input(
            "Add a symbol", placeholder="e.g. RELIANCE or NSE:RELIANCE", label_visibility="collapsed",
        )
        submitted = submit_col.form_submit_button("Add")
        if submitted and new_sym.strip():
            for entry in parse_entries(new_sym, exchange):
                if entry not in st.session_state.watchlist:
                    st.session_state.watchlist.append(entry)

with st.expander("Bulk add (paste a comma or newline separated list, e.g. NSE:INOXWIND,NSE:SUZLON,...)"):
    with st.form("bulk_add_form", clear_on_submit=True):
        bulk_text = st.text_area(
            "Paste symbols", placeholder="NSE:INOXWIND,NSE:MOTILALOFS,NSE:OBEROIRLTY,...",
            label_visibility="collapsed", height=100,
        )
        bulk_submitted = st.form_submit_button("Add pasted list")
        if bulk_submitted and bulk_text.strip():
            added = 0
            for entry in parse_entries(bulk_text, exchange):
                if entry not in st.session_state.watchlist:
                    st.session_state.watchlist.append(entry)
                    added += 1
            st.toast(f"Added {added} symbol(s) to the watchlist.")

st.session_state.watchlist = st.multiselect(
    "Current watchlist (deselect to remove)",
    options=sorted(set(st.session_state.watchlist)),
    default=st.session_state.watchlist,
)

if st.button("Clear watchlist"):
    st.session_state.watchlist = []
    st.rerun()

watchlist = st.session_state.watchlist
run_clicked = st.button("Run Scan", type="primary", disabled=not watchlist)

# --- Scan ---
if run_clicked:
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=lookback_days)
    parsed_watchlist = [tuple(entry.split(":", 1)) for entry in watchlist]  # (exchange, symbol)

    try:
        mq = client.multiquotes(symbols=[{"symbol": sym, "exchange": ex} for ex, sym in parsed_watchlist])
        ltp_map = {q["symbol"]: q.get("ltp") for q in mq.get("data", [])} if mq.get("status") == "success" else {}
    except Exception:
        ltp_map = {}

    results, skipped = [], []
    progress = st.progress(0.0, text="Starting scan...")
    for i, (ex, sym) in enumerate(parsed_watchlist):
        progress.progress((i + 1) / len(parsed_watchlist), text=f"Scanning {ex}:{sym} ({i + 1}/{len(parsed_watchlist)})")
        try:
            result = run_symbol(
                client, sym, ex, interval, start_date, end_date,
                sma_len, rsi_len, rsi_threshold, max_gap_filter,
                entry_start, entry_end, time_exit,
            )
        except Exception as e:
            skipped.append({"Symbol": f"{ex}:{sym}", "Reason": str(e)})
            continue

        df = result["df"]
        last = df.iloc[-1]
        last_day = df.index[-1].date()
        ind = result["ind"]
        trades = result["trades"]
        state = result["state"]
        position, pending = state["position"], state["pending"]
        signal_locked, trade_taken_today = state["signal_locked"], state["trade_taken_today"]

        entry = sl = tp = exit_price = reason = entry_candle = None
        if position is not None:
            status = "OPEN POSITION"
            entry, sl, tp = position["entry"], position["sl"], position["tp"]
            entry_candle = position["entry_time"]
        elif pending is not None:
            status = "RESTING ORDER"
            entry = pending["entry"]
            entry_candle = pending["entry_time"]
        elif signal_locked and trade_taken_today:
            status = "TRADE DONE TODAY"
            # position/pending are already cleared once a trade closes -- the
            # entry/SL/TP for today's (now-closed) trade only live in the trade log.
            if trades and trades[-1]["Entry Time"].date() == last_day:
                t = trades[-1]
                entry, sl, tp, exit_price, reason = t["Entry"], t["SL"], t["TP"], t["Exit"], t["Reason"]
                entry_candle = t["Entry Time"]
        elif signal_locked:
            status = "SIGNAL CANCELLED (EOD)"
        else:
            status = "NO SIGNAL"

        results.append({
            "Symbol": sym,
            "Exchange": ex,
            "LTP": ltp_map.get(sym, round(last["close"], 2)),
            "Status": status,
            "Entry Candle": entry_candle,
            "Entry": round(entry, 2) if entry is not None else None,
            "SL": round(sl, 2) if sl is not None else None,
            "TP": round(tp, 2) if tp is not None else None,
            "Exit": round(exit_price, 2) if exit_price is not None else None,
            "Reason": reason,
            "RSI": round(ind["rsi"].iloc[-1], 1),
            "Above SMA": bool(last["close"] > ind["sma"].iloc[-1]),
            "PSAR Bullish": bool(last["close"] > ind["psar"].iloc[-1]),
            "MACD Bullish": bool(ind["macd_line"].iloc[-1] > ind["macd_signal"].iloc[-1]),
            "Last Bar": df.index[-1],
        })
    progress.empty()

    results_df = pd.DataFrame(results)
    if not results_df.empty:
        results_df["_sort"] = results_df["Status"].map(STATUS_ORDER)
        results_df = results_df.sort_values(["_sort", "Symbol"]).drop(columns="_sort").reset_index(drop=True)

    st.session_state.scan_results = results_df
    st.session_state.scan_skipped = skipped
    st.session_state.scan_requested = len(watchlist)

# --- Results ---
if "scan_results" in st.session_state:
    results_df = st.session_state.scan_results
    skipped = st.session_state.scan_skipped
    requested = st.session_state.scan_requested

    if results_df.empty:
        st.warning("No symbols returned usable data. Check the skipped list below.")
    else:
        active = results_df[results_df["Status"].isin(["OPEN POSITION", "RESTING ORDER"])]
        if len(active):
            st.success(f"{len(active)} symbol(s) with an active setup right now.")
        else:
            st.info("No active setups right now across the scanned watchlist.")

        def _style_status(val):
            color = STATUS_COLOR.get(val, "#6b7280")
            return f"color: {color}; font-weight: 600"

        st.dataframe(
            results_df.style.map(_style_status, subset=["Status"]),
            use_container_width=True,
        )
        st.download_button(
            "Download CSV", results_df.to_csv(index=False),
            file_name=f"mechanical_protocol_scan_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        )

    st.caption(
        f"Scanned {len(results_df)}/{requested} requested symbols"
        + (f", {len(skipped)} skipped." if skipped else ".")
    )
    if skipped:
        with st.expander(f"Skipped symbols ({len(skipped)})"):
            st.dataframe(pd.DataFrame(skipped), use_container_width=True)

st.caption(
    "Status reflects each symbol's own end-of-data state from the same one-signal-per-day "
    "logic as the single-symbol dashboard: a RESTING ORDER or OPEN POSITION is live right "
    "now; TRADE DONE TODAY / SIGNAL CANCELLED (EOD) / NO SIGNAL are informational only."
)
