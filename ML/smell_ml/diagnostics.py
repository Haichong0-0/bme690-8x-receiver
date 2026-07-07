"""Visual sanity checks the plan explicitly calls for before trusting Stage 3's
labels ("validate the exponential fit visually against a real decay curve").
Renders one PNG per (run, sensor): the cleaned mean log-resistance curve,
colour-coded by detected phase, with the resulting y_conc curve on a twin axis.
Optionally overlays a model's predicted y_conc (evaluate.py) as a third line.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

PHASE_COLOURS = {
    "warmup": "tab:gray",
    "baseline": "tab:blue",
    "rise": "tab:orange",
    "plateau": "tab:red",
    "decay": "tab:purple",
}


def plot_phase_labels(
    run_id: str,
    sensor_id: int,
    cycle_timestamps: pd.Series,
    log_resistance: np.ndarray,
    phase: np.ndarray,
    y_conc: np.ndarray,
    out_dir: Path,
    y_pred: Optional[np.ndarray] = None,
    out_suffix: str = "fit",
    title_extra: str = "",
    pred_label: str = "predicted",
) -> Path:
    """`y_pred`, if given, is a per-cycle predicted strength (NaN where
    there's no prediction, e.g. cycles that aren't a window end) — drawn as a
    dotted line alongside the dashed true `y_conc`, legended
    `strength (<pred_label>)`. `out_suffix` names the file
    (`..._<suffix>.png`); `title_extra` appends to the title (e.g. the
    per-run MAE/R²)."""
    t = (cycle_timestamps - cycle_timestamps.iloc[0]).dt.total_seconds().to_numpy()

    fig, ax1 = plt.subplots(figsize=(9, 4.5))
    for name, colour in PHASE_COLOURS.items():
        mask = phase == name
        if mask.any():
            ax1.scatter(t[mask], log_resistance[mask], s=10, color=colour, label=name)
    ax1.set_xlabel("time since run start (s)")
    ax1.set_ylabel("mean log-resistance")

    ax2 = ax1.twinx()
    valid = ~np.isnan(y_conc)
    ax2.plot(t[valid], y_conc[valid], color="black", linestyle="--", linewidth=1.2,
             label="strength (true)")
    if y_pred is not None:
        pvalid = ~np.isnan(y_pred)
        ax2.plot(t[pvalid], y_pred[pvalid], color="tab:green", linestyle=":", linewidth=1.8,
                 label=f"strength ({pred_label})")
    ax2.set_ylabel("strength (0-1)")
    ax2.set_ylim(-0.05, 1.05)

    title = f"{run_id}  sensor {sensor_id}"
    if title_extra:
        title += f"   {title_extra}"
    fig.suptitle(title)
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="center right", fontsize=8)
    fig.tight_layout()

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_id}_sensor{sensor_id}_{out_suffix}.png"
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path
