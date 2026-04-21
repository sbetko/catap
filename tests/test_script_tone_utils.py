"""Regression tests for packaged helper-tone utilities."""

from __future__ import annotations

import wave
from pathlib import Path

from catap._devtools.tone import write_tone_wav


def test_write_tone_wav_writes_expected_audio_file(tmp_path: Path) -> None:
    output_path = tmp_path / "helper-tone.wav"

    result = write_tone_wav(
        output_path,
        seconds=0.1,
        frequency_hz=220.0,
        sample_rate=8_000,
        channels=2,
        amplitude=0.12,
    )

    assert result == output_path
    assert output_path.exists()

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnchannels() == 2
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 8_000
        assert wav_file.getnframes() == 800
        assert any(byte != 0 for byte in wav_file.readframes(800))
