"""Real classify-then-regress Estimator — the swap point `dummy_ml.py`
described, now filled in with the models trained in `ML/train.py` (copied
here from `ML/models/`: `classifier.joblib`, `classifier_scaler.joblib`,
`regressor.joblib`, `regressor_scaler.joblib`, `metadata.json`).

Server/ has no runtime dependency on ML/ (per ML/README.md's own boundary:
"nothing in Server/ imports back into ML/") — it should be deployable on its
own once the models are copied over. So the feature extraction here
deliberately duplicates the *causal* subset of ML/smell_ml/'s offline
pipeline (cycle detection, log-transform, windowing) rather than importing
it. Two real differences from how the models were trained, both because
training used information a live stream doesn't have yet:

  1. No Stage 1 Butterworth filtering. `filtfilt` is zero-phase and needs
     future samples; that's fine for offline batch cleaning but not for a
     causal per-cycle feed. The trained models' winning config used
     lowpass=ON, so live classifier accuracy is expected to sit closer to the
     lowpass=OFF sweep number (0.746) than the shipped 0.755 — see
     ML/EXPERIMENTS.md. A causal one-pass IIR (`scipy.signal.lfilter` with
     the same Butterworth coefficients) would close some of this gap if it
     matters in practice; not implemented here yet.
  2. No phase detection (baseline/rise/plateau/decay). That's a whole-run
     shape analysis (find the widest stable high/low regions) that isn't
     computable causally — a live stream doesn't know if "now" is the
     plateau until it's seen the rest of the run. So the classifier here
     runs on *every* completed cycle, not just plateau cycles the way it was
     trained. This is the more significant train/serve mismatch and was the
     cause of live lemon<->grapefruit confusion: a lemon run's clean-air
     baseline cycles were classified as grapefruit ~100% of the time
     (accuracy 0.33 below strength 0.2 vs 0.71 above 0.8, pooled over the
     training runs). Mitigated by the **strength gate** in `infer()`: the
     regressor's predicted strength is a causal proxy for "is an odour
     present now", so when it's below `strength_gate` (default 0.6) the odour
     confidence is forced to 0 — the classifier's output is only trusted on
     the near-plateau cycles it was trained on. UPDATE: the "fuller fix" this
     anticipated is now the deployed default — the 4-class **"detect"**
     classifier (ML/train.py --classifier-phase-filter detect) has an explicit
     clean-air **"none"** class and trains the odour classes across a
     concentration range, so it rejects clean air itself and identifies odour
     below the plateau. When a none-class model is loaded, `infer()` reports the
     best non-none odour and the strength gate drops to a low backstop (0.15).
     The description above is retained for the legacy plateau-only model.

The regression side has no such mismatch: `windowing.py`'s "last N cycles,
label = the most recent cycle's y_conc" was already a causal design, so
`RealEstimator`'s rolling window mirrors it exactly.

Multi-sensor combination: predictions from the 4 HP354 sensors (1/3/5/7) are
averaged (classifier: mean predict_proba before argmax; regressor: mean
prediction). Training/evaluation treated each sensor's cycles as independent
samples and never tested this ensemble explicitly — untested but a
reasonable default, since replicate physical sensors averaging out
independent noise is a standard expectation.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

import joblib
import numpy as np
import pandas as pd

from bme690_viz import CsvTail
from bmeconfig_to_profile import convert as convert_bmeconfig
from dummy_ml import Estimator, Inference, ODOURS

THIS_DIR = Path(__file__).resolve().parent
DEFAULT_MODELS_DIR = THIS_DIR / "models"
DEFAULT_BMECONFIG = THIS_DIR / "Sample.bmeconfig"
N_STEPS = 10
HP354_ID_SUBSTRING = "354"
# A sensor that hasn't delivered a new cycle in this long is dropped from
# inference (and logged) rather than silently averaged in on stale data —
# real cycles land every ~8-11s on real captures, so this is a generous margin.
DEFAULT_STALE_TIMEOUT_S = 60.0
# The classifier can carry a dedicated clean-air "none" class (the 4-class
# "detect" model, ML/train.py --classifier-phase-filter detect). When it does,
# it rejects no-odour itself, so the strength gate is only a light backstop.
NONE_LABEL = "none"
# Strength gate: below this predicted strength the odour label is suppressed
# (confidence forced to 0) so the XR visual shows "no smell" instead of a bogus
# odour. Two regimes:
#   * Legacy PLATEAU-only classifier (no "none" class): it emits confident-but-
#     meaningless labels on low-strength cycles (clean air read as grapefruit
#     ~100% of the time), so the gate is the PRIMARY clean-air guard -> 0.6.
#   * DETECT classifier (has a "none" class): the model rejects clean air
#     itself, and a high gate would defeat the point (identifying odour below
#     the plateau), so the gate is a low backstop -> 0.15.
# strength_gate=None (the default) auto-selects based on the loaded model.
DEFAULT_STRENGTH_GATE = 0.6
DEFAULT_STRENGTH_GATE_WITH_NONE = 0.15


def hp354_sensor_indices(bmeconfig_path: Path = DEFAULT_BMECONFIG) -> List[int]:
    """Mirrors ML/smell_ml/grid.py's function of the same name — duplicated,
    not imported, per this module's docstring."""
    result = convert_bmeconfig(bmeconfig_path)
    return sorted(
        sc["sensor_index"] for sc in result["sensor_configs"]
        if sc["heater_id"] and HP354_ID_SUBSTRING in sc["heater_id"]
    )


class RealEstimator(Estimator):
    """Loads the trained classifier + regressor and turns buffered sensor
    cycles into an `Inference`. Buffers are empty until something calls
    `push_cycle` — until then `infer()` returns a neutral/idle reading rather
    than guessing, so a not-yet-connected live feed doesn't broadcast
    confident nonsense.

    Logs sensor status at state *transitions* (filling -> ready -> stale),
    not every tick — `infer()` is typically called at the broadcast rate
    (~5 Hz), so per-call logging would drown the terminal. Pass
    `verbose=True` for a line on every `push_cycle` too (per-cycle mean
    log-resistance), useful for eyeballing whether a sensor's readings look
    sane while debugging."""

    def __init__(
        self,
        models_dir: Path = DEFAULT_MODELS_DIR,
        verbose: bool = False,
        stale_timeout_s: float = DEFAULT_STALE_TIMEOUT_S,
        strength_gate: Optional[float] = None,
    ) -> None:
        self.classifier = joblib.load(models_dir / "classifier.joblib")
        self.classifier_scaler = joblib.load(models_dir / "classifier_scaler.joblib")
        self.regressor = joblib.load(models_dir / "regressor.joblib")
        self.regressor_scaler = joblib.load(models_dir / "regressor_scaler.joblib")
        meta = json.loads((models_dir / "metadata.json").read_text())
        self.window: int = meta["window_size_cycles"]
        # Which feature transform the deployed classifier was trained with, so
        # the live path matches it exactly (see `_classifier_features`). Older
        # metadata predates this key -> raw 10 steps, the original default.
        self.classifier_features: str = meta.get("classifier_features", "raw")
        # Does the classifier have a dedicated clean-air "none" class? If so it
        # detects no-odour itself (see infer()) and the strength gate is only a
        # low backstop; otherwise the gate is the primary clean-air guard.
        self._classes = [str(c) for c in self.classifier.classes_]
        self.has_none = NONE_LABEL in self._classes
        if strength_gate is None:
            strength_gate = (DEFAULT_STRENGTH_GATE_WITH_NONE if self.has_none
                             else DEFAULT_STRENGTH_GATE)
        self.verbose = verbose
        self.stale_timeout_s = stale_timeout_s
        self.strength_gate = strength_gate
        self._buffers: Dict[int, deque] = {}
        self._last_push: Dict[int, float] = {}
        self._sensor_state: Dict[int, str] = {}  # sensor_id -> "filling" | "ready" | "stale"
        self._inference_active = False

    def push_cycle(self, sensor_id: int, step_log_resistance: np.ndarray) -> None:
        """Feed one completed heater-profile cycle's log-resistance vector
        (shape (10,), one value per heater step) for a given HP354 sensor.
        Called once per completed cycle, per sensor, by whatever feeds this
        estimator live data (today: `replay_into` below; eventually:
        bme690_receiver.py's live capture loop)."""
        vec = np.asarray(step_log_resistance, dtype=np.float32)
        if vec.shape != (N_STEPS,):
            raise ValueError(f"expected a ({N_STEPS},) step vector, got shape {vec.shape}")
        buf = self._buffers.setdefault(sensor_id, deque(maxlen=self.window))
        buf.append(vec)
        self._last_push[sensor_id] = time.monotonic()
        if self.verbose:
            print(f"# real_ml: sensor {sensor_id} cycle buffered ({len(buf)}/{self.window})  "
                  f"mean_log_r={float(vec.mean()):.3f}  range=[{float(vec.min()):.3f}, "
                  f"{float(vec.max()):.3f}]", file=sys.stderr)

    def _sensor_readiness(self) -> Dict[int, deque]:
        """Update per-sensor state (filling/ready/stale), logging on every
        transition, and return the buffers currently usable for inference."""
        now = time.monotonic()
        ready: Dict[int, deque] = {}
        for sid, buf in self._buffers.items():
            is_full = len(buf) == self.window
            is_fresh = now - self._last_push.get(sid, 0.0) <= self.stale_timeout_s
            state = "ready" if (is_full and is_fresh) else ("stale" if is_full else "filling")
            prev = self._sensor_state.get(sid)
            if state != prev:
                if state == "ready" and self.verbose:
                    # routine positive transition — verbose only
                    print(f"# real_ml: sensor {sid} ready ({self.window} cycles buffered) "
                          f"— now contributing to inference", file=sys.stderr)
                elif state == "stale":
                    # a sensor going quiet mid-run IS abnormal — always surface it
                    print(f"# real_ml: sensor {sid} stale — no new cycle in "
                          f">{self.stale_timeout_s:g}s, dropped from inference", file=sys.stderr)
                self._sensor_state[sid] = state
            if state == "ready":
                ready[sid] = buf
        return ready

    def _classifier_features(self, step_vector: np.ndarray) -> np.ndarray:
        """Apply the SAME feature transform the deployed classifier was trained
        with (`metadata.json`'s "classifier_features") to one cycle's 10-step
        log-resistance vector. Mirrors ML/train.load_classification_data so the
        live features line up with the scaler/model exactly.

        The gradient (np.diff across steps) is the derivative of the
        temperature sweep — in log-resistance a log response-ratio, the
        shape/selectivity feature that lifted LORO accuracy 0.755 -> 0.799 and
        near-eliminated lemon misclassification (see ML/EXPERIMENTS.md). Only
        the level-based sets are implemented here; "temp_contrast" isn't
        (would need porting temperature_contrast_features) — it's not deployed."""
        steps = np.asarray(step_vector, dtype=np.float32).reshape(1, -1)  # (1, N_STEPS)
        f = self.classifier_features
        if f == "raw":
            return steps
        if f == "gradient":
            return np.diff(steps, axis=1)
        if f == "raw_gradient":
            return np.concatenate([steps, np.diff(steps, axis=1)], axis=1)
        raise ValueError(
            f"real_ml doesn't implement live classifier_features={f!r} "
            "(supports raw/gradient/raw_gradient). Retrain with a supported set "
            "or add the transform here to match ML/train.load_classification_data.")

    def infer(self, scan=None, *, t: Optional[float] = None) -> Inference:
        ready = self._sensor_readiness()

        if not ready:
            if self._inference_active:
                # was producing real readings, now has none — abnormal, always show
                print("# real_ml: no sensors ready — reporting idle "
                      "(odour_confidence=0, intensity=0)", file=sys.stderr)
                self._inference_active = False
            return Inference(odour=ODOURS[0], odour_confidence=0.0, intensity=0.0)

        if not self._inference_active:
            if self.verbose:  # routine positive transition — verbose only
                print(f"# real_ml: inference active — {len(ready)}/{len(self._buffers)} "
                      f"sensor(s) ready", file=sys.stderr)
            self._inference_active = True

        clf_probs = []
        for buf in ready.values():
            latest = self._classifier_features(buf[-1])
            scaled = self.classifier_scaler.transform(latest)
            clf_probs.append(self.classifier.predict_proba(scaled)[0])
        mean_proba = np.mean(clf_probs, axis=0)
        classes = self.classifier.classes_
        if self.has_none:
            # 4-class "detect" model: report the best *real* odour (never
            # "none") and use P(that odour) as the confidence. When "none"
            # dominates (clean air, or too faint to identify) that probability
            # is low, so the XR visual's confidence gate hides it — the
            # classifier now does the no-odour detection the strength gate used
            # to stand in for.
            odour_idx = [i for i, c in enumerate(self._classes) if c != NONE_LABEL]
            best_idx = odour_idx[int(np.argmax(mean_proba[odour_idx]))]
        else:
            best_idx = int(np.argmax(mean_proba))
        odour = str(classes[best_idx])
        confidence = float(mean_proba[best_idx])

        reg_preds = []
        for buf in ready.values():
            stacked = np.stack(buf)  # (window, N_STEPS)
            cycle_means = stacked.mean(axis=1)
            trend_slope = np.polyfit(np.arange(self.window), cycle_means, 1)[0]
            trend_diff_last = cycle_means[-1] - cycle_means[-2]
            feats = np.concatenate(
                [stacked.flatten(), [trend_slope, trend_diff_last]]
            ).reshape(1, -1).astype(np.float32)
            scaled = self.regressor_scaler.transform(feats)
            reg_preds.append(self.regressor.predict(scaled)[0])
        intensity = float(np.clip(np.mean(reg_preds), 0.0, 1.0))

        # Strength gate: zero the confidence when the regressor says almost no
        # odour is present, so the XR visual shows "no smell" rather than a bogus
        # odour. For a "detect" classifier this is a low backstop (0.15) behind
        # the model's own "none" class; for a legacy plateau-only classifier it
        # is the primary clean-air guard (0.6). See DEFAULT_STRENGTH_GATE.
        gated = intensity < self.strength_gate
        if gated:
            confidence = 0.0

        if self.verbose:
            gate_note = f"  [gated: strength<{self.strength_gate:g}]" if gated else ""
            print(f"# real_ml: infer -> odour={odour} confidence={confidence:.3f} "
                  f"intensity={intensity:.3f}  ({len(ready)} sensor(s)){gate_note}", file=sys.stderr)

        return Inference(
            odour=odour,
            odour_confidence=round(confidence, 4),
            intensity=round(intensity, 4),
        )


def _extract_cycles(df: pd.DataFrame, sensor_index: int) -> List[tuple]:
    """[(cycle_end_time, log-resistance step vector)] for one sensor,
    time-ordered. Cycle boundary = heater_profile_step_index wraparound —
    the same rule ML/smell_ml/grid.py's real-data investigation established
    (`scanning_cycle_index` does NOT mark cycle boundaries), duplicated here
    per this module's docstring.

    Within-cycle gap handling differs from training on purpose: training
    imputes a missing step from the *next* cycle's same step too (Stage 1),
    which needs data this cycle doesn't have yet when running causally. Here
    a missing step is linearly interpolated from this cycle's own other
    steps only."""
    s = df[df["sensor_index"] == sensor_index].sort_values("time").reset_index(drop=True)
    step = s["heater_profile_step_index"].to_numpy()
    if len(step) == 0:
        return []
    wrapped = np.concatenate([[False], step[1:] < step[:-1]])
    cycle_id = np.cumsum(wrapped)
    resistance = s["resistance_gassensor"].to_numpy()
    times = s["time"]

    cycles = []
    for cid in np.unique(cycle_id):
        mask = cycle_id == cid
        steps_in_cycle = step[mask]
        if len(steps_in_cycle) < N_STEPS - 1:
            continue  # ragged partial cycle (run start/end) — skip, matches grid.raw_grid
        vec = np.full(N_STEPS, np.nan)
        for si, r in zip(steps_in_cycle, resistance[mask]):
            if 0 <= si < N_STEPS:
                vec[si] = r
        nan_mask = np.isnan(vec)
        if nan_mask.any():
            valid_idx = np.where(~nan_mask)[0]
            if len(valid_idx) < 2:
                continue
            vec[nan_mask] = np.interp(np.where(nan_mask)[0], valid_idx, vec[valid_idx])
        cycles.append((times[mask].max(), np.log(vec).astype(np.float32)))
    return cycles


def build_replay_events(csv_path: Path, sensor_indices: List[int]) -> List[tuple]:
    """[(seconds_since_run_start, sensor_id, log-resistance step vector)],
    time-ordered across all sensors — the event stream `replay_into` feeds
    into a RealEstimator at (a multiple of) real pace."""
    df = pd.read_csv(csv_path, parse_dates=["time"])
    t0 = df["time"].min()
    events = []
    for sid in sensor_indices:
        for end_time, vec in _extract_cycles(df, sid):
            events.append(((end_time - t0).total_seconds(), sid, vec))
    events.sort(key=lambda e: e[0])
    return events


async def replay_into(
    estimator: RealEstimator, csv_path: Path, sensor_indices: List[int], speed: float = 1.0,
    verbose: bool = False,
) -> None:
    """Stands in for a live SPI feed by replaying a captured CSV's cycles
    into `estimator.push_cycle` at (a multiple of) their original real-world
    pace. Use this until bme690_receiver.py's live capture is wired directly
    into ws_publisher.py — see CLAUDE.md's "Gap vs plan" note.

    Routine start/progress/finish lines are `verbose`-only — normal output is
    just the packet stream the estimator drives."""
    events = build_replay_events(csv_path, sensor_indices)
    n = len(events)
    if verbose:
        print(f"# real_ml: replaying {n} cycles from {csv_path.name} "
              f"(speed={speed:g}x)", file=sys.stderr)
    progress_step = max(1, n // 10)
    prev_t = 0.0
    for i, (t, sensor_id, vec) in enumerate(events):
        wait = (t - prev_t) / speed
        if wait > 0:
            await asyncio.sleep(wait)
        prev_t = t
        estimator.push_cycle(sensor_id, vec)
        if verbose and ((i + 1) % progress_step == 0 or i + 1 == n):
            print(f"# real_ml: replay progress {i + 1}/{n} cycles "
                  f"({100 * (i + 1) / n:.0f}%)", file=sys.stderr)
    if verbose:
        print(f"# real_ml: replay of {csv_path.name} finished", file=sys.stderr)


class LiveCycleFeeder:
    """Incrementally builds per-sensor cycles from a *growing* CSV — the one
    bme690_receiver.py is actively writing — and pushes each one into a
    RealEstimator as soon as it completes. This is the real live-data path
    (`replay_into` above is a stand-in for when there's no live capture
    running).

    Reuses `bme690_viz.CsvTail` for the byte-position file-tail itself (same
    file, same auto-newest-file discovery, same already-fixed UTF-8-boundary
    safety) — this script and bme690_viz.py both just read whatever
    bme690_receiver.py writes, per its own docstring: "the contract... is the
    CSV file, nothing else." Cycle detection (step-index wraparound) mirrors
    `_extract_cycles`/`build_replay_events` above, but incrementally: a
    partial cycle's steps accumulate across polls and only get finalised
    (interpolated + pushed) once a wraparound proves the cycle is done.

    `explicit_path`, when given, pins the tail to that exact file instead of
    globbing for the newest `bme690_receiver_*.csv` in `data/`. Matters when
    ws_publisher.py spawns the receiver itself (--auto-receiver): glob
    discovery would happily lock onto an unrelated *older* capture still
    sitting in data/ if the freshly-spawned receiver hasn't written its file
    yet (or never does, e.g. it fails to find hardware) — confirmed live:
    it silently fed a dead ~40s-old session into the estimator while the new
    receiver process had already exited. Pinning to the exact path the
    subprocess was told to use (see ws_publisher.py's --auto-receiver
    handling) makes that impossible: nothing streams until that specific
    file exists and grows."""

    def __init__(self, estimator: RealEstimator, sensor_indices: List[int],
                 patterns=CsvTail.DEFAULT_PATTERNS, explicit_path: Optional[Path] = None,
                 verbose: bool = False) -> None:
        self.estimator = estimator
        self.sensor_indices = set(sensor_indices)
        self.tail = CsvTail(patterns=patterns, explicit=explicit_path)
        self.verbose = verbose
        self._partial: Dict[int, Dict[int, float]] = {}
        self._last_step: Dict[int, int] = {}

    def _finalize(self, sensor_id: int) -> None:
        partial = self._partial.get(sensor_id)
        if not partial or len(partial) < N_STEPS - 1:
            return  # too ragged (matches build_replay_events' N_STEPS-1 minimum)
        vec = np.full(N_STEPS, np.nan)
        for si, r in partial.items():
            if 0 <= si < N_STEPS:
                vec[si] = r
        nan_mask = np.isnan(vec)
        if nan_mask.any():
            valid_idx = np.where(~nan_mask)[0]
            if len(valid_idx) < 2:
                return
            vec[nan_mask] = np.interp(np.where(nan_mask)[0], valid_idx, vec[valid_idx])
        self.estimator.push_cycle(sensor_id, np.log(vec).astype(np.float32))

    def process_rows(self, rows: List[dict]) -> None:
        for row in rows:
            try:
                sensor_id = int(row["sensor_index"])
            except (KeyError, ValueError):
                continue
            if sensor_id not in self.sensor_indices:
                continue
            try:
                step = int(row["heater_profile_step_index"])
                resistance = float(row["resistance_gassensor"])
            except (KeyError, ValueError):
                continue
            last = self._last_step.get(sensor_id)
            if last is not None and step < last:
                self._finalize(sensor_id)
                self._partial[sensor_id] = {}
            self._partial.setdefault(sensor_id, {})[step] = resistance
            self._last_step[sensor_id] = step

    async def run(self, poll_interval_s: float = 1.0) -> None:
        """Poll for new rows every `poll_interval_s` — real cycles land every
        ~8-11s on real captures, so polling faster than that just wastes CPU;
        this default catches a completed cycle within a second or two of it
        landing. Routine tail-status lines are `verbose`-only."""
        if self.verbose:
            print(f"# real_ml: live-tailing {self.tail.patterns} for a growing "
                  f"bme690_receiver capture...", file=sys.stderr)
        while True:
            switched, rows = self.tail.poll()
            if switched:
                if self.verbose:
                    print(f"# real_ml: live-tailing {self.tail.path}", file=sys.stderr)
                self._partial.clear()
                self._last_step.clear()
            if rows:
                self.process_rows(rows)
            await asyncio.sleep(poll_interval_s)
