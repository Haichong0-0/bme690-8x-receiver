"""Placeholder ML for the XR smell pipeline.

This is the swap point for the real classify-then-regress model (plan.md §1).
It exists so the *publisher*, the *WebSocket server*, and the *Unity client*
can all be built and tested end-to-end before the real model is ready.

The contract is deliberately tiny and stable, so swapping in real inference is
a one-class change with no ripple into `ws_publisher.py`:

    estimator = DummyEstimator()          # later: RealEstimator(model_path=...)
    result = estimator.infer()            # -> Inference(odour, confidence, intensity)

A real implementation would take sensor scan vectors as input
(`infer(scan: ...)`) and return the same `Inference` dataclass. The publisher
only depends on that return type.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# The odour classes the system reports (plan.md §3). Extend here when the real
# classifier gains labels.
ODOURS = ("lemon", "grapefruit", "lavender")


@dataclass(frozen=True)
class Inference:
    """One model output. Mirrors the data half of the plan.md §3 packet."""

    odour: str
    odour_confidence: float  # 0..1
    intensity: float  # 0..1
    # Host-side only — NOT part of the plan.md §3 wire packet. Lets the publisher
    # log the real state instead of a placeholder odour:
    #   "ok"          — a genuine classification (odour/confidence are real)
    #   "warming_up"  — still capturing the live clean-air baseline, so odour is
    #                   a placeholder (ODOURS[0]) and confidence is 0
    #   "idle"        — no sensor delivering fresh cycles
    status: str = "ok"


class Estimator:
    """Interface the publisher depends on. Implemented by Dummy/Real estimators."""

    def infer(self, scan=None) -> Inference:  # noqa: D401 - simple stub
        raise NotImplementedError


class DummyEstimator(Estimator):
    """Simulates a believable live signal without any sensor or model.

    Behaviour (purely for wiring/visual testing):
      * Cycles through ``ODOURS`` every ``odour_period_s`` seconds.
      * Ramps ``intensity`` up and down as a smooth triangle wave so the Unity
        side has something obvious to map to scale/opacity/emission.
      * Reports a high, mildly wobbling ``odour_confidence``.

    ``infer`` ignores any ``scan`` argument — it's accepted only so the call
    site matches the real estimator's signature.
    """

    def __init__(
        self,
        odour_period_s: float = 6.0,
        intensity_period_s: float = 4.0,
    ) -> None:
        self.odour_period_s = odour_period_s
        self.intensity_period_s = intensity_period_s

    def infer(self, scan=None, *, t: float | None = None) -> Inference:
        """Return a simulated inference.

        ``t`` is the elapsed time in seconds used to drive the waveforms. The
        caller passes a monotonic clock value; we avoid reading the clock here
        so the function stays pure and easy to test.
        """
        if t is None:
            t = 0.0

        # Which odour: step through the list once per odour_period_s.
        idx = int(t // self.odour_period_s) % len(ODOURS)
        odour = ODOURS[idx]

        # Intensity: triangle wave in [0, 1] over intensity_period_s.
        phase = (t % self.intensity_period_s) / self.intensity_period_s
        intensity = 1.0 - abs(2.0 * phase - 1.0)

        # Confidence: high, with a small sinusoidal wobble, clamped to [0, 1].
        confidence = 0.9 + 0.08 * math.sin(2.0 * math.pi * t / 3.0)
        confidence = max(0.0, min(1.0, confidence))

        return Inference(
            odour=odour,
            odour_confidence=round(confidence, 4),
            intensity=round(intensity, 4),
        )


if __name__ == "__main__":
    # Quick visual sanity check: print a few seconds of dummy inferences.
    est = DummyEstimator()
    for i in range(20):
        t = i * 0.5
        print(f"t={t:5.1f}s  {est.infer(t=t)}")
