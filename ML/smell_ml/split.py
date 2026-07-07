"""Stage 6: group-aware train/test split.

`group = run_id` satisfies both leakage risks at once: the 4 HP354 sensors'
vectors from one cycle are near-duplicates (same headspace instant) and stay
together because they share a run_id; adjacent cycles within one decay sweep
are smooth neighbours and stay together for the same reason. Splitting by
run therefore automatically respects both constraints.

With only 3 repeats per odour (as of the captures in data/raw/), a
single held-out split is coarse — leave-one-run-out cross-validation is
offered for a more stable estimate from this little data.
"""
from __future__ import annotations

from typing import Iterator, Tuple

import numpy as np
from sklearn.model_selection import GroupShuffleSplit, LeaveOneGroupOut


def train_test_split_by_run(
    run_ids: np.ndarray, test_size: float = 0.3, seed: int = 0
) -> Tuple[np.ndarray, np.ndarray]:
    gss = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    idx = np.arange(len(run_ids))
    train_idx, test_idx = next(gss.split(idx, groups=run_ids))
    return train_idx, test_idx


def leave_one_run_out(run_ids: np.ndarray) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
    logo = LeaveOneGroupOut()
    idx = np.arange(len(run_ids))
    yield from logo.split(idx, groups=run_ids)
