# Feasibility report on Live Gas Detection on BME690 8x 

## Goal

**Live detection** from a Bosch APP3.1 board (BME690 8x shuttle, ID `0x57`),
emitting two streams programmatically as the sensor measures:

1. **Class** — the predicted gas/event label from a trained classifier
   (BME AI-Studio model trained on our `.bmeconfig` heater profile).
2. **Concentration** — a continuous estimate of the detected analyte's level
   (e.g. ppm-equivalent or normalised intensity), updated in real time.

Both must be available to downstream code (file/socket/Python API) the moment
they're produced at end-of-session.

## Existing approaches


| Path                                                               | What it is                                                      | Live?                  | Programmatic? | Works for 8x shuttle?                             |
| ------------------------------------------------------------------ | --------------------------------------------------------------- | ---------------------- | ------------- | ------------------------------------------------- |
| BME AI-Studio + DemoSample firmware                                | Record to onboard flash, copy via MTP, convert with Bosch tools | Live view*in app only* | No (GUI only) | Yes — official path                              |
| BME AI-Studio Live Test mode                                       | Trained classifier shown live in app                            | Yes (in app only)      | No            | Yes                                               |
| coines-bridge + coinespy + Python                                  | Drive I²C/SPI on the shuttle from Python, do own compensation  | Yes                    | Yes           | **Reaches register read, fails on parallel mode** |
| Raspberry Pi + single-sensor breakout (Pimoroni / Adafruit / PI3G) | I²C BME690 on a Pi, classifier via BSEC Python wrapper         | Yes                    | Yes           | N/A — different hardware                         |

## Important caveat on the BME AI-Studio paths

The live view and live-test classifier output exist only inside the BMEDesktop application — the values update on screen, but the app holds the USB port exclusively and exposes no real-time IPC, socket, or streaming export. We can save a CSV at session end, but we cannot consume live samples or predictions from our own code while a run is in progress. For programmatic real-time use this path is effectively unavailable.

The community pattern ([teach-your-pi-to-sniff-with-bme688](https://github.com/mcalisterkm/teach-your-pi-to-sniff-with-bme688)) splits the workflow: **train on the 8x shuttle, deploy live on a single-sensor breakout**. No public project live-streams from the 8x shuttle to Python.

## Why "live Python from the 8x shuttle" doesn't exist

- **The 8x shuttle is a training tool, not a runtime platform.** Bosch markets
  it for parallel data collection across 8 identical sensors during model
  development, not for field deployment.
- **No public driver or schematic for the 8x shuttle.** Bosch's
  [`BME690_SensorAPI`](https://github.com/boschsensortec/BME690_SensorAPI) examples target the single-sensor shuttle (`0x93`); the 8x
  shuttle (`0x57`) needs per-sensor select logic that isn't documented.
- **BME AI-Studio's live USB protocol is closed.** It works, but Bosch hasn't
  published the frame format. Reverse-engineering it is possible but not done
  publicly.
- **Empirically:** our coinespy-based driver gets chip ID + variant + a few
  measurements out of FIELD_0, but parallel mode never completes a full cycle
  on this shuttle — heater never stabilises, gas remains invalid, raw pressure
  reads as a near-zero ADC. Consistent with the sensor receiving config but
  the shuttle's per-sensor power/enable lines being uncontrolled.

## Possible approaches forward

Ranked by effort × likelihood of success.

1. **Train on 8x, run live on a single-sensor breakout**
   Record + label on the APP3.1 + 8x via BME AI-Studio, export the BSEC
   config, run live inference on a Raspberry Pi (or any I²C host) with a
   Pimoroni / Adafruit BME690 breakout. Established pattern, working examples,
   ~$15 of extra hardware. **Live + Python from day one.**
2. **B1 + tail-read CSV exports.** Record short sessions in BME AI-Studio,
   have Python tail the exported CSV. Crude pseudo-live (seconds of latency),
   no hardware purchase, no protocol work.
3. **Continue debugging coinespy on 8x shuttle.** Try Bosch's full init
   sequence (power-cycle VDD, SDO low, STANDARD_MODE I²C, set `CONFIG`,
   forced mode first). Uncertain — could break through, could hit deeper
   shuttle-specific quirks we can't see without schematics.
4. **Reverse-engineer BME AI-Studio's USB protocol.** Capture the USB-CDC
   traffic the app sends, decode the frame format, mirror it from Python.
   Highest effort, highest payoff (true live raw + classifier in Python on
   this hardware), but undocumented and brittle.
