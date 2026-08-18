#!/usr/bin/env python
"""
Mechanical Protocol (Long Only) -- Forward-Testing Strategy

Upload this via /python (Add Strategy) and run it with OpenAlgo's Sandbox
/ Analyzer mode ON for paper trading before ever going live -- flip it at
/analyzer or via client.analyzer_toggle(). Sandbox mode uses the exact same
placeorder() call and simulates fills against real market data, so nothing
in this script changes between paper and live; only the toggle does.

Design: on every poll, each symbol's ENTIRE day of candles so far is
replayed through the same one-signal-per-day entry/exit logic used by the
dashboard and scanner (mechanical_protocol_core.run_symbol). That replay is
the single source of truth for both entry and exit -- there is no separate,
hand-rolled SL/TP polling loop that could drift from what the dashboard/
scanner would show for the same symbol at the same moment. A small local
JSON state file per symbol records whether today's entry/exit have already
been sent, so a process restart mid-day (crash, manual stop/start, the
host's holiday/market-close enforcer) does not risk placing a duplicate
order for a signal already acted on -- it only prevents re-sending; it
never fabricates a decision the replay itself hasn't shown yet.

Watchlist: this install's /python "Add Strategy" form has no Parameters/
env-var input (only name, file upload, exchange, schedule) -- so there is
no way to pass a SYMBOLS env var per-upload. The watchlist is instead
DEFAULT_SYMBOLS below, baked into the script: edit that line directly and
re-upload this file to change it. Same "EXCHANGE:SYMBOL" comma/newline
format as the scanner's bulk-add box
(workspace/indicators/scanners/mechanical_protocol_watchlist_scan.py), so
you can copy-paste the same watchlist text between the two tools. A SYMBOLS
env var, if one is ever actually set (e.g. via .env + an app restart, or a
shell that exports it for a standalone run), overrides DEFAULT_SYMBOLS.

QUANTITY/PRODUCT/strategy filters are shared across the whole watchlist in
this version -- per-symbol overrides would need a small config file, which
is a reasonable v2 if you need different position sizing per symbol.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

from openalgo import api

# --- Shared strategy core ---
# The /python host launches strategy subprocesses with cwd = the OpenAlgo
# repo root (see blueprints/python_strategy.py), regardless of whether this
# file lives here or gets copied into strategies/scripts/ on upload -- so a
# cwd-relative path (not a __file__-relative one) is what actually works
# after upload.
CORE_DIR = Path(os.getenv("MECHANICAL_PROTOCOL_CORE_DIR", "workspace/indicators")).resolve()
sys.path.insert(0, str(CORE_DIR))
from mechanical_protocol_core import run_symbol  # noqa: E402

# --- Watchlist ---
# NOTE: OpenAlgo's /python "Add Strategy" form in this install has no
# Parameters/env-var input -- only name, file upload, exchange, and
# schedule. So there is no way to set a SYMBOLS env var per-upload.
# EDIT THE LINE BELOW DIRECTLY and re-upload this file to change the
# watchlist -- the SYMBOLS env var is still read as an override (highest
# priority) in case you ever run this standalone, from a shell that sets
# it, or a future version of the UI adds a params section.
DEFAULT_SYMBOLS = (
    "NSE:INOXWIND,NSE:MOTILALOFS,NSE:OBEROIRLTY,NSE:SUZLON,NSE:KFINTECH,"
    "NSE:BHEL,NSE:GODREJPROP,NSE:ASHOKLEY,NSE:NATIONALUM,NSE:KEI,"
    "NSE:NAM-INDIA,NSE:LTF,NSE:UNOMINDA,NSE:SAIL,NSE:JINDALSTEL,"
    "NSE:MFSL,NSE:ANGELONE,NSE:PGEL,NSE:PHOENIXLTD,NSE:HINDALCO"
)

SYMBOL = os.getenv("SYMBOL", "TITAN")
EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
SYMBOLS_RAW = os.getenv("SYMBOLS", "").strip()

INTERVAL = os.getenv("INTERVAL", "15m")
QUANTITY = int(os.getenv("QUANTITY", "1"))
PRODUCT = os.getenv("PRODUCT", "MIS")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "45"))

SMA_LEN = int(os.getenv("SMA_LEN", "10"))
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
RSI_THRESHOLD = int(os.getenv("RSI_THRESHOLD", "60"))
MAX_GAP_FILTER = float(os.getenv("MAX_GAP_FILTER", "1.0"))
ENTRY_START = dtime(*map(int, os.getenv("ENTRY_START", "9:15").split(":")))
ENTRY_END = dtime(*map(int, os.getenv("ENTRY_END", "14:15").split(":")))
TIME_EXIT = dtime(*map(int, os.getenv("TIME_EXIT", "14:30").split(":")))
RR_RATIO = float(os.getenv("RR_RATIO", "1.0"))

STRATEGY_NAME = os.getenv("STRATEGY_NAME", "MechanicalProtocol")
STRATEGY_ID = os.getenv("STRATEGY_ID", "mechanical_protocol_local")

API_KEY = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")

if not API_KEY:
    print("Error: OPENALGO_API_KEY environment variable not set")
    sys.exit(1)

client = api(api_key=API_KEY, host=API_HOST)


def parse_watchlist(text, default_exchange):
    """
    Same format as the scanner's bulk-add box: comma/newline separated
    "EXCHANGE:SYMBOL" entries, or a bare "SYMBOL" (falls back to
    default_exchange). Returns a de-duplicated list of (exchange, symbol).
    """
    seen, entries = set(), []
    for raw in re.split(r"[,\n]", text):
        raw = raw.strip()
        if not raw:
            continue
        if ":" in raw:
            ex, sym = raw.split(":", 1)
            ex, sym = ex.strip().upper(), sym.strip().upper()
        else:
            ex, sym = default_exchange, raw.upper()
        if sym and (ex, sym) not in seen:
            seen.add((ex, sym))
            entries.append((ex, sym))
    return entries


if SYMBOLS_RAW:
    # Explicit SYMBOLS env var (set via .env + app restart, or a shell that
    # exports it for a standalone run) -- highest priority.
    WATCHLIST = parse_watchlist(SYMBOLS_RAW, EXCHANGE)
elif "SYMBOL" in os.environ:
    # Explicit single-symbol env var override, same precedence rule.
    WATCHLIST = [(EXCHANGE, SYMBOL)]
else:
    # No env override present at all -- baked-in default watchlist, since
    # this install's /python upload form has no way to set env vars per
    # strategy. Edit DEFAULT_SYMBOLS above and re-upload to change it.
    WATCHLIST = parse_watchlist(DEFAULT_SYMBOLS, EXCHANGE)

STATE_DIR = Path("strategies") / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def state_file(ex, sym):
    return STATE_DIR / f"{STRATEGY_ID}_{sym}_{ex}.json"


def load_state(ex, sym):
    f = state_file(ex, sym)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def save_state(ex, sym, state):
    state_file(ex, sym).write_text(json.dumps(state, default=str))


def process_symbol(ex, sym):
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)

    result = run_symbol(
        client, sym, ex, INTERVAL, start_date, end_date,
        SMA_LEN, RSI_LEN, RSI_THRESHOLD, MAX_GAP_FILTER,
        ENTRY_START, ENTRY_END, TIME_EXIT, RR_RATIO,
    )
    df = result["df"]
    last_day = df.index[-1].date()
    trades = result["trades"]
    sm = result["state"]
    position = sm["position"]
    last_close = df["close"].iloc[-1]

    todays_trade = None
    if trades and trades[-1]["Entry Time"].date() == last_day:
        todays_trade = trades[-1]

    state = load_state(ex, sym)
    if state.get("date") != str(last_day):
        # New trading day (per the replay's own day boundary) -- reset the
        # local "have we sent an order" guard.
        state = {"date": str(last_day), "order_placed": False, "order_closed": False}

    status = (
        "OPEN POSITION" if position is not None else
        "TRADE DONE TODAY" if todays_trade is not None else
        "RESTING ORDER" if sm["pending"] is not None else
        "SIGNAL CANCELLED (EOD)" if sm["signal_locked"] else
        "NO SIGNAL"
    )
    print(f"[{datetime.now():%H:%M:%S}] {ex}:{sym} LTP={last_close:.2f} status={status} "
          f"order_placed={state.get('order_placed')} order_closed={state.get('order_closed')}")

    # ENTRY: the replay shows a fill happened today (either still open, or
    # already closed by the time we polled) and we haven't sent the order yet.
    if not state.get("order_placed") and (position is not None or todays_trade is not None):
        entry_price = position["entry"] if position is not None else todays_trade["Entry"]
        response = client.placeorder(
            strategy=STRATEGY_NAME, symbol=sym, exchange=ex,
            action="BUY", price_type="MARKET", product=PRODUCT, quantity=QUANTITY,
        )
        print(f"[{datetime.now():%H:%M:%S}] {ex}:{sym} BUY placed (replay entry {entry_price}): {response}")
        state["order_placed"] = True
        save_state(ex, sym, state)

    # EXIT: the replay shows today's trade has already closed (SL, TP, or
    # the time exit -- whichever the replay hit first), and we entered but
    # haven't exited yet.
    if state.get("order_placed") and not state.get("order_closed") and todays_trade is not None:
        response = client.placeorder(
            strategy=STRATEGY_NAME, symbol=sym, exchange=ex,
            action="SELL", price_type="MARKET", product=PRODUCT, quantity=QUANTITY,
        )
        print(f"[{datetime.now():%H:%M:%S}] {ex}:{sym} SELL placed "
              f"({todays_trade['Reason']}, replay exit {todays_trade['Exit']}): {response}")
        state["order_closed"] = True
        save_state(ex, sym, state)


def main():
    print(f"Mechanical Protocol forward-test started at {datetime.now()}")
    print(f"Watchlist ({len(WATCHLIST)}): {', '.join(f'{ex}:{sym}' for ex, sym in WATCHLIST)}")
    print(f"Interval: {INTERVAL} | Qty: {QUANTITY} | Product: {PRODUCT} | Poll: {POLL_SECONDS}s")
    print(f"Filters: RSI>{RSI_THRESHOLD} | RR={RR_RATIO}:1 | Session {ENTRY_START}-{ENTRY_END} | Time exit {TIME_EXIT}")
    print("Reminder: confirm Sandbox/Analyzer mode is ON before treating fills as paper trades.")

    while True:
        cycle_start = time.time()
        for ex, sym in WATCHLIST:
            try:
                process_symbol(ex, sym)
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] {ex}:{sym} error: {e}")

        elapsed = time.time() - cycle_start
        if len(WATCHLIST) > 1:
            print(f"[{datetime.now():%H:%M:%S}] Cycle done for {len(WATCHLIST)} symbols in {elapsed:.1f}s")

        try:
            time.sleep(max(1.0, POLL_SECONDS - elapsed))
        except KeyboardInterrupt:
            print("Strategy stopped")
            break


if __name__ == "__main__":
    main()
