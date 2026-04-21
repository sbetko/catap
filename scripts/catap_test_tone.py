#!/usr/bin/env python3
"""Generate and optionally play a deterministic helper tone for catap tests."""

from __future__ import annotations

import argparse
import ctypes
import math
import signal
import struct
import sys
import tempfile
import time
import warnings
import wave
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

_OBJC: Any = None
_AVFOUNDATION_BUNDLE_LOADED = False
_AVAudioEngine: Any = None
_AVAudioFormat: Any = None
_AVAudioPCMBuffer: Any = None
_AVAudioPlayerNode: Any = None


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


def _fade_gain(frame_index: int, total_frames: int, sample_rate: int) -> float:
    fade_frames = max(1, min(int(sample_rate * 0.02), total_frames // 2 or 1))
    fade_in = min(1.0, (frame_index + 1) / fade_frames)
    fade_out = min(1.0, (total_frames - frame_index) / fade_frames)
    return min(fade_in, fade_out)


def _pleasant_tone_sample(phase: float) -> float:
    return (
        0.72 * math.sin(phase)
        + 0.20 * math.sin(phase * 2.0)
        + 0.08 * math.sin(phase * 3.0)
    )


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
        frame_index = 0
        frames_remaining = total_frames

        while frames_remaining > 0:
            frame_count = min(chunk_frames, frames_remaining)
            chunk = bytearray()
            for _ in range(frame_count):
                gain = _fade_gain(frame_index, total_frames, sample_rate)
                sample = int(
                    amplitude_i16 * gain * _pleasant_tone_sample(phase)
                )
                chunk.extend(struct.pack("<" + "h" * channels, *([sample] * channels)))
                phase += phase_step
                if phase > 2.0 * math.pi:
                    phase -= 2.0 * math.pi
                frame_index += 1
            wav_file.writeframes(chunk)
            frames_remaining -= frame_count

    return path


def _load_avfoundation() -> None:
    global _OBJC
    global _AVFOUNDATION_BUNDLE_LOADED
    global _AVAudioEngine
    global _AVAudioFormat
    global _AVAudioPCMBuffer
    global _AVAudioPlayerNode

    if _AVFOUNDATION_BUNDLE_LOADED:
        return

    import objc

    _OBJC = objc
    objc.loadBundle(
        "AVFoundation",
        globals(),
        bundle_path=objc.pathForFramework(
            "/System/Library/Frameworks/AVFoundation.framework"
        ),
    )
    _AVAudioEngine = objc.lookUpClass("AVAudioEngine")
    _AVAudioFormat = objc.lookUpClass("AVAudioFormat")
    _AVAudioPCMBuffer = objc.lookUpClass("AVAudioPCMBuffer")
    _AVAudioPlayerNode = objc.lookUpClass("AVAudioPlayerNode")
    _AVFOUNDATION_BUNDLE_LOADED = True


def _resolve_output_device(device_uid: str | None) -> tuple[int | None, str]:
    if not device_uid:
        return None, "Default Output"

    from catap import list_audio_devices

    for device in list_audio_devices():
        if device.uid == device_uid and device.output_streams:
            return device.audio_object_id, device.name

    raise LookupError(f"No output-capable device matches UID '{device_uid}'")


class TonePlayer:
    """Simple in-process mellow tone generator backed by AVAudioEngine."""

    def __init__(
        self,
        *,
        duration_seconds: float,
        sample_rate: float,
        channels: int,
        frequency_hz: float,
        amplitude: float,
        device_id: int | None,
    ) -> None:
        _load_avfoundation()

        self._duration_seconds = duration_seconds
        self._sample_rate = sample_rate
        self._channels = channels
        self._frequency_hz = frequency_hz
        self._amplitude = amplitude

        self._engine = _AVAudioEngine.alloc().init()
        self._player = _AVAudioPlayerNode.alloc().init()
        self._engine.attachNode_(self._player)

        self._format = (
            _AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(
                self._sample_rate,
                self._channels,
            )
        )
        self._engine.connect_to_format_(
            self._player,
            self._engine.mainMixerNode(),
            self._format,
        )

        if device_id is not None:
            ok = self._engine.outputNode().AUAudioUnit().setDeviceID_error_(
                device_id,
                None,
            )
            if not ok:
                raise OSError(f"Failed to set helper tone output device {device_id}")

        frame_count = max(1, int(self._sample_rate * self._duration_seconds))
        self._buffer = _AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
            self._format,
            frame_count,
        )
        self._buffer.setFrameLength_(frame_count)
        self._fill_buffer(frame_count)

    @property
    def is_playing(self) -> bool:
        return bool(self._player.isPlaying())

    def _fill_buffer(self, frame_count: int) -> None:
        warnings.filterwarnings("ignore", category=_OBJC.ObjCPointerWarning)
        channel_ptrs = ctypes.cast(
            self._buffer.floatChannelData().pointerAsInteger,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),
        )
        phase_step = 2.0 * math.pi * self._frequency_hz / self._sample_rate

        for channel_index in range(self._channels):
            channel = ctypes.cast(
                channel_ptrs[channel_index],
                ctypes.POINTER(ctypes.c_float * frame_count),
            ).contents
            phase = 0.0
            for frame_index in range(frame_count):
                gain = _fade_gain(
                    frame_index,
                    frame_count,
                    int(self._sample_rate),
                )
                channel[frame_index] = (
                    self._amplitude * gain * _pleasant_tone_sample(phase)
                )
                phase += phase_step
                if phase > 2.0 * math.pi:
                    phase -= 2.0 * math.pi

    def start(self) -> None:
        ok = self._engine.startAndReturnError_(None)
        if not ok:
            raise OSError("AVAudioEngine failed to start")
        self._player.scheduleBuffer_completionHandler_(self._buffer, None)
        self._player.play()

    def stop(self) -> None:
        self._player.stop()
        self._engine.stop()


def _build_parser() -> argparse.ArgumentParser:
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
