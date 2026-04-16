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

    assert "TapDescription" in module.__all__
    assert "AudioRecorder" in module.__all__
    assert isinstance(module.__version__, str)


def test_public_exports_reference_expected_symbols() -> None:
    module = importlib.import_module("catap")
    recorder_module = importlib.import_module("catap.core.recorder")
    tap_module = importlib.import_module("catap.bindings.tap_description")
    process_module = importlib.import_module("catap.bindings.process")
    hardware_module = importlib.import_module("catap.bindings.hardware")

    assert module.AudioRecorder is recorder_module.AudioRecorder
    assert module.TapDescription is tap_module.TapDescription
    assert module.TapMuteBehavior is tap_module.TapMuteBehavior
    assert module.AudioProcess is process_module.AudioProcess
    assert module.list_audio_processes is process_module.list_audio_processes
    assert module.find_process_by_name is process_module.find_process_by_name
    assert module.create_process_tap is hardware_module.create_process_tap
    assert module.destroy_process_tap is hardware_module.destroy_process_tap


def test_unknown_attribute_raises_attribute_error() -> None:
    module = importlib.import_module("catap")

    with pytest.raises(AttributeError):
        module.__getattribute__("this_attribute_does_not_exist")


def test_import_raises_on_non_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    _purge_catap_modules()

    try:
        with pytest.raises(
            ImportError, match="catap only supports macOS 14.2 or later."
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
            match=r"catap requires macOS 14.2 or later. Detected macOS 13.6.7.",
        ):
            importlib.import_module("catap")
    finally:
        _purge_catap_modules()
