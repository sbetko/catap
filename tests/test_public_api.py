"""Tests for package-level API behavior."""

from __future__ import annotations

import importlib
import sys

import pytest


def _purge_catap_modules() -> None:
    for module_name in list(sys.modules):
        if module_name == "catap" or module_name.startswith("catap."):
            sys.modules.pop(module_name, None)


def test_module_has_expected_exports() -> None:
    module = importlib.import_module("catap")

    assert "AmbiguousAudioProcessError" in module.__all__
    assert "AmbiguousAudioDeviceError" in module.__all__
    assert "TapDescription" in module.__all__
    assert "AudioDevice" in module.__all__
    assert "AudioDeviceStream" in module.__all__
    assert "AudioTap" in module.__all__
    assert "AudioTapNotFoundError" in module.__all__
    assert "AudioRecorder" in module.__all__
    assert "UnsupportedTapFormatError" in module.__all__
    assert "RecordingSession" in module.__all__
    assert "list_audio_devices" in module.__all__
    assert "find_audio_device_by_uid" in module.__all__
    assert "find_audio_device_by_name" in module.__all__
    assert "list_audio_taps" in module.__all__
    assert "find_tap_by_uid" in module.__all__
    assert "record_tap" in module.__all__
    assert "record_process" in module.__all__
    assert "record_system_audio" in module.__all__
    assert isinstance(module.__version__, str)


def test_public_exports_reference_expected_symbols() -> None:
    module = importlib.import_module("catap")
    device_module = importlib.import_module("catap.bindings.device")
    recorder_module = importlib.import_module("catap.recorder")
    tap_module = importlib.import_module("catap.bindings.tap_description")
    visible_tap_module = importlib.import_module("catap.bindings.tap")
    process_module = importlib.import_module("catap.bindings.process")
    hardware_module = importlib.import_module("catap.bindings.hardware")
    session_module = importlib.import_module("catap.session")

    assert module.AmbiguousAudioDeviceError is device_module.AmbiguousAudioDeviceError
    assert module.AudioDevice is device_module.AudioDevice
    assert module.AudioDeviceStream is device_module.AudioDeviceStream
    assert module.AudioRecorder is recorder_module.AudioRecorder
    assert module.UnsupportedTapFormatError is recorder_module.UnsupportedTapFormatError
    assert module.TapDescription is tap_module.TapDescription
    assert module.TapMuteBehavior is tap_module.TapMuteBehavior
    assert (
        module.AmbiguousAudioProcessError is process_module.AmbiguousAudioProcessError
    )
    assert module.AudioProcess is process_module.AudioProcess
    assert module.AudioTap is visible_tap_module.AudioTap
    assert module.AudioTapNotFoundError is visible_tap_module.AudioTapNotFoundError
    assert module.list_audio_devices is device_module.list_audio_devices
    assert module.find_audio_device_by_name is device_module.find_audio_device_by_name
    assert module.find_audio_device_by_uid is device_module.find_audio_device_by_uid
    assert module.list_audio_processes is process_module.list_audio_processes
    assert module.find_process_by_name is process_module.find_process_by_name
    assert module.list_audio_taps is visible_tap_module.list_audio_taps
    assert module.find_tap_by_uid is visible_tap_module.find_tap_by_uid
    assert module.create_process_tap is hardware_module.create_process_tap
    assert module.destroy_process_tap is hardware_module.destroy_process_tap
    assert module.AudioProcessNotFoundError is session_module.AudioProcessNotFoundError
    assert module.RecordingSession is session_module.RecordingSession
    assert module.record_tap is session_module.record_tap
    assert module.record_process is session_module.record_process
    assert module.record_system_audio is session_module.record_system_audio


def test_unknown_attribute_raises_attribute_error() -> None:
    module = importlib.import_module("catap")

    with pytest.raises(AttributeError):
        module.__getattribute__("this_attribute_does_not_exist")


def test_import_raises_on_non_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    _purge_catap_modules()

    try:
        with pytest.raises(
            ImportError, match=r"catap only supports macOS 14\.2 or later\."
        ):
            importlib.import_module("catap")
    finally:
        _purge_catap_modules()


def test_import_raises_on_old_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "platform.mac_ver",
        lambda: ("13.6.7", ("", "", ""), ""),
    )
    _purge_catap_modules()

    try:
        with pytest.raises(
            ImportError,
            match=r"catap requires macOS 14\.2 or later\. Detected macOS 13\.6\.7\.",
        ):
            importlib.import_module("catap")
    finally:
        _purge_catap_modules()
