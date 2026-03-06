"""Tests for package-level API behavior."""

from __future__ import annotations

import importlib
import types

import pytest


def test_module_has_expected_exports() -> None:
    module = importlib.import_module("catap")

    assert "TapDescription" in module.__all__
    assert "AudioRecorder" in module.__all__
    assert isinstance(module.__version__, str)


def test_lazy_import_uses_export_map(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("catap")
    module.__dict__.pop("AudioRecorder", None)

    fake_audio_recorder = type("AudioRecorder", (), {})

    def fake_import_module(module_name: str) -> types.SimpleNamespace:
        assert module_name == "catap.core.recorder"
        return types.SimpleNamespace(AudioRecorder=fake_audio_recorder)

    monkeypatch.setattr(module, "import_module", fake_import_module)

    assert module.AudioRecorder is fake_audio_recorder


def test_unknown_attribute_raises_attribute_error() -> None:
    module = importlib.import_module("catap")

    with pytest.raises(AttributeError):
        _ = module.this_attribute_does_not_exist
