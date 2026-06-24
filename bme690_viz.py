"""Live visualiser for the BME690 receiver stream.

The contract between this script and `bme690_receiver.py` is the **CSV file**
— nothing else. Run them as two independent processes; either can be
started, stopped, or restarted without touching the other:

    # terminal 1
    python bme690_receiver.py          # forced mode (wide CSV)
    # or
    python bme690_receiver.py --mode parallel    # parallel mode (long CSV)

    # terminal 2
    python bme690_viz.py

The visualiser auto-detects which schema the CSV uses:

* **Wide** (forced mode): one row per cycle, columns
  `s{0..7}_temp_c, s{0..7}_gas_ohm, ...`. One gas line per sensor.
* **Long** (parallel mode): one row per (sensor, step), columns
  `sensor, step, target_c, gas_ohm, ...`. **Ten coloured gas lines per
  sensor**, matching BME AI-Studio's per-step view.

It re-checks for newer `bme690_receiver_*.csv` files every poll, so if the
receiver restarts mid-session the viz hops to the new file automatically.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation


# ---------------------------------------------------------------------------
# CSV tail — byte-position file tail, no long-lived csv.reader.
# ---------------------------------------------------------------------------


class CsvTail:
    # Look in ./data/ first (where the receiver writes by default) and fall
    # back to the current directory for old-style captures.
    DEFAULT_PATTERNS = ("data/bme690_receiver_*.csv",
                       "bme690_receiver_*.csv")

    def __init__(self, patterns=DEFAULT_PATTERNS,
                 explicit: Optional[Path] = None):
        self.patterns = patterns
        self.explicit = explicit
        self.path: Optional[Path] = None
        self.header: Optional[List[str]] = None
        self._byte_pos: int = 0

    def _pick_newest(self) -> Optional[Path]:
        if self.explicit is not None:
            return self.explicit if self.explicit.exists() else None
        candidates = []
        for p in self.patterns:
            candidates.extend(Path.cwd().glob(p))
        return max(candidates, key=lambda p: p.stat().st_mtime, default=None)

    def poll(self) -> Tuple[bool, List[dict]]:
        """Return (switched_files, new_rows). `switched_files=True` only on
        the first poll for a given path, so callers can reset buffers."""
        newest = self._pick_newest()
        if newest is None:
            return False, []
        switched = newest != self.path
        if switched:
            self.path = newest
            self._byte_pos = 0
            self.header = None

        try:
            with open(self.path, "rb") as fp:
                fp.seek(self._byte_pos)
                data = fp.read()
        except OSError:
            return switched, []
        if not data:
            return switched, []

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return switched, []

        if text.endswith("\n"):
            complete = text
            consumed = len(data)
        else:
            last_nl = text.rfind("\n")
            if last_nl < 0:
                return switched, []
            complete = text[:last_nl + 1]
            consumed = len(complete.encode("utf-8"))
        self._byte_pos += consumed

        rows: List[dict] = []
        for row in csv.reader(complete.splitlines()):
            if not row:
                continue
            if self.header is None:
                self.header = row
                continue
            if len(row) != len(self.header):
                continue
            rows.append(dict(zip(self.header, row)))
        return switched, rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


N_SENSORS = 8
GRID_ROWS, GRID_COLS = 2, 4
MAX_STEPS = 10

# BME AI-Studio "Step 1..10" palette — by-eye match from the app.
STEP_PALETTE = [
    "#1f3b73", "#3e1259", "#7a1b6b", "#b5197f", "#d878a7",
    "#1a4f8a", "#3f8ec7", "#79c8df", "#5fb39e", "#1f6b4f",
]


def _f(cell: str) -> Optional[float]:
    if cell is None or cell == "":
        return None
    try:
        return float(cell)
    except ValueError:
        return None


def _status_int(cell: str) -> int:
    try:
        return int(cell, 0) if cell else 0
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Buffers — one schema-agnostic interface; two implementations.
# ---------------------------------------------------------------------------


class WideBuffers:
    """Forced-mode CSV: 1 line per sensor."""

    LABEL = "forced (wide)"

    def __init__(self, capacity: int = 4096):
        self.t = [deque(maxlen=capacity) for _ in range(N_SENSORS)]
        self.gas = [deque(maxlen=capacity) for _ in range(N_SENSORS)]
        self.heat_stab = [False] * N_SENSORS
        self.has_data = [False] * N_SENSORS
        self.last_temp: List[Optional[float]] = [None] * N_SENSORS
        self.last_hum:  List[Optional[float]] = [None] * N_SENSORS
        self.last_pres: List[Optional[float]] = [None] * N_SENSORS

    def push_row(self, row: dict, t_sec: float) -> None:
        for i in range(N_SENSORS):
            gas = _f(row.get(f"s{i}_gas_ohm", ""))
            if gas is None or gas <= 0:
                continue
            self.t[i].append(t_sec)
            self.gas[i].append(gas / 1000.0)   # kΩ
            status = _status_int(row.get(f"s{i}_status", ""))
            self.heat_stab[i] = bool(status & 0x10)
            self.has_data[i] = True
            self.last_temp[i] = _f(row.get(f"s{i}_temp_c", ""))
            self.last_hum[i]  = _f(row.get(f"s{i}_humidity_pct", ""))
            self.last_pres[i] = _f(row.get(f"s{i}_pressure_hpa", ""))

    def clear(self) -> None:
        for d in self.t:  d.clear()
        for d in self.gas: d.clear()
        self.heat_stab = [False] * N_SENSORS
        self.has_data = [False] * N_SENSORS
        self.last_temp = [None] * N_SENSORS
        self.last_hum  = [None] * N_SENSORS
        self.last_pres = [None] * N_SENSORS


class LongBuffers:
    """Parallel-mode CSV: 10 lines per sensor (one per heater step)."""

    LABEL = "parallel (long)"

    def __init__(self, capacity: int = 4096):
        # buffers[sensor][step] -> (deque_t, deque_gas_kohm)
        self.buf: List[List[Tuple[deque, deque]]] = [
            [(deque(maxlen=capacity), deque(maxlen=capacity))
             for _ in range(MAX_STEPS)]
            for _ in range(N_SENSORS)
        ]
        self.heat_stab = [False] * N_SENSORS
        self.has_data = [False] * N_SENSORS
        self.last_temp: List[Optional[float]] = [None] * N_SENSORS
        self.last_hum:  List[Optional[float]] = [None] * N_SENSORS
        self.last_pres: List[Optional[float]] = [None] * N_SENSORS

    def push_row(self, row: dict, t_sec: float) -> None:
        # New schema (bmerawdata-compatible) takes precedence; the older
        # short names (`sensor` / `step` / `gas_ohm`) are kept as a fallback
        # so this viz still works against older CSV captures.
        sensor = _f(row.get("sensor_index", row.get("sensor", "")))
        step = _f(row.get("heater_profile_step_index", row.get("step", "")))
        if sensor is None or step is None:
            return
        i, j = int(sensor), int(step)
        if not (0 <= i < N_SENSORS and 0 <= j < MAX_STEPS):
            return
        gas = _f(row.get("resistance_gassensor", row.get("gas_ohm", "")))
        if gas is None or gas <= 0 or gas >= 6_300_000:
            return  # skip "no-measurement" sentinel values
        # Honour the new "scanning_enabled" column when present so sleep-
        # phase rows (if the receiver ever emits them) don't pollute the plot.
        if "scanning_enabled" in row:
            try:
                if int(row["scanning_enabled"]) == 0:
                    return
            except ValueError:
                pass
        t_d, g_d = self.buf[i][j]
        t_d.append(t_sec)
        g_d.append(gas / 1000.0)
        self.heat_stab[i] = bool(_status_int(row.get("heat_stab", "")))
        self.has_data[i] = True
        self.last_temp[i] = _f(row.get("temperature", row.get("temp_c", "")))
        self.last_hum[i]  = _f(row.get("relative_humidity",
                                       row.get("humidity_pct", "")))
        self.last_pres[i] = _f(row.get("pressure", row.get("pressure_hpa", "")))

    def clear(self) -> None:
        for s in self.buf:
            for (t_d, g_d) in s:
                t_d.clear()
                g_d.clear()
        self.heat_stab = [False] * N_SENSORS
        self.has_data = [False] * N_SENSORS
        self.last_temp = [None] * N_SENSORS
        self.last_hum  = [None] * N_SENSORS
        self.last_pres = [None] * N_SENSORS


def detect_format(header: List[str]):
    # New long format (bmerawdata-compatible).
    if ("sensor_index" in header and
            "heater_profile_step_index" in header and
            "resistance_gassensor" in header):
        return LongBuffers
    # Older long format used by earlier receiver iterations.
    if "sensor" in header and "step" in header and "gas_ohm" in header:
        return LongBuffers
    # Wide / forced format.
    if "s0_gas_ohm" in header:
        return WideBuffers
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, default=None,
                    help="Specific CSV to tail. Default: newest "
                         "data/bme690_receiver_*.csv (or in current dir).")
    ap.add_argument("--window", type=float, default=60.0,
                    help="Seconds of history per sensor (default 60).")
    ap.add_argument("--refresh-ms", type=int, default=500,
                    help="Refresh interval in ms (default 500).")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print tail diagnostics to stderr.")
    args = ap.parse_args()

    tail = CsvTail(explicit=args.file)
    buffers = None
    fmt_label = "—"
    t0: Optional[float] = None

    print(f"# viz starting; CWD={Path.cwd()}", file=sys.stderr)

    fig, axes = plt.subplots(GRID_ROWS, GRID_COLS, figsize=(13, 6),
                             sharex=True, sharey=True)
    fig.canvas.manager.set_window_title("BME690 8x — live gas resistance")
    fig.suptitle("waiting for data...", fontsize=11)
    axes_flat = axes.flatten()
    # We allocate enough lines for the long format (10 per panel). In wide
    # format we just use index 0.
    lines: List[List] = []
    heat_dots = []
    for i, ax in enumerate(axes_flat):
        ax.set_title(f"s{i}", fontsize=9, loc="left")
        ax.set_yscale("log")
        ax.grid(True, which="both", linestyle=":", alpha=0.4)
        ax.tick_params(labelsize=7)
        per_panel = []
        for j in range(MAX_STEPS):
            (ln,) = ax.plot([], [], lw=1.0, color=STEP_PALETTE[j],
                            label=f"step {j}")
            per_panel.append(ln)
        lines.append(per_panel)
        dot = ax.scatter([], [], s=22, marker="o", color="grey",
                         zorder=5, transform=ax.transAxes)
        dot.set_offsets([[0.94, 0.92]])
        heat_dots.append(dot)
    for ax in axes_flat[-GRID_COLS:]:
        ax.set_xlabel("t (s)", fontsize=8)
    for ax in axes_flat[::GRID_COLS]:
        ax.set_ylabel("gas kΩ (log)", fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, 0.94))

    def update(_frame):
        nonlocal buffers, fmt_label, t0
        switched, rows = tail.poll()

        if switched:
            if buffers is not None:
                buffers.clear()
            t0 = None
            fmt_label = "—"
            if args.verbose:
                print(f"# switched to {tail.path}", file=sys.stderr)

        # Late binding of buffer type — we need the header to know format.
        if buffers is None and tail.header is not None:
            cls = detect_format(tail.header)
            if cls is None:
                if args.verbose:
                    print(f"# unknown CSV format: header={tail.header[:6]}",
                          file=sys.stderr)
            else:
                buffers = cls()
                fmt_label = cls.LABEL
                if args.verbose:
                    print(f"# format detected: {fmt_label}", file=sys.stderr)

        if buffers is None:
            return _all_artists(lines, heat_dots)

        now_wall = time.time()
        for row in rows:
            if t0 is None:
                t0 = now_wall
            buffers.push_row(row, now_wall - t0)

        if args.verbose and rows:
            print(f"# +{len(rows)} rows (bytes={tail._byte_pos})",
                  file=sys.stderr)

        if t0 is None:
            return _all_artists(lines, heat_dots)
        now_rel = now_wall - t0
        t_lo = max(0.0, now_rel - args.window)
        t_hi = max(args.window, now_rel)

        gas_min, gas_max = float("inf"), 0.0
        temps, hums, press = [], [], []

        if isinstance(buffers, WideBuffers):
            for i in range(N_SENSORS):
                # Only use the first line in each panel.
                ln = lines[i][0]
                if not buffers.has_data[i]:
                    ln.set_data([], [])
                    continue
                xs = [t for t in buffers.t[i] if t >= t_lo]
                ys = [g for t, g in zip(buffers.t[i], buffers.gas[i])
                      if t >= t_lo]
                ln.set_color(STEP_PALETTE[0])
                ln.set_data(xs, ys)
                # Hide the other 9 lines.
                for j in range(1, MAX_STEPS):
                    lines[i][j].set_data([], [])
                if ys:
                    gas_min = min(gas_min, min(y for y in ys if y > 0))
                    gas_max = max(gas_max, max(ys))
                heat_dots[i].set_color(
                    "#2bbf3a" if buffers.heat_stab[i] else "#c43d3d")
                if buffers.last_temp[i] is not None: temps.append(buffers.last_temp[i])
                if buffers.last_hum[i]  is not None: hums.append(buffers.last_hum[i])
                if buffers.last_pres[i] is not None: press.append(buffers.last_pres[i])
        else:
            assert isinstance(buffers, LongBuffers)
            for i in range(N_SENSORS):
                if not buffers.has_data[i]:
                    for j in range(MAX_STEPS):
                        lines[i][j].set_data([], [])
                    continue
                for j in range(MAX_STEPS):
                    t_d, g_d = buffers.buf[i][j]
                    xs = [t for t in t_d if t >= t_lo]
                    ys = [g for t, g in zip(t_d, g_d) if t >= t_lo]
                    lines[i][j].set_data(xs, ys)
                    if ys:
                        gas_min = min(gas_min, min(y for y in ys if y > 0))
                        gas_max = max(gas_max, max(ys))
                heat_dots[i].set_color(
                    "#2bbf3a" if buffers.heat_stab[i] else "#c43d3d")
                if buffers.last_temp[i] is not None: temps.append(buffers.last_temp[i])
                if buffers.last_hum[i]  is not None: hums.append(buffers.last_hum[i])
                if buffers.last_pres[i] is not None: press.append(buffers.last_pres[i])

        for ax in axes_flat:
            ax.set_xlim(t_lo, t_hi)
        if gas_max > 0:
            axes_flat[0].set_ylim(gas_min * 0.6, gas_max * 1.6)

        live = sum(1 for b in buffers.has_data if b)
        if temps:
            fig.suptitle(
                f"{tail.path.name if tail.path else 'no file'}  |  "
                f"{fmt_label}  |  {live}/{N_SENSORS} live  |  "
                f"T={sum(temps)/len(temps):.1f}°C   "
                f"P={sum(press)/len(press):.1f} hPa   "
                f"H={sum(hums)/len(hums):.1f}%RH",
                fontsize=10,
            )
        return _all_artists(lines, heat_dots)

    anim = FuncAnimation(fig, update, interval=args.refresh_ms,
                         blit=False, cache_frame_data=False)
    plt.show()
    _ = anim
    return 0


def _all_artists(lines, heat_dots):
    out = []
    for panel in lines:
        out.extend(panel)
    out.extend(heat_dots)
    return out


if __name__ == "__main__":
    sys.exit(main())
