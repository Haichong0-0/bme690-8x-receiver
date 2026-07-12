# Preprocessing pipeline

How a raw BME690 capture becomes a training-ready dataset. This is the
stage-by-stage detail behind [`build_dataset.py`](build_dataset.py); for a
quick usage/orientation see [`README.md`](README.md), and for the
model-selection experiments that consume the output see
[`EXPERIMENTS.md`](EXPERIMENTS.md).

Everything here was built and corrected against the **real** captures in
`data/raw/` (15 runs: lemon 6, grapefruit 6, lavender 3 — each a mix of the
originals plus longer-decay "v2" re-captures), not against the
original plan's assumptions. Where the two diverge, the deviation is called
out.

---

## At a glance

```
data/raw/*.csv                          one CSV per capture session (~30 min; v2 ~35)
   │
   ▼  Stage 2a  select the 4 HP354 sensors (1,3,5,7)
   ▼  Stage 2b  cut each sensor's stream into heater cycles (step-index wraparound)
   ▼  Stage 2c  spline-align steps within a cycle        [OFF — jitter negligible]
   ▼  Stage 1   impute → log-transform → low-pass filter, per (sensor, step) channel
   ▼  Stage 3   detect phases from curve shape → strength label from baseline/plateau levels
   ▼  Stage 4   slide an N-cycle window per sensor → regression samples
   │
   ▼
data/processed/                         cycle_dataset.csv · window_dataset.npz
                                        run_fits.csv · meta.json
data/diagnostics/*.png                  one phase/label plot per run × sensor
   │
   ▼  Stage 5   z-score scaling      ─┐  applied at TRAIN time (train.py), not here —
   ▼  Stage 6   group-aware split    ─┘  which split to fit a scaler on is a training choice
```

Stages are numbered per the original preprocessing plan. **Code execution
order differs slightly** from the numbering: `build_dataset.py` runs Stage 2
(build the cycle grid) *before* Stage 1 (clean the grid's columns), because
"clean each sensor channel" is only well-defined once the interleaved raw
stream has been pivoted into per-(sensor, step) columns. See
[`clean.py`](smell_ml/clean.py)'s module docstring for the full reasoning.

---

## Input: the raw capture

`bme690_receiver.py` writes one CSV per session. Filename encodes the odour
and session timestamp:

```
bme690_receiver_20260625_172513_lemon0.6.csv
                └── date ──┘└time┘ └odour┘└conc┘
```

One row per (sensor, heater-step) reading:

```
time,sensor_index,sensor_id,timestamp_since_poweron,real_time_clock,temperature,
pressure,relative_humidity,resistance_gassensor,heater_profile_step_index,
target_c,scanning_enabled,scanning_cycle_index,label_tag,error_code
2026-06-25T17:25:15.309,1,857616676,676,1782404715,31.1121,1015.0558,47.9856,
5684.85,0,320,1,1,0,0
```

The only columns the pipeline uses: `time`, `sensor_index`,
`resistance_gassensor`, `heater_profile_step_index`, `target_c`, `label_tag`.
Everything else (temperature, pressure, humidity, RTC…) is ignored — the gas
signal is the sensor resistance, and the heater step index tells us at what
temperature that resistance was measured.

`load_run()` in [`io.py`](smell_ml/io.py) parses the filename into a `run_id`
(`lemon_20260625_172513`) and `odour` (`lemon`), and returns the sorted rows.
An **optional variant tag** after the concentration — e.g.
`bme690_receiver_20260709_110002_lemon0.6v2.csv` — folds into the `run_id`
(→ `lemon_20260709_110002_v2`) while still parsing to the same `odour`, which
keeps the newer longer-decay lemon re-captures distinct from the originals.
Odours are **discovered from filenames**, not hard-coded — which is how the
pipeline flagged an earlier round that contained `sorange` instead of the plan's
`lavender`; those sessions were re-captured as lavender, so the current 15-run
set matches the plan.

---

## Stage 2 — cycle extraction, sensor filter, step alignment

### 2a · Keep only the HP354 sensors

The 8-sensor shuttle runs two heater profiles. Four sensors (indices 0, 2, 4,
6) sit at a constant 320 °C; the other four (**1, 3, 5, 7**) run the
variable-temperature `heater_354` profile — a 10-step cycle through 100/200/
320 °C. Only the variable-temperature sensors carry the temperature-resolved
signal we want, so the rest are dropped.

`hp354_sensor_indices()` in [`grid.py`](smell_ml/grid.py) reads this from the
`.bmeconfig` used at capture (`Server/Sample.bmeconfig`) rather than guessing —
though it happens to match the data-driven heuristic "sensors whose `target_c`
varies."

### 2b · Cut the stream into heater cycles

Each HP354 sensor emits a repeating 10-step heater pass every ~8–11 s. To turn
its flat stream of rows into one row per cycle, we need to know where each
10-step pass starts.

> **Deviation from the obvious approach.** The CSV has a `scanning_cycle_index`
> column that *looks* like the cycle counter — but checked against real data
> it does **not** mark heater-loop boundaries: a single `scanning_cycle_index`
> value can contain step readings from up to 3 different heater passes. The
> real boundary is a **`heater_profile_step_index` wraparound** — the point
> where the step index drops (…8, 9, 0…). Verified: 213/215 cycles land on
> exactly 10 rows this way, vs. a garbled 1–11 rows/group using
> `scanning_cycle_index`.

`true_cycle_index()` detects the wraparound; `raw_grid()` pivots each cycle
into a row of 10 columns (`step_0`…`step_9`) holding the raw resistance at each
heater step, tagged with the cycle's start timestamp. Ragged partial cycles at
the start/end of a capture (fewer than 9 of the 10 steps present) are dropped.

**Worked example** — the first complete cycle of sensor 1 in the lemon
capture. Notice the resistance is *not* flat across the cycle — it's a
temperature signature:

| step | temp | raw resistance (Ω) |
|-----:|-----:|-------------------:|
| 0 | 320 °C | 5 684.85 |
| 1 | 100 °C | 57 722.66 |
| 2 | 100 °C | 55 255.77 |
| 3 | 100 °C | 58 314.35 |
| 4 | 200 °C | 10 578.51 |
| 5 | 200 °C | 11 142.06 |
| 6 | 200 °C | 12 105.16 |
| 7 | 320 °C | 6 129.09 |
| 8 | 320 °C | 10 568.03 |
| 9 | 320 °C | 14 956.77 |

Resistance is ~10× higher at 100 °C than at 320 °C. That temperature
dependence is where MOx selectivity comes from — different VOCs oxidise
preferentially at different surface temperatures — which is exactly why the
10 steps are kept as separate features rather than averaged. The deployed
classifier goes one step further and adds the *step-to-step gradients* across
these temperatures (the `raw_gradient` feature set — the shape of the sweep,
not just its levels), which measurably improved accuracy; see
[`EXPERIMENTS.md`](EXPERIMENTS.md) and the feature sweep in
[`README.md`](README.md).

### 2c · Spline-align steps within a cycle — OFF

The plan allowed for the 10 steps not landing at identical relative times
every cycle, and offers a per-cycle spline to resample them onto fixed
offsets. `decide_alignment_needed()` in [`align.py`](smell_ml/align.py)
measures the real jitter first: worst-case std ≈ **0.09 s** against a ~0.3 s
threshold on an ~8–11 s cycle. It's negligible, so **alignment never
triggers** on any real capture (`meta.json`: `"any_alignment_applied":
false`). The spline path exists and is exercised for robustness but isn't used.

---

## Stage 1 — signal cleaning

Applied to each of the grid's 10 step-columns independently — i.e. per
(sensor, heater-step) channel, which is the same physical measurement repeated
once per cycle, so it forms a clean continuous time-series across cycles.

Three steps, in order:

1. **Impute** missing readings — mean of the preceding and succeeding cycle's
   value for that step (`impute_grid`). Real data is very clean: the imputed
   rate is ~**0.15 %** of all cells (`meta.json`:
   `"mean_imputed_rate": 0.0015`).
2. **Log-transform** — `log(resistance)`. MOx resistance spans orders of
   magnitude across temperatures (the example above: ~5.7 kΩ to ~58 kΩ within
   one cycle), and the recovery curves are near-exponential; log-space makes
   them approximately linear, which Stage 3's curve fit relies on.
3. **Low-pass filter** — a zero-phase Butterworth (`scipy.signal.filtfilt`,
   order 1, cutoff = 0.05 × the per-run sample rate). `filtfilt` is applied
   forwards+backwards so it introduces no phase shift, which would otherwise
   corrupt the decay-curve timing.

> **The filter was off, then turned back on.** A cutoff sweep first showed the
> raw curve nearly identical to `cutoff=0.05` (the mean-across-10-steps signal
> is inherently smooth), so filtering looked like it wasn't earning its
> complexity and was disabled. It was re-enabled after the algorithm sweep
> showed it *measurably* helps the classifier (logreg 0.746 → 0.755, rf
> 0.581 → 0.611) with no cost to the regressor. `meta.json`:
> `"lowpass_filter_applied": true`. Pass `--no-lowpass` to `build_dataset.py`
> to turn it off.

Output: the grid's `step_*` columns are now **cleaned log-resistance**. The
per-run sample rate, cutoff, and imputation count are recorded per (run,
sensor) in `run_fits.csv`.

---

## Stage 3 — label generation

This is the part with no precedent in the reference literature (their labels
were fixed at collection time). It produces two labels per cycle:

- **`odour`** (classification target) — constant per run, straight from the
  filename.
- **`y_conc`** (regression target) — a 0→1 relative strength.

### Phases from curve shape, not from tags

The plan expected phase labels (baseline / rise / plateau / decay) to come
from buttons pressed on the capture rig, recorded in the `label_tag` column.

> **`label_tag` is unused — 0 throughout every capture.** But the phase
> structure is unmistakably *present in the curve shape*: every run traces
> the same arc — a warm-up transient, a wide stable clean-air **baseline**
> (high resistance), a **rise** as the odour is introduced (resistance drops),
> a wide stable exposure **plateau** (low resistance), then a slow **decay**
> back toward baseline. `detect_phases()` in
> [`labels.py`](smell_ml/labels.py) recovers this directly from the mean
> log-resistance curve: it anchors the exposure **plateau** on the point of
> maximum *drawdown* below the running maximum (`cummax - value`), takes the
> **baseline** as the widest stable high region *before* that trough, and
> everything between/after is rise/decay.
>
> If a future capture *does* populate `label_tag`, `build_dataset.py` raises
> `NotImplementedError` rather than silently mislabelling — tag-based
> segmentation was never built because there was no tagged data to test it on.

> **Why drawdown, not a global min/max.** Two real-capture shapes break a
> global-extremum anchor, both surfacing in the longer-decay **v2** lemon runs:
> (a) the sensor's cold-start warm-up can read *lower* than peak exposure, so
> the global minimum would be the warm-up, not the trough; (b) a long recovery
> tail can climb back *above* the pre-exposure baseline, so the global maximum
> would be the tail, not the baseline — which silently tagged whole exposures as
> `warmup`, with no rise/plateau/decay at all. Drawdown (`cummax - value`) peaks
> at the exposure trough no matter how low the warm-up dips or how high the tail
> climbs; searching for the baseline only *before* that trough keeps the tail
> out of it.

Note the **inverted** convention this implies: the HIGH-resistance stable
region is clean-air baseline (`y_conc = 0.0`) and the LOW-resistance region is
peak exposure (`y_conc = 1.0`), because MOx resistance *drops* under reducing
VOCs.

Cycle counts by detected phase across all 15 runs (`meta.json`; 15 592 total):

| phase | cycles | y_conc |
|---|--:|---|
| baseline | 3 150 | 0.0 (level anchor) |
| rise | 778 | 0→1 (level read-off) |
| plateau | 3 616 | 1.0 (level anchor) |
| decay | 6 784 | 1→0 (level read-off) |
| warmup | 1 264 | *excluded* (NaN) |

### The strength label — read off the levels, not the fit

`y_conc` is read **linearly between the two observed stable levels the rise
spans**: the clean-air **baseline** level (strength 0) and the peak-exposure
**plateau** level (strength 1). Each cycle's strength is simply where its mean
log-resistance sits between those two levels, clipped to [0, 1]
(`label_run_by_shape`). Baseline cycles land at ~0 and plateau cycles at ~1 by
construction (their means *define* the anchors); rise and decay cycles
interpolate across the gap. Warm-up cycles — and any degenerate run with no
detectable baseline or plateau — are left `NaN` and excluded downstream.

> **This replaced a per-segment exponential fit as the label source.** The old
> path fit an exponential to each rise/decay segment and read `y_conc` off the
> fitted curve. Whenever a capture ended before the sensor had recovered, that
> fit *extrapolated* the decay asymptote (the `asymptote_unreliable` fits
> below), distorting exactly the low-strength tail. Anchoring to the
> actually-observed baseline level is extrapolation-free and measurably improved
> the strength regressor — **LORO R² 0.828 → 0.891** (see
> [`EXPERIMENTS.md`](EXPERIMENTS.md)). A side effect: cycles in a rise/decay
> segment too short to fit used to be left `NaN`; the level read-off needs no
> fit, so they now get labelled too.

The exponential is **still computed, but only for diagnostics** now — its
`tau_s` (recovery time constant), `r_squared`, and the `asymptote_unreliable`
flag characterise each segment without setting any label. Those diagnostic fits
are excellent: mean **R² = 0.993**, zero below 0.8, across all **120 fitted
segments** (15 runs × 4 sensors × 2 = 120 — every rise/decay segment fit this
time; 9 carry an `asymptote_unreliable` flag). Example from `run_fits.csv`
(lemon, sensor 1):

```
phase   r_squared   tau_s     n_cycles   duration_s   asymptote_unreliable
rise    0.9954      207.96    32         259.3        False
decay   0.9846      164.77    69         569.0        False
```

> **A caveat this stage records honestly — now diagnostic-only.** 8 fits have
> `tau_s` far larger than the segment they were fit on
> (`asymptote_unreliable: true`) — the capture ended before the recovery
> levelled off, so the fitted asymptote is extrapolated, not observed. That
> used to make those `y_conc` values shakier and is *why* the label was moved
> off the fit; now that strength is a level read-off, the flag touches no label
> and is purely a "capture ended before the sensor recovered" signal.
> `meta.json` still counts them (`"n_unreliable_asymptote_fits": 8`) and the
> per-run PNGs let you eyeball them.

### The mean-collapse, and what it does *not* affect

The label read-off (and the diagnostic fit) run on the **mean** of the 10 steps
per cycle (`mean_log_resistance`). This is the only place the 10 steps are collapsed to
one number — and it only affects the **label**, not the model features. The
classifier and regressor both consume the full 10-step vector (the classifier
additionally uses the 9 step-to-step gradients — the `raw_gradient` set). (Whether the
label itself would be sharper from per-temperature fitting is discussed in
[`EXPERIMENTS.md`](EXPERIMENTS.md); empirically it's a low-stakes choice
because the label is already a constructed proxy.)

**Worked example** — the same lemon sensor-1 run, one baseline cycle and one
plateau cycle from `cycle_dataset.csv` (values are cleaned log-resistance):

| | phase | y_conc | step_0 | step_1 | step_4 | step_7 |
|---|---|---:|---:|---:|---:|---:|
| cycle 13 | baseline | 0.00 | 11.00 | 15.46 | 12.34 | 10.78 |
| cycle 102 | plateau | 1.00 | 10.07 | 13.25 | 9.96 | 9.83 |

Every step is lower at the plateau than at baseline — resistance dropped under
exposure — and the level read-off turns that whole trajectory into the smooth 0→1 label.

---

## Stage 4 — windowing (regression samples)

A single cycle is a snapshot; strength lives in the *dynamics* (how fast
resistance is recovering). So for regression, `make_windows()` in
[`windowing.py`](smell_ml/windowing.py) slides a window of **N = 3 consecutive
cycles** per sensor, and labels each window with its **last** cycle's `y_conc`
— matching deployment, where the model only ever knows the past.

- Window size 3 won a sweep over 3/5/10/15 (smaller = better here; the
  strength changes faster than a big window stays local to). `meta.json`:
  `"window_size_cycles": 3`.
- Windows never bridge a phase gap: warm-up cycles (NaN `y_conc`) break the
  stream, so a window is always 3 genuinely consecutive labelled cycles
  (`_valid_blocks` in `build_dataset.py`).
- Two explicit trend features are added per window — `trend_slope` (slope of
  mean log-resistance across the 3 cycles) and `trend_diff_last` (last-minus-
  previous) — giving a non-sequential model the dynamics signal directly.

Classification does **not** use windows — odour identity is a static
fingerprint, so the classifier trains on single cycles.

---

## Stages 5 & 6 — scaling and splitting (train-time, not here)

Deliberately **not** applied by `build_dataset.py`:

- **Stage 5 — scaling.** Z-score, but which split to fit the mean/std on is a
  training decision, so it's left to the consumer: `train.py` fits scikit-learn's
  `StandardScaler` on the training fold only.
- **Stage 6 — group-aware split.** `split.py` splits by `run_id`, which
  satisfies both leakage risks at once: the 4 sensors of one cycle are
  near-duplicates and stay together, and adjacent cycles of one sweep are
  smooth neighbours and stay together. With only 3 repeats per odour,
  leave-one-run-out is the honest CV and is used throughout `train.py`.

Keeping these out of the dataset means the exported files are a neutral,
leakage-free starting point that any training script can split its own way.

---

## Outputs

Written to `data/processed/` (regenerable, git-ignored):

| File | Shape / rows | Contents |
|---|---|---|
| `cycle_dataset.csv` | 15 592 rows | one row per (run, sensor, cycle): `run_id, odour, sensor_id, cycle_index, cycle_timestamp, phase, y_conc, step_0…step_9`. Classification-ready (single cycles); also human-inspectable. |
| `window_dataset.npz` | 14 208 windows | regression-ready arrays: `X_window` `(14208, 3, 10)`, `y_conc`, `y_class`, `run_id`, `sensor_id`, `trend_slope`, `trend_diff_last`. |
| `run_fits.csv` | 120 rows | one row per (run, sensor, rise/decay segment): the now-diagnostic-only exp-fit `tau_s, r_squared, n_cycles, duration_s, asymptote_unreliable`, plus per-run `fs_hz`, `cutoff_hz`, imputation count. |
| `meta.json` | — | pipeline summary: odours discovered, cycle counts by phase, mean R², low-R²/unreliable-fit counts, whether lowpass/alignment were applied. |
| `data/diagnostics/*.png` | 48 plots | one per (run, sensor): cleaned curve colour-coded by detected phase with the `y_conc` overlay — the plan's mandated "validate the fit visually before trusting it." |

---

## Running it

```
pip install -r requirements.txt
python build_dataset.py                     # defaults: lowpass on, window 3
python build_dataset.py --no-lowpass --window 5   # override for comparison
python build_dataset.py --no-diagnostics    # skip the PNGs (faster)
```

Reads every `bme690_receiver_*.csv` in `data/raw/`, uses
`Server/Sample.bmeconfig` to identify the HP354 sensors, and writes the
outputs above. It prints per-run fit quality and flags any low-R² or
unreliable-asymptote fits so problems surface immediately.

`build_dataset.py` is a thin wrapper around [`preprocess.py`](preprocess.py),
which owns the per-CSV pipeline (`preprocess_csv()`) and adds a **training /
diagnostics-only switch** (drop `--training` to draw the PNGs but write no
dataset). That switch — and how to read every graph the pipeline produces — is
the next section.

---

## Diagnostic graphs — generating and reading them

The plan mandates *visually validating the labels before trusting them*, so the
pipeline renders one PNG per (run, sensor). Three scripts produce three flavours;
all share the same layout, drawn by `plot_phase_labels` in
[`diagnostics.py`](smell_ml/diagnostics.py).

### What each graph shows

File name: `{run_id}_sensor{sensor_id}_<suffix>.png`. Two things share one time
axis (seconds since the run started):

- **Left axis — mean log-resistance**, one dot per heater cycle, **coloured by
  the detected phase**:

  | phase | colour | where on the curve |
  |---|---|---|
  | warmup | grey | initial cold-start climb — excluded from labels |
  | baseline | blue | wide stable **high** region = clean air (strength 0) |
  | rise | orange | resistance dropping as the odour arrives |
  | plateau | red | wide stable **low** region = peak exposure (strength 1) |
  | decay | purple | resistance climbing back toward baseline |

- **Right axis — strength `y_conc` (0–1)**: a **dashed black** line = the label
  the pipeline assigned (`strength (true)`). Where a model prediction is
  available it is added as a **dotted green** line.

**How to read it** — a shape-vs-label check. The coloured dots should trace the
arc grey → blue (high) → orange (falling) → red (low) → purple (rising), and the
dashed strength line should sit at 0 through the blue baseline, ramp up through
the orange rise, hold at 1 across the red plateau, and ramp back down through the
purple decay. (This is exactly how the v2 mislabelling was caught: the whole
exposure came out grey/"warmup" with a flat-zero strength line — see Stage 3's
drawdown note.)

### 1 · Label-validation graphs → `data/diagnostics/` (suffix `_fit`)

Drawn automatically by the preprocessing; they show only the **shape-derived
label** (dashed black, no prediction). This is the "are my labels right?" pass,
done before any training.

```
python build_dataset.py                   # HP354 sensors → data/diagnostics/  (+ writes the dataset)
python preprocess.py --profile hp354       # HP354 diagnostics ONLY — nothing written to data/processed/
python preprocess.py --profile const320    # the constant-320 sensors → data/diagnostics_const320/
python build_dataset.py --no-diagnostics   # skip them (faster dataset build)
```

48 plots for the 12 HP354 runs (× 4 sensors). `--profile const320` is how you
eyeball the four constant-temperature sensors (0, 2, 4, 6) that run
`heater_const_320` and never enter the training set — same cleaning + phase
detection, plotted separately. In practice those single-temperature sensors
trace an even cleaner exposure arc than the modulated HP354 ones (their per-cycle
mean isn't blended across three temperatures), though their recovery segments
more often trip the `asymptote_unreliable` flag.

### 2 · Model-evaluation graphs → `data/diagnostics_eval/` (suffix `_eval`)

[`evaluate.py`](evaluate.py) adds the strength regressor's **leave-one-run-out
(out-of-fold) prediction** as the dotted green line over the dashed true label —
each run predicted by a model trained on the *other 11* — so you can see where
strength tracking is tight and where it drifts. Each title carries that run's
LORO MAE / R².

```
python evaluate.py                        # the deployed rf regressor
python evaluate.py --regressor-algo gb     # compare a different regressor
```

Needs `data/processed/` built first (`build_dataset.py`).

### 3 · Fresh-capture test graphs → `testing/output/` (suffix `_test`)

[`testing/test_capture.py`](testing/test_capture.py) runs the **deployed** model
(`ML/models/`, trained on *all* runs) against a brand-new capture it has never
seen — genuine held-out testing. The dotted line is the deployed regressor's
prediction; the title shows the predicted odour + confidence and the strength MAE
against the shape-derived reference. The input CSV needn't follow the training
filename convention.

```
python testing/test_capture.py path/to/bme690_receiver_<ts>.csv
python testing/test_capture.py my_capture.csv --no-lowpass   # match a model trained without the filter
```

### What to flag

- **grey** anywhere but the very start, or **blue** on the recovered end-tail →
  a phase-detection failure (the pre-drawdown bug).
- **no red / no purple** → no exposure detected in that run.
- a strength line that **never reaches 0 or 1**, or is **jagged** → a labelling
  problem worth inspecting before training on it.
- (eval / test only) the green prediction **lagging or overshooting** the decay
  → regressor drift, expected on the hardest folds.

---

## End-to-end trace of one cycle

Putting the stages together for the first cycle of sensor 1 in the lemon run:

1. **2a** sensor 1 is HP354 → kept.
2. **2b** the 10 rows up to the first step-index wraparound become one grid
   row: raw resistances `[5684.85, 57722.66, 55255.77, …, 14956.77]`, tagged
   `cycle_timestamp = 2026-06-25 17:25:15.309`.
3. **2c** jitter is 0.09 s → no alignment.
4. **1** no missing steps → log-transform → `[8.646, 10.963, 10.920, …,
   9.613]` → low-pass filtered across cycles.
5. **3** this cycle falls in the warm-up transient of the run → `phase =
   warmup`, `y_conc = NaN`, excluded. (A later cycle in the stable low region
   would get `phase = plateau, y_conc = 1.0`.)
6. **4** once the run reaches labelled cycles, this sensor's stream is sliced
   into 3-cycle windows for regression.

Multiply by 4 sensors × 15 runs and you get the 15 592 cycles / 14 208 windows in
`data/processed/`.
