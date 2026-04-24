"""Regression tests for the headless tone analyzer."""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

import pytest

import catap._devtools.tone_analyzer as tone_analyzer


def _write_stereo_sine(
    path: Path,
    *,
    frequency_hz: float,
    left_amplitude: float,
    right_amplitude: float,
    seconds: float = 0.25,
    sample_rate: int = 8_000,
) -> None:
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)

        payload = bytearray()
        for frame_index in range(frames):
            phase = 2.0 * math.pi * frequency_hz * frame_index / sample_rate
            left = int(left_amplitude * math.sin(phase) * 32767)
            right = int(right_amplitude * math.sin(phase) * 32767)
            payload.extend(struct.pack("<hh", left, right))
        wav_file.writeframes(payload)


def test_analyze_wav_detects_expected_left_channel_tone(tmp_path: Path) -> None:
    recording = tmp_path / "recording.wav"
    _write_stereo_sine(
        recording,
        frequency_hz=431.0,
        left_amplitude=0.08,
        right_amplitude=0.0,
    )

    analysis = tone_analyzer.analyze_wav(
        recording,
        [tone_analyzer.ToneTarget("left-id", 431.0, "left")],
        min_dbfs=-50,
    )

    assert analysis["summary"]["ok"] is True
    assert analysis["summary"]["present"] == 1

    tone = analysis["tones"][0]
    assert tone["present"] is True
    assert tone["channels"][0]["present"] is True
    assert tone["channels"][1]["present"] is False
    assert tone["max_amplitude"] == pytest.approx(0.08, rel=0.05)


def test_analyze_wav_reports_missing_and_unexpected_channel(
    tmp_path: Path,
) -> None:
    recording = tmp_path / "recording.wav"
    _write_stereo_sine(
        recording,
        frequency_hz=431.0,
        left_amplitude=0.08,
        right_amplitude=0.0,
    )

    analysis = tone_analyzer.analyze_wav(
        recording,
        [tone_analyzer.ToneTarget("right-id", 431.0, "right")],
        min_dbfs=-50,
    )

    assert analysis["summary"]["ok"] is False
    assert analysis["summary"]["missing"] == ["right-id"]
    assert analysis["summary"]["unexpected_channels"] == {"right-id": [0]}


def test_main_uses_manifest_and_returns_failure_for_missing_tone(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recording = tmp_path / "recording.wav"
    manifest = tmp_path / "manifest.json"
    _write_stereo_sine(
        recording,
        frequency_hz=431.0,
        left_amplitude=0.08,
        right_amplitude=0.0,
    )
    manifest.write_text(
        json.dumps(
            {
                "schema": "catap-tone-farm/v1",
                "tones": [
                    {
                        "id": "missing",
                        "frequency_hz": 719.0,
                        "channel_mode": "all",
                    }
                ],
            }
        )
    )

    exit_code = tone_analyzer.main(
        [str(recording), "--manifest", str(manifest), "--json"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    output = json.loads(captured.out)
    assert output["summary"]["missing"] == ["missing"]
