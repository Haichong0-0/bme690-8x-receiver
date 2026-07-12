# ML — odour classification + strength regression preprocessing

Turns raw `data/raw/*.csv` captures into a training-ready dataset:
per-cycle odour fingerprints (`X_window`) plus a 0→1 relative-strength
regression target (`y_conc`), per the pipeline in the original preprocessing
plan (Stages 1-6, `plan.md`'s classify-then-regress design).

This folder is offline/training-side only — no runtime dependency on
`Server/`, not a git repo of its own. Raw capture CSVs live here
(`data/raw/`, gitignored), not in `Server/` — `Server/` is the live/deployed
side (`real_ml.py`) and only needs the trained model artifacts copied over
from `models/`, not the data that produced them. `ML/` does read
`Server/bmeconfig_to_profile.py` (code, to know which chip-selects run the
HP354 profile) and `Server/Sample.bmeconfig` (the capture config, not
training data) — but nothing in `Server/` imports back into `ML/`.

## Usage

```
pip install -r requirements.txt
python build_dataset.py
```

Writes to `data/processed/`:

- **`cycle_dataset.csv`** — one row per (run, sensor, cycle): `run_id, odour,
  sensor_id, cycle_index, cycle_timestamp, phase, y_conc, step_0..step_9`
  (cleaned log-resistance at each of the 10 heater steps). Good for
  classification (works off single cycles, no windowing needed) and for
  inspecting labels directly in a spreadsheet/pandas.
- **`window_dataset.npz`** — regression-ready: `X_window` `[n, N, 10]` float32
  (N = `--window` cycles, default 5), `y_conc` `[n]` float32, `y_class` `[n]`
  string, `run_id` `[n]` string, `sensor_id` `[n]` int, plus `trend_slope` /
  `trend_diff_last` engineered dynamics features per window.
- **`run_fits.csv`** — one row per (run, sensor, rise-or-decay segment):
  `tau_s, r_squared, n_cycles, duration_s, asymptote_unreliable`. These
  exponential fits are now **diagnostic-only** (the `y_conc` labels are read off
  the observed baseline/plateau levels, not the fit — see Stage 3 below); check
  this (and the PNGs below) to spot captures that ended before the sensor
  recovered.
- **`meta.json`** — pipeline-wide summary: odours discovered, cycle counts by
  phase, low-R² / unreliable-asymptote counts, imputation rate.
- **`data/diagnostics/*.png`** (one per run×sensor) — the cleaned curve
  colour-coded by detected phase with the resulting `y_conc` overlay. The
  plan explicitly calls for visually validating the curve fit before trusting
  it — do that here before training on this data.

Stage 5 (scaling) and Stage 6 (split) are **not** applied by this script —
which split to fit a scaler on is a training-time decision. Import the
dataset above, then split by run with `smell_ml.split.leave_one_run_out(run_id)`
and z-score in your own training script — as `train.py` does with scikit-learn's
`StandardScaler`, fitting it on the training split only.

## Module map (`smell_ml/`)

For the stage-by-stage writeup with worked examples (a raw cycle traced all
the way to a training sample, and every deviation from the original plan
explained), see [`PREPROCESSING.md`](PREPROCESSING.md).

| File | Stage | What |
|---|---|---|
| `io.py` | — | Load a capture CSV, parse `run_id`/`odour` from the filename |
| `grid.py` | 2a/2b | HP354 sensor selection, true cycle-boundary detection, (cycle × step) grid |
| `clean.py` | 1 | Impute, low-pass filter (on by default), log-transform, per (sensor, step) channel |
| `align.py` | 2c | Optional spline (linear, log-space) step-offset alignment |
| `labels.py` | 3 | Automatic phase detection from curve shape → rise-anchored linear `y_conc` (per-segment exponential fit still computed, now diagnostic-only) |
| `windowing.py` | 4 | Sliding windows over cycles, per sensor |
| `split.py` | 6 | Group-aware leave-one-run-out split, `group=run_id` (Stage 5 scaling is done inline in `train.py` with scikit-learn's `StandardScaler`) |
| `diagnostics.py` | — | Per-run fit/phase PNGs |
| `models.py` | — | Classifier/regressor definitions + leave-one-run-out evaluation harness |

## Script reference

The runnable entry points, what each is for, and its flags (run from `ML/`;
`python <script> -h` for the full list). The `smell_ml/` modules above are a
**library** — imported by these scripts, not run directly; see
[`PREPROCESSING.md`](PREPROCESSING.md) for their stage-by-stage detail.

### `build_dataset.py` — raw captures → training dataset

The main entry point. Runs Stages 1–4 over every
`data/raw/bme690_receiver_*.csv` for the HP354 sensors and writes the processed
dataset to `data/processed/`. Thin wrapper around
`preprocess.py --training --profile hp354`.

```bash
python build_dataset.py                          # defaults: lowpass on, window 3
python build_dataset.py --window 5 --no-lowpass  # override for comparison
```

| Flag | Default | Meaning |
|---|---|---|
| `--window <n>` | 3 | Cycles per regression window. |
| `--no-lowpass` | (lowpass on) | Disable the Stage-1 Butterworth filter. |
| `--no-diagnostics` | (on) | Skip the per-run diagnostic PNGs. |
| `--data-dir` / `--out-dir` / `--bmeconfig` | see `-h` | Input / output / config paths. |

### `preprocess.py` — the pipeline, with a diagnostics-only mode

What `build_dataset.py` wraps. Adds a `--profile` switch and a training vs
diagnostics-only toggle — use it directly to eyeball the **constant-320**
sensors (never used for training) without writing a dataset.

```bash
python preprocess.py --training                  # == build_dataset.py (HP354 → dataset)
python preprocess.py --profile const320          # const-320 sensors → PNGs only, nothing saved
python preprocess.py --profile hp354             # HP354 diagnostics only (no dataset)
```

| Flag | Default | Meaning |
|---|---|---|
| `--profile {hp354,const320}` | hp354 | Which sensor set to process. |
| `--training` | off | Save the dataset (hp354 only). Without it, only PNGs are drawn. |
| `--window` / `--no-lowpass` / `--no-diagnostics` / `--data-dir` / `--out-dir` / `--diag-dir` / `--bmeconfig` | see `-h` | As `build_dataset.py`. |

### `train.py` — evaluate (LORO) + refit + save models

Reads `data/processed/`, runs leave-one-run-out evaluation, refits on all data,
and writes `models/` (`classifier.joblib`, `regressor.joblib`, scalers,
`metadata.json`) plus a row in `experiments.csv` / `EXPERIMENTS.md`. Run
`build_dataset.py` first.

```bash
python train.py                                  # the deployed model: 4-class detect-SVM on baseline-relative raw_gradient (all defaults)
python train.py --no-baseline-relative --classifier-phase-filter plateau --classifier-algo logreg  # legacy 3-class absolute-level comparison
```

| Flag | Default | Meaning |
|---|---|---|
| `--classifier-algo {rf,gb,svm,logreg,knn}` | svm | Classifier algorithm. The deployed detect classifier uses `svm` (its factory sets `probability=True` so `predict_proba` works at serving); `logreg` won the older 3-class plateau sweep. |
| `--classifier-features {raw,gradient,raw_gradient,temp_contrast}` | raw_gradient | Feature set — must match what `real_ml.py` will serve. |
| `--baseline-relative` / `--no-baseline-relative` | on | Subtract each (run,sensor)'s clean-air baseline before the feature transform (log R/R₀). **The drift fix** — deployed on. `--no-baseline-relative` gives the old absolute-level features (higher-looking LORO, but leaky and not deployable live). |
| `--classifier-phase-filter {plateau,high_conc,detect}` | detect | Which cycles train the classifier. `detect` (default) is the deployed 4-class mode: a clean-air `none` class + odour classes trained across a concentration range (rise/plateau/decay). `plateau`/`high_conc` are the legacy 3-class modes. |
| `--odour-conc-threshold <f>` | 0.4 | For `--classifier-phase-filter detect`: min `y_conc` for a cycle to carry its odour label (below it, only baseline cycles feed the `none` class). |
| `--regressor-algo {rf,gb,ridge,svr,knn}` | rf | Regressor algorithm. |
| `--models-dir` / `--seed` / `--note` | see `-h` | Output dir / RNG seed / experiments.csv note. |

**To deploy:** copy `ML/models/*` → `Server/models/`.

### `evaluate.py` — out-of-fold prediction plots

Overlays the regressor's leave-one-run-out predictions on each run's curve
(dashed = true strength, dotted = predicted) → `data/diagnostics_eval/`. For
*seeing* where strength tracking drifts, per run and sensor.

```bash
python evaluate.py                               # rf (the deployed regressor)
python evaluate.py --regressor-algo gb
```

| Flag | Default | Meaning |
|---|---|---|
| `--regressor-algo {rf,gb,ridge,svr,knn}` | rf | Regressor to evaluate. |
| `--eval-dir` / `--seed` | see `-h` | Output dir / RNG seed. |

### `testing/test_capture.py` — test the deployed model on a fresh capture

Takes one raw CSV the model has never seen, runs it through the same
preprocessing, and plots predicted strength + predicted odour per sensor.
Genuine held-out testing (vs `evaluate.py`, which is LORO over the *training*
runs). The input needn't follow the training filename convention.

```bash
python testing/test_capture.py path/to/bme690_receiver_<ts>.csv
```

| Flag | Default | Meaning |
|---|---|---|
| `--models-dir` | `ML/models/` | Which trained model to test. |
| `--bmeconfig` | `Sample.bmeconfig` | Identifies the HP354 sensors. |
| `--out-dir` | `testing/output/` | Where plots go. |
| `--no-lowpass` | (on) | Match a model trained without the filter. |

---

## Training

```
python train.py --classifier-phase-filter detect --classifier-algo svm
```

Reads `data/processed/`, evaluates via leave-one-run-out (12 folds — every
capture held out once), then refits on all data and writes to `models/`:
`classifier.joblib` + `classifier_scaler.joblib`, `regressor.joblib` +
`regressor_scaler.joblib`, `metadata.json` (LORO metrics, confusion matrix,
which cycle-phase filter the classifier used, whether baseline-relative). The
bare `python train.py` now reproduces the deployed model directly — 4-class
detect-SVM on baseline-relative `raw_gradient` — since those are the defaults;
pass `--no-baseline-relative --classifier-phase-filter plateau --classifier-algo
logreg` for the legacy 3-class comparison.

### Evaluation plots

```
python evaluate.py                    # rf regressor (the deployed default)
```

Overlays the strength regressor's **out-of-fold** predictions (each run
predicted by a model trained on the other 11) onto each run's diagnostic curve:
dashed black = true strength, dotted green = predicted. (The strength label is
the `y_conc` column in the processed data — same 0-1 value, just named
"strength" in the human-facing wording to avoid implying absolute
concentration.) One PNG per (run, sensor) to `data/diagnostics_eval/`, titled
with that run's LORO MAE/R². Makes it visible *where* the model tracks strength
and where it drifts — e.g. the worst fold predicts rise/plateau fine but keeps
strength pinned high through the recovery instead of following it down.

Every run also appends a row to **`experiments.csv`** (machine-readable) and
regenerates **`EXPERIMENTS.md`** (the same data as a markdown table) —
whatever preprocessing params `data/processed/meta.json` says were used
(lowpass filter, window size, ...) alongside that run's LORO results, plus
which classifier/regressor algorithm was used (`--classifier-algo`,
`--regressor-algo`; see `smell_ml.models.CLASSIFIER_FACTORIES` /
`REGRESSOR_FACTORIES` for the full list — RandomForest, GradientBoosting,
SVM, logistic regression / Ridge, k-NN). Use `--note "..."` to record what
you changed. Don't hand-edit `EXPERIMENTS.md`; it's regenerated from
`experiments.csv` every `train.py` run.

### Algorithm + preprocessing sweep results

31 experiments in `experiments.csv` / `EXPERIMENTS.md` — see there for the
full table. The sweep below (exps 1–23) was run on the original **9-run**,
**sorange-era** dataset and picked the 3-class classifier + regressor defaults;
the **deployed** model has since moved to a 4-class detect-SVM, rise-anchored
labels, and **baseline-relative** features on the current **15-run** (lavender)
dataset — see "Deployed classifier + regressor" at the end of this section.
Summary of what moved the numbers in the sweep, each holding everything else
fixed at the exp-2 baseline (lowpass off, window=5, `plateau` classifier cycles,
RandomForest/RandomForest):

**Classifier algorithm** (regressor held at `rf`): **logreg 0.746** > svm
0.688 > rf 0.581 (baseline) > gb 0.561 > knn 0.534. Logistic regression won
by a wide margin — plausible given the classifier's job here is close to
linearly separating 10-dim step vectors by odour, which a simpler,
lower-variance model handles better than tree ensembles with only ~700
plateau cycles per class to fit on.

**Regressor algorithm** (classifier held at `rf`): **rf R²=0.859**
(baseline) > svr 0.799 > knn 0.793 > ridge 0.736. RandomForest stayed best —
Ridge's poor showing (linear model) confirms the strength curve's
dynamics genuinely need a nonlinear fit, consistent with Stage 3's
exponential curve-fit design.

**Window size** (classifier unaffected — window only changes regression
data): R² 3=**0.868** > 5=0.859 > 10=0.837 > 15=0.817. Monotonically worse
with larger windows — the strength changes faster (tau mostly 100-600s,
cycles ~8-11s apart) than a big window can stay local to, so it starts
blending in stale context instead of helping.

**Lowpass filter, revisited with the better classifier**: turning it back on
still helps logreg on raw features a little (0.746 → 0.755) — smaller effect
than it had on `rf` (0.611 → 0.581 the other way, i.e. filtering helped `rf`
more than it now helps `logreg`), but real. Regressor R² unaffected either
way, as before.

**Classifier phase filter**: `plateau` still beats `high_conc` — consistent
with the exp-2 finding, at every feature set tried.

**Classifier features** (lowpass ON, window=3, logreg, plateau): raw 10 steps
0.755 → **`raw_gradient` 0.799** — the 10 steps PLUS their 9 step-to-step
gradients (`np.diff`), i.e. the derivative/shape of the temperature sweep —
beating `gradient`-only (0.793) and `temp_contrast` (0.728). The gradient is a
log response-ratio across temperatures, the classic MOx selectivity feature,
and it lifts *every* classifier (rf 0.611→0.714, svm 0.697→0.796). It helps
`logreg` too, even though a gradient is a linear combination of the raw steps:
with L2 regularisation + per-feature standardisation the representation still
matters (an *unregularised* linear model would gain nothing from it). See
`train.load_classification_data` and `real_ml.py`'s docstring.

**Best combined 3-class config** (exp 23: lowpass ON + window=3 + logreg + rf +
`raw_gradient` features) beat every single-dimension winner on the 9-run data:
classifier **accuracy 0.799, f1_macro 0.543**; regressor **MAE 0.069,
RMSE 0.118, R² 0.872**. Confusion matrix (grapefruit↔sorange was the whole
error mode; lemon cleanly separated):

```
              predicted:
              grapefruit  lemon  sorange
  grapefruit:    502        0      198
  lemon:           0      747        2
  sorange:        196        0      433
```

Lemon was near-perfect (747/749, up from 724/749 on raw features — the gradient
features near-eliminated the lemon↔sorange leak raw had). The residual was
entirely grapefruit↔sorange, both directions — plausibly the same underlying
cause (shared dominant citrus terpenes).

#### Deployed classifier + regressor (what `models/` currently holds)

The shipped model has moved on from that 3-class config in three ways
(exps 24–30, on the **15-run** dataset — lemon ×6, grapefruit ×6, lavender ×3):

1. **Classifier → 4-class "detect" SVM** (exp 24). The plateau-only 3-class
   classifier had no way to say "clean air" and forced an odour label onto
   every cycle — live, clean-air baseline cycles were classified as an odour
   ~100% of the time. The `detect` mode adds a clean-air **`none`** class
   (baseline cycles) and trains the odour classes across a concentration
   **range** (every cycle with `y_conc ≥ 0.4 = --odour-conc-threshold`, spanning
   rise/plateau/decay, not just the plateau), so it learns each odour's
   fingerprint *below* full strength. SVM (rbf, `probability=True`) won this
   decisively over logreg.

2. **Baseline-relative features → the drift fix** (exp 30, now the default).
   The detect SVM on *absolute* log-resistance scored a higher-looking **0.925**
   LORO — but that was leaky. Absolute resistance encodes the session (humidity,
   board, day), and because each odour was captured on its own day the model
   latched onto *which day* rather than the odour; on a live sensor at a
   different humidity it collapsed to a single class (the real live failure).
   Subtracting each run/sensor's own clean-air baseline (**`--baseline-relative`**,
   log R/R₀) is drift-invariant. The honest LORO drops to **0.771** (f1_macro
   0.612, **9,745 cycles**) but the model survives live: clean air is `none`
   **100%** of the time, and the residual is odour-vs-odour (~0.6). An early
   version of this test — run on the in-distribution offline data alone —
   *lowered* `rf` accuracy (0.61 → 0.48) and was once written off as "ruling out
   drift"; that was the wrong read. Offline there's little cross-session drift to
   correct, so subtraction only costs a little discriminative signal; the payoff
   appears exactly where it's needed, on live out-of-distribution captures. See
   `train.py`'s `load_classification_data` docstring.

3. **Rise-anchored linear strength labels** (exp 25, see Stage 3 below). Reading
   `y_conc` off the observed baseline/plateau levels instead of the exponential
   fit's extrapolated asymptote lifted the regressor to R² **0.891**: on the
   15-run data **MAE 0.083, RMSE 0.118, R² 0.891**, trained on **14,208 windows**.

Deployed confusion matrix (4-class detect-SVM, baseline-relative, rows = true,
cols = predicted; clean air is perfectly rejected, and `lemon` is now the
confusion sink — both grapefruit and lavender leak into it):

```
              predicted:
              grapefruit  lavender  lemon  none
  grapefruit:    2207         0      441     3    (83%)
    lavender:      22       684      668     0    (50%)
       lemon:     581       450     1539     0    (60%)
        none:       0         0        0  3150   (100%)
```

The fix from here is better data collection, not more modelling: interleave the
odours within a single session (so capture-day no longer ≡ odour and LORO
becomes truly honest), use a hotter heater profile (the 100 °C steps currently
saturate out of range), and add repeats (lavender is only 3 runs).

For reference, this is the exp-2 baseline confusion matrix (RandomForest,
lowpass off, window=5) on the earlier **sorange-era** 3-class data — kept because
it motivated trying other algorithms in the first place:

```
              predicted:
              grapefruit  lemon  sorange
  grapefruit:    186        1      533
  lemon:           5      737       16
  sorange:        266      51      321
```

## What the real data actually looks like (read this before trusting labels)

The original plan was written before any real multi-run captures existed, so
several of its stages made assumptions that turned out not to match the real
captures (`data/raw/*.csv`). This pipeline was built and corrected against
that real data, not against the plan's assumptions — here's what changed and
why, in the order they were found:

1. **`scanning_cycle_index` (the CSV's own column) does not mark heater-cycle
   boundaries.** It looked like the obvious cycle key (already in the raw
   CSV, no heuristic needed), but a single `scanning_cycle_index` value can
   contain step readings from up to 3 different heater passes — it's some
   other, coarser counter. The real cycle boundary is a
   `heater_profile_step_index` wraparound (this row's step index < the
   previous row's), which `grid.true_cycle_index` detects directly: 213/215
   detected cycles in a spot-checked run land on exactly 10 rows, vs. a
   garbled 1-11 rows/group grouping by `scanning_cycle_index`.

2. **`label_tag` (the collection board's phase-labelling buttons) is unused —
   always 0 — in every capture so far.** So Stage 2b/3's phase tags can't
   come from the tags. But the phase structure is very much *present in the
   curve shape*: every one of the 12 real captures traces a near-identical
   warm-up → baseline (wide, stable, high resistance) → rise → plateau (wide,
   stable, low resistance) → decay shape, with consistent timing across runs
   regardless of odour (rise ~0-100s, baseline plateau ~100-550s, transition
   ~550-700s, exposure plateau ~700-1250s, recovery ~1250s to file end) — the 3
   newer lemon "v2" captures being the deliberate exception, running a longer
   decay (see item 5). `labels.detect_phases` recovers this directly from
   the curve instead of relying on tags that were never populated. If a
   future capture actually populates `label_tag`, `process_run` in
   `build_dataset.py` will raise `NotImplementedError` rather than silently
   mis-labelling it — tag-based segmentation isn't implemented because there
   was no tagged data to build or test it against.

3. **Resistance direction is inverted from what "plateau"/"baseline" sound
   like.** BME690 (MOx) resistance drops under exposure to reducing-gas VOCs
   (citrus terpenes qualify) and recovers on clearance. The *low*, wide,
   stable region is peak exposure (`y_conc = 1.0`) and the *high*, wide,
   stable region is clean-air baseline (`y_conc = 0.0`) — confirmed by which
   region is wider/more stable and which comes first in time, not by raw
   magnitude.

4. **The very first ~100s of every run is excluded as `phase = "warmup"`,
   not treated as the baseline.** It's a short, one-off climb up to the same
   level as the (much longer) stable baseline plateau that follows — almost
   certainly heater/thermal warm-up transient, not part of the odour
   protocol. Its `y_conc` is left `NaN` and it's excluded from
   `window_dataset.npz` (windowing skips gaps in valid `y_conc` rather than
   bridging across them — see `_valid_blocks` in `build_dataset.py`).

5. **Most runs stop before the post-exposure recovery fully reaches baseline —
   the newer lemon "v2" captures much less so.** The `decay` phase (the plan's
   actual strength-decay sweep) is captured from ~1250s to file end, but the
   curve is often still climbing at that point. This *used to* distort the
   labels: Stage 3 read strength off a per-segment exponential fit whose
   asymptote was then an extrapolation past what was actually observed. Two
   things fixed that:
   - **The 3 new lemon "v2" runs extend the decay to ~35–36 min (~252–261
     cycles) vs the older ~30 min (~216 cycles), so recovery reaches much closer
     to baseline** — and their long recovery tail can even climb *above* the
     pre-exposure baseline, which is why `labels.detect_phases` now anchors the
     exposure trough on the maximum drawdown below the running max (cummax −
     value) rather than the global min/max (a global-extremum anchor mislabelled
     those v2 runs entirely, tagging the whole exposure as "warmup").
   - **Stage 3 no longer labels from the fit at all** — `y_conc` is read
     linearly between the observed baseline and plateau levels the rise spans
     (extrapolation-free; see Stage 3 in the module map). The exponential fit is
     still computed for diagnostics, and `run_fits.csv`'s `asymptote_unreliable`
     column still flags fits where `tau > 3×` the segment's observed duration (8
     such fits across the dataset), but those flags no longer affect any label.

6. **The `.bmeconfig`'s `gas_wait` for `heater_354` step 3 silently saturates**
   (4200ms requested, 4032ms max — this is a real bug in
   `Server/bmeconfig_to_profile.py`, now fixed and warns on stderr). Doesn't
   affect this pipeline directly (it reads captured resistance, not the
   heater-wait encoding) but explains the warning printed at the top of
   `build_dataset.py`'s output.

7. **Captured odours are `lemon`, `grapefruit`, `lavender`** — matching
   `plan.md`. (An earlier round captured `sorange` (sweet orange) in lavender's
   place; those sessions were re-collected as lavender.) `build_dataset.py`
   doesn't hard-code an odour set — it discovers odours from filenames and warns
   on any mismatch with `plan.md` — and that check now passes with no
   substitution.

## Open parameters (plan explicitly leaves these for empirical tuning)

| Parameter | Code default | Basis / sweep finding |
|---|---|---|
| Butterworth low-pass | **on** (`clean.clean_sensor_grid(apply_lowpass=True)`; `--no-lowpass` to disable) | Went back and forth on this. A cutoff-fraction sweep first suggested off (raw and `cutoff_fraction=0.05` nearly overlap on the raw signal). The later algorithm sweep showed it measurably helps classification either way: `rf` 0.611 vs 0.581 off, and `logreg` (on raw features) 0.755 vs 0.746 off — with no regression-side cost. Net: on. |
| Butterworth order | 1 | Copied from the honey paper — untouched since order interacts less with the wrong-`fs` problem than cutoff does. |
| log-transform order | before filtering | Chosen empirically: the real decay/rise segments are close to linear in log-space (fits hit R² 0.96-1.0), which wouldn't hold filtering-then-logging raw resistance given the multi-order-of-magnitude swings between heater steps. |
| Step-alignment (Stage 2c) | off (`align.JITTER_ABS_THRESHOLD_S = 0.3`s) | Measured real jitter is std ~0.05-0.09s against multi-second step spacings — confirmed negligible, not assumed. The spline path exists and is exercised for robustness but not currently triggered by any real run. |
| Window size `N` | **3 cycles** (`windowing.DEFAULT_WINDOW`) | Swept 3/5/10/15: regressor R² is monotonically worse with larger windows (0.868/0.859/0.837/0.817) — the strength changes faster than a big window stays local to. 3 was the smallest tried and won; going smaller still is untested. |
| Classifier algorithm | **svm** (`--classifier-algo`; the deployed detect default) | On the 4-class `detect` task SVM (rbf, `probability=True`) won and is shipped. The earlier 9-run 3-class plateau sweep of rf/gb/svm/logreg/knn was won instead by **logreg** by a wide margin (0.746-0.755 vs rf's 0.581-0.611) — that task is closer to linearly separable than tree ensembles can exploit with ~700 examples/class — so `logreg` is the right pick if you go back to `--classifier-phase-filter plateau`. |
| Classifier features | **raw_gradient** (`--classifier-features`) | Swept raw / gradient / raw_gradient / temp_contrast: `raw_gradient` (the 10 log-R steps + their 9 step-to-step gradients — the temperature-sweep shape) won at 0.799 vs raw's 0.755, and near-eliminated lemon misclassification. Helps every classifier. `real_ml.py` reads the choice from `metadata.json` and applies the same transform live. |
| Baseline-relative features | **on** (`--baseline-relative`; `--no-baseline-relative` to disable) | Subtract each (run,sensor)'s clean-air baseline before the feature transform (log R/R₀). The **drift fix**: absolute log-R encodes session/humidity/board, and with odour ≡ capture-day the raw-level model leaks (LORO 0.925) and collapses to one class on a live sensor at a different humidity; baseline-relative is drift-invariant (honest LORO 0.771, survives live). `real_ml.py` estimates the baseline live and subtracts it to match. |
| Regressor algorithm | **rf** (`--regressor-algo`) | Swept rf/gb/ridge/svr/knn: `rf` stayed best (R²=0.859-0.872 vs next-best svr 0.799). Ridge's poor showing confirms the dynamics are genuinely nonlinear. |
| Strength signal / label | mean log-resistance across all 10 steps; `y_conc` read linearly between the observed baseline (0) and plateau (1) levels | Simplification: averages across very different heater temperatures rather than picking one "best" step. Labels now come from the two observed stable levels (extrapolation-free); the per-segment exponential fit (R² 0.96-1.0) is still computed but is diagnostic-only. A per-step or best-step signal is worth trying if classification/regression accuracy plateaus. |
| Rise phase included | yes | Plan calls this optional; included since it roughly doubles labelled decay-direction data and its fits are consistently the best (R² ~0.995-1.0). |
| Purge phase | not separately detected | No real capture shows the post-decay curve re-stabilizing before file end, so there's nothing to detect yet — the whole post-plateau tail is treated as one continuous `decay` fit. Revisit if a future capture runs long enough to show it. |

Full sweep: `experiments.csv` / `EXPERIMENTS.md` (31 runs — the 9-run
algorithm/feature sweep in exps 1–23, exps 24–25 that switched the deployed
model to the 4-class detect-SVM + rise-anchored labels, then exps 30–31 that made
it **baseline-relative** on the 15-run data). To go back to the original vanilla
baseline (RandomForest/RandomForest, lowpass off, window=5) for comparison:
`python build_dataset.py --no-lowpass --window 5` + `python train.py
--no-baseline-relative --classifier-phase-filter plateau --classifier-algo rf
--regressor-algo rf`.

## Known caveats

- Few repeats per odour — 6 lemon (3 original + 3 longer-decay "v2"), 6
  grapefruit (3 + 3 "v2"), 3 lavender ("v2") — 15 runs total, all captured at a
  single nominal concentration (0.6). `smell_ml.split` offers leave-one-run-out
  for a more stable estimate than a single held-out split, per the plan's own
  caution about this. Because each odour was captured on its own day, even LORO
  is slightly optimistic (capture-day ≡ odour); interleaving odours within a
  session is the fix — see the deployed-classifier note on baseline-relative.
- `sensor_id` in the output is the physical chip-select (1/3/5/7), not a
  0-indexed position — keep that in mind if you one-hot it.
- Classification labels (`y_class`) are just the filename-parsed odour string
  — there's no independent ground truth beyond that filename, so a
  mislabelled capture would propagate silently.
