"""ctypes loader for catap's native CoreAudio library."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_LIBRARY_NAME: Final = "libcatap_coreaudio.dylib"
_ENV_LIBRARY_PATH: Final = "CATAP_NATIVE_COREAUDIO_PATH"
_ABI_VERSION: Final = 2

CATAP_MAX_BUFFERS: Final = 16

CATAP_STATUS_OK: Final = 0
CATAP_STATUS_RING_FULL: Final = 1
CATAP_STATUS_RING_EMPTY: Final = 2
CATAP_STATUS_BUFFER_TOO_SMALL: Final = 3
CATAP_STATUS_BUFFER_TOO_LARGE: Final = 4
CATAP_STATUS_INVALID_ARGUMENT: Final = -1
CATAP_STATUS_OUT_OF_MEMORY: Final = -2
CATAP_STATUS_UNSUPPORTED_AUDIO_LAYOUT: Final = -3
CATAP_STATUS_INVALID_AUDIO_BUFFER: Final = -4
CATAP_CHUNK_HAS_INPUT_SAMPLE_TIME: Final = 1


class NativeCoreAudioUnavailable(RuntimeError):
    """Raised when the native CoreAudio dylib cannot be loaded."""


class NativeCoreAudioError(RuntimeError):
    """Raised when the native CoreAudio dylib reports an unexpected failure."""

    def __init__(self, status: int, status_name: str) -> None:
        super().__init__(f"Native CoreAudio call failed: {status_name} ({status})")
        self.status = status
        self.status_name = status_name


class _AudioChunkInfo(ctypes.Structure):
    _fields_ = [
        ("byte_count", ctypes.c_uint32),
        ("frame_count", ctypes.c_uint32),
        ("buffer_index", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("input_sample_time", ctypes.c_double),
    ]


class _AudioRingStats(ctypes.Structure):
    _fields_ = [
        ("slot_count", ctypes.c_uint32),
        ("slot_capacity", ctypes.c_uint32),
        ("queued_chunks", ctypes.c_uint32),
        ("dropped_chunks", ctypes.c_uint64),
        ("dropped_frames", ctypes.c_uint64),
        ("oversized_chunks", ctypes.c_uint64),
    ]


class _RecorderConfig(ctypes.Structure):
    _fields_ = [
        ("slot_count", ctypes.c_uint32),
        ("slot_capacity", ctypes.c_uint32),
        ("buffer_count", ctypes.c_uint32),
        ("expected_channel_count", ctypes.c_uint32 * CATAP_MAX_BUFFERS),
        ("bytes_per_frame", ctypes.c_uint32 * CATAP_MAX_BUFFERS),
    ]


class _RecorderStats(ctypes.Structure):
    _fields_ = [
        ("captured_chunks", ctypes.c_uint64),
        ("captured_frames", ctypes.c_uint64),
        ("callback_failures", ctypes.c_uint64),
        ("last_error_status", ctypes.c_int32),
        ("ring", _AudioRingStats),
    ]


@dataclass(frozen=True, slots=True)
class NativeAudioChunk:
    """One audio chunk read from the native SPSC ring."""

    data: bytes
    frame_count: int
    input_sample_time: float | None
    buffer_index: int = 0


@dataclass(frozen=True, slots=True)
class NativeAudioRingStats:
    """Counters exported by the native SPSC ring."""

    slot_count: int
    slot_capacity: int
    queued_chunks: int
    dropped_chunks: int
    dropped_frames: int
    oversized_chunks: int


@dataclass(frozen=True, slots=True)
class NativeCoreAudioRecorderStats:
    """Counters exported by the native recorder IOProc."""

    captured_chunks: int
    captured_frames: int
    callback_failures: int
    last_error_status: int
    last_error_name: str
    ring: NativeAudioRingStats


class NativeCoreAudioLibrary:
    """Loaded native CoreAudio dylib with ctypes signatures attached."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._cdll = ctypes.CDLL(str(self.path))
        except OSError as exc:
            raise NativeCoreAudioUnavailable(
                f"Failed to load native CoreAudio dylib at {self.path}: {exc}"
            ) from exc
        self._bind()
        version = self.abi_version()
        if version != _ABI_VERSION:
            raise NativeCoreAudioUnavailable(
                "Unsupported native CoreAudio ABI version "
                f"{version}; expected {_ABI_VERSION}"
            )

    def _bind(self) -> None:
        lib = self._cdll

        lib.catap_abi_version.argtypes = []
        lib.catap_abi_version.restype = ctypes.c_uint32

        lib.catap_status_name.argtypes = [ctypes.c_int32]
        lib.catap_status_name.restype = ctypes.c_char_p

        lib.catap_audio_ring_create.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.catap_audio_ring_create.restype = ctypes.c_int32

        lib.catap_audio_ring_destroy.argtypes = [ctypes.c_void_p]
        lib.catap_audio_ring_destroy.restype = None

        lib.catap_audio_ring_reset.argtypes = [ctypes.c_void_p]
        lib.catap_audio_ring_reset.restype = None

        lib.catap_audio_ring_try_write.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_double,
            ctypes.c_uint32,
        ]
        lib.catap_audio_ring_try_write.restype = ctypes.c_int32

        lib.catap_audio_ring_try_read.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_AudioChunkInfo),
        ]
        lib.catap_audio_ring_try_read.restype = ctypes.c_int32

        lib.catap_audio_ring_stats.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_AudioRingStats),
        ]
        lib.catap_audio_ring_stats.restype = ctypes.c_int32

        lib.catap_recorder_create.argtypes = [
            ctypes.POINTER(_RecorderConfig),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.catap_recorder_create.restype = ctypes.c_int32

        lib.catap_recorder_destroy.argtypes = [ctypes.c_void_p]
        lib.catap_recorder_destroy.restype = None

        lib.catap_recorder_reset.argtypes = [ctypes.c_void_p]
        lib.catap_recorder_reset.restype = None

        lib.catap_recorder_read.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_AudioChunkInfo),
        ]
        lib.catap_recorder_read.restype = ctypes.c_int32

        lib.catap_recorder_stats.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_RecorderStats),
        ]
        lib.catap_recorder_stats.restype = ctypes.c_int32

        lib.catap_recorder_io_proc.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        lib.catap_recorder_io_proc.restype = ctypes.c_int32

    @property
    def cdll(self) -> ctypes.CDLL:
        return self._cdll

    def abi_version(self) -> int:
        return int(self._cdll.catap_abi_version())

    def status_name(self, status: int) -> str:
        raw = self._cdll.catap_status_name(status)
        if raw is None:
            return "UNKNOWN"
        return raw.decode("ascii")

    def raise_for_status(self, status: int) -> None:
        if status != CATAP_STATUS_OK:
            raise NativeCoreAudioError(status, self.status_name(status))


class NativeAudioRing:
    """Small Python owner for the native SPSC audio ring."""

    def __init__(
        self,
        slot_count: int,
        slot_capacity: int,
        *,
        library: NativeCoreAudioLibrary | None = None,
    ) -> None:
        self._library = load_native_coreaudio() if library is None else library
        self._handle = ctypes.c_void_p()
        status = self._library.cdll.catap_audio_ring_create(
            slot_count,
            slot_capacity,
            ctypes.byref(self._handle),
        )
        self._library.raise_for_status(status)
        self._slot_capacity = slot_capacity

    def close(self) -> None:
        handle = self._handle
        if handle.value is None:
            return
        self._handle = ctypes.c_void_p()
        self._library.cdll.catap_audio_ring_destroy(handle)

    def __enter__(self) -> NativeAudioRing:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.close()

    def __del__(self) -> None:
        if hasattr(self, "_handle") and hasattr(self, "_library"):
            self.close()

    def reset(self) -> None:
        self._library.cdll.catap_audio_ring_reset(self._handle)

    def write(
        self,
        data: Any,
        *,
        frame_count: int,
        buffer_index: int = 0,
        input_sample_time: float | None = None,
    ) -> int:
        raw_data = bytes(data)
        byte_count = len(raw_data)
        source_buffer: ctypes.Array[ctypes.c_ubyte] | None = None
        if byte_count == 0:
            source = ctypes.c_void_p()
        else:
            source_buffer = (ctypes.c_ubyte * byte_count).from_buffer_copy(raw_data)
            source = ctypes.cast(source_buffer, ctypes.c_void_p)
        flags = (
            CATAP_CHUNK_HAS_INPUT_SAMPLE_TIME
            if input_sample_time is not None
            else 0
        )
        return int(
            self._library.cdll.catap_audio_ring_try_write(
                self._handle,
                source,
                byte_count,
                frame_count,
                buffer_index,
                0.0 if input_sample_time is None else input_sample_time,
                flags,
            )
        )

    def read(self, *, max_bytes: int | None = None) -> NativeAudioChunk | None:
        capacity = self._slot_capacity if max_bytes is None else max_bytes
        destination = (ctypes.c_ubyte * capacity)()
        info = _AudioChunkInfo()
        status = int(
            self._library.cdll.catap_audio_ring_try_read(
                self._handle,
                destination,
                capacity,
                ctypes.byref(info),
            )
        )
        if status == CATAP_STATUS_RING_EMPTY:
            return None
        self._library.raise_for_status(status)

        sample_time = (
            info.input_sample_time
            if info.flags & CATAP_CHUNK_HAS_INPUT_SAMPLE_TIME
            else None
        )
        return NativeAudioChunk(
            data=bytes(memoryview(destination)[: info.byte_count]),
            frame_count=info.frame_count,
            input_sample_time=sample_time,
            buffer_index=info.buffer_index,
        )

    def stats(self) -> NativeAudioRingStats:
        stats = _AudioRingStats()
        status = self._library.cdll.catap_audio_ring_stats(
            self._handle,
            ctypes.byref(stats),
        )
        self._library.raise_for_status(status)
        return _ring_stats_from_native(stats)


class NativeCoreAudioRecorder:
    """Owner for the native recorder handle and CoreAudio IOProc callback."""

    def __init__(
        self,
        *,
        slot_count: int,
        slot_capacity: int,
        expected_channel_count: int | Sequence[int],
        bytes_per_frame: int | Sequence[int],
        library: NativeCoreAudioLibrary | None = None,
    ) -> None:
        """Create the native recorder.

        ``expected_channel_count`` and ``bytes_per_frame`` accept either one
        value for the classic single-buffer capture, or equal-length
        sequences describing every buffer of the aggregate device's input
        AudioBufferList in buffer order.
        """
        channel_counts = (
            [expected_channel_count]
            if isinstance(expected_channel_count, int)
            else list(expected_channel_count)
        )
        frame_sizes = (
            [bytes_per_frame]
            if isinstance(bytes_per_frame, int)
            else list(bytes_per_frame)
        )
        if len(channel_counts) != len(frame_sizes):
            raise ValueError(
                "expected_channel_count and bytes_per_frame must describe "
                f"the same buffers; got {len(channel_counts)} and "
                f"{len(frame_sizes)}"
            )
        if not 1 <= len(channel_counts) <= CATAP_MAX_BUFFERS:
            raise ValueError(
                "Native recorder supports 1 to "
                f"{CATAP_MAX_BUFFERS} buffers, got {len(channel_counts)}"
            )
        # The config crosses the ABI as UInt32 fields; ctypes would silently
        # truncate larger values into a tiny (or invalid) ring.
        for name, value in (
            ("slot_count", slot_count),
            ("slot_capacity", slot_capacity),
        ):
            if not 0 < value < 2**32:
                raise ValueError(
                    f"{name} must fit an unsigned 32-bit integer, got {value}"
                )

        self._library = load_native_coreaudio() if library is None else library
        self._handle = ctypes.c_void_p()
        self._slot_capacity = slot_capacity
        config = _RecorderConfig(
            slot_count=slot_count,
            slot_capacity=slot_capacity,
            buffer_count=len(channel_counts),
        )
        for index, (channels, frame_size) in enumerate(
            zip(channel_counts, frame_sizes, strict=True)
        ):
            config.expected_channel_count[index] = channels
            config.bytes_per_frame[index] = frame_size
        status = self._library.cdll.catap_recorder_create(
            ctypes.byref(config),
            ctypes.byref(self._handle),
        )
        self._library.raise_for_status(status)

    @property
    def handle(self) -> ctypes.c_void_p:
        """Opaque recorder handle to pass as CoreAudio client data."""
        return self._handle

    @property
    def io_proc_pointer(self) -> ctypes.c_void_p:
        """Function pointer for ``AudioDeviceCreateIOProcID``."""
        return ctypes.cast(self._library.cdll.catap_recorder_io_proc, ctypes.c_void_p)

    def close(self) -> None:
        handle = self._handle
        if handle.value is None:
            return
        self._handle = ctypes.c_void_p()
        self._library.cdll.catap_recorder_destroy(handle)

    def abandon(self) -> None:
        """Leak native state that Core Audio may still reference.

        This is reserved for failed IOProc deregistration. Clearing the Python
        handle prevents ``__del__`` from freeing client data that a live HAL
        callback may still access; the operating system reclaims it at process
        exit.
        """
        self._handle = ctypes.c_void_p()

    def __enter__(self) -> NativeCoreAudioRecorder:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.close()

    def __del__(self) -> None:
        if hasattr(self, "_handle") and hasattr(self, "_library"):
            self.close()

    def reset(self) -> None:
        self._library.cdll.catap_recorder_reset(self._handle)

    def read(self, *, max_bytes: int | None = None) -> NativeAudioChunk | None:
        capacity = self._slot_capacity if max_bytes is None else max_bytes
        destination = (ctypes.c_ubyte * capacity)()
        info = _AudioChunkInfo()
        status = int(
            self._library.cdll.catap_recorder_read(
                self._handle,
                destination,
                capacity,
                ctypes.byref(info),
            )
        )
        if status == CATAP_STATUS_RING_EMPTY:
            return None
        self._library.raise_for_status(status)

        sample_time = (
            info.input_sample_time
            if info.flags & CATAP_CHUNK_HAS_INPUT_SAMPLE_TIME
            else None
        )
        return NativeAudioChunk(
            data=bytes(memoryview(destination)[: info.byte_count]),
            frame_count=info.frame_count,
            input_sample_time=sample_time,
            buffer_index=info.buffer_index,
        )

    def stats(self) -> NativeCoreAudioRecorderStats:
        stats = _RecorderStats()
        status = self._library.cdll.catap_recorder_stats(
            self._handle,
            ctypes.byref(stats),
        )
        self._library.raise_for_status(status)
        return NativeCoreAudioRecorderStats(
            captured_chunks=stats.captured_chunks,
            captured_frames=stats.captured_frames,
            callback_failures=stats.callback_failures,
            last_error_status=stats.last_error_status,
            last_error_name=self._library.status_name(stats.last_error_status),
            ring=_ring_stats_from_native(stats.ring),
        )


def _ring_stats_from_native(stats: _AudioRingStats) -> NativeAudioRingStats:
    return NativeAudioRingStats(
        slot_count=stats.slot_count,
        slot_capacity=stats.slot_capacity,
        queued_chunks=stats.queued_chunks,
        dropped_chunks=stats.dropped_chunks,
        dropped_frames=stats.dropped_frames,
        oversized_chunks=stats.oversized_chunks,
    )


def _bundled_library_path() -> Path:
    return Path(__file__).with_name("native") / _LIBRARY_NAME


def find_native_coreaudio_path(path: str | Path | None = None) -> Path:
    """Resolve the native dylib path from an explicit path, env var, or bundle."""
    if path is not None:
        return Path(path)

    env_path = os.environ.get(_ENV_LIBRARY_PATH)
    if env_path:
        return Path(env_path)

    return _bundled_library_path()


def load_native_coreaudio(path: str | Path | None = None) -> NativeCoreAudioLibrary:
    """Load catap's native CoreAudio dylib."""
    library_path = find_native_coreaudio_path(path)
    if not library_path.exists():
        raise NativeCoreAudioUnavailable(
            "Native CoreAudio dylib is not available at "
            f"{library_path}. Build it with scripts/build_native_coreaudio.py "
            f"or set {_ENV_LIBRARY_PATH}."
        )
    return NativeCoreAudioLibrary(library_path)
