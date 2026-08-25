"""Multitrack recorder construction and pre-start behavior tests."""

from __future__ import annotations

import ctypes
import threading
import time
import wave
from pathlib import Path
from typing import Any, cast

import pytest

import catap._capture_engine as capture_module
import catap.recorder as recorder_module
from catap._multitrack import MultitrackAudioRecorder
from catap._native_coreaudio import NativeAudioChunk
from catap.bindings._coreaudio import kAudioHardwareBadObjectError
from catap.drift import DriftCompensationQuality


def _paths(tmp_path: Path, count: int) -> list[Path]:
    return [tmp_path / f"track-{index}.wav" for index in range(count)]


def _stream_format() -> capture_module._TapStreamFormat:
    return capture_module._TapStreamFormat(
        48_000.0,
        1,
        16,
        False,
        bytes_per_frame=2,
        is_signed_integer=True,
    )


def _start_test_workers(recorder: MultitrackAudioRecorder) -> None:
    recorder._apply_stream_formats([_stream_format()] * recorder.track_count)
    for index, worker in enumerate(recorder._workers):
        worker.start(recorder._make_worker_config(index))
    recorder._reached_recording = True
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"


class _ChunkRecorder:
    def __init__(self, chunks: list[NativeAudioChunk]) -> None:
        self._chunks = chunks

    def read(self) -> NativeAudioChunk | None:
        return self._chunks.pop(0) if self._chunks else None


def test_rejects_empty_tap_ids() -> None:
    with pytest.raises(ValueError, match="At least one tap is required"):
        MultitrackAudioRecorder([], [])


def test_rejects_output_path_count_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Expected 2 output paths"):
        MultitrackAudioRecorder([11, 12], _paths(tmp_path, 1))


def test_rejects_input_device_without_stream_count(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        MultitrackAudioRecorder(
            [11],
            _paths(tmp_path, 1),
            input_device_uid="BuiltInMicDevice",
        )


def test_rejects_stream_count_without_input_device(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        MultitrackAudioRecorder(
            [11],
            _paths(tmp_path, 2),
            input_stream_count=1,
        )


def test_rejects_more_than_native_buffer_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="up to 16 tracks, got 17"):
        MultitrackAudioRecorder(list(range(1, 18)), _paths(tmp_path, 17))


def test_rejects_tracks_without_any_output_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"track\(s\) \[1, 2\] have neither"):
        MultitrackAudioRecorder(
            [11, 12, 13],
            [tmp_path / "one.wav", None, None],
        )


def test_rejects_non_positive_max_pending_buffers(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match="max_pending_buffers must be greater than 0",
    ):
        MultitrackAudioRecorder(
            [11],
            _paths(tmp_path, 1),
            max_pending_buffers=0,
        )


def test_allows_callback_only_tracks() -> None:
    recorder = MultitrackAudioRecorder(
        [11, 12],
        [None, None],
        on_track_buffer=lambda track_index, buffer: None,
    )

    assert recorder.output_paths == [None, None]
    assert recorder.track_count == 2


def test_track_count_includes_input_device_streams(tmp_path: Path) -> None:
    recorder = MultitrackAudioRecorder(
        [11],
        _paths(tmp_path, 3),
        input_device_uid="BuiltInMicDevice",
        input_stream_count=2,
    )

    assert recorder.track_count == 3
    assert recorder.track_captured_only_silence == (True, True, True)


def test_pre_start_properties_report_idle_state(tmp_path: Path) -> None:
    recorder = MultitrackAudioRecorder([11, 12], _paths(tmp_path, 2))

    assert recorder.track_count == 2
    assert recorder.is_recording is False
    assert recorder.needs_cleanup is False
    assert recorder.frames_recorded == 0
    assert recorder.duration_seconds == 0.0
    assert recorder.stream_formats == []
    assert recorder.captured_only_silence is True
    assert recorder.track_captured_only_silence == (True, True)
    assert recorder.max_pending_buffers == 256
    assert recorder.capture_failed is False
    assert recorder.wait_for_capture_failure(timeout=0) is False


def test_start_threads_drift_quality_to_aggregate_engine(tmp_path: Path) -> None:
    class _QualityCaptureEngine:
        failed_capture_session = None

        def __init__(self) -> None:
            self.quality: DriftCompensationQuality | None = None

        def describe_tap_stream(
            self,
            tap_id: int,
        ) -> capture_module._TapStreamFormat:
            del tap_id
            return _stream_format()

        def create_aggregate_for_taps(
            self,
            tap_ids: list[int],
            *,
            input_device_uids: tuple[str, ...] = (),
            drift_compensation_quality: DriftCompensationQuality | None = None,
            out: ctypes.c_uint32 | None = None,
        ) -> int:
            del tap_ids, input_device_uids, out
            self.quality = drift_compensation_quality
            raise OSError("stop after observing quality")

    engine = _QualityCaptureEngine()
    recorder = MultitrackAudioRecorder(
        [11, 12],
        _paths(tmp_path, 2),
        drift_compensation_quality=DriftCompensationQuality.MAXIMUM,
    )
    recorder._capture_engine = cast(Any, engine)
    recorder._capture_failure_event.set()

    with pytest.raises(OSError, match="stop after observing quality"):
        recorder.start()

    assert engine.quality is DriftCompensationQuality.MAXIMUM
    assert recorder.capture_failed is False


@pytest.mark.parametrize(
    "paths",
    [
        ("same.wav", "same.wav"),
        ("Track.wav", "track.wav"),
    ],
)
def test_rejects_aliased_output_paths(
    tmp_path: Path,
    paths: tuple[str, str],
) -> None:
    with pytest.raises(ValueError, match="same destination"):
        MultitrackAudioRecorder(
            [11, 12],
            [tmp_path / paths[0], tmp_path / paths[1]],
        )


def test_rejects_existing_hardlinked_output_paths(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    first.write_bytes(b"existing")
    second.hardlink_to(first)

    with pytest.raises(ValueError, match="same destination"):
        MultitrackAudioRecorder([11, 12], [first, second])


def test_drain_rejects_unequal_frame_group() -> None:
    recorder = MultitrackAudioRecorder(
        [11, 12],
        [None, None],
        on_track_buffer=lambda track_index, buffer: None,
    )
    recorder._apply_stream_formats([_stream_format(), _stream_format()])
    native = _ChunkRecorder(
        [
            NativeAudioChunk(b"\x01\x00\x02\x00", 2, 10.0, 0),
            NativeAudioChunk(b"\x03\x00", 1, 10.0, 1),
        ]
    )

    with pytest.raises(RuntimeError, match="unequal frame counts"):
        recorder._drain_native_recorder(
            cast(Any, native),
            threading.Event(),
        )


def test_callback_failure_disables_future_callbacks_for_every_track() -> None:
    callback_tracks: list[int] = []

    def on_track_buffer(track_index: int, buffer: object) -> None:
        del buffer
        callback_tracks.append(track_index)
        if track_index == 0:
            raise RuntimeError("callback failed")

    recorder = MultitrackAudioRecorder(
        [11, 12],
        [None, None],
        on_track_buffer=on_track_buffer,
    )
    recorder._apply_stream_formats([_stream_format(), _stream_format()])
    first_callback = recorder._make_worker_config(0).on_buffer
    second_callback = recorder._make_worker_config(1).on_buffer
    assert first_callback is not None
    assert second_callback is not None

    with pytest.raises(RuntimeError, match="callback failed"):
        first_callback(cast(Any, object()))
    second_callback(cast(Any, object()))

    assert callback_tracks == [0]
    assert recorder._capture_is_invalid()


def test_worker_queue_admits_or_drops_whole_group() -> None:
    slow_callback_started = threading.Event()
    release_slow_callback = threading.Event()
    fast_callback_seen = threading.Event()
    seen: list[tuple[int, bytes]] = []

    def on_track_buffer(track_index: int, buffer: object) -> None:
        audio_buffer = cast(Any, buffer)
        seen.append((track_index, audio_buffer.data))
        if track_index == 0:
            slow_callback_started.set()
            assert release_slow_callback.wait(timeout=2)
        else:
            fast_callback_seen.set()

    recorder = MultitrackAudioRecorder(
        [11, 12],
        [None, None],
        on_track_buffer=on_track_buffer,
        max_pending_buffers=1,
    )
    _start_test_workers(recorder)

    first_group = _ChunkRecorder(
        [
            NativeAudioChunk(b"\x01\x00", 1, 10.0, 0),
            NativeAudioChunk(b"\x02\x00", 1, 10.0, 1),
        ]
    )
    second_group = _ChunkRecorder(
        [
            NativeAudioChunk(b"\x03\x00", 1, 11.0, 0),
            NativeAudioChunk(b"\x04\x00", 1, 11.0, 1),
        ]
    )

    try:
        assert recorder._drain_native_recorder(
            cast(Any, first_group), threading.Event()
        )
        assert slow_callback_started.wait(timeout=1)
        assert fast_callback_seen.wait(timeout=1)

        # Confirm the fast track has returned its reservation while the slow
        # track still owns its only slot.
        deadline = time.monotonic() + 1
        while not recorder._workers[1].try_reserve_audio_slot():
            if time.monotonic() >= deadline:
                pytest.fail("fast track did not return its worker reservation")
            threading.Event().wait(0.001)
        recorder._workers[1].cancel_reserved_audio_slot()

        assert recorder._drain_native_recorder(
            cast(Any, second_group), threading.Event()
        )
        assert recorder._capture_is_invalid()
        assert recorder.capture_failed is True
    finally:
        release_slow_callback.set()
        worker_errors = [
            error
            for worker in recorder._workers
            for error in worker.drain_and_collect()
        ]
        for worker in recorder._workers:
            worker.finalize(publish=False)

    assert len(worker_errors) == 2
    assert all("Dropped 1 audio buffer" in str(error) for error in worker_errors)
    assert sorted(seen) == [(0, b"\x01\x00"), (1, b"\x02\x00")]
    assert recorder._accepted_frames == [1, 1]


def test_failed_capture_remains_unpublishable_across_cleanup_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_paths = _paths(tmp_path, 2)

    def on_track_buffer(track_index: int, buffer: object) -> None:
        del buffer
        if track_index == 0:
            raise RuntimeError("callback failed")

    recorder = MultitrackAudioRecorder(
        [11, 12],
        output_paths,
        on_track_buffer=on_track_buffer,
    )
    _start_test_workers(recorder)
    for worker in recorder._workers:
        assert worker.enqueue_audio_bytes(b"\x01\x00", 1)

    first_worker = recorder._workers[0]
    real_discard = first_worker._discard_temporary_output
    monkeypatch.setattr(
        first_worker,
        "_discard_temporary_output",
        lambda state: OSError("temporary unlink failed"),
    )

    with pytest.raises(RuntimeError, match="callback failed"):
        recorder.stop()

    assert recorder.needs_cleanup
    assert recorder._capture_is_invalid()
    assert not any(path.exists() for path in output_paths)

    monkeypatch.setattr(first_worker, "_discard_temporary_output", real_discard)
    recorder.stop()

    assert not any(path.exists() for path in output_paths)
    assert recorder.needs_cleanup is False


def test_worker_failure_stays_sticky_if_error_collection_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_paths = _paths(tmp_path, 2)

    def on_track_buffer(track_index: int, buffer: object) -> None:
        del buffer
        if track_index == 0:
            raise RuntimeError("callback failed")

    recorder = MultitrackAudioRecorder(
        [11, 12],
        output_paths,
        on_track_buffer=on_track_buffer,
    )
    _start_test_workers(recorder)
    for worker in recorder._workers:
        assert worker.enqueue_audio_bytes(b"\x01\x00", 1)

    first_worker = recorder._workers[0]
    real_drain = first_worker.drain_and_collect

    def interrupt_after_consuming_errors() -> list[BaseException]:
        real_drain()
        raise KeyboardInterrupt("after worker errors were consumed")

    monkeypatch.setattr(
        first_worker,
        "drain_and_collect",
        interrupt_after_consuming_errors,
    )
    with pytest.raises(KeyboardInterrupt, match="errors were consumed"):
        recorder.stop()

    assert first_worker.capture_is_invalid
    assert recorder.needs_cleanup

    monkeypatch.setattr(first_worker, "drain_and_collect", real_drain)
    recorder.stop()

    assert not any(path.exists() for path in output_paths)
    assert recorder.needs_cleanup is False


def test_publication_failure_restores_every_preexisting_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_paths = _paths(tmp_path, 2)
    original_contents = [b"original-one", b"original-two"]
    for path, content in zip(output_paths, original_contents, strict=True):
        path.write_bytes(content)

    recorder = MultitrackAudioRecorder([11, 12], output_paths)
    _start_test_workers(recorder)
    for worker, data in zip(
        recorder._workers,
        (b"\x01\x00", b"\x02\x00"),
        strict=True,
    ):
        assert worker.enqueue_audio_bytes(data, 1)

    monkeypatch.setattr(
        recorder._workers[1],
        "publish_staged_output",
        lambda: (_ for _ in ()).throw(OSError("second publish failed")),
    )

    with pytest.raises(OSError, match="second publish failed"):
        recorder.stop()

    assert [path.read_bytes() for path in output_paths] == original_contents
    assert list(tmp_path.glob("*.catap-backup")) == []
    assert recorder.needs_cleanup is False


def test_successful_publication_commits_every_track_and_removes_backups(
    tmp_path: Path,
) -> None:
    output_paths = _paths(tmp_path, 2)
    for path in output_paths:
        path.write_bytes(b"preexisting")

    recorder = MultitrackAudioRecorder([11, 12], output_paths)
    _start_test_workers(recorder)
    for worker, data in zip(
        recorder._workers,
        (b"\x01\x00", b"\x02\x00"),
        strict=True,
    ):
        assert worker.enqueue_audio_bytes(data, 1)

    recorder.stop()

    captured_data: list[bytes] = []
    for path in output_paths:
        with wave.open(str(path), "rb") as wav_file:
            assert wav_file.getnframes() == 1
            captured_data.append(wav_file.readframes(1))
    assert captured_data == [b"\x01\x00", b"\x02\x00"]
    assert list(tmp_path.glob("*.catap-backup")) == []
    assert recorder.needs_cleanup is False


def test_rollback_retries_backup_restore_and_preserves_originals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_paths = _paths(tmp_path, 2)
    original_contents = [b"original-one", b"original-two"]
    for path, content in zip(output_paths, original_contents, strict=True):
        path.write_bytes(content)

    recorder = MultitrackAudioRecorder([11, 12], output_paths)
    _start_test_workers(recorder)
    for worker, data in zip(
        recorder._workers,
        (b"\x01\x00", b"\x02\x00"),
        strict=True,
    ):
        assert worker.enqueue_audio_bytes(data, 1)

    def fail_second_publish() -> None:
        raise OSError("second publish failed")

    monkeypatch.setattr(
        recorder._workers[1],
        "publish_staged_output",
        fail_second_publish,
    )
    real_replace = Path.replace
    restore_attempts = 0

    def fail_first_restore(source: Path, target: Path) -> Path:
        nonlocal restore_attempts
        if source.name.endswith(".catap-backup"):
            restore_attempts += 1
            if restore_attempts == 1:
                raise OSError("backup restore failed")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_first_restore)

    with pytest.raises(OSError, match="second publish failed"):
        recorder.stop()

    assert recorder.needs_cleanup
    assert output_paths[0].exists() is False
    assert output_paths[1].read_bytes() == original_contents[1]
    assert list(tmp_path.glob("*.catap-backup"))

    recorder.stop()

    assert [path.read_bytes() for path in output_paths] == original_contents
    assert list(tmp_path.glob("*.catap-backup")) == []
    assert restore_attempts == 2
    assert recorder.needs_cleanup is False


def test_commit_retries_backup_unlink_without_rolling_back_published_wavs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_paths = _paths(tmp_path, 2)
    for path in output_paths:
        path.write_bytes(b"preexisting")

    recorder = MultitrackAudioRecorder([11, 12], output_paths)
    _start_test_workers(recorder)
    for worker, data in zip(
        recorder._workers,
        (b"\x01\x00", b"\x02\x00"),
        strict=True,
    ):
        assert worker.enqueue_audio_bytes(data, 1)

    real_unlink = Path.unlink
    backup_unlink_attempts = 0

    def fail_first_backup_unlink(
        path: Path,
        missing_ok: bool = False,
    ) -> None:
        nonlocal backup_unlink_attempts
        if path.name.endswith(".catap-backup"):
            backup_unlink_attempts += 1
            if backup_unlink_attempts == 1:
                raise OSError("backup unlink failed")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_first_backup_unlink)

    with pytest.raises(OSError, match="backup unlink failed"):
        recorder.stop()

    assert recorder.needs_cleanup
    first_stop_contents = [path.read_bytes() for path in output_paths]
    assert all(content.startswith(b"RIFF") for content in first_stop_contents)

    recorder.stop()

    assert [path.read_bytes() for path in output_paths] == first_stop_contents
    assert list(tmp_path.glob("*.catap-backup")) == []
    assert backup_unlink_attempts >= 2
    assert recorder.needs_cleanup is False


def test_stop_retries_bare_aggregate_cleanup_without_losing_id() -> None:
    class _RetryingAggregateEngine:
        def __init__(self) -> None:
            self.destroy_calls: list[int] = []

        def destroy_aggregate_device(self, device_id: int) -> None:
            self.destroy_calls.append(device_id)
            if len(self.destroy_calls) == 1:
                raise OSError("aggregate cleanup failed")

    engine = _RetryingAggregateEngine()
    recorder = MultitrackAudioRecorder(
        [11],
        [None],
        on_track_buffer=lambda track_index, buffer: None,
    )
    recorder._capture_engine = cast(Any, engine)
    recorder._aggregate_device_id = 55
    recorder._lifecycle_state = "cleanup_failed"

    with pytest.raises(OSError, match="aggregate cleanup failed"):
        recorder.stop()

    assert recorder._aggregate_device_id == 55
    assert recorder.needs_cleanup

    recorder.stop()

    assert engine.destroy_calls == [55, 55]
    assert recorder._aggregate_device_id is None
    assert recorder.needs_cleanup is False


def test_stop_accepts_bad_object_after_interrupted_aggregate_destroy() -> None:
    class _AmbiguousAggregateEngine:
        def __init__(self) -> None:
            self.destroy_calls: list[int] = []

        def destroy_aggregate_device(self, device_id: int) -> None:
            self.destroy_calls.append(device_id)
            if len(self.destroy_calls) == 1:
                raise KeyboardInterrupt("interrupted after native destroy")
            error = OSError("aggregate no longer exists")
            error.status = kAudioHardwareBadObjectError  # type: ignore[attr-defined]
            raise error

    engine = _AmbiguousAggregateEngine()
    recorder = MultitrackAudioRecorder(
        [11],
        [None],
        on_track_buffer=lambda track_index, buffer: None,
    )
    recorder._capture_engine = cast(Any, engine)
    recorder._aggregate_device_id = 55
    recorder._lifecycle_state = "cleanup_failed"

    with pytest.raises(KeyboardInterrupt, match="interrupted after native destroy"):
        recorder.stop()

    assert recorder._aggregate_device_id == 55
    assert recorder.needs_cleanup

    recorder.stop()

    assert engine.destroy_calls == [55, 55]
    assert recorder._aggregate_device_id is None
    assert recorder.needs_cleanup is False


def test_stop_retries_drift_watch_close() -> None:
    class _RetryingWatch:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("listener removal failed")

    watch = _RetryingWatch()
    recorder = MultitrackAudioRecorder(
        [11],
        [None],
        on_track_buffer=lambda track_index, buffer: None,
    )
    recorder._drift_watch = cast(Any, watch)
    recorder._lifecycle_state = "cleanup_failed"

    with pytest.raises(OSError, match="listener removal failed"):
        recorder.stop()

    assert recorder._drift_watch is watch
    assert recorder.needs_cleanup

    recorder.stop()

    assert watch.close_calls == 2
    assert recorder._drift_watch is None
    assert recorder.needs_cleanup is False


def test_native_retention_interruption_keeps_retryable_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeNativeRecorder:
        def __init__(self) -> None:
            self.abandon_calls = 0

        def abandon(self) -> None:
            self.abandon_calls += 1

        def close(self) -> None:
            raise AssertionError("unsafe native recorder must not be freed")

    class _InterruptingList(list[object]):
        def append(self, value: object) -> None:
            del value
            raise KeyboardInterrupt("retention interrupted")

    capture_session = capture_module._TapCaptureSession(
        55,
        ctypes.c_void_p(77),
    )
    native_recorder = _FakeNativeRecorder()
    recorder = MultitrackAudioRecorder(
        [11],
        [None],
        on_track_buffer=lambda track_index, buffer: None,
    )
    recorder._capture_session = capture_session
    recorder._native_recorder = cast(Any, native_recorder)
    monkeypatch.setattr(
        recorder_module,
        "_ABANDONED_NATIVE_CAPTURES",
        _InterruptingList(),
    )

    errors = recorder._release_native_recorder(drain_quiesced=True)

    assert len(errors) == 2
    assert recorder._native_recorder is native_recorder
    assert native_recorder.abandon_calls == 0

    retained: list[object] = []
    monkeypatch.setattr(recorder_module, "_ABANDONED_NATIVE_CAPTURES", retained)
    retry_errors = recorder._release_native_recorder(drain_quiesced=True)

    assert len(retry_errors) == 1
    assert "Retained native recorder state" in str(retry_errors[0])
    assert recorder._native_recorder is None
    assert retained == [(capture_session, native_recorder)]
    assert native_recorder.abandon_calls == 1
