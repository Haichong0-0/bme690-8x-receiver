#!/usr/bin/env python3
"""Preprocess BME690 capture CSVs (Stage 1-4) — per-CSV functions plus a batch
runner with a training / diagnostics-only switch.

Two heater profiles are captured per session (see Server/Sample.bmeconfig):
  * hp354    (sensors 1,3,5,7) — the variable-temperature 100/200/320C profile
             the model trains on.
  * const320 (sensors 0,2,4,6) — a constant-320C profile. Worth *looking* at
             (does a single-temperature sensor show the same clean exposure
             arc as the modulated ones?) but NOT used for training.

The --training flag decides what happens with the result:
  * with --training: the processed dataset (cycle_dataset.csv,
    window_dataset.npz, run_fits.csv, meta.json) is written to
    data/processed/, and per-run diagnostic PNGs are drawn.
  * without --training: ONLY the per-run diagnostic PNGs are drawn — nothing
    is saved to the processed dataset. This is the mode for eyeballing the
    const-320 sensors (or any capture) without touching the training set.

The per-CSV work lives in `preprocess_csv`; `build_dataset.py` is a thin
wrapper that calls the same `run_batch` with --training for the HP354 sensors,
so there's a single implementation.

Usage:
    python preprocess.py --training                 # HP354 -> training dataset + diagnostics
    python preprocess.py --profile const320         # const-320 -> diagnostics only, nothing saved
    python preprocess.py --profile hp354            # HP354 -> diagnostics only, nothing saved
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smell_ml import align, clean, diagnostics, grid, io, labels, windowing  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = THIS_DIR / "data" / "raw"
DEFAULT_BMECONFIG = THIS_DIR.parent / "Server" / "Sample.bmeconfig"
DEFAULT_OUT_DIR = THIS_DIR / "data" / "processed"

# plan.md's declared odour set — flagged (not enforced) if real captures differ.
PLAN_ODOURS = {"lemon", "grapefruit", "lavender"}

# --profile -> (.bmeconfig heater-id substring, default diagnostics dir). The
# const-320 diagnostics go to their own dir so they never mix with the HP354
# training diagnostics.
PROFILES = {
    "hp354":    (grid.HP354_ID_SUBSTRING,      THIS_DIR / "data" / "diagnostics"),
    "const320": (grid.CONST_TEMP_ID_SUBSTRING, THIS_DIR / "data" / "diagnostics_const320"),
}
STEP_COLS = [f"step_{i}" for i in range(grid.N_STEPS)]


def valid_blocks(cleaned: pd.DataFrame) -> List[pd.DataFrame]:
    """Maximal contiguous runs of cycles with a non-NaN y_conc (warmup and any
    too-short-to-fit segment leave gaps) — windowing must not bridge across a
    gap, since that would silently splice unrelated cycles into one
    'consecutive' window."""
    valid = cleaned["y_conc"].notna().to_numpy()
    blocks, start = [], None
    for i, v in enumerate(valid):
        if v and start is None:
            start = i
        if not v and start is not None:
            blocks.append(cleaned.iloc[start:i])
            start = None
    if start is not None:
        blocks.append(cleaned.iloc[start:])
    return blocks


def preprocess_csv(
    csv_path: Path,
    sensor_indices: List[int],
    *,
    window: int = windowing.DEFAULT_WINDOW,
    apply_lowpass: bool = True,
    diag_dir: Optional[Path] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Run Stages 1-4 over one capture CSV, for the given sensors. Loads the
    CSV (filename must follow the `bme690_receiver_<ts>_<odour><conc>.csv`
    convention so the odour label can be parsed), then delegates to
    `preprocess_dataframe`. Returns (cycle_df, window_df, fit_rows)."""
    meta = io.parse_run_meta(csv_path)
    df_run = io.load_run(csv_path)
    return preprocess_dataframe(
        df_run, meta.run_id, meta.odour, sensor_indices,
        window=window, apply_lowpass=apply_lowpass, diag_dir=diag_dir,
    )


def preprocess_dataframe(
    df_run: pd.DataFrame,
    run_id: str,
    odour: Optional[str],
    sensor_indices: List[int],
    *,
    window: int = windowing.DEFAULT_WINDOW,
    apply_lowpass: bool = True,
    diag_dir: Optional[Path] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Run Stages 1-4 over an already-loaded capture, for the given sensors.
    Separated from `preprocess_csv` so callers with a CSV that doesn't follow
    the training filename convention (e.g. a fresh, unlabelled test recording —
    see testing/) can supply their own `run_id`/`odour` and still go through
    the exact same pipeline. Draws the per-(run, sensor) diagnostic PNG into
    `diag_dir` when provided. Returns (cycle_df, window_df, fit_rows).

    Works for any sensor set — the HP354 training sensors or the const-320
    sensors — since nothing here is temperature-specific: cycle detection,
    cleaning, shape-based phase detection, and the exponential fit all operate
    on the per-cycle curve whatever heater profile produced it."""
    df_sel = grid.filter_sensors(df_run, sensor_indices)

    cycle_rows, window_rows, fit_rows = [], [], []

    for sensor_id, df_sensor in df_sel.groupby("sensor_index"):
        jitter = grid.step_offset_jitter(df_sensor)
        align_needed, jitter_std = align.decide_alignment_needed(jitter)

        if align_needed:
            base_grid = align.spline_align_sensor(df_sensor)
            cycle_ts = grid.true_cycle_index(df_sensor).groupby("cycle")["time"].min()
            base_grid.insert(0, "cycle_timestamp", cycle_ts.reindex(base_grid.index))
        else:
            base_grid = grid.raw_grid(df_sensor)

        cleaned, report = clean.clean_sensor_grid(base_grid, apply_lowpass=apply_lowpass)
        mean_log_r = labels.mean_log_resistance(cleaned)

        tagged = grid.true_cycle_index(df_sensor).groupby("cycle")["label_tag"].agg(lambda s: s.mode().iat[0])
        if not (tagged.reindex(cleaned.index).fillna(0) == 0).all():
            raise NotImplementedError(
                f"{run_id} sensor {sensor_id}: label_tag is populated on this capture, "
                "but tag-based phase segmentation isn't implemented — every run used to build "
                "and validate this pipeline had label_tag == 0 throughout. See labels.py."
            )
        phase, y_conc, seg_fits = labels.label_run_by_shape(cleaned["cycle_timestamp"], mean_log_r)

        cleaned["phase"] = phase
        cleaned["y_conc"] = y_conc
        cleaned["odour"] = odour
        cleaned["run_id"] = run_id
        cleaned["sensor_id"] = sensor_id
        cycle_rows.append(cleaned.reset_index().rename(columns={"cycle": "cycle_index"}))

        for block in valid_blocks(cleaned):
            w = windowing.make_windows(block, run_id, sensor_id, window=window)
            if not w.empty:
                window_rows.append(w)

        for f in seg_fits:
            fit_rows.append({
                "run_id": run_id, "odour": odour, "sensor_id": sensor_id,
                "n_imputed": report.n_imputed, "imputed_rate": report.imputed_rate,
                "fs_hz": report.fs_hz, "cutoff_hz": report.cutoff_hz,
                "alignment_applied": align_needed, "jitter_std_s": jitter_std,
                **f,
            })

        if diag_dir is not None:
            diagnostics.plot_phase_labels(
                run_id, sensor_id, cleaned["cycle_timestamp"], mean_log_r, phase, y_conc, diag_dir,
            )

    cycle_df = pd.concat(cycle_rows, ignore_index=True) if cycle_rows else pd.DataFrame()
    window_df = pd.concat(window_rows, ignore_index=True) if window_rows else pd.DataFrame()
    return cycle_df, window_df, fit_rows


def _print_fit_summary(fit_rows: list[dict]) -> None:
    for r in fit_rows:
        if "r_squared" not in r:
            print(f"    sensor {r['sensor_id']} {r['phase']}: {r.get('skipped') or r.get('error')}")
            continue
        flag = "" if r["r_squared"] >= 0.8 else "  ** LOW R^2, inspect diagnostics PNG **"
        if r.get("asymptote_unreliable"):
            flag += "  ** tau >> segment duration, asymptote extrapolated **"
        print(f"    sensor {r['sensor_id']} {r['phase']}: tau={r['tau_s']:.1f}s  "
              f"R^2={r['r_squared']:.3f}  n={r['n_cycles']}{flag}")


def _save_training_dataset(
    out_dir: Path, cycle_df: pd.DataFrame, window_df: pd.DataFrame, fits_df: pd.DataFrame,
    *, n_runs: int, odours_seen: set, sensor_indices: List[int], window: int, apply_lowpass: bool,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    cycle_out_cols = ["run_id", "odour", "sensor_id", "cycle_index", "cycle_timestamp",
                       "phase", "y_conc"] + STEP_COLS
    cycle_df[cycle_out_cols].to_csv(out_dir / "cycle_dataset.csv", index=False)
    fits_df.to_csv(out_dir / "run_fits.csv", index=False)

    if not window_df.empty:
        np.savez(
            out_dir / "window_dataset.npz",
            X_window=windowing.stack_windows(window_df),
            y_conc=window_df["y_conc"].to_numpy(dtype=np.float32),
            y_class=window_df["y_class"].to_numpy(dtype=object),
            run_id=window_df["run_id"].to_numpy(dtype=object),
            sensor_id=window_df["sensor_id"].to_numpy(dtype=np.int64),
            # cycle the window predicts (its last cycle) — lets evaluate.py map a
            # window prediction back to a cycle in cycle_dataset.csv for the overlay plots.
            window_end_cycle=window_df["window_end_cycle"].to_numpy(dtype=np.int64),
            trend_slope=window_df["trend_slope"].to_numpy(dtype=np.float32),
            trend_diff_last=window_df["trend_diff_last"].to_numpy(dtype=np.float32),
        )

    fitted = fits_df[fits_df.get("r_squared").notna()] if "r_squared" in fits_df else fits_df.iloc[0:0]
    low_r2 = fitted[fitted["r_squared"] < 0.8]
    unreliable = fitted[fitted.get("asymptote_unreliable", False) == True]  # noqa: E712
    phase_counts = cycle_df["phase"].value_counts().to_dict() if len(cycle_df) else {}
    meta_out = {
        "n_runs": n_runs,
        "odours_discovered": sorted(odours_seen),
        "odours_matching_plan_md": sorted(odours_seen & PLAN_ODOURS),
        "odours_not_in_plan_md": sorted(odours_seen - PLAN_ODOURS),
        "hp354_sensor_indices": sensor_indices,
        "window_size_cycles": window,
        "lowpass_filter_applied": apply_lowpass,
        "n_cycles_total": len(cycle_df),
        "n_cycles_by_phase": phase_counts,
        "n_windows_total": len(window_df),
        "n_low_r_squared_fits": int(len(low_r2)),
        "n_unreliable_asymptote_fits": int(len(unreliable)),
        "mean_r_squared": float(fitted["r_squared"].mean()) if len(fitted) else None,
        "mean_imputed_rate": float(fits_df["imputed_rate"].mean()) if len(fits_df) else None,
        "any_alignment_applied": bool(fits_df["alignment_applied"].any()) if len(fits_df) else False,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta_out, indent=2))

    print(f"\nwrote {out_dir / 'cycle_dataset.csv'}  ({len(cycle_df)} rows)")
    print(f"wrote {out_dir / 'window_dataset.npz'}  ({len(window_df)} windows)")
    print(f"wrote {out_dir / 'run_fits.csv'}  ({len(fits_df)} fits)")
    print(f"wrote {out_dir / 'meta.json'}")
    if meta_out["odours_not_in_plan_md"]:
        print(f"\n** NOTE: captured odours {meta_out['odours_not_in_plan_md']} are not in "
              f"plan.md's declared set {sorted(PLAN_ODOURS)} — confirm this is intentional. **")
    if len(low_r2):
        print(f"** NOTE: {len(low_r2)} rise/decay fits have R^2 < 0.8 — check their PNGs "
              f"before trusting those y_conc labels. **")
    if len(unreliable):
        print(f"** NOTE: {len(unreliable)} decay/rise fits have tau >> the observed segment "
              f"duration — the capture ended before the curve levelled off. This is now "
              f"DIAGNOSTIC ONLY: y_conc is read off the observed baseline/plateau levels, not the "
              f"fitted asymptote, so these labels are not extrapolated. See run_fits.csv. **")
    return meta_out


def run_batch(
    *,
    data_dir: Path,
    bmeconfig: Path,
    profile: str,
    for_training: bool,
    window: int = windowing.DEFAULT_WINDOW,
    apply_lowpass: bool = True,
    out_dir: Path = DEFAULT_OUT_DIR,
    diag_dir: Optional[Path],
) -> int:
    """Discover and preprocess every capture in `data_dir` for `profile`'s
    sensors. When `for_training`, writes the processed dataset; otherwise only
    the diagnostic PNGs are drawn (already done inside preprocess_csv) and
    nothing is saved."""
    id_substring, _ = PROFILES[profile]
    runs = io.discover_runs(data_dir)
    if not runs:
        print(f"ERROR: no bme690_receiver_*.csv files found in {data_dir}", file=sys.stderr)
        return 2

    sensor_indices = grid.sensor_indices_by_profile(bmeconfig, id_substring)
    if not sensor_indices:
        print(f"ERROR: no '{profile}' sensors found in {bmeconfig.name}", file=sys.stderr)
        return 2

    mode = "TRAINING (dataset will be saved)" if for_training else "DIAGNOSTICS-ONLY (nothing saved)"
    print(f"# profile '{profile}' sensors (from {bmeconfig.name}): {sensor_indices}")
    print(f"# mode: {mode}")
    if diag_dir is not None:
        print(f"# diagnostics -> {diag_dir}/")

    all_cycles, all_windows, all_fits = [], [], []
    odours_seen = set()
    for csv_path in runs:
        meta = io.parse_run_meta(csv_path)
        odours_seen.add(meta.odour)
        print(f"# processing {csv_path.name}  (run_id={meta.run_id})")
        cycle_df, window_df, fit_rows = preprocess_csv(
            csv_path, sensor_indices, window=window, apply_lowpass=apply_lowpass, diag_dir=diag_dir,
        )
        all_cycles.append(cycle_df)
        all_windows.append(window_df)
        all_fits.extend(fit_rows)
        _print_fit_summary(fit_rows)

    if for_training:
        cycle_df = pd.concat(all_cycles, ignore_index=True)
        window_df = pd.concat(all_windows, ignore_index=True)
        fits_df = pd.DataFrame(all_fits)
        _save_training_dataset(
            out_dir, cycle_df, window_df, fits_df,
            n_runs=len(runs), odours_seen=odours_seen, sensor_indices=sensor_indices,
            window=window, apply_lowpass=apply_lowpass,
        )
    else:
        n_cycles = sum(len(c) for c in all_cycles)
        print(f"\n# diagnostics-only: drew {len(runs) * len(sensor_indices)} plots from "
              f"{n_cycles} cycles across {len(runs)} runs x {len(sensor_indices)} sensors.")
        print(f"# nothing written to {out_dir} (not --training). See {diag_dir}/ for the images.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", choices=list(PROFILES), default="hp354",
                     help="which heater profile's sensors to preprocess (default: hp354)")
    ap.add_argument("--training", action="store_true",
                     help="save the processed training dataset (cycle_dataset.csv, "
                          "window_dataset.npz, run_fits.csv, meta.json). Without it, ONLY the "
                          "diagnostic PNGs are drawn and nothing is saved.")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                     help="directory of bme690_receiver_*.csv captures")
    ap.add_argument("--bmeconfig", type=Path, default=DEFAULT_BMECONFIG,
                     help=".bmeconfig used for capture, to identify each profile's sensors")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                     help="where the training dataset is written (only used with --training)")
    ap.add_argument("--diag-dir", type=Path, default=None,
                     help="where diagnostic PNGs go (default: per-profile dir under data/)")
    ap.add_argument("--window", type=int, default=windowing.DEFAULT_WINDOW,
                     help="cycles per regression window (default: %(default)s)")
    ap.add_argument("--no-lowpass", dest="apply_lowpass", action="store_false",
                     help="disable Stage 1's Butterworth low-pass filter (on by default)")
    ap.add_argument("--no-diagnostics", action="store_true",
                     help="skip the diagnostic PNGs (only meaningful with --training)")
    args = ap.parse_args()

    if args.training and args.profile != "hp354":
        sys.exit(f"ERROR: --training only makes sense for the hp354 sensors; profile "
                  f"'{args.profile}' is the constant-temperature set, which isn't used for "
                  f"training. Drop --training to draw its diagnostics.")
    if not args.training and args.no_diagnostics:
        sys.exit("ERROR: without --training the only output is diagnostics, so --no-diagnostics "
                  "would produce nothing. Drop one of the two.")

    diag_dir = args.diag_dir or PROFILES[args.profile][1]
    if args.no_diagnostics:
        diag_dir = None

    return run_batch(
        data_dir=args.data_dir, bmeconfig=args.bmeconfig, profile=args.profile,
        for_training=args.training, window=args.window, apply_lowpass=args.apply_lowpass,
        out_dir=args.out_dir, diag_dir=diag_dir,
    )


if __name__ == "__main__":
    sys.exit(main())
