"""Stage 2c: spline-align heater steps within a cycle to fixed relative time
offsets — optional, per the plan ("skip only if you've confirmed on real data
that offsets are negligible").

`decide_alignment_needed` makes that check data-driven rather than assumed,
using an absolute-seconds threshold: an earlier version divided each step's
jitter (std of elapsed-time-since-cycle-start) by the *average* step spacing
across the whole cycle, which is meaningless when spacing itself varies by
10x between transitions (observed: ~0.2s between steps 0-1, ~3.3s between
steps 2-3) — it always tripped "needs alignment" even when the real jitter
(std ~0.05-0.09s on a ~8-11s cycle, confirmed on real data) is negligible.

Measured on the real captures in data/raw/: worst-case std is ~0.09s
against an absolute threshold of 0.3s (~3% of a cycle) -> alignment is NOT
triggered for any of them. See ML/README.md.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.interpolate import interp1d

from .grid import true_cycle_index

N_STEPS = 10
JITTER_ABS_THRESHOLD_S = 0.3  # ~3% of the observed ~8-11s cycle length


def decide_alignment_needed(jitter: pd.DataFrame) -> tuple[bool, float]:
    """`jitter` = grid.step_offset_jitter() output. Returns (needed, worst_std_s)."""
    worst_std = jitter["std"].max()
    if np.isnan(worst_std):
        return False, float("nan")
    return bool(worst_std > JITTER_ABS_THRESHOLD_S), float(worst_std)


def spline_align_sensor(df_sensor: pd.DataFrame, value_col: str = "resistance_gassensor") -> pd.DataFrame:
    """Resample each cycle's steps at 10 fixed relative offsets (the mean
    observed offsets across the whole run) via per-cycle linear interpolation
    in log-space. Only meaningful when `decide_alignment_needed` says jitter
    is non-trivial.

    Linear, not cubic: adjacent steps can swing by orders of magnitude
    (different heater temperatures), and an exact-fit cubic spline through
    points like that oscillates wildly between knots (Runge's phenomenon) and
    produces negative "resistances" — confirmed on real data. Interpolating
    in log-space and linearly is bounded between the two adjacent real
    samples, so it can't do that; it's also all this correction needs, since
    it's only nudging sub-second timing offsets, not modelling the curve
    shape."""
    df_sensor = true_cycle_index(df_sensor)
    cycle_start = df_sensor.groupby("cycle")["time"].transform("min")
    elapsed_s = (df_sensor["time"] - cycle_start).dt.total_seconds()
    work = df_sensor.assign(elapsed_s=elapsed_s, log_value=np.log(df_sensor[value_col]))

    target_offsets = (
        work.groupby("heater_profile_step_index")["elapsed_s"].mean().reindex(range(N_STEPS)).to_numpy()
    )

    rows = []
    for cycle_idx, cycle_df in work.groupby("cycle"):
        cycle_df = cycle_df.sort_values("elapsed_s")
        if len(cycle_df) < 2:
            continue
        lo, hi = cycle_df["elapsed_s"].iloc[0], cycle_df["elapsed_s"].iloc[-1]
        clamped_targets = np.clip(target_offsets, lo, hi)
        interp = interp1d(cycle_df["elapsed_s"], cycle_df["log_value"], kind="linear")
        aligned = np.exp(interp(clamped_targets))
        rows.append({"cycle": cycle_idx,
                      **{f"step_{i}": v for i, v in enumerate(aligned)}})
    return pd.DataFrame(rows).set_index("cycle")
