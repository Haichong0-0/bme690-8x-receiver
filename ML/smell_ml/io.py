"""Load raw BME690 capture CSVs and attach run metadata parsed from the filename.

Filename convention observed in data/raw/ (not documented anywhere,
reverse-engineered from the real captures):

    bme690_receiver_<YYYYMMDD>_<HHMMSS>_<odour><conc>.csv
    e.g. bme690_receiver_20260625_172513_lemon0.6.csv

`<odour>` is whatever alpha string appears (lemon, grapefruit, sorange, ...) —
this pipeline does NOT hard-code plan.md's odour set (lemon/grapefruit/lavender)
because the real captures don't match it exactly (sorange, not lavender, has
been captured so far). See ML/README.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

import pandas as pd

FILENAME_RE = re.compile(
    r"^bme690_receiver_(?P<ts>\d{8}_\d{6})_(?P<odour>[A-Za-z]+)(?P<conc>[\d.]+)\.csv$"
)

RAW_COLUMNS = [
    "time", "sensor_index", "sensor_id", "timestamp_since_poweron",
    "real_time_clock", "temperature", "pressure", "relative_humidity",
    "resistance_gassensor", "heater_profile_step_index", "target_c",
    "scanning_enabled", "scanning_cycle_index", "label_tag", "error_code",
]


@dataclass(frozen=True)
class RunMeta:
    run_id: str
    odour: str
    session_conc: float
    source_path: Path


def parse_run_meta(csv_path: Path) -> RunMeta:
    m = FILENAME_RE.match(csv_path.name)
    if not m:
        raise ValueError(
            f"{csv_path.name!r} doesn't match the expected "
            f"bme690_receiver_<ts>_<odour><conc>.csv naming convention"
        )
    return RunMeta(
        run_id=f"{m['odour']}_{m['ts']}",
        odour=m["odour"],
        session_conc=float(m["conc"]),
        source_path=csv_path,
    )


def load_run(csv_path: Path) -> pd.DataFrame:
    """One run = one capture session = one CSV file."""
    meta = parse_run_meta(csv_path)
    df = pd.read_csv(csv_path, parse_dates=["time"])
    missing = set(RAW_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{csv_path}: missing expected columns {missing}")
    df["run_id"] = meta.run_id
    df["odour"] = meta.odour
    df["session_conc"] = meta.session_conc
    return df.sort_values("time").reset_index(drop=True)


def discover_runs(data_dir: Path) -> List[Path]:
    return sorted(data_dir.glob("bme690_receiver_*.csv"))
