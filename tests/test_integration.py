"""Opt-in macOS integration smoke tests."""

from __future__ import annotations

import os
import platform
import wave

import pytest

RUN_INTEGRATION = os.getenv("CATAP_RUN_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only"),
]


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
    assert session.sample_rate is not None
    assert session.num_channels is not None
    assert session.duration_seconds >= 0.0

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnchannels() > 0
        assert wav_file.getframerate() > 0
        assert wav_file.getsampwidth() > 0
        assert wav_file.getnframes() >= 0
