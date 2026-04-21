"""Regression tests for the helper-tone devtools script."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

import catap._devtools.test_tone as test_tone


@dataclass
class _FakeDevice:
    uid: str
    name: str
    audio_object_id: int
    output_streams: tuple[object, ...]


class _FakePlayer:
    instances: ClassVar[list[_FakePlayer]] = []

    def __init__(
        self,
        *,
        duration_seconds: float,
        sample_rate: float,
        channels: int,
        frequency_hz: float,
        amplitude: float,
        device_id: int | None,
        sample_fn: object,
        apply_fade: bool,
    ) -> None:
        self.duration_seconds = duration_seconds
        self.sample_rate = sample_rate
        self.channels = channels
        self.frequency_hz = frequency_hz
        self.amplitude = amplitude
        self.device_id = device_id
        self.sample_fn = sample_fn
        self.apply_fade = apply_fade
        self.start_calls = 0
        self.stop_calls = 0
        self._is_playing_checks = 0
        type(self).instances.append(self)

    @property
    def is_playing(self) -> bool:
        self._is_playing_checks += 1
        return self._is_playing_checks == 1

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


def test_positive_float_rejects_zero() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="must be greater than 0"):
        test_tone._positive_float("0")


def test_positive_int_rejects_negative() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="must be greater than 0"):
        test_tone._positive_int("-2")


def test_resolve_output_device_uses_default_label_when_uid_missing() -> None:
    device_id, device_name = test_tone._resolve_output_device(None)

    assert device_id is None
    assert device_name == "Default Output"


def test_resolve_output_device_returns_matching_output_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        test_tone,
        "list_audio_devices",
        lambda: [
            _FakeDevice("speaker", "Built-in Speakers", 41, (object(),)),
            _FakeDevice("silent", "Silent", 42, ()),
        ],
    )

    device_id, device_name = test_tone._resolve_output_device("speaker")

    assert device_id == 41
    assert device_name == "Built-in Speakers"


def test_resolve_output_device_rejects_non_output_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        test_tone,
        "list_audio_devices",
        lambda: [_FakeDevice("silent", "Silent", 42, ())],
    )

    with pytest.raises(
        LookupError,
        match="No output-capable device matches UID 'silent'",
    ):
        test_tone._resolve_output_device("silent")


def test_main_write_only_writes_tone_and_skips_playback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "helper-tone.wav"
    write_calls: list[dict[str, object]] = []

    def _fake_write_tone_wav(
        path: Path,
        *,
        seconds: float,
        frequency_hz: float,
        sample_rate: int,
        channels: int,
        amplitude: float,
    ) -> Path:
        write_calls.append(
            {
                "path": path,
                "seconds": seconds,
                "frequency_hz": frequency_hz,
                "sample_rate": sample_rate,
                "channels": channels,
                "amplitude": amplitude,
            }
        )
        return path

    def _unexpected_resolve(_device_uid: str | None) -> tuple[int | None, str]:
        raise AssertionError("device lookup should be skipped in write-only mode")

    monkeypatch.setattr(test_tone, "write_tone_wav", _fake_write_tone_wav)
    monkeypatch.setattr(test_tone, "_resolve_output_device", _unexpected_resolve)

    exit_code = test_tone.main(
        [
            "--seconds",
            "1.5",
            "--frequency",
            "330",
            "--sample-rate",
            "48000",
            "--channels",
            "1",
            "--amplitude",
            "0.25",
            "--output",
            str(output_path),
            "--write-only",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert write_calls == [
        {
            "path": output_path,
            "seconds": 1.5,
            "frequency_hz": 330.0,
            "sample_rate": 48_000,
            "channels": 1,
            "amplitude": 0.25,
        }
    ]
    assert f"Wrote helper tone: {output_path}" in captured.out


def test_main_rejects_amplitude_above_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        test_tone.main(["--amplitude", "1.1", "--write-only"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--amplitude must be between 0 and 1" in captured.err


def test_main_plays_tone_and_stops_player(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "helper-tone.wav"
    registered_signals: list[tuple[object, object]] = []
    sleep_calls: list[float] = []
    monotonic_values = iter([100.0, 100.05, 100.1])
    _FakePlayer.instances.clear()

    monkeypatch.setattr(test_tone, "write_tone_wav", lambda path, **_: path)
    monkeypatch.setattr(
        test_tone,
        "_resolve_output_device",
        lambda device_uid: (99, "Studio Display"),
    )
    monkeypatch.setattr(test_tone, "TonePlayer", _FakePlayer)
    monkeypatch.setattr(
        test_tone.signal,
        "signal",
        lambda signum, handler: registered_signals.append((signum, handler)),
    )
    monkeypatch.setattr(test_tone.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        test_tone.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    exit_code = test_tone.main(
        [
            "--seconds",
            "0.2",
            "--device-uid",
            "speaker",
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Wrote helper tone: {output_path}" in captured.out
    assert "Playing helper tone on Studio Display" in captured.out
    assert len(registered_signals) == 2
    assert sleep_calls == [0.05]
    assert len(_FakePlayer.instances) == 1

    player = _FakePlayer.instances[0]
    assert player.start_calls == 1
    assert player.stop_calls == 1
    assert player.duration_seconds == 0.2
    assert player.sample_rate == 44_100.0
    assert player.channels == 2
    assert player.frequency_hz == 220.0
    assert player.amplitude == 0.12
    assert player.device_id == 99
    assert player.sample_fn is test_tone.pleasant_tone_sample
    assert player.apply_fade is True
