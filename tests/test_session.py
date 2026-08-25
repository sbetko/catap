"""High-level recording session tests."""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import catap.session as session_module
from catap.audio_buffer import AudioStreamFormat
from catap.bindings._coreaudio import kAudioHardwareBadObjectError
from catap.bindings.process import AmbiguousAudioProcessError, AudioProcess
from catap.bindings.tap import AudioTap, AudioTapNotFoundError
from catap.bindings.tap_description import TapDescription, TapMuteBehavior
from catap.drift import DriftCompensationQuality


class _FakeTapDescription:
    def __init__(
        self,
        processes: list[int],
        *,
        exclusive: bool = False,
        bundle_ids: list[str] | None = None,
    ) -> None:
        self.processes = processes
        self.is_exclusive = exclusive
        self.bundle_ids = [] if bundle_ids is None else list(bundle_ids)
        self.name = ""
        self.is_private = False
        self.mute_behavior = None

    @classmethod
    def stereo_mixdown_of_processes(cls, processes: list[int]) -> _FakeTapDescription:
        return cls(list(processes))

    @classmethod
    def stereo_global_tap_excluding(cls, processes: list[int]) -> _FakeTapDescription:
        return cls(list(processes), exclusive=True)


class _FakeRecorder:
    def __init__(
        self,
        tap_id: int,
        output_path: Path | None,
        on_buffer: object = None,
        *,
        max_pending_buffers: int = 256,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> None:
        self.tap_id = tap_id
        self.output_path = output_path
        self.on_buffer = on_buffer
        self.max_pending_buffers = max_pending_buffers
        self.drift_compensation_quality = drift_compensation_quality
        self.is_recording = False
        self.needs_cleanup = False
        self.captured_only_silence = True
        self.start_calls = 0
        self.stop_calls = 0
        self.frames_recorded = 24_000
        self.duration_seconds = 0.5
        self._capture_failed = False
        self.failure_wait_timeouts: list[float | None] = []
        self.stream_format = AudioStreamFormat(
            sample_rate=48_000.0,
            num_channels=2,
            bits_per_sample=32,
            sample_type="float",
            format_id="lpcm",
        )

    def start(self) -> None:
        self.start_calls += 1
        self._capture_failed = False
        self.is_recording = True

    def stop(self) -> None:
        if not self.is_recording:
            raise RuntimeError("Not recording")

        self.stop_calls += 1
        self.is_recording = False

    @property
    def capture_failed(self) -> bool:
        return self._capture_failed

    def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
        self.failure_wait_timeouts.append(timeout)
        return self._capture_failed


class _StartFailingRecorder(_FakeRecorder):
    def start(self) -> None:
        self.start_calls += 1
        raise OSError("boom")


class _StopFailingRecorder(_FakeRecorder):
    def stop(self) -> None:
        self.stop_calls += 1
        self.is_recording = False
        raise OSError("stop boom")


class _WaitFailingRecorder(_FakeRecorder):
    def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
        self.failure_wait_timeouts.append(timeout)
        raise RuntimeError("wait boom")


class _RetryingStopRecorder(_FakeRecorder):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stop_failures_remaining = 1

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_failures_remaining:
            self.stop_failures_remaining -= 1
            raise RuntimeError("stop deferred")
        self.is_recording = False


class _ActiveStartFailingRecorder(_FakeRecorder):
    def start(self) -> None:
        self.start_calls += 1
        self.is_recording = True
        raise OSError("start failed while active")


class _PendingCleanupStartRecorder(_FakeRecorder):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.needs_cleanup = False

    def start(self) -> None:
        self.start_calls += 1
        self.needs_cleanup = True
        raise OSError("start cleanup pending")

    def stop(self) -> None:
        self.stop_calls += 1
        self.needs_cleanup = False


class _PendingCleanupStopRecorder(_FakeRecorder):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.needs_cleanup = False
        self.stop_failures_remaining = 1

    def stop(self) -> None:
        self.stop_calls += 1
        self.is_recording = False
        if self.stop_failures_remaining:
            self.stop_failures_remaining -= 1
            self.needs_cleanup = True
            raise RuntimeError("native cleanup deferred")
        self.needs_cleanup = False


class _MissingTapRecorder(_FakeRecorder):
    def start(self) -> None:
        self.start_calls += 1
        raise AudioTapNotFoundError(
            "Audio tap 91 is no longer available. It may have been destroyed."
        )


class _FakeMultitrackRecorder:
    def __init__(
        self,
        tap_ids: list[int],
        output_paths: list[Path | None],
        on_track_buffer: object = None,
        *,
        max_pending_buffers: int = 256,
        input_device_uid: str | None = None,
        input_stream_count: int = 0,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> None:
        self.tap_ids = list(tap_ids)
        self.output_paths = list(output_paths)
        self.on_track_buffer = on_track_buffer
        self.max_pending_buffers = max_pending_buffers
        self.input_device_uid = input_device_uid
        self.input_stream_count = input_stream_count
        self.drift_compensation_quality = drift_compensation_quality
        self.is_recording = False
        self.needs_cleanup = False
        self.captured_only_silence = True
        self.track_captured_only_silence = tuple(
            True for _ in range(input_stream_count + len(tap_ids))
        )
        self.frames_recorded = 48_000
        self.duration_seconds = 1.0
        self._capture_failed = False
        self.failure_wait_timeouts: list[float | None] = []
        self.stream_formats: list[AudioStreamFormat] = []
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self._capture_failed = False
        self.is_recording = True

    def stop(self) -> None:
        if not self.is_recording:
            raise RuntimeError("Not recording")

        self.stop_calls += 1
        self.is_recording = False

    @property
    def capture_failed(self) -> bool:
        return self._capture_failed

    def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
        self.failure_wait_timeouts.append(timeout)
        return self._capture_failed


class _StartFailingMultitrackRecorder(_FakeMultitrackRecorder):
    def start(self) -> None:
        self.start_calls += 1
        raise OSError("multitrack boom")


class _FakeSessionBackend:
    def __init__(
        self,
        *,
        process_lookup: dict[str, AudioProcess] | None = None,
        recorder_cls: type[_FakeRecorder] = _FakeRecorder,
        multitrack_recorder_cls: type[
            _FakeMultitrackRecorder
        ] = _FakeMultitrackRecorder,
        created_tap_ids: list[int] | None = None,
        destroyed_tap_ids: list[int] | None = None,
        destroy_error: OSError | None = None,
    ) -> None:
        self.process_lookup = process_lookup or {}
        self.recorder_cls = recorder_cls
        self.multitrack_recorder_cls = multitrack_recorder_cls
        self.created_tap_ids = created_tap_ids if created_tap_ids is not None else []
        self.destroyed_tap_ids = (
            destroyed_tap_ids if destroyed_tap_ids is not None else []
        )
        self.destroy_error = destroy_error
        self.created_recorders: list[_FakeRecorder] = []
        self.created_multitrack_recorders: list[_FakeMultitrackRecorder] = []
        self.default_input_device: object | None = None
        self.taps_described: list[int] = []
        self.tap_descriptions_set: list[tuple[int, _FakeTapDescription]] = []
        self.process_resolver: Callable[[str], AudioProcess | None] | None = None

    def find_process_by_name(self, name: str) -> AudioProcess | None:
        if self.process_resolver is not None:
            return self.process_resolver(name)
        return self.process_lookup.get(name)

    def find_audio_device(self, query: str) -> object | None:
        return None

    def build_processes_tap_description(
        self,
        processes: tuple[AudioProcess, ...] | list[AudioProcess],
        *,
        mute: object = False,
        mono: bool = False,
        visible: bool = False,
    ) -> _FakeTapDescription:
        tap_description = _FakeTapDescription(
            [process.audio_object_id for process in processes]
        )
        names = ", ".join(process.name for process in processes)
        tap_description.name = f"catap recording {names}"
        tap_description.is_private = not visible
        tap_description.mute_behavior = "muted" if mute else "unmuted"
        return tap_description

    def build_system_tap_description(
        self,
        excluded: tuple[AudioProcess, ...] | list[AudioProcess] = (),
        *,
        mute: object = False,
        mono: bool = False,
        visible: bool = False,
    ) -> _FakeTapDescription:
        tap_description = _FakeTapDescription(
            [process.audio_object_id for process in excluded],
            exclusive=True,
        )
        tap_description.name = "catap global recording"
        tap_description.is_private = not visible
        tap_description.mute_behavior = "muted" if mute else "unmuted"
        return tap_description

    def build_device_tap_description(
        self,
        stream: object,
        *,
        included: tuple[AudioProcess, ...] | list[AudioProcess] = (),
        excluded: tuple[AudioProcess, ...] | list[AudioProcess] = (),
        mute: object = False,
        visible: bool = False,
    ) -> _FakeTapDescription:
        tap_description = _FakeTapDescription(
            [process.audio_object_id for process in included or excluded],
            exclusive=not included,
        )
        tap_description.name = "catap recording device"
        tap_description.is_private = not visible
        return tap_description

    def build_bundle_tap_description(
        self,
        bundle_ids: tuple[str, ...] | list[str],
        *,
        restore: bool = True,
        mute: object = False,
        mono: bool = False,
        visible: bool = False,
    ) -> _FakeTapDescription:
        tap_description = _FakeTapDescription([], bundle_ids=list(bundle_ids))
        tap_description.name = f"catap recording {', '.join(bundle_ids)}"
        tap_description.is_private = not visible
        return tap_description

    def get_tap_description(self, tap_id: int) -> _FakeTapDescription:
        self.taps_described.append(tap_id)
        return _FakeTapDescription([tap_id])

    def set_tap_description(
        self, tap_id: int, description: _FakeTapDescription
    ) -> None:
        self.tap_descriptions_set.append((tap_id, description))

    def create_process_tap(
        self,
        description: _FakeTapDescription,
        *,
        out: ctypes.c_uint32 | None = None,
    ) -> int:
        self.created_tap_ids.append(
            description.processes[0] if description.processes else 99
        )
        if out is not None:
            out.value = 77
        return 77

    def destroy_process_tap(self, tap_id: int) -> None:
        self.destroyed_tap_ids.append(tap_id)
        if self.destroy_error is not None:
            raise self.destroy_error

    def create_recorder(
        self,
        tap_id: int,
        output_path: Path | None,
        on_buffer: object = None,
        *,
        max_pending_buffers: int = 256,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> _FakeRecorder:
        recorder = self.recorder_cls(
            tap_id,
            output_path,
            on_buffer,
            max_pending_buffers=max_pending_buffers,
            drift_compensation_quality=drift_compensation_quality,
        )
        self.created_recorders.append(recorder)
        return recorder

    def find_default_input_device(self) -> object | None:
        return self.default_input_device

    def create_multitrack_recorder(
        self,
        tap_ids: list[int],
        output_paths: list[Path | None],
        on_track_buffer: object = None,
        *,
        max_pending_buffers: int = 256,
        input_device_uid: str | None = None,
        input_stream_count: int = 0,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> _FakeMultitrackRecorder:
        recorder = self.multitrack_recorder_cls(
            tap_ids,
            output_paths,
            on_track_buffer,
            max_pending_buffers=max_pending_buffers,
            input_device_uid=input_device_uid,
            input_stream_count=input_stream_count,
            drift_compensation_quality=drift_compensation_quality,
        )
        self.created_multitrack_recorders.append(recorder)
        return recorder


class _SequentialTapBackend(_FakeSessionBackend):
    """Backend that assigns distinct tap IDs across create calls."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.next_tap_id = 101

    def create_process_tap(
        self,
        description: _FakeTapDescription,
        *,
        out: ctypes.c_uint32 | None = None,
    ) -> int:
        tap_id = self.next_tap_id
        self.next_tap_id += 1
        self.created_tap_ids.append(tap_id)
        if out is not None:
            out.value = tap_id
        return tap_id


def _install_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: _FakeSessionBackend,
) -> None:
    monkeypatch.setattr(session_module, "_DEFAULT_SESSION_BACKEND", backend)


def test_record_process_context_manager_manages_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    destroyed_tap_ids: list[int] = []
    backend = _FakeSessionBackend(
        process_lookup={"Music": process},
        destroyed_tap_ids=destroyed_tap_ids,
    )
    _install_backend(monkeypatch, backend)

    session = session_module.record_process(
        "Music",
        output_path="recording.wav",
        mute=True,
    )

    assert session.source_process == process
    assert session.tap_description.name == "catap recording Music"
    assert session.tap_description.processes == [11]
    assert session.tap_description.is_private is True
    assert session.tap_description.mute_behavior == "muted"
    assert backend.created_recorders == []

    with session as active_session:
        assert active_session.tap_id == 77
        assert active_session.is_recording is True
        assert len(backend.created_recorders) == 1
        recorder = backend.created_recorders[0]
        assert recorder.output_path == Path("recording.wav")
        assert recorder.max_pending_buffers == 256

    assert session.tap_id is None
    assert session.is_recording is False
    assert session.duration_seconds == 0.5
    assert recorder.start_calls == 1
    assert recorder.stop_calls == 1
    assert destroyed_tap_ids == [77]


def test_recording_session_exposes_stream_format_once_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    backend = _FakeSessionBackend(process_lookup={"Music": process})
    _install_backend(monkeypatch, backend)

    session = session_module.record_process("Music", output_path="recording.wav")

    assert session.stream_format is None

    with session:
        recorder = backend.created_recorders[0]
        assert session.stream_format is recorder.stream_format


def test_recording_session_forwards_sticky_capture_failure_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    backend = _FakeSessionBackend(process_lookup={"Music": process})
    _install_backend(monkeypatch, backend)
    session = session_module.record_process("Music", output_path="recording.wav")

    assert session.capture_failed is False
    with pytest.raises(RuntimeError, match="Recording has not started"):
        session.wait_for_capture_failure(timeout=0)

    session.start()
    recorder = backend.created_recorders[0]
    recorder._capture_failed = True

    assert session.capture_failed is True
    assert session.wait_for_capture_failure(timeout=0.25) is True
    assert recorder.failure_wait_timeouts == [0.25]

    session.stop()
    assert session.capture_failed is True
    with pytest.raises(RuntimeError, match="Not recording"):
        session.wait_for_capture_failure()


def test_record_process_threads_drift_quality_to_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    backend = _FakeSessionBackend(process_lookup={"Music": process})
    _install_backend(monkeypatch, backend)

    session = session_module.record_process(
        "Music",
        output_path="recording.wav",
        drift_compensation_quality=DriftCompensationQuality.HIGH,
    )
    session.start()

    assert session.drift_compensation_quality is DriftCompensationQuality.HIGH
    assert (
        backend.created_recorders[0].drift_compensation_quality
        is DriftCompensationQuality.HIGH
    )

    session.stop()


def test_record_process_raises_for_missing_process_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_backend(monkeypatch, _FakeSessionBackend())

    with pytest.raises(
        session_module.AudioProcessNotFoundError,
        match="No audio process found matching 'Missing'",
    ):
        session_module.record_process("Missing")


def test_record_process_propagates_ambiguous_process_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    backend = _FakeSessionBackend()

    def _raise_ambiguous(name: str) -> AudioProcess | None:
        raise AmbiguousAudioProcessError(name, [process, process])

    backend.process_resolver = _raise_ambiguous
    _install_backend(monkeypatch, backend)

    with pytest.raises(AmbiguousAudioProcessError, match="Multiple audio processes"):
        session_module.record_process("Music")


def test_recording_session_start_cleans_up_tap_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed_tap_ids: list[int] = []
    backend = _FakeSessionBackend(
        recorder_cls=_StartFailingRecorder,
        destroyed_tap_ids=destroyed_tap_ids,
    )
    _install_backend(monkeypatch, backend)

    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(OSError, match="boom"):
        session.start()

    assert session.tap_id is None
    assert len(backend.created_recorders) == 1
    assert backend.created_recorders[0].start_calls == 1
    assert destroyed_tap_ids == [77]


def test_recording_session_start_reports_tap_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed_tap_ids: list[int] = []
    backend = _FakeSessionBackend(
        recorder_cls=_StartFailingRecorder,
        destroyed_tap_ids=destroyed_tap_ids,
        destroy_error=OSError("destroy boom"),
    )
    _install_backend(monkeypatch, backend)

    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(OSError, match="boom") as exc_info:
        session.start()

    assert session.tap_id == 77
    assert destroyed_tap_ids == [77]
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("Cleanup failure during session startup" in note for note in notes)
    assert any("destroy boom" in note for note in notes)

    backend.destroy_error = None
    session.close()
    assert session.tap_id is None
    assert destroyed_tap_ids == [77, 77]


def test_start_preserves_primary_when_tap_cleanup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = OSError("recorder startup failed")
    interrupt = KeyboardInterrupt("tap cleanup interrupted")
    destroy_calls = 0

    class _PrimaryStartFailingRecorder(_FakeRecorder):
        def start(self) -> None:
            self.start_calls += 1
            raise primary

    backend = _FakeSessionBackend(recorder_cls=_PrimaryStartFailingRecorder)

    def _interrupt_first_destroy(tap_id: int) -> None:
        nonlocal destroy_calls
        destroy_calls += 1
        if destroy_calls == 1:
            raise interrupt
        backend.destroyed_tap_ids.append(tap_id)

    monkeypatch.setattr(backend, "destroy_process_tap", _interrupt_first_destroy)
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(OSError, match="recorder startup failed") as exc_info:
        session.start()

    assert exc_info.value is primary
    assert any("tap cleanup interrupted" in note for note in exc_info.value.__notes__)
    assert session.tap_id == 77
    assert destroy_calls == 1

    session.close()
    assert destroy_calls == 2
    assert session.tap_id is None
    assert backend.destroyed_tap_ids == [77]


def test_close_accepts_bad_object_after_interrupted_tap_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroy_calls = 0
    backend = _FakeSessionBackend()

    def _ambiguous_destroy(tap_id: int) -> None:
        nonlocal destroy_calls
        destroy_calls += 1
        if destroy_calls == 1:
            raise KeyboardInterrupt("interrupted after native destroy")
        error = OSError("tap no longer exists")
        error.status = kAudioHardwareBadObjectError
        raise error

    monkeypatch.setattr(backend, "destroy_process_tap", _ambiguous_destroy)
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )
    session._tap_id = 77
    session._owns_tap = True

    with pytest.raises(KeyboardInterrupt, match="interrupted after native destroy"):
        session.close()

    assert session.tap_id == 77

    session.close()

    assert destroy_calls == 2
    assert session.tap_id is None


def test_start_failure_retains_active_recorder_and_tap_for_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend(recorder_cls=_ActiveStartFailingRecorder)
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(OSError, match="start failed while active"):
        session.start()

    assert session.is_recording is True
    assert session.tap_id == 77
    assert backend.destroyed_tap_ids == []

    session.close()
    assert session.is_recording is False
    assert session.tap_id is None
    assert backend.destroyed_tap_ids == [77]


def test_start_failure_retains_pending_recorder_cleanup_for_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend(recorder_cls=_PendingCleanupStartRecorder)
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(OSError, match="start cleanup pending"):
        session.start()

    recorder = backend.created_recorders[0]
    assert recorder.is_recording is False
    assert recorder.needs_cleanup is True
    assert session.tap_id == 77
    assert backend.destroyed_tap_ids == []

    session.close()
    assert recorder.stop_calls == 1
    assert recorder.needs_cleanup is False
    assert session.tap_id is None
    assert backend.destroyed_tap_ids == [77]


def test_session_forwards_captured_only_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend()
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    assert session.captured_only_silence is True

    session.start()
    recorder = backend.created_recorders[0]
    assert session.captured_only_silence is True

    recorder.captured_only_silence = False
    assert session.captured_only_silence is False

    session.close()
    assert session.captured_only_silence is False


def test_needs_cleanup_tracks_failed_stop_and_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend(recorder_cls=_PendingCleanupStopRecorder)
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    assert session.needs_cleanup is False

    session.start()
    assert session.needs_cleanup is False

    with pytest.raises(RuntimeError, match="native cleanup deferred"):
        session.stop()

    assert session.needs_cleanup is True
    assert session.tap_id == 77

    session.close()
    assert session.needs_cleanup is False
    assert session.tap_id is None
    assert backend.destroyed_tap_ids == [77]


def test_needs_cleanup_tracks_failed_tap_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend(destroy_error=OSError("destroy boom"))
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    session.start()
    with pytest.raises(OSError, match="destroy boom"):
        session.stop()

    assert session.needs_cleanup is True
    assert session.tap_id == 77

    backend.destroy_error = None
    session.close()
    assert session.needs_cleanup is False
    assert session.tap_id is None


def test_needs_cleanup_is_false_while_recording_holds_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ResourceHoldingRecorder(_FakeRecorder):
        """Mirrors AudioRecorder, whose needs_cleanup is true while running."""

        def start(self) -> None:
            super().start()
            self.needs_cleanup = True

        def stop(self) -> None:
            super().stop()
            self.needs_cleanup = False

    backend = _FakeSessionBackend(recorder_cls=_ResourceHoldingRecorder)
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    session.start()
    assert session.is_recording is True
    assert session.needs_cleanup is False

    session.close()
    assert session.needs_cleanup is False


class _InterruptedCreateBackend(_FakeSessionBackend):
    """Backend whose tap creation is interrupted after the tap exists."""

    def create_process_tap(
        self,
        description: _FakeTapDescription,
        *,
        out: ctypes.c_uint32 | None = None,
    ) -> int:
        super().create_process_tap(description, out=out)
        raise KeyboardInterrupt


def test_interrupted_tap_creation_destroys_recovered_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _InterruptedCreateBackend()
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(KeyboardInterrupt):
        session.start()

    assert backend.created_recorders == []
    assert backend.destroyed_tap_ids == [77]
    assert session.tap_id is None
    assert session.is_recording is False


def test_interrupted_tap_creation_retains_tap_when_destroy_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _InterruptedCreateBackend(destroy_error=OSError("destroy deferred"))
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(KeyboardInterrupt):
        session.start()

    assert backend.destroyed_tap_ids == [77]
    assert session.tap_id == 77

    backend.destroy_error = None
    session.close()
    assert backend.destroyed_tap_ids == [77, 77]
    assert session.tap_id is None


def test_record_system_audio_tracks_excluded_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _FakeSessionBackend(
        process_lookup={"Music": music},
    )
    _install_backend(monkeypatch, backend)

    session = session_module.record_system_audio(
        output_path="system.wav",
        exclude=["Music", zoom],
    )

    assert session.excluded_processes == (music, zoom)
    assert session.tap_description.name == "catap global recording"
    assert session.tap_description.processes == [11, 12]
    assert session.tap_description.is_exclusive is True
    assert session.tap_description.is_private is True
    assert session.tap_description.mute_behavior == "unmuted"


def test_record_for_starts_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    destroyed_tap_ids: list[int] = []
    backend = _FakeSessionBackend(
        process_lookup={"Music": process},
        destroyed_tap_ids=destroyed_tap_ids,
    )
    _install_backend(monkeypatch, backend)

    session = session_module.record_process("Music", output_path="recording.wav")
    returned_session = session.record_for(2.5)

    assert returned_session is session
    assert session.tap_id is None
    assert session.is_recording is False
    assert len(backend.created_recorders) == 1
    fake_recorder = backend.created_recorders[0]
    assert fake_recorder.failure_wait_timeouts == [2.5]
    assert fake_recorder.start_calls == 1
    assert fake_recorder.stop_calls == 1
    assert destroyed_tap_ids == [77]


def test_record_for_propagates_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed_tap_ids: list[int] = []
    backend = _FakeSessionBackend(
        recorder_cls=_StartFailingRecorder,
        destroyed_tap_ids=destroyed_tap_ids,
    )
    _install_backend(monkeypatch, backend)

    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(OSError, match="boom"):
        session.record_for(1.5)

    assert session.tap_id is None
    assert len(backend.created_recorders) == 1
    assert backend.created_recorders[0].start_calls == 1
    assert destroyed_tap_ids == [77]


def test_record_for_rejects_non_positive_duration() -> None:
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )
    with pytest.raises(ValueError, match="duration must be greater than 0"):
        session.record_for(0)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
def test_record_for_rejects_non_finite_duration(duration: float) -> None:
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(ValueError, match="duration must be finite"):
        session.record_for(duration)


def test_record_for_preserves_body_exception_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend(
        recorder_cls=_WaitFailingRecorder,
        destroy_error=OSError("destroy boom"),
    )
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(RuntimeError, match="wait boom") as exc_info:
        session.record_for(1.0)

    assert session.tap_id == 77
    notes = getattr(exc_info.value, "__notes__", [])
    assert any(
        "Cleanup failure while recording for a fixed duration" in note for note in notes
    )
    assert any("destroy boom" in note for note in notes)

    backend.destroy_error = None
    session.close()


def test_recording_session_requires_output_path_or_callback() -> None:
    with pytest.raises(
        ValueError,
        match="output_path must be provided unless on_buffer is set for streaming mode",
    ):
        session_module.RecordingSession(cast(TapDescription, _FakeTapDescription([42])))


def test_record_process_forwards_max_pending_buffers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    backend = _FakeSessionBackend(process_lookup={"Music": process})
    _install_backend(monkeypatch, backend)

    session = session_module.record_process(
        "Music",
        output_path="recording.wav",
        max_pending_buffers=32,
    )

    assert session.max_pending_buffers == 32

    with session:
        assert len(backend.created_recorders) == 1
        assert backend.created_recorders[0].max_pending_buffers == 32


def test_record_process_rejects_non_positive_max_pending_buffers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    _install_backend(
        monkeypatch,
        _FakeSessionBackend(process_lookup={"Music": process}),
    )

    with pytest.raises(
        ValueError,
        match="max_pending_buffers must be greater than 0",
    ):
        session_module.record_process(
            "Music",
            output_path="recording.wav",
            max_pending_buffers=0,
        )


@pytest.mark.parametrize("value", [True, 1.5, "8"])
def test_record_process_rejects_non_integer_max_pending_buffers(
    monkeypatch: pytest.MonkeyPatch,
    value: object,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    _install_backend(
        monkeypatch,
        _FakeSessionBackend(process_lookup={"Music": process}),
    )

    with pytest.raises(TypeError, match="max_pending_buffers must be an integer"):
        session_module.record_process(
            "Music",
            output_path="recording.wav",
            max_pending_buffers=cast(Any, value),
        )


def test_record_tap_context_manager_uses_existing_tap_without_destroying_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed_tap_ids: list[int] = []
    _install_backend(
        monkeypatch,
        _FakeSessionBackend(destroyed_tap_ids=destroyed_tap_ids),
    )

    tap = AudioTap(88, "tap-uid", cast(TapDescription, _FakeTapDescription([88])))
    session = session_module.record_tap(tap, output_path="recording.wav")

    assert session.source_tap is tap
    assert session.tap_description.processes == [88]

    with session as active_session:
        assert active_session.tap_id == 88
        assert active_session.is_recording is True

    assert session.tap_id is None
    assert session.is_recording is False
    assert destroyed_tap_ids == []


def test_record_tap_fetches_description_for_raw_tap_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed_tap_ids: list[int] = []
    backend = _FakeSessionBackend(destroyed_tap_ids=destroyed_tap_ids)
    _install_backend(monkeypatch, backend)

    session = session_module.record_tap(91, output_path="recording.wav")

    assert session.source_tap is None
    assert session.tap_description.processes == [91]
    assert backend.taps_described == [91]

    with session:
        assert session.tap_id == 91

    assert destroyed_tap_ids == []


def test_record_tap_does_not_destroy_existing_tap_when_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed_tap_ids: list[int] = []
    backend = _FakeSessionBackend(
        recorder_cls=_StartFailingRecorder,
        destroyed_tap_ids=destroyed_tap_ids,
    )
    _install_backend(monkeypatch, backend)

    session = session_module.record_tap(91, output_path="recording.wav")

    with pytest.raises(OSError, match="boom"):
        session.start()

    assert session.tap_id is None
    assert len(backend.created_recorders) == 1
    assert backend.created_recorders[0].start_calls == 1
    assert destroyed_tap_ids == []


def test_record_tap_propagates_stale_tap_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed_tap_ids: list[int] = []
    backend = _FakeSessionBackend(
        recorder_cls=_MissingTapRecorder,
        destroyed_tap_ids=destroyed_tap_ids,
    )
    _install_backend(monkeypatch, backend)

    session = session_module.record_tap(91, output_path="recording.wav")

    with pytest.raises(AudioTapNotFoundError, match="Audio tap 91 is no longer"):
        session.record_for(1.0)

    assert session.tap_id is None
    assert len(backend.created_recorders) == 1
    assert backend.created_recorders[0].start_calls == 1
    assert destroyed_tap_ids == []


def test_stop_combines_recorder_and_tap_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend(
        recorder_cls=_StopFailingRecorder,
        destroy_error=OSError("destroy boom"),
    )
    _install_backend(monkeypatch, backend)

    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )
    session.start()

    with pytest.raises(OSError, match="stop boom") as exc_info:
        session.stop()

    assert session.tap_id == 77
    assert backend.destroyed_tap_ids == [77]
    notes = getattr(exc_info.value, "__notes__", [])
    assert "Failed to stop recording session" in notes
    assert any("destroy boom" in note for note in notes)

    backend.destroy_error = None
    session.stop()
    assert session.tap_id is None
    assert backend.destroyed_tap_ids == [77, 77]


def test_close_combines_recorder_and_tap_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend(
        recorder_cls=_StopFailingRecorder,
        destroy_error=OSError("destroy boom"),
    )
    _install_backend(monkeypatch, backend)

    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )
    session.start()

    with pytest.raises(OSError, match="stop boom") as exc_info:
        session.close()

    assert session.tap_id == 77
    assert backend.destroyed_tap_ids == [77]
    notes = getattr(exc_info.value, "__notes__", [])
    assert "Failed to close recording session" in notes
    assert any("destroy boom" in note for note in notes)

    with pytest.raises(RuntimeError, match="pending tap cleanup"):
        session.start()
    assert len(backend.created_recorders) == 1

    backend.destroy_error = None
    session.close()
    assert session.tap_id is None
    assert backend.destroyed_tap_ids == [77, 77]


def test_stop_failure_while_active_retains_tap_and_retries_on_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend(recorder_cls=_RetryingStopRecorder)
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )
    session.start()

    with pytest.raises(RuntimeError, match="stop deferred"):
        session.stop()

    recorder = backend.created_recorders[0]
    assert recorder.is_recording is True
    assert recorder.stop_calls == 1
    assert session.tap_id == 77
    assert backend.destroyed_tap_ids == []

    session.close()
    assert recorder.is_recording is False
    assert recorder.stop_calls == 2
    assert session.tap_id is None
    assert backend.destroyed_tap_ids == [77]


def test_stop_failure_retains_pending_cleanup_and_tap_for_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend(recorder_cls=_PendingCleanupStopRecorder)
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )
    session.start()

    with pytest.raises(RuntimeError, match="native cleanup deferred"):
        session.stop()

    recorder = backend.created_recorders[0]
    assert recorder.is_recording is False
    assert recorder.needs_cleanup is True
    assert recorder.stop_calls == 1
    assert session.tap_id == 77
    assert backend.destroyed_tap_ids == []

    session.close()
    assert recorder.needs_cleanup is False
    assert recorder.stop_calls == 2
    assert session.tap_id is None
    assert backend.destroyed_tap_ids == [77]


def test_stop_interrupt_still_destroys_tap_after_recorder_quiesces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend()
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )
    session.start()
    recorder = backend.created_recorders[0]
    interrupt = KeyboardInterrupt("recorder stop interrupted")

    def _interrupt_stop() -> None:
        recorder.stop_calls += 1
        recorder.is_recording = False
        raise interrupt

    monkeypatch.setattr(recorder, "stop", _interrupt_stop)

    with pytest.raises(
        KeyboardInterrupt,
        match="recorder stop interrupted",
    ) as exc_info:
        session.stop()

    assert exc_info.value is interrupt
    assert session.tap_id is None
    assert backend.destroyed_tap_ids == [77]


def test_context_manager_attaches_cleanup_failure_to_body_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend(destroy_error=OSError("destroy boom"))
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(ValueError, match="body boom") as exc_info, session:
        raise ValueError("body boom")

    assert session.tap_id == 77
    notes = getattr(exc_info.value, "__notes__", [])
    assert any(
        "Cleanup failure while exiting recording session" in note for note in notes
    )
    assert any("destroy boom" in note for note in notes)

    backend.destroy_error = None
    session.close()
    assert session.tap_id is None


def test_from_processes_builds_single_tap_for_several_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _FakeSessionBackend(process_lookup={"Music": music, "Zoom": zoom})
    _install_backend(monkeypatch, backend)

    session = session_module.RecordingSession.from_processes(
        ["Music", zoom],
        output_path="mix.wav",
    )

    assert session.source_processes == (music, zoom)
    assert session.source_process is music
    assert session.tap_description.processes == [11, 12]
    assert session.tap_description.name == "catap recording Music, Zoom"


def test_record_processes_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="At least one process is required"):
        session_module.record_processes([])


def test_from_bundle_ids_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="At least one bundle ID is required"):
        session_module.RecordingSession.from_bundle_ids([])


def test_set_processes_retargets_live_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _FakeSessionBackend(process_lookup={"Music": music, "Zoom": zoom})
    _install_backend(monkeypatch, backend)
    session = session_module.record_process("Music", output_path="recording.wav")
    session.start()

    session.set_processes(["Zoom"])

    assert backend.taps_described == [77]
    assert len(backend.tap_descriptions_set) == 1
    tap_id, description = backend.tap_descriptions_set[0]
    assert tap_id == 77
    assert description.processes == [12]
    assert session.tap_description is description
    assert session.source_processes == (zoom,)
    assert session.source_process is zoom

    session.close()


@pytest.mark.parametrize(
    ("description", "match"),
    [
        (
            _FakeTapDescription([11], exclusive=True),
            "only supports inclusive taps",
        ),
        (
            _FakeTapDescription([], bundle_ids=["com.apple.Music"]),
            "cannot retarget a bundle-ID tap",
        ),
    ],
    ids=["exclusive", "bundle-ids"],
)
def test_set_processes_rejects_targets_with_different_list_semantics(
    monkeypatch: pytest.MonkeyPatch,
    description: _FakeTapDescription,
    match: str,
) -> None:
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _FakeSessionBackend(process_lookup={"Zoom": zoom})
    _install_backend(monkeypatch, backend)
    monkeypatch.setattr(
        backend,
        "get_tap_description",
        lambda tap_id: description,
    )
    session = session_module.RecordingSession(
        cast(TapDescription, description),
        output_path="recording.wav",
    )
    session.start()

    with pytest.raises(ValueError, match=match):
        session.set_processes(["Zoom"])

    assert backend.tap_descriptions_set == []
    assert session.tap_description is description
    session.close()


def test_set_mute_behavior_updates_live_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    backend = _FakeSessionBackend(process_lookup={"Music": music})
    _install_backend(monkeypatch, backend)
    session = session_module.record_process("Music", output_path="recording.wav")
    session.start()

    session.set_mute_behavior(True)

    assert backend.taps_described == [77]
    tap_id, description = backend.tap_descriptions_set[0]
    assert tap_id == 77
    assert description.mute_behavior is TapMuteBehavior.MUTED
    assert session.tap_description is description

    session.close()


def test_set_processes_and_mute_behavior_require_live_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeSessionBackend()
    _install_backend(monkeypatch, backend)
    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(RuntimeError, match="no live tap"):
        session.set_processes(["Music"])
    with pytest.raises(RuntimeError, match="no live tap"):
        session.set_mute_behavior(True)

    assert backend.tap_descriptions_set == []


def test_record_multitrack_derives_deduplicated_track_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    other_music = AudioProcess(12, 222, "com.example.music", "Music", True)
    backend = _FakeSessionBackend(process_lookup={"Music": music})
    _install_backend(monkeypatch, backend)

    session = session_module.record_multitrack(
        ["Music", other_music],
        output_dir=tmp_path,
    )

    assert session.output_paths == [
        tmp_path / "Music.wav",
        tmp_path / "Music-2.wav",
    ]
    assert session.track_labels == ("Music", "Music")
    assert session.source_processes == (music, other_music)
    assert session.track_count == 2
    assert session.tap_ids == []
    assert session.is_recording is False


def test_record_multitrack_creates_nested_output_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _FakeSessionBackend(process_lookup={"Music": music, "Zoom": zoom})
    _install_backend(monkeypatch, backend)
    output_dir = tmp_path / "nested" / "session"

    session = session_module.record_multitrack(
        ["Music", "Zoom"],
        output_dir=output_dir,
    )

    assert output_dir.is_dir()
    assert session.output_paths == [
        output_dir / "Music.wav",
        output_dir / "Zoom.wav",
    ]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("track.wav", "track.wav"),
        ("track.wav", "nested/../track.wav"),
        ("Music.wav", "music.WAV"),
        (
            "Caf\N{LATIN SMALL LETTER E WITH ACUTE}.wav",
            "Cafe\N{COMBINING ACUTE ACCENT}.wav",
        ),
    ],
    ids=["identical", "relative-components", "case", "unicode"],
)
def test_multitrack_session_rejects_aliased_output_paths(
    tmp_path: Path,
    first: str,
    second: str,
) -> None:
    descriptions = [
        cast(TapDescription, _FakeTapDescription([11])),
        cast(TapDescription, _FakeTapDescription([12])),
    ]

    with pytest.raises(ValueError, match="refer to the same destination"):
        session_module.MultitrackRecordingSession(
            descriptions,
            [tmp_path / first, tmp_path / second],
        )


def test_multitrack_session_creates_taps_on_start_and_destroys_on_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _SequentialTapBackend(
        process_lookup={"Music": music, "Zoom": zoom},
    )
    _install_backend(monkeypatch, backend)
    session = session_module.record_multitrack(
        ["Music", "Zoom"],
        output_dir=tmp_path,
    )

    session.start()

    assert session.tap_ids == [101, 102]
    assert session.is_recording is True
    assert len(backend.created_multitrack_recorders) == 1
    recorder = backend.created_multitrack_recorders[0]
    assert recorder.tap_ids == [101, 102]
    assert recorder.output_paths == [
        tmp_path / "Music.wav",
        tmp_path / "Zoom.wav",
    ]
    assert recorder.input_device_uid is None
    assert recorder.start_calls == 1

    session.stop()

    assert session.is_recording is False
    assert session.tap_ids == []
    assert recorder.stop_calls == 1
    assert backend.destroyed_tap_ids == [101, 102]


def test_multitrack_record_for_waits_for_failure_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _SequentialTapBackend()
    _install_backend(monkeypatch, backend)
    session = session_module.MultitrackRecordingSession(
        [
            cast(TapDescription, _FakeTapDescription([11])),
            cast(TapDescription, _FakeTapDescription([12])),
        ],
        [tmp_path / "one.wav", tmp_path / "two.wav"],
    )

    assert session.capture_failed is False
    with pytest.raises(RuntimeError, match="Recording has not started"):
        session.wait_for_capture_failure(timeout=0)

    returned_session = session.record_for(1.25)

    assert returned_session is session
    recorder = backend.created_multitrack_recorders[0]
    assert recorder.failure_wait_timeouts == [1.25]
    assert recorder.start_calls == 1
    assert recorder.stop_calls == 1
    assert session.is_recording is False
    assert session.tap_ids == []
    assert backend.destroyed_tap_ids == [101, 102]
    with pytest.raises(RuntimeError, match="Not recording"):
        session.wait_for_capture_failure()


def test_record_multitrack_threads_drift_quality_to_recorder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _SequentialTapBackend(
        process_lookup={"Music": music, "Zoom": zoom},
    )
    _install_backend(monkeypatch, backend)

    session = session_module.record_multitrack(
        ["Music", "Zoom"],
        output_dir=tmp_path,
        drift_compensation_quality=DriftCompensationQuality.MAXIMUM,
    )
    session.start()

    assert session.drift_compensation_quality is DriftCompensationQuality.MAXIMUM
    assert (
        backend.created_multitrack_recorders[0].drift_compensation_quality
        is DriftCompensationQuality.MAXIMUM
    )

    session.stop()


def test_multitrack_session_start_failure_destroys_created_taps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _SequentialTapBackend(
        process_lookup={"Music": music, "Zoom": zoom},
        multitrack_recorder_cls=_StartFailingMultitrackRecorder,
    )
    _install_backend(monkeypatch, backend)
    session = session_module.record_multitrack(
        ["Music", "Zoom"],
        output_dir=tmp_path,
    )

    with pytest.raises(OSError, match="multitrack boom"):
        session.start()

    assert backend.created_tap_ids == [101, 102]
    assert backend.destroyed_tap_ids == [101, 102]
    assert session.tap_ids == []
    assert session.is_recording is False
    assert session.needs_cleanup is False


def test_multitrack_stop_keeps_undestroyed_taps_for_retried_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _SequentialTapBackend(
        process_lookup={"Music": music, "Zoom": zoom},
        destroy_error=OSError("destroy boom"),
    )
    _install_backend(monkeypatch, backend)
    session = session_module.record_multitrack(
        ["Music", "Zoom"],
        output_dir=tmp_path,
    )
    session.start()

    with pytest.raises(OSError, match="destroy boom"):
        session.stop()

    assert session.tap_ids == [101, 102]
    assert session.needs_cleanup is True

    backend.destroy_error = None
    session.close()

    assert session.tap_ids == []
    assert session.needs_cleanup is False
    assert backend.destroyed_tap_ids == [101, 102, 101, 102]


def test_multitrack_destroy_taps_publishes_progress_before_interruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _InterruptingDestroyBackend(_SequentialTapBackend):
        interrupt_destroy = True

        def destroy_process_tap(self, tap_id: int) -> None:
            self.destroyed_tap_ids.append(tap_id)
            if tap_id == 102:
                if self.interrupt_destroy:
                    raise KeyboardInterrupt
                error = OSError("tap no longer exists")
                error.status = (  # type: ignore[attr-defined]
                    kAudioHardwareBadObjectError
                )
                raise error

    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _InterruptingDestroyBackend(process_lookup={"Music": music, "Zoom": zoom})
    _install_backend(monkeypatch, backend)
    session = session_module.record_multitrack(
        ["Music", "Zoom"],
        output_dir=tmp_path,
    )
    session.start()

    with pytest.raises(KeyboardInterrupt):
        session.close()

    assert session.tap_ids == [102]
    assert session.needs_cleanup is True

    backend.interrupt_destroy = False
    session.close()

    assert session.tap_ids == []
    assert session.needs_cleanup is False
    assert backend.destroyed_tap_ids == [101, 102, 102]


def test_record_multitrack_requires_an_output_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _FakeSessionBackend(process_lookup={"Music": music, "Zoom": zoom})
    _install_backend(monkeypatch, backend)

    with pytest.raises(
        ValueError,
        match="Provide output_dir, output_paths, or on_track_buffer",
    ):
        session_module.record_multitrack(["Music", "Zoom"])


def test_record_multitrack_requires_default_input_device_for_microphone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    backend = _FakeSessionBackend(process_lookup={"Music": music, "Zoom": zoom})
    _install_backend(monkeypatch, backend)

    with pytest.raises(
        session_module.AudioDeviceNotFoundError,
        match="No default input device",
    ):
        session_module.record_multitrack(
            ["Music", "Zoom"],
            output_dir=tmp_path,
            microphone=True,
        )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", "track"),
        ("a b/c", "a_b_c"),
        ("🎵 🎶", "track"),
        ("  Music  ", "Music"),
    ],
    ids=["empty", "separators", "emoji-only", "whitespace-padded"],
)
def test_sanitize_track_filename_edge_cases(name: str, expected: str) -> None:
    assert session_module._sanitize_track_filename(name) == expected


def test_derive_track_paths_never_collides_with_suffixed_labels(
    tmp_path: Path,
) -> None:
    paths = session_module._derive_track_paths(tmp_path, ["Music", "Music", "Music-2"])

    assert len(set(paths)) == 3
    assert paths[0] == tmp_path / "Music.wav"
    assert paths[1] == tmp_path / "Music-2.wav"
    assert paths[2] == tmp_path / "Music-2-2.wav"


def test_derive_track_paths_deduplicates_case_insensitively(
    tmp_path: Path,
) -> None:
    paths = session_module._derive_track_paths(tmp_path, ["Music", "music"])

    assert paths == [tmp_path / "Music.wav", tmp_path / "music-2.wav"]
