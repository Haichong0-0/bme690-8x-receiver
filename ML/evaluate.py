#!/usr/bin/env python3
"""Evaluation visualisation: overlay the strength regressor's predicted
y_conc onto each run's diagnostic curve, next to the true label — so you can
SEE where the model tracks strength and where it drifts, per run and
sensor.

Predictions are **leave-one-run-out / out-of-fold**: every run is predicted by
a model trained on the other 8 runs. That's the honest, leakage-free view the
R² in `train.py` reports — never plot in-sample predictions here, they'd look
misleadingly perfect.

Reads `data/processed/` (needs `window_dataset.npz` with `window_end_cycle`,
written by the current `preprocess.py`/`build_dataset.py`) and writes one PNG
per (run, sensor) to `data/diagnostics_eval/`. Each plot is the familiar
phase-coloured mean-log-resistance curve with two lines on the strength
axis: dashed black = true strength, dotted green = predicted. (The strength
label is the `y_conc` column in the processed data — same 0-1 value, just
named "strength" in the human-facing wording to avoid implying absolute
concentration.)

Usage:
    python evaluate.py                       # rf regressor (the deployed default)
    python evaluate.py --regressor-algo gb
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, r2_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smell_ml import diagnostics, models  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
PROCESSED_DIR = THIS_DIR / "data" / "processed"
DEFAULT_EVAL_DIR = THIS_DIR / "data" / "diagnostics_eval"
STEP_COLS = [f"step_{i}" for i in range(10)]


def load_regression_data_with_keys():
    """Same feature construction as train.load_regression_data, but also
    returns sensor_id and window_end_cycle so predictions can be mapped back
    to cycles for plotting."""
    d = np.load(PROCESSED_DIR / "window_dataset.npz", allow_pickle=True)
    if "window_end_cycle" not in d.files:
        sys.exit("ERROR: window_dataset.npz has no 'window_end_cycle' — regenerate it with "
                  "`python build_dataset.py` (the current preprocess.py adds it).")
    n = len(d["y_conc"])
    X = np.concatenate([
        d["X_window"].reshape(n, -1),
        np.stack([d["trend_slope"], d["trend_diff_last"]], axis=1),
    ], axis=1).astype(np.float32)
    return (X, d["y_conc"].astype(np.float32), d["run_id"], d["sensor_id"],
            d["window_end_cycle"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regressor-algo", choices=list(models.REGRESSOR_FACTORIES), default="rf",
                     help="regressor to evaluate (default: rf, the deployed model)")
    ap.add_argument("--eval-dir", type=Path, default=DEFAULT_EVAL_DIR)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if not (PROCESSED_DIR / "window_dataset.npz").exists():
        sys.exit("ERROR: no processed dataset — run `python build_dataset.py` first.")

    X, y, run_id, sensor_id, window_end_cycle = load_regression_data_with_keys()

    # Out-of-fold predictions (each run predicted by a model trained on the rest).
    y_pred = np.clip(models.loro_regressor_predictions(X, y, run_id, algo=args.regressor_algo, seed=args.seed), 0, 1)
    overall_mae = mean_absolute_error(y, y_pred)
    overall_r2 = r2_score(y, y_pred)
    print(f"# regressor '{args.regressor_algo}' — pooled LORO out-of-fold: "
          f"MAE={overall_mae:.3f}  R^2={overall_r2:.3f}  (n={len(y)} windows)")

    # Index predictions by (run_id, sensor_id, window_end_cycle) for lookup per plot.
    pred_df = pd.DataFrame({
        "run_id": run_id, "sensor_id": sensor_id.astype(int),
        "cycle_index": window_end_cycle.astype(int), "y_pred": y_pred, "y_true": y,
    })

    cycles = pd.read_csv(PROCESSED_DIR / "cycle_dataset.csv", parse_dates=["cycle_timestamp"])

    args.eval_dir.mkdir(parents=True, exist_ok=True)
    n_plots = 0
    for (rid, sid), cyc in cycles.groupby(["run_id", "sensor_id"]):
        cyc = cyc.sort_values("cycle_index").reset_index(drop=True)
        log_r = cyc[STEP_COLS].mean(axis=1).to_numpy()

        # Per-cycle predicted array: NaN except cycles that are a window end.
        p = pred_df[(pred_df.run_id == rid) & (pred_df.sensor_id == sid)]
        pred_by_cycle = dict(zip(p["cycle_index"], p["y_pred"]))
        y_pred_cycle = np.array([pred_by_cycle.get(int(c), np.nan) for c in cyc["cycle_index"]])

        title_extra = ""
        if len(p):
            title_extra = (f"LORO  MAE={mean_absolute_error(p['y_true'], p['y_pred']):.3f}  "
                           f"R^2={r2_score(p['y_true'], p['y_pred']):.3f}") if len(p) > 1 else ""

        diagnostics.plot_phase_labels(
            rid, int(sid), cyc["cycle_timestamp"], log_r,
            cyc["phase"].to_numpy(), cyc["y_conc"].to_numpy(), args.eval_dir,
            y_pred=y_pred_cycle, out_suffix="eval", title_extra=title_extra,
            pred_label="predicted, LORO",
        )
        n_plots += 1

    print(f"wrote {n_plots} evaluation plots to {args.eval_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
