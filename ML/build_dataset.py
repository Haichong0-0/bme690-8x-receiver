#!/usr/bin/env python3
"""Build the training dataset from every capture in ML/data/raw/.

Thin wrapper around `preprocess.py` — this is exactly
`preprocess.py --training --profile hp354`, kept as a familiar entry point
(and because the docs/README reference it). All the per-CSV and batch logic
lives in preprocess.py so there's a single implementation; see
[`PREPROCESSING.md`](PREPROCESSING.md) for the stage-by-stage detail.

Raw capture CSVs live under ML/data/raw/ (gitignored), not Server/ — Server/
is the live/deployed side and only needs the trained model artifacts.

Stage 5 (scaling) and Stage 6 (split) are intentionally NOT applied here —
which split to fit a scaler on is a training-time decision. Use
smell_ml.split / smell_ml.scaling after importing the dataset.

Usage:
    python build_dataset.py
    python build_dataset.py --data-dir data/raw --window 5 --no-lowpass
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preprocess  # noqa: E402
from smell_ml import windowing  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=preprocess.DEFAULT_DATA_DIR,
                     help="directory of bme690_receiver_*.csv captures")
    ap.add_argument("--bmeconfig", type=Path, default=preprocess.DEFAULT_BMECONFIG,
                     help=".bmeconfig used for capture, to identify HP354 sensors")
    ap.add_argument("--out-dir", type=Path, default=preprocess.DEFAULT_OUT_DIR)
    ap.add_argument("--window", type=int, default=windowing.DEFAULT_WINDOW,
                     help="cycles per regression window (default: %(default)s)")
    ap.add_argument("--no-diagnostics", action="store_true",
                     help="skip writing per-run fit PNGs to data/diagnostics/")
    ap.add_argument("--no-lowpass", dest="apply_lowpass", action="store_false",
                     help="disable Stage 1's Butterworth low-pass filter (on by default — "
                          "won the algorithm sweep, see clean.py module docstring / ML/EXPERIMENTS.md)")
    args = ap.parse_args()

    diag_dir = None if args.no_diagnostics else preprocess.PROFILES["hp354"][1]
    return preprocess.run_batch(
        data_dir=args.data_dir, bmeconfig=args.bmeconfig, profile="hp354",
        for_training=True, window=args.window, apply_lowpass=args.apply_lowpass,
        out_dir=args.out_dir, diag_dir=diag_dir,
    )


if __name__ == "__main__":
    sys.exit(main())
