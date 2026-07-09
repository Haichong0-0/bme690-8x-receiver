"""Classifier + regressor definitions and the leave-one-run-out evaluation
harness used by train.py.

Classical ML only (RandomForest, GradientBoosting, SVM, linear, k-NN) — no
deep learning: the dataset is small (9 runs total; leave-one-run-out is the
only honest CV given only 3 repeats per odour, per plan.md's own caution
about this), nowhere near enough data for a neural net to earn its keep over
these.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np
from sklearn.ensemble import (
    GradientBoostingClassifier, GradientBoostingRegressor,
    RandomForestClassifier, RandomForestRegressor,
)
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, mean_absolute_error, r2_score
from sklearn.neighbors import KNeighborsClassifier, KNeighborsRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC, SVR

from .split import leave_one_run_out

CLASSIFIER_FACTORIES = {
    "rf": lambda seed: RandomForestClassifier(
        n_estimators=300, class_weight="balanced", random_state=seed, n_jobs=-1),
    "gb": lambda seed: GradientBoostingClassifier(n_estimators=200, random_state=seed),
    # probability=True so predict_proba works: real_ml.py averages predict_proba
    # across the 4 sensors, and SVM is the deployed 'detect' classifier (it won
    # the low-concentration eval). Adds an internal CV for Platt scaling, so
    # training is a little slower.
    "svm": lambda seed: SVC(kernel="rbf", C=1.0, class_weight="balanced",
                            probability=True, random_state=seed),
    "logreg": lambda seed: LogisticRegression(max_iter=2000, class_weight="balanced", random_state=seed),
    "knn": lambda seed: KNeighborsClassifier(n_neighbors=5),
}

REGRESSOR_FACTORIES = {
    "rf": lambda seed: RandomForestRegressor(n_estimators=300, random_state=seed, n_jobs=-1),
    "gb": lambda seed: GradientBoostingRegressor(n_estimators=200, random_state=seed),
    "ridge": lambda seed: Ridge(alpha=1.0, random_state=seed),
    "svr": lambda seed: SVR(kernel="rbf", C=1.0),
    "knn": lambda seed: KNeighborsRegressor(n_neighbors=5),
}


def make_classifier(algo: str = "rf", seed: int = 0):
    if algo not in CLASSIFIER_FACTORIES:
        raise ValueError(f"unknown classifier algo {algo!r}; choose from {list(CLASSIFIER_FACTORIES)}")
    return CLASSIFIER_FACTORIES[algo](seed)


def make_regressor(algo: str = "rf", seed: int = 0):
    if algo not in REGRESSOR_FACTORIES:
        raise ValueError(f"unknown regressor algo {algo!r}; choose from {list(REGRESSOR_FACTORIES)}")
    return REGRESSOR_FACTORIES[algo](seed)


@dataclass
class LOROResult:
    fold_metrics: List[dict]
    mean_metrics: dict
    labels: List[str] = None
    confusion: np.ndarray = None  # rows=true, cols=predicted, ordered per `labels`


def evaluate_classifier_loro(
    X: np.ndarray, y: np.ndarray, run_id: np.ndarray, algo: str = "rf", seed: int = 0
) -> LOROResult:
    labels = sorted(set(y.tolist()))
    fold_metrics = []
    all_true, all_pred = [], []
    for train_idx, test_idx in leave_one_run_out(run_id):
        scaler = StandardScaler().fit(X[train_idx])
        clf = make_classifier(algo, seed)
        clf.fit(scaler.transform(X[train_idx]), y[train_idx])
        pred = clf.predict(scaler.transform(X[test_idx]))
        fold_metrics.append({
            "held_out_run": str(run_id[test_idx][0]),
            "accuracy": float(accuracy_score(y[test_idx], pred)),
            "f1_macro": float(f1_score(y[test_idx], pred, average="macro")),
            "n_test": int(len(test_idx)),
        })
        all_true.extend(y[test_idx].tolist())
        all_pred.extend(pred.tolist())
    mean_metrics = {
        "accuracy": float(np.mean([m["accuracy"] for m in fold_metrics])),
        "f1_macro": float(np.mean([m["f1_macro"] for m in fold_metrics])),
    }
    confusion = confusion_matrix(all_true, all_pred, labels=labels)
    return LOROResult(fold_metrics, mean_metrics, labels=labels, confusion=confusion)


def loro_regressor_predictions(
    X: np.ndarray, y: np.ndarray, run_id: np.ndarray, algo: str = "rf", seed: int = 0
) -> np.ndarray:
    """Out-of-fold leave-one-run-out predictions, aligned to the input rows:
    every row is predicted by a model trained on all the OTHER runs. This is
    the honest, leakage-free prediction the R² in `evaluate_regressor_loro`
    measures — use it (not in-sample predictions) for evaluation plots."""
    preds = np.full(len(y), np.nan, dtype=float)
    for train_idx, test_idx in leave_one_run_out(run_id):
        scaler = StandardScaler().fit(X[train_idx])
        reg = make_regressor(algo, seed)
        reg.fit(scaler.transform(X[train_idx]), y[train_idx])
        preds[test_idx] = reg.predict(scaler.transform(X[test_idx]))
    return preds


def evaluate_regressor_loro(
    X: np.ndarray, y: np.ndarray, run_id: np.ndarray, algo: str = "rf", seed: int = 0
) -> LOROResult:
    fold_metrics = []
    for train_idx, test_idx in leave_one_run_out(run_id):
        scaler = StandardScaler().fit(X[train_idx])
        reg = make_regressor(algo, seed)
        reg.fit(scaler.transform(X[train_idx]), y[train_idx])
        pred = reg.predict(scaler.transform(X[test_idx]))
        err = y[test_idx] - pred
        fold_metrics.append({
            "held_out_run": str(run_id[test_idx][0]),
            "mae": float(mean_absolute_error(y[test_idx], pred)),
            "rmse": float(np.sqrt(np.mean(err ** 2))),
            "r2": float(r2_score(y[test_idx], pred)),
            "n_test": int(len(test_idx)),
        })
    mean_metrics = {
        "mae": float(np.mean([m["mae"] for m in fold_metrics])),
        "rmse": float(np.mean([m["rmse"] for m in fold_metrics])),
        "r2": float(np.mean([m["r2"] for m in fold_metrics])),
    }
    return LOROResult(fold_metrics, mean_metrics)
