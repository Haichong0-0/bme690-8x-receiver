# BME690 8x Receiver

Pure-Python live capture and visualisation for the **Bosch BME690 8x shuttle
board** on a **Bosch Application Board 3.0 / 3.1**.

All 8 BME690 sensors are driven simultaneously over SPI with per-sensor
chip-select lines. The heater profile and duty cycle are read directly from a
BME AI-Studio `.bmeconfig`, and the CSV output matches AI-Studio's
`.bmerawdata` column schema so the captures are interchangeable with the
official tool.

```
┌─────────────────┐    USB    ┌────────────────────┐    SPI    ┌─────────────────┐
│   your laptop   │ ◄───────► │   APP3.x board     │ ◄───────► │  BME690 8x      │
│                 │           │   (nRF52840 +      │           │  shuttle        │
│  python +       │           │    coines-bridge   │           │  (8 sensors,    │
│  this repo      │           │    firmware)       │           │   8 CS pins)    │
└─────────────────┘           └────────────────────┘           └─────────────────┘
```

---

## Quick start

If you already have a working BME AI-Studio install with the board flashed
and a `.bmeconfig` ready, this is everything:

```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>
pip install -r requirements.txt

# drop your .bmeconfig in the project root (Sample.bmeconfig is included
# for testing without one)

# verify the board is detected and all sensors respond
python bme690_receiver.py --check

# real capture — one terminal
python bme690_receiver.py

# live visualiser — another terminal
python bme690_viz.py
```

If that doesn't work end-to-end, follow the **Setup** section below. If
something specific breaks, see **Troubleshooting**.

---

## Hardware required

| Item | Notes |
|---|---|
| Bosch Application Board 3.0 or 3.1 | The host board with the nRF52840 MCU |
| BME690 8x Shuttle Board | Plugs into the APP board. Shuttle ID `0x57`. |
| USB cable | Must be **data-capable** — charge-only cables don't enumerate |
| A computer | Windows, macOS, or Linux — any with Python 3.8+ |

You can use this on a single-sensor BME690 shuttle (ID `0x93`) too, but the
CS pin map in `SENSOR_CS_PINS` is for the 8x layout — the receiver will warn
you if it sees a different shuttle ID and may not find all sensors.

---

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

This installs `coinespy` (the Python wrapper for Bosch's COINES C library
that talks to the APP board over USB) and `matplotlib` (for the visualiser).
On Windows you may also need the **Bosch COINES SDK** installed once for the
USB driver to be available — see step 2.

### 2. Get the COINES SDK and flash `coines-bridge` firmware

The APP board ships with BME AI-Studio firmware which speaks a different
protocol. This project needs the lower-level **coines-bridge** firmware,
which lets the host drive SPI directly.

1. Download the **COINES SDK** from
   https://www.bosch-sensortec.com/software-tools/tools/coines/ (free, no
   account needed). Run the Windows installer or extract the Linux/macOS
   tarball.
2. On Windows the installer also drops the USB driver the board needs to
   enumerate as a COM port.
3. Flash the coines-bridge firmware:

   ```bash
   # Windows (default install path):
   cd "C:\COINES_SDK\v2.12.1\firmware\app3.1\coines_bridge"
   .\update_coines_bridge_flash_fw.bat
   ```

   Replace `app3.1` with `app3.0` if you have the older board. The script
   uses `dfu-util` to push `coines_bridge_flash_firmware.bin` over USB —
   should take ~10 s and report `Done!`.

After this the board, when plugged in, should enumerate as something like
`Bosch Sensortec APP3.1 Board (COM3)` on Windows or `/dev/ttyACM0` on Linux.

### 3. Get a `.bmeconfig`

A `.bmeconfig` describes the heater profile and duty cycle the receiver
should program into each sensor. There are three ways to obtain one:

- **Use the included one.** `Sample.bmeconfig` in this repo is a real
  AI-Studio export and runs the standard HP-354 gas-scan profile alongside
  the HP-001 stabilisation profile.
- **Export from BME AI-Studio Desktop.** Open AI-Studio, configure your
  heater profile + duty cycle, then save the project — `.bmeconfig` files
  live under `projects/<your-project>.bmeproject/config/`.
- **Hand-write one.** The format is small enough to author by hand — see
  `Sample.bmeconfig` as a template.

### 4. Verify the wiring with `--check`

```bash
python bme690_receiver.py --check
```

Expected output:

```
# loaded 2 heater profile(s) from C:\…\Sample.bmeconfig
#   profile 'heater_1'   on sensors [0, 2, 4, 6]: cycle ≈ 600.60 s
#   profile 'heater_354' on sensors [1, 3, 5, 7]: cycle ≈ 10.78 s
# duty cycle: 1 scan / 0 sleep
# board hw=0x11 sw=0x1206 shuttle=0x57
# s0 cs=0x10: chip=0x61 variant=0x02 id=0x331E7310 init OK
# s1 cs=0x11: chip=0x61 variant=0x02 id=0x331E3710 init OK
…
--check summary: 8 / 8 sensors initialised successfully.
  responding: [0, 1, 2, 3, 4, 5, 6, 7]
  CSV would be: data/bme690_receiver_<ts>.csv  (not written in --check mode)
Looks good. Run again without --check to start capturing.
```

If you see `chip=0x00` or `chip=0xFF` for some sensors, those positions on
the shuttle are either unpopulated, dead, or have a hardware issue — the
receiver will still work with the responding ones.

### 5. Capture and visualise

```bash
# terminal 1 — streams CSV to data/
python bme690_receiver.py

# terminal 2 — live plot
python bme690_viz.py
```

`Ctrl+C` in either terminal stops it cleanly. The CSV is flushed every row
so anything captured up to that point is on disk.

---

## What you get out

### CSV (one row per sensor per heater step)

Columns mirror BME AI-Studio's `.bmerawdata` schema:

```
time, sensor_index, sensor_id, timestamp_since_poweron, real_time_clock,
temperature, pressure, relative_humidity, resistance_gassensor,
heater_profile_step_index, target_c, scanning_enabled,
scanning_cycle_index, label_tag, error_code
```

Rows are filtered using Bosch's `BME69X_VALID_DATA = 0xB0` mask
(new_data ∧ gas_valid ∧ heat_stab) — same filter the `parallel_mode.c`
example uses, so the data is directly comparable to AI-Studio's captures.

### Live plot

The visualiser tails the CSV (no IPC with the receiver — pure file-tail
pattern) and shows an 8-panel grid of gas resistance per sensor, one
coloured line per heater step. A green dot in each panel indicates the
heater has stabilised; red means warming up.

---

## How it works

`bme690_receiver.py` reimplements the relevant parts of Bosch's
[BME690 SensorAPI](https://github.com/boschsensortec/BME690_SensorAPI)
(BSD-3-Clause) in pure Python on top of `coinespy`:

- per-sensor SPI access via memory-page tracking (`0xF3` MEM_PAGE register)
- 42-byte calibration parse (3 blocks: `COEFF1` @ `0x8A`, `COEFF2` @ `0xE1`, `COEFF3` @ `0x00`)
- compensation polynomials for T / P / H / gas resistance (BME690 variant)
- heater code / gas-wait byte encoding
- parallel-mode setup: 10 heater slots, `GAS_WAIT_SHARED`, `CTRL_GAS_1`
- per-sensor profile assignment for AI-Studio's grouped layout
- 3-field rotation polling at `GAS_WAIT_SHARED + meas_dur` cadence

No C is invoked at runtime — `bme690_receiver.py` only depends on
`coinespy` and the standard library.

The CS-pin map for the 8x shuttle is in `SENSOR_CS_PINS`:

| sensor | CS pin | coinespy constant |
|---|---|---|
| 0 | `0x10` | `MINI_SHUTTLE_PIN_1_4` |
| 1 | `0x11` | `MINI_SHUTTLE_PIN_1_5` |
| 2 | `0x12` | `MINI_SHUTTLE_PIN_1_6` |
| 3 | `0x13` | `MINI_SHUTTLE_PIN_1_7` |
| 4 | `0x14` | `MINI_SHUTTLE_PIN_2_5` |
| 5 | `0x15` | `MINI_SHUTTLE_PIN_2_6` |
| 6 | `0x1D` | `MINI_SHUTTLE_PIN_2_7` |
| 7 | `0x1E` | `MINI_SHUTTLE_PIN_2_8` |

---

## Live inference & WebSocket publisher

Beyond raw capture, `Server/` also hosts the **live classify-then-regress
publisher** the HoloLens client connects to. Each sensor cycle becomes an
odour + a 0→1 strength, and `ws_publisher.py` broadcasts that as a compact
JSON packet (`{timestamp, odour, odour_confidence, intensity, seq}` — the
contract in [`../plan.md`](../plan.md) §3) over WebSocket at a fixed rate.

```bash
python ws_publisher.py                    # default: spawn the receiver, tail its CSV, serve on ws://<lan-ip>:8765
python ws_publisher.py --live-tail        # receiver already running elsewhere
python ws_publisher.py --replay-csv PATH  # demo from a past capture (no hardware)
python ws_publisher.py --dummy            # simulated waveform, no model (pure transport test)
```

The server stays up and keeps logging live predictions even after the client
disconnects. Terminal output is just the packet stream plus abnormal events;
pass `-v` for per-cycle detail.

- **`real_ml.py`** — `RealEstimator`: loads the trained artifacts from
  `models/` and turns buffered cycles into an inference. The classifier is a
  4-class **detect-mode SVM** over `{none, lemon, grapefruit, lavender}` — a
  dedicated clean-air **`none`** class plus the three odours trained across a
  concentration range, so it identifies an odour even at low concentration and
  rejects clean air itself. It runs on **baseline-relative `raw_gradient`**
  features (the 10 log-resistance steps plus their step-to-step gradients — the
  temperature-sweep shape — with each run's clean-air level subtracted, log
  R/R₀), read via `metadata.json`'s `classifier_features` + `baseline_relative`
  so the live features match training. **This is the drift fix:** absolute
  resistance drifts with humidity/board/session, and the old raw-level model
  latched onto that instead of the odour and collapsed to one class live;
  baseline-relative cancels it. To subtract the baseline live, `RealEstimator`
  **estimates each sensor's clean-air baseline** — discard a minimum warmup, then
  freeze the per-step median of the last 8 cycles **once the level has flattened**
  (adaptive: it waits until the last 8 cycles' mean log-R drifts ≤ 5%, so a cold
  first-run-of-day that's still climbing doesn't freeze a too-low baseline; a hard
  cap forces capture with a warning if it never settles) — and won't classify a
  sensor until that baseline is captured. `infer()` auto-detects the `none` class: it reports the
  best **real** odour (never `none`) and uses P(that odour) as `odour_confidence`,
  so clean-air or faint cycles come through with low confidence and the XR
  visual's confidence gate hides them. The regressor is a RandomForest over a
  3-cycle window predicting 0→1 strength. The **strength gate** is now vestigial
  — the `none` class does the primary clean-air rejection — and both
  `DEFAULT_STRENGTH_GATE_WITH_NONE` / `DEFAULT_STRENGTH_GATE` are currently **0**
  (disabled for live testing; design backstops are 0.15 for a `none`-class model
  and 0.6 for a legacy no-`none` one; `strength_gate=None` auto-selects). On the
  wire the odour is always one of `{lemon, grapefruit, lavender}` — `none` is a
  host-side detail that only ever lowers confidence.
- **`dummy_ml.py`** — the placeholder `Estimator` (`--dummy`), kept for
  transport testing without a model.
- **`models/`** — the trained artifacts (`classifier.joblib`,
  `regressor.joblib`, scalers, `metadata.json`), **copied from `ML/models/`**.
  `Server/` holds no training data — only the models, per the ML/Server
  boundary (see [`../CLAUDE.md`](../CLAUDE.md) and [`../ML/README.md`](../ML/README.md)).

> Models are trained offline in [`../ML/`](../ML/) and copied here. The deployed
> detect-SVM + baseline-relative config is now `train.py`'s default, so retraining
> and redeploying is just `python ../ML/train.py` (equivalently
> `--classifier-phase-filter detect --classifier-algo svm --baseline-relative`),
> then copy `ML/models/*` into `Server/models/`.

---

## Script reference

Every runnable script, what it's for, and its flags. Run all from the `Server/`
directory; each also takes `-h/--help`.

### `bme690_receiver.py` — capture sensor data to CSV

Initialises the 8 sensors, programs the heater profile + duty cycle from a
`.bmeconfig`, and streams one CSV row per (sensor, heater step). The data source
everything else consumes.

```bash
python bme690_receiver.py --check                    # probe hardware, print a summary, no capture
python bme690_receiver.py                             # capture → data/bme690_receiver_<ts>.csv
python bme690_receiver.py --config my.bmeconfig -o run.csv
```

| Flag | Default | Meaning |
|---|---|---|
| `--check` | off | Open board, init sensors, print a summary, exit. Run before a real capture to verify wiring. |
| `--config <file>` | newest `*.bmeconfig` in cwd | Heater profile + duty cycle to program. |
| `-o, --output <file>` | `data/bme690_receiver_<ts>.csv` | CSV output path. |

`Ctrl+C` stops cleanly; the CSV is flushed per row.

### `bme690_viz.py` — live 8-panel plot

Tails the newest capture CSV (no IPC with the receiver — pure file-tail) and
draws gas resistance per sensor, one line per heater step. Run in a second
terminal alongside the receiver.

```bash
python bme690_viz.py                                 # tail newest data/bme690_receiver_*.csv
python bme690_viz.py --file run.csv --window 120
```

| Flag | Default | Meaning |
|---|---|---|
| `--file <csv>` | newest capture | Tail a specific CSV instead of auto-discovering. |
| `--window <s>` | 60 | Seconds of history shown per sensor. |
| `--refresh-ms <ms>` | 500 | Redraw interval. |
| `-v, --verbose` | off | Tail diagnostics to stderr. |

### `ws_publisher.py` — WebSocket server + publisher

The piece the HoloLens connects to. Each tick it asks the ML for an inference
and broadcasts the §3 packet to all clients on `ws://<host>:<port>`. Pick **one**
data source — the default (no flags) spawns `bme690_receiver.py` and live-tails
it.

```bash
python ws_publisher.py                               # default: spawn receiver + tail + serve (needs hardware)
python ws_publisher.py --dummy                        # simulated waveform, no model/hardware (transport test)
python ws_publisher.py --replay-csv ../ML/data/raw/x.csv --replay-speed 4
python ws_publisher.py --live-tail                    # tail a receiver already running elsewhere
python ws_publisher.py --once                         # print one packet to stdout and exit
```

| Flag | Default | Meaning |
|---|---|---|
| `--dummy` | off | `DummyEstimator` (simulated) — no models or hardware needed. |
| `--replay-csv <path>` | — | Replay a captured CSV through the real model at real pace. |
| `--replay-speed <x>` | 1.0 | Replay speed multiplier. |
| `--live-tail` | off | Tail an already-running receiver instead of spawning one. |
| `--no-auto-receiver` | off | Spawn/tail nothing — estimator stays idle (transport-only). |
| `--host` / `--port` | `0.0.0.0` / `8765` | Bind address / port. |
| `--rate <hz>` | 5.0 | Packets per second. |
| `--once` | off | Emit one packet to stdout and exit (no server). |
| `--bmeconfig <file>` | `Sample.bmeconfig` | Identifies HP354 sensors; passed to the auto-spawned receiver. |
| `-v, --verbose` | off | Per-packet + per-cycle detail. |

`--dummy`, `--replay-csv`, `--live-tail`, and `--no-auto-receiver` are mutually
exclusive.

### `bmeconfig_to_profile.py` — inspect / convert a `.bmeconfig`

A helper, not part of the live path. Parses a `.bmeconfig` and (with `--print`)
shows a human-readable heater-profile breakdown.

```bash
python bmeconfig_to_profile.py Sample.bmeconfig --print
```

| Flag | Default | Meaning |
|---|---|---|
| `--print` | off | Print the parsed profile (steps, temps, cycle time). |
| `-o, --output <file>` | derived from input | Also emit a Python module of the profile. |
| `--profile-index <n>` | 0 | Which `heaterProfile` to summarise. |
| `--divisor <x>` | 1.0 | `timeBase*multiplier / divisor` = step ms. |

### `real_ml.py` / `dummy_ml.py` — inference (imported, not run directly)

Libraries used *by* `ws_publisher.py`, not standalone tools:

- **`real_ml.py`** — `RealEstimator` (loads `models/`, classify-then-regress with
  the strength gate) plus `replay_into` / `LiveCycleFeeder` (the CSV feeds).
  Exercised via `ws_publisher.py` (default or `--replay-csv`).
- **`dummy_ml.py`** — the `Estimator` interface + `DummyEstimator`. Exercised via
  `ws_publisher.py --dummy`. Running it directly (`python dummy_ml.py`) prints a
  few seconds of simulated inferences as a sanity check.

---

## Troubleshooting

### `ERROR: the coinespy package is not installed`

You skipped step 1 of Setup. Run `pip install -r requirements.txt`.

### `ERROR: could not open the Application Board over USB`

The board isn't reachable. Common causes:

- **Other program holds the COM port** — close BME AI-Studio, any serial
  terminal, or a previous `bme690_receiver.py` process.
- **Charge-only USB cable** — try a different cable that's known to carry
  data.
- **Driver not installed** — on Windows, install the COINES SDK (it bundles
  the driver). Check Device Manager: the board should appear as
  "Bosch Sensortec APP3.x Board (COMx)".
- **Wrong firmware** — if BME AI-Studio's firmware is on the board, the
  protocol won't match. Flash `coines-bridge` as in Setup step 2.

### `# no .bmeconfig found in the current directory`

The receiver auto-discovers `*.bmeconfig` in the working directory. Either
drop one in or pass `--config /path/to/yours.bmeconfig`.

### `# WARN: shuttle ID 0xNN is not 0x57 (BME690 8x)`

You're not running a BME690 8x shuttle. The receiver will still try, but
the per-sensor chip-select map is wrong for other shuttles and most sensors
will return `chip=0x00`. For a single-sensor shuttle (ID `0x93`), only
sensor 0 (`cs=0x10`) is meaningful.

### Some sensors show `chip_id=0x00` in `--check`

That sensor position is silent on the SPI bus. Possible reasons:

- The shuttle has a bad solder joint or dead die at that position
  (hardware issue — not fixable in software)
- The CS pin map is incorrect for your shuttle revision (open an issue
  with a photo of your shuttle's silkscreen)
- Some shuttle revisions only populate 4-6 of the 8 sensor slots — count
  the chips on your shuttle and see if that matches what's responding

### Receiver runs but no rows appear in the CSV

Rows are emitted only when a sample passes the strict `0xB0` filter
(new_data + gas_valid + heat_stab). For the first few seconds of a fresh
capture the heater is warming up and `heat_stab=0`, so rows are dropped.
Wait through one full cycle of the longest profile (look at the receiver's
stderr `cycle ≈ N.NN s` line) before deciding it's broken.

### Visualiser says "waiting for data..." forever

The viz auto-tails the newest `data/bme690_receiver_*.csv`. Confirm:

- a CSV is being written (`ls -la data/` should show recent timestamps)
- you started the viz from the same directory as the receiver
- pass `-v` to the viz for stderr diagnostics

---

## Project files

| File | Purpose |
|---|---|
| `bme690_receiver.py` | The receiver. Initialises sensors, programs heater profiles + duty cycle, streams CSV. |
| `bme690_viz.py` | Live matplotlib visualiser. Tails the CSV. |
| `bmeconfig_to_profile.py` | Parser for `.bmeconfig` JSON; also a CLI inspector (`python bmeconfig_to_profile.py FILE.bmeconfig --print`). |
| `ws_publisher.py` | Approach-A WebSocket server + publisher: runs the ML each tick, broadcasts the §3 packet to connected clients. |
| `real_ml.py` | `RealEstimator` — loads `models/`, runs classify-then-regress; estimates each sensor's clean-air baseline live for the baseline-relative classifier. |
| `dummy_ml.py` | Placeholder `Estimator` for pure transport testing (`--dummy`). |
| `models/` | Trained model artifacts (classifier/regressor + scalers + `metadata.json`), copied from `ML/models/`. |
| `Sample.bmeconfig` | Example BME AI-Studio config — two heater profiles, grouped layout. |
| `BME68x_registers.md` | Distilled BME69x register reference: addresses, calibration layout, compensation formulas. |
| `reference/` | The collaborator's C reader and its README — the demonstration that motivated this Python port. Not used at runtime. |
| `requirements.txt` | Python dependencies. |

## Authors and acknowledgements

- **Zhaoqi He** — Python receiver, visualiser, bmeconfig parser, project
  organisation.
- The collaborator whose C reader (`reference/bme690_69x_reader.c`) first
  demonstrated the SPI + per-CS approach for the BME690 8x shuttle. The
  Python receiver mirrors that flow.
- Bosch Sensortec for the
  [BME690 SensorAPI](https://github.com/boschsensortec/BME690_SensorAPI),
  whose register definitions and compensation polynomials are ported here.

## License

MIT — see [LICENSE](LICENSE). The BME690 SensorAPI that informed the
register parsing and compensation polynomials is BSD-3-Clause at its
[upstream repo](https://github.com/boschsensortec/BME690_SensorAPI); no
Bosch code is bundled here.
