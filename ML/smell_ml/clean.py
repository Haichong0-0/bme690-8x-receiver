"""Stage 1: signal cleaning — impute, (optionally) low-pass filter, log-transform.

Deviation from the plan's literal ordering, and why: the plan says Stage 1
runs on "the whole run, continuous... before any segmentation." Taken
completely literally that would mean filtering one raw row-stream per sensor
that interleaves all 10 heater-step temperatures (100/200/320 degC) back to
back — but those steps have wildly different absolute resistance baselines,
so a single low-pass filter across that interleaved stream would smear across
temperature regimes rather than clean anything.

"Per sensor channel" (the imputation instruction's own words) is read here as
per (sensor, heater-step) — i.e. the same physical measurement repeated once
per ~10.8s cycle — which is the actual continuous, comparable-units channel.
`scanning_cycle_index` (already in the raw CSV) gives cycle ordering for free,
so building the (cycle x step) grid first is just bookkeeping, not the
phase/run segmentation Stage 2b is about. Each of the grid's 10 columns is
then cleaned as its own continuous series across cycles.

Low-pass filtering is ON by default (`apply_lowpass=True`), after going back
and forth on this. A cutoff-fraction sweep (0.01, 0.02, 0.05, 0.1, 0.2,
unfiltered) plotted against the raw signal on a real run showed the raw
curve nearly identical to `cutoff_fraction=0.05` and everything above it —
the mean-across-10-steps signal is inherently smooth, there wasn't much
noise for the filter to remove — so filtering was turned off for a while.
It was turned back on after the classifier/regressor algorithm sweep
(ML/EXPERIMENTS.md) showed the filter measurably helps downstream: LORO
classifier accuracy improved with it on for both RandomForest (0.581 -> 0.611)
and the eventual best classifier, logistic regression (0.746 -> 0.755), with
no regression-side cost either way. Going *lower* than `cutoff_fraction=0.05`
still visibly hurts (clips the true peak, rounds off transitions) — that
part of the original finding stands, it just turned out the filter's
removing enough classifier-relevant noise to be worth keeping at 0.05.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt

N_STEPS = 10
DEFAULT_CUTOFF_FRACTION = 0.05  # of fs, when apply_lowpass=True; see module docstring


@dataclass
class CleanReport:
    n_imputed: int
    n_total: int
    fs_hz: float
    cutoff_hz: Optional[float]
    order: int
    log_before_filter: bool
    apply_lowpass: bool

    @property
    def imputed_rate(self) -> float:
        return self.n_imputed / self.n_total if self.n_total else 0.0


def impute_grid(grid: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Mean of preceding + succeeding neighbour cycle, per step column."""
    step_cols = [f"step_{i}" for i in range(N_STEPS)]
    out = grid.copy()
    n_missing = int(out[step_cols].isna().to_numpy().sum())
    for col in step_cols:
        s = out[col]
        isna = s.isna()
        if not isna.any():
            continue
        prev, nxt = s.ffill(), s.bfill()
        s = s.copy()
        s[isna] = (prev[isna] + nxt[isna]) / 2.0
        # start/end-of-run gaps have no neighbour on one side; nearest-valid fallback
        out[col] = s.ffill().bfill()
    return out, n_missing


def estimate_sample_rate_hz(cycle_timestamps: pd.Series) -> float:
    dt = cycle_timestamps.diff().dt.total_seconds().dropna()
    median_dt = dt.median()
    if not median_dt or median_dt <= 0:
        raise ValueError("non-positive median cycle interval; check cycle_timestamp column")
    return 1.0 / median_dt


def lowpass_filter(values: np.ndarray, fs_hz: float, cutoff_hz: float, order: int = 1) -> np.ndarray:
    nyquist = fs_hz / 2.0
    if cutoff_hz >= nyquist:
        raise ValueError(
            f"cutoff {cutoff_hz:.5f} Hz >= Nyquist {nyquist:.5f} Hz for fs={fs_hz:.5f} Hz. "
            "This project's real per-cycle sample rate (~1 sample per ~10.8s heater cycle, "
            "i.e. fs ~0.09 Hz) is nothing like the honey paper's 50 Hz assumption — the "
            "cutoff must be re-derived from the actual fs, not copied from the paper. "
            "See ML/README.md 'open parameters'."
        )
    b, a = butter(order, cutoff_hz / nyquist, btype="low")
    # padlen must be < signal length; short runs (few cycles) can't be filtfilt'd
    padlen = 3 * (max(len(a), len(b)) - 1)
    if len(values) <= padlen:
        return values.copy()
    return filtfilt(b, a, values)


def clean_sensor_grid(
    grid: pd.DataFrame,
    apply_lowpass: bool = True,
    cutoff_hz: float | None = None,
    order: int = 1,
    log_before_filter: bool = True,
) -> tuple[pd.DataFrame, CleanReport]:
    """`grid` = raw_grid() output for one (run, sensor). Returns a grid with
    the same step_* columns replaced by cleaned log-resistance, plus a report
    for the pipeline's diagnostics output.

    `apply_lowpass=True` (default): Butterworth low-pass filter (see module
    docstring for why). Pass `apply_lowpass=False` for impute + log-transform
    only, no filtering."""
    step_cols = [f"step_{i}" for i in range(N_STEPS)]
    imputed, n_missing = impute_grid(grid)
    fs_hz = estimate_sample_rate_hz(imputed["cycle_timestamp"])

    out = imputed.copy()
    if apply_lowpass:
        if cutoff_hz is None:
            cutoff_hz = fs_hz * DEFAULT_CUTOFF_FRACTION
        for col in step_cols:
            raw = imputed[col].to_numpy(dtype=float)
            if log_before_filter:
                signal = lowpass_filter(np.log(raw), fs_hz, cutoff_hz, order)
            else:
                signal = np.log(lowpass_filter(raw, fs_hz, cutoff_hz, order))
            out[col] = signal
    else:
        cutoff_hz = None
        for col in step_cols:
            out[col] = np.log(imputed[col].to_numpy(dtype=float))

    report = CleanReport(
        n_imputed=n_missing,
        n_total=len(grid) * N_STEPS,
        fs_hz=fs_hz,
        cutoff_hz=cutoff_hz,
        order=order,
        log_before_filter=log_before_filter,
        apply_lowpass=apply_lowpass,
    )
    return out, report
