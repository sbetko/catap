"""Native CoreAudio dylib smoke tests."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

from catap._capture_engine import (
    AudioTimeStamp,
    kAudioTimeStampSampleTimeValid,
)
from catap._native_coreaudio import (
    CATAP_STATUS_BUFFER_TOO_LARGE,
    CATAP_STATUS_BUFFER_TOO_SMALL,
    CATAP_STATUS_OK,
    CATAP_STATUS_RING_FULL,
    CATAP_STATUS_UNSUPPORTED_AUDIO_LAYOUT,
    NativeAudioRing,
    NativeCoreAudioError,
    NativeCoreAudioLibrary,
    NativeCoreAudioRecorder,
    load_native_coreaudio,
)
from catap.bindings._audiotoolbox import (
    AudioBuffer as CoreAudioBuffer,
)


@pytest.fixture(scope="session")
def native_library_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("native-coreaudio") / "libcatap_coreaudio.dylib"
    subprocess.run(
        [
            sys.executable,
            "scripts/build_native_coreaudio.py",
            "--output",
            str(output),
        ],
        check=True,
    )
    return output


@pytest.fixture(scope="session")
def native_library(native_library_path: Path) -> NativeCoreAudioLibrary:
    return load_native_coreaudio(native_library_path)


def _audio_buffer_list_pointer(
    *buffers: tuple[bytes, int],
) -> tuple[ctypes.c_void_p, list[object]]:
    class _TestAudioBufferList(ctypes.Structure):
        _fields_ = [
            ("mNumberBuffers", ctypes.c_uint32),
            ("mBuffers", CoreAudioBuffer * len(buffers)),
        ]

    buffer_list = _TestAudioBufferList()
    buffer_list.mNumberBuffers = len(buffers)
    keepalive: list[object] = [buffer_list]

    for index, (data, channels) in enumerate(buffers):
        data_buffer = (ctypes.c_char * len(data)).from_buffer_copy(data)
        keepalive.append(data_buffer)
        buffer_list.mBuffers[index].mNumberChannels = channels
        buffer_list.mBuffers[index].mDataByteSize = len(data)
        buffer_list.mBuffers[index].mData = ctypes.cast(data_buffer, ctypes.c_void_p)

    return (
        ctypes.cast(ctypes.pointer(buffer_list), ctypes.c_void_p),
        keepalive,
    )


def _timestamp_pointer(sample_time: float) -> tuple[ctypes.c_void_p, AudioTimeStamp]:
    timestamp = AudioTimeStamp()
    timestamp.mSampleTime = sample_time
    timestamp.mFlags = kAudioTimeStampSampleTimeValid
    return ctypes.cast(ctypes.pointer(timestamp), ctypes.c_void_p), timestamp


def _call_io_proc(
    library: NativeCoreAudioLibrary,
    recorder: NativeCoreAudioRecorder,
    input_data: ctypes.c_void_p,
    input_time: ctypes.c_void_p | None = None,
) -> int:
    return int(
        library.cdll.catap_recorder_io_proc(
            0,
            None,
            input_data,
            input_time,
            None,
            None,
            recorder.handle,
        )
    )


def test_loads_native_library(native_library_path: Path) -> None:
    library = load_native_coreaudio(native_library_path)

    assert library.abi_version() == 1
    assert library.status_name(CATAP_STATUS_OK) == "OK"


def test_env_path_can_select_native_library(
    monkeypatch: pytest.MonkeyPatch,
    native_library_path: Path,
) -> None:
    monkeypatch.setenv("CATAP_NATIVE_COREAUDIO_PATH", str(native_library_path))

    library = load_native_coreaudio()

    assert library.path == native_library_path


def test_audio_ring_round_trips_bytes(native_library: NativeCoreAudioLibrary) -> None:
    with NativeAudioRing(2, 16, library=native_library) as ring:
        status = ring.write(b"abcd", frame_count=1, input_sample_time=123.5)

        assert status == CATAP_STATUS_OK
        chunk = ring.read()
        assert chunk is not None
        assert chunk.data == b"abcd"
        assert chunk.frame_count == 1
        assert chunk.input_sample_time == 123.5
        assert ring.read() is None


def test_audio_ring_reports_full_without_consuming(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with NativeAudioRing(1, 8, library=native_library) as ring:
        assert ring.write(b"aaaa", frame_count=4) == CATAP_STATUS_OK
        assert ring.write(b"bbbb", frame_count=4) == CATAP_STATUS_RING_FULL

        stats = ring.stats()
        assert stats.queued_chunks == 1
        assert stats.dropped_chunks == 1
        assert stats.dropped_frames == 4
        chunk = ring.read()
        assert chunk is not None
        assert chunk.data == b"aaaa"


def test_audio_ring_preserves_chunk_when_read_buffer_is_small(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with NativeAudioRing(1, 8, library=native_library) as ring:
        assert ring.write(b"abcdef", frame_count=3) == CATAP_STATUS_OK

        with pytest.raises(NativeCoreAudioError) as exc_info:
            ring.read(max_bytes=4)

        assert exc_info.value.status == CATAP_STATUS_BUFFER_TOO_SMALL
        chunk = ring.read()
        assert chunk is not None
        assert chunk.data == b"abcdef"


def test_audio_ring_rejects_oversized_chunks(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with NativeAudioRing(1, 4, library=native_library) as ring:
        assert ring.write(b"abcde", frame_count=5) == CATAP_STATUS_BUFFER_TOO_LARGE

        stats = ring.stats()
        assert stats.queued_chunks == 0
        assert stats.dropped_chunks == 1
        assert stats.dropped_frames == 5
        assert stats.oversized_chunks == 1


def test_native_recorder_io_proc_copies_core_audio_buffer(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with NativeCoreAudioRecorder(
        slot_count=2,
        slot_capacity=16,
        expected_channel_count=2,
        bytes_per_frame=4,
        library=native_library,
    ) as recorder:
        input_data, keepalive = _audio_buffer_list_pointer((b"abcdefgh", 2))
        input_time, timestamp = _timestamp_pointer(321.5)

        assert recorder.io_proc_pointer.value is not None
        assert _call_io_proc(native_library, recorder, input_data, input_time) == 0

        chunk = recorder.read()
        assert chunk is not None
        assert chunk.data == b"abcdefgh"
        assert chunk.frame_count == 2
        assert chunk.input_sample_time == 321.5
        stats = recorder.stats()
        assert stats.captured_chunks == 1
        assert stats.captured_frames == 2
        assert stats.callback_failures == 0
        assert keepalive
        assert timestamp.mFlags == kAudioTimeStampSampleTimeValid


def test_native_recorder_io_proc_records_layout_failures(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with NativeCoreAudioRecorder(
        slot_count=2,
        slot_capacity=16,
        expected_channel_count=2,
        bytes_per_frame=4,
        library=native_library,
    ) as recorder:
        input_data, keepalive = _audio_buffer_list_pointer((b"abcd", 1))

        assert _call_io_proc(native_library, recorder, input_data) == 0
        assert recorder.read() is None
        stats = recorder.stats()
        assert stats.callback_failures == 1
        assert stats.last_error_status == CATAP_STATUS_UNSUPPORTED_AUDIO_LAYOUT
        assert stats.last_error_name == "UNSUPPORTED_AUDIO_LAYOUT"
        assert keepalive


def test_native_recorder_io_proc_drops_when_ring_is_full(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with NativeCoreAudioRecorder(
        slot_count=1,
        slot_capacity=16,
        expected_channel_count=2,
        bytes_per_frame=4,
        library=native_library,
    ) as recorder:
        first_input_data, first_keepalive = _audio_buffer_list_pointer((b"abcd", 2))
        second_input_data, second_keepalive = _audio_buffer_list_pointer((b"efgh", 2))

        assert _call_io_proc(native_library, recorder, first_input_data) == 0
        assert _call_io_proc(native_library, recorder, second_input_data) == 0
        stats = recorder.stats()
        assert stats.captured_chunks == 1
        assert stats.captured_frames == 1
        assert stats.callback_failures == 0
        assert stats.ring.queued_chunks == 1
        assert stats.ring.dropped_chunks == 1
        assert stats.ring.dropped_frames == 1
        chunk = recorder.read()
        assert chunk is not None
        assert chunk.data == b"abcd"
        assert first_keepalive
        assert second_keepalive
