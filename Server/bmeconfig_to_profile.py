"""Convert a BME AI-Studio .bmeconfig into a Python heater profile.

Reads the JSON, pulls the first heater profile, and emits a Python module
defining HEATER_PROFILE = [(temp_C, dur_ms), ...] plus encoded
res_heat_x / gas_wait_x register values, ready to be imported by
bme690_live.py once it's switched to parallel mode.

BME AI-Studio convention used here:
    step_duration_ms = timeBase * multiplier
i.e. the .bmeconfig encodes durations in milliseconds. The default 10-step
sample profile sums to ~10.78 s total, matching what BME AI-Studio shows
visually. Override with `--divisor` if a particular config encodes
differently.
"""

import argparse
import json
import sys
from pathlib import Path


def encode_gas_wait(dur_ms: float) -> tuple[int, float]:
    """Pack a duration into the gas_wait byte (factor[7:6] | multiplier[5:0]).

    Returns (encoded_byte, realised_ms). gas_wait units are 1/4/16/64 ms.
    """
    dur = int(round(dur_ms))
    if dur >= 0xFC0:
        return 0xFF, 0xFC0  # max ≈ 4032 ms
    factor = 0
    while dur > 0x3F:
        dur //= 4
        factor += 1
    encoded = (dur & 0x3F) | (factor << 6)
    realised = dur * (4 ** factor)
    return encoded, float(realised)


def _build_steps(profile: dict, divisor: float) -> list:
    time_base = profile["timeBase"]
    steps = []
    for i, (temp_c, mult) in enumerate(profile["temperatureTimeVectors"]):
        dur_ms = (time_base * mult) / divisor
        encoded, realised = encode_gas_wait(dur_ms)
        if int(round(dur_ms)) >= 0xFC0:
            print(f"# warning: profile {profile.get('id')!r} step {i} requested "
                  f"{dur_ms:.1f} ms but gas_wait saturates at {realised:.1f} ms",
                  file=sys.stderr)
        steps.append({
            "temp_c": int(temp_c),
            "multiplier": int(mult),
            "dur_ms_requested": float(dur_ms),
            "dur_ms_realised": realised,
            "gas_wait_byte": encoded,
        })
    return steps


def convert(path: Path, divisor: float = 1.0, profile_index: int = 0) -> dict:
    """Parse a .bmeconfig into a complete description of:
      - all heater profiles
      - all duty cycle profiles
      - the per-sensor mapping (which heater + which duty each sensor uses)

    `profile_index` is kept for the standalone CLI `--print` mode (it picks
    one profile to summarise). The receiver uses `heater_profiles` and
    `sensor_configs` directly to support BME AI-Studio's "grouped" layout
    where different sensors run different heater profiles."""
    cfg = json.loads(path.read_text(encoding='utf-8'))
    hdr = cfg.get("configHeader", {})
    body = cfg.get("configBody", {})

    raw_profiles = body.get("heaterProfiles", [])
    if not raw_profiles:
        raise SystemExit("no heaterProfiles in config")

    heater_profiles = []
    for p in raw_profiles:
        steps = _build_steps(p, divisor)
        heater_profiles.append({
            "id": p["id"],
            "time_base": int(p["timeBase"]),
            "steps": steps,
            "step_count": len(steps),
            "total_cycle_ms": sum(s["dur_ms_realised"] for s in steps),
            "sum_multipliers": sum(s["multiplier"] for s in steps),
        })

    duty_profiles = []
    for d in body.get("dutyCycleProfiles", []):
        duty_profiles.append({
            "id": d["id"],
            "scan":  int(d.get("numberScanningCycles", 1)),
            "sleep": int(d.get("numberSleepingCycles", 0)),
        })

    sensor_configs = []
    for s in body.get("sensorConfigurations", []):
        sensor_configs.append({
            "sensor_index": int(s["sensorIndex"]),
            "heater_id":    s.get("heaterProfile"),
            "duty_id":      s.get("dutyCycleProfile"),
        })

    # --- Backward-compatible "single profile" fields for the CLI -----------
    # Pick the requested profile (or the first one) and expose it the way the
    # old convert() did.
    if profile_index >= len(heater_profiles):
        raise SystemExit(f"profile index {profile_index} out of range "
                         f"(file has {len(heater_profiles)})")
    chosen = heater_profiles[profile_index]

    duty_cycle = (1, 0)
    if sensor_configs and duty_profiles:
        # Use the sensor(s) actually running the chosen heater profile, not
        # just sensor_configs[0] — under BME AI-Studio's "grouped" layout,
        # different sensors can pair different heater profiles with different
        # duty cycles, so sensor 0's duty cycle isn't necessarily the one that
        # goes with `chosen`.
        matching = next((s for s in sensor_configs if s["heater_id"] == chosen["id"]),
                         sensor_configs[0])
        want = matching["duty_id"]
        dp = next((d for d in duty_profiles if d["id"] == want), duty_profiles[0])
        duty_cycle = (dp["scan"], dp["sleep"])

    return {
        "source_file": str(path),
        "board_type":  hdr.get("boardType"),
        "board_mode":  hdr.get("boardMode"),
        "divisor":     divisor,
        # New structured view (used by the receiver) ------------------------
        "heater_profiles":     heater_profiles,
        "duty_cycle_profiles": duty_profiles,
        "sensor_configs":      sensor_configs,
        # Backward-compat fields for the CLI inspector ----------------------
        "profile_id":      chosen["id"],
        "time_base":       chosen["time_base"],
        "step_count":      chosen["step_count"],
        "total_cycle_ms":  chosen["total_cycle_ms"],
        "sum_multipliers": chosen["sum_multipliers"],
        "steps":           chosen["steps"],
        "duty_cycle":      duty_cycle,
    }


def emit_python_module(result: dict, out: Path) -> None:
    scan_n, sleep_n = result["duty_cycle"]
    lines = [
        '"""Auto-generated from BME AI-Studio bmeconfig — do not edit by hand."""',
        f'# source: {result["source_file"]}',
        f'# profile id: {result["profile_id"]}',
        f'# board: {result["board_type"]} mode: {result["board_mode"]}',
        f'# steps: {result["step_count"]}  total cycle: {result["total_cycle_ms"]:.1f} ms',
        f'# duty cycle: {scan_n} scan / {sleep_n} sleep',
        '',
        '# (target_temp_C, duration_ms_realised, gas_wait_byte)',
        'HEATER_PROFILE = [',
    ]
    for s in result["steps"]:
        lines.append(
            f'    ({s["temp_c"]}, {s["dur_ms_realised"]:.1f}, '
            f'0x{s["gas_wait_byte"]:02X}),'
        )
    lines.append(']')
    lines.append('')
    lines.append('# (number_scanning_cycles, number_sleeping_cycles)')
    lines.append(f'DUTY_CYCLE = ({scan_n}, {sleep_n})')
    lines.append('')
    out.write_text('\n'.join(lines), encoding='utf-8')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path,
                    help="Python module to write (default: derived from input)")
    ap.add_argument("--divisor", type=float, default=1.0,
                    help="timeBase*multiplier / divisor = step_ms (default 1)")
    ap.add_argument("--profile-index", type=int, default=0,
                    help="which heaterProfile to use (default 0)")
    ap.add_argument("--print", dest="print_report", action="store_true",
                    help="also print a human-readable breakdown")
    args = ap.parse_args()

    if not args.input.exists():
        print(f"ERROR: {args.input} not found", file=sys.stderr)
        return 2

    result = convert(args.input, args.divisor, args.profile_index)

    out = args.output or args.input.with_name(
        args.input.stem.replace('.', '_') + "_profile.py"
    )
    emit_python_module(result, out)
    print(f"wrote {out}")

    if args.print_report:
        # On-chip parallel-mode cycle: step time = multiplier × timeBase.
        # (In forced mode the chip would interpret gas_wait_x as an absolute
        # duration, which saturates at 4032 ms; that's what `real(ms)` shows.)
        parallel_step_ms = [s["multiplier"] * result["time_base"]
                            for s in result["steps"]]
        parallel_cycle_ms = sum(parallel_step_ms)
        print(f"\nprofile id : {result['profile_id']}")
        print(f"board      : {result['board_type']}  mode: {result['board_mode']}")
        print(f"timeBase   : {result['time_base']}  divisor: {result['divisor']}")
        print(f"parallel-mode cycle: {parallel_cycle_ms/1000:.2f} s "
              f"(this is what the receiver uses)")
        print(f"\n  #   T(°C)    mul   step (parallel)   gas_wait   forced(ms)")
        for i, s in enumerate(result["steps"]):
            print(f"  {i:<3} {s['temp_c']:>5}   {s['multiplier']:>4}   "
                  f"{parallel_step_ms[i]/1000:>10.2f}s        "
                  f"0x{s['gas_wait_byte']:02X}       {s['dur_ms_realised']:>6.1f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
