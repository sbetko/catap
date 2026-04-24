"""Shared helper-tone primitives for catap development scripts."""

from __future__ import annotations

import ctypes
import math
import struct
import warnings
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

SampleFn = Callable[[float], float]

_TAU = 2.0 * math.pi

_OBJC: Any = None
_AVFOUNDATION_BUNDLE_LOADED = False
_AVAudioEngine: Any = None
_AVAudioFormat: Any = None
_AVAudioPCMBuffer: Any = None
_AVAudioPlayerNode: Any = None


def fade_gain(frame_index: int, total_frames: int, sample_rate: int) -> float:
    """Return a short edge fade to avoid clicks at buffer boundaries."""
    fade_frames = max(1, min(int(sample_rate * 0.02), total_frames // 2 or 1))
    fade_in = min(1.0, (frame_index + 1) / fade_frames)
    fade_out = min(1.0, (total_frames - frame_index) / fade_frames)
    return min(fade_in, fade_out)


def pure_sine_sample(phase: float) -> float:
    """Return a pure sine-wave sample for the provided phase."""
    return math.sin(phase)


def pleasant_tone_sample(phase: float) -> float:
    """Return a slightly richer helper tone with gentle harmonics."""
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
    sample_fn: SampleFn = pleasant_tone_sample,
    apply_fade: bool = True,
) -> Path:
    """Write a deterministic stereo tone WAV for helper playback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total_frames = max(1, int(seconds * sample_rate))
    chunk_frames = 4096
    amplitude_i16 = max(0, min(int(amplitude * 32767), 32767))
    phase_step = _TAU * frequency_hz / sample_rate
    phase = 0.0
    frames_remaining = total_frames
    frame_index = 0

    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)

        while frames_remaining > 0:
            frame_count = min(chunk_frames, frames_remaining)
            chunk = bytearray()
            for _ in range(frame_count):
                gain = (
                    fade_gain(frame_index, total_frames, sample_rate)
                    if apply_fade
                    else 1.0
                )
                sample = int(amplitude_i16 * gain * sample_fn(phase))
                chunk.extend(struct.pack("<" + "h" * channels, *([sample] * channels)))
                phase += phase_step
                if phase >= _TAU:
                    phase -= _TAU
                frame_index += 1
            wav_file.writeframes(chunk)
            frames_remaining -= frame_count

    return path


def _load_avfoundation() -> None:
    """Load the AVFoundation classes used by the helper tone player."""
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
    objc.loadBundle(  # ty: ignore[unresolved-attribute]
        "AVFoundation",
        globals(),
        bundle_path=objc.pathForFramework(
            "/System/Library/Frameworks/AVFoundation.framework"
        ),
    )
    _AVAudioEngine = objc.lookUpClass("AVAudioEngine")  # ty: ignore[unresolved-attribute]
    _AVAudioFormat = objc.lookUpClass("AVAudioFormat")  # ty: ignore[unresolved-attribute]
    _AVAudioPCMBuffer = objc.lookUpClass("AVAudioPCMBuffer")  # ty: ignore[unresolved-attribute]
    _AVAudioPlayerNode = objc.lookUpClass("AVAudioPlayerNode")  # ty: ignore[unresolved-attribute]
    _AVFOUNDATION_BUNDLE_LOADED = True


class TonePlayer:
    """Simple AVAudioEngine-backed tone player for helper scripts."""

    def __init__(
        self,
        *,
        duration_seconds: float,
        sample_rate: float = 44_100.0,
        channels: int = 2,
        frequency_hz: float = 440.0,
        amplitude: float = 0.08,
        device_id: int | None = None,
        sample_fn: SampleFn = pure_sine_sample,
        apply_fade: bool = False,
        channel_gains: Sequence[float] | None = None,
    ) -> None:
        _load_avfoundation()

        self._duration_seconds = duration_seconds
        self._sample_rate = sample_rate
        self._channels = channels
        self._frequency_hz = frequency_hz
        self._amplitude = amplitude
        self._sample_fn = sample_fn
        self._apply_fade = apply_fade
        if channel_gains is None:
            self._channel_gains = tuple(1.0 for _ in range(channels))
        elif len(channel_gains) != channels:
            raise ValueError("channel_gains length must match channels")
        else:
            self._channel_gains = tuple(float(gain) for gain in channel_gains)

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
        assert _OBJC is not None
        warnings.filterwarnings("ignore", category=_OBJC.ObjCPointerWarning)
        channel_ptrs = ctypes.cast(
            self._buffer.floatChannelData().pointerAsInteger,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),
        )
        phase_step = _TAU * self._frequency_hz / self._sample_rate

        for channel_index in range(self._channels):
            channel = ctypes.cast(
                channel_ptrs[channel_index],
                ctypes.POINTER(ctypes.c_float * frame_count),
            ).contents
            phase = 0.0
            for frame_index in range(frame_count):
                gain = (
                    fade_gain(frame_index, frame_count, int(self._sample_rate))
                    if self._apply_fade
                    else 1.0
                )
                channel[frame_index] = (
                    self._amplitude
                    * self._channel_gains[channel_index]
                    * gain
                    * self._sample_fn(phase)
                )
                phase += phase_step
                if phase >= _TAU:
                    phase -= _TAU

    def start(self) -> None:
        ok = self._engine.startAndReturnError_(None)
        if not ok:
            raise OSError("AVAudioEngine failed to start")
        self._player.scheduleBuffer_completionHandler_(self._buffer, None)
        self._player.play()

    def stop(self) -> None:
        self._player.stop()
        self._engine.stop()
