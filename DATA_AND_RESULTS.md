# Data & results — availability and reproduction

The code in this repository is self-contained, but the **raw sensor data**, the
**processed dataset**, the **trained models**, and the **diagnostic/evaluation
figures** are deliberately **not committed** — they're `.gitignore`d (see
[`ML/.gitignore`](ML/.gitignore)). This keeps the public repo to code only and
keeps the raw research data private. Everything non-committed is regenerable
from the raw captures plus this code (see below).

## Where the data is

The raw captures, processed dataset, and all figures are packaged in a private
**data & results bundle** (`raw/` + `processed/` + `figures/` + the experiment
log, ~32 MB), available from the author / project supervisor on request.

| Artifact | Path in the repo (when present) | In the bundle? | Committed? |
|---|---|---|---|
| Raw captures (12 CSVs) | `ML/data/raw/` | ✅ `raw/` | ❌ gitignored |
| Processed dataset | `ML/data/processed/` | ✅ `processed/` | ❌ gitignored |
| Label-validation figures | `ML/data/diagnostics/` | ✅ `figures/diagnostics/` | ❌ gitignored |
| Const-320 sensor figures | `ML/data/diagnostics_const320/` | ✅ `figures/diagnostics_const320/` | ❌ gitignored |
| Evaluation figures | `ML/data/diagnostics_eval/` | ✅ `figures/diagnostics_eval/` | ❌ gitignored |
| Held-out test figures | `ML/testing/output/` | ✅ `figures/test_output/` | ❌ gitignored |
| Trained models | `ML/models/`, `Server/models/` | ❌ **left out** (regenerable; regressor ~155 MB) | ❌ gitignored |
| Experiment log | `ML/experiments.csv`, `ML/EXPERIMENTS.md` | ✅ `experiments/` | ✅ **committed** |

## Reproducing everything from the raw data

Python 3.8+ and `pip install -r ML/requirements.txt`, run from the repo root:

```bash
# 0. put the bundle's raw/ CSVs into the repo
cp <bundle>/raw/*.csv ML/data/raw/

# 1. build the processed dataset + label-validation figures (data/diagnostics/)
python ML/build_dataset.py

# 2. train + save the deployed models to ML/models/
python ML/train.py --classifier-phase-filter detect --classifier-algo svm

# 3. deploy the models to the live server
cp ML/models/* Server/models/

# 4. evaluation figures — out-of-fold strength predictions (data/diagnostics_eval/)
python ML/evaluate.py

# 5. (optional) const-320 sensor diagnostics, and a held-out test on any capture
python ML/preprocess.py --profile const320
python ML/testing/test_capture.py ML/data/raw/<some_capture>.csv
```

Step 2 produces the deployed models: a 4-class `{none, lemon, grapefruit,
sorange}` SVM classifier (leave-one-run-out accuracy **0.837**; clean-air `none`
recall 97%) and a RandomForest strength regressor (LORO **R² 0.891**). Training
is seeded (`--seed 0`, the default), so a rerun reproduces the same models and
metrics; each run also appends a row to `ML/experiments.csv` / `EXPERIMENTS.md`.

For the stage-by-stage preprocessing detail and how to read the figures, see
[`ML/PREPROCESSING.md`](ML/PREPROCESSING.md); for the model-selection history,
[`ML/EXPERIMENTS.md`](ML/EXPERIMENTS.md).
