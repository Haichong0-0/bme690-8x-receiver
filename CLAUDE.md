# CLAUDE.md — Codebase map

Orientation file for working in `C:\kurf\Codebase`. This is the root of a BSc
dissertation project (George / Zhaoqi He, King's College London):
**XR Smell Reconstruction** — classify an odour from a gas sensor and render it
on a HoloLens 2 as a 3D visual whose appearance reflects *which* smell and *how
strong* it is.

Read [`plan.md`](plan.md) for the full target architecture. This file is the
quick index of what each folder actually contains *today*.

---

## The intended pipeline (per plan.md)

```
BME690 8x sensor ──SPI──▶ Python host ──┐
                          classify (odour ID)
                          + regress (0→1 intensity)
                                         │  JSON packets @ ~5 Hz
                                         ▼
                          WebSocket  (A: ws:// LAN  |  B: wss:// relay)
                                         │
                                         ▼
                          HoloLens 2 (Unity + MRTK3) — per-odour prefab,
                                         intensity-driven magnitude
```

Odours: **lemon, grapefruit, lavender**. The HoloLens only ever receives
*results* (odour + intensity), never raw sensor data. Message contract is in
plan.md §3 (`{timestamp, odour, odour_confidence, intensity, seq}`).

---

## Folder map

### `Server/`  — ACTIVE. The real, current codebase. (Renamed from `Receiver/`.)
Pure-Python live capture from a **Bosch APP3.1 board + BME690 8x shuttle**
(8 sensors over SPI, one chip-select each) via `coinespy` + `coines-bridge`
firmware. Streams CSV matching BME AI-Studio's `.bmerawdata` schema.

- Has its **own git repo** (`Server/.git`) — the other two folders do not.
- **`bme690_receiver.py`** (~960 lines) — the receiver. Ports Bosch's BME69x
  SensorAPI register/compensation logic to Python. CLI: `--check` (hardware
  probe, no capture), `--config <file.bmeconfig>`, `-o <out.csv>`. Writes
  `data/bme690_receiver_<ts>.csv`.
- **`bme690_viz.py`** — live matplotlib 8-panel plot; tails the newest CSV
  (pure file-tail, no IPC).
- **`bmeconfig_to_profile.py`** — parses AI-Studio `.bmeconfig` (heater
  profiles + duty cycle); also a CLI inspector (`--print`).
- **`Sample.bmeconfig`** — working example config (constant-320 °C profile).
- **`reference/`** — collaborator's C reader (`bme690_69x_reader.c`) + sample
  CSV that first proved the SPI/per-CS approach. **Not used at runtime.**
- **`README.md`** — thorough setup/troubleshooting.
- `BME68x_registers.md` — distilled register/calibration/compensation reference.
- **`dummy_ml.py`** — the original placeholder classifier+regressor
  (`Estimator`/`Inference` interface + `DummyEstimator`). Still used for pure
  transport testing via `ws_publisher.py --dummy`.
- **`real_ml.py`** (new) — `RealEstimator`, the real `Estimator` implementation.
  Loads `models/{classifier,regressor}.joblib` + scalers + `metadata.json`
  (copied from `ML/models/`, produced by `ML/train.py`) and turns buffered
  sensor cycles into an `Inference` (see `ML/EXPERIMENTS.md` for the
  classifier/regressor algorithm + preprocessing sweep that picked them:
  a **4-class SVM** (rbf, `probability=True`) classifier on `raw_gradient`
  features — the 10 log-resistance steps plus their 9 step-to-step gradients,
  the temperature-sweep shape — and a RandomForest regressor, 3-cycle window;
  LORO classifier accuracy 0.837). The classifier runs in **"detect"** mode
  (`train.py --classifier-phase-filter detect --classifier-algo svm`): a
  dedicated clean-air **"none"** class (baseline cycles) alongside the three
  odour classes {lemon, grapefruit, sorange}, and the odour classes are trained
  across a concentration *range* (cycles with `y_conc ≥ 0.4`, spanning
  rise/plateau/decay — not just the plateau). So the model rejects clean air
  itself (labels it "none" 97% of the time, up from 0%) and identifies an odour
  before it reaches the plateau — the low-concentration fix that replaced the
  old plateau-only logistic-regression classifier. The regressor's targets are
  now **rise-anchored linear strength labels** (`y_conc` read off linearly
  between the observed baseline (0) and plateau (1) rather than a per-segment
  exponential fit), lifting it to LORO R² 0.891 / MAE 0.067 (from R² 0.828).
  `real_ml.py` reads `classifier_features` from `metadata.json` and applies
  the matching transform live (`_classifier_features`), so the serving path
  stays in sync with whatever `train.py` deployed.
  Buffers are fed via `push_cycle(sensor_id, step_vector)`; empty buffers
  report a neutral/idle reading rather than guessing. Deliberately has no
  import dependency on `ML/` (duplicates the causal subset of its feature
  extraction) so `Server/` stays deployable on its own — see the module
  docstring for the two documented train/serve mismatches (no Stage-1
  low-pass filtering, no phase-aware cycle selection for the classifier).
  `RealEstimator` **auto-detects the "none" class**: for the deployed detect
  model it reports the best *real* odour (never "none") with P(that odour) as
  `odour_confidence`, and drops the **strength gate** to a 0.15 backstop
  (`DEFAULT_STRENGTH_GATE_WITH_NONE`) — the classifier now does the no-odour
  detection the gate used to stand in for, so the earlier live lemon↔grapefruit
  confusion (clean-air cycles classified grapefruit ~100% of the time) is
  handled by the model, not the gate. Legacy plateau-only classifiers (no
  "none" class) still fall back to the 0.6 gate (`DEFAULT_STRENGTH_GATE`) as
  their primary clean-air guard. Wire contract unchanged — `odour` is always one
  of {lemon, grapefruit, sorange}; "none" is host-side only. Also provides
  `replay_into()` / `build_replay_events()`, which replay a captured CSV's
  cycles into a `RealEstimator` at real pace, standing in for live hardware.
- **`ws_publisher.py`** — Approach-A WebSocket **server** (runs *with* the
  detector). Calls the ML each tick and broadcasts the plan.md §3 JSON packet
  to all connected clients at a fixed rate. This is the "publisher + server" the
  Unity client dials into. Uses `RealEstimator` by default now (`--dummy` for
  the old simulated waveform); `--replay-csv <path>` feeds it a captured
  CSV's cycles at real pace via `real_ml.replay_into`.

> **Gap vs plan:** the real classify-then-regress model is now wired in
> (`real_ml.py`) and the WebSocket transport + message contract (plan §3) are
> implemented, but the receiver's **live** sensor stream is still **not wired
> directly into** the publisher — `bme690_receiver.py` only writes CSV today.
> `--replay-csv` bridges this for demos by replaying a past capture (point it
> at any CSV path, e.g. one from `ML/`'s copy of the training data — by
> design `Server/` doesn't hold training CSVs itself, see `.gitignore`).
> True live capture → publisher wiring, and the Unity MRTK3 client (plan §5),
> remain to build.

### `Fruit_vis/`  — ACTIVE. The new HoloLens client (plan §5).
Unity **6000.4.11f1** (Unity 6.4), URP, new Input System. The fresh build of the
packet **receiver + visualiser**. XR stack (no MRTK), all declared in
`Packages/manifest.json`: Unity OpenXR + XR Plug-in Management + AR Foundation
(pinned ≥6.x for Unity 6) + **Microsoft's Mixed Reality OpenXR Plugin**
(`com.microsoft.mixedreality.openxr`, local tarball under `Packages/MixedReality/`
— Unity's own OpenXR package has no UWP/HoloLens loader, so without Microsoft's
plugin the UWP tab of XR Plug-in Management says "no XR plugins applicable").
`ENABLE_VR` is force-defined in Player scripting defines (Standalone + UWP):
Input System ≥1.19 compiles `XRDeviceDescriptor.characteristics` as private
without it, breaking the OpenXR package before any loader can be enabled
(chicken-and-egg via Safe Mode). OpenXR still needs to be *enabled* for the UWP
target in Project Settings → XR Plug-in Management after any settings reset —
without that a build runs as a flat 2D slate, not immersive stereo. Camera is
set to Solid Color / black for HoloLens's additive display.
- `Assets/Scripts/SmellPacket.cs` — §3 message model (JsonUtility-mapped).
- `Assets/Scripts/SmellReceiver.cs` — `ClientWebSocket` client; background receive
  Task → thread-safe `ConcurrentQueue`; auto-reconnect. Configurable `serverUrl`,
  overridable at runtime from `<persistentDataPath>/server_url.txt` (UWP:
  package's LocalState folder, editable via Device Portal's File Explorer) —
  lets you repoint at a new host IP when switching networks without a
  rebuild/redeploy. Drains the queue on the main thread each frame and exposes
  `LatestPacket` / `HasReceivedData` / `PacketsPerSecond` so multiple consumers
  can read the feed without racing over the queue themselves.
- `Assets/Scripts/SmellVisualiser.cs` — per-odour visuals (array of
  odour→prefab-or-placeholder, adapted from `HololensFruit/ModelController.cs`'s
  model-array-toggle pattern), toggled active by the packet's `odour`; intensity
  →scale, smoothing, confidence gate, idle decay (§6). Real fruit prefabs can be
  dropped into the `odourVisuals` array later — placeholders (coloured spheres)
  are used until then.
- `Assets/Scripts/PacketDebugHud.cs` — dev-time-only debug HUD; builds its own
  Canvas/Text at runtime (no scene setup) and shows the raw wire packet (seq,
  timestamp, odour, confidence, intensity, link state, rate).
- `Assets/Scripts/BodyLockedFollow.cs` — generic body-locked "tag-along +
  billboard" follow (MRTK RadialView/Follow-solver equivalent, hand-rolled since
  MRTK isn't installed). Used by the debug HUD only.
- Deployed and verified end-to-end on HoloLens 2 (Unity OpenXR, no MRTK) over
  Approach A / local Wi-Fi against `ws_publisher.py`.

**Anchoring decision (resolves plan.md §8's open item):** researched Microsoft's
Mixed Reality comfort guidelines — rigid head-locked content (1:1 translate +
rotate with the camera) is explicitly called out as causing discomfort;
body-locked tag-along is the documented alternative for HUDs/auxiliary overlays,
while primary content should stay **world-locked**. Applied here: `SmellVisualiser`'s
odour objects are world-locked (unchanged); only `PacketDebugHud` is body-locked
via `BodyLockedFollow`, placed ~1.5 m out (HoloLens' optical focal plane is
~2.0 m; comfort zone is 1.25-5 m) with lerped reposition-only-outside-view-cone
and lerped billboard rotation — never rigidly parented to the camera.
Sources: [Comfort](https://learn.microsoft.com/en-us/windows/mixed-reality/design/comfort),
[Billboarding and tag-along](https://learn.microsoft.com/en-us/windows/mixed-reality/design/billboarding-and-tag-along).

**Camera rig note:** the scene has a plain `Main Camera`, no `XR Origin` /
`TrackedPoseDriver`. Head tracking still works (Unity's built-in XR stereo
camera path), fine for a stationary AR app with no virtual locomotion — but if
teleport/locomotion is ever added, follow Unity's standard: never move
`Camera.main` directly, move an `XR Origin` parent instead.

### `FruitServer/`  — REMOVED (July 2026, sent to Recycle Bin).
Was a Unity 2019.4.16 standalone app whose single script listened for UDP text
packets on port 8888 and appended them to `D:\FruitData.txt` — the PC-side sink
for the old HoloLens fruit demo's interaction telemetry. Nothing in the current
pipeline used it.

### `HololensFruit/`  — OLD. Reusable assets, wrong architecture.
Unity **2019.4.16**, HoloLens 2 / UWP, **MRTK v2** (full source in
`Assets/MRTK`), Windows MR (`com.unity.xr.windowsmr.metro`). A fruit
visualization/interaction trainer: voice commands show 3D fruit models
(Apple, Banana, **Lemon, Orange**…), scale/reset them, and spawn duplicate
prefabs at symmetric angles around the user.

- **`Assets/Scripts/ModelController.cs`** — 10-model gallery, `KeywordRecognizer`
  voice control, scaling, angle-based spawning.
- **`Assets/Scripts/LogManager.cs`** — UDP singleton that sends interaction logs
  (`category,name,delay`) **out** to `192.168.1.100:8888` (i.e. to FruitServer).
- `NumberSelectorUI.cs`, `Function.cs`, `CloseMain.cs` — UI panels.
- Asset packs: `Food - Fruits Pack/`, `CartoonFoodPack2/` — 3D fruit models.

> **HololensFruit was paired with the removed FruitServer:** HoloLens
> *displayed* fruit and *sent* UDP telemetry out to FruitServer's logger. This
> is the **reverse** of the planned data flow (where HoloLens *receives* a live
> signal that *drives* the visual). Kept only for its fruit model asset packs.

---

## How the old code relates to the plan (important)

| Aspect            | Plan target                          | Old `HololensFruit`/`FruitServer`        |
|-------------------|--------------------------------------|------------------------------------------|
| Toolkit           | **MRTK3**                            | MRTK **v2**                              |
| Unity             | LTS compatible with MRTK3 (newer)    | 2019.4.16                                |
| HoloLens role     | WebSocket **client**, receives data  | UDP **sender** (telemetry out)           |
| Transport         | WebSocket (`ws://` / `wss://`)       | raw UDP                                   |
| Host              | **Python** (the Server)              | Unity C# (`FruitServer`)                  |
| Visual driver     | live `intensity` signal → magnitude  | voice commands / manual                  |

The networking and control direction differ fundamentally; the *assets*
(fruit models, MRTK scene, voice/scale scaffolding) are the salvageable part.

---

## Conventions / gotchas
- Platform is **Windows / PowerShell** (Bash tool also available). Paths like
  `D:\FruitData.txt` and `C:\COINES_SDK\...` appear in code/docs.
- Only `Server/` is version-controlled. Edits to the Unity folders aren't
  tracked by git.
- `ML/` (new) is the offline preprocessing/training side — reads captured CSVs
  out of `Server/alog/data/`, has no runtime dependency on `Server/`, and isn't
  a git repo of its own. See [`ML/README.md`](ML/README.md).
- When working on Unity folders, touch only `Assets/`, `ProjectSettings/`,
  `Packages/manifest.json` — everything else (`Library/`, `obj/`, `Logs/`,
  `.vs/`, `Builds/`, `QCAR/`) is generated.
