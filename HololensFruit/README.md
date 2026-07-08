# HololensFruit — legacy fruit demo (kept for assets)

> **This is not the current app.** The live HoloLens client for
> [XR Smell Reconstruction](../README.md) is [`../Fruit_vis/`](../Fruit_vis/README.md).
> This older project is kept in the repo for its **3D fruit asset packs** and
> its MRTK scene/voice/scale scaffolding — the networking and control direction
> are the *wrong* way round for this pipeline (explained below).

A Unity **2019.4.16** / HoloLens 2 (UWP) fruit visualization-and-interaction
trainer built on **MRTK v2** (full source under `Assets/MRTK`) and Windows
Mixed Reality (`com.unity.xr.windowsmr.metro`). Voice commands show 3D fruit
models, scale/reset them, and spawn duplicates at symmetric angles around the
user.

## What's here

| File (`Assets/Scripts/`) | Purpose |
|---|---|
| `ModelController.cs` | 10-model fruit gallery; `KeywordRecognizer` voice control; scaling; angle-based spawning. **The reusable pattern** — its model-array toggle inspired `Fruit_vis/SmellVisualiser.cs`. |
| `LogManager.cs` | UDP singleton that *sends* interaction logs (`category,name,delay`) out to `192.168.1.100:8888`. |
| `NumberSelectorUI.cs`, `Function.cs`, `CloseMain.cs` | UI panels. |

Asset packs worth keeping: **`Assets/Food - Fruits Pack/`** and
**`Assets/CartoonFoodPack2/`** — the 3D fruit models. These are the real reason
this project is still in the repo; drop them into `Fruit_vis/`'s
`SmellVisualiser.odourVisuals` array to replace the placeholder spheres with
real fruit.

## Why it's the wrong architecture

It was paired with a now-removed `FruitServer` (a Unity UDP sink that logged to
`D:\FruitData.txt`). The HoloLens *displayed* fruit and *sent telemetry out* —
the **reverse** of this project's data flow, where the HoloLens *receives* a
live signal that *drives* the visual.

| Aspect | This project's target (`Fruit_vis`) | HololensFruit (legacy) |
|---|---|---|
| Toolkit | Unity 6 OpenXR (no MRTK) | MRTK **v2** |
| Unity | 6000.4.11f1 | 2019.4.16 |
| HoloLens role | WebSocket **client**, *receives* data | UDP **sender** (telemetry out) |
| Transport | WebSocket (`ws://` / `wss://`) | raw UDP |
| Visual driver | live `intensity` signal → magnitude | voice commands / manual |

The *assets* (fruit models, MRTK scene, voice/scale scaffolding) are the
salvageable part; the networking and control direction are not.

## Notes if you open it

- Needs **Unity 2019.4.16** specifically (MRTK v2 era). Don't upgrade it in
  place just to browse assets — export the packs and import them into
  `Fruit_vis/` instead.
- Only `Assets/`, `ProjectSettings/`, and `Packages/` are meaningful;
  `Library/`, `Temp/`, `obj/`, `Builds/`, `QCAR/` are generated and
  `.gitignore`d.
- **Two large assets are excluded from git** (they exceed GitHub's 100 MB
  limit) and live on local disk only: `Assets/msyhbd SDF.asset` (a TextMeshPro
  font atlas, regenerable from its `.ttc`) and a ~105 MB `.obj` model under
  `Assets/模型/`. Neither is used by the active pipeline; if you clone fresh
  they simply won't be present.
