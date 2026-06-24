"""BME690 8x shuttle receiver — PARALLEL mode + BME AI-Studio duty cycle.

Operating model (matches BME AI-Studio's `gas_scan` flow):

  Setup (once per sensor):
    soft reset → chip ID → calibration → unique ID
    program all 10 RES_HEAT_x and GAS_WAIT_x slots
    program GAS_WAIT_SHARED (the per-step base wait)
    set CTRL_GAS_1 = run_gas | nb_conv=9
    flip CTRL_MEAS → PARALLEL_MODE

  Runtime (one polling loop, no per-step host involvement):
    every ~140 ms:
      for each sensor:
        read 51 bytes (FIELD_0/1/2)
        for each of 3 rotating fields:
          if status == 0xB0 (new_data + gas_valid + heat_stab):
            decode + emit one CSV row
            update last_meas_index

The chip runs the heater profile autonomously, rotating each completed
measurement into the next FIELD_0/1/2 slot. The host's job is just to
notice fresh samples and persist them. `BME69X_VALID_DATA = 0xB0` is the
canonical Bosch filter from `examples/parallel_mode/parallel_mode.c`.

Duty cycle:
  After `scan_n` full profile runs (gas_index sees 0→9 that many times)
  the receiver puts every sensor back into SLEEP mode, idles for
  `sleep_n × cycle_duration` of wall time, then re-arms parallel mode.
  No CSV rows are emitted during the sleep phase — same as AI-Studio.

CSV columns mirror BME AI-Studio's `.bmerawdata` schema.
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import coinespy as cpy

from bmeconfig_to_profile import convert as parse_bmeconfig


# ---------------------------------------------------------------------------
# Register addresses, masks, defaults
# ---------------------------------------------------------------------------

REG_CHIP_ID         = 0xD0
REG_VARIANT_ID      = 0xF0
REG_RESET           = 0xE0
REG_UNIQUE_ID       = 0x83
REG_CTRL_GAS_0      = 0x70
REG_CTRL_GAS_1      = 0x71
REG_CTRL_HUM        = 0x72
REG_CTRL_MEAS       = 0x74
REG_CONFIG          = 0x75
REG_FIELD0          = 0x1D
REG_RES_HEAT_0      = 0x5A
REG_GAS_WAIT_0      = 0x64
REG_GAS_WAIT_SHARED = 0x6E
REG_COEFF1          = 0x8A
REG_COEFF2          = 0xE1
REG_COEFF3          = 0x00
REG_MEM_PAGE        = 0xF3

MEM_PAGE_MSK   = 0x10
MEM_PAGE0_VAL  = 0x10
MEM_PAGE1_VAL  = 0x00

LEN_COEFF1, LEN_COEFF2, LEN_COEFF3 = 23, 14, 5
LEN_COEFF_ALL = LEN_COEFF1 + LEN_COEFF2 + LEN_COEFF3
LEN_FIELD = 17
N_FIELDS = 3
LEN_ALL_FIELDS = LEN_FIELD * N_FIELDS   # 51 bytes — one read covers FIELD_0/1/2

EXPECTED_CHIP_ID = 0x61
EXPECTED_VARIANT = 0x02

MODE_SLEEP    = 0b00
MODE_PARALLEL = 0b10
RUN_GAS_ON    = 0x20   # bit 5 in CTRL_GAS_1
NB_CONV_MSK   = 0x0F

# OSR — matches examples/parallel_mode/parallel_mode.c exactly.
OS_TEMP = 2   # ×2
OS_PRES = 5   # ×16
OS_HUM  = 1   # ×1

# BME AI-Studio's strict-valid filter: new_data + gas_valid + heat_stab.
BME69X_VALID_DATA = 0xB0

# CS-pin map for the 8x shuttle.
SENSOR_CS_PINS: List[int] = [0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x1D, 0x1E]

# Fallback target if the bmeconfig doesn't supply a usable timeBase.
# Matches the constant used in Bosch's parallel_mode.c example.
FALLBACK_TARGET_PER_STEP_MS = 140

AMBIENT_INIT_C = 25.0
MAX_CONSECUTIVE_ERRORS = 5

DATA_DIR = Path("data")

CSV_HEADER = [
    "time", "sensor_index", "sensor_id", "timestamp_since_poweron",
    "real_time_clock", "temperature", "pressure", "relative_humidity",
    "resistance_gassensor", "heater_profile_step_index", "target_c",
    "scanning_enabled", "scanning_cycle_index", "label_tag", "error_code",
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Calib:
    par_t1: int = 0; par_t2: int = 0; par_t3: int = 0
    par_p1: int = 0; par_p2: int = 0; par_p3: int = 0; par_p4: int = 0
    par_p5: int = 0; par_p6: int = 0; par_p7: int = 0; par_p8: int = 0
    par_p9: int = 0; par_p10: int = 0; par_p11: int = 0
    par_h1: int = 0; par_h2: int = 0; par_h3: int = 0; par_h4: int = 0
    par_h5: int = 0; par_h6: int = 0
    par_g1: int = 0; par_g2: int = 0; par_g3: int = 0
    res_heat_range: int = 0; res_heat_val: int = 0; range_sw_err: int = 0


@dataclass
class SensorState:
    cs_pin: int
    index: int
    calib: Calib = field(default_factory=Calib)
    variant_id: int = 0
    sensor_id: int = 0
    consecutive_errors: int = 0
    disabled: bool = False
    # Per-sensor running state.
    last_meas_index: Optional[int] = None
    last_step: Optional[int] = None
    profile_runs: int = 0
    # Per-sensor heater profile. Different sensors can run different
    # profiles in BME AI-Studio's grouped layout (e.g., even sensors
    # stabilising while odd sensors gas-scan).
    profile: Optional["ProfileInfo"] = None


@dataclass
class ProfileInfo:
    """Compact view of the bmeconfig we need at runtime."""
    target_c: List[int]          # 10 entries, °C per step
    multipliers: List[int]       # 10 entries, raw multipliers from bmeconfig
    gas_wait_shared_ms: int      # one shared per-step wait
    cycle_ms: float              # estimated total time per heater profile run

    @property
    def n_steps(self) -> int:
        return len(self.target_c)


# ---------------------------------------------------------------------------
# Two's-complement helpers
# ---------------------------------------------------------------------------


def s8(b):  return b - 256 if b > 127 else b
def s16(lo, hi):
    v = (hi << 8) | lo
    return v - 65536 if v > 32767 else v
def u16(lo, hi): return (hi << 8) | lo


# ---------------------------------------------------------------------------
# SPI with BME69x memory-page tracking
# ---------------------------------------------------------------------------

_PAGE_STATE: dict = {}


def invalidate_page_cache(cs: int) -> None:
    _PAGE_STATE.pop(cs, None)


def _ensure_page(board: cpy.CoinesBoard, cs: int, reg: int) -> None:
    want = MEM_PAGE1_VAL if reg > 0x7F else MEM_PAGE0_VAL
    if _PAGE_STATE.get(cs) == want:
        return
    cur = list(board.read_spi(cpy.SPIBus.BUS_SPI_0, REG_MEM_PAGE, 1,
                              sensor_interface_detail=cs))[0]
    new = (cur & ~MEM_PAGE_MSK) | want
    if new != cur:
        board.write_spi(cpy.SPIBus.BUS_SPI_0, REG_MEM_PAGE & 0x7F, new,
                        sensor_interface_detail=cs)
    _PAGE_STATE[cs] = want


def spi_read(board, cs: int, reg: int, n: int = 1) -> List[int]:
    _ensure_page(board, cs, reg)
    return list(board.read_spi(cpy.SPIBus.BUS_SPI_0, reg, n,
                               sensor_interface_detail=cs))


def spi_write(board, cs: int, reg: int, val) -> None:
    _ensure_page(board, cs, reg)
    board.write_spi(cpy.SPIBus.BUS_SPI_0, reg & 0x7F, val,
                    sensor_interface_detail=cs)


# ---------------------------------------------------------------------------
# Calibration parsing — bme69x.c::get_calib_data
# ---------------------------------------------------------------------------


def parse_calib(buf: List[int]) -> Calib:
    assert len(buf) == LEN_COEFF_ALL
    c = Calib(); a = buf
    c.par_t2 = u16(a[0], a[1]); c.par_t3 = s8(a[2]); c.par_t1 = u16(a[31], a[32])
    c.par_p5 = s16(a[4], a[5]);  c.par_p6 = s16(a[6], a[7])
    c.par_p7 = s8(a[8]);          c.par_p8 = s8(a[9])
    c.par_p1 = s16(a[10], a[11]); c.par_p2 = u16(a[12], a[13])
    c.par_p3 = s8(a[14]);         c.par_p4 = s8(a[15])
    c.par_p9 = s16(a[18], a[19]); c.par_p10 = s8(a[20]); c.par_p11 = s8(a[21])
    par_h5 = (a[23] << 4) | (a[24] >> 4)
    if par_h5 > 2047: par_h5 -= 4096
    par_h1 = (a[25] << 4) | (a[24] & 0x0F)
    if par_h1 > 2047: par_h1 -= 4096
    c.par_h5 = par_h5; c.par_h1 = par_h1
    c.par_h2 = s8(a[26]); c.par_h4 = s8(a[27])
    c.par_h3 = a[28];     c.par_h6 = a[29]
    c.par_g2 = s16(a[33], a[34]); c.par_g1 = s8(a[35]); c.par_g3 = s8(a[36])
    c.res_heat_val   = s8(a[37])
    c.res_heat_range = (a[39] & 0x30) >> 4
    c.range_sw_err   = (s8(a[41]) & 0xF0) // 16
    return c


def read_sensor_id(board, cs: int) -> int:
    b = spi_read(board, cs, REG_UNIQUE_ID, 4)
    return (b[3] << 24) | (b[2] << 16) | (b[1] << 8) | b[0]


# ---------------------------------------------------------------------------
# Heater encoding
# ---------------------------------------------------------------------------


def calc_res_heat(c: Calib, target_c: int, amb_c: float) -> int:
    target_c = min(target_c, 400)
    var1 = (c.par_g1 / 16.0) + 49.0
    var2 = ((c.par_g2 / 32768.0) * 0.0005) + 0.00235
    var3 = c.par_g3 / 1024.0
    var4 = var1 * (1.0 + (var2 * target_c))
    var5 = var4 + (var3 * amb_c)
    code = 3.4 * ((var5 * (4.0 / (4.0 + c.res_heat_range)) *
                   (1.0 / (1.0 + c.res_heat_val * 0.002))) - 25.0)
    return max(0, min(255, int(code)))


def calc_gas_wait(dur: int) -> int:
    """Pack a value into the gas_wait byte: (factor[7:6] | multiplier[5:0])."""
    if dur >= 0xFC0:
        return 0xFF
    factor = 0
    d = int(dur)
    while d > 0x3F:
        d //= 4
        factor += 1
    return (d & 0x3F) | (factor << 6)


def parallel_meas_dur_us() -> int:
    """T/P/H + gas measurement duration in µs for parallel mode (no wake-up).
    From bme69x.c::bme69x_get_meas_dur."""
    cycles = [0, 1, 2, 4, 8, 16]
    meas_cycles = cycles[OS_TEMP] + cycles[OS_PRES] + cycles[OS_HUM]
    return meas_cycles * 1963 + 477 * 4 + 477 * 5
    # parallel mode does NOT add the 1 ms wake-up.


# ---------------------------------------------------------------------------
# Compensation
# ---------------------------------------------------------------------------


def calc_temperature(raw_t: int, c: Calib) -> float:
    do1 = c.par_t1 << 8
    dtk1 = c.par_t2 / (1 << 30)
    dtk2 = c.par_t3 / (1 << 48)
    cf = raw_t - do1
    return cf * dtk1 + cf * cf * dtk2


def calc_pressure(raw_p: int, T: float, c: Calib) -> float:
    o    = c.par_p1 << 3
    tk10 = c.par_p2 / (1 << 6);  tk20 = c.par_p3 / (1 << 8)
    tk30 = c.par_p4 / (1 << 15)
    s    = (c.par_p5 - (1 << 14)) / (1 << 20)
    tk1s = (c.par_p6 - (1 << 14)) / (1 << 29)
    tk2s = c.par_p7 / (1 << 32); tk3s = c.par_p8 / (1 << 37)
    nls   = c.par_p9  / (1 << 48); tknls = c.par_p10 / (1 << 48)
    nls3  = c.par_p11 / ((1 << 35) * (1 << 30))
    tmp1 = o + tk10*T + tk20*T*T + tk30*T*T*T
    tmp2 = raw_p * (s + tk1s*T + tk2s*T*T + tk3s*T*T*T)
    tmp3 = raw_p * raw_p * (nls + tknls*T)
    tmp4 = raw_p * raw_p * raw_p * nls3
    return tmp1 + tmp2 + tmp3 + tmp4


def calc_humidity(raw_h: int, T: float, c: Calib) -> float:
    temp_comp = T * 5120 - 76800
    oh    = c.par_h1 * (1 << 6); sh = c.par_h5 / (1 << 16)
    tk10h = c.par_h2 / (1 << 14); tk1sh = c.par_h4 / (1 << 26)
    tk2sh = c.par_h3 / (1 << 26); hlin2 = c.par_h6 / (1 << 19)
    hoff  = raw_h - (oh + tk10h * temp_comp)
    hsens = hoff * sh * (1 + tk1sh*temp_comp
                          + tk1sh*tk2sh*temp_comp*temp_comp)
    return max(0.0, min(100.0, hsens * (1 - hlin2 * hsens)))


def calc_gas_resistance(raw_g: int, gas_range: int) -> float:
    var1 = 262144 >> gas_range
    var2 = (raw_g - 512) * 3 + 4096
    return 1e6 * var1 / var2 if var2 else 0.0


# ---------------------------------------------------------------------------
# Sensor lifecycle — init / arm-parallel / disarm
# ---------------------------------------------------------------------------


def init_sensor(board, cs: int, index: int) -> Optional[SensorState]:
    """Soft reset, identify, read calibration. Leaves the chip in SLEEP."""
    try:
        invalidate_page_cache(cs)
        spi_write(board, cs, REG_RESET, 0xB6)
        time.sleep(0.05)
        invalidate_page_cache(cs)

        chip_id = spi_read(board, cs, REG_CHIP_ID, 1)[0]
        if chip_id != EXPECTED_CHIP_ID:
            print(f"# s{index} cs=0x{cs:02X}: chip_id=0x{chip_id:02X} — skip",
                  file=sys.stderr)
            return None
        variant = spi_read(board, cs, REG_VARIANT_ID, 1)[0]
        sensor_id = read_sensor_id(board, cs)

        buf = (spi_read(board, cs, REG_COEFF1, LEN_COEFF1)
               + spi_read(board, cs, REG_COEFF2, LEN_COEFF2)
               + spi_read(board, cs, REG_COEFF3, LEN_COEFF3))
        calib = parse_calib(buf)

        s = SensorState(cs_pin=cs, index=index, calib=calib,
                        variant_id=variant, sensor_id=sensor_id)
        print(f"# s{index} cs=0x{cs:02X}: chip=0x{chip_id:02X} "
              f"variant=0x{variant:02X} id=0x{sensor_id:08X} init OK",
              file=sys.stderr)
        return s
    except Exception as exc:
        print(f"# s{index} cs=0x{cs:02X}: init failed — {exc}",
              file=sys.stderr)
        return None


def go_to_sleep(board, s: SensorState) -> None:
    """Read CTRL_MEAS, mask out mode bits, write back. Polls until SLEEP."""
    for _ in range(20):
        cur = spi_read(board, s.cs_pin, REG_CTRL_MEAS, 1)[0]
        if (cur & 0b11) == MODE_SLEEP:
            return
        spi_write(board, s.cs_pin, REG_CTRL_MEAS, cur & ~0b11)
        time.sleep(0.005)


def arm_parallel(board, s: SensorState, prof: "ProfileInfo",
                 amb_c: float = AMBIENT_INIT_C) -> None:
    """Program all heater slots, gas_wait_shared, then flip to PARALLEL.
    Mirrors `bme69x_set_heatr_conf(PARALLEL_MODE)` + `set_op_mode(PARALLEL)`."""
    go_to_sleep(board, s)

    # OSR — must be written while in SLEEP per bme69x_set_conf semantics.
    spi_write(board, s.cs_pin, REG_CTRL_HUM, OS_HUM & 0x07)
    spi_write(board, s.cs_pin, REG_CTRL_MEAS,
              (OS_TEMP << 5) | (OS_PRES << 2) | MODE_SLEEP)

    # Heater slots 0..N-1.
    for i in range(prof.n_steps):
        spi_write(board, s.cs_pin, REG_RES_HEAT_0 + i,
                  calc_res_heat(s.calib, prof.target_c[i], amb_c))
        # In parallel mode `gas_wait_x` encodes a multiplier of
        # `gas_wait_shared`, NOT an absolute duration. The bmeconfig's raw
        # multipliers (5/2/10/30/…) are what go through calc_gas_wait.
        spi_write(board, s.cs_pin, REG_GAS_WAIT_0 + i,
                  calc_gas_wait(prof.multipliers[i]))

    spi_write(board, s.cs_pin, REG_GAS_WAIT_SHARED,
              calc_gas_wait(prof.gas_wait_shared_ms))

    spi_write(board, s.cs_pin, REG_CTRL_GAS_0, 0x00)   # heater enabled
    spi_write(board, s.cs_pin, REG_CTRL_GAS_1,
              RUN_GAS_ON | ((prof.n_steps - 1) & NB_CONV_MSK))

    # Flip to PARALLEL — chip starts cycling the heater profile autonomously.
    cur = spi_read(board, s.cs_pin, REG_CTRL_MEAS, 1)[0]
    spi_write(board, s.cs_pin, REG_CTRL_MEAS, (cur & ~0b11) | MODE_PARALLEL)


def disarm(board, s: SensorState) -> None:
    try:
        spi_write(board, s.cs_pin, REG_CTRL_GAS_1, 0x00)
        spi_write(board, s.cs_pin, REG_CTRL_MEAS, MODE_SLEEP)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Field decode — pulls one row's worth of data out of a 17-byte FIELD block.
# ---------------------------------------------------------------------------


def decode_field(buf: List[int], offset: int, c: Calib) -> dict:
    f = buf[offset : offset + LEN_FIELD]
    status_byte = f[0]
    new_data   = status_byte & 0x80
    gas_index  = status_byte & 0x0F
    meas_index = f[1]
    raw_p = (f[2] << 16) | (f[3] << 8) | f[4]
    raw_t = (f[5] << 16) | (f[6] << 8) | f[7]
    raw_h = (f[8] << 8) | f[9]
    g_lsb = f[16]
    raw_g = (f[15] << 2) | (g_lsb >> 6)
    gas_range  = g_lsb & 0x0F
    gas_valid  = g_lsb & 0x20
    heat_stab  = g_lsb & 0x10
    # `data.status` per bme69x.c::read_field_data: ORs new_data with the two
    # gas-quality bits. Comparing this byte to BME69X_VALID_DATA (0xB0) gives
    # the strict "good sample" filter the parallel_mode example uses.
    status = new_data | gas_valid | heat_stab
    T = calc_temperature(raw_t, c)
    P = calc_pressure(raw_p, T, c)
    H = calc_humidity(raw_h, T, c)
    R = calc_gas_resistance(raw_g, gas_range)
    return {
        "status": status, "new_data": bool(new_data),
        "gas_index": gas_index, "meas_index": meas_index,
        "temperature": T, "pressure_hpa": P / 100.0, "humidity": H,
        "gas_ohm": R,
        "gas_valid": bool(gas_valid), "heat_stab": bool(heat_stab),
    }


def modular_newer(new: int, last: Optional[int]) -> bool:
    """Is `new` strictly newer than `last` in mod-256 arithmetic?
    `last is None` means we've never seen any sample yet."""
    if last is None:
        return True
    delta = (new - last) & 0xFF
    return 0 < delta < 128


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


_KEEP_RUNNING = True


def _stop(signum, frame):
    global _KEEP_RUNNING
    _KEEP_RUNNING = False


def poll_once(board, sensors: List[SensorState],
              t_poweron_ms: int, csv_writer, csv_file) -> int:
    """One pass over all sensors. Returns the number of rows emitted.

    Each sensor uses its own per-sensor profile (`s.profile`) for the
    target_c lookup and cycle-time computation."""
    rows_emitted = 0
    rtc = int(time.time())

    for s in sensors:
        if s.disabled:
            continue
        try:
            buf = spi_read(board, s.cs_pin, REG_FIELD0, LEN_ALL_FIELDS)
        except Exception as exc:
            s.consecutive_errors += 1
            if s.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                s.disabled = True
                print(f"# s{s.index} disabled after "
                      f"{MAX_CONSECUTIVE_ERRORS} consecutive failures: {exc}",
                      file=sys.stderr)
            continue
        if len(buf) != LEN_ALL_FIELDS:
            continue

        # Gather any of the 3 fields with new_data set.
        fresh = []
        for fi in range(N_FIELDS):
            off = fi * LEN_FIELD
            status = buf[off]
            if not (status & 0x80):
                continue
            meas_index = buf[off + 1]
            if not modular_newer(meas_index, s.last_meas_index):
                continue
            fresh.append((meas_index, off))

        # Sort by forward distance so we emit in measurement order.
        anchor = s.last_meas_index if s.last_meas_index is not None else 0
        fresh.sort(key=lambda x: (x[0] - anchor) & 0xFF)

        now_iso = datetime.now().isoformat(timespec="milliseconds")
        ts_now_ms = int(time.monotonic() * 1000) - t_poweron_ms

        # Per-sensor wall-time-derived scanning_cycle_index.
        prof = s.profile
        elapsed_s = ts_now_ms / 1000.0
        cycle_idx = int(elapsed_s / (prof.cycle_ms / 1000.0)) + 1 if prof else 1

        for meas_index, off in fresh:
            d = decode_field(buf, off, s.calib)
            s.last_meas_index = meas_index
            s.last_step = d["gas_index"]

            if d["status"] != BME69X_VALID_DATA:
                continue

            # Guard against weird gas_index values vs this sensor's profile.
            step = d["gas_index"]
            target_c = (prof.target_c[step]
                        if prof and 0 <= step < prof.n_steps else -1)

            csv_writer.writerow([
                now_iso, s.index, s.sensor_id, ts_now_ms, rtc,
                f"{d['temperature']:.4f}",
                f"{d['pressure_hpa']:.4f}",
                f"{d['humidity']:.4f}",
                f"{d['gas_ohm']:.2f}",
                step, target_c,
                1,
                cycle_idx,
                0, 0,
            ])
            rows_emitted += 1
            s.consecutive_errors = 0

    if rows_emitted:
        csv_file.flush()
    return rows_emitted


def stream(board, sensors: List[SensorState],
           scan_n: int, sleep_n: int, csv_writer, csv_file) -> None:
    n_live = sum(1 for s in sensors if not s.disabled)
    # Each sensor can have its own profile (BME AI-Studio grouped layout).
    by_prof: dict = {}
    for s in sensors:
        if s.disabled or s.profile is None:
            continue
        by_prof.setdefault(id(s.profile), (s.profile, [])) [1].append(s.index)
    print(f"# streaming {n_live} sensor(s); duty {scan_n}/{sleep_n} "
          f"({'continuous' if sleep_n == 0 else 'scan/sleep'})",
          file=sys.stderr)
    for _, (prof, idxs) in by_prof.items():
        print(f"#   sensors {idxs}: {prof.n_steps}-step profile, "
              f"cycle ≈ {prof.cycle_ms/1000:.2f} s, "
              f"shared_dur={prof.gas_wait_shared_ms} ms", file=sys.stderr)

    t_poweron_ms = int(time.monotonic() * 1000)

    # Poll period = ~meas_dur + shared_dur. The shared_dur differs per sensor
    # in a grouped layout; pick the smallest so we don't undersample the
    # fast group.
    min_shared = min(s.profile.gas_wait_shared_ms for s in sensors
                     if s.profile is not None)
    poll_period_s = (parallel_meas_dur_us() / 1e6) + (min_shared / 1000.0)

    # Reference cycle for duty-cycle timing. Use the LONGEST per-sensor
    # cycle so "scan_n cycles" means everyone gets at least scan_n profile
    # runs. For RDC-1-0 continuous, sleep_n=0 and we never sleep.
    max_cycle_ms = max(s.profile.cycle_ms for s in sensors
                       if s.profile is not None)

    while _KEEP_RUNNING:
        # ----- Arm parallel mode on every live sensor --------------------
        for s in sensors:
            if s.disabled or s.profile is None:
                continue
            try:
                arm_parallel(board, s, s.profile)
            except Exception as exc:
                print(f"# s{s.index} arm failed: {exc}", file=sys.stderr)
                s.consecutive_errors += 1
                if s.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    s.disabled = True

        ref = next((s for s in sensors if not s.disabled), None)
        if ref is None:
            print("# no live sensors; aborting", file=sys.stderr)
            break

        t_scan_start = time.monotonic()
        rows_in_scan = 0
        polls = 0
        last_progress = t_scan_start
        steps_seen: dict = {s.index: set() for s in sensors}

        if sleep_n == 0:
            print(f"# scan starting (continuous, Ctrl+C to stop)",
                  file=sys.stderr)
            t_scan_end = float("inf")
        else:
            scan_dur_s = scan_n * max_cycle_ms / 1000.0
            t_scan_end = t_scan_start + scan_dur_s
            print(f"# scan starting: {scan_n} cycle(s) × "
                  f"{max_cycle_ms/1000:.2f}s = ~{scan_dur_s:.1f}s",
                  file=sys.stderr)

        while _KEEP_RUNNING and time.monotonic() < t_scan_end:
            time.sleep(poll_period_s)
            polls += 1
            rows_in_scan += poll_once(board, sensors, t_poweron_ms,
                                      csv_writer, csv_file)
            for s in sensors:
                if s.last_step is not None:
                    steps_seen[s.index].add(s.last_step)
            now = time.monotonic()
            if now - last_progress >= 2.0:
                if sleep_n == 0:
                    pct_str = f"{int(now - t_scan_start)}s"
                else:
                    pct = min(100, int(100 * (now - t_scan_start)
                                       / (scan_n * max_cycle_ms / 1000.0)))
                    pct_str = f"{pct}%"
                # Per-sensor step coverage so the fast/slow groups are both
                # visible at a glance.
                coverage = "  ".join(
                    f"s{s.index}:{sorted(steps_seen[s.index])}"
                    for s in sensors if not s.disabled)
                print(f"  …{polls} polls, {rows_in_scan} valid rows  "
                      f"[{pct_str}]\n"
                      f"    {coverage}",
                      file=sys.stderr)
                last_progress = now

        if not _KEEP_RUNNING:
            break

        elapsed = time.monotonic() - t_scan_start
        live_now = [s.index for s in sensors if not s.disabled]
        print(f"{datetime.now().strftime('%H:%M:%S')}  scan complete: "
              f"{rows_in_scan} rows in {elapsed:.1f} s "
              f"({rows_in_scan/max(elapsed, 1e-9):.1f} r/s)  "
              f"sensors={live_now}")
        sys.stdout.flush()

        # Sleep phase only applies if sleep_n > 0.
        if sleep_n > 0 and _KEEP_RUNNING:
            for s in sensors:
                if not s.disabled:
                    disarm(board, s)
            sleep_dur = (max_cycle_ms / 1000.0) * sleep_n
            print(f"# sleeping {sleep_dur:.1f} s ({sleep_n} cycles)",
                  file=sys.stderr)
            t_end = time.monotonic() + sleep_dur
            while _KEEP_RUNNING and time.monotonic() < t_end:
                time.sleep(min(0.5, t_end - time.monotonic()))


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _profile_from_raw(raw: dict) -> ProfileInfo:
    targets = [int(s["temp_c"]) for s in raw["steps"]]
    mults   = [int(s["multiplier"]) for s in raw["steps"]]
    time_base = int(raw.get("time_base") or FALLBACK_TARGET_PER_STEP_MS)
    if time_base <= 0:
        time_base = FALLBACK_TARGET_PER_STEP_MS
    shared_ms = time_base
    cycle_ms = sum(mults) * shared_ms
    return ProfileInfo(target_c=targets, multipliers=mults,
                       gas_wait_shared_ms=shared_ms, cycle_ms=cycle_ms)


def load_profile(config_path: Path):
    """Read a .bmeconfig and return:
      - profiles_by_sensor: {sensor_index: ProfileInfo} for every sensor
        the config mentions (the BME AI-Studio "grouped" layout lets
        different sensors run different heater profiles)
      - duty_cycle: (scan_n, sleep_n) — taken from the first sensor's
        dutyCycleProfile (all sensors share one in practice)
      - cfg: the full parsed config (for printing)"""
    cfg = parse_bmeconfig(config_path, divisor=1.0, profile_index=0)
    by_id = {hp["id"]: _profile_from_raw(hp) for hp in cfg["heater_profiles"]}

    profiles_by_sensor: dict[int, ProfileInfo] = {}
    for sc in cfg["sensor_configs"]:
        prof = by_id.get(sc["heater_id"])
        if prof is None:
            print(f"# WARN: sensor {sc['sensor_index']} references unknown "
                  f"heater '{sc['heater_id']}', skipping", file=sys.stderr)
            continue
        profiles_by_sensor[sc["sensor_index"]] = prof

    # Fallback: if the config has no sensor_configs, assign the first heater
    # profile to all 8 sensors (older / simpler configs).
    if not profiles_by_sensor and cfg["heater_profiles"]:
        only = _profile_from_raw(cfg["heater_profiles"][0])
        profiles_by_sensor = {i: only for i in range(8)}

    return profiles_by_sensor, cfg["duty_cycle"], cfg


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def find_bmeconfig() -> Path:
    candidates = sorted(Path.cwd().glob("*.bmeconfig"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise SystemExit("no .bmeconfig found in the current directory")
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        print(f"# {len(candidates)} .bmeconfig files ({names}); "
              f"using newest: {candidates[0].name}", file=sys.stderr)
    return candidates[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="CSV file (default: data/bme690_receiver_<ts>.csv)")
    ap.add_argument("--config", type=Path, default=None,
                    help="BME AI-Studio .bmeconfig. Default: newest in "
                         "current directory.")
    args = ap.parse_args()

    config_path = args.config if args.config is not None else find_bmeconfig()
    if not config_path.exists():
        raise SystemExit(f"config file not found: {config_path}")

    if args.output is None:
        DATA_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = DATA_DIR / f"bme690_receiver_{ts}.csv"
    else:
        out_path = args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)

    profiles_by_sensor, (scan_n, sleep_n), cfg = load_profile(config_path)
    print(f"# loaded {len(cfg['heater_profiles'])} heater profile(s) "
          f"from {config_path}", file=sys.stderr)
    # Summarise unique profiles + which sensors use each.
    seen_profiles = {}
    for sidx, prof in profiles_by_sensor.items():
        key = id(prof)
        if key not in seen_profiles:
            # Find the matching heater profile in the raw config for its id.
            label = next((hp["id"] for hp in cfg["heater_profiles"]
                          if all(int(s["temp_c"]) == prof.target_c[i] and
                                 int(s["multiplier"]) == prof.multipliers[i]
                                 for i, s in enumerate(hp["steps"]))),
                         "?")
            seen_profiles[key] = (label, prof, [])
        seen_profiles[key][2].append(sidx)
    for label, prof, idxs in seen_profiles.values():
        print(f"#   profile '{label}' on sensors {sorted(idxs)}: "
              f"{prof.n_steps} steps  shared_dur={prof.gas_wait_shared_ms} ms  "
              f"cycle ≈ {prof.cycle_ms/1000:.2f} s", file=sys.stderr)
    print(f"# duty cycle: {scan_n} scan / {sleep_n} sleep "
          f"(board mode: {cfg['board_mode']})", file=sys.stderr)

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    board = cpy.CoinesBoard()
    board.open_comm_interface(cpy.CommInterface.USB)
    try:
        info = board.get_board_info()
        print(f"# board hw=0x{info.HardwareId:X} sw=0x{info.SoftwareId:X} "
              f"shuttle=0x{info.ShuttleID:X}", file=sys.stderr)

        # Power-cycle for a known start state.
        board.set_shuttleboard_vdd_vddio_config(0, 0)
        time.sleep(0.1)
        board.set_shuttleboard_vdd_vddio_config(3300, 3300)
        time.sleep(0.2)
        board.config_spi_bus(cpy.SPIBus.BUS_SPI_0,
                             cpy.MultiIOPin(SENSOR_CS_PINS[0]),
                             cpy.SPISpeed.SPI_1_MHZ,
                             cpy.SPIMode.MODE0)

        sensors: List[SensorState] = []
        for i, cs in enumerate(SENSOR_CS_PINS):
            s = init_sensor(board, cs, i)
            if s is None:
                continue
            s.profile = profiles_by_sensor.get(i)
            if s.profile is None:
                print(f"# s{i} cs=0x{cs:02X}: no heater profile assigned in "
                      "bmeconfig — skipping", file=sys.stderr)
                continue
            sensors.append(s)
        if not sensors:
            print("# no sensors initialised — aborting", file=sys.stderr)
            return 1

        with out_path.open("w", newline="", encoding="utf-8") as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(CSV_HEADER)
            csv_file.flush()
            print(f"# logging to {out_path}", file=sys.stderr)
            try:
                stream(board, sensors, scan_n, sleep_n,
                       csv_writer, csv_file)
            finally:
                for s in sensors:
                    disarm(board, s)
        print(f"# wrote {out_path}", file=sys.stderr)
        return 0
    finally:
        try:
            board.set_shuttleboard_vdd_vddio_config(0, 0)
        except Exception:
            pass
        board.close_comm_interface()


if __name__ == "__main__":
    sys.exit(main())
