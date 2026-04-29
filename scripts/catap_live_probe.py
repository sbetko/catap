"""Probe live catap capture timing through the public session API."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from catap import AudioBuffer, record_process, record_system_audio

_DROP_RE = re.compile(
    r"Dropped (?P<buffers>\d+) audio buffer\(s\) "
    r"\((?P<frames>\d+) frame\(s\)\)"
)


@dataclass(frozen=True, slots=True)
class LiveProbeResult:
    """Summary of one live recording probe."""

    source: str
    requested_seconds: float
    elapsed_seconds: float
    callbacks: int
    frames: int
    bytes_seen: int
    sample_rate: float | None
    num_channels: int | None
    session_duration_seconds: float
    callback_interval_min_ms: float | None
    callback_interval_mean_ms: float | None
    callback_interval_p95_ms: float | None
    callback_interval_max_ms: float | None
    callback_work_max_ms: float | None
    queue_depth_max: int | None
    queue_depth_mean: float | None
    dropped_buffers: int
    dropped_frames: int
    stop_error: str | None


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


def _ms(value: float | None) -> float | None:
    return None if value is None else value * 1000.0


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return ordered[index]


def _queue_depth(session: Any) -> int | None:
    """Best-effort queue depth for the current private recorder implementation."""
    recorder = getattr(session, "_recorder", None)
    worker = getattr(recorder, "_worker", None)
    state = getattr(worker, "_state", None)
    work_queue = getattr(state, "work_queue", None)
    if work_queue is None:
        return None
    return int(work_queue.qsize())


def _drop_counts(exc: BaseException | None) -> tuple[int, int]:
    if exc is None:
        return 0, 0

    messages = [str(exc), *getattr(exc, "__notes__", [])]
    for message in messages:
        match = _DROP_RE.search(message)
        if match:
            return int(match.group("buffers")), int(match.group("frames"))
    return 0, 0


def run_probe(args: argparse.Namespace) -> LiveProbeResult:
    """Run one live recording probe."""
    callback_times: list[float] = []
    callback_work_times: list[float] = []
    queue_depths: list[int] = []
    frames_seen = 0
    bytes_seen = 0
    slow_callback_seconds = args.slow_callback_ms / 1000.0

    def on_buffer(buffer: AudioBuffer) -> None:
        nonlocal frames_seen, bytes_seen
        started = time.perf_counter()
        callback_times.append(started)
        frames_seen += buffer.frame_count
        bytes_seen += buffer.byte_count
        if slow_callback_seconds > 0:
            time.sleep(slow_callback_seconds)
        callback_work_times.append(time.perf_counter() - started)

    if args.process:
        session = record_process(
            args.process,
            output_path=args.output,
            on_buffer=on_buffer,
            max_pending_buffers=args.max_pending_buffers,
        )
        source = f"process:{args.process}"
    else:
        session = record_system_audio(
            output_path=args.output,
            on_buffer=on_buffer,
            max_pending_buffers=args.max_pending_buffers,
        )
        source = "system"

    stop_error: BaseException | None = None
    sample_rate: float | None = None
    num_channels: int | None = None
    session_duration_seconds = 0.0
    capture_started = time.perf_counter()

    try:
        session.start()
        capture_started = time.perf_counter()
        stream_format = session.stream_format
        if stream_format is not None:
            sample_rate = stream_format.sample_rate
            num_channels = stream_format.num_channels

        deadline = capture_started + args.seconds
        while True:
            now = time.perf_counter()
            if now >= deadline:
                break
            depth = _queue_depth(session)
            if depth is not None:
                queue_depths.append(depth)
            time.sleep(min(args.poll_interval_ms / 1000.0, deadline - now))

        try:
            session.stop()
        except (OSError, RuntimeError) as exc:
            stop_error = exc
    finally:
        if session.is_recording:
            try:
                session.close()
            except (OSError, RuntimeError) as exc:
                if stop_error is None:
                    stop_error = exc

    elapsed = time.perf_counter() - capture_started
    session_duration_seconds = session.duration_seconds
    intervals = [
        current - previous
        for previous, current in pairwise(callback_times)
    ]
    dropped_buffers, dropped_frames = _drop_counts(stop_error)

    return LiveProbeResult(
        source=source,
        requested_seconds=args.seconds,
        elapsed_seconds=elapsed,
        callbacks=len(callback_times),
        frames=frames_seen,
        bytes_seen=bytes_seen,
        sample_rate=sample_rate,
        num_channels=num_channels,
        session_duration_seconds=session_duration_seconds,
        callback_interval_min_ms=_ms(min(intervals)) if intervals else None,
        callback_interval_mean_ms=_ms(statistics.fmean(intervals))
        if intervals
        else None,
        callback_interval_p95_ms=_ms(_p95(intervals)),
        callback_interval_max_ms=_ms(max(intervals)) if intervals else None,
        callback_work_max_ms=_ms(max(callback_work_times))
        if callback_work_times
        else None,
        queue_depth_max=max(queue_depths) if queue_depths else None,
        queue_depth_mean=statistics.fmean(queue_depths) if queue_depths else None,
        dropped_buffers=dropped_buffers,
        dropped_frames=dropped_frames,
        stop_error=str(stop_error) if stop_error is not None else None,
    )


def print_table(result: LiveProbeResult) -> None:
    """Print a compact human-readable probe summary."""
    print(f"source: {result.source}")
    print(
        "capture: "
        f"{result.callbacks} callbacks, "
        f"{result.frames} frames, "
        f"{result.bytes_seen} bytes, "
        f"{result.session_duration_seconds:.3f}s audio"
    )
    print(
        "format: "
        f"{result.sample_rate or 0:g} Hz, "
        f"{result.num_channels or 0} channel(s)"
    )
    print(
        "callback interval ms: "
        f"min={result.callback_interval_min_ms or 0:.3f}, "
        f"mean={result.callback_interval_mean_ms or 0:.3f}, "
        f"p95={result.callback_interval_p95_ms or 0:.3f}, "
        f"max={result.callback_interval_max_ms or 0:.3f}"
    )
    print(f"callback work max ms: {result.callback_work_max_ms or 0:.3f}")
    if result.queue_depth_max is not None:
        print(
            "queue depth: "
            f"mean={result.queue_depth_mean or 0:.2f}, "
            f"max={result.queue_depth_max}"
        )
    else:
        print("queue depth: unavailable")
    print(
        "drops: "
        f"{result.dropped_buffers} buffer(s), "
        f"{result.dropped_frames} frame(s)"
    )
    if result.stop_error:
        print(f"stop error: {result.stop_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe live catap capture timing through the public API."
    )
    parser.add_argument(
        "--seconds",
        type=_positive_float,
        default=2.0,
        help="Capture duration in seconds (default: 2)",
    )
    parser.add_argument(
        "--process",
        help="Record one process by name instead of system audio",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional WAV path; omitted by default for streaming-only capture",
    )
    parser.add_argument(
        "--slow-callback-ms",
        type=_non_negative_float,
        default=0.0,
        help="Sleep in on_buffer for this many milliseconds per buffer",
    )
    parser.add_argument(
        "--max-pending-buffers",
        type=_positive_int,
        default=256,
        help="Recorder worker queue depth (default: 256)",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=_positive_int,
        default=20,
        help="Best-effort queue-depth polling interval (default: 20)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of text",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_probe(args)
    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print_table(result)
    return 1 if result.stop_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
