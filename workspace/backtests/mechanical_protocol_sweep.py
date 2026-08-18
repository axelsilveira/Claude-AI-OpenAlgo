"""
Mechanical Protocol -- Parameter Sweep (self-improving backtest loop, v2)

Fetches each symbol's historical data ONCE, then cheaply re-runs the
indicator/entry/simulation math (no re-fetching) across a grid of
entry AND exit parameters, ranking every combination three ways: highest
total R, lowest drawdown, and a combined return-to-pain ratio -- profit and
drawdown often trade off against each other, so no single ranking tells the
whole story.

v1 swept sma_len x rsi_threshold x max_gap_filter and found sma_len/
max_gap_filter didn't matter at all in the tested window (a breakout above
yesterday's high is almost always above its own short SMA too, and this
symbol/period had no gap-downs exceeding 0.5%) -- so both are fixed here at
a sensible default, freeing the grid budget for a finer RSI sweep plus two
exit-side dimensions the v1 sweep couldn't touch at all: reward:risk ratio
(TP was hardcoded 1:1) and the time-based exit clock.

Run: uv run --group analysis python workspace/backtests/mechanical_protocol_sweep.py
Output: workspace/backtests/output/mechanical_protocol_sweep_<symbol>.csv
"""
import os
import sys
from datetime import datetime, timedelta, time as dtime
from itertools import product
from pathlib import Path

import pandas as pd
from dotenv import find_dotenv, load_dotenv
from openalgo import api

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "indicators"))
from mechanical_protocol_core import (  # noqa: E402
    compute_indicators, compute_long_condition, compute_metrics,
    compute_pdh_pdl_gap, fetch_data, simulate_trades,
)

load_dotenv(find_dotenv(), override=True)

# --- Fixed run settings ---
SYMBOL = os.getenv("SYMBOL", "TITAN")
EXCHANGE = os.getenv("EXCHANGE", "NSE")
INTERVAL = os.getenv("INTERVAL", "15m")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "45"))
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
ENTRY_START = dtime(9, 15)
ENTRY_END = dtime(14, 15)

# v1 finding: these didn't move the result at all in the tested range for
# this symbol/period -- fixed here, not swept, to spend the grid budget on
# entry (RSI) and exit (RR ratio, time exit) instead.
SMA_LEN = int(os.getenv("SMA_LEN", "10"))
MAX_GAP_FILTER = float(os.getenv("MAX_GAP_FILTER", "1.0"))

# --- Parameter grid ---
# RSI: finer resolution than v1's [50,55,60,65,70], focused on where v1
# showed the interesting transition (55 -> 60 flipped from best to worst).
RSI_THRESHOLD_GRID = [45, 48, 50, 52, 54, 56, 58, 60, 62, 64, 66, 68, 70]
# Exit-side: v1 couldn't touch these at all (TP was hardcoded 1:1).
RR_RATIO_GRID = [1.0, 1.5, 2.0, 2.5, 3.0]
TIME_EXIT_GRID = [dtime(14, 0), dtime(14, 15), dtime(14, 30), dtime(15, 0), dtime(15, 15)]

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def main():
    client = api(
        api_key=os.getenv("OPENALGO_API_KEY"),
        host=os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"),
    )

    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)

    print(f"Fetching {SYMBOL} ({EXCHANGE}, {INTERVAL}) from {start_date} to {end_date}...")
    df = fetch_data(client, SYMBOL, EXCHANGE, INTERVAL, start_date, end_date)
    df = df.copy()
    df["day"] = df.index.date

    days_sorted, pdh_by_day, pdl_by_day, true_gap_by_day = compute_pdh_pdl_gap(df)
    df["pdh"] = df["day"].map(pdh_by_day)
    df["pdl"] = df["day"].map(pdl_by_day)
    df["true_gap_pct"] = df["day"].map(true_gap_by_day)

    # SMA/RSI-length fixed this round -- indicators computed once, reused
    # across the whole grid (only rsi_threshold/rr_ratio/time_exit vary).
    ind = compute_indicators(df, SMA_LEN, RSI_LEN)

    grid = list(product(RSI_THRESHOLD_GRID, RR_RATIO_GRID, TIME_EXIT_GRID))
    print(f"Sweeping {len(grid)} combinations over {len(days_sorted)} trading days...")

    # long_condition only depends on rsi_threshold (sma/gap-filter fixed) --
    # cache per rsi_threshold so it isn't recomputed for every rr/time-exit pair.
    long_condition_cache = {}
    results = []

    for rsi_threshold, rr_ratio, time_exit in grid:
        if rsi_threshold not in long_condition_cache:
            long_condition_cache[rsi_threshold] = compute_long_condition(
                df, ind, ENTRY_START, ENTRY_END, rsi_threshold, MAX_GAP_FILTER,
            )
        long_condition = long_condition_cache[rsi_threshold]

        trades, _state = simulate_trades(df, long_condition, time_exit, rr_ratio)
        metrics = compute_metrics(trades)

        results.append({
            "rsi_threshold": rsi_threshold, "rr_ratio": rr_ratio,
            "time_exit": time_exit.strftime("%H:%M"),
            **metrics,
        })

    results_df = pd.DataFrame(results)
    # Combined "return per unit of pain" score -- rewards high total_r that
    # didn't require a deep drawdown to get there. Combos with zero
    # drawdown (e.g. zero or one trade) are excluded from this ranking so
    # they don't produce a divide-by-zero "infinite" score that looks best
    # but is really just an under-traded, low-confidence result.
    results_df["r_per_drawdown"] = results_df.apply(
        lambda row: row["total_r"] / row["max_drawdown_r"] if row["max_drawdown_r"] > 0 else float("nan"),
        axis=1,
    )

    out_path = OUTPUT_DIR / f"mechanical_protocol_sweep_{SYMBOL}.csv"
    results_df.to_csv(out_path, index=False)

    pd.set_option("display.width", 120)
    print(f"\nTop 10 by total_r (max profit):")
    print(results_df.sort_values("total_r", ascending=False).head(10).to_string(index=False))

    print(f"\nTop 10 by lowest max_drawdown_r among profitable combos (min pain):")
    profitable = results_df[results_df["total_r"] > 0]
    print(profitable.sort_values("max_drawdown_r", ascending=True).head(10).to_string(index=False))

    print(f"\nTop 10 by r_per_drawdown (best balance of profit vs. pain):")
    print(results_df.sort_values("r_per_drawdown", ascending=False).head(10).to_string(index=False))

    print(f"\nFull results ({len(results_df)} combinations) saved to: {out_path}")


if __name__ == "__main__":
    main()
