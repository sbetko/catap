"""Tap capture engine lifecycle tests."""

from __future__ import annotations

import ctypes
from typing import Any

import pytest

import catap._capture_engine as capture_module
from catap.bindings._audiotoolbox import (
    AudioStreamBasicDescription,
    kAudioFormatFlagIsFloat,
    kAudioFormatFlagIsNonInterleaved,
    kAudioFormatFlagIsPacked,
    kAudioFormatLinearPCM,
)
from catap.bindings._coreaudio import kAudioHardwareBadObjectError
from catap.bindings.tap import AudioTapNotFoundError
from catap.drift import DriftCompensationQuality


def _set_void_p(pointer: Any, value: int) -> None:
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = value


def _capture_aggregate_description(
    monkeypatch: pytest.MonkeyPatch,
    *,
    quality: DriftCompensationQuality | None,
) -> dict[str, Any]:
    dictionaries: list[dict[str, Any]] = []

    class _FakeCFDictionary(dict[str, Any]):
        def __c_void_p__(self) -> ctypes.c_void_p:
            return ctypes.c_void_p(1)

    class _FakeNSDictionary:
        @staticmethod
        def dictionaryWithDictionary_(entries: dict[str, Any]) -> _FakeCFDictionary:
            result = _FakeCFDictionary(entries)
            dictionaries.append(result)
            return result

    class _FakeNSArray:
        @staticmethod
        def arrayWithArray_(entries: list[Any]) -> list[Any]:
            return entries

    class _FakeNSNumber:
        @staticmethod
        def numberWithBool_(value: bool) -> bool:
            return value

        @staticmethod
        def numberWithUnsignedInt_(value: int) -> int:
            return value

    def create_aggregate_device(
        description: ctypes.c_void_p,
        device_id: Any,
    ) -> int:
        del description
        ctypes.cast(device_id, ctypes.POINTER(ctypes.c_uint32)).contents.value = 55
        return 0

    monkeypatch.setattr(capture_module, "NSDictionary", _FakeNSDictionary)
    monkeypatch.setattr(capture_module, "NSArray", _FakeNSArray)
    monkeypatch.setattr(capture_module, "NSNumber", _FakeNSNumber)
    monkeypatch.setattr(
        capture_module,
        "_AudioHardwareCreateAggregateDevice",
        create_aggregate_device,
    )

    device_id = capture_module._create_aggregate_device(
        ["tap-one", "tap-two"],
        "test aggregate",
        drift_compensation_quality=quality,
    )

    assert device_id == 55
    return dictionaries[-1]


def test_aggregate_omits_drift_quality_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    description = _capture_aggregate_description(monkeypatch, quality=None)

    assert [tap["uid"] for tap in description["taps"]] == [
        "tap-one",
        "tap-two",
    ]
    assert all(tap["drift"] is True for tap in description["taps"])
    assert all("drift quality" not in tap for tap in description["taps"])


@pytest.mark.parametrize(
    "quality",
    list(DriftCompensationQuality),
    ids=lambda quality: quality.name.lower(),
)
def test_aggregate_applies_drift_quality_to_every_tap(
    monkeypatch: pytest.MonkeyPatch,
    quality: DriftCompensationQuality,
) -> None:
    description = _capture_aggregate_description(
        monkeypatch,
        quality=quality,
    )

    assert [tap["drift quality"] for tap in description["taps"]] == [
        quality.value,
        quality.value,
    ]


@pytest.mark.parametrize("quality", [True, 0, 96, 127])
def test_aggregate_rejects_non_enum_drift_quality(quality: object) -> None:
    with pytest.raises(TypeError, match="DriftCompensationQuality"):
        capture_module._create_aggregate_device(
            ["tap-one"],
            "test aggregate",
            drift_compensation_quality=quality,  # type: ignore[arg-type]
        )


def test_destroy_aggregate_device_accepts_already_destroyed_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capture_module,
        "_AudioHardwareDestroyAggregateDevice",
        lambda device_id: kAudioHardwareBadObjectError,
    )

    capture_module._destroy_aggregate_device(55)


def test_destroy_io_proc_accepts_already_destroyed_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capture_module,
        "_AudioDeviceDestroyIOProcID",
        lambda device_id, io_proc_id: kAudioHardwareBadObjectError,
    )

    capture_module._destroy_io_proc(55, ctypes.c_void_p(77))


def test_describe_tap_stream_uses_tap_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asbd = AudioStreamBasicDescription()
    asbd.mSampleRate = 96_000.0
    asbd.mFormatID = kAudioFormatLinearPCM
    asbd.mChannelsPerFrame = 6
    asbd.mBitsPerChannel = 32
    asbd.mBytesPerFrame = 24
    asbd.mFormatFlags = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked
    monkeypatch.setattr(capture_module, "_get_tap_format", lambda tap_id: asbd)

    stream_format = capture_module._TapCaptureEngine().describe_tap_stream(123)

    assert stream_format == capture_module._TapStreamFormat(
        96_000.0,
        6,
        32,
        True,
        bytes_per_frame=24,
        is_interleaved=True,
        is_signed_integer=False,
    )


def test_describe_tap_stream_detects_non_interleaved_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asbd = AudioStreamBasicDescription()
    asbd.mSampleRate = 48_000.0
    asbd.mFormatID = kAudioFormatLinearPCM
    asbd.mChannelsPerFrame = 2
    asbd.mBitsPerChannel = 32
    asbd.mBytesPerFrame = 4
    asbd.mFormatFlags = (
        kAudioFormatFlagIsFloat
        | kAudioFormatFlagIsPacked
        | kAudioFormatFlagIsNonInterleaved
    )
    monkeypatch.setattr(capture_module, "_get_tap_format", lambda tap_id: asbd)

    stream_format = capture_module._TapCaptureEngine().describe_tap_stream(123)

    assert stream_format.is_interleaved is False


def test_describe_tap_stream_raises_for_unavailable_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capture_module,
        "_get_tap_format",
        lambda tap_id: (_ for _ in ()).throw(OSError("format unavailable")),
    )

    with pytest.raises(OSError, match="Failed to read audio format for tap 123"):
        capture_module._TapCaptureEngine().describe_tap_stream(123)


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
        capture_module._TapCaptureEngine().describe_tap_stream(123)


def test_open_tap_capture_creates_aggregate_device_and_io_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = object()
    calls: list[tuple[str, object]] = []

    def create_aggregate_device(
        tap_uid: str,
        name: str,
        out: object = None,
    ) -> int:
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


def test_open_tap_capture_passes_client_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = ctypes.c_void_p(123)
    client_data = ctypes.c_void_p(456)
    calls: list[tuple[int, object, object]] = []

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name, out=None: 55,
    )

    def create_io_proc(
        device_id: int,
        callback_arg: object,
        client_data_arg: object,
        io_proc_id: object,
    ) -> int:
        calls.append((device_id, callback_arg, client_data_arg))
        _set_void_p(io_proc_id, 77)
        return 0

    monkeypatch.setattr(capture_module, "_AudioDeviceCreateIOProcID", create_io_proc)

    session = capture_module._TapCaptureEngine().open_tap_capture(
        123,
        callback,
        client_data,
    )

    assert calls == [(55, callback, client_data)]
    assert session.io_proc_callback is callback
    assert session.client_data is client_data


def test_open_tap_capture_destroys_aggregate_when_io_proc_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed: list[int] = []
    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name, out=None: 55,
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


def test_open_tap_capture_destroys_aggregate_recovered_after_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed: list[int] = []

    def create_aggregate_device_then_interrupt(
        tap_uid: str,
        name: str,
        *,
        out: ctypes.c_uint32,
    ) -> int:
        del tap_uid, name
        out.value = 55
        raise KeyboardInterrupt

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        create_aggregate_device_then_interrupt,
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_aggregate_device",
        lambda device_id: destroyed.append(device_id),
    )

    engine = capture_module._TapCaptureEngine()
    with pytest.raises(KeyboardInterrupt):
        engine.open_tap_capture(123, object())

    assert destroyed == [55]
    assert engine.failed_capture_session is None


def test_open_tap_capture_notes_cleanup_failure_when_unwind_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def destroy_aggregate_device(device_id: int) -> None:
        raise OSError(f"destroy failed for {device_id}")

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name, out=None: 55,
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


def test_open_tap_capture_unwinds_registered_io_proc_when_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def create_io_proc(
        device_id: int,
        callback: object,
        client_data: object,
        io_proc_id: object,
    ) -> int:
        del callback, client_data
        _set_void_p(io_proc_id, 77)
        calls.append(f"create-io:{device_id}")
        raise KeyboardInterrupt("interrupted after registration")

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name, out=None: 55,
    )
    monkeypatch.setattr(capture_module, "_AudioDeviceCreateIOProcID", create_io_proc)
    monkeypatch.setattr(
        capture_module,
        "_destroy_io_proc",
        lambda device_id, io_proc_id: calls.append(
            f"destroy-io:{device_id}:{io_proc_id.value}"
        ),
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_aggregate_device",
        lambda device_id: calls.append(f"destroy-device:{device_id}"),
    )

    with pytest.raises(KeyboardInterrupt, match="interrupted after registration"):
        capture_module._TapCaptureEngine().open_tap_capture(123, object())

    assert calls == ["create-io:55", "destroy-io:55:77", "destroy-device:55"]


def test_open_tap_capture_preserves_session_when_io_proc_unwind_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def create_io_proc(
        device_id: int,
        callback: object,
        client_data: object,
        io_proc_id: object,
    ) -> int:
        del device_id, callback, client_data
        _set_void_p(io_proc_id, 77)
        raise KeyboardInterrupt("interrupted after registration")

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name, out=None: 55,
    )
    monkeypatch.setattr(capture_module, "_AudioDeviceCreateIOProcID", create_io_proc)
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
        lambda device_id: None,
    )
    engine = capture_module._TapCaptureEngine()

    with pytest.raises(KeyboardInterrupt) as exc_info:
        engine.open_tap_capture(123, object())

    assert engine.failed_capture_session is not None
    assert engine.failed_capture_session.io_proc_id.value == 77
    assert engine.failed_capture_session.io_proc_destroyed is False
    assert engine.failed_capture_session.aggregate_device_destroyed is True
    assert any("destroy io failed" in note for note in exc_info.value.__notes__)


def test_open_tap_capture_publishes_ownership_until_caller_acknowledges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def create_io_proc(
        device_id: int,
        callback: object,
        client_data: object,
        io_proc_id: object,
    ) -> int:
        del device_id, callback, client_data
        _set_void_p(io_proc_id, 77)
        return 0

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name, out=None: 55,
    )
    monkeypatch.setattr(capture_module, "_AudioDeviceCreateIOProcID", create_io_proc)
    engine = capture_module._TapCaptureEngine()

    session = engine.open_tap_capture(123, object())

    assert engine.failed_capture_session is session
    engine.acknowledge_capture_session(session)
    assert engine.failed_capture_session is None


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


def test_stop_preserves_started_state_when_core_audio_stop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=True,
    )
    monkeypatch.setattr(
        capture_module,
        "_AudioDeviceStop",
        lambda device_id, io_proc_id: 10,
    )

    with pytest.raises(OSError, match="Failed to stop audio device: status 10"):
        capture_module._TapCaptureEngine().stop(session)

    assert session.started is True


def test_close_stops_started_session_before_destroying_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=True,
    )
    calls: list[str] = []

    def stop_device(device_id: int, io_proc_id: ctypes.c_void_p) -> int:
        calls.append(f"stop:{device_id}:{io_proc_id.value}")
        return 0

    def destroy_io_proc(device_id: int, io_proc_id: ctypes.c_void_p) -> None:
        calls.append(f"destroy-io:{device_id}:{io_proc_id.value}")

    def destroy_aggregate_device(device_id: int) -> None:
        calls.append(f"destroy-device:{device_id}")

    monkeypatch.setattr(capture_module, "_AudioDeviceStop", stop_device)
    monkeypatch.setattr(capture_module, "_destroy_io_proc", destroy_io_proc)
    monkeypatch.setattr(
        capture_module, "_destroy_aggregate_device", destroy_aggregate_device
    )

    capture_module._TapCaptureEngine().close(session)

    assert calls == ["stop:55:77", "destroy-io:55:77", "destroy-device:55"]
    assert session.started is False
    assert session.io_proc_destroyed is True
    assert session.aggregate_device_destroyed is True


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
    assert session.io_proc_destroyed is False
    assert session.aggregate_device_destroyed is False


def test_close_finishes_cleanup_after_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("stop interrupted")
    session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=True,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        capture_module,
        "_stop_audio_device",
        lambda device_id, io_proc_id: (_ for _ in ()).throw(interrupt),
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_io_proc",
        lambda device_id, io_proc_id: calls.append("destroy-io"),
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_aggregate_device",
        lambda device_id: (_ for _ in ()).throw(OSError("destroy aggregate failed")),
    )

    with pytest.raises(KeyboardInterrupt, match="stop interrupted") as exc_info:
        capture_module._TapCaptureEngine().close(session)

    assert exc_info.value is interrupt
    assert calls == ["destroy-io"]
    assert session.started is False
    assert session.io_proc_destroyed is True
    assert session.aggregate_device_destroyed is False
    assert any("destroy aggregate failed" in note for note in exc_info.value.__notes__)


def test_close_tracks_aggregate_success_without_assuming_io_proc_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=True,
    )
    monkeypatch.setattr(
        capture_module,
        "_AudioDeviceStop",
        lambda device_id, io_proc_id: 10,
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
        lambda device_id: None,
    )

    with pytest.raises(OSError, match="Failed to stop audio device"):
        capture_module._TapCaptureEngine().close(session)

    assert session.started is True
    assert session.io_proc_destroyed is False
    assert session.aggregate_device_destroyed is True


def test_close_is_idempotent_after_resources_are_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        io_proc_destroyed=True,
        aggregate_device_destroyed=True,
    )

    monkeypatch.setattr(
        capture_module,
        "_destroy_io_proc",
        lambda device_id, io_proc_id: (_ for _ in ()).throw(
            AssertionError("IOProc destruction should not be retried")
        ),
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_aggregate_device",
        lambda device_id: (_ for _ in ()).throw(
            AssertionError("aggregate destruction should not be retried")
        ),
    )

    capture_module._TapCaptureEngine().close(session)
