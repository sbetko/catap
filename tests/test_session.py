"""High-level recording session tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import catap.session as session_module
from catap.bindings.process import AmbiguousAudioProcessError, AudioProcess
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


def _patch_session_symbols(
    monkeypatch: pytest.MonkeyPatch,
    *,
    process_lookup: dict[str, AudioProcess] | None = None,
    recorder_cls: type[_FakeRecorder] = _FakeRecorder,
    created_tap_ids: list[int] | None = None,
    destroyed_tap_ids: list[int] | None = None,
) -> None:
    process_lookup = process_lookup or {}
    created_tap_ids = created_tap_ids if created_tap_ids is not None else []
    destroyed_tap_ids = destroyed_tap_ids if destroyed_tap_ids is not None else []

    def _find_process_by_name(name: str) -> AudioProcess | None:
        return process_lookup.get(name)

    def _create_process_tap(description: _FakeTapDescription) -> int:
        created_tap_ids.append(
            description.processes[0] if description.processes else 99
        )
        return 77

    def _destroy_process_tap(tap_id: int) -> None:
        destroyed_tap_ids.append(tap_id)

    monkeypatch.setattr(session_module, "TapDescription", _FakeTapDescription)
    monkeypatch.setattr(
        session_module,
        "TapMuteBehavior",
        SimpleNamespace(UNMUTED="unmuted", MUTED="muted"),
    )
    monkeypatch.setattr(session_module, "find_process_by_name", _find_process_by_name)
    monkeypatch.setattr(session_module, "create_process_tap", _create_process_tap)
    monkeypatch.setattr(session_module, "destroy_process_tap", _destroy_process_tap)
    monkeypatch.setattr(session_module, "AudioRecorder", recorder_cls)


def test_record_process_context_manager_manages_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    destroyed_tap_ids: list[int] = []
    _patch_session_symbols(
        monkeypatch,
        process_lookup={"Music": process},
        destroyed_tap_ids=destroyed_tap_ids,
    )

    session = session_module.record_process(
        "Music",
        output_path="recording.wav",
        mute=True,
    )
    recorder = session.recorder

    assert session.source_process == process
    assert session.tap_description.name == "catap recording Music"
    assert session.tap_description.processes == [11]
    assert session.tap_description.is_private is True
    assert session.tap_description.mute_behavior == "muted"

    with session as active_session:
        assert active_session.tap_id == 77
        assert active_session.is_recording is True
        assert active_session.recorder is not None
        assert active_session.recorder.output_path == Path("recording.wav")
        assert active_session.recorder.max_pending_buffers == 256
        recorder = active_session.recorder

    assert session.tap_id is None
    assert session.is_recording is False
    assert session.duration_seconds == 0.5
    fake_recorder = cast(_FakeRecorder, recorder)
    assert fake_recorder.start_calls == 1
    assert fake_recorder.stop_calls == 1
    assert destroyed_tap_ids == [77]


def test_record_process_raises_for_missing_process_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session_symbols(monkeypatch)

    with pytest.raises(
        session_module.AudioProcessNotFoundError,
        match="No audio process found matching 'Missing'",
    ):
        session_module.record_process("Missing")


def test_record_process_propagates_ambiguous_process_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    _patch_session_symbols(monkeypatch)

    def _raise_ambiguous(name: str) -> AudioProcess | None:
        raise AmbiguousAudioProcessError(name, [process, process])

    monkeypatch.setattr(session_module, "find_process_by_name", _raise_ambiguous)

    with pytest.raises(AmbiguousAudioProcessError, match="Multiple audio processes"):
        session_module.record_process("Music")


def test_recording_session_start_cleans_up_tap_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed_tap_ids: list[int] = []
    _patch_session_symbols(
        monkeypatch,
        recorder_cls=_StartFailingRecorder,
        destroyed_tap_ids=destroyed_tap_ids,
    )

    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(OSError, match="boom"):
        session.start()

    assert session.tap_id is None
    assert session.recorder is None
    assert destroyed_tap_ids == [77]


def test_record_system_audio_tracks_excluded_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)
    _patch_session_symbols(
        monkeypatch,
        process_lookup={"Music": music},
    )

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
    _patch_session_symbols(
        monkeypatch,
        process_lookup={"Music": process},
        destroyed_tap_ids=destroyed_tap_ids,
    )
    monkeypatch.setattr(session_module.time, "sleep", slept.append)

    session = session_module.record_process("Music", output_path="recording.wav")
    returned_session = session.record_for(2.5)

    assert returned_session is session
    assert session.tap_id is None
    assert session.is_recording is False
    assert slept == [2.5]
    assert session.recorder is not None
    fake_recorder = cast(_FakeRecorder, session.recorder)
    assert fake_recorder.start_calls == 1
    assert fake_recorder.stop_calls == 1
    assert destroyed_tap_ids == [77]


def test_record_for_propagates_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed_tap_ids: list[int] = []
    slept: list[float] = []
    _patch_session_symbols(
        monkeypatch,
        recorder_cls=_StartFailingRecorder,
        destroyed_tap_ids=destroyed_tap_ids,
    )
    monkeypatch.setattr(session_module.time, "sleep", slept.append)

    session = session_module.RecordingSession(
        cast(TapDescription, _FakeTapDescription([42])),
        output_path="recording.wav",
    )

    with pytest.raises(OSError, match="boom"):
        session.record_for(1.5)

    assert slept == []
    assert session.tap_id is None
    assert session.recorder is None
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
    _patch_session_symbols(monkeypatch, process_lookup={"Music": process})

    session = session_module.record_process(
        "Music",
        output_path="recording.wav",
        max_pending_buffers=32,
    )

    assert session.max_pending_buffers == 32

    with session:
        assert session.recorder is not None
        assert session.recorder.max_pending_buffers == 32


def test_record_process_rejects_non_positive_max_pending_buffers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    _patch_session_symbols(monkeypatch, process_lookup={"Music": process})

    with pytest.raises(
        ValueError,
        match="max_pending_buffers must be greater than 0",
    ):
        session_module.record_process(
            "Music",
            output_path="recording.wav",
            max_pending_buffers=0,
        )
