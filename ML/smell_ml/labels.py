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
with consistent timing across the 12 captures regardless of odour (the 3
newer "v2" lemon runs extend the recovery tail with a longer decay).
`detect_phases` recovers that structure directly from the curve instead of
relying on tags that were never populated.

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


def mean_log_resistance(grid: pd.DataFrame) -> np.ndarray:
    """The curve-fit target: mean log-resistance across the 10 steps per
    cycle. An empirical simplification (documented in ML/README.md) —
    averaging across steps of very different heater temperatures rather than
    picking one 'best' step, since no labelled ground truth exists yet to
    justify preferring a specific step."""
    step_cols = [f"step_{i}" for i in range(10)]
    return grid[step_cols].mean(axis=1).to_numpy()


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

    # Anchor the exposure trough on the maximum DRAWDOWN below the running
    # maximum (cummax - value), not the global minimum. Two shapes seen on real
    # captures break a global-extremum anchor, both visible in the longer-decay
    # "v2" lemon runs:
    #   * the sensor's cold-start WARMUP can read lower than peak exposure, so
    #     the global minimum is the warmup, not the exposure trough;
    #   * a long recovery/decay tail can climb back ABOVE the pre-exposure
    #     baseline, so the global maximum is the tail, not the baseline — which
    #     silently mislabelled whole runs (exposure tagged "warmup", no
    #     rise/plateau/decay at all).
    # Drawdown peaks at the exposure trough regardless of how low the warmup
    # dips (its running max is still small there) or how high the tail climbs.
    drawdown = np.maximum.accumulate(values) - values
    trough_idx = int(np.argmax(drawdown))
    low_mask = values <= (values[trough_idx] + tol)
    t_lo, t_hi = trough_idx, trough_idx
    while t_lo > 0 and low_mask[t_lo - 1]:
        t_lo -= 1
    while t_hi < n - 1 and low_mask[t_hi + 1]:
        t_hi += 1
    plateau = slice(t_lo, t_hi + 1)

    # Baseline = widest stable high region BEFORE the trough (pre-exposure clean
    # air). Restricting the search to [0, t_lo) keeps the recovery tail out of
    # the baseline even when that tail is the global maximum.
    pre = values[:t_lo]
    if len(pre) == 0:
        # trough at the very start — no pre-exposure baseline captured
        empty = slice(0, 0)
        return PhaseSegments(warmup=empty, baseline=empty, rise=empty,
                             plateau=plateau, decay=slice(t_hi + 1, n))
    peak_idx = int(np.argmax(pre))
    high_mask = pre >= (pre[peak_idx] - tol)
    lo, hi = peak_idx, peak_idx
    while lo > 0 and high_mask[lo - 1]:
        lo -= 1
    while hi < len(pre) - 1 and high_mask[hi + 1]:
        hi += 1
    baseline = slice(lo, hi + 1)

    return PhaseSegments(
        warmup=slice(0, lo),
        baseline=baseline,
        rise=slice(hi + 1, t_lo),
        plateau=plateau,
        decay=slice(t_hi + 1, n),
    )


def label_run_by_shape(
    cycle_timestamps: pd.Series, values: np.ndarray, min_segment_cycles: int = 4
) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Full Stage 3 pipeline for one (run, sensor): detect phases from curve
    shape, then label every cycle's strength. Returns (phase, y_conc,
    fit_diagnostics).

    Strength is read off LINEARLY between the two observed, stable levels the
    rise spans — the clean-air baseline (strength 0) and the peak-exposure
    plateau (strength 1): each cycle's strength is where its mean log-resistance
    sits between those levels, clipped to [0, 1]. This replaces the per-segment
    exponential fit as the LABEL source. That fit extrapolated the decay
    asymptote whenever a capture ended before the sensor recovered (the
    `asymptote_unreliable` fits), distorting exactly the low-strength tail we
    care about; anchoring to the actually-observed baseline level is
    extrapolation-free and measurably improved both the decay labels and the
    strength regressor (LORO R^2 0.83 -> 0.89 — see ML/EXPERIMENTS.md).

    Only `phase == 'warmup'` cycles (and degenerate runs with no detectable
    baseline or plateau) are left y_conc = NaN — callers drop those before
    windowing. The rise/decay exponential fits are still computed but now ONLY
    for diagnostics (tau = recovery time constant, R^2, and the
    `asymptote_unreliable` flag); they no longer set the label."""
    seg = detect_phases(values)
    n = len(values)
    phase = np.full(n, "warmup", dtype=object)
    y_conc = np.full(n, np.nan)
    fits = []

    phase[seg.baseline] = "baseline"
    phase[seg.rise] = "rise"
    phase[seg.plateau] = "plateau"
    phase[seg.decay] = "decay"

    # Rise-anchored linear labels: read strength off between the observed
    # baseline and plateau levels (the stable regions the rise connects), for
    # every non-warmup cycle at once. Warmup stays NaN — its low log-resistance
    # is the cold-start transient, not exposure, so it must not read as strength.
    base_vals, plat_vals = values[seg.baseline], values[seg.plateau]
    baseline_level = float(base_vals.mean()) if base_vals.size else np.nan
    plateau_level = float(plat_vals.mean()) if plat_vals.size else np.nan
    span = baseline_level - plateau_level
    if np.isfinite(span) and span > 1e-6:
        non_warmup = phase != "warmup"
        y_conc[non_warmup] = np.clip((baseline_level - values[non_warmup]) / span, 0.0, 1.0)

    for name, sl in [("rise", seg.rise), ("decay", seg.decay)]:
        idx = np.arange(n)[sl]
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
        duration_s = (seg_ts.iloc[-1] - seg_ts.iloc[0]).total_seconds()
        # tau >> the observed segment duration means the exponential barely
        # curves within what we saw, so its asymptote is extrapolated. This no
        # longer affects the label (now level-based), but it's still a useful
        # "the capture ended before the sensor levelled off" diagnostic flag.
        asymptote_unreliable = duration_s > 0 and fit.tau > 3 * duration_s
        fits.append({
            "phase": name, "r_squared": fit.r_squared, "tau_s": fit.tau,
            "n_cycles": fit.n_cycles, "duration_s": duration_s,
            "asymptote_unreliable": asymptote_unreliable,
        })

    return phase, y_conc, fits
