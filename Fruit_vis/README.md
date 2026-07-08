# Fruit_vis — HoloLens 2 smell visualiser

The Unity client for [XR Smell Reconstruction](../README.md). It dials out to
the Python host over WebSocket, receives the odour + intensity packets (the
[`../plan.md`](../plan.md) §3 contract), and renders a per-odour 3D object
scaled by the live intensity. Deployed and verified end-to-end on a real
HoloLens 2 over local Wi-Fi.

This is a fresh, **MRTK-free** build on Unity's OpenXR stack — only the small
amount of XR needed for a stationary AR overlay, nothing more. (The old
MRTK-based project is [`../HololensFruit/`](../HololensFruit/README.md), kept
only for assets.)

- **Unity:** `6000.4.11f1` (Unity 6.4), URP, new Input System
- **Build target:** UWP · ARM64 · IL2CPP (HoloLens 2)
- **WebSocket client:** `System.Net.WebSockets.ClientWebSocket` (same client for
  Approach A `ws://` LAN and Approach B `wss://` relay — only the URL changes)

---

## Scripts

All under `Assets/Scripts/`. This is the entire app — five small files.

| File | Role |
|---|---|
| `SmellPacket.cs` | The §3 message model. `JsonUtility`-mapped struct; field names **must** match the JSON keys `ws_publisher.py` emits. `TryParse` returns false on malformed input. |
| `SmellReceiver.cs` | The WebSocket client. Background receive `Task` → thread-safe `ConcurrentQueue` → drained on the main thread each frame; auto-reconnect. Exposes `LatestPacket` / `HasReceivedData` / `PacketsPerSecond` so multiple consumers read the feed without racing. |
| `SmellVisualiser.cs` | Turns packets into visuals: per-odour object (real prefab or coloured placeholder sphere), `intensity`→scale with smoothing, a confidence gate, and idle decay when the confident feed stops. |
| `PacketDebugHud.cs` | Dev-only overlay showing the raw wire packet (seq, odour, confidence, intensity, link state, rate). Builds its own Canvas at runtime — just drop it on any object. Remove for release. |
| `BodyLockedFollow.cs` | Generic body-locked "tag-along + billboard" follow (a hand-rolled MRTK RadialView equivalent). Used by the debug HUD only. |

### Threading rule (do not break)

Frames are received on a **background Task** and pushed into a queue; **no Unity
API is touched off the main thread**. `SmellReceiver.Update()` drains the queue
on the main thread. If you extend the client, keep this — Unity API calls from
the receive task will crash or misbehave.

### Anchoring

Primary content (the odour objects in `SmellVisualiser`) is **world-locked**.
Only the debug HUD is **body-locked** (via `BodyLockedFollow`), per Microsoft's
Mixed Reality comfort guidance — rigid head-locked content causes discomfort, so
the HUD lazily follows and billboards instead of rigidly parenting to the
camera. Don't parent visuals directly to `Camera.main`.

---

## Run it in the editor (fastest dev loop)

1. Start a publisher on your PC (see [`../Server/README.md`](../Server/README.md)):
   ```bash
   cd ../Server && python ws_publisher.py --dummy
   ```
2. Open this project in Unity `6000.4.11f1`.
3. On the `SmellReceiver` component, set **`serverUrl`** to `ws://127.0.0.1:8765`
   (same machine) or `ws://<pc-lan-ip>:8765`.
4. Press Play. The `PacketDebugHud` should show packets arriving and the
   placeholder object should scale with intensity.

---

## Build & deploy to HoloLens 2

### One-time project settings (and the gotchas that bite)

This project is already configured, but if settings ever reset (Safe Mode, a
fresh clone that regenerated `Library/`, a Unity upgrade) these are the
non-obvious things that **must** be true, in order:

1. **Microsoft's Mixed Reality OpenXR plugin must be present.** It's vendored
   as a local tarball at `Packages/MixedReality/com.microsoft.mixedreality.openxr-1.11.2.tgz`
   and referenced from `Packages/manifest.json`. Unity's *own* OpenXR package
   has **no UWP/HoloLens loader** — without Microsoft's plugin, the UWP tab of
   **Project Settings → XR Plug-in Management** shows "no XR plugins applicable".
2. **`ENABLE_VR` must be in Player scripting define symbols** (Standalone **and**
   UWP). Input System ≥1.19 compiles `XRDeviceDescriptor.characteristics` as
   private without it, which breaks the OpenXR package *before any loader can be
   enabled* — a chicken-and-egg that drops you into Safe Mode. It's force-defined
   here; keep it.
3. **OpenXR must be enabled for the UWP target** in Project Settings → XR
   Plug-in Management (the UWP tab, not just Standalone). If it isn't, the app
   builds and runs as a **flat 2D slate**, not immersive stereo. This is the
   single most common "it deployed but it's not in AR" cause — check this first.
4. **Main Camera → Clear Flags = Solid Color, background = black.** HoloLens'
   display is additive, so black renders as transparent.

> **Camera rig note:** the scene uses a plain `Main Camera` — no `XR Origin` /
> `TrackedPoseDriver`. Head tracking still works via Unity's built-in XR stereo
> path, which is fine for a *stationary* AR app. If you ever add
> teleport/locomotion, switch to an `XR Origin` and move that, never
> `Camera.main` directly.

### Build steps

1. **File → Build Profiles / Build Settings →** platform **Universal Windows
   Platform**. Architecture **ARM64**, Build Type **D3D Project**, Scripting
   Backend **IL2CPP**, Target Device **HoloLens**.
2. Build to a folder → open the generated `.sln` in **Visual Studio**.
3. In VS: configuration **Release**, platform **ARM64**, target **Remote
   Machine** (the HoloLens IP) or **Device** over USB. Deploy.

First deploy pairs with the device (PIN from the HoloLens *Settings → Update →
For developers*). Subsequent deploys are incremental.

### Point it at your host

The connect URL is a config value, not hard-coded — two ways to set it:

- **Before building:** the `serverUrl` field on the `SmellReceiver` component.
- **After deploying, no rebuild:** drop a one-line `server_url.txt` (containing
  e.g. `ws://192.168.1.40:8765`) into the app's `LocalState` folder via the
  HoloLens **Device Portal → File Explorer**. `SmellReceiver` reads it at
  startup and overrides `serverUrl`. This lets you repoint at a new host IP when
  you switch networks without redeploying.

---

## XR / package stack

Declared in [`Packages/manifest.json`](Packages/manifest.json):

| Package | Version | Why |
|---|---|---|
| `com.microsoft.mixedreality.openxr` | 1.11.2 (local tarball) | The HoloLens/UWP OpenXR loader Unity's package lacks |
| `com.unity.xr.openxr` | 1.17.1 | OpenXR runtime |
| `com.unity.xr.management` | 4.5.1 | XR loader lifecycle |
| `com.unity.xr.arfoundation` | 6.4.3 | AR session/anchoring types (pinned ≥6.x for Unity 6) |
| `com.unity.inputsystem` | 1.19.0 | New Input System (drives the `ENABLE_VR` requirement above) |
| `com.unity.render-pipelines.universal` | 17.4.0 | URP |

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Deploys but shows a flat 2D window, not stereo AR | OpenXR not enabled for the **UWP** target (settings gotcha #3) |
| UWP tab says "no XR plugins applicable" | Microsoft MR OpenXR tarball missing/unreferenced (gotcha #1) |
| Project drops to Safe Mode / OpenXR won't compile | `ENABLE_VR` missing from scripting defines (gotcha #2) |
| Content renders but the world is opaque black | Camera not set to Solid Color / black (gotcha #4) |
| HUD shows `DISCONNECTED / reconnecting...` | Wrong `serverUrl`, publisher not running, or a firewall blocking `:8765` on the host. Confirm the PC's LAN IP and that `ws_publisher.py` printed `# serving ws://...`. |
| Connected but no packets | Publisher is up but idle (e.g. `--no-auto-receiver`, or real models report `intensity=0`). Try `--dummy` to confirm the link. |

Only `Assets/`, `ProjectSettings/`, and `Packages/manifest.json` are
source-controlled; `Library/`, `Temp/`, `Build/`, `Logs/`, `obj/` are
generated — don't commit them (the `.gitignore` already excludes them).
