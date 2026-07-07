#!/usr/bin/env python3
"""Train the odour classifier + strength regressor on
ML/data/processed/ and save deployable artifacts to ML/models/.

Evaluation is leave-one-run-out (9 folds, one per capture) — the only honest
CV given just 3 repeats per odour (plan.md's own caution). The saved models
are then refit on *all* available data (standard practice: CV estimates
generalisation, the shipped model should use every sample it can).

Classifier training data: cycles need a confident odour actually present in
the air to be a meaningful fingerprint example. Baseline-phase cycles are
clean air with no odour, yet would carry that run's odour label if included
naively — three different odours' baseline readings look almost identical,
so training on them would inject label noise, not signal. Two candidate
cycle subsets are evaluated and the one with the better LORO accuracy is
kept: 'plateau' (peak-exposure cycles only) and 'high_conc' (any cycle with
y_conc >= 0.5, which also pulls in the upper part of rise/decay).

Usage:
    python train.py
    python train.py --classifier-phase-filter plateau
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smell_ml import models  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
PROCESSED_DIR = THIS_DIR / "data" / "processed"
DEFAULT_MODELS_DIR = THIS_DIR / "models"
EXPERIMENTS_CSV = THIS_DIR / "experiments.csv"
EXPERIMENTS_MD = THIS_DIR / "EXPERIMENTS.md"
# Rows parked here when experiments.csv can't be written (e.g. it's open in
# Excel — a very common locked-file scenario). Auto-merged on the next
# successful log_experiment so an expensive training run's result is never
# lost to a locked log file.
EXPERIMENTS_PENDING = THIS_DIR / "experiments.pending.jsonl"

CLASSIFIER_HIGH_CONC_THRESHOLD = 0.5
STEP_COLS = [f"step_{i}" for i in range(10)]

# Heater-step -> temperature grouping, confirmed from data/raw (target_c per
# step): 100C = steps 1,2,3 | 200C = steps 4,5,6 | 320C = steps 0,7,8,9.
TEMP_GROUPS = {100: [1, 2, 3], 200: [4, 5, 6], 320: [0, 7, 8, 9]}
TEMP_CONTRAST_COLS = [
    "m100", "m200", "m320",                  # per-temperature mean level
    "d100_320", "d100_200", "d200_320",      # cross-temperature log-ratios (selectivity)
    "range_all", "range_100", "range_200", "range_320",  # transient ranges (nonlinear)
]


def temperature_contrast_features(steps: np.ndarray) -> np.ndarray:
    """`steps`: (n, 10) log-resistance. Returns (n, 10) engineered features
    that make the cross-temperature structure explicit — motivated by the
    e-nose literature (deep-research 2026-07-03): MOx selectivity comes from
    the temperature dependence, and a temperature *gradient* raised array
    discrimination by up to ~35% vs single-temperature operation (Sensors
    2014, PMC3954066); treating the temperature response as a shape beats
    reducing/averaging it (statistical shape analysis, S0925400520315276).

    Data is log-resistance, so a difference IS a log response-ratio
    (logRa - logRb = log(Ra/Rb)) — the classic response-across-temperatures
    selectivity feature. CAVEAT: the group means and pairwise differences are
    linear combinations of the raw steps, so a purely linear model (logreg)
    can already represent them and gains nothing from them — they mainly help
    the tree/kernel models. The min/max RANGE features are nonlinear (per
    temperature and overall), capturing the reaction transient *within* each
    temperature hold, and are the part that can add signal even to logreg."""
    g100 = steps[:, TEMP_GROUPS[100]]
    g200 = steps[:, TEMP_GROUPS[200]]
    g320 = steps[:, TEMP_GROUPS[320]]
    m100, m200, m320 = g100.mean(1), g200.mean(1), g320.mean(1)
    feats = np.stack([
        m100, m200, m320,
        m100 - m320, m100 - m200, m200 - m320,
        steps.max(1) - steps.min(1),
        g100.max(1) - g100.min(1),
        g200.max(1) - g200.min(1),
        g320.max(1) - g320.min(1),
    ], axis=1)
    return feats.astype(np.float32)


def build_classifier_features(steps: np.ndarray, features: str = "raw") -> np.ndarray:
    """Map an (n, 10) log-resistance step matrix to the classifier feature set
    named by `features` (see load_classification_data for what each means).
    Shared by `load_classification_data` (training) and
    `testing/test_capture.py` (applying the deployed model) so the two stay in
    sync; `Server/real_ml.py` deliberately re-implements the level-based cases
    to keep its no-ML-import deployment boundary."""
    if features == "raw":
        return steps
    if features == "temp_contrast":
        return np.concatenate([steps, temperature_contrast_features(steps)], axis=1)
    if features == "gradient":
        return np.diff(steps, axis=1)  # 9 step-to-step slopes; pure level-invariant shape
    if features == "raw_gradient":
        return np.concatenate([steps, np.diff(steps, axis=1)], axis=1)  # 10 levels + 9 slopes
    raise ValueError(f"unknown features {features!r}")

# One row per train.py run. Preprocessing params come from data/processed/meta.json
# (i.e. whatever build_dataset.py was last run with) plus this run's own choices.
EXPERIMENT_COLUMNS = [
    "exp_id", "timestamp", "lowpass_filter_applied", "window_size_cycles",
    "classifier_phase_filter", "classifier_algo", "classifier_features", "regressor_algo",
    "n_cycles_total", "n_windows_total",
    "classifier_loro_accuracy", "classifier_loro_f1_macro",
    "regressor_loro_mae", "regressor_loro_rmse", "regressor_loro_r2", "notes",
]

# Value to backfill into rows logged before a column existed, so old rows stay
# truthful about the (then-implicit) defaults rather than showing blanks.
EXPERIMENT_COLUMN_BACKFILL = {
    "classifier_algo": "rf",       # pre-algo-sweep runs were RandomForest
    "regressor_algo": "rf",
    "classifier_features": "raw",  # pre-temp-contrast runs used the raw 10 steps
}


def _write_experiments_md(df: pd.DataFrame) -> None:
    lines = [
        "# Preprocessing / training experiment log",
        "",
        "Auto-generated by `train.py` from `experiments.csv` every run — don't hand-edit this file, "
        "edit `experiments.csv` and rerun `train.py` (or regenerate the markdown) instead.",
        "",
        "| " + " | ".join(EXPERIMENT_COLUMNS) + " |",
        "|" + "---|" * len(EXPERIMENT_COLUMNS),
    ]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in EXPERIMENT_COLUMNS) + " |")
    EXPERIMENTS_MD.write_text("\n".join(lines) + "\n")


def log_experiment(row: dict) -> None:
    """Append one row to experiments.csv (creating it if needed) and
    regenerate the human-readable EXPERIMENTS.md table from it. Auto-called by
    every train.py run so the log can't silently drift out of date.

    Resilient to experiments.csv being locked (open in Excel etc.): the new
    row is parked in a sidecar and merged on the next successful run, so an
    expensive training run's result is never lost to a locked log file."""
    existing = pd.read_csv(EXPERIMENTS_CSV) if EXPERIMENTS_CSV.exists() else pd.DataFrame(columns=EXPERIMENT_COLUMNS)
    for col in EXPERIMENT_COLUMNS:
        if col not in existing.columns:
            existing[col] = EXPERIMENT_COLUMN_BACKFILL.get(col, pd.NA)

    # Merge any rows earlier runs had to park because the CSV was locked.
    parked = []
    if EXPERIMENTS_PENDING.exists():
        parked = [json.loads(ln) for ln in EXPERIMENTS_PENDING.read_text().splitlines() if ln.strip()]

    new_rows = parked + [dict(row)]
    updated = pd.concat([existing] + [pd.DataFrame([r]) for r in new_rows], ignore_index=True)
    updated = updated.reindex(columns=EXPERIMENT_COLUMNS)
    updated["exp_id"] = range(1, len(updated) + 1)  # sequential in file order

    try:
        updated.to_csv(EXPERIMENTS_CSV, index=False)
    except PermissionError:
        with EXPERIMENTS_PENDING.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(row)) + "\n")
        print(f"# WARN: {EXPERIMENTS_CSV.name} is locked (is it open in Excel / a viewer?). "
              f"Parked this run's row in {EXPERIMENTS_PENDING.name} — it'll be merged "
              f"automatically on the next train.py run after you close the file. "
              f"The models and metadata.json were still saved.", file=sys.stderr)
        return

    EXPERIMENTS_PENDING.unlink(missing_ok=True)  # everything merged in; sidecar is clear
    _write_experiments_md(updated)


def load_classification_data(phase_filter: str, features: str = "raw") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Classifier features per cycle. `features`:
      - "raw": the 10 per-step log-resistance values (the original default).
      - "temp_contrast": the 10 raw steps PLUS the temperature-contrast
        features (see temperature_contrast_features) — tests the e-nose
        literature's recommendation to make cross-temperature structure
        explicit rather than leaving it implicit in 10 correlated steps.
      - "gradient": ONLY the 9 step-to-step differences np.diff(steps) — the
        derivative of the temperature sweep. In log-resistance a difference is
        a log response-ratio, and it's level-invariant (a uniform per-cycle
        offset cancels), so this is a pure-shape representation.
      - "raw_gradient": the 10 raw steps PLUS the 9 gradients (19 features) —
        keeps the absolute level AND makes the local slopes explicit.

    Caveats worth stating (this is why we sweep rather than assume):
      * The gradient is a LINEAR combination of the raw steps, so a linear
        model (logreg) can already represent it and gains nothing from the
        "raw_gradient" augmentation — gradients help the nonlinear models
        (rf/gb/svm), which can't synthesise arbitrary linear combos as splits.
      * A pure level-invariant representation is not obviously better here: a
        baseline-delta variant (subtracting each run/sensor's own clean-air
        mean) was tried and made LORO accuracy *worse* (0.61 -> 0.48), so
        absolute level does carry some signal. "gradient" (pure shape) may
        show the same effect; "raw_gradient" hedges by keeping both.
    The known residual failure mode (see train.py's module docstring / the
    confusion matrix it prints) is that lemon is highly distinct but grapefruit
    and sorange are confused with each other in both directions — consistent
    with those two sharing similar dominant citrus terpenes."""
    df = pd.read_csv(PROCESSED_DIR / "cycle_dataset.csv")
    if phase_filter == "plateau":
        subset = df[df["phase"] == "plateau"]
    elif phase_filter == "high_conc":
        subset = df[df["y_conc"] >= CLASSIFIER_HIGH_CONC_THRESHOLD]
    else:
        raise ValueError(f"unknown phase_filter {phase_filter!r}")
    steps = subset[STEP_COLS].to_numpy(dtype=np.float32)
    X = build_classifier_features(steps, features)
    y = subset["odour"].to_numpy()
    run_id = subset["run_id"].to_numpy()
    return X, y, run_id


def load_regression_data() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    d = np.load(PROCESSED_DIR / "window_dataset.npz", allow_pickle=True)
    n = len(d["y_conc"])
    X = np.concatenate([
        d["X_window"].reshape(n, -1),
        np.stack([d["trend_slope"], d["trend_diff_last"]], axis=1),
    ], axis=1).astype(np.float32)
    return X, d["y_conc"].astype(np.float32), d["run_id"]


def _print_loro(label: str, result: models.LOROResult, extra_keys: list[str]) -> None:
    summary = "  ".join(f"{k}={result.mean_metrics[k]:.3f}" for k in extra_keys)
    print(f"# {label}: LORO {summary}")
    for m in result.fold_metrics:
        per_fold = "  ".join(f"{k}={m[k]:.3f}" for k in extra_keys)
        print(f"    held out {m['held_out_run']}: {per_fold}  (n_test={m['n_test']})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--classifier-phase-filter", choices=["plateau", "high_conc"], default=None,
                     help="which cycles train the classifier; default: try both, keep the better LORO accuracy")
    ap.add_argument("--classifier-algo", choices=list(models.CLASSIFIER_FACTORIES), default="logreg",
                     help="default: logreg -- won the algorithm sweep (ML/EXPERIMENTS.md), "
                          "0.746-0.755 LORO accuracy vs rf's 0.581-0.611")
    ap.add_argument("--classifier-features",
                     choices=["raw", "temp_contrast", "gradient", "raw_gradient"], default="raw_gradient",
                     help="raw_gradient = 10 per-step log-resistance levels + their 9 step-to-step "
                          "gradients (default -- won the feature sweep, LORO 0.755 -> 0.799, and is "
                          "what the deployed model in models/ was trained with; keep the default when "
                          "retraining or the shipped accuracy silently regresses -- real_ml.py follows "
                          "metadata.json's classifier_features either way, so nothing errors); "
                          "raw = the 10 levels only; gradient = the 9 slopes only (pure shape); "
                          "temp_contrast = raw + cross-temperature contrast features "
                          "(see load_classification_data)")
    ap.add_argument("--regressor-algo", choices=list(models.REGRESSOR_FACTORIES), default="rf",
                     help="default: rf -- won the algorithm sweep (ML/EXPERIMENTS.md), R^2 0.859-0.872")
    ap.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--note", default="", help="free-text note for this run's experiments.csv row")
    args = ap.parse_args()

    if not (PROCESSED_DIR / "window_dataset.npz").exists():
        print(f"ERROR: {PROCESSED_DIR} has no processed dataset — run build_dataset.py first.",
              file=sys.stderr)
        return 2

    args.models_dir.mkdir(parents=True, exist_ok=True)

    # --- classifier: pick the best cycle-phase filter by LORO accuracy ---
    filters_to_try = [args.classifier_phase_filter] if args.classifier_phase_filter else ["plateau", "high_conc"]
    best = None  # (filter_name, LOROResult, X, y, run_id)
    for f in filters_to_try:
        X, y, run_id = load_classification_data(f, features=args.classifier_features)
        result = models.evaluate_classifier_loro(X, y, run_id, algo=args.classifier_algo, seed=args.seed)
        _print_loro(f"classifier [{f}/{args.classifier_algo}/{args.classifier_features}] "
                    f"(n={len(y)}, dim={X.shape[1]})", result, ["accuracy", "f1_macro"])
        if best is None or result.mean_metrics["accuracy"] > best[1].mean_metrics["accuracy"]:
            best = (f, result, X, y, run_id)
    clf_filter, clf_result, clf_X, clf_y, _ = best
    print(f"# classifier: keeping '{clf_filter}' (best LORO accuracy)")
    print(f"# confusion matrix (rows=true, cols=predicted), labels={clf_result.labels}:")
    for label, row in zip(clf_result.labels, clf_result.confusion):
        print(f"    {label:>12}: {row.tolist()}")
    print()

    clf_scaler = StandardScaler().fit(clf_X)
    classifier = models.make_classifier(args.classifier_algo, args.seed)
    classifier.fit(clf_scaler.transform(clf_X), clf_y)
    joblib.dump(classifier, args.models_dir / "classifier.joblib")
    joblib.dump(clf_scaler, args.models_dir / "classifier_scaler.joblib")

    # --- regressor: trained on every window (all phases carry real y_conc signal) ---
    reg_X, reg_y, reg_run_id = load_regression_data()
    reg_result = models.evaluate_regressor_loro(reg_X, reg_y, reg_run_id, algo=args.regressor_algo, seed=args.seed)
    _print_loro(f"regressor [{args.regressor_algo}] (n={len(reg_y)})", reg_result, ["mae", "rmse", "r2"])

    reg_scaler = StandardScaler().fit(reg_X)
    regressor = models.make_regressor(args.regressor_algo, args.seed)
    regressor.fit(reg_scaler.transform(reg_X), reg_y)
    joblib.dump(regressor, args.models_dir / "regressor.joblib")
    joblib.dump(reg_scaler, args.models_dir / "regressor_scaler.joblib")

    proc_meta = json.loads((PROCESSED_DIR / "meta.json").read_text())
    metadata = {
        "odours": sorted(set(clf_y.tolist())),
        "classifier_phase_filter": clf_filter,
        "classifier_algo": args.classifier_algo,
        "classifier_features": args.classifier_features,
        "regressor_algo": args.regressor_algo,
        "classifier_loro_accuracy": clf_result.mean_metrics["accuracy"],
        "classifier_loro_f1_macro": clf_result.mean_metrics["f1_macro"],
        "classifier_confusion_labels": clf_result.labels,
        "classifier_confusion_matrix": clf_result.confusion.tolist(),
        "classifier_n_train": int(len(clf_y)),
        "regressor_loro_mae": reg_result.mean_metrics["mae"],
        "regressor_loro_rmse": reg_result.mean_metrics["rmse"],
        "regressor_loro_r2": reg_result.mean_metrics["r2"],
        "regressor_n_train": int(len(reg_y)),
        "window_size_cycles": proc_meta["window_size_cycles"],
        "hp354_sensor_indices": proc_meta["hp354_sensor_indices"],
        "seed": args.seed,
    }
    (args.models_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    log_experiment({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lowpass_filter_applied": proc_meta.get("lowpass_filter_applied"),
        "window_size_cycles": proc_meta["window_size_cycles"],
        "classifier_phase_filter": clf_filter,
        "classifier_algo": args.classifier_algo,
        "classifier_features": args.classifier_features,
        "regressor_algo": args.regressor_algo,
        "n_cycles_total": proc_meta["n_cycles_total"],
        "n_windows_total": proc_meta["n_windows_total"],
        "classifier_loro_accuracy": round(clf_result.mean_metrics["accuracy"], 4),
        "classifier_loro_f1_macro": round(clf_result.mean_metrics["f1_macro"], 4),
        "regressor_loro_mae": round(reg_result.mean_metrics["mae"], 4),
        "regressor_loro_rmse": round(reg_result.mean_metrics["rmse"], 4),
        "regressor_loro_r2": round(reg_result.mean_metrics["r2"], 4),
        "notes": args.note,
    })

    print(f"\nwrote {args.models_dir / 'classifier.joblib'}")
    print(f"wrote {args.models_dir / 'regressor.joblib'}")
    print(f"wrote {args.models_dir / 'metadata.json'}")
    print(f"wrote {EXPERIMENTS_CSV}")
    print(f"wrote {EXPERIMENTS_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
