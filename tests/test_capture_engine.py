"""Tap capture engine lifecycle tests."""

from __future__ import annotations

import ctypes
from typing import Any

import pytest

import catap._capture_engine as capture_module
from catap.bindings._audiotoolbox import (
    AudioStreamBasicDescription,
    kAudioFormatFlagIsFloat,
)
from catap.bindings._coreaudio import kAudioHardwareBadObjectError
from catap.bindings.tap import AudioTapNotFoundError


def _set_void_p(pointer: Any, value: int) -> None:
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = value


def test_describe_tap_stream_uses_tap_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asbd = AudioStreamBasicDescription()
    asbd.mSampleRate = 96_000.0
    asbd.mChannelsPerFrame = 6
    asbd.mBitsPerChannel = 32
    asbd.mFormatFlags = kAudioFormatFlagIsFloat
    monkeypatch.setattr(capture_module, "_get_tap_format", lambda tap_id: asbd)

    stream_format = capture_module._TapCaptureEngine().describe_tap_stream(
        123,
        default=capture_module._TapStreamFormat(44_100.0, 2, 16, False),
    )

    assert stream_format == capture_module._TapStreamFormat(96_000.0, 6, 32, True)


def test_describe_tap_stream_returns_default_for_unavailable_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = capture_module._TapStreamFormat(44_100.0, 2, 16, False)
    monkeypatch.setattr(
        capture_module,
        "_get_tap_format",
        lambda tap_id: (_ for _ in ()).throw(OSError("format unavailable")),
    )

    stream_format = capture_module._TapCaptureEngine().describe_tap_stream(
        123,
        default=default,
    )

    assert stream_format is default


def test_describe_tap_stream_raises_stale_tap_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_error = OSError("bad object")
    stale_error.status = kAudioHardwareBadObjectError  # type: ignore[attr-defined]
    monkeypatch.setattr(
        capture_module,
        "_get_tap_format",
        lambda tap_id: (_ for _ in ()).throw(stale_error),
    )

    with pytest.raises(AudioTapNotFoundError, match="Audio tap 123 is no longer"):
        capture_module._TapCaptureEngine().describe_tap_stream(
            123,
            default=capture_module._TapStreamFormat(44_100.0, 2, 16, False),
        )


def test_open_tap_capture_creates_aggregate_device_and_io_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = object()
    calls: list[tuple[str, object]] = []

    def create_aggregate_device(tap_uid: str, name: str) -> int:
        calls.append(("aggregate", (tap_uid, name)))
        return 55

    def create_io_proc(
        device_id: int,
        callback_arg: object,
        client_data: object,
        io_proc_id: object,
    ) -> int:
        calls.append(("io-proc", (device_id, callback_arg, client_data)))
        _set_void_p(io_proc_id, 77)
        return 0

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module, "_create_aggregate_device_for_tap", create_aggregate_device
    )
    monkeypatch.setattr(capture_module, "_AudioDeviceCreateIOProcID", create_io_proc)

    session = capture_module._TapCaptureEngine().open_tap_capture(123, callback)

    assert calls == [
        ("aggregate", ("tap-uid", "catap Recording Device")),
        ("io-proc", (55, callback, None)),
    ]
    assert session.aggregate_device_id == 55
    assert session.io_proc_id.value == 77
    assert session.started is False


def test_open_tap_capture_destroys_aggregate_when_io_proc_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed: list[int] = []
    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name: 55,
    )
    monkeypatch.setattr(
        capture_module,
        "_AudioDeviceCreateIOProcID",
        lambda device_id, callback, client_data, io_proc_id: 9,
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_aggregate_device",
        lambda device_id: destroyed.append(device_id),
    )

    with pytest.raises(OSError, match="Failed to create IO proc: status 9"):
        capture_module._TapCaptureEngine().open_tap_capture(123, object())

    assert destroyed == [55]


def test_open_tap_capture_notes_cleanup_failure_when_unwind_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def destroy_aggregate_device(device_id: int) -> None:
        raise OSError(f"destroy failed for {device_id}")

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name: 55,
    )
    monkeypatch.setattr(
        capture_module,
        "_AudioDeviceCreateIOProcID",
        lambda device_id, callback, client_data, io_proc_id: 9,
    )
    monkeypatch.setattr(
        capture_module, "_destroy_aggregate_device", destroy_aggregate_device
    )

    with pytest.raises(OSError, match="Failed to create IO proc") as exc_info:
        capture_module._TapCaptureEngine().open_tap_capture(123, object())

    assert any(
        "Cleanup failure while opening capture engine" in note
        and "destroy failed for 55" in note
        for note in exc_info.value.__notes__
    )


def test_start_marks_session_started_only_after_core_audio_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
    )
    monkeypatch.setattr(
        capture_module,
        "_AudioDeviceStart",
        lambda device_id, io_proc_id: 9,
    )

    with pytest.raises(OSError, match="Failed to start audio device: status 9"):
        capture_module._TapCaptureEngine().start(session)

    assert session.started is False


def test_stop_skips_unstarted_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=False,
    )

    def unexpected_stop(device_id: int, io_proc_id: ctypes.c_void_p) -> int:
        raise AssertionError("Core Audio stop should not be called")

    monkeypatch.setattr(capture_module, "_AudioDeviceStop", unexpected_stop)

    capture_module._TapCaptureEngine().stop(session)

    assert session.started is False


def test_close_combines_io_proc_and_aggregate_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_io_proc",
        lambda device_id, io_proc_id: (_ for _ in ()).throw(
            OSError("destroy io failed")
        ),
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_aggregate_device",
        lambda device_id: (_ for _ in ()).throw(OSError("destroy aggregate failed")),
    )

    with pytest.raises(OSError, match="destroy io failed") as exc_info:
        capture_module._TapCaptureEngine().close(session)

    notes = exc_info.value.__notes__
    assert "Failed to close tap capture session" in notes
    assert any("destroy aggregate failed" in note for note in notes)
