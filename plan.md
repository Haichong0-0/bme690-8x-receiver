# XR Smell Reconstruction — HoloLens 2 Stage Plan

**Project:** Gas/odour classification and XR smell reconstruction (BME690 → ML → HoloLens 2) **This document:** Architecture and build plan for the XR receiver stage **Odours:** Lemon, grapefruit, lavender (essential oils) **Author:** George — King's College London BSc dissertation

---

## 1. Goal

Take the output of the live data receiver (odour identity + relative 0→1 intensity, produced by the classify-then-regress pipeline on the Python host) and render it on a HoloLens 2 as a 3D visual whose appearance reflects *which* smell is present and *how strong* it is, at a believable relative intensity.

Success = HoloLens displays the correct per-odour 3D object, scaled/animated by the live intensity signal, with low enough latency to feel responsive (<\~500 ms end-to-end).

---

## 2. End-to-end architecture

```
BME690 sensor
    │  (resistance scan vectors)
    ▼
Python host  ── classify (odour ID) + regress (0→1 intensity)
    │  (sliding-window inference over live stream)
    ▼
WebSocket publisher  ── emits compact JSON packets at 2–5 Hz
    │
    ▼
Transport (§4): A — direct LAN  OR  B — relay over internet
    │
    ▼
HoloLens 2 — Unity + MRTK3 app
    │  WebSocket client receives packets
    ▼
Smell visualiser  ── per-odour prefab, intensity-driven magnitude
```

The ML is already done on the host. The HoloLens only ever receives **results**, never raw sensor data. This keeps the payload tiny and the device-side logic simple.

---

## 3. Signal / message contract

The single interface between host and HoloLens. Lock this down first — everything else depends on it.

**Packet (JSON text frame):**

```json
{
  "timestamp": 1719772800.123,
  "odour": "lemon",
  "odour_confidence": 0.94,
  "intensity": 0.62,
  "seq": 1043
}
```


| Field              | Type       | Meaning                                                           |
| ------------------ | ---------- | ----------------------------------------------------------------- |
| `timestamp`        | float      | Host epoch time when inference ran                                |
| `odour`            | string     | One of`lemon`/`grapefruit`/`lavender`(extend later)               |
| `odour_confidence` | float 0–1 | Classifier confidence; lets HoloLens ignore low-confidence frames |
| `intensity`        | float 0–1 | Normalised relative concentration → drives visual magnitude      |
| `seq`              | int        | Monotonic counter; lets receiver drop stale/out-of-order frames   |

**Rules:**

* Emit at a fixed rate (start at 5 Hz).
* `intensity` is already normalised (1 = saturation plateau, 0 = clean baseline) — no scaling needed device-side.
* Receiver should treat a missing/idle stream gracefully (decay visual to nothing if no packet for N seconds).

---

## 4. Transport — two supported approaches

The system supports **two deployment modes**, sharing the same message contract (§3), the same `ClientWebSocket` client, and the same visualiser. Only the connection target changes. Build Approach A as the simpler baseline, then add Approach B for remote operation.

The key design principle that makes both work: the **HoloLens is always the client that dials out**, and the Python side always speaks WebSocket. Swapping modes is just changing the URL the HoloLens connects to (`ws://<lan-ip>` vs `wss://<relay-domain>`).

### Approach A — Local network (direct)

Host and HoloLens are on the **same WiFi/LAN**. The Python host runs a WebSocket **server**; the HoloLens connects straight to it.

```
HoloLens 2 ──ws://<host-LAN-ip>:8765──▶ Python host (WebSocket server + publisher)
```

* Simplest — no third machine, no public infrastructure.
* Works because on a shared LAN the host is directly reachable at its private IP (e.g. `192.168.1.40`); there's no NAT in between.
* `ws://` (plain, unencrypted) is acceptable on a trusted local network, which also avoids the TLS-cert hurdle.
* **Use for:** same-room demos, development, debugging the visual loop.

### Approach B — Remote relay (over the internet)

Host and HoloLens are on **different networks**. A WebSocket **relay** with a stable public address sits in the middle; both ends connect *outward* to it.

```
Python host ──connect out──▶  RELAY (public wss://)  ◀──connect out── HoloLens 2
   (publisher role)            forwards messages          (subscriber role)
```

* **Why a relay:** NAT and CGNAT block *incoming* connections but allow *outgoing* ones. Campus/home networks won't let you port-forward to the host. Both ends dialling out sidesteps all of it.
* **Relay options:** a cheap/free-tier VPS running a tiny custom WebSocket relay (\~50–100 lines; recommended, keeps everything pure WebSocket and matches `ClientWebSocket`), or a managed MQTT broker (zero relay code, but reintroduces a protocol mismatch with `ClientWebSocket`).
* **Requirements:**`wss://` (TLS) over port 443; a real TLS cert (Let's Encrypt + domain) so the HoloLens trusts it — self-signed certs are rejected by `ClientWebSocket` on UWP; a connect-token so only your host and HoloLens can use the relay.
* **Use for:** the genuinely remote deployment — host in the lab, HoloLens anywhere.

**Minimal relay logic (Approach B):**

```
on publisher message → forward to every connected subscriber
on subscriber connect → register; on disconnect → deregister
ping/pong keepalive on all connections
```

### Comparison


|                      | A — Local network        | B — Remote relay                   |
| -------------------- | ------------------------- | ----------------------------------- |
| Topology             | HoloLens → host directly | Host → relay ← HoloLens           |
| Python role          | WebSocket**server**       | WebSocket**client**(publisher)      |
| URL on HoloLens      | `ws://<lan-ip>:8765`      | `wss://<relay-domain>`              |
| Encryption           | Plain`ws://`(trusted LAN) | `wss://`(TLS, required)             |
| Extra infrastructure | None                      | VPS/broker + domain + cert          |
| Cost                 | £0                       | £0 possible (free-tier VPS/broker) |
| NAT/firewall issues  | None (same subnet)        | Solved by outward dialling          |
| Best for             | Demo, dev, debugging      | True remote operation               |

---

## 5. HoloLens 2 / Unity build

### Stack

* **Unity** (LTS version compatible with MRTK3)
* **MRTK3** (Mixed Reality Toolkit) — spatial rendering, hand tracking, anchoring
* **Build target:** UWP, ARM64, IL2CPP
* **WebSocket client:**`System.Net.WebSockets.ClientWebSocket` (committed) — same client for both approaches; only the connect URL differs (`ws://` LAN vs `wss://` relay). Verify it builds and runs under UWP/IL2CPP early.
* **Tip:** make the connect URL a config value, not hard-coded, so you can switch between Approach A and B without rebuilding logic.

### Threading rule (important)

Receive WebSocket frames on a **background task**; never block Unity's main thread. Push parsed packets into a thread-safe queue; drain the queue on the main thread in `Update()` to apply visual changes. Unity API calls must happen on the main thread only.

### Receive loop sketch

```
background: connect → loop { receive frame → parse JSON → enqueue }
main thread (Update): while queue not empty → dequeue → update target state
```

---

## 6. Smell → 3D mapping

Two packet fields drive the visual:

**`odour` selects the object/material:**

* lemon → citrus / yellow particle cloud
* grapefruit → pink-red cloud
* lavender → purple wisp
* (one prefab per odour, swapped on identity change)

**`intensity` (0→1) drives magnitude** — map to one or more of:

* particle emission rate
* cloud density / opacity
* object scale
* animation speed

**Smoothing:** lerp the visual toward the target each frame rather than snapping, so per-frame inference noise doesn't make the object jitter. Optionally gate on `odour_confidence` (ignore frames below a threshold).

This reuses the exact same 0→1 signal that would drive a physical XR emitter — just rendered visually.

---

## 7. Build order (milestones)

Develop on **Approach A (local network)** first — it's the fastest way to prove the whole loop — then add **Approach B (relay)** once the visual works.

1. **Lock the message contract** (§3) — agree the JSON, write it down.
2. **Host WebSocket server (Approach A)** — Python runs a LAN WebSocket server emitting dummy packets at 5 Hz (hard-coded odour + ramping intensity). No ML yet; proves the host side.
3. **Unity WebSocket receiver** — `ClientWebSocket` connects to the host over LAN (`ws://<lan-ip>`), parse, log packets to console. Background thread + main-thread queue. Make the connect URL a config value. Verify UWP/IL2CPP build works here — early, not late.
4. **Single static visual** — one prefab, scale driven by `intensity`. Prove the loop end-to-end over LAN with dummy data.
5. **Per-odour prefabs** — switch object/material on `odour`.
6. **Smoothing + confidence gating + idle decay.**
7. **Swap dummy publisher for the real classify-then-regress output** (still on LAN).
8. **Add Approach B (relay)** — stand up the VPS relay (`wss://`, TLS cert via domain, connect-token); refactor the Python host into a publisher that dials out; point the HoloLens config URL at the relay. Same visualiser, just a different endpoint.
9. **Latency + robustness pass** — measure end-to-end lag on both approaches, handle disconnects, auto-reconnect, ping/pong keepalive.

---

## 8. Open decisions / to confirm

* [X]  **Transport:** both supported — Approach A (local) and Approach B (relay) (§4). Develop on A, deploy on B.
* [X]  **Unity WebSocket client:**`ClientWebSocket` — same for both approaches, URL is configurable.
* [ ]  **Relay host (Approach B):** own VPS + custom relay, or managed MQTT broker?
* [ ]  **Domain + TLS cert (Approach B):** needed for `wss://` the HoloLens will trust.
* [ ]  **Connect auth (Approach B):** token scheme so only your host + HoloLens can use the relay.
* [ ]  Packet rate: start 5 Hz, tune for latency vs smoothness.
* [ ]  Idle timeout: how long with no packet before the visual decays to nothing.
* [ ]  Visual design per odour: agree the look (particle system parameters, colours, forms).
* [ ]  Anchoring: world-locked in space, or head-locked / hand-attached?

---

## 9. Risks

* **NAT/CGNAT/campus firewall (Approach B)** blocks any direct internet connection → solved by the relay (both ends dial out). Approach A avoids this entirely by staying on one LAN.
* **TLS cert not trusted by HoloLens (Approach B)** → `ClientWebSocket` on UWP rejects untrusted/self-signed certs. Use a real cert (Let's Encrypt + domain) on the relay; test the `wss://` handshake from the device early. Approach A sidesteps this with plain `ws://` on a trusted LAN.
* **Relay single point of failure (Approach B)** → if the VPS is down, nothing flows. Add auto-reconnect on both ends; consider a managed broker if uptime matters.
* **WebSocket build incompatibility with UWP/IL2CPP** → test `ClientWebSocket` on-device in milestone 3, not late.
* **Main-thread blocking** in Unity → enforce background-receive + queue pattern from the start.
* **Inference jitter** making the visual flicker → smoothing + confidence gating (milestone 7).
