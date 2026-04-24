"""Spawn deterministic, machine-identifiable tone processes for catap tests."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from catap._devtools.tone import TonePlayer, pure_sine_sample

DEFAULT_BASE_FREQUENCY_HZ = 431.0
DEFAULT_FREQUENCY_STEP_HZ = 173.0
DEFAULT_AMPLITUDE = 0.04
DEFAULT_BUFFER_SECONDS = 30.0
WAVEFORM = "pure_sine"


@dataclass(frozen=True)
class ToneSpec:
    """One tone source managed by the farm."""

    tone_id: str
    frequency_hz: float
    amplitude: float
    channel_mode: str


@dataclass
class WorkerProcess:
    """A spawned tone worker process."""

    spec: ToneSpec
    process: subprocess.Popen[str]


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _format_float(value: float) -> str:
    return f"{value:.12g}"


def _parse_frequencies(value: str | None) -> list[float]:
    if not value:
        return []

    frequencies = []
    for part in value.split(","):
        stripped = part.strip()
        if not stripped:
            raise argparse.ArgumentTypeError("frequency list contains an empty item")
        frequency = float(stripped)
        if frequency <= 0:
            raise argparse.ArgumentTypeError("frequencies must be greater than 0")
        frequencies.append(frequency)
    return frequencies


def _default_frequencies(count: int, sample_rate: int) -> list[float]:
    nyquist_margin_hz = 100.0
    max_frequency = (sample_rate / 2.0) - nyquist_margin_hz
    frequencies = [
        DEFAULT_BASE_FREQUENCY_HZ + (DEFAULT_FREQUENCY_STEP_HZ * index)
        for index in range(count)
    ]
    if frequencies[-1] >= max_frequency:
        raise ValueError(
            "too many default tones for the sample rate; pass --frequencies explicitly"
        )
    return frequencies


def _build_specs(
    *,
    count: int | None,
    frequency_arg: str | None,
    sample_rate: int,
    amplitude: float,
    channel_mode: str,
) -> list[ToneSpec]:
    frequencies = _parse_frequencies(frequency_arg)
    resolved_count = count if count is not None else len(frequencies) or 1
    if frequencies and len(frequencies) != resolved_count:
        raise ValueError("--count must match the number of --frequencies")
    if not frequencies:
        frequencies = _default_frequencies(resolved_count, sample_rate)

    return [
        ToneSpec(
            tone_id=f"tone-{index + 1:03d}",
            frequency_hz=frequency,
            amplitude=amplitude,
            channel_mode=channel_mode,
        )
        for index, frequency in enumerate(frequencies)
    ]


def _channel_gains(channels: int, mode: str) -> tuple[float, ...]:
    if mode == "all":
        return tuple(1.0 for _ in range(channels))
    if mode == "left":
        return (1.0, *tuple(0.0 for _ in range(channels - 1)))
    if mode == "right":
        if channels < 2:
            raise ValueError("--channel-mode right requires at least two channels")
        return (0.0, 1.0, *tuple(0.0 for _ in range(channels - 2)))
    raise ValueError(f"unknown channel mode: {mode}")


def _resolve_output_device_id(device_uid: str | None) -> int | None:
    if device_uid is None:
        return None

    from catap import list_audio_devices

    for device in list_audio_devices():
        if device.uid == device_uid and device.output_streams:
            return device.audio_object_id
    raise LookupError(f"No output-capable device matches UID '{device_uid}'")


def _make_player(
    args: argparse.Namespace,
    *,
    device_id: int | None,
    duration_seconds: float,
) -> TonePlayer:
    return TonePlayer(
        duration_seconds=duration_seconds,
        sample_rate=float(args.sample_rate),
        channels=args.channels,
        frequency_hz=args.frequency,
        amplitude=args.amplitude,
        device_id=device_id,
        sample_fn=pure_sine_sample,
        apply_fade=True,
        channel_gains=_channel_gains(args.channels, args.channel_mode),
    )


def _print_worker_ready(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            {
                "event": "worker_ready",
                "tone_id": args.tone_id,
                "pid": os.getpid(),
                "frequency_hz": args.frequency,
                "waveform": WAVEFORM,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def _worker_main(args: argparse.Namespace) -> int:
    stop_requested = False

    def _request_stop(_signum: int, _frame: object | None) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    device_id = _resolve_output_device_id(args.device_uid)
    deadline = None if args.seconds == 0 else time.monotonic() + args.seconds
    ready_printed = False
    player: TonePlayer | None = None

    try:
        while not stop_requested:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break

            duration_seconds = args.buffer_seconds
            if deadline is not None:
                duration_seconds = min(duration_seconds, max(0.01, deadline - now))

            if player is not None:
                player.stop()
            player = _make_player(
                args,
                device_id=device_id,
                duration_seconds=duration_seconds,
            )
            player.start()
            if not ready_printed:
                _print_worker_ready(args)
                ready_printed = True

            chunk_deadline = time.monotonic() + duration_seconds
            while not stop_requested:
                if time.monotonic() >= chunk_deadline:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
    finally:
        if player is not None:
            player.stop()
    return 0


def _worker_command(args: argparse.Namespace, spec: ToneSpec) -> list[str]:
    command = [
        sys.executable,
        "-u",
        "-m",
        "catap._devtools.tone_farm",
        "--worker",
        "--tone-id",
        spec.tone_id,
        "--frequency",
        _format_float(spec.frequency_hz),
        "--seconds",
        _format_float(args.seconds),
        "--buffer-seconds",
        _format_float(args.buffer_seconds),
        "--sample-rate",
        str(args.sample_rate),
        "--channels",
        str(args.channels),
        "--amplitude",
        _format_float(spec.amplitude),
        "--channel-mode",
        spec.channel_mode,
    ]
    if args.device_uid:
        command.extend(["--device-uid", args.device_uid])
    return command


def _spawn_workers(
    args: argparse.Namespace,
    specs: Sequence[ToneSpec],
) -> list[WorkerProcess]:
    workers = []
    for spec in specs:
        process = subprocess.Popen(
            _worker_command(args, spec),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        workers.append(WorkerProcess(spec=spec, process=process))
    return workers


def _read_worker_stderr(worker: WorkerProcess) -> str:
    stderr = worker.process.stderr
    if stderr is None:
        return ""
    with contextlib.suppress(Exception):
        return stderr.read().strip()
    return ""


def _raise_worker_failed(worker: WorkerProcess) -> None:
    stderr = _read_worker_stderr(worker)
    detail = f": {stderr}" if stderr else ""
    raise RuntimeError(
        f"{worker.spec.tone_id} exited before becoming ready "
        f"(status {worker.process.returncode}){detail}"
    )


def _wait_for_ready(
    workers: Sequence[WorkerProcess],
    *,
    timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    selector = selectors.DefaultSelector()
    ready: dict[str, dict[str, Any]] = {}
    try:
        for worker in workers:
            if worker.process.stdout is None:
                raise RuntimeError(f"{worker.spec.tone_id} has no stdout pipe")
            selector.register(worker.process.stdout, selectors.EVENT_READ, worker)

        deadline = time.monotonic() + timeout_seconds
        while len(ready) < len(workers):
            for worker in workers:
                if (
                    worker.spec.tone_id not in ready
                    and worker.process.poll() is not None
                ):
                    _raise_worker_failed(worker)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pending = [
                    worker.spec.tone_id
                    for worker in workers
                    if worker.spec.tone_id not in ready
                ]
                raise TimeoutError(
                    "Timed out waiting for tone worker readiness: "
                    + ", ".join(pending)
                )

            for key, _mask in selector.select(timeout=min(0.1, remaining)):
                worker = cast(WorkerProcess, key.data)
                stream = cast(TextIO, key.fileobj)
                line = stream.readline()
                if not line:
                    if worker.process.poll() is not None:
                        _raise_worker_failed(worker)
                    continue

                event = json.loads(line)
                if event.get("event") != "worker_ready":
                    continue
                tone_id = str(event["tone_id"])
                ready[tone_id] = event
                selector.unregister(key.fileobj)
    finally:
        selector.close()
    return ready


def _wait_for_audio_processes(
    ready_events: dict[str, dict[str, Any]],
    *,
    timeout_seconds: float,
) -> dict[int, Any]:
    from catap import list_audio_processes

    pending_pids = {int(event["pid"]) for event in ready_events.values()}
    processes_by_pid: dict[int, Any] = {}
    deadline = time.monotonic() + timeout_seconds

    while pending_pids and time.monotonic() < deadline:
        for process in list_audio_processes():
            if process.pid in pending_pids:
                processes_by_pid[process.pid] = process
                pending_pids.remove(process.pid)
        if pending_pids:
            time.sleep(0.05)

    return processes_by_pid


def _build_manifest(
    *,
    args: argparse.Namespace,
    specs: Sequence[ToneSpec],
    ready_events: dict[str, dict[str, Any]],
    processes_by_pid: dict[int, Any],
) -> dict[str, Any]:
    tones = []
    for spec in specs:
        ready_event = ready_events[spec.tone_id]
        pid = int(ready_event["pid"])
        process = processes_by_pid.get(pid)
        tones.append(
            {
                "id": spec.tone_id,
                "pid": pid,
                "audio_object_id": (
                    None if process is None else int(process.audio_object_id)
                ),
                "process_name": None if process is None else process.name,
                "bundle_id": None if process is None else process.bundle_id,
                "is_outputting": (
                    None if process is None else bool(process.is_outputting)
                ),
                "frequency_hz": spec.frequency_hz,
                "amplitude": spec.amplitude,
                "waveform": WAVEFORM,
                "channel_mode": spec.channel_mode,
                "channels": args.channels,
                "sample_rate": args.sample_rate,
                "device_uid": args.device_uid,
            }
        )

    return {
        "schema": "catap-tone-farm/v1",
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "manager_pid": os.getpid(),
        "seconds": args.seconds,
        "buffer_seconds": args.buffer_seconds,
        "tones": tones,
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _stop_workers(workers: Sequence[WorkerProcess]) -> None:
    for worker in workers:
        if worker.process.poll() is None:
            worker.process.terminate()

    deadline = time.monotonic() + 2.0
    for worker in workers:
        remaining = max(0.0, deadline - time.monotonic())
        with contextlib.suppress(subprocess.TimeoutExpired):
            worker.process.wait(timeout=remaining)

    for worker in workers:
        if worker.process.poll() is None:
            worker.process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                worker.process.wait(timeout=0.5)


def _monitor_workers(workers: Sequence[WorkerProcess], seconds: float) -> int:
    stop_requested = False

    def _request_stop(_signum: int, _frame: object | None) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    deadline = None if seconds == 0 else time.monotonic() + seconds + 0.5
    while not stop_requested:
        return_codes = [worker.process.poll() for worker in workers]
        if all(return_code is not None for return_code in return_codes):
            return 0 if all(return_code == 0 for return_code in return_codes) else 1
        for worker, return_code in zip(workers, return_codes, strict=True):
            if return_code not in (None, 0):
                print(
                    f"{worker.spec.tone_id} exited unexpectedly with {return_code}",
                    file=sys.stderr,
                )
                return 1
        if deadline is not None and time.monotonic() >= deadline:
            return 0
        time.sleep(0.1)
    return 0


def _manager_main(args: argparse.Namespace) -> int:
    if args.amplitude > 1.0:
        raise ValueError("--amplitude must be between 0 and 1")
    if args.channels < 2 and args.channel_mode == "right":
        raise ValueError("--channel-mode right requires at least two channels")

    specs = _build_specs(
        count=args.count,
        frequency_arg=args.frequencies,
        sample_rate=args.sample_rate,
        amplitude=args.amplitude,
        channel_mode=args.channel_mode,
    )
    workers = _spawn_workers(args, specs)
    try:
        ready_events = _wait_for_ready(
            workers,
            timeout_seconds=args.ready_timeout,
        )
        processes_by_pid = _wait_for_audio_processes(
            ready_events,
            timeout_seconds=args.process_timeout,
        )
        manifest = _build_manifest(
            args=args,
            specs=specs,
            ready_events=ready_events,
            processes_by_pid=processes_by_pid,
        )
        if args.manifest is not None:
            _write_manifest(args.manifest, manifest)
        print(json.dumps(manifest, sort_keys=True), flush=True)
        return _monitor_workers(workers, args.seconds)
    finally:
        _stop_workers(workers)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tone-id", default="tone-001", help=argparse.SUPPRESS)
    parser.add_argument(
        "--count",
        type=_positive_int,
        help="Number of independent tone worker processes to spawn.",
    )
    parser.add_argument(
        "--frequencies",
        help="Comma-separated tone frequencies in Hz. Count must match --count.",
    )
    parser.add_argument(
        "--frequency",
        type=_positive_float,
        default=DEFAULT_BASE_FREQUENCY_HZ,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--seconds",
        type=_non_negative_float,
        default=0.0,
        help="How long to keep tones alive; 0 means indefinitely (default: 0).",
    )
    parser.add_argument(
        "--buffer-seconds",
        type=_positive_float,
        default=DEFAULT_BUFFER_SECONDS,
        help=(
            "Playback chunk length for each worker in seconds "
            f"(default: {DEFAULT_BUFFER_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--sample-rate",
        type=_positive_int,
        default=44_100,
        help="Worker sample rate in Hz (default: 44100).",
    )
    parser.add_argument(
        "--channels",
        type=_positive_int,
        default=2,
        help="Worker output channel count (default: 2).",
    )
    parser.add_argument(
        "--amplitude",
        type=_positive_float,
        default=DEFAULT_AMPLITUDE,
        help=f"Linear peak amplitude from 0 to 1 (default: {DEFAULT_AMPLITUDE}).",
    )
    parser.add_argument(
        "--channel-mode",
        choices=["all", "left", "right"],
        default="all",
        help="Which output channel(s) carry each tone (default: all).",
    )
    parser.add_argument(
        "--device-uid",
        help="Output device UID to use; defaults to the system output.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Optional path where the JSON tone manifest should be written.",
    )
    parser.add_argument(
        "--ready-timeout",
        type=_positive_float,
        default=5.0,
        help="Seconds to wait for worker playback readiness (default: 5).",
    )
    parser.add_argument(
        "--process-timeout",
        type=_non_negative_float,
        default=5.0,
        help="Seconds to wait for Core Audio process metadata (default: 5).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.worker:
            return _worker_main(args)
        return _manager_main(args)
    except Exception as exc:
        print(f"tone_farm: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
