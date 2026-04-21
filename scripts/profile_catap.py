#!/usr/bin/env python3
"""Synthetic and live profiling helpers for catap."""

from __future__ import annotations

import argparse
import array
import cProfile
import ctypes
import gc
import io
import json
import math
import os
import platform
import pstats
import queue
import resource
import statistics
import sys
import tempfile
import time
import tracemalloc
import wave
from collections import Counter, deque
from collections.abc import Callable, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

from catap import record_system_audio
from catap.bindings._audiotoolbox import (
    ExtAudioFileWavWriter,
    PcmAudioConverter,
    make_linear_pcm_asbd,
)
from catap.bindings.process import list_audio_processes
from catap.recorder import (
    AudioBuffer,
    AudioBufferList,
    AudioRecorder,
)


def _discard_audio_data(data: bytes, num_frames: int) -> None:
    """Accept streamed audio data without retaining it."""
    del data, num_frames


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


def _rusage_peak_rss_bytes() -> int:
    """Return the peak RSS high-water mark for this process, in bytes.

    Darwin reports ``ru_maxrss`` in bytes; Linux reports KiB.
    """
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(max_rss)
    return int(max_rss) * 1024


def _cpu_time_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _gc_collections_total() -> int:
    return sum(generation["collections"] for generation in gc.get_stats())


def _summarize_durations_us(times_ns: Sequence[int]) -> dict[str, float]:
    """Summarize a list of per-call durations (in ns) as microseconds."""
    if not times_ns:
        return {
            "iterations": 0,
            "mean_us": 0.0,
            "median_us": 0.0,
            "p95_us": 0.0,
            "p99_us": 0.0,
            "max_us": 0.0,
        }
    times_us = [t / 1000.0 for t in times_ns]
    return {
        "iterations": len(times_us),
        "mean_us": round(statistics.mean(times_us), 3),
        "median_us": round(statistics.median(times_us), 3),
        "p95_us": round(_percentile(times_us, 0.95), 3),
        "p99_us": round(_percentile(times_us, 0.99), 3),
        "max_us": round(max(times_us), 3),
    }


def _measure_timing_distribution(
    fn: Callable[[], Any], iterations: int
) -> dict[str, float]:
    """Time each call individually to expose jitter (p95/p99/max)."""
    gc.collect()
    times_ns: list[int] = [0] * iterations
    perf = time.perf_counter_ns
    for i in range(iterations):
        start = perf()
        fn()
        times_ns[i] = perf() - start
    return _summarize_durations_us(times_ns)


def _measure_allocations(
    fn: Callable[[], Any], iterations: int
) -> dict[str, float | int]:
    """Measure Python-tracked allocations over ``iterations`` calls.

    tracemalloc perturbs timing, so this pass is kept separate from the
    timing-distribution pass; call counts are reliable, wall time is not.
    """
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        current_before, _ = tracemalloc.get_traced_memory()
        for _ in range(iterations):
            fn()
        current_after, peak_after = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    retained = current_after - current_before
    return {
        "iterations": iterations,
        "retained_bytes": int(retained),
        "retained_bytes_per_call": round(retained / iterations, 3),
        "peak_tracked_bytes": int(peak_after),
    }


def _measure_resource_usage(
    fn: Callable[[], Any],
) -> dict[str, float | int]:
    """Capture CPU time, wall time, and GC-collection delta for a single run."""
    gc.collect()
    gc_before = _gc_collections_total()
    cpu_before = _cpu_time_seconds()
    wall_before = time.perf_counter()
    rss_before = _rusage_peak_rss_bytes()

    fn()

    wall_elapsed = time.perf_counter() - wall_before
    cpu_elapsed = _cpu_time_seconds() - cpu_before
    rss_after = _rusage_peak_rss_bytes()
    gc_delta = _gc_collections_total() - gc_before

    return {
        "wall_s": round(wall_elapsed, 4),
        "cpu_s": round(cpu_elapsed, 4),
        "cpu_utilization_pct": round(
            (cpu_elapsed / wall_elapsed * 100.0) if wall_elapsed > 0 else 0.0, 1
        ),
        "peak_rss_bytes": int(rss_after),
        "peak_rss_delta_bytes": int(rss_after - rss_before),
        "gc_collections": int(gc_delta),
    }


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


def _make_synthetic_streaming_recorder(
    max_pending_buffers: int = 256,
) -> AudioRecorder:
    """Create a recorder for synthetic benchmarks that don't write a file."""
    return AudioRecorder(
        1,
        on_data=_discard_audio_data,
        max_pending_buffers=max_pending_buffers,
    )


def _install_pool(
    recorder: AudioRecorder, depth: int, buffer_bytes: int = 4096
) -> None:
    """Populate ``recorder._buffer_pool`` for synthetic io_proc runs.

    Mirrors the production ``_start_worker`` setup so the hot path measures
    the steady-state ``pool.pop`` / ``memmove`` / enqueue path rather than the
    drop-on-exhausted path.
    """
    pool_type = ctypes.c_char * buffer_bytes
    recorder._buffer_pool = deque(pool_type() for _ in range(depth))


def _make_pool_buffer(data: bytes) -> ctypes.Array[ctypes.c_char]:
    """Wrap ``data`` in a ctypes array that matches the worker's pool item type."""
    return (ctypes.c_char * len(data)).from_buffer_copy(data)


def _profile_audio_converter_conversion() -> dict[str, Any]:
    results: dict[str, Any] = {}
    source_format = make_linear_pcm_asbd(48_000, 2, 32, is_float=True)
    destination_format = make_linear_pcm_asbd(48_000, 2, 16, is_float=False)

    for frames, iterations in ((512, 20_000), (4_096, 3_000)):
        data = _make_float_buffer(frames)
        input_buffer = _make_pool_buffer(data)
        byte_count = len(data)
        captured_total: list[int] = []

        with PcmAudioConverter(source_format, destination_format) as converter:
            def _convert_once(
                input_buffer: ctypes.Array[ctypes.c_char] = input_buffer,
                byte_count: int = byte_count,
            ) -> int:
                return converter.convert(input_buffer, byte_count)

            def _run_and_collect(
                converter: PcmAudioConverter = converter,
                input_buffer: ctypes.Array[ctypes.c_char] = input_buffer,
                byte_count: int = byte_count,
                iterations: int = iterations,
                captured_total: list[int] = captured_total,
            ) -> None:
                total = 0
                for _ in range(iterations):
                    total += converter.convert(input_buffer, byte_count)
                captured_total.append(total)

            usage = _measure_resource_usage(_run_and_collect)
            per_call = _measure_timing_distribution(
                _convert_once,
                iterations=min(iterations, 5_000),
            )
            allocations = _measure_allocations(
                _convert_once,
                iterations=min(iterations, 2_000),
            )

        total_output_bytes = captured_total[0]
        input_megabytes = (len(data) * iterations) / (1024 * 1024)
        audio_seconds = (frames * iterations) / 48_000

        results[f"{frames}_frame_buffers"] = {
            "frames_per_buffer": frames,
            "iterations": iterations,
            "elapsed_s": usage["wall_s"],
            "input_mb": round(input_megabytes, 2),
            "throughput_mb_s": round(input_megabytes / usage["wall_s"], 2)
            if usage["wall_s"] > 0
            else 0.0,
            "realtime_factor": round(audio_seconds / usage["wall_s"], 1)
            if usage["wall_s"] > 0
            else 0.0,
            "output_mb": round(total_output_bytes / (1024 * 1024), 2),
            "resource": usage,
            "per_call": per_call,
            "allocations": allocations,
        }

    profile = cProfile.Profile()
    data = _make_float_buffer(512)
    input_buffer = _make_pool_buffer(data)
    with PcmAudioConverter(source_format, destination_format) as converter:
        profile.enable()
        for _ in range(5_000):
            converter.convert(input_buffer, len(data))
        profile.disable()
    results["profile"] = _profile_summary(profile)
    return results


def _profile_write_paths() -> dict[str, Any]:
    results: dict[str, Any] = {}
    source_format = make_linear_pcm_asbd(48_000, 2, 32, is_float=True)
    destination_format = make_linear_pcm_asbd(48_000, 2, 16, is_float=False)

    frames = 512
    num_buffers = 12_000
    data = _make_float_buffer(frames)
    input_buffer = _make_pool_buffer(data)
    byte_count = len(data)
    audio_seconds = (frames * num_buffers) / 48_000

    wav_sizes: list[int] = []

    def _run_audio_converter_wave_once() -> None:
        fd, temp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        os.unlink(temp_path)
        try:
            with PcmAudioConverter(
                source_format, destination_format
            ) as converter, Path(temp_path).open("wb") as output_file:
                wav_file = wave.open(output_file, "wb")  # noqa: SIM115
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(48_000)
                try:
                    for _ in range(num_buffers):
                        converter.convert(input_buffer, byte_count)
                        wav_file.writeframesraw(converter.output_view())
                finally:
                    wav_file.close()
            wav_sizes.append(Path(temp_path).stat().st_size)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    usage = _measure_resource_usage(_run_audio_converter_wave_once)

    fd, temp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    os.unlink(temp_path)
    try:
        with PcmAudioConverter(
            source_format, destination_format
        ) as converter, Path(temp_path).open("wb") as output_file:
            wav_file = wave.open(output_file, "wb")  # noqa: SIM115
            wav_file.setnchannels(2)
            wav_file.setsampwidth(2)
            wav_file.setframerate(48_000)
            try:
                profile = cProfile.Profile()
                profile.enable()
                for _ in range(5_000):
                    converter.convert(input_buffer, byte_count)
                    wav_file.writeframesraw(converter.output_view())
                profile.disable()
            finally:
                wav_file.close()
        profile_output = _profile_summary(profile)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    fd, alloc_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    os.unlink(alloc_path)
    try:

        def _drain_audio_converter_wave_once() -> None:
            with PcmAudioConverter(
                source_format, destination_format
            ) as converter, Path(alloc_path).open("wb") as output_file:
                wav_file = wave.open(output_file, "wb")  # noqa: SIM115
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(48_000)
                try:
                    for _ in range(2_000):
                        converter.convert(input_buffer, byte_count)
                        wav_file.writeframesraw(converter.output_view())
                finally:
                    wav_file.close()

        audio_converter_wave_allocations = _measure_allocations(
            _drain_audio_converter_wave_once,
            iterations=1,
        )
        audio_converter_wave_allocations["buffers_processed"] = 2_000
        audio_converter_wave_allocations["retained_bytes_per_buffer"] = round(
            audio_converter_wave_allocations["retained_bytes"] / 2_000, 3
        )
    finally:
        if os.path.exists(alloc_path):
            os.remove(alloc_path)

    input_megabytes = (len(data) * num_buffers) / (1024 * 1024)
    results["audio_converter_wave"] = {
        "buffers": num_buffers,
        "audio_s_equivalent": round(audio_seconds, 2),
        "elapsed_s": usage["wall_s"],
        "input_mb_s": round(input_megabytes / usage["wall_s"], 2)
        if usage["wall_s"] > 0
        else 0.0,
        "realtime_factor": round(audio_seconds / usage["wall_s"], 1)
        if usage["wall_s"] > 0
        else 0.0,
        "wav_size_mb": round(wav_sizes[0] / (1024 * 1024), 2),
        "resource": usage,
        "allocations": audio_converter_wave_allocations,
        "profile": profile_output,
    }

    wav_sizes = []

    def _run_ext_audio_file_once() -> None:
        fd, temp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        os.unlink(temp_path)
        try:
            with ExtAudioFileWavWriter(
                temp_path,
                sample_rate=48_000,
                num_channels=2,
                client_bits_per_sample=32,
                client_is_float=True,
            ) as writer:
                for _ in range(num_buffers):
                    writer.write(input_buffer, frames, byte_count)
            wav_sizes.append(Path(temp_path).stat().st_size)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    usage = _measure_resource_usage(_run_ext_audio_file_once)

    fd, temp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    os.unlink(temp_path)
    try:
        writer = ExtAudioFileWavWriter(
            temp_path,
            sample_rate=48_000,
            num_channels=2,
            client_bits_per_sample=32,
            client_is_float=True,
        )
        try:
            profile = cProfile.Profile()
            profile.enable()
            for _ in range(5_000):
                writer.write(input_buffer, frames, byte_count)
            profile.disable()
        finally:
            writer.close()
        profile_output = _profile_summary(profile)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    fd, alloc_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    os.unlink(alloc_path)
    try:

        def _drain_ext_audio_file_once() -> None:
            with ExtAudioFileWavWriter(
                alloc_path,
                sample_rate=48_000,
                num_channels=2,
                client_bits_per_sample=32,
                client_is_float=True,
            ) as writer:
                for _ in range(2_000):
                    writer.write(input_buffer, frames, byte_count)

        ext_audio_file_allocations = _measure_allocations(
            _drain_ext_audio_file_once,
            iterations=1,
        )
        ext_audio_file_allocations["buffers_processed"] = 2_000
        ext_audio_file_allocations["retained_bytes_per_buffer"] = round(
            ext_audio_file_allocations["retained_bytes"] / 2_000, 3
        )
    finally:
        if os.path.exists(alloc_path):
            os.remove(alloc_path)

    results["ext_audio_file_wav"] = {
        "buffers": num_buffers,
        "audio_s_equivalent": round(audio_seconds, 2),
        "elapsed_s": usage["wall_s"],
        "input_mb_s": round(input_megabytes / usage["wall_s"], 2)
        if usage["wall_s"] > 0
        else 0.0,
        "realtime_factor": round(audio_seconds / usage["wall_s"], 1)
        if usage["wall_s"] > 0
        else 0.0,
        "wav_size_mb": round(wav_sizes[0] / (1024 * 1024), 2),
        "resource": usage,
        "allocations": ext_audio_file_allocations,
        "profile": profile_output,
    }

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

    # no_queue path recycles the one pool buffer back on every call via
    # ``_enqueue_audio_data``'s no-queue short-circuit. A tiny pool is fine.
    recorder = _make_synthetic_streaming_recorder()
    recorder._bits_per_sample = 32
    recorder._is_recording = True
    _install_pool(recorder, depth=recorder.max_pending_buffers)

    iterations = 20_000
    start = time.perf_counter()
    for _ in range(iterations):
        recorder._io_proc(1, None, buffer_ptr, None, None, None, None)
    elapsed = time.perf_counter() - start
    callback_budget_ms = (512 / 48_000) * 1000

    no_queue_distribution = _measure_timing_distribution(
        lambda: recorder._io_proc(1, None, buffer_ptr, None, None, None, None),
        iterations=5_000,
    )
    no_queue_allocs = _measure_allocations(
        lambda: recorder._io_proc(1, None, buffer_ptr, None, None, None, None),
        iterations=2_000,
    )

    profile = cProfile.Profile()
    profile.enable()
    for _ in range(5_000):
        recorder._io_proc(1, None, buffer_ptr, None, None, None, None)
    profile.disable()

    # with_queue path doesn't recycle: items accumulate in the queue, so the
    # pool drains by ``queue_iterations`` entries. Size accordingly.
    queue_iterations = 5_000
    queued = _make_synthetic_streaming_recorder(max_pending_buffers=6_000)
    queued._bits_per_sample = 32
    queued._is_recording = True
    queued._work_queue = queue.Queue(maxsize=6_000)
    _install_pool(queued, depth=queued.max_pending_buffers)

    start = time.perf_counter()
    for _ in range(queue_iterations):
        queued._io_proc(1, None, buffer_ptr, None, None, None, None)
    queue_elapsed = time.perf_counter() - start
    assert queued._work_queue is not None
    queued_item_count = queued._work_queue.qsize()

    queued_refill = _make_synthetic_streaming_recorder(max_pending_buffers=6_000)
    queued_refill._bits_per_sample = 32
    queued_refill._is_recording = True
    queued_refill._work_queue = queue.Queue(maxsize=6_000)
    _install_pool(queued_refill, depth=queued_refill.max_pending_buffers)
    with_queue_distribution = _measure_timing_distribution(
        lambda: queued_refill._io_proc(
            1, None, buffer_ptr, None, None, None, None
        ),
        iterations=5_000,
    )

    queued_allocs = _make_synthetic_streaming_recorder(max_pending_buffers=6_000)
    queued_allocs._bits_per_sample = 32
    queued_allocs._is_recording = True
    queued_allocs._work_queue = queue.Queue(maxsize=6_000)
    _install_pool(queued_allocs, depth=queued_allocs.max_pending_buffers)
    with_queue_allocs = _measure_allocations(
        lambda: queued_allocs._io_proc(
            1, None, buffer_ptr, None, None, None, None
        ),
        iterations=2_000,
    )

    return {
        "no_queue": {
            "iterations": iterations,
            "elapsed_s": round(elapsed, 4),
            "us_per_callback": round((elapsed / iterations) * 1_000_000, 2),
            "callback_budget_ms_at_48k": round(callback_budget_ms, 3),
            "pct_of_budget": round(
                (((elapsed / iterations) * 1000) / callback_budget_ms) * 100, 3
            ),
            "per_call": no_queue_distribution,
            "allocations": no_queue_allocs,
        },
        "with_queue": {
            "iterations": queue_iterations,
            "elapsed_s": round(queue_elapsed, 4),
            "us_per_callback": round(
                (queue_elapsed / queue_iterations) * 1_000_000, 2
            ),
            "queued_items": queued_item_count,
            "per_call": with_queue_distribution,
            "allocations": with_queue_allocs,
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

    def _run_worker_once() -> None:
        recorder = AudioRecorder(1, temp_path)
        _configure_synthetic_recorder(recorder)
        recorder._start_worker()
        assert recorder._work_queue is not None
        # Pre-build one ctypes buffer and reuse the reference for every queue
        # item. The worker's ``_release_pool_buffer`` appends back to the pool;
        # since there's no io_proc consumer, the pool grows, but the underlying
        # storage is shared so we aren't allocating per item.
        shared_buf = _make_pool_buffer(data)
        byte_count = len(data)
        for _ in range(num_buffers):
            recorder._work_queue.put((shared_buf, frames, byte_count))
        recorder._stop_worker()

    try:
        usage = _measure_resource_usage(_run_worker_once)
        wav_size_bytes = (
            Path(temp_path).stat().st_size if os.path.exists(temp_path) else 0
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    elapsed = usage["wall_s"]
    input_megabytes = (len(data) * num_buffers) / (1024 * 1024)
    audio_seconds = (frames * num_buffers) / 48_000
    thread_results = {
        "buffers": num_buffers,
        "audio_s_equivalent": round(audio_seconds, 2),
        "elapsed_s": elapsed,
        "input_mb_s": round(input_megabytes / elapsed, 2) if elapsed > 0 else 0.0,
        "realtime_factor": round(audio_seconds / elapsed, 1) if elapsed > 0 else 0.0,
        "wav_size_mb": round(wav_size_bytes / (1024 * 1024), 2),
        "resource": usage,
    }

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
        shared_buf = _make_pool_buffer(data)
        byte_count = len(data)
        for _ in range(5_000):
            recorder._work_queue.put((shared_buf, frames, byte_count))
        recorder._work_queue.put(None)

        profile = cProfile.Profile()
        profile.enable()
        recorder._worker_loop()
        profile.disable()
        profile_output = _profile_summary(profile)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    fd, alloc_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    os.unlink(alloc_path)

    try:
        alloc_recorder = AudioRecorder(1, alloc_path)
        _configure_synthetic_recorder(alloc_recorder)
        alloc_recorder._output_file = Path(alloc_path).open("wb")  # noqa: SIM115
        alloc_recorder._wav_file = wave.open(alloc_recorder._output_file, "wb")  # noqa: SIM115
        alloc_recorder._wav_file.setnchannels(alloc_recorder._num_channels)
        alloc_recorder._wav_file.setsampwidth(
            alloc_recorder._output_bits_per_sample // 8
        )
        alloc_recorder._wav_file.setframerate(int(alloc_recorder._sample_rate))
        alloc_recorder._work_queue = queue.Queue()
        buffers_for_alloc = 2_000
        shared_buf = _make_pool_buffer(data)
        byte_count = len(data)
        for _ in range(buffers_for_alloc):
            alloc_recorder._work_queue.put((shared_buf, frames, byte_count))
        alloc_recorder._work_queue.put(None)

        def _drain_once() -> None:
            alloc_recorder._worker_loop()

        allocations = _measure_allocations(_drain_once, iterations=1)
        allocations["buffers_processed"] = buffers_for_alloc
        allocations["retained_bytes_per_buffer"] = round(
            allocations["retained_bytes"] / buffers_for_alloc, 3
        )
    finally:
        if os.path.exists(alloc_path):
            os.remove(alloc_path)

    return {
        "threaded_wav": thread_results,
        "allocations": allocations,
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
            "audio_converter": _profile_audio_converter_conversion(),
            "wav_write_paths": _profile_write_paths(),
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
