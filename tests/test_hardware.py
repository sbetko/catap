"""Tap creation binding tests."""

from __future__ import annotations

import ctypes
from typing import Any, cast

import pytest

import catap.bindings.hardware as hardware_module
from catap.bindings._coreaudio import kAudioHardwareBadObjectError
from catap.bindings.tap_description import TapDescription


class _FakeObjCDescription:
    def __c_void_p__(self) -> ctypes.c_void_p:
        return ctypes.c_void_p(0)


class _FakeTapDescription:
    objc_object = _FakeObjCDescription()


def _set_uint32(ref: Any, value: int) -> None:
    ref._obj.value = value


def test_create_process_tap_returns_id_and_fills_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def create_tap(description_ptr: object, tap_id_ref: object) -> int:
        del description_ptr
        _set_uint32(tap_id_ref, 501)
        return 0

    monkeypatch.setattr(hardware_module, "_AudioHardwareCreateProcessTap", create_tap)

    out = ctypes.c_uint32(999)
    tap_id = hardware_module.create_process_tap(
        cast(TapDescription, _FakeTapDescription()),
        out=out,
    )

    assert tap_id == 501
    assert out.value == 501


def test_create_process_tap_zeroes_out_on_status_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hardware_module,
        "_AudioHardwareCreateProcessTap",
        lambda description_ptr, tap_id_ref: 9,
    )

    out = ctypes.c_uint32(999)
    with pytest.raises(
        OSError,
        match="AudioHardwareCreateProcessTap failed with status 9",
    ):
        hardware_module.create_process_tap(
            cast(TapDescription, _FakeTapDescription()),
            out=out,
        )

    assert out.value == 0


def test_create_process_tap_rejects_success_status_without_tap_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def create_tap_without_id(description_ptr: object, tap_id_ref: object) -> int:
        del description_ptr
        _set_uint32(tap_id_ref, 0)
        return 0

    monkeypatch.setattr(
        hardware_module,
        "_AudioHardwareCreateProcessTap",
        create_tap_without_id,
    )

    out = ctypes.c_uint32(999)
    with pytest.raises(OSError, match="returned no tap for this description"):
        hardware_module.create_process_tap(
            cast(TapDescription, _FakeTapDescription()),
            out=out,
        )

    assert out.value == 0


def test_interrupted_create_process_tap_destroys_orphan_without_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed: list[int] = []

    def create_tap_then_interrupt(description_ptr: object, tap_id_ref: object) -> int:
        del description_ptr
        _set_uint32(tap_id_ref, 501)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        hardware_module,
        "_AudioHardwareCreateProcessTap",
        create_tap_then_interrupt,
    )
    monkeypatch.setattr(
        hardware_module,
        "_AudioHardwareDestroyProcessTap",
        lambda tap_id: destroyed.append(tap_id) or 0,
    )

    with pytest.raises(KeyboardInterrupt):
        hardware_module.create_process_tap(cast(TapDescription, _FakeTapDescription()))

    assert destroyed == [501]


def test_interrupted_create_process_tap_leaves_recovery_to_out_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed: list[int] = []

    def create_tap_then_interrupt(description_ptr: object, tap_id_ref: object) -> int:
        del description_ptr
        _set_uint32(tap_id_ref, 501)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        hardware_module,
        "_AudioHardwareCreateProcessTap",
        create_tap_then_interrupt,
    )
    monkeypatch.setattr(
        hardware_module,
        "_AudioHardwareDestroyProcessTap",
        lambda tap_id: destroyed.append(tap_id) or 0,
    )

    out = ctypes.c_uint32(0)
    with pytest.raises(KeyboardInterrupt):
        hardware_module.create_process_tap(
            cast(TapDescription, _FakeTapDescription()),
            out=out,
        )

    assert destroyed == []
    assert out.value == 501


def test_destroy_process_tap_exposes_core_audio_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hardware_module,
        "_AudioHardwareDestroyProcessTap",
        lambda tap_id: kAudioHardwareBadObjectError,
    )

    with pytest.raises(OSError, match="AudioHardwareDestroyProcessTap") as exc_info:
        hardware_module.destroy_process_tap(501)

    assert getattr(exc_info.value, "status", None) == kAudioHardwareBadObjectError
