#!/usr/bin/env python3
"""Test a fresh capture against the deployed model — and see it.

Takes ONE raw `bme690_receiver_*.csv` (a new recording the model has never
seen), runs it through the exact same preprocessing as training, and draws a
diagnostic-style graph per HP354 sensor with THREE things on the strength
axis:

  * dashed black — the shape-derived "true" strength label (baseline=0,
    plateau=1, exponential-fit transitions — the same Stage-3 label training
    uses). It's a reference derived from THIS run's own curve, not an
    absolute ground truth.
  * dotted green — the strength the deployed regressor (ML/models/) predicts
    for this run. This is the real test: does the model track the strength on
    a capture it never trained on?

The predicted odour (from the deployed classifier, on the plateau cycles) is
shown in each plot title, next to the strength agreement (MAE) for that sensor.

Unlike `ML/evaluate.py` (which plots leave-one-run-out predictions over the 9
TRAINING runs), this uses the shipped model trained on ALL data and applies it
to a brand-new file — genuine held-out testing.

The input CSV does NOT need the training filename convention
(`..._<odour><conc>.csv`); a plain `bme690_receiver_<ts>.csv` fresh off the
receiver works. If the odour IS in the filename it's shown for comparison.

Usage:
    python test_capture.py path/to/bme690_receiver_20260703_120000.csv
    python test_capture.py my_session.csv --out-dir output --no-lowpass
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

THIS_DIR = Path(__file__).resolve().parent
ML_DIR = THIS_DIR.parent
sys.path.insert(0, str(ML_DIR))
import preprocess  # noqa: E402
from train import build_classifier_features  # noqa: E402
from smell_ml import diagnostics, grid, io, windowing  # noqa: E402

DEFAULT_MODELS_DIR = ML_DIR / "models"
DEFAULT_BMECONFIG = ML_DIR.parent / "Server" / "Sample.bmeconfig"
DEFAULT_OUT_DIR = THIS_DIR / "output"
STEP_COLS = [f"step_{i}" for i in range(10)]
REQUIRED_COLS = {"time", "sensor_index", "heater_profile_step_index",
                 "resistance_gassensor", "label_tag"}


def load_capture(csv_path: Path) -> tuple[pd.DataFrame, str, str | None]:
    """Lenient loader: any CSV with the raw receiver columns. Parses the odour
    from the filename if it follows the training convention, else odour=None
    and run_id is the filename stem."""
    df = pd.read_csv(csv_path, parse_dates=["time"])
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        sys.exit(f"ERROR: {csv_path.name} is missing required columns {sorted(missing)} — "
                 "is it a raw bme690_receiver capture?")
    try:
        meta = io.parse_run_meta(csv_path)
        return df, meta.run_id, meta.odour
    except ValueError:
        return df, csv_path.stem, None


def build_regressor_features(window_df: pd.DataFrame) -> np.ndarray:
    """Same feature vector train.py / evaluate.py / real_ml.py use: the
    flattened N×10 window plus the two trend features."""
    n = len(window_df)
    return np.concatenate([
        windowing.stack_windows(window_df).reshape(n, -1),
        np.stack([window_df["trend_slope"], window_df["trend_diff_last"]], axis=1),
    ], axis=1).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, help="raw bme690_receiver_*.csv from a recording session")
    ap.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR,
                     help="trained model artifacts (default: ML/models/)")
    ap.add_argument("--bmeconfig", type=Path, default=DEFAULT_BMECONFIG,
                     help=".bmeconfig used at capture, to identify the HP354 sensors")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--no-lowpass", dest="apply_lowpass", action="store_false",
                     help="disable Stage-1 low-pass filter (on by default, matching training)")
    args = ap.parse_args()

    if not args.csv.exists():
        sys.exit(f"ERROR: {args.csv} not found")
    for f in ("regressor.joblib", "regressor_scaler.joblib",
              "classifier.joblib", "classifier_scaler.joblib", "metadata.json"):
        if not (args.models_dir / f).exists():
            sys.exit(f"ERROR: {args.models_dir / f} not found — train first (python ../train.py)")

    df, run_id, odour_true = load_capture(args.csv)
    sensors = grid.hp354_sensor_indices(args.bmeconfig)
    print(f"# testing {args.csv.name}  (run_id={run_id}, "
          f"filename odour={odour_true or 'unknown'}, HP354 sensors={sensors})")

    regressor = joblib.load(args.models_dir / "regressor.joblib")
    regressor_scaler = joblib.load(args.models_dir / "regressor_scaler.joblib")
    classifier = joblib.load(args.models_dir / "classifier.joblib")
    classifier_scaler = joblib.load(args.models_dir / "classifier_scaler.joblib")
    # The deployed classifier may use engineered features (e.g. raw_gradient);
    # apply the SAME transform its scaler was fit on, or predict_proba below
    # gets the wrong feature count.
    clf_features = json.loads((args.models_dir / "metadata.json").read_text()).get("classifier_features", "raw")

    cycle_df, window_df, _ = preprocess.preprocess_dataframe(
        df, run_id, odour_true, sensors, apply_lowpass=args.apply_lowpass,
    )
    if cycle_df.empty:
        sys.exit("ERROR: no HP354 cycles found — check the sensors ran the 354 profile in this capture.")

    # --- predicted strength (regressor) on every window ---
    if not window_df.empty:
        window_df = window_df.copy()
        window_df["y_pred"] = np.clip(
            regressor.predict(regressor_scaler.transform(build_regressor_features(window_df))), 0, 1)

    # --- predicted odour (classifier) on the plateau cycles, mean-proba like real_ml ---
    plateau = cycle_df[cycle_df["phase"] == "plateau"]
    odour_pred, odour_conf = None, None
    if len(plateau):
        feats = build_classifier_features(plateau[STEP_COLS].to_numpy(np.float32), clf_features)
        proba = classifier.predict_proba(classifier_scaler.transform(feats))
        mean_proba = proba.mean(axis=0)
        best = int(mean_proba.argmax())
        odour_pred, odour_conf = str(classifier.classes_[best]), float(mean_proba[best])
        verdict = ""
        if odour_true:
            verdict = "  [matches filename]" if odour_pred == odour_true else f"  [MISMATCH: filename says {odour_true}]"
        print(f"# predicted odour: {odour_pred}  (confidence {odour_conf:.2f}, "
              f"from {len(plateau)} plateau cycles){verdict}")
    else:
        print("# no plateau phase detected — cannot predict odour (unusual capture shape?)")

    # --- one plot per sensor ---
    args.out_dir.mkdir(parents=True, exist_ok=True)
    odour_tag = f"pred odour={odour_pred} ({odour_conf:.0%})" if odour_pred else "odour n/a"
    written = []
    for sid, cyc in cycle_df.groupby("sensor_id"):
        cyc = cyc.sort_values("cycle_index").reset_index(drop=True)
        log_r = cyc[STEP_COLS].mean(axis=1).to_numpy()

        pred_by_cycle: dict = {}
        if not window_df.empty:
            w = window_df[window_df["sensor_id"] == sid]
            pred_by_cycle = dict(zip(w["window_end_cycle"].astype(int), w["y_pred"]))
        y_pred_cycle = np.array([pred_by_cycle.get(int(c), np.nan) for c in cyc["cycle_index"]])

        # strength agreement (MAE) where we have both a prediction and a label
        both = ~np.isnan(y_pred_cycle) & ~np.isnan(cyc["y_conc"].to_numpy())
        mae = float(np.mean(np.abs(y_pred_cycle[both] - cyc["y_conc"].to_numpy()[both]))) if both.any() else float("nan")
        title_extra = f"{odour_tag} | strength MAE={mae:.3f}"

        path = diagnostics.plot_phase_labels(
            run_id, int(sid), cyc["cycle_timestamp"], log_r,
            cyc["phase"].to_numpy(), cyc["y_conc"].to_numpy(), args.out_dir,
            y_pred=y_pred_cycle, out_suffix="test", title_extra=title_extra,
            pred_label="predicted, deployed model",
        )
        written.append(path)
        print(f"    sensor {sid}: strength MAE (pred vs shape-label) = {mae:.3f}")

    print(f"\nwrote {len(written)} plot(s) to {args.out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
