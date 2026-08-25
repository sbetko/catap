"""Native CoreAudio dylib smoke tests."""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from catap._capture_engine import (
    AudioTimeStamp,
    kAudioTimeStampSampleTimeValid,
)
from catap._native_coreaudio import (
    CATAP_STATUS_BUFFER_TOO_LARGE,
    CATAP_STATUS_BUFFER_TOO_SMALL,
    CATAP_STATUS_INVALID_AUDIO_BUFFER,
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

    assert library.abi_version() == 2
    assert library.status_name(CATAP_STATUS_OK) == "OK"


def test_editable_build_generates_bundled_native_library(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    editable_project = tmp_path / "editable-project"
    editable_project.mkdir()

    for filename in (
        "CHANGELOG.md",
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "pyproject.toml",
        "setup.py",
    ):
        shutil.copy2(project_root / filename, editable_project / filename)
    for directory in ("native", "scripts", "src"):
        shutil.copytree(
            project_root / directory,
            editable_project / directory,
            ignore=shutil.ignore_patterns("*.dylib", "*.egg-info", "__pycache__"),
        )

    bundled_library = (
        editable_project
        / "src"
        / "catap"
        / "native"
        / "libcatap_coreaudio.dylib"
    )
    assert not bundled_library.exists()

    build = subprocess.run(
        [
            sys.executable,
            "-c",
            "from setuptools.build_meta import build_editable; "
            "build_editable('wheelhouse')",
        ],
        cwd=editable_project,
        capture_output=True,
        text=True,
        check=False,
    )

    assert build.returncode == 0, build.stdout + build.stderr
    assert bundled_library.is_file()
    assert load_native_coreaudio(bundled_library).abi_version() == 2


def test_sdist_excludes_editable_native_build_output(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    source_project = tmp_path / "source-project"
    source_project.mkdir()

    for filename in (
        "CHANGELOG.md",
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "pyproject.toml",
        "setup.py",
    ):
        shutil.copy2(project_root / filename, source_project / filename)
    for directory in ("native", "scripts", "src"):
        shutil.copytree(
            project_root / directory,
            source_project / directory,
            ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"),
        )

    generated_library = (
        source_project
        / "src"
        / "catap"
        / "native"
        / "libcatap_coreaudio.dylib"
    )
    generated_library.parent.mkdir(parents=True, exist_ok=True)
    generated_library.write_bytes(b"editable build output")

    build = subprocess.run(
        [
            sys.executable,
            "-c",
            "from setuptools.build_meta import build_sdist; "
            "build_sdist('dist')",
        ],
        cwd=source_project,
        capture_output=True,
        text=True,
        check=False,
    )

    assert build.returncode == 0, build.stdout + build.stderr
    sdist = next((source_project / "dist").glob("catap-*.tar.gz"))
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getnames()
    assert not any(member.endswith(".dylib") for member in members)
    assert any(member.endswith("catap_coreaudio.c") for member in members)
    assert any(member.endswith("catap_coreaudio.h") for member in members)


def test_abandoned_recorder_handle_is_not_destroyed() -> None:
    destroyed_handles: list[int] = []

    class _FakeCdll:
        @staticmethod
        def catap_recorder_destroy(handle: ctypes.c_void_p) -> None:
            assert handle.value is not None
            destroyed_handles.append(handle.value)

    class _FakeLibrary:
        cdll = _FakeCdll()

    recorder = NativeCoreAudioRecorder.__new__(NativeCoreAudioRecorder)
    recorder._library = _FakeLibrary()
    recorder._handle = ctypes.c_void_p(123)

    recorder.abandon()
    recorder.close()

    assert recorder.handle.value is None
    assert destroyed_handles == []


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


def test_audio_ring_round_trips_buffer_index(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with NativeAudioRing(2, 16, library=native_library) as ring:
        status = ring.write(b"abcd", frame_count=1, buffer_index=3)

        assert status == CATAP_STATUS_OK
        chunk = ring.read()
        assert chunk is not None
        assert chunk.data == b"abcd"
        assert chunk.buffer_index == 3


def test_native_recorder_accepts_per_buffer_layout_sequences(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with NativeCoreAudioRecorder(
        slot_count=2,
        slot_capacity=32,
        expected_channel_count=[2, 1],
        bytes_per_frame=[8, 4],
        library=native_library,
    ) as recorder:
        assert recorder.handle.value is not None
        assert recorder.io_proc_pointer.value is not None


def test_native_recorder_rejects_mismatched_layout_sequences(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with pytest.raises(ValueError, match="must describe the same buffers"):
        NativeCoreAudioRecorder(
            slot_count=2,
            slot_capacity=32,
            expected_channel_count=[2, 1],
            bytes_per_frame=[8],
            library=native_library,
        )


def test_native_recorder_rejects_too_many_buffers(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with pytest.raises(ValueError, match="1 to 16 buffers, got 17"):
        NativeCoreAudioRecorder(
            slot_count=2,
            slot_capacity=32,
            expected_channel_count=[2] * 17,
            bytes_per_frame=[8] * 17,
            library=native_library,
        )


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


def test_native_recorder_io_proc_publishes_equal_frame_multibuffer_group(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with NativeCoreAudioRecorder(
        slot_count=4,
        slot_capacity=16,
        expected_channel_count=[2, 1],
        bytes_per_frame=[4, 2],
        library=native_library,
    ) as recorder:
        input_data, keepalive = _audio_buffer_list_pointer(
            (b"abcdefgh", 2),
            (b"ijkl", 1),
        )

        assert _call_io_proc(native_library, recorder, input_data) == 0
        chunks = [recorder.read(), recorder.read()]
        assert [chunk.buffer_index for chunk in chunks if chunk is not None] == [
            0,
            1,
        ]
        assert [chunk.frame_count for chunk in chunks if chunk is not None] == [
            2,
            2,
        ]
        assert recorder.read() is None
        stats = recorder.stats()
        assert stats.captured_chunks == 2
        assert stats.captured_frames == 4
        assert stats.callback_failures == 0
        assert keepalive


@pytest.mark.parametrize(
    ("first_data", "second_data"),
    [
        (b"abcdefgh", b"ijkl"),
        (b"abcdefgh", b""),
        (b"", b"ijklmnop"),
    ],
)
def test_native_recorder_io_proc_rejects_unequal_frame_multibuffer_group(
    native_library: NativeCoreAudioLibrary,
    first_data: bytes,
    second_data: bytes,
) -> None:
    with NativeCoreAudioRecorder(
        slot_count=4,
        slot_capacity=16,
        expected_channel_count=[2, 1],
        bytes_per_frame=[4, 4],
        library=native_library,
    ) as recorder:
        input_data, keepalive = _audio_buffer_list_pointer(
            (first_data, 2),
            (second_data, 1),
        )

        assert _call_io_proc(native_library, recorder, input_data) == 0
        assert recorder.read() is None
        stats = recorder.stats()
        assert stats.captured_chunks == 0
        assert stats.captured_frames == 0
        assert stats.callback_failures == 1
        assert stats.last_error_status == CATAP_STATUS_INVALID_AUDIO_BUFFER
        assert stats.last_error_name == "INVALID_AUDIO_BUFFER"
        assert keepalive


def test_native_recorder_io_proc_ignores_entirely_empty_multibuffer_group(
    native_library: NativeCoreAudioLibrary,
) -> None:
    with NativeCoreAudioRecorder(
        slot_count=4,
        slot_capacity=16,
        expected_channel_count=[2, 1],
        bytes_per_frame=[4, 4],
        library=native_library,
    ) as recorder:
        input_data, keepalive = _audio_buffer_list_pointer(
            (b"", 99),
            (b"", 0),
        )

        assert _call_io_proc(native_library, recorder, input_data) == 0
        assert recorder.read() is None
        stats = recorder.stats()
        assert stats.captured_chunks == 0
        assert stats.callback_failures == 0
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
