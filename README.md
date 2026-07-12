# XR Smell Reconstruction

Classify an odour from a gas sensor and render it on a **HoloLens 2** as a 3D
visual whose appearance reflects *which* smell is present and *how strong* it
is. A BSc dissertation project (Zhaoqi He / George, King's College London).

A Bosch **BME690 8-sensor** array scans a headspace over a temperature-cycled
heater profile; a Python host classifies the odour and regresses a 0→1
relative strength; the result is broadcast as a tiny JSON packet over
WebSocket; a Unity/HoloLens client turns each packet into a per-odour object
scaled by the live intensity.

```
BME690 8x sensor ──SPI──▶ Python host ─────────────────────┐
                          classify (odour) + regress (0→1)  │
                                                            │  {timestamp, odour,
                                                            │   odour_confidence,
                                                            │   intensity, seq}
                                                            │  JSON @ ~5 Hz
                                                            ▼
                          WebSocket  (A: ws:// LAN  |  B: wss:// relay)
                                                            │
                                                            ▼
                          HoloLens 2 (Unity 6 + OpenXR) — per-odour object,
                                                          intensity-driven scale
```

The HoloLens only ever receives **results** (odour + intensity), never raw
sensor data — the payload stays tiny and the device-side logic simple.

---

## What's in this repo

Four projects plus the planning docs. Each module has its own README with the
detail; this table is the index.

| Folder | What it is | Status | README |
|---|---|---|---|
| **[`Server/`](Server/README.md)** | Python: BME690 capture, the live classify-then-regress inference, and the WebSocket publisher the HoloLens connects to. | **Active** | [Server/README.md](Server/README.md) |
| **[`ML/`](ML/README.md)** | Offline pipeline that turns raw captures into a labelled dataset and trains the classifier + regressor. No runtime dependency on `Server/`. | **Active** | [ML/README.md](ML/README.md) · [PREPROCESSING.md](ML/PREPROCESSING.md) · [EXPERIMENTS.md](ML/EXPERIMENTS.md) |
| **[`Fruit_vis/`](Fruit_vis/README.md)** | Unity 6 / HoloLens 2 client — the WebSocket receiver + smell visualiser. Deployed and verified on-device. | **Active** | [Fruit_vis/README.md](Fruit_vis/README.md) |
| **[`HololensFruit/`](HololensFruit/README.md)** | Legacy Unity 2019 / MRTK v2 fruit demo. Kept for its 3D fruit asset packs; **not** the current architecture. | Legacy | [HololensFruit/README.md](HololensFruit/README.md) |

Planning / reference docs at the root: **[`plan.md`](plan.md)** (the target
architecture and build milestones), **[`progress.md`](progress.md)** (a
status snapshot of what's built), and **[`CLAUDE.md`](CLAUDE.md)** (an
orientation map used when working on the code with an AI assistant).

---

## The message contract

The single interface between host and HoloLens — a JSON text frame, one per
inference (see [`plan.md`](plan.md) §3). Everything else on both sides is built
around this; it's deliberately tiny and stable.

```json
{
  "timestamp": 1719772800.123,
  "odour": "lemon",
  "odour_confidence": 0.94,
  "intensity": 0.62,
  "seq": 1043
}
```

| Field | Type | Meaning |
|---|---|---|
| `timestamp` | float | Host epoch seconds when inference ran |
| `odour` | string | `lemon` / `grapefruit` / `lavender` (see below) |
| `odour_confidence` | float 0–1 | Classifier confidence; the client can ignore low-confidence frames |
| `intensity` | float 0–1 | Normalised relative strength → drives the visual's magnitude |
| `seq` | int | Monotonic counter; lets the receiver drop stale/out-of-order frames |

The Python side emits it (`Server/ws_publisher.py`), the C# side parses it
(`Fruit_vis/Assets/Scripts/SmellPacket.cs`). If you change one, change both.

---

## The odours

The models are trained on **lemon, grapefruit, lavender** — matching
[`plan.md`](plan.md). (An earlier capture round used `sorange` (sweet orange) in
lavender's place; those sessions were re-collected as lavender, so the pipeline's
odour-vs-plan check now passes with no substitution.)

All captures so far are at a single nominal concentration (0.6) — **15 runs** in
all (6 lemon, 6 grapefruit, 3 lavender); the later "v2" runs are held well past
exposure to capture the full decay tail. The classifier carries a clean-air
**`none`** class (handled host-side, never sent on the wire), so it flags an
odour even at low concentration yet correctly rejects clean air ~100% of the
time, where the earlier model — having no "no-odour" class — always mislabelled
clean air as an odour. `lemon` is the residual confusion sink (both grapefruit
and lavender leak into it); odour-vs-odour discrimination is ~0.6. See the
discussion in [`ML/README.md`](ML/README.md).

---

## Quick start

Three ways in, from zero-dependency to full hardware. All commands are run
from the repo root unless noted.

### Option A — no hardware, no models (proves the transport + visual loop)

`ws_publisher.py --dummy` emits a simulated waveform (odour cycling,
intensity ramping) with no sensor and no trained model — the fastest way to
see the whole loop light up.

```bash
cd Server
pip install -r requirements.txt
python ws_publisher.py --dummy        # serves ws://<this-pc-ip>:8765
```

Then run the `Fruit_vis` client (in the Unity editor, or deployed to a
HoloLens) pointed at `ws://<this-pc-ip>:8765`. See
[`Fruit_vis/README.md`](Fruit_vis/README.md).

### Option B — replay a real capture through the real models

Needs a capture CSV **and** trained models in `Server/models/` (neither is
committed — see *What's in the repo vs. what you provide* below).

```bash
cd Server
python ws_publisher.py --replay-csv ../ML/data/raw/<some_capture>.csv --replay-speed 4
```

### Option C — live hardware

A Bosch APP3.x board + BME690 8x shuttle flashed with `coines-bridge`
firmware. The publisher spawns the receiver itself and live-tails its CSV.

```bash
cd Server
python bme690_receiver.py --check     # verify wiring first
python ws_publisher.py                # spawn receiver + tail + serve
```

Full hardware setup (firmware, `.bmeconfig`, troubleshooting) is in
[`Server/README.md`](Server/README.md).

---

## Requirements

| For | You need |
|---|---|
| `Server/` + `ML/` | **Python 3.8+**; `pip install -r requirements.txt` in each. Key libs: `coinespy` (hardware), `websockets`, `numpy`/`pandas`/`scikit-learn`/`scipy`/`joblib` (ML), `matplotlib` (viz). |
| Live capture | Bosch **Application Board 3.0/3.1** + **BME690 8x shuttle** (ID `0x57`), a data-capable USB cable, and the **COINES SDK** with `coines-bridge` firmware flashed. |
| `Fruit_vis/` | **Unity 6000.4.11f1** (Unity 6.4). Build target UWP / ARM64 / IL2CPP for HoloLens 2. |
| `HololensFruit/` (legacy) | Unity **2019.4.16** — only if you need to open the old project for its assets. |

## What's in the repo vs. what you provide

To keep the repo self-contained and reasonably sized, some regenerable/large
inputs are **not** committed (they're `.gitignore`d):

- **Raw captures** (`ML/data/raw/*.csv`) — the sensor recordings. Bring your
  own, or capture with `Server/`.
- **Trained models** (`ML/models/`, `Server/models/`) — regenerate with
  `python ML/train.py`, then copy `ML/models/*` → `Server/models/`.
- **Two oversized legacy HololensFruit assets** (a font SDF atlas and a 105 MB
  `.obj`) — kept on local disk only; see [`HololensFruit/README.md`](HololensFruit/README.md).

So a fresh clone runs **Option A immediately**; Options B and C need a capture
and/or trained models you supply.

---

## Status

Built and working: end-to-end capture → CSV, the offline ML pipeline + trained
models (drift-robust classifier LORO accuracy 0.771, regressor R² 0.891), the
live classify-then-regress publisher, and the HoloLens client (verified on-device
over local Wi-Fi, Approach A). The classifier uses **baseline-relative** features
(each session's clean-air level subtracted) so it survives live sensor drift; the
raw-level model scored a higher-looking 0.925 offline but collapsed to a single
class on a live sensor at a different humidity. Known gaps: the live sensor stream reaches the
publisher via CSV tail rather than a direct in-process feed; **Approach B** (the
remote `wss://` relay) is designed but unbuilt; and the dataset covers only a
single nominal concentration (0.6), so behaviour across concentration levels is
unvalidated. See [`progress.md`](progress.md) and each module's README.

## Repository history

This is a single repository combining what were previously separate projects.
`Server/` and `Fruit_vis/` each had their own git history, now preserved on
GitHub at `Haichong0-0/bme690-8x-receiver` and `Haichong0-0/Fruit_vis`; this
combined repo starts fresh.

## License & acknowledgements

Original code is **MIT** (see [`Server/LICENSE`](Server/LICENSE)). Bundled
third-party components keep their own licenses — notably Microsoft's **MRTK**
and the fruit **asset packs** under `HololensFruit/`, and the register/
compensation logic in `Server/` informed by Bosch's BSD-3-Clause
[BME690 SensorAPI](https://github.com/boschsensortec/BME690_SensorAPI) (no
Bosch code bundled). Author: **Zhaoqi He (George)**, King's College London.
