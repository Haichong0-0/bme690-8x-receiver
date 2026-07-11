"""Stage 2a/2b groundwork: HP354 sensor selection, and pivoting each sensor's
raw rows into a (cycle x step) grid — the structure both Stage 1 (cleaning,
"per sensor channel") and the rest of Stage 2 operate on.

Cycle-boundary detection, and why it's NOT just `scanning_cycle_index`: that
column looks like the obvious candidate (it's already in the raw CSV, no
heuristic needed) but checking it against real captures shows it does NOT
align to heater-profile-loop boundaries — `heater_profile_step_index` wraps
mid-`scanning_cycle_index` group, and a single `scanning_cycle_index` value
can contain step readings from up to 3 different heater passes. It's some
other, coarser counter (probably tied to the duty-cycle scan/sleep schedule),
not "one 10-step pass." The real cycle boundary is a `heater_profile_step_index`
wraparound (this row's step index < the previous row's, for this sensor's
time-ordered rows) — verified on real data: 213/215 detected cycles land on
exactly 10 rows, vs. a garbled 1-11 rows/group using `scanning_cycle_index`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pandas as pd

_SERVER_DIR = Path(__file__).resolve().parents[2] / "Server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from bmeconfig_to_profile import convert as convert_bmeconfig  # noqa: E402

HP354_ID_SUBSTRING = "354"
CONST_TEMP_ID_SUBSTRING = "const"  # heater_const_320 -> the constant-temperature sensors
N_STEPS = 10


def sensor_indices_by_profile(bmeconfig_path: Path, id_substring: str) -> List[int]:
    """Sensor indices whose heater-profile id contains `id_substring`, read
    from the actual .bmeconfig used for capture (not guessed from the data).
    Generalises the two profiles this rig captures: pass `HP354_ID_SUBSTRING`
    ('354') for the variable-temperature training sensors, or
    `CONST_TEMP_ID_SUBSTRING` ('const') for the constant-320 sensors."""
    result = convert_bmeconfig(bmeconfig_path)
    return sorted(
        sc["sensor_index"] for sc in result["sensor_configs"]
        if sc["heater_id"] and id_substring in sc["heater_id"]
    )


def hp354_sensor_indices(bmeconfig_path: Path) -> List[int]:
    """Which chip-selects run the variable-temperature (354) heater profile —
    per the plan, read from the .bmeconfig, not guessed from the data (though
    in practice it matches the data-driven heuristic: sensors whose target_c
    varies vs. sensors stuck at 320)."""
    return sensor_indices_by_profile(bmeconfig_path, HP354_ID_SUBSTRING)


def filter_sensors(df_run: pd.DataFrame, sensor_indices: List[int]) -> pd.DataFrame:
    return df_run[df_run["sensor_index"].isin(sensor_indices)].copy()


def true_cycle_index(df_sensor: pd.DataFrame) -> pd.DataFrame:
    """`df_sensor` re-sorted by time with a `cycle` column added: a real
    heater-profile-pass counter derived from step-index wraparound, distinct
    from (and not to be confused with) the `scanning_cycle_index` column."""
    out = df_sensor.sort_values("time").reset_index(drop=True)
    wrapped = (out["heater_profile_step_index"].diff() < 0).fillna(False)
    out["cycle"] = wrapped.cumsum()
    return out


def raw_grid(df_sensor: pd.DataFrame) -> pd.DataFrame:
    """One row per true heater-profile cycle, columns 0..9 = raw resistance
    at each heater step, plus `cycle_timestamp` (cycle start time). Un-imputed,
    un-filtered, un-logged — Stage 1 (clean.py) operates on this."""
    df_sensor = true_cycle_index(df_sensor)
    pivot = df_sensor.pivot_table(
        index="cycle", columns="heater_profile_step_index",
        values="resistance_gassensor", aggfunc="mean",
    )
    pivot = pivot.reindex(columns=range(N_STEPS))
    pivot.columns = [f"step_{c}" for c in pivot.columns]
    cycle_ts = df_sensor.groupby("cycle")["time"].min()
    pivot.insert(0, "cycle_timestamp", cycle_ts)
    # Drop the ragged first/last partial cycles (e.g. capture started mid-loop
    # or was cut off before a full 10-step pass completed).
    complete = df_sensor.groupby("cycle").size() >= N_STEPS - 1
    return pivot.loc[complete.reindex(pivot.index, fill_value=False)]


def step_offset_jitter(df_sensor: pd.DataFrame) -> pd.DataFrame:
    """Diagnostic for the plan's Stage 2c decision ('check before skipping
    spline alignment'): elapsed time since cycle start, per step index,
    summarised across all true cycles of this sensor/run."""
    df_sensor = true_cycle_index(df_sensor)
    cycle_start = df_sensor.groupby("cycle")["time"].transform("min")
    elapsed_s = (df_sensor["time"] - cycle_start).dt.total_seconds()
    return (
        df_sensor.assign(elapsed_s=elapsed_s)
        .groupby("heater_profile_step_index")["elapsed_s"]
        .agg(["mean", "std", "min", "max"])
        .reindex(range(N_STEPS))
    )
