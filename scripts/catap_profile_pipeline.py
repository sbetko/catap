"""Profile catap's synthetic recording pipeline.

This script does not create a Core Audio tap and does not require system-audio
permission. It feeds synthetic float32 stereo buffers through the same
AudioConverter and background worker pieces used by AudioRecorder.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import sys
import tempfile
import time
from array import array
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from catap._recording_worker import _AudioWorker, _WorkerConfig
from catap.bindings._audiotoolbox import PcmAudioConverter, make_linear_pcm_asbd


@dataclass(frozen=True, slots=True)
class ProfileResult:
    """One profiling result row."""

    name: str
    buffers: int
    dropped_buffers: int
    frames_per_buffer: int
    input_bytes: int
    elapsed_seconds: float
    audio_seconds: float
    audio_realtime_factor: float
    input_mebibytes_per_second: float
    note: str = ""


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def _float32_payload(frames: int, channels: int) -> bytes:
    """Return a deterministic little-endian float32 PCM buffer."""
    samples = array("f")
    for index in range(frames * channels):
        phase = (index % 97) / 97.0
        samples.append(0.25 * math.sin(phase * math.tau))

    if samples.itemsize != 4:
        raise RuntimeError("array('f') is not 32-bit on this Python build")
    if sys.byteorder != "little":
        samples.byteswap()

    return samples.tobytes()


def _make_result(
    *,
    name: str,
    buffers: int,
    dropped_buffers: int,
    frames_per_buffer: int,
    sample_rate: float,
    input_bytes: int,
    elapsed_seconds: float,
    note: str = "",
) -> ProfileResult:
    audio_seconds = (buffers * frames_per_buffer) / sample_rate
    if elapsed_seconds > 0:
        realtime_factor = audio_seconds / elapsed_seconds
        mib_per_second = (input_bytes / (1024 * 1024)) / elapsed_seconds
    else:
        realtime_factor = 0.0
        mib_per_second = 0.0

    return ProfileResult(
        name=name,
        buffers=buffers,
        dropped_buffers=dropped_buffers,
        frames_per_buffer=frames_per_buffer,
        input_bytes=input_bytes,
        elapsed_seconds=elapsed_seconds,
        audio_seconds=audio_seconds,
        audio_realtime_factor=realtime_factor,
        input_mebibytes_per_second=mib_per_second,
        note=note,
    )


def profile_converter(
    *,
    payload: bytes,
    buffers: int,
    frames_per_buffer: int,
    sample_rate: float,
    channels: int,
) -> ProfileResult:
    """Measure raw AudioConverter float32-to-int16 throughput."""
    source_format = make_linear_pcm_asbd(sample_rate, channels, 32, is_float=True)
    destination_format = make_linear_pcm_asbd(sample_rate, channels, 16, is_float=False)
    input_buffer = (ctypes.c_char * len(payload)).from_buffer_copy(payload)

    with PcmAudioConverter(source_format, destination_format) as converter:
        converter.convert(input_buffer, len(payload))

        started = time.perf_counter()
        for _ in range(buffers):
            converter.convert(input_buffer, len(payload))
        elapsed = time.perf_counter() - started

    return _make_result(
        name="converter",
        buffers=buffers,
        dropped_buffers=0,
        frames_per_buffer=frames_per_buffer,
        sample_rate=sample_rate,
        input_bytes=buffers * len(payload),
        elapsed_seconds=elapsed,
    )


def profile_worker(
    *,
    name: str,
    payload: bytes,
    buffers: int,
    frames_per_buffer: int,
    sample_rate: float,
    channels: int,
    max_pending_buffers: int,
    output_path: Path | None,
    on_data: Callable[[bytes, int], None] | None,
    convert_float_output: bool,
) -> ProfileResult:
    """Measure background worker throughput for one sink configuration."""
    dropped_buffers = 0
    dropped_frames = 0

    def record_dropped_frames(num_frames: int) -> None:
        nonlocal dropped_buffers, dropped_frames
        dropped_buffers += 1
        dropped_frames += num_frames

    def consume_dropped_stats() -> tuple[int, int]:
        return dropped_buffers, dropped_frames

    worker = _AudioWorker(
        record_dropped_frames=record_dropped_frames,
        consume_dropped_stats=consume_dropped_stats,
    )
    config = _WorkerConfig(
        output_path=output_path,
        on_data=on_data,
        max_pending_buffers=max_pending_buffers,
        sample_rate=sample_rate,
        num_channels=channels,
        bits_per_sample=32,
        output_bits_per_sample=16 if convert_float_output else 32,
        convert_float_output=convert_float_output,
    )

    input_buffer = (ctypes.c_char * len(payload)).from_buffer_copy(payload)
    accepted_buffers = 0
    note = ""

    worker.start(config)
    started = time.perf_counter()
    try:
        for _ in range(buffers):
            buffer = worker.acquire_pool_buffer(len(payload))
            if buffer is None:
                record_dropped_frames(frames_per_buffer)
                continue

            ctypes.memmove(buffer, input_buffer, len(payload))
            if worker.enqueue_audio_data(buffer, frames_per_buffer, len(payload)):
                accepted_buffers += 1
    finally:
        try:
            worker.stop()
        except (OSError, RuntimeError) as exc:
            note = str(exc)
    elapsed = time.perf_counter() - started

    return _make_result(
        name=name,
        buffers=accepted_buffers,
        dropped_buffers=dropped_buffers,
        frames_per_buffer=frames_per_buffer,
        sample_rate=sample_rate,
        input_bytes=accepted_buffers * len(payload),
        elapsed_seconds=elapsed,
        note=note,
    )


def run_profiles(args: argparse.Namespace) -> list[ProfileResult]:
    """Run the selected synthetic profiles."""
    payload = _float32_payload(args.buffer_frames, args.channels)
    max_pending_buffers = args.max_pending_buffers or args.buffers

    results = [
        profile_converter(
            payload=payload,
            buffers=args.buffers,
            frames_per_buffer=args.buffer_frames,
            sample_rate=args.sample_rate,
            channels=args.channels,
        )
    ]

    callback_bytes = 0

    def on_data(data: bytes, _num_frames: int) -> None:
        nonlocal callback_bytes
        callback_bytes += len(data)

    results.append(
        profile_worker(
            name="worker-callback",
            payload=payload,
            buffers=args.buffers,
            frames_per_buffer=args.buffer_frames,
            sample_rate=args.sample_rate,
            channels=args.channels,
            max_pending_buffers=max_pending_buffers,
            output_path=None,
            on_data=on_data,
            convert_float_output=False,
        )
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        results.append(
            profile_worker(
                name="worker-wav",
                payload=payload,
                buffers=args.buffers,
                frames_per_buffer=args.buffer_frames,
                sample_rate=args.sample_rate,
                channels=args.channels,
                max_pending_buffers=max_pending_buffers,
                output_path=Path(tmpdir) / "profile.wav",
                on_data=None,
                convert_float_output=True,
            )
        )

    if args.slow_callback_ms is not None:
        delay_seconds = args.slow_callback_ms / 1000.0

        def slow_on_data(data: bytes, _num_frames: int) -> None:
            del data
            time.sleep(delay_seconds)

        results.append(
            profile_worker(
                name="slow-callback",
                payload=payload,
                buffers=args.buffers,
                frames_per_buffer=args.buffer_frames,
                sample_rate=args.sample_rate,
                channels=args.channels,
                max_pending_buffers=args.slow_max_pending_buffers,
                output_path=None,
                on_data=slow_on_data,
                convert_float_output=False,
            )
        )

    return results


def print_table(results: Sequence[ProfileResult]) -> None:
    """Print profiling results as a compact table."""
    print(
        f"{'profile':<18} {'buffers':>8} {'dropped':>8} "
        f"{'elapsed':>9} {'audio':>9} {'x realtime':>11} {'MiB/s':>9}"
    )
    print("-" * 82)
    for result in results:
        print(
            f"{result.name:<18} "
            f"{result.buffers:>8} "
            f"{result.dropped_buffers:>8} "
            f"{result.elapsed_seconds:>8.3f}s "
            f"{result.audio_seconds:>8.3f}s "
            f"{result.audio_realtime_factor:>11.1f} "
            f"{result.input_mebibytes_per_second:>9.1f}"
        )
        if result.note:
            print(f"{'':<18} note: {result.note}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Profile catap's synthetic converter and worker pipeline."
    )
    parser.add_argument(
        "--buffers",
        type=_positive_int,
        default=2_000,
        help="Number of buffers to feed into each profile (default: 2000)",
    )
    parser.add_argument(
        "--buffer-frames",
        type=_positive_int,
        default=512,
        help="Frames per synthetic input buffer (default: 512)",
    )
    parser.add_argument(
        "--sample-rate",
        type=float,
        default=48_000.0,
        help="Synthetic sample rate in Hz (default: 48000)",
    )
    parser.add_argument(
        "--channels",
        type=_positive_int,
        default=2,
        help="Synthetic channel count (default: 2)",
    )
    parser.add_argument(
        "--max-pending-buffers",
        type=_positive_int,
        default=None,
        help="Worker queue depth for normal profiles (default: --buffers)",
    )
    parser.add_argument(
        "--slow-callback-ms",
        type=_non_negative_float,
        default=None,
        help="Also run a slow callback profile with this per-buffer delay",
    )
    parser.add_argument(
        "--slow-max-pending-buffers",
        type=_positive_int,
        default=8,
        help="Queue depth for --slow-callback-ms (default: 8)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a table",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.sample_rate <= 0:
        parser.error("--sample-rate must be greater than 0")

    results = run_profiles(args)
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
