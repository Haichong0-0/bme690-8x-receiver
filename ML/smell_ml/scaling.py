"""Stage 5: z-score scaling, fit on the training split only.

Log-transform already happened in Stage 1 (clean.py) — this is scaling on
top of that, not a replacement for it. Deliberately NOT applied by
build_dataset.py: which split to fit on is a training-time decision (depends
on the split/cross-validation strategy chosen downstream), so this is left
for the consumer of the exported dataset to call after importing.
"""
from __future__ import annotations

import numpy as np


class WindowScaler:
    """Per-step-column z-score scaling for [n, N, 10] window arrays."""

    def fit(self, X: np.ndarray) -> "WindowScaler":
        self.mean_ = X.mean(axis=(0, 1), keepdims=True)
        std = X.std(axis=(0, 1), keepdims=True)
        std[std == 0] = 1.0
        self.std_ = std
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        return (X - self.mean_) / self.std_

    def fit_transform(self, X: np.ndarray) -> np.ndarray:
        return self.fit(X).transform(X)
