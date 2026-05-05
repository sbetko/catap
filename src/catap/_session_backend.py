"""Internal backend seam for high-level recording sessions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from catap.audio_buffer import AudioBuffer, AudioStreamFormat
from catap.bindings.hardware import create_process_tap, destroy_process_tap
from catap.bindings.process import AudioProcess, find_process_by_name
from catap.bindings.tap import get_tap_description
from catap.bindings.tap_description import TapDescription, TapMuteBehavior
from catap.recorder import AudioRecorder


class _RecorderLike(Protocol):
    """Recorder methods used by the session layer."""

    is_recording: bool
    frames_recorded: int
    duration_seconds: float
    stream_format: AudioStreamFormat | None

    def start(self) -> None: ...

    def stop(self) -> None: ...


class _SessionBackend(Protocol):
    """Operations the session layer needs from the Core Audio backend."""

    def find_process_by_name(self, name: str) -> AudioProcess | None: ...

    def build_process_tap_description(
        self,
        process: AudioProcess,
        *,
        mute: bool = False,
    ) -> TapDescription: ...

    def build_system_tap_description(
        self,
        excluded: Sequence[AudioProcess] = (),
    ) -> TapDescription: ...

    def get_tap_description(self, tap_id: int) -> TapDescription: ...

    def create_process_tap(self, description: TapDescription) -> int: ...

    def destroy_process_tap(self, tap_id: int) -> None: ...

    def create_recorder(
        self,
        tap_id: int,
        output_path: Path | None,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        *,
        max_pending_buffers: int,
    ) -> _RecorderLike: ...


class _CoreAudioSessionBackend:
    """Production backend for ``RecordingSession``."""

    def find_process_by_name(self, name: str) -> AudioProcess | None:
        return find_process_by_name(name)

    def build_process_tap_description(
        self,
        process: AudioProcess,
        *,
        mute: bool = False,
    ) -> TapDescription:
        tap_description = TapDescription.stereo_mixdown_of_processes(
            [process.audio_object_id]
        )
        tap_description.name = f"catap recording {process.name}"
        tap_description.is_private = True
        tap_description.mute_behavior = (
            TapMuteBehavior.MUTED if mute else TapMuteBehavior.UNMUTED
        )
        return tap_description

    def build_system_tap_description(
        self,
        excluded: Sequence[AudioProcess] = (),
    ) -> TapDescription:
        tap_description = TapDescription.stereo_global_tap_excluding(
            [process.audio_object_id for process in excluded]
        )
        tap_description.name = "catap global recording"
        tap_description.is_private = True
        tap_description.mute_behavior = TapMuteBehavior.UNMUTED
        return tap_description

    def get_tap_description(self, tap_id: int) -> TapDescription:
        return get_tap_description(tap_id)

    def create_process_tap(self, description: TapDescription) -> int:
        return create_process_tap(description)

    def destroy_process_tap(self, tap_id: int) -> None:
        destroy_process_tap(tap_id)

    def create_recorder(
        self,
        tap_id: int,
        output_path: Path | None,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        *,
        max_pending_buffers: int,
    ) -> AudioRecorder:
        return AudioRecorder(
            tap_id,
            output_path,
            on_buffer=on_buffer,
            max_pending_buffers=max_pending_buffers,
        )


_DEFAULT_SESSION_BACKEND: _SessionBackend = _CoreAudioSessionBackend()
