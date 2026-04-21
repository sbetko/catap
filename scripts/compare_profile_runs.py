#!/usr/bin/env python3
"""Compare two ``profile_catap.py`` JSON outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

WORKER_ROWS = (
    (
        "elapsed_s (lower is better)",
        ("synthetic", "worker_wav", "threaded_wav", "elapsed_s"),
        "lower",
    ),
    (
        "realtime_factor (higher better)",
        ("synthetic", "worker_wav", "threaded_wav", "realtime_factor"),
        "higher",
    ),
    (
        "cpu_utilization_pct (lower)",
        (
            "synthetic",
            "worker_wav",
            "threaded_wav",
            "resource",
            "cpu_utilization_pct",
        ),
        "lower",
    ),
)

SCENARIO_SPECS = {
    "streaming_light_realtime": (
        ("enqueue mean us", ("enqueue_duration_us", "mean_us"), "lower"),
        ("enqueue p95 us", ("enqueue_duration_us", "p95_us"), "lower"),
        ("enqueue p99 us", ("enqueue_duration_us", "p99_us"), "lower"),
        ("callback mean us", ("callback_start_latency_us", "mean_us"), "lower"),
        ("callback p95 us", ("callback_start_latency_us", "p95_us"), "lower"),
        ("callback p99 us", ("callback_start_latency_us", "p99_us"), "lower"),
        ("queue depth mean", ("queue_depth", "mean"), "lower"),
        ("queue depth p95", ("queue_depth", "p95"), "lower"),
        ("producer elapsed s", ("producer_elapsed_s",), "lower"),
    ),
    "streaming_busy_burst": (
        ("enqueue mean us", ("enqueue_duration_us", "mean_us"), "lower"),
        ("enqueue p95 us", ("enqueue_duration_us", "p95_us"), "lower"),
        ("enqueue p99 us", ("enqueue_duration_us", "p99_us"), "lower"),
        ("callback mean us", ("callback_start_latency_us", "mean_us"), "lower"),
        ("callback p95 us", ("callback_start_latency_us", "p95_us"), "lower"),
        ("callback p99 us", ("callback_start_latency_us", "p99_us"), "lower"),
        ("queue depth mean", ("queue_depth", "mean"), "lower"),
        ("queue depth p95", ("queue_depth", "p95"), "lower"),
        ("producer elapsed s", ("producer_elapsed_s",), "lower"),
    ),
    "wav_burst": (
        ("enqueue mean us", ("enqueue_duration_us", "mean_us"), "lower"),
        ("enqueue p95 us", ("enqueue_duration_us", "p95_us"), "lower"),
        ("enqueue p99 us", ("enqueue_duration_us", "p99_us"), "lower"),
        ("write mean us", ("write_start_latency_us", "mean_us"), "lower"),
        ("write p95 us", ("write_start_latency_us", "p95_us"), "lower"),
        ("write p99 us", ("write_start_latency_us", "p99_us"), "lower"),
        ("queue depth mean", ("queue_depth", "mean"), "lower"),
        ("queue depth p95", ("queue_depth", "p95"), "lower"),
        ("producer elapsed s", ("producer_elapsed_s",), "lower"),
    ),
    "streaming_and_wav_burst": (
        ("enqueue mean us", ("enqueue_duration_us", "mean_us"), "lower"),
        ("enqueue p95 us", ("enqueue_duration_us", "p95_us"), "lower"),
        ("enqueue p99 us", ("enqueue_duration_us", "p99_us"), "lower"),
        ("callback mean us", ("callback_start_latency_us", "mean_us"), "lower"),
        ("callback p95 us", ("callback_start_latency_us", "p95_us"), "lower"),
        ("callback p99 us", ("callback_start_latency_us", "p99_us"), "lower"),
        ("write mean us", ("write_start_latency_us", "mean_us"), "lower"),
        ("write p95 us", ("write_start_latency_us", "p95_us"), "lower"),
        ("write p99 us", ("write_start_latency_us", "p99_us"), "lower"),
        ("queue depth mean", ("queue_depth", "mean"), "lower"),
        ("queue depth p95", ("queue_depth", "p95"), "lower"),
        ("producer elapsed s", ("producer_elapsed_s",), "lower"),
    ),
}


def _get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = data
    for key in path:
        value = value[key]
    return value


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        absolute = abs(value)
        if absolute >= 100000:
            return f"{value:,.0f}"
        if absolute >= 1000:
            return f"{value:,.1f}"
        if absolute >= 100:
            return f"{value:,.2f}"
        return f"{value:,.3f}"
    return str(value)


def _fmt_delta(value: float | None) -> str:
    if value is None:
        return "-"
    absolute = abs(value)
    if absolute >= 100000:
        return f"{value:+,.0f}"
    if absolute >= 1000:
        return f"{value:+,.1f}"
    if absolute >= 100:
        return f"{value:+,.2f}"
    return f"{value:+,.3f}"


def _fmt_pct(main: Any, batch: Any) -> str:
    if not isinstance(main, (int, float)) or not isinstance(batch, (int, float)):
        return "n/a"
    if main == 0:
        return "n/a"
    pct = ((batch - main) / main) * 100.0
    return f"{pct:+.1f}%"


def _better(main: Any, batch: Any, direction: str) -> str:
    if not isinstance(main, (int, float)) or not isinstance(batch, (int, float)):
        return "-"
    if main == batch:
        return "="
    if direction == "lower":
        return "batch" if batch < main else "main"
    return "batch" if batch > main else "main"


def _render_table(title: str, rows: list[tuple[str, Any, Any, str]]) -> list[str]:
    lines = [
        title,
        "-" * len(title),
        f"{'Metric':<34} {'Main':>12} {'Batch':>12} {'Delta':>12} {'Delta %':>9} {'Better':>8}",
        "-" * 93,
    ]

    for label, main, batch, direction in rows:
        delta = (
            batch - main
            if isinstance(main, (int, float)) and isinstance(batch, (int, float))
            else None
        )
        lines.append(
            f"{label:<34} "
            f"{_fmt(main):>12} "
            f"{_fmt(batch):>12} "
            f"{_fmt_delta(delta):>12} "
            f"{_fmt_pct(main, batch):>9} "
            f"{_better(main, batch, direction):>8}"
        )

    return lines


def format_report(
    baseline_profile: dict[str, Any],
    candidate_profile: dict[str, Any],
) -> str:
    sections: list[str] = []

    worker_rows = [
        (label, _get(baseline_profile, path), _get(candidate_profile, path), direction)
        for label, path, direction in WORKER_ROWS
    ]
    sections.extend(_render_table("Worker WAV Throughput", worker_rows))

    for scenario, specs in SCENARIO_SPECS.items():
        baseline_scenario = baseline_profile["synthetic"]["worker_queue_latency"][
            scenario
        ]
        candidate_scenario = candidate_profile["synthetic"]["worker_queue_latency"][
            scenario
        ]
        rows = [
            (
                label,
                _get(baseline_scenario, path),
                _get(candidate_scenario, path),
                direction,
            )
            for label, path, direction in specs
        ]
        sections.append("")
        sections.extend(_render_table(f"Scenario: {scenario}", rows))

    sections.extend(
        [
            "",
            "Notes",
            "-----",
            "`Better=candidate` means the second JSON file won for that metric.",
            "For most rows lower is better; `realtime_factor` is the main higher-is-better metric.",
        ]
    )

    return "\n".join(sections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two profile_catap.py JSON result files."
    )
    parser.add_argument("baseline_profile", type=Path, help="Baseline JSON file")
    parser.add_argument("candidate_profile", type=Path, help="Candidate JSON file")
    args = parser.parse_args(argv)

    baseline_profile = json.loads(args.baseline_profile.read_text())
    candidate_profile = json.loads(args.candidate_profile.read_text())
    print(format_report(baseline_profile, candidate_profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
