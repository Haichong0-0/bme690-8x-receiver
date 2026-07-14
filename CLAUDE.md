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
  a **4-class SVM** (rbf, `probability=True`) classifier on **baseline-relative
  `raw_gradient`** features — the 10 log-resistance steps plus their 9
  step-to-step gradients (the temperature-sweep shape), each step first shifted
  by that run/sensor's own clean-air baseline (log R/R₀) — and a RandomForest
  regressor, 3-cycle window; drift-robust LORO classifier accuracy 0.771). The
  classifier runs in **"detect"** mode and is now the `train.py` default
  (`--classifier-phase-filter detect --classifier-algo svm --baseline-relative`):
  a dedicated clean-air **"none"** class (baseline cycles) alongside the three
  odour classes {lemon, grapefruit, lavender}, and the odour classes are trained
  across a concentration *range* (cycles with `y_conc ≥ 0.4`, spanning
  rise/plateau/decay — not just the plateau). So the model rejects clean air
  itself (labels it "none" ~100% of the time, up from 0%) and identifies an odour
  before it reaches the plateau — the low-concentration fix that replaced the
  old plateau-only logistic-regression classifier. **Baseline-relative is the
  drift fix:** absolute log-resistance encodes the session (humidity, board,
  day), and since each odour was captured on its own day the raw-level model
  latched onto *which day* rather than the odour — inflating LORO to a leaky
  0.925 and collapsing to a single class on a live sensor at a different
  humidity/operating point. Subtracting each session's own clean-air baseline is
  drift-invariant: the honest number is lower (0.771; odour-vs-odour ~0.6, `none`
  100%) but it survives live. The regressor's targets are **rise-anchored linear
  strength labels** (`y_conc` read off linearly between the observed baseline (0)
  and plateau (1) rather than a per-segment exponential fit), giving LORO R²
  0.891 / MAE 0.083 on the 15-run set. `real_ml.py` reads `classifier_features`
  and `baseline_relative` from `metadata.json` and applies the matching
  transform live (`_classifier_features`, plus live baseline subtraction), so
  the serving path stays in sync with whatever `train.py` deployed.
  Buffers are fed via `push_cycle(sensor_id, step_vector)`; empty buffers
  report a neutral/idle reading rather than guessing. Deliberately has no
  import dependency on `ML/` (duplicates the causal subset of its feature
  extraction) so `Server/` stays deployable on its own — see the module
  docstring for the two documented train/serve mismatches (no Stage-1
  low-pass filtering, no phase-aware cycle selection for the classifier).
  `RealEstimator` **auto-detects the "none" class**: for the deployed detect
  model it reports the best *real* odour (never "none") with P(that odour) as
  `odour_confidence`. Because the model is baseline-relative it **estimates the
  live clean-air baseline per sensor** — accumulate cycles, discard a minimum
  warmup (`N_WARMUP_SKIP` 12), then freeze the per-step median of the last
  `N_BASELINE_CYCLES` (8) **once the clean-air level has flattened** (adaptive:
  the last 8 cycles' mean log-R drifts ≤ `BASELINE_STABLE_EPS` 0.05, ~5%, else
  keep waiting up to `N_BASELINE_MAX_CYCLES` 90 then force + warn) — and subtracts
  it before classifying (a sensor isn't classified until its baseline is
  captured). The stability gate matters because a cold first-run-of-day keeps
  climbing well past 3 min; freezing on a fixed count would bake a too-low
  baseline in. This is what makes the classifier survive the live drift the old
  raw-level model died on. The **strength gate** is now vestigial: the `none` class does
  the clean-air rejection it used to stand in for, so both gates are currently
  **0** (`DEFAULT_STRENGTH_GATE_WITH_NONE` / `DEFAULT_STRENGTH_GATE`, disabled
  for live testing; the design backstop for a `none`-class model is 0.15, and
  legacy no-`none` plateau models used 0.6). Wire contract unchanged — `odour`
  is always one of {lemon, grapefruit, lavender}; "none" is host-side only. Also provides
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

### `fruit_unity/`  — ACTIVE (new). HoloLens client on Unity's Mixed Reality template.
Unity **6000.4.11f1**, created from Unity Hub's **Mixed Reality template**
(`com.unity.template.mixed-reality`) — brings a preconfigured XR Origin rig,
**XR Interaction Toolkit** 3.4.1, **XR Hands** 1.7.3, AR Foundation, URP, plus
sample scenes under `Assets/Samples/`. Has its own `.git`. Migrated from
`Fruit_vis/`:
- The five scripts copied verbatim into `Assets/Scripts/` (no code changes).
- `com.microsoft.mixedreality.openxr` 1.11.2 — the template does **not** ship a
  HoloLens/UWP loader (its own docs tell you to install this via the MR Feature
  Tool). Installed as an **embedded package** at
  `Packages/com.microsoft.mixedreality.openxr/` (NOT a `file:` tarball ref and
  NOT in `manifest.json` — Unity auto-discovers embedded packages), because it
  needs a local source patch (below). Pristine tarball kept at
  `ThirdParty/com.microsoft.mixedreality.openxr-1.11.2.tgz`; diff against it to
  see the patch. Re-extracting the tarball over the folder **loses the patch**.
- **Local patch — `Editor/Settings/PlatformValidation.cs`,
  `GenerateHL2RenderGraphRule()`:** Unity 6000.4's URP removed Render Graph
  compatibility mode — `RenderGraphSettings.enableRenderCompatibilityMode` is
  now `[Obsolete]`, get-only, hardcoded `false`. The plugin assigns it, giving
  `CS0200: ... cannot be assigned to -- it is read only` (plus a cascading
  Burst/Cecil "Failed to resolve assembly 'Assembly-CSharp'", which is just
  fallout from Assembly-CSharp never being built). Patched to short-circuit the
  rule under `UNITY_6000_4_OR_NEWER`; older-editor branches untouched. The rule
  was an optional perf hint with no remedy left on 6000.4.
- **AR Foundation pinned to 6.3.5, NOT 6.4.x** (template default was 6.4.3).
  AF **6.4.0-pre.1** added its own `UnityEngine.XR.ARSubsystems.XRMarker` +
  `ARMarkerManager`, which collide with the identically-named types Microsoft's
  plugin has shipped for years. On 6.4.x the MR plugin fails to compile:
  `CS0104: 'XRMarker' is an ambiguous reference` and a knock-on `CS0311` in
  `ARMarkerManager.cs`. 6.3.5 is the last AF release before those types existed.
  Nothing else in the template depends on AR Foundation (XRI, XR Hands and
  Composition Layers all declare no AF dependency), so the downgrade is free.
  Revisit if Microsoft ships a plugin release that guards the AF-6.4 collision.
- **Removed `com.unity.xr.androidxr-openxr` and `com.unity.xr.meta-openxr`** —
  Android XR / Meta Quest support we don't target. Removing androidxr also cures
  the "asset in immutable package was unexpectedly altered
  (`.../androidxr-openxr/Assets/URP/UniversalRenderPipelineAsset.asset`)" warning:
  URP's asset upgrader rewrites that file inside the read-only package cache.
- UWP `platformCapabilities` added (template ships them **empty** — without
  `InternetClient` the WebSocket receiver silently fails to connect on device):
  InternetClient, InternetClientServer, PrivateNetworkClientServer,
  SpatialPerception.
- **UWP scripting defines must be exactly `USE_STICK_CONTROL_THUMBSTICKS`.**
  Remove the template's `USE_INPUT_SYSTEM_POSE_CONTROL`, and never add
  `ENABLE_VR`, on the **UWP** tab. (Standalone is different — see below.)
  **Unity 6000.4 does not ship `UnityEngine.VRModule` for UWP** — legacy
  built-in VR is gone there, HoloLens goes through OpenXR. Proof: the compiler
  response file `Library/Bee/artifacts/*.dag/Unity.RenderPipelines.Core.Runtime.rsp`
  contains `-define:UNITY_WSA` but no `-r:...UnityEngine.VRModule.dll`, while the
  same file in `Fruit_vis` (`-define:UNITY_STANDALONE_WIN`) does have it.
  Consequences, both of which cost a day in July 2026:
  - `ENABLE_VR` on UWP makes URP compile `XRSettings` (which lives *only* in
    `UnityEngine.VRModule.dll`) against a module that isn't referenced →
    `XRSRPSettings.cs: CS0103: The name 'XRSettings' does not exist` on ~8 lines.
    The define is harmless on Standalone and poison on UWP — which is why it
    fixed `Fruit_vis` and broke this project.
  - `USE_INPUT_SYSTEM_POSE_CONTROL` (a define the *template* sets project-wide;
    OpenXR's asmdef does not) makes OpenXR compile
    `using PoseControl = UnityEngine.InputSystem.XR.PoseControl;`, and Input
    System only declares that type under `(ENABLE_VR || UNITY_GAMECORE)` →
    `OculusTouchControllerProfile.cs(12): CS0234`. Dropping the define is safe:
    OpenXR ships its own `Runtime/input/PoseControl.cs` fallback, and XRI /
    XR Hands reference the define in **zero** files.

  Both errors cascade: URP core fails → OpenXR, `Assembly-CSharp` and
  `MRTemplate` never build → Burst reports
  `Failed to resolve assembly 'Assembly-CSharp'` and the console shows
  `"Performant URP Renderer Config is missing RendererFeatures"`. Those two are
  *symptoms of failed compilation*, not separate bugs. Diagnose from the `.rsp`
  (`-define:` and `-r:` lines), not from the error text.
- **Editing `ProjectSettings.asset` while the editor is open is pointless** —
  Unity caches scripting defines in memory and rewrites the file on quit,
  clobbering the edit. Either close Unity first, or change defines in
  Player Settings → Other Settings → Scripting Define Symbols.
- OpenXR loader **is** enabled for UWP: `Assets/XR/XRGeneralSettingsPerBuildTarget.asset`
  → "Metro Providers" → `OpenXRLoader.asset`. (Standalone is on the AR
  `SimulationLoader` instead — that's the template's editor-play default.)

**Scene: the template's own `Assets/Scenes/SampleScene.unity`** (the build
scene). Kept deliberately — it brings prebuilt lighting, the platform-aware
background (`ARFeatureController` handles passthrough vs skybox), and the hand-
tracking rig (`MR Interaction Setup` prefab, which holds the XR Origin + Main
Camera). We add exactly one root object to it:
- **`SmellSource`** at (0, -0.2, 1.5) — carries `SmellReceiver` +
  `SmellVisualiser` (wired to each other) and `PacketDebugHud`. All three live
  on this single object. The HUD is *not* parented to the camera: there is no
  camera in the scene YAML (it's inside the `MR Interaction Setup` prefab), and
  `PacketDebugHud` builds its own body-locked canvas at runtime, resolving
  `Camera.main` via `BodyLockedFollow`. So nothing to maintain scene-side.

Scene roots, in order: `MR Interaction Setup` (rig/hands) → `Permissions Manager`
→ `Lighting` → `Environment` → `SmellSource`. The template's **onboarding-tutorial
UI was stripped** (72 YAML blocks: the `UI` root and its whole subtree — Coaching
UI, Tutorial Player, Delete All Button, Tap Tooltip / Tooltip Worldspace, Spatial
Panel Manipulator and its affordance demos, Snap Volume). Backup at
`SampleScene.unity.bak`.

> **Stripping the tutorial requires disabling `Goal Manager`.** It lives inside
> the `MR Interaction Setup` prefab, and its `ProcessGoals()` — called every frame
> from `Update()` — dereferences `m_GoalPanelLazyFollow` **unguarded**
> (`MRTemplateAssets/Scripts/GoalManager.cs:196`). With the Coaching UI gone that
> NREs every frame. Handled by an `m_Enabled: 0` prefab-instance override on the
> GoalManager MonoBehaviour (prefab-internal fileID `9177915868812651432`), plus
> nulling every `objectReference` in the instance's `m_Modifications` that pointed
> into the deleted subtree. Don't re-enable it without restoring that UI.

> **Do NOT delete `Assets/Samples/XR Interaction Toolkit/3.4.1/{AR Starter
> Assets, Hands Interaction Demo, Starter Assets}`.** They look like disposable
> demo content but are **not self-contained**: `MRTemplateAssets` prefabs
> (`MR Interaction Setup` → XR Origin Hands rig + `XRI Default Input Actions`,
> `HandMenuSetupVariant_MRTemplate`, `Permissions Manager Variant`, …) reference
> into all three, and `SampleScene` is built from those prefabs. Deleting them
> breaks the camera rig and hand tracking. (Tried in July 2026, reverted.)
> The camera is three prefabs deep:
> `MR Interaction Setup` → `XR Origin Hands (XR Rig)` (Hands Interaction Demo) →
> `XR Origin (XR Rig)` (Starter Assets) — which is why `PacketDebugHud` lives on
> `SmellSource` and resolves `Camera.main` at runtime rather than being parented.
> `Assets/Samples/XR Hands/1.7.3/HandVisualizer/` *is* free-standing.

Still to do in-editor: enable OpenXR + Microsoft HoloLens feature group on the
**UWP** tab of XR Plug-in Management, then build.

### `Fruit_vis/`  — SUPERSEDED by `fruit_unity/`, kept as a verified fallback.
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
