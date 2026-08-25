"""Opt-in macOS integration smoke tests."""

from __future__ import annotations

import array
import math
import os
import platform
import subprocess
import time
import wave
from pathlib import Path

import pytest

RUN_INTEGRATION = os.getenv("CATAP_RUN_INTEGRATION") == "1"
RUN_TONE_INTEGRATION = os.getenv("CATAP_RUN_TONE_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only"),
]

_TONE_FREQUENCY_HZ = 1_000.0


def _write_tone_wav(
    path: Path,
    *,
    frequency: float,
    duration: float,
    sample_rate: int = 44_100,
    amplitude: float = 0.4,
) -> None:
    """Write a mono 16-bit sine tone for playback through ``afplay``."""
    frame_count = int(duration * sample_rate)
    scale = amplitude * 32_767.0
    samples = array.array(
        "h",
        (
            int(scale * math.sin(2.0 * math.pi * frequency * index / sample_rate))
            for index in range(frame_count)
        ),
    )
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())


def _read_mono_samples(path: Path) -> tuple[list[float], float]:
    """Read a captured 16-bit WAV as a mono float mixdown."""
    with wave.open(str(path), "rb") as wav_file:
        num_channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = float(wav_file.getframerate())
        frames = wav_file.readframes(wav_file.getnframes())

    if sample_width != 2:
        pytest.fail(f"expected 16-bit capture output, got {8 * sample_width}-bit")

    interleaved = array.array("h", frames)
    mono = [
        sum(interleaved[base : base + num_channels]) / (num_channels * 32_768.0)
        for base in range(0, len(interleaved), num_channels)
    ]
    return mono, sample_rate


def _tone_power_ratio(
    samples: list[float],
    sample_rate: float,
    frequency: float,
) -> float:
    """Return the fraction of signal energy at ``frequency`` (Goertzel).

    A pure sine yields ~0.5 (its mirror bin holds the other half); silence
    and broadband noise yield ~0.
    """
    total = len(samples)
    windowed = [
        sample * (0.5 - 0.5 * math.cos(2.0 * math.pi * index / (total - 1)))
        for index, sample in enumerate(samples)
    ]

    omega = 2.0 * math.pi * frequency / sample_rate
    coeff = 2.0 * math.cos(omega)
    s_prev = s_prev2 = 0.0
    for sample in windowed:
        s_prev2, s_prev = s_prev, sample + coeff * s_prev - s_prev2
    bin_power = s_prev * s_prev + s_prev2 * s_prev2 - coeff * s_prev * s_prev2

    total_energy = sum(sample * sample for sample in windowed)
    if total_energy <= 0.0:
        return 0.0
    return bin_power / (total * total_energy)


def test_list_audio_processes_smoke() -> None:
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    from catap import list_audio_processes

    processes = list_audio_processes()
    assert isinstance(processes, list)

    for process in processes[:5]:
        assert isinstance(process.audio_object_id, int)
        assert process.audio_object_id > 0
        assert isinstance(process.pid, int)
        assert process.pid >= 0
        assert isinstance(process.name, str)
        assert process.name
        assert isinstance(process.is_outputting, bool)


def test_list_audio_devices_smoke() -> None:
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    from catap import list_audio_devices

    devices = list_audio_devices()
    assert isinstance(devices, list)

    for device in devices[:5]:
        assert isinstance(device.audio_object_id, int)
        assert device.audio_object_id > 0
        assert isinstance(device.uid, str)
        assert device.uid
        assert isinstance(device.name, str)
        assert device.name
        assert isinstance(device.streams, tuple)


def test_record_system_audio_smoke(tmp_path) -> None:
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    from catap import record_system_audio

    output_path = tmp_path / "integration-recording.wav"
    session = record_system_audio(
        output_path=output_path,
        max_pending_buffers=64,
    )
    session.record_for(0.2)

    assert output_path.exists()
    assert session.stream_format is not None
    assert session.duration_seconds >= 0.0

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnchannels() > 0
        assert wav_file.getframerate() > 0
        assert wav_file.getsampwidth() > 0
        assert wav_file.getnframes() == session.frames_recorded


def test_record_known_tone_delivers_audio(tmp_path) -> None:
    """Play a known tone aloud and require the capture to contain it."""
    if not RUN_TONE_INTEGRATION:
        pytest.skip(
            "set CATAP_RUN_TONE_INTEGRATION=1 to run the audible tone capture test"
        )

    from catap import record_system_audio

    tone_path = tmp_path / "tone.wav"
    _write_tone_wav(tone_path, frequency=_TONE_FREQUENCY_HZ, duration=4.0)
    output_path = tmp_path / "tone-capture.wav"

    player = subprocess.Popen(["afplay", str(tone_path)])
    try:
        # Give afplay time to open the output device and start rendering.
        time.sleep(1.0)
        session = record_system_audio(output_path=output_path)
        session.record_for(1.5)
    finally:
        player.terminate()
        player.wait()

    assert session.stream_format is not None
    sample_rate = session.stream_format.sample_rate
    assert session.frames_recorded >= sample_rate, (
        f"captured only {session.frames_recorded} frames at {sample_rate:.0f} Hz; "
        "expected at least one second of audio"
    )

    assert session.captured_only_silence is False, (
        "session.captured_only_silence should be false after a real capture"
    )

    samples, capture_rate = _read_mono_samples(output_path)
    peak = max((abs(sample) for sample in samples), default=0.0)
    assert peak > 0.0, (
        "capture delivered frames but every sample is zero. macOS zeroes tap "
        "audio when the recording process lacks system-audio permission; "
        "grant System Audio Recording to the app hosting this test run."
    )

    ratio = _tone_power_ratio(samples, capture_rate, _TONE_FREQUENCY_HZ)
    assert ratio > 0.1, (
        f"{_TONE_FREQUENCY_HZ:.0f} Hz tone holds only {ratio:.3f} of captured "
        "energy; expected a dominant tone. Is the test tone audible on the "
        "default output device?"
    )
