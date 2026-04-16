"""Tests for package-level API behavior."""

from __future__ import annotations

import importlib

import pytest


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
