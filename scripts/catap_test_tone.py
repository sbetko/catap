#!/usr/bin/env python3
"""Generate and optionally play a deterministic helper tone for catap tests."""

from __future__ import annotations

import argparse
import math
import os
import shutil
import struct
import tempfile
import wave
from pathlib import Path


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _default_output_path() -> Path:
    return Path(tempfile.gettempdir()) / "catap-test-tone.wav"


def write_tone_wav(
    path: Path,
    *,
    seconds: float,
    frequency_hz: float,
    sample_rate: int,
    channels: int,
    amplitude: float,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    total_frames = max(1, int(seconds * sample_rate))
    chunk_frames = 4096
    amplitude_i16 = max(0, min(int(amplitude * 32767), 32767))

    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)

        phase = 0.0
        phase_step = 2.0 * math.pi * frequency_hz / sample_rate

        frames_remaining = total_frames
        while frames_remaining > 0:
            frame_count = min(chunk_frames, frames_remaining)
            chunk = bytearray()
            for _ in range(frame_count):
                sample = int(amplitude_i16 * math.sin(phase))
                chunk.extend(struct.pack("<" + "h" * channels, *([sample] * channels)))
                phase += phase_step
                if phase > 2.0 * math.pi:
                    phase -= 2.0 * math.pi
            wav_file.writeframes(chunk)
            frames_remaining -= frame_count

    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a predictable sine-wave helper tone and optionally play "
            "it via afplay so catap always has a known audio source."
        )
    )
    parser.add_argument(
        "--seconds",
        type=_positive_float,
        default=60.0,
        help="Tone length in seconds (default: 60)",
    )
    parser.add_argument(
        "--frequency",
        type=_positive_float,
        default=440.0,
        help="Sine-wave frequency in Hz (default: 440)",
    )
    parser.add_argument(
        "--sample-rate",
        type=_positive_int,
        default=44_100,
        help="Sample rate for the generated WAV (default: 44100)",
    )
    parser.add_argument(
        "--channels",
        type=_positive_int,
        default=2,
        help="Channel count for the generated WAV (default: 2)",
    )
    parser.add_argument(
        "--amplitude",
        type=_positive_float,
        default=0.18,
        help="Linear amplitude between 0 and 1 (default: 0.18)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_default_output_path(),
        help="Where to write the helper WAV file",
    )
    parser.add_argument(
        "--write-only",
        action="store_true",
        help="Generate the WAV but do not launch afplay",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.amplitude > 1.0:
        parser.error("--amplitude must be between 0 and 1")

    output_path = write_tone_wav(
        args.output,
        seconds=args.seconds,
        frequency_hz=args.frequency,
        sample_rate=args.sample_rate,
        channels=args.channels,
        amplitude=args.amplitude,
    )
    print(f"Wrote helper tone: {output_path}", flush=True)

    if args.write_only:
        return 0

    afplay_path = shutil.which("afplay")
    if afplay_path is None:
        parser.error("afplay was not found on PATH")

    print(f"Launching afplay for {output_path}", flush=True)
    os.execv(afplay_path, [afplay_path, str(output_path)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
