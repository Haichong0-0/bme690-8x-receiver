# XR smell reconstruction — status summary

What's been built since the preprocessing plan: the odour captures collected, the pipeline that turns them into labels and features, the models trained on top, and how it now reaches the HoloLens.

**3** odours captured · **9** capture sessions · **7,736** cycles processed · **23** training experiments logged

```
BME690 sensor → bme690_receiver.py → ML/ pipeline → trained models → real_ml.py → ws_publisher.py → HoloLens 2
```

---

## Data — what was collected

Nine real capture sessions off the BME690 8x shuttle, three repeats each of three odours: `lemon`, `grapefruit`, `sorange`.

> **Not what the plan specified.** `plan.md` declares lemon / grapefruit / lavender. The third odour actually captured is `sorange` (sweet orange). Whether that's a deliberate substitute or a placeholder session hasn't been confirmed — the pipeline discovers odours from filenames rather than assuming the original three, and flags the mismatch on every build.

Each session runs ~30 minutes on the four HP354 chip-selects (sensors 1/3/5/7 — a 10-step, 100°C/200°C/320°C heater profile); the other four sensors hold a constant 320°C and are unused downstream. Looking at the raw curves turned up two things the plan didn't anticipate:

- **The CSV's own `scanning_cycle_index` column doesn't mark heater cycles** — one value can span up to three different heater passes. The real boundary is a step-index wraparound, found by inspection.
- **Every capture traces the same shape** — warm-up, then a long stable clean-air baseline, a resistance drop as the odour is introduced, a low-resistance exposure hold, then a slow climb back that often hasn't finished by the time the file ends. None of this was tagged by the collection hardware (`label_tag` is unused in all nine files) — the phase structure had to be recovered from curve shape instead.

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
| 3 · Label | Phase detection, exponential fit → strength | Phases detected from curve shape; the plan assumed hardware tags that were never populated |
| 4 · Window | Sliding windows for regression | Window size tuned down from 5 to 3 cycles |
| 5 · Scale | Per-feature z-score | Applied at train time, not baked into the dataset |
| 6 · Split | Group-aware, by `run_id` | Leave-one-run-out used throughout, given only 3 repeats/odour |

### Live — `Server/ws_publisher.py`

Running it with no flags now spawns `bme690_receiver.py` itself and live-tails the CSV it writes — the same file `bme690_viz.py` already reads, so nothing about that script changed. A background feeder detects completed cycles and pushes them into the loaded model; a fixed-rate loop reads back a classification + intensity and broadcasts it over WebSocket.

```
python ws_publisher.py                          # default: spawn receiver + tail it
python ws_publisher.py --live-tail               # receiver already running elsewhere
python ws_publisher.py --replay-csv <path>       # demo from a capture
```

---

## Models — what got trained, and how it was chosen

23 logged experiments — a preprocessing sweep, an algorithm sweep, then a feature sweep — before landing on the current pair.

| Metric | Value |
|---|---|
| Classifier accuracy (logreg, `raw_gradient`, LORO) | **0.799** |
| Regressor R² (RandomForest, LORO) | **0.872** |
| Regression window size | **3 cycles** |

Logistic regression beat RandomForest, GradientBoosting, SVM and k-NN for classification by a wide margin (0.755 vs. 0.581–0.611 on the raw 10-step features) — the fingerprint is close to linearly separable in this feature space, which a lower-variance model exploits better than tree ensembles with only ~700 examples per class. Adding the **temperature-sweep gradient** as a feature (the 10 steps plus their 9 step-to-step differences — the shape of the sweep, not just its levels) then lifted it further to **0.799**. RandomForest stayed best for regression; Ridge's poor showing there confirmed the strength curve is genuinely nonlinear.

> **Lemon is distinct; grapefruit and sorange are not.** With the gradient features, 747 of 749 lemon cycles classify correctly (up from 724/749 on raw features — it near-eliminated the lemon↔sorange leak). The residual error is entirely grapefruit↔sorange, in both directions — plausibly because they share dominant citrus terpenes; no feature separated them. (A separate, earlier attempt — subtracting each session's own clean-air baseline — made accuracy *worse*, ruling out simple sensor drift as the cause.)

Two gaps between how the models were trained and how they now run live, both logged in `real_ml.py`:

- The winning config's low-pass filter is zero-phase and needs future samples — not possible on a live stream, so live filtering is skipped.
- Phase detection needs the whole run's shape, so the live classifier scores *every* cycle rather than just the plateau cycles it was trained on. Left unchecked this caused live lemon↔grapefruit confusion (clean-air cycles carry no odour identity yet were labelled grapefruit ~100% of the time). It's now mitigated by a **strength gate**: the regressor's predicted strength is a causal "is a smell present now" proxy, so below a threshold the odour label is suppressed and the classifier is trusted only on the near-plateau cycles it was trained on.

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

`ML/EXPERIMENTS.md` has the full 23-run log · `ML/README.md` has the stage-by-stage reasoning · `CLAUDE.md` tracks the current gap vs. `plan.md`
