#!/usr/bin/env python3
"""Synthetic and live profiling helpers for catap."""

from __future__ import annotations

import argparse
import array
import cProfile
import ctypes
import io
import json
import math
import os
import platform
import pstats
import queue
import statistics
import sys
import tempfile
import time
import wave
from collections import Counter
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

from catap import record_system_audio
from catap.bindings.process import list_audio_processes
from catap.recorder import (
    AudioBuffer,
    AudioBufferList,
    AudioRecorder,
    _float32_to_int16,
)


def _profile_summary(profile: cProfile.Profile, limit: int = 12) -> str:
    stream = io.StringIO()
    stats = pstats.Stats(profile, stream=stream).strip_dirs().sort_stats("cumulative")
    stats.print_stats(limit)
    return stream.getvalue().strip()


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _make_float_buffer(num_frames: int, num_channels: int = 2) -> bytes:
    samples = array.array("f")
    for index in range(num_frames * num_channels):
        samples.append(math.sin(index / 17.0) * 0.8)
    return samples.tobytes()


def _configure_synthetic_recorder(recorder: AudioRecorder) -> None:
    recorder._sample_rate = 48_000
    recorder._num_channels = 2
    recorder._bits_per_sample = 32
    recorder._is_float = True
    recorder._output_bits_per_sample = 16
    recorder._convert_float_output = True


def _profile_float_conversion() -> dict[str, Any]:
    results: dict[str, Any] = {}

    for frames, iterations in ((512, 20_000), (4_096, 3_000)):
        data = _make_float_buffer(frames)
        start = time.perf_counter()
        total_output_bytes = 0
        for _ in range(iterations):
            total_output_bytes += len(_float32_to_int16(data))
        elapsed = time.perf_counter() - start
        input_megabytes = (len(data) * iterations) / (1024 * 1024)
        audio_seconds = (frames * iterations) / 48_000
        results[f"{frames}_frame_buffers"] = {
            "frames_per_buffer": frames,
            "iterations": iterations,
            "elapsed_s": round(elapsed, 4),
            "input_mb": round(input_megabytes, 2),
            "throughput_mb_s": round(input_megabytes / elapsed, 2),
            "realtime_factor": round(audio_seconds / elapsed, 1),
            "output_mb": round(total_output_bytes / (1024 * 1024), 2),
        }

    profile = cProfile.Profile()
    data = _make_float_buffer(512)
    profile.enable()
    for _ in range(5_000):
        _float32_to_int16(data)
    profile.disable()
    results["profile"] = _profile_summary(profile)
    return results


def _make_callback_buffer_list(
    num_frames: int, num_channels: int = 2
) -> tuple[ctypes.Array[ctypes.c_char], ctypes.POINTER(AudioBufferList), bytes]:
    audio_data = _make_float_buffer(num_frames, num_channels)
    raw_buffer = ctypes.create_string_buffer(audio_data)
    audio_buffer = AudioBuffer(
        num_channels,
        len(audio_data),
        ctypes.cast(raw_buffer, ctypes.c_void_p),
    )
    buffer_list = AudioBufferList()
    buffer_list.mNumberBuffers = 1
    buffer_list.mBuffers[0] = audio_buffer
    return raw_buffer, ctypes.pointer(buffer_list), audio_data


def _profile_io_proc() -> dict[str, Any]:
    _, buffer_ptr, _ = _make_callback_buffer_list(512)

    recorder = AudioRecorder(1)
    recorder._bits_per_sample = 32
    recorder._is_recording = True

    iterations = 20_000
    start = time.perf_counter()
    for _ in range(iterations):
        recorder._io_proc(1, None, buffer_ptr, None, None, None, None)
    elapsed = time.perf_counter() - start
    callback_budget_ms = (512 / 48_000) * 1000

    profile = cProfile.Profile()
    profile.enable()
    for _ in range(5_000):
        recorder._io_proc(1, None, buffer_ptr, None, None, None, None)
    profile.disable()

    queued = AudioRecorder(1, max_pending_buffers=6_000)
    queued._bits_per_sample = 32
    queued._is_recording = True
    queued._work_queue = queue.Queue(maxsize=6_000)

    queue_iterations = 5_000
    start = time.perf_counter()
    for _ in range(queue_iterations):
        queued._io_proc(1, None, buffer_ptr, None, None, None, None)
    queue_elapsed = time.perf_counter() - start

    return {
        "no_queue": {
            "iterations": iterations,
            "elapsed_s": round(elapsed, 4),
            "us_per_callback": round((elapsed / iterations) * 1_000_000, 2),
            "callback_budget_ms_at_48k": round(callback_budget_ms, 3),
            "pct_of_budget": round(
                (((elapsed / iterations) * 1000) / callback_budget_ms) * 100, 3
            ),
        },
        "with_queue": {
            "iterations": queue_iterations,
            "elapsed_s": round(queue_elapsed, 4),
            "us_per_callback": round(
                (queue_elapsed / queue_iterations) * 1_000_000, 2
            ),
            "queued_items": queued._work_queue.qsize(),
        },
        "profile": _profile_summary(profile),
    }


def _profile_worker_wav() -> dict[str, Any]:
    frames = 512
    num_buffers = 12_000
    data = _make_float_buffer(frames)
    fd, temp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    os.unlink(temp_path)

    try:
        recorder = AudioRecorder(1, temp_path)
        _configure_synthetic_recorder(recorder)

        start = time.perf_counter()
        recorder._start_worker()
        assert recorder._work_queue is not None
        for _ in range(num_buffers):
            recorder._work_queue.put((data, frames))
        recorder._stop_worker()
        elapsed = time.perf_counter() - start

        input_megabytes = (len(data) * num_buffers) / (1024 * 1024)
        audio_seconds = (frames * num_buffers) / 48_000
        thread_results = {
            "buffers": num_buffers,
            "audio_s_equivalent": round(audio_seconds, 2),
            "elapsed_s": round(elapsed, 4),
            "input_mb_s": round(input_megabytes / elapsed, 2),
            "realtime_factor": round(audio_seconds / elapsed, 1),
            "wav_size_mb": round(Path(temp_path).stat().st_size / (1024 * 1024), 2),
        }
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    fd, temp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    os.unlink(temp_path)

    try:
        recorder = AudioRecorder(1, temp_path)
        _configure_synthetic_recorder(recorder)
        recorder._output_file = Path(temp_path).open("wb")  # noqa: SIM115
        recorder._wav_file = wave.open(recorder._output_file, "wb")  # noqa: SIM115
        recorder._wav_file.setnchannels(recorder._num_channels)
        recorder._wav_file.setsampwidth(recorder._output_bits_per_sample // 8)
        recorder._wav_file.setframerate(int(recorder._sample_rate))
        recorder._work_queue = queue.Queue()
        for _ in range(5_000):
            recorder._work_queue.put((data, frames))
        recorder._work_queue.put(None)

        profile = cProfile.Profile()
        profile.enable()
        recorder._worker_loop()
        profile.disable()
        profile_output = _profile_summary(profile)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    return {
        "threaded_wav": thread_results,
        "profile": profile_output,
    }


def _profile_live_process_listing(iterations: int) -> dict[str, Any]:
    wall_times: list[float] = []
    last_count = 0

    for _ in range(iterations):
        start = time.perf_counter()
        processes = list_audio_processes()
        wall_times.append(time.perf_counter() - start)
        last_count = len(processes)

    profile = cProfile.Profile()
    profile.enable()
    for _ in range(5):
        list_audio_processes()
    profile.disable()

    return {
        "iterations": iterations,
        "mean_ms": round(statistics.mean(wall_times) * 1000, 2),
        "median_ms": round(statistics.median(wall_times) * 1000, 2),
        "p95_ms": round(_percentile(wall_times, 0.95) * 1000, 2),
        "count_last": last_count,
        "profile": _profile_summary(profile, 15),
    }


def _profile_live_recording(iterations: int, hold_seconds: float) -> dict[str, Any]:
    startup_times: list[float] = []
    stop_times: list[float] = []
    recorded_frames: list[int] = []

    for _ in range(iterations):
        fd, temp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        os.unlink(temp_path)

        try:
            session = record_system_audio(output_path=temp_path, max_pending_buffers=64)

            start = time.perf_counter()
            session.start()
            startup_times.append(time.perf_counter() - start)

            time.sleep(hold_seconds)

            start = time.perf_counter()
            session.stop()
            stop_times.append(time.perf_counter() - start)
            recorded_frames.append(session.frames_recorded)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    return {
        "iterations": iterations,
        "hold_seconds": hold_seconds,
        "start_mean_ms": round(statistics.mean(startup_times) * 1000, 2),
        "start_median_ms": round(statistics.median(startup_times) * 1000, 2),
        "stop_mean_ms": round(statistics.mean(stop_times) * 1000, 2),
        "stop_median_ms": round(statistics.median(stop_times) * 1000, 2),
        "frames_mean": round(statistics.mean(recorded_frames), 1),
        "frames_min": min(recorded_frames),
        "frames_max": max(recorded_frames),
    }


def _profile_live_callback_shape(hold_seconds: float) -> dict[str, Any]:
    frame_sizes: list[int] = []
    timestamps: list[float] = []

    def on_data(data: bytes, num_frames: int) -> None:
        del data
        frame_sizes.append(num_frames)
        timestamps.append(time.perf_counter())

    session = record_system_audio(on_data=on_data, max_pending_buffers=64)
    session.record_for(hold_seconds)

    intervals = [b - a for a, b in pairwise(timestamps)]
    counter = Counter(frame_sizes)
    bytes_per_frame = 0
    if session.num_channels is not None and session.is_float is not None:
        sample_bytes = 4 if session.is_float else 2
        bytes_per_frame = session.num_channels * sample_bytes

    return {
        "callback_count": len(frame_sizes),
        "unique_frame_sizes": sorted(counter),
        "most_common_frame_sizes": counter.most_common(5),
        "mean_frames": round(statistics.mean(frame_sizes), 2) if frame_sizes else 0.0,
        "median_frames": round(statistics.median(frame_sizes), 2)
        if frame_sizes
        else 0.0,
        "mean_interval_ms": round(statistics.mean(intervals) * 1000, 3)
        if intervals
        else None,
        "median_interval_ms": round(statistics.median(intervals) * 1000, 3)
        if intervals
        else None,
        "sample_rate": session.sample_rate,
        "channels": session.num_channels,
        "is_float": session.is_float,
        "duration_s": round(session.duration_seconds, 6),
        "frames_recorded": session.frames_recorded,
        "bytes_per_buffer_at_mode": (
            frame_sizes[0] * bytes_per_frame
            if frame_sizes and bytes_per_frame
            else None
        ),
    }


def _profile_cli_subprocesses() -> dict[str, Any]:
    import subprocess

    commands = {
        "import_catap": [sys.executable, "-c", "import catap"],
        "list_apps_cli": [sys.executable, "-m", "catap", "list-apps"],
    }
    results: dict[str, Any] = {}

    for name, command in commands.items():
        wall_times: list[float] = []
        for _ in range(7):
            start = time.perf_counter()
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            wall_times.append(time.perf_counter() - start)
        results[name] = {
            "runs": len(wall_times),
            "mean_ms": round(statistics.mean(wall_times) * 1000, 2),
            "median_ms": round(statistics.median(wall_times) * 1000, 2),
            "min_ms": round(min(wall_times) * 1000, 2),
            "max_ms": round(max(wall_times) * 1000, 2),
        }

    return results


def _collect_results(
    skip_live: bool, live_iterations: int, hold_seconds: float
) -> dict[str, Any]:
    results: dict[str, Any] = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        },
        "synthetic": {
            "float32_to_int16": _profile_float_conversion(),
            "io_proc": _profile_io_proc(),
            "worker_wav": _profile_worker_wav(),
        },
    }

    if skip_live:
        results["live"] = {"skipped": True}
        return results

    live_results: dict[str, Any] = {}
    live_sections = (
        ("process_listing", lambda: _profile_live_process_listing(live_iterations)),
        ("record_system_audio", lambda: _profile_live_recording(5, hold_seconds)),
        ("callback_shape", lambda: _profile_live_callback_shape(0.3)),
        ("subprocess_startup", _profile_cli_subprocesses),
    )

    for name, runner in live_sections:
        try:
            live_results[name] = runner()
        except Exception as exc:  # pragma: no cover - best-effort live profiling
            live_results[name] = {"error": repr(exc)}

    results["live"] = live_results
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Profile catap hot paths.")
    parser.add_argument(
        "--skip-live",
        action="store_true",
        help="Skip live macOS recording and process-enumeration checks.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON.",
    )
    parser.add_argument(
        "--live-iterations",
        type=int,
        default=20,
        help="How many times to sample live process enumeration.",
    )
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=0.05,
        help="How long each live recording sample should run.",
    )
    args = parser.parse_args(argv)

    results = _collect_results(args.skip_live, args.live_iterations, args.hold_seconds)

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0

    for section, payload in results.items():
        print(f"=== {section} ===")
        print(json.dumps(payload, indent=2, sort_keys=True))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
