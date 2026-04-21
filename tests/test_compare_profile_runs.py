"""Regression tests for the profile comparison helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_compare_script():
    script_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "compare_profile_runs.py"
    )
    spec = importlib.util.spec_from_file_location(
        "compare_profile_runs_script", script_path
    )
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scenario(
    *,
    enqueue_mean: float,
    enqueue_p95: float,
    enqueue_p99: float,
    callback_mean: float = 0.0,
    callback_p95: float = 0.0,
    callback_p99: float = 0.0,
    write_mean: float = 0.0,
    write_p95: float = 0.0,
    write_p99: float = 0.0,
    depth_mean: float = 1.0,
    depth_p95: float = 1.0,
    producer_elapsed_s: float = 0.1,
) -> dict[str, object]:
    return {
        "enqueue_duration_us": {
            "mean_us": enqueue_mean,
            "p95_us": enqueue_p95,
            "p99_us": enqueue_p99,
        },
        "callback_start_latency_us": {
            "mean_us": callback_mean,
            "p95_us": callback_p95,
            "p99_us": callback_p99,
        },
        "write_start_latency_us": {
            "mean_us": write_mean,
            "p95_us": write_p95,
            "p99_us": write_p99,
        },
        "queue_depth": {"mean": depth_mean, "p95": depth_p95},
        "producer_elapsed_s": producer_elapsed_s,
    }


def test_format_report_renders_aligned_tables_and_percent_deltas() -> None:
    compare_script = _load_compare_script()

    baseline_profile = {
        "synthetic": {
            "worker_wav": {
                "threaded_wav": {
                    "elapsed_s": 0.08,
                    "realtime_factor": 1600.0,
                    "resource": {"cpu_utilization_pct": 120.0},
                }
            },
            "worker_queue_latency": {
                "streaming_light_realtime": _scenario(
                    enqueue_mean=20.0,
                    enqueue_p95=30.0,
                    enqueue_p99=40.0,
                    callback_mean=28.0,
                    callback_p95=50.0,
                    callback_p99=100.0,
                ),
                "streaming_busy_burst": _scenario(
                    enqueue_mean=1.0,
                    enqueue_p95=1.5,
                    enqueue_p99=2.0,
                    callback_mean=80_000.0,
                    callback_p95=150_000.0,
                    callback_p99=160_000.0,
                    depth_mean=750.5,
                    depth_p95=1425.0,
                    producer_elapsed_s=0.002,
                ),
                "wav_burst": _scenario(
                    enqueue_mean=0.5,
                    enqueue_p95=0.7,
                    enqueue_p99=0.9,
                    write_mean=6000.0,
                    write_p95=9000.0,
                    write_p99=9500.0,
                    depth_mean=1500.5,
                    depth_p95=2850.0,
                    producer_elapsed_s=0.003,
                ),
                "streaming_and_wav_burst": _scenario(
                    enqueue_mean=0.5,
                    enqueue_p95=0.7,
                    enqueue_p99=0.9,
                    callback_mean=84_000.0,
                    callback_p95=157_000.0,
                    callback_p99=164_000.0,
                    write_mean=84_000.0,
                    write_p95=157_000.0,
                    write_p99=164_000.0,
                    depth_mean=1500.5,
                    depth_p95=2850.0,
                    producer_elapsed_s=0.003,
                ),
            },
        }
    }
    candidate_profile = {
        "synthetic": {
            "worker_wav": {
                "threaded_wav": {
                    "elapsed_s": 0.06,
                    "realtime_factor": 2200.0,
                    "resource": {"cpu_utilization_pct": 100.0},
                }
            },
            "worker_queue_latency": {
                "streaming_light_realtime": _scenario(
                    enqueue_mean=19.0,
                    enqueue_p95=31.0,
                    enqueue_p99=42.0,
                    callback_mean=30.0,
                    callback_p95=60.0,
                    callback_p99=140.0,
                ),
                "streaming_busy_burst": _scenario(
                    enqueue_mean=1.2,
                    enqueue_p95=1.6,
                    enqueue_p99=2.1,
                    callback_mean=79_000.0,
                    callback_p95=149_000.0,
                    callback_p99=159_000.0,
                    depth_mean=750.5,
                    depth_p95=1425.0,
                    producer_elapsed_s=0.002,
                ),
                "wav_burst": _scenario(
                    enqueue_mean=0.45,
                    enqueue_p95=0.6,
                    enqueue_p99=0.8,
                    write_mean=5800.0,
                    write_p95=8800.0,
                    write_p99=9200.0,
                    depth_mean=1500.5,
                    depth_p95=2850.0,
                    producer_elapsed_s=0.002,
                ),
                "streaming_and_wav_burst": _scenario(
                    enqueue_mean=0.45,
                    enqueue_p95=0.6,
                    enqueue_p99=0.8,
                    callback_mean=83_900.0,
                    callback_p95=156_900.0,
                    callback_p99=163_900.0,
                    write_mean=83_900.0,
                    write_p95=156_900.0,
                    write_p99=163_900.0,
                    depth_mean=1500.5,
                    depth_p95=2850.0,
                    producer_elapsed_s=0.002,
                ),
            },
        }
    }

    report = compare_script.format_report(baseline_profile, candidate_profile)

    assert "Worker WAV Throughput" in report
    assert "Scenario: streaming_light_realtime" in report
    assert "Scenario: wav_burst" in report
    assert "Delta %" in report
    assert "Better" in report
    assert "candidate" in report
    assert "-25.0%" in report
    assert "+37.5%" in report
