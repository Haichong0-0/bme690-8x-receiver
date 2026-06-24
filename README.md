# BME690 8x Receiver

Pure-Python receiver and visualiser for the **Bosch BME690 8x shuttle board** on a
**BME AI-Studio Application Board 3.x** running `coines-bridge` firmware.

The 8 BME690 sensors are driven over SPI in **parallel mode** with per-sensor
chip-select lines. Each sensor's heater profile and duty cycle are read directly
from a BME AI-Studio `.bmeconfig` JSON file (no intermediate Python config). The
CSV output schema matches BME AI-Studio's `.bmerawdata` columns so the captures
are interchangeable with the official tool.

## Why this exists

The official Bosch examples (`BME690_SensorAPI`) target the single-sensor
shuttle (`shuttle ID 0x93`) and assume a C build environment. The 8x shuttle
(`shuttle ID 0x57`) uses SPI with one chip-select per sensor, and there is no
published Python driver or schematic. This project ports the BME69x driver's
calibration parsing, compensation polynomials, memory-page handling and
parallel-mode setup into Python on top of `coinespy`, then iterates over the
8 chip-select pins so all 8 sensors run from one process on Windows / Linux.

## Hardware

| Component | Notes |
|---|---|
| Bosch Application Board 3.0 / 3.1 | nRF52840-based USB host running coines-bridge firmware |
| BME690 8x Shuttle Board | shuttle ID `0x57`; 8 sensors on SPI bus 0, CS pins `0x10..0x15, 0x1D, 0x1E` |
| USB cable | data-capable (charge-only won't enumerate) |

Flash `coines_bridge_flash_firmware.bin` from the Bosch COINES SDK onto the
APP board before running the receiver — that's the firmware coinespy talks to.

## Setup

```bash
pip install -r requirements.txt
```

Drop your `.bmeconfig` (exported from BME AI-Studio) into the project root, or
keep the provided `Sample.bmeconfig`.

## Usage

Two terminals.

```bash
# terminal 1: stream measurements to CSV
python bme690_receiver.py

# terminal 2: live visualiser tailing the CSV
python bme690_viz.py
```

Receiver flags:
- `--config PATH` — explicit `.bmeconfig` (default: the newest `*.bmeconfig` in the cwd).
- `-o PATH` — explicit output CSV (default: `data/bme690_receiver_<timestamp>.csv`).

Viz flags:
- `--file PATH` — pin to a specific CSV instead of auto-discovering the newest in `data/`.
- `--window N` — seconds of history to show per sensor (default 60).
- `--refresh-ms N` — redraw interval (default 500).
- `-v` — verbose diagnostics to stderr.

## CSV schema

Mirrors BME AI-Studio's `.bmerawdata` dataColumns plus a leading ISO timestamp:

```
time, sensor_index, sensor_id, timestamp_since_poweron, real_time_clock,
temperature, pressure, relative_humidity, resistance_gassensor,
heater_profile_step_index, target_c, scanning_enabled,
scanning_cycle_index, label_tag, error_code
```

Rows are filtered with Bosch's `BME69X_VALID_DATA = 0xB0` mask (`new_data |
gas_valid | heat_stab`), matching the `parallel_mode` example. Warm-up samples
are silently dropped, same as AI-Studio.

## Configuration

The receiver loads everything from the `.bmeconfig`:

| Field | Used for |
|---|---|
| `heaterProfiles[*].temperatureTimeVectors` | per-step (target °C, multiplier) pairs |
| `heaterProfiles[*].timeBase` | sets `GAS_WAIT_SHARED` (per-step base time) |
| `dutyCycleProfiles[*].numberScanningCycles` | how many full profile runs per scan window |
| `dutyCycleProfiles[*].numberSleepingCycles` | how many cycles of wall-time to idle after each scan window (0 = continuous) |
| `sensorConfigurations[*]` | per-sensor mapping of which heater + duty profile to use |

The BME AI-Studio "grouped" layout is supported: each of the 8 sensors can run
a different heater profile (e.g. four sensors stabilising while four do active
gas scanning).

## Project files

| File | Purpose |
|---|---|
| `bme690_receiver.py` | Receiver. Initialises all 8 sensors, programs heater profiles + duty cycle, streams CSV. |
| `bme690_viz.py` | Live matplotlib visualiser. Tails the CSV (file-tail pattern, no coupling to the receiver process). |
| `bmeconfig_to_profile.py` | Parser for `.bmeconfig` JSON. Used internally by the receiver; also a standalone CLI inspector (`python bmeconfig_to_profile.py FILE.bmeconfig --print`). |
| `Sample.bmeconfig` | Example BME AI-Studio config (two heater profiles, grouped layout). |
| `BME68x_registers.md` | Distilled register reference: addresses, calibration layout, compensation formulas, mode transitions. Used to build / verify the Python port. |
| `FEASIBILITY.md` | Project journal / decision log. |
| `reference/` | The C reader and its README that demonstrated the SPI + per-CS approach on the 8x shuttle. See `reference/README.md` for credit. |

## Provenance

Register addresses, calibration parsing, and compensation formulas are ported
from Bosch Sensortec's
[BME690_SensorAPI](https://github.com/boschsensortec/BME690_SensorAPI)
(BSD-3-Clause). No Bosch C code is bundled here — the Python implementation
is standalone.

The SPI + per-CS approach for the 8x shuttle was first demonstrated by a
collaborator's C reader, archived in `reference/` — see `reference/README.md`.

## License

This project's code is released under the [MIT License](LICENSE). The
referenced Bosch SensorAPI remains under BSD-3-Clause at its
[upstream repo](https://github.com/boschsensortec/BME690_SensorAPI).
