"""Opt-in macOS integration smoke tests."""

from __future__ import annotations

import json
import os
import platform
import selectors
import subprocess
import sys
import time
import wave
from contextlib import suppress
from pathlib import Path

import pytest

RUN_INTEGRATION = os.getenv("CATAP_RUN_INTEGRATION") == "1"
RUN_TONE_INTEGRATION = os.getenv("CATAP_RUN_TONE_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only"),
]


def _read_tone_farm_manifest(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float = 8.0,
) -> dict:
    if process.stdout is None:
        pytest.fail("tone farm has no stdout pipe")

    stdout = process.stdout
    selector = selectors.DefaultSelector()
    selector.register(stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr is not None else ""
                pytest.fail(
                    "tone farm exited before becoming ready "
                    f"(status {process.returncode}): {stderr}"
                )

            remaining = max(0.0, deadline - time.monotonic())
            for _key, _mask in selector.select(timeout=min(0.1, remaining)):
                line = stdout.readline()
                if line:
                    return json.loads(line)
    finally:
        selector.close()

    pytest.fail("timed out waiting for tone farm readiness")


def _stop_tone_farm(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
    finally:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                with suppress(OSError):
                    pipe.close()


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


def test_cli_records_headless_tone_by_audio_object_id(tmp_path: Path) -> None:
    if not RUN_TONE_INTEGRATION:
        pytest.skip(
            "set CATAP_RUN_TONE_INTEGRATION=1 to run tone-fixture integration tests"
        )

    manifest_path = tmp_path / "tones.json"
    tone_farm = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "catap._devtools.tone_farm",
            "--count",
            "2",
            "--frequencies",
            "2897,3251",
            "--seconds",
            "8",
            "--manifest",
            str(manifest_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        manifest = _read_tone_farm_manifest(tone_farm)
        first_tone = manifest["tones"][0]
        audio_object_id = first_tone["audio_object_id"]
        if audio_object_id is None:
            pytest.skip("tone farm did not resolve Core Audio process metadata")

        output_path = tmp_path / "tone-001.wav"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "catap",
                "record",
                "--audio-id",
                str(audio_object_id),
                "--duration",
                "2",
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=6,
        )

        assert result.returncode == 0, result.stderr or result.stdout

        from catap._devtools.tone_analyzer import ToneTarget, analyze_wav

        analysis = analyze_wav(
            output_path,
            [
                ToneTarget(
                    str(tone["id"]),
                    float(tone["frequency_hz"]),
                    str(tone.get("channel_mode", "all")),
                )
                for tone in manifest["tones"]
            ],
        )
        tones = {tone["id"]: tone for tone in analysis["tones"]}

        assert tones["tone-001"]["present"] is True
        assert tones["tone-002"]["present"] is False
    finally:
        _stop_tone_farm(tone_farm)
