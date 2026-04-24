"""Regression tests for the headless tone-farm helpers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import pytest

import catap._devtools.tone_farm as tone_farm


@dataclass
class _FakeAudioProcess:
    audio_object_id: int
    pid: int
    bundle_id: str | None
    name: str
    is_outputting: bool


def test_build_specs_generates_guard_spaced_default_frequencies() -> None:
    specs = tone_farm._build_specs(
        count=3,
        frequency_arg=None,
        sample_rate=44_100,
        amplitude=0.04,
        channel_mode="all",
    )

    assert [spec.tone_id for spec in specs] == ["tone-001", "tone-002", "tone-003"]
    assert [spec.frequency_hz for spec in specs] == [431.0, 604.0, 777.0]
    assert {spec.amplitude for spec in specs} == {0.04}


def test_build_specs_requires_count_to_match_explicit_frequencies() -> None:
    with pytest.raises(ValueError, match="--count must match"):
        tone_farm._build_specs(
            count=2,
            frequency_arg="431,719,1031",
            sample_rate=44_100,
            amplitude=0.04,
            channel_mode="all",
        )


def test_channel_gains_selects_single_channels() -> None:
    assert tone_farm._channel_gains(2, "all") == (1.0, 1.0)
    assert tone_farm._channel_gains(2, "left") == (1.0, 0.0)
    assert tone_farm._channel_gains(3, "right") == (0.0, 1.0, 0.0)

    with pytest.raises(ValueError, match="requires at least two channels"):
        tone_farm._channel_gains(1, "right")


def test_build_manifest_maps_ready_workers_to_audio_process_metadata() -> None:
    args = argparse.Namespace(
        seconds=0.0,
        buffer_seconds=1.0,
        channels=2,
        sample_rate=44_100,
        device_uid="speaker",
    )
    specs = [
        tone_farm.ToneSpec(
            tone_id="tone-001",
            frequency_hz=431.0,
            amplitude=0.04,
            channel_mode="all",
        )
    ]
    manifest = tone_farm._build_manifest(
        args=args,
        specs=specs,
        ready_events={"tone-001": {"pid": 1234}},
        processes_by_pid={
            1234: _FakeAudioProcess(
                audio_object_id=99,
                pid=1234,
                bundle_id="org.python.python",
                name="Python",
                is_outputting=True,
            )
        },
    )

    assert manifest["schema"] == "catap-tone-farm/v1"
    assert manifest["seconds"] == 0.0
    assert manifest["tones"] == [
        {
            "id": "tone-001",
            "pid": 1234,
            "audio_object_id": 99,
            "process_name": "Python",
            "bundle_id": "org.python.python",
            "is_outputting": True,
            "frequency_hz": 431.0,
            "amplitude": 0.04,
            "waveform": "pure_sine",
            "channel_mode": "all",
            "channels": 2,
            "sample_rate": 44_100,
            "device_uid": "speaker",
        }
    ]
