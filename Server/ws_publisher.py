"""Approach-A WebSocket server + publisher for the XR smell pipeline.

Runs *on the host, with the detector* (plan.md §4 Approach A). It:

  1. runs a WebSocket **server** the HoloLens dials into (``ws://<lan-ip>:8765``),
  2. on a fixed-rate clock, asks the ML for an inference, and
  3. broadcasts the plan.md §3 JSON packet to every connected client.

Inference now comes from ``real_ml.RealEstimator`` (the classifier +
regressor trained in ML/train.py, copied into Server/models/) by default —
pass ``--dummy`` to fall back to the old simulated ``dummy_ml.DummyEstimator``
for pure wiring/transport tests that don't need real models present.

**By default** (no flags needed) it spawns ``bme690_receiver.py`` itself as a
subprocess (hardware must be connected) and live-tails its CSV output into
the estimator — the CSV keeps being written exactly as before, so
``bme690_viz.py`` still works unmodified against the same file. Override with
exactly one of:

  * ``--live-tail``  same live-tailing, but doesn't spawn a receiver — use
    when one is already running separately (e.g. in another terminal); two
    processes both trying to talk to the same SPI hardware would conflict.
  * ``--replay-csv <path>``  replays a previously captured CSV's cycles at
    real pace instead of live/spawned hardware — a stand-in for demos/testing
    when nothing's connected. By design ``Server/`` doesn't hold training
    CSVs itself (those live in ``ML/`` — see its own ``.gitignore``); point
    this at a path there, or anywhere else you have a capture.
  * ``--no-auto-receiver``  don't spawn or tail anything — ``RealEstimator``'s
    buffers stay empty and it reports a neutral/idle reading
    (``odour_confidence=0, intensity=0``) forever (you'll get a one-time
    startup warning explaining why). For transport-only testing without
    hardware or a capture file.

Packet (plan.md §3)::

    {"timestamp": 1719772800.123, "odour": "lemon",
     "odour_confidence": 0.94, "intensity": 0.62, "seq": 1043}

Terminal output: normal operation shows only the **packet stream** (the ML
result — a heartbeat line once/sec while producing real readings, once/10s
while idle, and immediately on an odour change) plus anything **abnormal**
(warnings, errors, a sensor going quiet, a client dis/connecting, the spawned
receiver dying or printing a problem). Routine status chatter — startup steps,
"sensor ready", live-tail/replay progress, and the receiver subprocess's own
boot dump — is suppressed. Pass **--verbose** to show all of it (plus a line
on every packet and every buffered sensor cycle).

Usage::

    python ws_publisher.py                                    # spawn the receiver + tail it (hardware required, default)
    python ws_publisher.py --live-tail                        # tail a receiver already running elsewhere
    python ws_publisher.py --replay-csv ../ML/data/some_capture.csv --replay-speed 4
    python ws_publisher.py --replay-csv ../ML/data/some_capture.csv --verbose
    python ws_publisher.py --dummy                           # old simulated waveform, for transport-only testing
    python ws_publisher.py --once                             # print one packet and exit (no server)

Find the LAN IP to put in the Unity config with ``ipconfig`` (Windows) /
``ip addr`` (Linux); the HoloLens connects to ``ws://<that-ip>:8765``.

Requires the ``websockets`` package and ``real_ml.py``'s dependencies
(joblib, numpy, pandas, scikit-learn — already in requirements.txt).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dummy_ml import DummyEstimator, Estimator

try:
    import websockets
except ImportError:  # pragma: no cover - guidance only
    sys.exit(
        "ERROR: the 'websockets' package is not installed.\n"
        "Run: pip install -r requirements.txt"
    )


# --- Logging policy ---------------------------------------------------------
# Normal operation shows only the packet stream (the ML result) plus anything
# abnormal (warnings, errors, a sensor going quiet, a client dis/connecting,
# the receiver dying). Routine status chatter (startup steps, "sensor ready",
# live-tail progress) is suppressed unless --verbose. `print(..., stderr)` is
# used directly for always-on lines; `log_routine()` gates the rest.
_VERBOSE = False


def log_routine(msg: str) -> None:
    """Routine status — shown only under --verbose."""
    if _VERBOSE:
        print(msg, file=sys.stderr)


# Lines from the spawned bme690_receiver.py subprocess matching this are
# treated as abnormal and always forwarded; everything else (its routine
# startup dump: heater profiles, board mode, "streaming N sensors", ...) is
# hidden unless --verbose. Keeps the terminal to packets + real problems.
_RECEIVER_ABNORMAL_RE = re.compile(
    r"error|fail|abort|warn|skip|disabled|no sensors|exception|traceback|"
    r"cannot|not found|invalid|0x00",
    re.IGNORECASE,
)


def _forward_receiver_output(proc: subprocess.Popen) -> None:
    """Runs in a daemon thread: read the receiver subprocess's stderr and
    forward only abnormal lines (or all lines under --verbose), prefixed so
    it's clear which process they came from."""
    if proc.stderr is None:
        return
    for raw in proc.stderr:
        line = raw.rstrip("\n")
        if line and (_VERBOSE or _RECEIVER_ABNORMAL_RE.search(line)):
            print(f"[receiver] {line}", file=sys.stderr)


def make_estimator(use_dummy: bool = False, verbose: bool = False) -> Estimator:
    """Return the inference source. Today: RealEstimator (the trained
    models) unless --dummy asks for the old simulated waveform."""
    if use_dummy:
        return DummyEstimator()
    from real_ml import RealEstimator
    return RealEstimator(verbose=verbose)


def build_packet(est: Estimator, seq: int, t: float) -> dict:
    """Assemble one plan.md §3 packet from an inference."""
    inf = est.infer(t=t)
    return {
        "timestamp": round(time.time(), 3),
        "odour": inf.odour,
        "odour_confidence": inf.odour_confidence,
        "intensity": inf.intensity,
        "seq": seq,
    }


STATUS_LOG_INTERVAL_S = 1.0        # heartbeat while producing real (non-idle) readings
IDLE_STATUS_LOG_INTERVAL_S = 1.0  # slower heartbeat while idle (odour_confidence==0) -- nothing's
                                    # changing, so once/sec for a potentially unattended stretch is just noise


class Publisher:
    """Holds the set of connected clients and broadcasts packets to them."""

    def __init__(self, rate_hz: float, estimator: Estimator, verbose: bool = False) -> None:
        self.period = 1.0 / rate_hz
        self.clients: set = set()
        self.estimator = estimator
        self.seq = 0
        self.verbose = verbose
        self._last_status_log = 0.0
        self._last_logged_odour: str | None = None

    async def handler(self, ws) -> None:
        """Register a client for its lifetime; we never expect inbound data.

        A client leaving (clean close OR an abnormal drop — network blip, app
        killed, HoloLens sleeping) only removes it from `self.clients`; it never
        stops the server or the broadcast loop. Inference keeps running and the
        live prediction keeps being logged with `clients=0` until a client (the
        same or another) reconnects. The broad `except` guarantees an abnormal
        drop can't propagate out of this per-connection coroutine and disturb
        anything else."""
        peer = getattr(ws, "remote_address", "?")
        self.clients.add(ws)
        print(f"# client connected: {peer}  ({len(self.clients)} total)",
              file=sys.stderr)
        try:
            # The HoloLens is a pure subscriber; just wait until it disconnects.
            await ws.wait_closed()
        except Exception:
            pass  # abnormal close — the finally still cleans up; server unaffected
        finally:
            self.clients.discard(ws)
            print(f"# client disconnected: {peer}  ({len(self.clients)} total)",
                  file=sys.stderr)

    def _maybe_log_status(self, packet: dict, now: float) -> None:
        """Status line at most once/sec while producing real readings (once
        per 10s while idle — see IDLE_STATUS_LOG_INTERVAL_S), plus
        immediately whenever the classified odour changes — that's the
        "interesting" event, not every tick. --verbose logs every packet."""
        odour_changed = packet["odour"] != self._last_logged_odour
        interval = IDLE_STATUS_LOG_INTERVAL_S if packet["odour_confidence"] == 0 else STATUS_LOG_INTERVAL_S
        due = (now - self._last_status_log) >= interval
        if self.verbose or odour_changed or due:
            print(f"# packet seq={packet['seq']}: odour={packet['odour']} "
                  f"confidence={packet['odour_confidence']:.2f} "
                  f"intensity={packet['intensity']:.2f}  clients={len(self.clients)}",
                  file=sys.stderr)
            self._last_status_log = now
            self._last_logged_odour = packet["odour"]

    async def broadcast_loop(self) -> None:
        """Emit a packet every ``self.period`` seconds — forever, independent
        of how many clients are connected. Inference runs and the prediction is
        logged every tick regardless of `self.clients`; the broadcast is simply
        skipped when there's no one to send to. So with zero clients (before the
        first connects, or after the last disconnects) the server stays up and
        keeps logging the live prediction."""
        start = time.monotonic()
        next_tick = start
        while True:
            now = time.monotonic()
            try:
                packet = build_packet(self.estimator, self.seq, t=now - start)
                self.seq += 1
                if self.clients:
                    msg = json.dumps(packet)
                    # websockets.broadcast sends to all without awaiting each,
                    # and swallows per-client send errors internally — a client
                    # dropping mid-send can't break this tick.
                    websockets.broadcast(self.clients, msg)
                self._maybe_log_status(packet, now)
            except Exception as e:
                # A bad tick shouldn't take the whole server down; skip it.
                print(f"# broadcast_loop: skipping tick, error: {e}", file=sys.stderr)
            # Fixed-rate scheduling that doesn't drift under normal load, but
            # resyncs to "now" instead of firing a backlog burst after a long
            # stall (e.g. the host process being suspended).
            next_tick = max(next_tick + self.period, now)
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))


async def _watch_receiver_process(proc: subprocess.Popen, poll_interval_s: float = 2.0) -> None:
    """Surface the spawned receiver dying in ws_publisher's own output —
    otherwise the only sign is the receiver's own interleaved stderr (easy to
    miss), and the estimator just keeps reporting whatever it last had
    (frozen, if a stale file's cycles already filled its buffers) with no
    obvious link back to "the receiver isn't running any more"."""
    while proc.poll() is None:
        await asyncio.sleep(poll_interval_s)
    print(f"# WARN: the spawned bme690_receiver.py subprocess exited (code {proc.returncode}) — "
          "no new cycles will arrive. Check the hardware connection (the receiver's own output "
          "above should say why, e.g. 'no sensors initialised'), then restart ws_publisher.py.",
          file=sys.stderr)


async def serve(
    host: str, port: int, rate_hz: float, estimator: Estimator, verbose: bool = False,
    replay_csv: Optional[Path] = None, replay_speed: float = 1.0,
    replay_sensor_indices: Optional[List[int]] = None,
    live_tail: bool = False, live_tail_path: Optional[Path] = None,
    receiver_proc: Optional[subprocess.Popen] = None,
) -> None:
    pub = Publisher(rate_hz, estimator, verbose=verbose)
    tasks = [asyncio.ensure_future(pub.broadcast_loop())]
    if replay_csv is not None:
        from real_ml import replay_into
        tasks.append(asyncio.ensure_future(
            replay_into(estimator, replay_csv, replay_sensor_indices, speed=replay_speed, verbose=verbose)
        ))
    elif live_tail:
        from real_ml import LiveCycleFeeder
        feeder = LiveCycleFeeder(estimator, replay_sensor_indices, explicit_path=live_tail_path, verbose=verbose)
        tasks.append(asyncio.ensure_future(feeder.run()))
    elif hasattr(estimator, "push_cycle"):
        # RealEstimator (duck-typed, not isinstance'd, to avoid an eager
        # real_ml import here) with no data source: it'll report idle
        # forever, which otherwise looks identical to "broken" in the
        # terminal (see the packet heartbeat below) — say so once, up front.
        # (Only reachable via --no-auto-receiver — the default spawns+tails.)
        print("# WARN: --no-auto-receiver given — every packet will report "
              "odour_confidence=0, intensity=0 until something calls the estimator's "
              "push_cycle(). Drop --no-auto-receiver to spawn+tail bme690_receiver.py, or "
              "pass --live-tail if it's already running elsewhere, or --replay-csv <path> "
              "to demo with a capture.",
              file=sys.stderr)
    if receiver_proc is not None:
        tasks.append(asyncio.ensure_future(_watch_receiver_process(receiver_proc)))
    async with websockets.serve(pub.handler, host, port):
        print(f"# serving ws://{host}:{port}  @ {rate_hz:g} Hz  "
              f"(Ctrl+C to stop)", file=sys.stderr)
        print(f"# HoloLens connects to ws://<this-host-LAN-ip>:{port}",
              file=sys.stderr)
        await asyncio.gather(*tasks)


def main() -> int:
    # Status logging (this module + real_ml.py) is only useful if it shows up
    # as it happens — on some platforms sys.stderr block-buffers once
    # redirected/piped rather than flushing per line, which would silently
    # delay every "sensor ready"/heartbeat message. Force line buffering so
    # `python ws_publisher.py ... | tee log.txt` etc. still show messages live.
    sys.stderr.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address (default 0.0.0.0 = all interfaces)")
    ap.add_argument("--port", type=int, default=8765,
                    help="bind port (default 8765)")
    ap.add_argument("--rate", type=float, default=5.0,
                    help="packets per second (default 5)")
    ap.add_argument("--once", action="store_true",
                    help="print a single packet to stdout and exit (no server)")
    ap.add_argument("--dummy", action="store_true",
                    help="use the old simulated DummyEstimator instead of the trained models "
                         "in models/ (for transport-only testing without the model files)")
    ap.add_argument("--replay-csv", type=Path, default=None,
                    help="replay a captured CSV's cycles into the real estimator at real "
                         "pace instead of using a live/spawned receiver (see real_ml.py)")
    ap.add_argument("--replay-speed", type=float, default=1.0,
                    help="replay speed multiplier (default 1.0 = real-time pace)")
    ap.add_argument("--live-tail", action="store_true",
                    help="live-tail the newest bme690_receiver_*.csv into the real estimator "
                         "without spawning a receiver — use when one is already running "
                         "separately (e.g. in another terminal); the default spawns one itself")
    ap.add_argument("--no-auto-receiver", action="store_true",
                    help="don't spawn or tail a receiver at all — the estimator stays idle "
                         "(reports odour_confidence=0) unless combined with --dummy")
    ap.add_argument("--bmeconfig", type=Path,
                    default=Path(__file__).resolve().parent / "Sample.bmeconfig",
                    help="config used to identify HP354 sensors, and passed as --config to "
                         "the auto-spawned receiver")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="show all routine status (startup steps, sensor-ready transitions, "
                         "live-tail/replay progress, the receiver's own output) plus a line on "
                         "every packet and every buffered cycle — normally only the packet "
                         "stream and abnormal events are shown")
    args = ap.parse_args()

    global _VERBOSE
    _VERBOSE = args.verbose

    explicit_sources = sum([args.replay_csv is not None, args.live_tail, args.no_auto_receiver])
    if explicit_sources > 1:
        sys.exit("ERROR: choose only one of --replay-csv, --live-tail, --no-auto-receiver")
    if args.dummy and explicit_sources:
        sys.exit("ERROR: --replay-csv/--live-tail/--no-auto-receiver select how the real "
                  "estimator gets data; no effect with --dummy")

    if args.dummy:
        mode = "dummy"
    elif args.replay_csv is not None:
        mode = "replay"
    elif args.live_tail:
        mode = "live_tail"
    elif args.no_auto_receiver:
        mode = "idle"
    else:
        mode = "auto_receiver"  # default: spawn bme690_receiver.py ourselves and live-tail it

    estimator = make_estimator(use_dummy=(mode == "dummy"), verbose=args.verbose)

    if args.once:
        print(json.dumps(build_packet(estimator, seq=0, t=0.0)))
        return 0

    replay_sensor_indices = None
    if mode in ("replay", "live_tail", "auto_receiver"):
        from real_ml import hp354_sensor_indices
        replay_sensor_indices = hp354_sensor_indices(args.bmeconfig)

    receiver_proc: Optional[subprocess.Popen] = None
    auto_receiver_csv: Optional[Path] = None
    if mode == "auto_receiver":
        this_dir = Path(__file__).resolve().parent
        receiver_script = this_dir / "bme690_receiver.py"
        # Tell the subprocess exactly where to write, and tail exactly that
        # file (LiveCycleFeeder's explicit_path) instead of globbing for
        # "newest CSV in data/" — glob discovery can silently lock onto an
        # unrelated older capture still sitting in data/ if this process
        # hasn't written its file yet (or never does, e.g. no hardware
        # found), which happened in practice.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        auto_receiver_csv = this_dir / "data" / f"bme690_receiver_{ts}.csv"
        log_routine(f"# ws_publisher: starting bme690_receiver.py --config {args.bmeconfig.name} "
                    f"-o {auto_receiver_csv.name} (pass --live-tail if you'd rather run it "
                    f"yourself, or --no-auto-receiver for none at all)")
        # Capture the receiver's stderr and forward only abnormal lines (its
        # routine startup dump is otherwise pure noise interleaved with the
        # packet stream); --verbose forwards everything. stdout is unused (the
        # receiver writes its CSV to the -o file).
        receiver_proc = subprocess.Popen(
            [sys.executable, str(receiver_script), "--config", str(args.bmeconfig),
             "-o", str(auto_receiver_csv)],
            cwd=str(this_dir),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        threading.Thread(target=_forward_receiver_output, args=(receiver_proc,), daemon=True).start()

    try:
        asyncio.run(serve(
            args.host, args.port, args.rate, estimator, verbose=args.verbose,
            replay_csv=(args.replay_csv if mode == "replay" else None),
            replay_speed=args.replay_speed,
            replay_sensor_indices=replay_sensor_indices,
            live_tail=(mode in ("live_tail", "auto_receiver")),
            live_tail_path=auto_receiver_csv,
            receiver_proc=receiver_proc,
        ))
    except KeyboardInterrupt:
        print("\n# stopped.", file=sys.stderr)
    finally:
        if receiver_proc is not None and receiver_proc.poll() is None:
            log_routine("# ws_publisher: stopping bme690_receiver.py subprocess")
            receiver_proc.terminate()
            try:
                receiver_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                receiver_proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
