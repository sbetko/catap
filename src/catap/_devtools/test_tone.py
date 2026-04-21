"""Generate and optionally play a deterministic helper tone for catap tests."""

from __future__ import annotations

import argparse
import signal
import tempfile
import time
from pathlib import Path

from catap import list_audio_devices
from catap._devtools.tone import TonePlayer, pleasant_tone_sample, write_tone_wav


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


def _resolve_output_device(device_uid: str | None) -> tuple[int | None, str]:
    if not device_uid:
        return None, "Default Output"

    for device in list_audio_devices():
        if device.uid == device_uid and device.output_streams:
            return device.audio_object_id, device.name

    raise LookupError(f"No output-capable device matches UID '{device_uid}'")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a predictable mellow helper tone and optionally play it "
            "through a selected output device so catap always has a known "
            "audio source."
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
        default=220.0,
        help="Tone fundamental in Hz (default: 220)",
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
        default=0.12,
        help="Linear amplitude between 0 and 1 (default: 0.12)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_default_output_path(),
        help="Where to write the helper WAV file",
    )
    parser.add_argument(
        "--device-uid",
        help="Output device UID to use for playback; defaults to the system output",
    )
    parser.add_argument(
        "--write-only",
        action="store_true",
        help="Generate the WAV but do not launch playback",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

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

    device_id, device_name = _resolve_output_device(args.device_uid)
    print(f"Playing helper tone on {device_name}", flush=True)

    stop_requested = False

    def _request_stop(_signum: int, _frame: object | None) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    player = TonePlayer(
        duration_seconds=args.seconds,
        sample_rate=float(args.sample_rate),
        channels=args.channels,
        frequency_hz=args.frequency,
        amplitude=args.amplitude,
        device_id=device_id,
        sample_fn=pleasant_tone_sample,
        apply_fade=True,
    )
    player.start()

    deadline = time.monotonic() + args.seconds + 0.2
    try:
        while not stop_requested and time.monotonic() < deadline and player.is_playing:
            time.sleep(0.05)
    finally:
        player.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
