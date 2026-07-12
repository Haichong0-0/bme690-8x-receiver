# testing — eyeball the deployed model on a fresh capture

Take one raw `bme690_receiver_*.csv` from a recording session and draw a
graph that looks like the training diagnostic plots, **plus the deployed
model's predicted strength** overlaid — so you can see, on a capture the model
never trained on, whether it tracks the strength and calls the odour right.

```
python test_capture.py path/to/bme690_receiver_20260703_120000.csv
python test_capture.py my_session.csv --out-dir output --no-lowpass
```

One PNG per HP354 sensor is written to `output/`. Each plot has:

- **phase-coloured points** — the cleaned mean log-resistance curve, coloured
  by detected phase (warmup / baseline / rise / plateau / decay), exactly like
  the diagnostic graphs.
- **dashed black** — the shape-derived "true" strength label (the same Stage-3
  label training uses: baseline = 0, plateau = 1, exponential-fit transitions).
  It's a reference derived from *this* run's own curve shape, **not** an
  absolute ground truth.
- **dotted green** — the strength the deployed regressor (`../models/`)
  predicts for this run.

The title shows the predicted **odour** (deployed classifier, on the plateau
cycles) and the per-sensor strength **MAE** (predicted vs. shape-label). Text
output prints the odour verdict and per-sensor MAE.

## How this differs from `../evaluate.py`

| | `evaluate.py` | `testing/test_capture.py` |
|---|---|---|
| input | the 15 processed **training** runs | one **new** raw CSV |
| predictions | leave-one-run-out (each run held out) | the **deployed** model (trained on all data) |
| purpose | honest CV estimate over training data | test generalisation to a genuinely unseen capture |

## Notes

- The input CSV does **not** need the training filename convention
  (`..._<odour><conc>.csv`); a plain `bme690_receiver_<ts>.csv` fresh off the
  receiver works. If the odour *is* in the filename it's shown and compared to
  the prediction.
- If you point this at a CSV that *was* in training, the numbers look
  optimistically good (the shipped model was refit on it) — that only verifies
  the tool. Real testing needs a capture the model hasn't seen; expect strength
  MAE around the ~0.07 leave-one-run-out figure, not ~0.01.
- Preprocessing is identical to training (it calls `preprocess.preprocess_dataframe`),
  so the "true" line here is directly comparable to the training diagnostics.
- **Caveat — the odour verdict does not yet apply the baseline-relative
  transform.** The deployed classifier is trained on baseline-relative features
  (each session's clean-air level subtracted, log R/R₀), but this harness feeds
  it the raw absolute plateau features, so its **odour** call is unreliable for
  the current model — the strength (regressor) line is unaffected. Serving
  (`Server/real_ml.py`) does apply the subtraction (it estimates the live
  baseline); mirror that here before trusting the odour verdict.
