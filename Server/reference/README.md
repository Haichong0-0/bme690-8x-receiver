# Reference materials

Provenance for the SPI + per-chip-select approach that drives the BME690 8x
shuttle from this project's Python receiver.

## Files

- **`bme690_69x_reader.c`** — a C reader that demonstrated the working SPI
  flow on the 8x shuttle: forced mode, 300 °C / 100 ms heater, one
  chip-select pin per sensor (`0x10..0x15, 0x1D, 0x1E`), CSV streaming over
  stdout. Builds against Bosch's BME69x SensorAPI plus the COINES C
  library. **Not used at runtime by the Python receiver**, included only
  as documentation of the working approach.

- **`BME690_8x_README.md`** — the original build / run instructions for the
  C reader, including the chip-select pin map and hardware observations
  (e.g. how many of the 8 sensors responded on the author's board).

- **`bme690_8x_live.csv`** — a small sample CSV capture from the C reader,
  useful as a reference for the column format and value ranges.

## Credit

The C reader and its README are an external contribution from a collaborator,
predating this Python port. Their working code is what proved the 8x shuttle
could be driven over SPI with per-sensor CS lines — a path not covered by
Bosch's published examples. The Python `bme690_receiver.py` mirrors the
same flow.

## Why these files aren't required

`bme690_receiver.py` re-implements all of the relevant logic in Python on top
of `coinespy`. It does not invoke, link, or import any C code. The reference
materials are kept here for verifiability:

- The register addresses, calibration parsing, and compensation polynomials
  in `bme690_receiver.py` were ported from Bosch Sensortec's
  [BME690_SensorAPI](https://github.com/boschsensortec/BME690_SensorAPI)
  (BSD-3-Clause). That code is not bundled in this repo; the Python port
  is standalone.
- The CS-pin mapping and per-sensor SPI flow were taken from the C reader
  in this folder.
