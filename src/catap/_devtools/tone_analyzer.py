"""Analyze recorded WAV files for tone-farm signal identities."""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToneTarget:
    """One expected tone in a recording."""

    tone_id: str
    frequency_hz: float
    channel_mode: str = "all"


@dataclass
class GoertzelState:
    """Streaming exact-frequency detector state."""

    coefficient: float
    previous: float = 0.0
    previous2: float = 0.0

    def update(self, sample: float) -> None:
        current = sample + (self.coefficient * self.previous) - self.previous2
        self.previous2 = self.previous
        self.previous = current

    def amplitude(self, frames: int) -> float:
        if frames <= 0:
            return 0.0
        power = (
            (self.previous2 * self.previous2)
            + (self.previous * self.previous)
            - (self.coefficient * self.previous * self.previous2)
        )
        return (2.0 * math.sqrt(max(power, 0.0))) / frames


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


def _dbfs(amplitude: float) -> float | None:
    if amplitude <= 0.0:
        return None
    return 20.0 * math.log10(amplitude)


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


def _targets_from_manifest(path: Path) -> list[ToneTarget]:
    manifest = json.loads(path.read_text())
    tones = manifest.get("tones")
    if not isinstance(tones, list):
        raise ValueError("manifest does not contain a tones list")

    targets = []
    for index, tone in enumerate(tones):
        if not isinstance(tone, dict):
            raise ValueError(f"manifest tone {index} is not an object")
        targets.append(
            ToneTarget(
                tone_id=str(tone.get("id", f"tone-{index + 1:03d}")),
                frequency_hz=float(tone["frequency_hz"]),
                channel_mode=str(tone.get("channel_mode", "all")),
            )
        )
    return targets


def _targets_from_frequencies(frequencies: list[float]) -> list[ToneTarget]:
    return [
        ToneTarget(tone_id=f"tone-{index + 1:03d}", frequency_hz=frequency)
        for index, frequency in enumerate(frequencies)
    ]


def _expected_channels(channels: int, mode: str) -> set[int]:
    if mode == "left":
        return {0}
    if mode == "right":
        return {1} if channels >= 2 else set()
    return set(range(channels))


def _decode_samples(raw: bytes, sample_width: int) -> list[float]:
    if sample_width == 1:
        return [(sample - 128) / 128.0 for sample in raw]

    if sample_width == 2:
        count = len(raw) // 2
        return [
            sample / 32768.0
            for sample in struct.unpack(f"<{count}h", raw)
        ]

    if sample_width == 3:
        samples = []
        for offset in range(0, len(raw), 3):
            value = int.from_bytes(raw[offset : offset + 3], "little", signed=False)
            if value & 0x800000:
                value -= 0x1000000
            samples.append(value / 8_388_608.0)
        return samples

    if sample_width == 4:
        count = len(raw) // 4
        return [
            sample / 2_147_483_648.0
            for sample in struct.unpack(f"<{count}i", raw)
        ]

    raise ValueError(f"unsupported WAV sample width: {sample_width} bytes")


def analyze_wav(
    path: Path,
    targets: list[ToneTarget],
    *,
    start_seconds: float = 0.0,
    duration_seconds: float | None = None,
    min_dbfs: float = -60.0,
) -> dict[str, Any]:
    """Return a JSON-serializable tone analysis for a PCM WAV file."""
    if not targets:
        raise ValueError("at least one target frequency is required")

    with wave.open(str(path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        total_frames = wav_file.getnframes()

        start_frame = min(total_frames, int(start_seconds * sample_rate))
        if duration_seconds is None:
            frames_to_analyze = total_frames - start_frame
        else:
            frames_to_analyze = min(
                total_frames - start_frame,
                int(duration_seconds * sample_rate),
            )

        states = [
            [
                GoertzelState(
                    coefficient=2.0
                    * math.cos(2.0 * math.pi * target.frequency_hz / sample_rate)
                )
                for _channel_index in range(channels)
            ]
            for target in targets
        ]

        wav_file.setpos(start_frame)
        frames_remaining = frames_to_analyze
        frames_read = 0
        chunk_frames = 4096
        channel_energy = [0.0 for _channel_index in range(channels)]

        while frames_remaining > 0:
            frame_count = min(chunk_frames, frames_remaining)
            raw = wav_file.readframes(frame_count)
            if not raw:
                break

            samples = _decode_samples(raw, sample_width)
            actual_frames = len(samples) // channels
            for frame_index in range(actual_frames):
                frame_start = frame_index * channels
                for channel_index in range(channels):
                    sample = samples[frame_start + channel_index]
                    channel_energy[channel_index] += sample * sample
                    for target_states in states:
                        target_states[channel_index].update(sample)

            frames_read += actual_frames
            frames_remaining -= actual_frames

    threshold_amplitude = 10.0 ** (min_dbfs / 20.0)
    tone_results = []
    missing = []
    unexpected_channels: dict[str, list[int]] = {}

    for target, target_states in zip(targets, states, strict=True):
        channel_results = []
        expected = _expected_channels(channels, target.channel_mode)

        for channel_index, state in enumerate(target_states):
            amplitude = state.amplitude(frames_read)
            dbfs = _dbfs(amplitude)
            present = amplitude >= threshold_amplitude
            channel_results.append(
                {
                    "index": channel_index,
                    "amplitude": amplitude,
                    "dbfs": dbfs,
                    "present": present,
                    "expected": channel_index in expected,
                }
            )

        expected_present = any(
            result["present"] for result in channel_results if result["expected"]
        )
        unexpected = [
            result["index"]
            for result in channel_results
            if result["present"] and not result["expected"]
        ]
        if not expected_present:
            missing.append(target.tone_id)
        if unexpected:
            unexpected_channels[target.tone_id] = unexpected

        max_amplitude = max(
            (float(result["amplitude"]) for result in channel_results),
            default=0.0,
        )
        tone_results.append(
            {
                "id": target.tone_id,
                "frequency_hz": target.frequency_hz,
                "channel_mode": target.channel_mode,
                "present": expected_present,
                "unexpected_channels": unexpected,
                "max_amplitude": max_amplitude,
                "max_dbfs": _dbfs(max_amplitude),
                "channels": channel_results,
            }
        )

    channel_rms = [
        math.sqrt(energy / frames_read) if frames_read else 0.0
        for energy in channel_energy
    ]
    ok = not missing and not unexpected_channels

    return {
        "schema": "catap-tone-analysis/v1",
        "input": str(path),
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "start_seconds": start_seconds,
        "duration_seconds": frames_read / sample_rate if sample_rate else 0.0,
        "frames_analyzed": frames_read,
        "min_dbfs": min_dbfs,
        "channel_rms": channel_rms,
        "tones": tone_results,
        "summary": {
            "ok": ok,
            "expected": len(targets),
            "present": len(targets) - len(missing),
            "missing": missing,
            "unexpected_channels": unexpected_channels,
        },
    }


def _print_human(analysis: dict[str, Any]) -> None:
    summary = analysis["summary"]
    status = "ok" if summary["ok"] else "failed"
    print(
        f"{status}: {summary['present']}/{summary['expected']} tones present "
        f"in {analysis['duration_seconds']:.3f}s"
    )
    for tone in analysis["tones"]:
        dbfs = tone["max_dbfs"]
        dbfs_text = "-inf" if dbfs is None else f"{dbfs:.1f}"
        marker = "present" if tone["present"] else "missing"
        print(
            f"{tone['id']}: {tone['frequency_hz']:.3f} Hz {marker}, "
            f"max {dbfs_text} dBFS"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Recorded PCM WAV file to analyze.")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Tone farm JSON manifest to use as the expected signal map.",
    )
    parser.add_argument(
        "--frequencies",
        help="Comma-separated target frequencies in Hz when no manifest is provided.",
    )
    parser.add_argument(
        "--start-seconds",
        type=_non_negative_float,
        default=0.0,
        help="Offset into the WAV before analysis starts (default: 0).",
    )
    parser.add_argument(
        "--duration-seconds",
        type=_positive_float,
        help="Maximum duration to analyze.",
    )
    parser.add_argument(
        "--min-dbfs",
        type=float,
        default=-60.0,
        help=(
            "Minimum matched-filter amplitude in dBFS to count present "
            "(default: -60)."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full machine-readable analysis JSON.",
    )
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Return exit code 0 even when expected tones are missing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.manifest is not None:
            targets = _targets_from_manifest(args.manifest)
        else:
            targets = _targets_from_frequencies(_parse_frequencies(args.frequencies))
        analysis = analyze_wav(
            args.input,
            targets,
            start_seconds=args.start_seconds,
            duration_seconds=args.duration_seconds,
            min_dbfs=args.min_dbfs,
        )
    except Exception as exc:
        print(f"tone_analyzer: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(analysis, indent=2, sort_keys=True))
    else:
        _print_human(analysis)

    if args.no_fail or analysis["summary"]["ok"]:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
