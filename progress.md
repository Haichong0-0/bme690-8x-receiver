# XR smell reconstruction — status summary

What's been built since the preprocessing plan: the odour captures collected, the pipeline that turns them into labels and features, the models trained on top, and how it now reaches the HoloLens.

**3** odours captured · **12** capture sessions · **10,785** cycles processed · **25** training experiments logged

```
BME690 sensor → bme690_receiver.py → ML/ pipeline → trained models → real_ml.py → ws_publisher.py → HoloLens 2
```

---

## Data — what was collected

Fifteen real capture sessions off the BME690 8x shuttle across three odours — `lemon` (six), `grapefruit` (six), `lavender` (three) — all at the same 0.6 concentration. The later runs (three lemon, three grapefruit, all three lavender) are **"v2" batches recorded with a longer decay tail**, held well past exposure to capture the sensor's full recovery.

> **Now matches the plan.** `plan.md` declares lemon / grapefruit / lavender, and that's what's collected. (An earlier round captured `sorange` (sweet orange) in lavender's place; those sessions were re-collected as lavender, so the pipeline's odour-vs-plan check — it discovers odours from filenames and flags any mismatch on every build — now passes cleanly.)

Each session runs ~30 minutes on the four HP354 chip-selects (sensors 1/3/5/7 — a 10-step, 100°C/200°C/320°C heater profile); the other four sensors hold a constant 320°C and are unused downstream. Looking at the raw curves turned up two things the plan didn't anticipate:

- **The CSV's own `scanning_cycle_index` column doesn't mark heater cycles** — one value can span up to three different heater passes. The real boundary is a step-index wraparound, found by inspection.
- **Every capture traces the same shape** — warm-up, then a long stable clean-air baseline, a resistance drop as the odour is introduced, a low-resistance exposure hold, then a slow climb back. On the original runs that climb often hadn't finished by the time the file ended; the v2 lemon captures deliberately record more of it, and their recovery tail can drift *back above* the starting baseline. None of this was tagged by the collection hardware (`label_tag` is unused in all twelve files) — the phase structure had to be recovered from curve shape instead.

Raw captures now live in `ML/data/raw/`. `Server/` deliberately holds none of them — it only needs the trained models, not the data that produced them.

---

## Pipeline — two pipelines, one contract

An offline one that turns captures into training data, and a live one that turns sensor readings into packets. Both were built against what the real data actually does, not the plan's first-draft assumptions.

### Offline — `ML/build_dataset.py`

Six stages, following the original preprocessing plan but corrected against real captures at each step:

| Stage | Does | Deviation |
|---|---|---|
| 1 · Clean | Impute, low-pass filter, log-transform | Filter was dropped then reinstated once the algorithm sweep showed it measurably helps |
| 2 · Segment | True cycle boundaries, HP354 filter | Boundary rule rebuilt from step-index wraparound, not the CSV's own cycle column |
| 3 · Label | Phase detection, linear baseline-to-plateau read-off → strength | Phases from curve shape (the plan assumed hardware tags that were never populated); strength now read off linearly between the clean-air baseline (0) and peak plateau (1) — extrapolation-free — instead of an exponential fit that overshot on runs ending before recovery; exposure trough anchored on maximum drawdown, robust to the v2 captures' above-baseline tail |
| 4 · Window | Sliding windows for regression | Window size tuned down from 5 to 3 cycles |
| 5 · Scale | Per-feature z-score | Applied at train time, not baked into the dataset |
| 6 · Split | Group-aware, by `run_id` | Leave-one-run-out used throughout, given only a handful of repeats per odour |

### Live — `Server/ws_publisher.py`

Running it with no flags now spawns `bme690_receiver.py` itself and live-tails the CSV it writes — the same file `bme690_viz.py` already reads, so nothing about that script changed. A background feeder detects completed cycles and pushes them into the loaded model; a fixed-rate loop reads back a classification + intensity and broadcasts it over WebSocket.

```
python ws_publisher.py                          # default: spawn receiver + tail it
python ws_publisher.py --live-tail               # receiver already running elsewhere
python ws_publisher.py --replay-csv <path>       # demo from a capture
```

---

## Models — what got trained, and how it was chosen

31 logged experiments — a preprocessing sweep, an algorithm sweep, a feature sweep, the switch to a clean-air-aware **detect** classifier with extrapolation-free strength labels, and finally **baseline-relative** features to make the classifier survive live sensor drift — before landing on the current pair.

| Metric | Value |
|---|---|
| Classifier accuracy (4-class detect SVM, baseline-relative `raw_gradient`, LORO) | **0.771** (drift-robust; `none` 100%, odour-vs-odour ~0.6) |
| Regressor R² (RandomForest, LORO) | **0.891** (MAE 0.083) |
| Regression window size | **3 cycles** |

The early sweeps were three-class and ran on plateau cycles only. There, logistic regression beat RandomForest, GradientBoosting, SVM and k-NN by a wide margin (0.755 vs. 0.581–0.611 on the raw 10-step features) — the fingerprint is close to linearly separable in this feature space, which a lower-variance model exploits better than tree ensembles with only ~700 examples per class — and adding the **temperature-sweep gradient** (the 10 steps plus their 9 step-to-step differences, the shape of the sweep rather than just its levels) lifted it to **0.799**.

But a three-class model has no way to say *"no odour"*: every clean-air cycle was forced into lemon, grapefruit or lavender. The current classifier fixes that at the root — a four-class **"detect" SVM** with an explicit clean-air `none` class, trained on odour cycles across the whole exposure (first rise through plateau) instead of plateau-only. Clean air is now correctly called `none` **~100%** of the time, and odour is identified at low concentration rather than only at full plateau. RandomForest stayed best for regression; Ridge's poor showing there confirmed the strength curve is genuinely nonlinear. Switching the strength labels from an extrapolating exponential fit to an extrapolation-free linear read-off between the clean-air baseline (0) and the peak plateau (1) lifted its LORO R² to **0.891** (MAE 0.083 on the 15-run set).

> **The honest accuracy is 0.771 — and that number *is* the drift fix.** A first cut of the detect SVM used the *absolute* log-resistance levels and scored a higher-looking **0.925** LORO, but that was leaky. Absolute resistance encodes the session (humidity, board, day), and because each odour was captured on its own day the model latched onto *which day* rather than the odour; on a live sensor at a different humidity it collapsed to a single class. Subtracting each session's own clean-air baseline (**baseline-relative**, log R/R₀) is drift-invariant and is what's deployed: the honest LORO is **0.771** — `none` is caught 100% of the time, and the residual is odour-vs-odour (~0.6), with **`lemon` now the confusion sink** (both grapefruit and lavender leak into it). This supersedes an earlier note here that baseline-subtraction "made accuracy worse, ruling out drift" — that was measured on in-distribution offline data, where there's little cross-session drift to correct; the payoff only shows on live, out-of-distribution captures, which is exactly where the raw-level model failed.

Two train/serve gaps were logged in `real_ml.py`; the second is now largely closed:

- The winning config's low-pass filter is zero-phase and needs future samples — not possible on a live stream, so live filtering is still skipped.
- Phase detection needs the whole run's shape, so the live classifier still scores *every* cycle rather than just the plateau cycles it was trained on. This used to cause live lemon↔grapefruit confusion — clean-air cycles carry no odour identity yet were labelled grapefruit ~100% of the time — patched at the time with a **strength gate** set high (0.6) so a low predicted strength suppressed the label. The four-class detect classifier now handles it at the source: its `none` class rejects clean air directly, so `real_ml.py` simply reports the best *real* odour with P(odour) as its confidence and the strength gate becomes vestigial (currently **0**, disabled for live testing; 0.15 is the design backstop). Serving the baseline-relative model also means `real_ml.py` now **estimates each sensor's clean-air baseline live** (skip 12 warmup cycles, median the next 8) and subtracts it before classifying — the live half of the drift fix. The wire contract is unchanged — `odour` is always one of the three real odours; `none` never leaves the host.

---

## Device — link to the HoloLens

`Fruit_vis/` — the Unity 6 / HoloLens 2 client — was verified end-to-end on real hardware over local Wi-Fi before any of the above existed, using simulated packets. The transport is proven; what's new is that the packets it receives can now come from the real model instead.

| File (`Fruit_vis/Assets/Scripts`) | Role |
|---|---|
| `SmellReceiver.cs` | WebSocket client; background receive + main-thread queue, auto-reconnect |
| `SmellVisualiser.cs` | Odour → prefab, intensity → scale, smoothing, confidence gate, idle decay |
| `PacketDebugHud.cs` | Body-locked debug overlay showing the raw wire packet |

Message contract is unchanged — `{timestamp, odour, odour_confidence, intensity, seq}` — so no client-side change is needed to start receiving real inferences. What hasn't happened yet is a fresh on-device check with the trained model driving the visual, and Approach B (the remote relay) remains unbuilt.

---

`ML/EXPERIMENTS.md` has the full 25-run log · `ML/README.md` has the stage-by-stage reasoning · `CLAUDE.md` tracks the current gap vs. `plan.md`
