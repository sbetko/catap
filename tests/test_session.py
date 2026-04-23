"""High-level recording session tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import catap.session as session_module
from catap.bindings.process import AmbiguousAudioProcessError, AudioProcess
from catap.bindings.tap import AudioTap, AudioTapNotFoundError
from catap.bindings.tap_description import TapDescription


class _FakeTapDescription:
    def __init__(self, processes: list[int], *, exclusive: bool = False) -> None:
        self.processes = processes
        self.is_exclusive = exclusive
        self.name = ""
        self.is_private = False
        self.mute_behavior = None

    @classmethod
    def stereo_mixdown_of_processes(cls, processes: list[int]) -> _FakeTapDescription:
        return cls(list(processes))

    @classmethod
    def stereo_global_tap_excluding(
        cls, processes: list[int]
    ) -> _FakeTapDescription:
        return cls(list(processes), exclusive=True)


class _FakeRecorder:
    def __init__(
        self,
        tap_id: int,
        output_path: Path | None,
        on_data: object = None,
        *,
        max_pending_buffers: int = 256,
    ) -> None:
        self.tap_id = tap_id
        self.output_path = output_path
        self.on_data = on_data
        self.max_pending_buffers = max_pending_buffers
        self.is_recording = False
        self.start_calls = 0
        self.stop_calls = 0
        self.frames_recorded = 24_000
        self.duration_seconds = 0.5
        self.sample_rate = 48_000.0
        self.num_channels = 2
        self.is_float = True

    def start(self) -> None:
        self.start_calls += 1
        self.is_recording = True

    def stop(self) -> None:
        if not self.is_recording:
            raise RuntimeError("Not recording")

        self.stop_calls += 1
        self.is_recording = False


class _StartFailingRecorder(_FakeRecorder):
    def start(self) -> None:
        self.start_calls += 1
        raise OSError("boom")


class _MissingTapRecorder(_FakeRecorder):
    def start(self) -> None:
        self.start_calls += 1
        raise AudioTapNotFoundError(
            "Audio tap 91 is no longer available. It may have been destroyed."
        )


class _FakeSessionBackend:
    def __init__(
        self,
        *,
        process_lookup: dict[str, AudioProcess] | None = None,
        recorder_cls: type[_FakeRecorder] = _FakeRecorder,
        created_tap_ids: list[int] | None = None,
        destroyed_tap_ids: list[int] | None = None,
    ) -> None:
        self.process_lookup = process_lookup or {}
        self.recorder_cls = recorder_cls
        self.created_tap_ids = created_tap_ids if created_tap_ids is not None else []
        self.destroyed_tap_ids = (
            destroyed_tap_ids if destroyed_tap_ids is not None else []
        )
        self.created_recorders: list[_FakeRecorder] = []
        self.taps_described: list[int] = []
        self.process_resolver: Callable[[str], AudioProcess | None] | None = None

    def find_process_by_name(self, name: str) -> AudioProcess | None:
        if self.process_resolver is not None:
            return self.process_resolver(name)
        return self.process_lookup.get(name)

    def build_process_tap_description(
        self,
        process: AudioProcess,
        *,
        mute: bool = False,
    ) -> _FakeTapDescription:
        tap_description = _FakeTapDescription([process.audio_object_id])
        tap_description.name = f"catap recording {process.name}"
        tap_description.is_private = True
        tap_description.mute_behavior = "muted" if mute else "unmuted"
        return tap_description

    def build_system_tap_description(
        self,
        excluded: tuple[AudioProcess, ...] | list[AudioProcess] = (),
    ) -> _FakeTapDescription:
        tap_description = _FakeTapDescription(
            [process.audio_object_id for process in excluded],
            exclusive=True,
        )
        tap_description.name = "catap system recording"
        tap_description.is_private = True
        tap_description.mute_behavior = "unmuted"
        return tap_description

    def get_tap_description(self, tap_id: int) -> _FakeTapDescription:
        self.taps_described.append(tap_id)
        return _FakeTapDescription([tap_id])

    def create_process_tap(self, description: _FakeTapDescription) -> int:
        self.created_tap_ids.append(
            description.processes[0] if description.processes else 99
        )
        return 77

    def destroy_process_tap(self, tap_id: int) -> None:
        self.destroyed_tap_ids.append(tap_id)

    def create_recorder(
        self,
        tap_id: int,
        output_path: Path | None,
        on_data: object = None,
        *,
        max_pending_buffers: int = 256,
    ) -> _FakeRecorder:
        recorder = self.recorder_cls(
            tap_id,
            output_path,
            on_data,
            max_pending_buffers=max_pending_buffers,
        )
        self.created_recorders.append(recorder)
        return recorder


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
    assert session.tap_description.name == "catap system recording"
    assert session.tap_description.processes == [11, 12]
    assert session.tap_description.is_exclusive is True
    assert session.tap_description.is_private is True
    assert session.tap_description.mute_behavior == "unmuted"


def test_record_for_starts_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    destroyed_tap_ids: list[int] = []
    slept: list[float] = []
    backend = _FakeSessionBackend(
        process_lookup={"Music": process},
        destroyed_tap_ids=destroyed_tap_ids,
    )
    _install_backend(monkeypatch, backend)
    monkeypatch.setattr(session_module.time, "sleep", slept.append)

    session = session_module.record_process("Music", output_path="recording.wav")
    returned_session = session.record_for(2.5)

    assert returned_session is session
    assert session.tap_id is None
    assert session.is_recording is False
    assert slept == [2.5]
    assert len(backend.created_recorders) == 1
    fake_recorder = backend.created_recorders[0]
    assert fake_recorder.start_calls == 1
    assert fake_recorder.stop_calls == 1
    assert destroyed_tap_ids == [77]


def test_record_for_propagates_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed_tap_ids: list[int] = []
    slept: list[float] = []
    backend = _FakeSessionBackend(
        recorder_cls=_StartFailingRecorder,
        destroyed_tap_ids=destroyed_tap_ids,
    )
    _install_backend(monkeypatch, backend)
    monkeypatch.setattr(session_module.time, "sleep", slept.append)

    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(OSError, match="boom"):
        session.record_for(1.5)

    assert slept == []
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


def test_recording_session_requires_output_path_or_callback() -> None:
    with pytest.raises(
        ValueError,
        match="output_path must be provided unless on_data is set for streaming mode",
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
