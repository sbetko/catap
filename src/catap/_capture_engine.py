"""Internal Core Audio capture-session management."""

from __future__ import annotations

import ctypes
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

from Foundation import NSArray, NSDictionary, NSNumber  # ty: ignore[unresolved-import]

from catap._recording_support import _add_secondary_failure, _combine_errors
from catap.bindings._audiotoolbox import (
    AudioBufferList,
    AudioStreamBasicDescription,
    kAudioFormatFlagIsBigEndian,
    kAudioFormatFlagIsFloat,
    kAudioFormatFlagIsNonInterleaved,
    kAudioFormatFlagIsPacked,
    kAudioFormatFlagIsSignedInteger,
    kAudioFormatLinearPCM,
)
from catap.bindings._coreaudio import (
    _CoreAudio,
    _status_error,
    get_property_cfstring,
    get_property_object_ids,
    get_property_struct,
    kAudioHardwareBadObjectError,
    kAudioObjectPropertyElementMain,
    kAudioObjectPropertyScopeGlobal,
    kAudioObjectPropertyScopeInput,
)
from catap.bindings.tap import _raise_if_missing_tap
from catap.drift import (
    DriftCompensationQuality,
    _validate_drift_compensation_quality,
)


class AudioTimeStamp(ctypes.Structure):
    """Core Audio AudioTimeStamp structure."""

    _fields_ = [
        ("mSampleTime", ctypes.c_double),
        ("mHostTime", ctypes.c_uint64),
        ("mRateScalar", ctypes.c_double),
        ("mWordClockTime", ctypes.c_uint64),
        ("mSMPTETime", ctypes.c_uint8 * 24),
        ("mFlags", ctypes.c_uint32),
        ("mReserved", ctypes.c_uint32),
    ]


if TYPE_CHECKING:
    AudioTimeStampPtr: TypeAlias = ctypes._Pointer[AudioTimeStamp]
    AudioBufferListPtr: TypeAlias = ctypes._Pointer[AudioBufferList]
else:
    AudioTimeStampPtr = ctypes.c_void_p
    AudioBufferListPtr = ctypes.c_void_p


kAudioTapPropertyUID = int.from_bytes(b"tuid", "big")
kAudioTapPropertyFormat = int.from_bytes(b"tfmt", "big")
kAudioDevicePropertyStreams = int.from_bytes(b"stm#", "big")
kAudioStreamPropertyVirtualFormat = int.from_bytes(b"sfmt", "big")
kAudioTimeStampSampleTimeValid = 1 << 0
kAudioTimeStampHostTimeValid = 1 << 1
kAudioTimeStampRateScalarValid = 1 << 2
kAudioTimeStampWordClockTimeValid = 1 << 3


AudioDeviceIOProcType = ctypes.CFUNCTYPE(
    ctypes.c_int32,  # OSStatus return
    ctypes.c_uint32,  # AudioObjectID inDevice
    ctypes.POINTER(AudioTimeStamp),  # const AudioTimeStamp* inNow
    ctypes.POINTER(AudioBufferList),  # const AudioBufferList* inInputData
    ctypes.POINTER(AudioTimeStamp),  # const AudioTimeStamp* inInputTime
    ctypes.POINTER(AudioBufferList),  # AudioBufferList* outOutputData
    ctypes.POINTER(AudioTimeStamp),  # const AudioTimeStamp* inOutputTime
    ctypes.c_void_p,  # void* inClientData
)


_AudioHardwareCreateAggregateDevice = _CoreAudio.AudioHardwareCreateAggregateDevice
_AudioHardwareCreateAggregateDevice.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
]
_AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32

_AudioHardwareDestroyAggregateDevice = _CoreAudio.AudioHardwareDestroyAggregateDevice
_AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
_AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32

_AudioDeviceCreateIOProcID = _CoreAudio.AudioDeviceCreateIOProcID
_AudioDeviceCreateIOProcID.argtypes = [
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]
_AudioDeviceCreateIOProcID.restype = ctypes.c_int32

_AudioDeviceDestroyIOProcID = _CoreAudio.AudioDeviceDestroyIOProcID
_AudioDeviceDestroyIOProcID.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_AudioDeviceDestroyIOProcID.restype = ctypes.c_int32

_AudioDeviceStart = _CoreAudio.AudioDeviceStart
_AudioDeviceStart.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_AudioDeviceStart.restype = ctypes.c_int32

_AudioDeviceStop = _CoreAudio.AudioDeviceStop
_AudioDeviceStop.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_AudioDeviceStop.restype = ctypes.c_int32


def _get_tap_uid(tap_id: int) -> str:
    """Return the UID string for a tap."""
    uid = get_property_cfstring(
        tap_id,
        kAudioTapPropertyUID,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    if uid is None:
        raise OSError(f"Tap {tap_id} reported an empty UID")
    return uid


def _get_tap_format(tap_id: int) -> AudioStreamBasicDescription:
    """Return the audio format for a tap."""
    result = get_property_struct(
        tap_id,
        kAudioTapPropertyFormat,
        AudioStreamBasicDescription,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    assert isinstance(result, AudioStreamBasicDescription)
    return result


def _create_aggregate_device(
    tap_uids: Sequence[str],
    name: str,
    *,
    input_device_uids: Sequence[str] = (),
    drift_compensation_quality: DriftCompensationQuality | None = None,
    out: ctypes.c_uint32 | None = None,
) -> int:
    """Create a private aggregate device over taps and optional input devices.

    Hardware input subdevices come before taps in the aggregate's input
    stream order, which is also the IOProc's buffer order. When an input
    device is present it becomes the aggregate's master clock and each tap
    is drift-compensated against it.

    Core Audio writes the new device's ID into ``out`` when provided, so a
    caller interrupted between creation and storing the returned ID can still
    recover and destroy the device.
    """
    quality = _validate_drift_compensation_quality(drift_compensation_quality)
    agg_uid = f"io.github.catap.aggregate.{uuid.uuid4()}"

    tap_entries: list[Any] = []
    for tap_uid in tap_uids:
        tap_entry: dict[str, Any] = {
            "uid": tap_uid,
            "drift": NSNumber.numberWithBool_(True),
        }
        if quality is not None:
            tap_entry["drift quality"] = NSNumber.numberWithUnsignedInt_(int(quality))
        tap_entries.append(NSDictionary.dictionaryWithDictionary_(tap_entry))

    tap_list = NSArray.arrayWithArray_(tap_entries)

    description_entries: dict[str, Any] = {
        "name": name,
        "uid": agg_uid,
        "private": NSNumber.numberWithBool_(True),
        "taps": tap_list,
        "tapautostart": NSNumber.numberWithBool_(False),
    }
    if input_device_uids:
        description_entries["subdevices"] = NSArray.arrayWithArray_(
            [
                NSDictionary.dictionaryWithDictionary_({"uid": device_uid})
                for device_uid in input_device_uids
            ]
        )
        description_entries["master"] = input_device_uids[0]

    description = NSDictionary.dictionaryWithDictionary_(description_entries)

    cf_dict_ptr = description.__c_void_p__()
    device_id = ctypes.c_uint32(0) if out is None else out
    device_id.value = 0
    status = _AudioHardwareCreateAggregateDevice(cf_dict_ptr, ctypes.byref(device_id))
    if status != 0:
        device_id.value = 0
        raise OSError(f"Failed to create aggregate device: status {status}")

    return device_id.value


def _create_aggregate_device_for_tap(
    tap_uid: str,
    name: str,
    *,
    drift_compensation_quality: DriftCompensationQuality | None = None,
    out: ctypes.c_uint32 | None = None,
) -> int:
    """Create an aggregate device that includes the specified tap."""
    return _create_aggregate_device(
        [tap_uid],
        name,
        drift_compensation_quality=drift_compensation_quality,
        out=out,
    )


def _destroy_aggregate_device(device_id: int) -> None:
    """Destroy an aggregate device."""
    status = _AudioHardwareDestroyAggregateDevice(device_id)
    # A previous call may have completed in Core Audio before an asynchronous
    # Python exception recorded that progress.  In teardown, "already gone"
    # is the desired end state and makes the retry unambiguous.
    if status == kAudioHardwareBadObjectError:
        return
    if status != 0:
        raise _status_error(
            f"Failed to destroy aggregate device: status {status}",
            status,
        )


def _destroy_io_proc(device_id: int, io_proc_id: ctypes.c_void_p) -> None:
    """Destroy a Core Audio IO proc."""
    status = _AudioDeviceDestroyIOProcID(device_id, io_proc_id)
    # The IOProc (or its aggregate device) may already be gone after an
    # interrupted teardown call. Treat that terminal state as success so the
    # owning session can publish cleanup progress and converge on retry.
    if status == kAudioHardwareBadObjectError:
        return
    if status != 0:
        raise _status_error(
            f"Failed to destroy IO proc: status {status}",
            status,
        )


def _stop_audio_device(device_id: int, io_proc_id: ctypes.c_void_p) -> None:
    """Stop a Core Audio device IO proc."""
    status = _AudioDeviceStop(device_id, io_proc_id)
    if status != 0:
        raise OSError(f"Failed to stop audio device: status {status}")


@dataclass(slots=True)
class _TapStreamFormat:
    """Tap stream metadata used to configure the worker pipeline."""

    sample_rate: float
    num_channels: int
    bits_per_sample: int
    is_float: bool
    bytes_per_frame: int | None = None
    is_interleaved: bool = True
    format_id: int = kAudioFormatLinearPCM
    is_big_endian: bool = False
    is_packed: bool = True
    is_signed_integer: bool = True


@dataclass(slots=True)
class _TapCaptureSession:
    """Live Core Audio objects needed for one active recorder session."""

    aggregate_device_id: int
    io_proc_id: ctypes.c_void_p
    io_proc_callback: object | None = None
    client_data: object | None = None
    started: bool = False
    io_proc_destroyed: bool = False
    aggregate_device_destroyed: bool = False


def _stream_format_from_asbd(
    asbd: AudioStreamBasicDescription,
) -> _TapStreamFormat:
    """Build tap stream metadata from a Core Audio stream description."""
    return _TapStreamFormat(
        sample_rate=asbd.mSampleRate,
        num_channels=asbd.mChannelsPerFrame,
        bits_per_sample=asbd.mBitsPerChannel,
        is_float=bool(asbd.mFormatFlags & kAudioFormatFlagIsFloat),
        bytes_per_frame=asbd.mBytesPerFrame,
        is_interleaved=not bool(asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved),
        format_id=asbd.mFormatID,
        is_big_endian=bool(asbd.mFormatFlags & kAudioFormatFlagIsBigEndian),
        is_packed=bool(asbd.mFormatFlags & kAudioFormatFlagIsPacked),
        is_signed_integer=bool(asbd.mFormatFlags & kAudioFormatFlagIsSignedInteger),
    )


class _TapCaptureEngine:
    """Owns the Core Audio object lifetimes behind a tap capture session."""

    def __init__(self) -> None:
        self.failed_capture_session: _TapCaptureSession | None = None

    def describe_tap_stream(
        self,
        tap_id: int,
    ) -> _TapStreamFormat:
        """Return the tap format reported by Core Audio."""
        try:
            asbd = _get_tap_format(tap_id)
        except OSError as exc:
            _raise_if_missing_tap(tap_id, exc)
            raise OSError(
                f"Failed to read audio format for tap {tap_id}: {exc}"
            ) from exc

        return _stream_format_from_asbd(asbd)

    def resolve_tap_uid(self, tap_id: int) -> str:
        """Return a tap's UID, translating stale-tap errors."""
        try:
            return _get_tap_uid(tap_id)
        except OSError as exc:
            _raise_if_missing_tap(tap_id, exc)
            raise

    def create_aggregate_for_taps(
        self,
        tap_ids: Sequence[int],
        *,
        input_device_uids: Sequence[str] = (),
        drift_compensation_quality: DriftCompensationQuality | None = None,
        out: ctypes.c_uint32 | None = None,
    ) -> int:
        """Create the private aggregate device for a multi-track capture."""
        tap_uids = [self.resolve_tap_uid(tap_id) for tap_id in tap_ids]
        return _create_aggregate_device(
            tap_uids,
            "catap Multitrack Recording Device",
            input_device_uids=input_device_uids,
            drift_compensation_quality=drift_compensation_quality,
            out=out,
        )

    def destroy_aggregate_device(self, device_id: int) -> None:
        """Destroy an aggregate device created by this engine."""
        _destroy_aggregate_device(device_id)

    def describe_aggregate_input_streams(
        self,
        device_id: int,
    ) -> list[_TapStreamFormat]:
        """Describe an aggregate's input streams in IOProc buffer order.

        The virtual format of each input stream is exactly the layout the
        IOProc receives for the matching buffer of the input
        AudioBufferList.
        """
        stream_ids = get_property_object_ids(
            device_id,
            kAudioDevicePropertyStreams,
            scope=kAudioObjectPropertyScopeInput,
        )
        formats: list[_TapStreamFormat] = []
        for stream_id in stream_ids:
            asbd = get_property_struct(
                stream_id,
                kAudioStreamPropertyVirtualFormat,
                AudioStreamBasicDescription,
            )
            assert isinstance(asbd, AudioStreamBasicDescription)
            formats.append(_stream_format_from_asbd(asbd))
        return formats

    def attach_io_proc(
        self,
        aggregate_device_id: int,
        callback: object,
        client_data: object | None = None,
    ) -> _TapCaptureSession:
        """Register an IOProc on an aggregate the caller already owns.

        On success the returned session owns both the IOProc and the
        aggregate device, and is published through
        ``failed_capture_session`` until the caller acknowledges it.
        """
        session = _TapCaptureSession(
            aggregate_device_id=aggregate_device_id,
            io_proc_id=ctypes.c_void_p(),
            io_proc_callback=callback,
            client_data=client_data,
        )
        # Publish ownership before Core Audio can retain the client-data
        # pointer, mirroring open_tap_capture's handoff contract.
        self.failed_capture_session = session
        status = _AudioDeviceCreateIOProcID(
            aggregate_device_id,
            callback,
            client_data,
            ctypes.byref(session.io_proc_id),
        )
        if status != 0:
            session.io_proc_destroyed = True
            self.failed_capture_session = None
            raise OSError(f"Failed to create IO proc: status {status}")
        return session

    def open_tap_capture(
        self,
        tap_id: int,
        callback: object,
        client_data: object | None = None,
        *,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> _TapCaptureSession:
        """Create the aggregate device and IOProc for a recorder session."""
        self.failed_capture_session = None
        try:
            tap_uid = _get_tap_uid(tap_id)
        except OSError as exc:
            _raise_if_missing_tap(tap_id, exc)
            raise

        cleanup_errors: list[BaseException] = []
        aggregate_device_id: int | None = None
        aggregate_device_box = ctypes.c_uint32(0)
        session: _TapCaptureSession | None = None

        try:
            if drift_compensation_quality is None:
                aggregate_device_id = _create_aggregate_device_for_tap(
                    tap_uid,
                    "catap Recording Device",
                    out=aggregate_device_box,
                )
            else:
                aggregate_device_id = _create_aggregate_device_for_tap(
                    tap_uid,
                    "catap Recording Device",
                    drift_compensation_quality=drift_compensation_quality,
                    out=aggregate_device_box,
                )
            # Allocate every Python owner before handing Core Audio the client-data
            # pointer. After successful registration, returning this already-built
            # session cannot lose the IOProc ID to a Python allocation failure.
            session = _TapCaptureSession(
                aggregate_device_id=aggregate_device_id,
                io_proc_id=ctypes.c_void_p(),
                io_proc_callback=callback,
                client_data=client_data,
            )
            # Publish ownership before Core Audio can retain the client-data
            # pointer. The recorder acknowledges it after storing its own
            # reference, closing the return-value handoff window.
            self.failed_capture_session = session
            status = _AudioDeviceCreateIOProcID(
                aggregate_device_id,
                callback,
                client_data,
                ctypes.byref(session.io_proc_id),
            )
            if status != 0:
                raise OSError(f"Failed to create IO proc: status {status}")

            return session
        except BaseException as exc:
            if aggregate_device_id is None and aggregate_device_box.value:
                # The device exists but its ID was lost to an interruption
                # before it could be stored; recover it for cleanup below.
                aggregate_device_id = aggregate_device_box.value
            if session is not None:
                if session.io_proc_id.value is None:
                    session.io_proc_destroyed = True
                else:
                    try:
                        _destroy_io_proc(
                            session.aggregate_device_id,
                            session.io_proc_id,
                        )
                    except BaseException as cleanup_exc:
                        cleanup_errors.append(cleanup_exc)
                    else:
                        session.io_proc_destroyed = True

            if aggregate_device_id is not None:
                try:
                    _destroy_aggregate_device(aggregate_device_id)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(cleanup_exc)
                else:
                    if session is not None:
                        session.aggregate_device_destroyed = True

            if session is not None and (
                session.io_proc_destroyed and session.aggregate_device_destroyed
            ):
                self.failed_capture_session = None

            for cleanup_exc in cleanup_errors:
                _add_secondary_failure(
                    exc,
                    "Cleanup failure while opening capture engine",
                    cleanup_exc,
                )
            raise

    def acknowledge_capture_session(self, session: _TapCaptureSession) -> None:
        """Acknowledge that the recorder now owns a returned capture session."""
        if self.failed_capture_session is session:
            self.failed_capture_session = None

    def start(self, session: _TapCaptureSession) -> None:
        """Start the device associated with an open capture session."""
        status = _AudioDeviceStart(session.aggregate_device_id, session.io_proc_id)
        if status != 0:
            raise OSError(f"Failed to start audio device: status {status}")
        session.started = True

    def stop(self, session: _TapCaptureSession) -> None:
        """Stop a running capture session."""
        if not session.started:
            return
        _stop_audio_device(session.aggregate_device_id, session.io_proc_id)
        session.started = False

    def close(self, session: _TapCaptureSession) -> None:
        """Destroy the IOProc and aggregate device for a capture session."""
        cleanup_errors: list[BaseException] = []

        try:
            self.stop(session)
        except BaseException as exc:
            cleanup_errors.append(exc)

        if not session.io_proc_destroyed:
            try:
                _destroy_io_proc(session.aggregate_device_id, session.io_proc_id)
            except BaseException as exc:
                cleanup_errors.append(exc)
            else:
                session.io_proc_destroyed = True
                session.started = False

        if not session.aggregate_device_destroyed:
            try:
                _destroy_aggregate_device(session.aggregate_device_id)
            except BaseException as exc:
                cleanup_errors.append(exc)
            else:
                session.aggregate_device_destroyed = True

        if cleanup_errors:
            raise _combine_errors(
                "Failed to close tap capture session",
                cleanup_errors,
            )
        self.acknowledge_capture_session(session)
