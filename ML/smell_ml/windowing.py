"""Stage 4: sliding windows over cycles, per sensor.

Regression needs the dynamics (rise/decay rate), not a single snapshot, so
windows of N consecutive cycles are built per sensor. Classification is
static-fingerprint-driven and does not go through this — see
`build_dataset.py`, which emits cycle-level rows for classification directly
from the cleaned grid, no windowing needed.
"""
from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

N_STEPS = 10
# cycles per window. Started at 5 ("start small" per the plan); a sweep over
# 3/5/10/15 (ML/EXPERIMENTS.md) showed regressor R^2 monotonically worse with
# larger windows (0.868/0.859/0.837/0.817) -- the strength changes
# faster than a big window stays local to. 3 was the smallest tried and won;
# untested below that.
DEFAULT_WINDOW = 3


def make_windows(
    cycle_df: pd.DataFrame,
    run_id: str,
    sensor_id: int,
    window: int = DEFAULT_WINDOW,
) -> pd.DataFrame:
    """`cycle_df` must be sorted by cycle index and contain step_0..step_9,
    y_conc, odour. One output row per window; label = the window's LAST
    cycle's y_conc — matches deployment, where the model only ever knows the
    past, not the future."""
    step_cols = [f"step_{i}" for i in range(N_STEPS)]
    n = len(cycle_df)
    if n < window:
        return pd.DataFrame()

    values = cycle_df[step_cols].to_numpy()
    cycle_means = values.mean(axis=1)
    rows: List[dict] = []
    for end in range(window - 1, n):
        start = end - window + 1
        stacked = values[start:end + 1]
        rows.append({
            "run_id": run_id,
            "sensor_id": sensor_id,
            "window_end_cycle": cycle_df.index[end],
            "y_conc": cycle_df["y_conc"].iloc[end],
            "y_class": cycle_df["odour"].iloc[end],
            "X_window": stacked,  # shape (window, N_STEPS)
            # explicit trend features (plan Stage 4.3) for non-sequential models
            "trend_slope": np.polyfit(np.arange(window), cycle_means[start:end + 1], 1)[0],
            "trend_diff_last": cycle_means[end] - cycle_means[end - 1],
        })
    return pd.DataFrame(rows)


def stack_windows(window_df: pd.DataFrame) -> np.ndarray:
    """[n_windows, N, 10] float32 array from a make_windows() DataFrame."""
    return np.stack(window_df["X_window"].to_numpy()).astype(np.float32)
