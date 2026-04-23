"""Shared Core Audio helper tests."""

from __future__ import annotations

import struct

import pytest

import catap.bindings._coreaudio as coreaudio_module


def test_get_property_object_ids_returns_empty_for_empty_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coreaudio_module, "get_property_bytes", lambda *args: b"")

    assert coreaudio_module.get_property_object_ids(1, 2) == []


def test_get_property_object_ids_decodes_complete_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_bytes",
        lambda *args: struct.pack("<III", 10, 20, 30),
    )

    assert coreaudio_module.get_property_object_ids(1, 2) == [10, 20, 30]


def test_get_property_object_ids_ignores_trailing_partial_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_bytes",
        lambda *args: struct.pack("<II", 10, 20) + b"\x99\x88",
    )

    assert coreaudio_module.get_property_object_ids(1, 2) == [10, 20]


def test_get_optional_property_cfstring_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_cfstring",
        lambda *args: (_ for _ in ()).throw(OSError("missing property")),
    )

    assert coreaudio_module.get_optional_property_cfstring(1, 2) is None


def test_get_optional_property_cfstring_returns_string_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_cfstring",
        lambda *args: "Built-in Speakers",
    )

    assert coreaudio_module.get_optional_property_cfstring(1, 2) == "Built-in Speakers"


def test_get_optional_property_cfstring_preserves_none_from_empty_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_cfstring",
        lambda *args: None,
    )

    assert coreaudio_module.get_optional_property_cfstring(1, 2) is None


def test_get_optional_property_cfstring_does_not_swallow_unrelated_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_cfstring",
        lambda *args: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        coreaudio_module.get_optional_property_cfstring(1, 2)
