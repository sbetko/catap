"""Public drift-compensation quality tests."""

from __future__ import annotations

from typing import Any, cast

import pytest

from catap import (
    AudioRecorder,
    DriftCompensationQuality,
    MultitrackAudioRecorder,
    MultitrackRecordingSession,
    RecordingSession,
)


def test_drift_compensation_quality_values_match_core_audio() -> None:
    assert [(quality.name, quality.value) for quality in DriftCompensationQuality] == [
        ("MINIMUM", 0),
        ("LOW", 0x20),
        ("MEDIUM", 0x40),
        ("HIGH", 0x60),
        ("MAXIMUM", 0x7F),
    ]


@pytest.mark.parametrize("quality", [False, True, 0, 0x40, 0x7F, object()])
def test_single_recorder_requires_exact_quality_enum(quality: object) -> None:
    with pytest.raises(TypeError, match="DriftCompensationQuality"):
        AudioRecorder(
            11,
            "recording.wav",
            drift_compensation_quality=cast(Any, quality),
        )


def test_multitrack_recorder_requires_exact_quality_enum() -> None:
    with pytest.raises(TypeError, match="DriftCompensationQuality"):
        MultitrackAudioRecorder(
            [11],
            ["recording.wav"],
            drift_compensation_quality=cast(Any, 0x60),
        )


def test_single_session_requires_exact_quality_enum() -> None:
    with pytest.raises(TypeError, match="DriftCompensationQuality"):
        RecordingSession(
            cast(Any, object()),
            "recording.wav",
            drift_compensation_quality=cast(Any, True),
        )


def test_multitrack_session_requires_exact_quality_enum() -> None:
    with pytest.raises(TypeError, match="DriftCompensationQuality"):
        MultitrackRecordingSession(
            [cast(Any, object())],
            ["recording.wav"],
            drift_compensation_quality=cast(Any, 0),
        )


def test_recorders_accept_every_quality_level() -> None:
    for quality in DriftCompensationQuality:
        recorder = AudioRecorder(
            11,
            "recording.wav",
            drift_compensation_quality=quality,
        )
        multitrack = MultitrackAudioRecorder(
            [11],
            ["recording.wav"],
            drift_compensation_quality=quality,
        )

        assert recorder.drift_compensation_quality is quality
        assert multitrack.drift_compensation_quality is quality
