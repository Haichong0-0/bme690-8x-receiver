"""Stage 3: classification + strength labels.

Strength labelling is the part of this pipeline that doesn't exist in
the honey paper (their labels were fixed at collection time) — it's specific
to this project's gradient-based design.

Deviation from the plan's design, and why: the plan expects phases
(baseline/rise/plateau/decay/purge) to come from the collection board's
labelling buttons via `label_tag`. Every real capture in data/raw/ so
far has label_tag == 0 throughout (buttons unused) — but plotting the raw
signal (see ML/data/diagnostics/*.png) shows the phase structure is very much
*present in the curve shape itself*: every run traces baseline (wide, stable,
high resistance) -> rise -> plateau (wide, stable, low resistance) -> decay,
with impressively consistent timing across all 9 captures regardless of
odour. `detect_phases` recovers that structure directly from the curve
instead of relying on tags that were never populated.

Resistance/strength direction is inverted from what the phase names
suggest: BME690 (MOx) resistance drops under exposure to reducing-gas VOCs
(citrus terpenes qualify) and recovers on clearance. Confirmed on real data —
the low, wide, stable region always follows the high, wide, stable region in
time, matching an "exposure introduced, then vented" protocol. So the
HIGH-resistance plateau is clean-air baseline (strength 0) and the
LOW-resistance trough is peak exposure (strength 1) — the assignment is
made in `detect_phases` from which region is *wider/more stable*, and which
comes *first*, not from raw resistance magnitude.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit


@dataclass
class ConcentrationFit:
    r_t0: float      # fitted log-resistance at t=0 of the fitted segment
    r_inf: float      # fitted asymptotic log-resistance as t -> infinity
    tau: float        # time constant, seconds
    r_squared: float
    n_cycles: int


@dataclass
class PhaseSegments:
    warmup: slice     # excluded from labels — heater/thermal warm-up transient, not odour-related
    baseline: slice   # clean-air plateau, y_conc = 0.0
    rise: slice        # strength rising (resistance falling), baseline -> plateau
    plateau: slice     # exposure hold, y_conc = 1.0
    decay: slice       # strength falling (resistance rising), plateau -> baseline (may be incomplete at run end)


def _exp_approach(t: np.ndarray, r_t0: float, r_inf: float, tau: float) -> np.ndarray:
    """Generic exponential approach from r_t0 (at t=0) toward asymptote r_inf.
    Direction-agnostic: works whether r_inf > r_t0 (rising) or < r_t0 (decaying)."""
    return r_inf - (r_inf - r_t0) * np.exp(-t / tau)


def fit_concentration_curve(cycle_timestamps: pd.Series, log_resistance: np.ndarray) -> ConcentrationFit:
    t = (cycle_timestamps - cycle_timestamps.iloc[0]).dt.total_seconds().to_numpy()
    r_t0_0, r_inf0 = log_resistance[0], log_resistance[-1]
    tau0 = max(t[-1] / 3, 1.0)
    try:
        popt, _ = curve_fit(
            _exp_approach, t, log_resistance,
            p0=[r_t0_0, r_inf0, tau0],
            maxfev=10000,
        )
    except RuntimeError as e:
        raise RuntimeError(f"exponential fit did not converge: {e}") from e

    r_t0, r_inf, tau = (float(x) for x in popt)
    fitted = _exp_approach(t, *popt)
    ss_res = float(np.sum((log_resistance - fitted) ** 2))
    ss_tot = float(np.sum((log_resistance - log_resistance.mean()) ** 2))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return ConcentrationFit(r_t0=r_t0, r_inf=r_inf, tau=tau,
                             r_squared=r_squared, n_cycles=len(t))


def concentration_from_fit(
    log_resistance: np.ndarray, fit: ConcentrationFit, conc_at_t0: float, conc_at_inf: float
) -> np.ndarray:
    """Evaluate strength at each cycle, given which anchor (t=0 vs
    asymptote) corresponds to which strength for this segment
    (rise: 0.0 -> 1.0; decay: 1.0 -> 0.0)."""
    if fit.r_inf == fit.r_t0:
        return np.full_like(log_resistance, conc_at_t0)
    progress = (log_resistance - fit.r_t0) / (fit.r_inf - fit.r_t0)  # 0 at t0, 1 at asymptote
    conc = conc_at_t0 + (conc_at_inf - conc_at_t0) * progress
    return np.clip(conc, 0.0, 1.0)


def mean_log_resistance(grid: pd.DataFrame) -> np.ndarray:
    """The curve-fit target: mean log-resistance across the 10 steps per
    cycle. An empirical simplification (documented in ML/README.md) —
    averaging across steps of very different heater temperatures rather than
    picking one 'best' step, since no labelled ground truth exists yet to
    justify preferring a specific step."""
    step_cols = [f"step_{i}" for i in range(10)]
    return grid[step_cols].mean(axis=1).to_numpy()


def _longest_run(mask: np.ndarray) -> tuple[int, int]:
    """[start, end) of the longest contiguous True run in `mask`."""
    best_start = best_len = cur_start = cur_len = 0
    for i, v in enumerate(mask):
        if v:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
            if cur_len > best_len:
                best_start, best_len = cur_start, cur_len
        else:
            cur_len = 0
    return best_start, best_start + best_len


def detect_phases(values: np.ndarray, tol_fraction: float = 0.1) -> PhaseSegments:
    """Segment one run's mean log-resistance curve by shape (see module
    docstring). `tol_fraction` sets how far from the plateau/trough extremum
    a point can be and still count as part of it, as a fraction of the run's
    total value range — 0.1 chosen by inspecting real captures, where the
    stable regions are flat to within a couple % of the range and the
    transitions are comparatively steep."""
    n = len(values)
    value_range = values.max() - values.min()
    tol = tol_fraction * value_range if value_range > 0 else 0.0

    peak_idx = int(np.argmax(values))
    high_mask = values >= (values[peak_idx] - tol)
    lo, hi = peak_idx, peak_idx
    while lo > 0 and high_mask[lo - 1]:
        lo -= 1
    while hi < n - 1 and high_mask[hi + 1]:
        hi += 1
    baseline = slice(lo, hi + 1)

    tail_start = hi + 1
    if tail_start >= n:
        # plateau runs to the end of the file — no decay/exposure observed at all
        empty = slice(tail_start, tail_start)
        return PhaseSegments(warmup=slice(0, lo), baseline=baseline,
                              rise=empty, plateau=empty, decay=empty)

    trough_idx = tail_start + int(np.argmin(values[tail_start:]))
    low_mask = values <= (values[trough_idx] + tol)
    t_lo, t_hi = trough_idx, trough_idx
    while t_lo > tail_start and low_mask[t_lo - 1]:
        t_lo -= 1
    while t_hi < n - 1 and low_mask[t_hi + 1]:
        t_hi += 1
    plateau = slice(t_lo, t_hi + 1)

    return PhaseSegments(
        warmup=slice(0, lo),
        baseline=baseline,
        rise=slice(tail_start, t_lo),
        plateau=plateau,
        decay=slice(t_hi + 1, n),
    )


def label_run_by_shape(
    cycle_timestamps: pd.Series, values: np.ndarray, min_segment_cycles: int = 4
) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Full Stage 3 pipeline for one (run, sensor): detect phases from curve
    shape, then label every cycle. Returns (phase, y_conc, fit_diagnostics).
    `phase[i] == 'warmup'` or a fit-too-short segment leaves y_conc[i] = NaN —
    callers should drop NaN y_conc rows before windowing for regression."""
    seg = detect_phases(values)
    n = len(values)
    phase = np.full(n, "warmup", dtype=object)
    y_conc = np.full(n, np.nan)
    fits = []

    phase[seg.baseline] = "baseline"
    y_conc[seg.baseline] = 0.0
    phase[seg.plateau] = "plateau"
    y_conc[seg.plateau] = 1.0

    for name, sl, conc_t0, conc_inf in [
        ("rise", seg.rise, 0.0, 1.0),
        ("decay", seg.decay, 1.0, 0.0),
    ]:
        idx = np.arange(n)[sl]
        phase[sl] = name
        if len(idx) < min_segment_cycles:
            if len(idx):
                fits.append({"phase": name, "n_cycles": len(idx), "skipped": "too short to fit"})
            continue
        seg_ts = cycle_timestamps.iloc[idx].reset_index(drop=True)
        try:
            fit = fit_concentration_curve(seg_ts, values[idx])
        except RuntimeError as e:
            fits.append({"phase": name, "n_cycles": len(idx), "error": str(e)})
            continue
        y_conc[idx] = concentration_from_fit(values[idx], fit, conc_t0, conc_inf)
        duration_s = (seg_ts.iloc[-1] - seg_ts.iloc[0]).total_seconds()
        # tau >> the observed segment duration means the exponential barely
        # curves within what we actually saw — the fitted asymptote (and so
        # the y_conc values derived from it) is an extrapolation, not an
        # observation. Flag it rather than silently trusting it.
        asymptote_unreliable = duration_s > 0 and fit.tau > 3 * duration_s
        fits.append({
            "phase": name, "r_squared": fit.r_squared, "tau_s": fit.tau,
            "n_cycles": fit.n_cycles, "duration_s": duration_s,
            "asymptote_unreliable": asymptote_unreliable,
        })

    return phase, y_conc, fits
