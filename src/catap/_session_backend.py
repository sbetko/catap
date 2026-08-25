"""Internal backend seam for high-level recording sessions."""

from __future__ import annotations

import ctypes
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from catap._multitrack import MultitrackAudioRecorder
from catap.audio_buffer import AudioBuffer, AudioStreamFormat
from catap.bindings.device import (
    AudioDevice,
    AudioDeviceStream,
    find_audio_device_by_name,
    find_audio_device_by_uid,
    list_audio_devices,
)
from catap.bindings.hardware import create_process_tap, destroy_process_tap
from catap.bindings.process import AudioProcess, find_process_by_name
from catap.bindings.tap import get_tap_description, set_tap_description
from catap.bindings.tap_description import TapDescription, TapMuteBehavior
from catap.drift import DriftCompensationQuality
from catap.recorder import AudioRecorder


class _RecorderLike(Protocol):
    """Recorder methods used by the session layer."""

    is_recording: bool
    needs_cleanup: bool
    capture_failed: bool
    captured_only_silence: bool
    frames_recorded: int
    duration_seconds: float
    stream_format: AudioStreamFormat | None

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def wait_for_capture_failure(self, timeout: float | None = None) -> bool: ...


class _MultitrackRecorderLike(Protocol):
    """Multitrack recorder methods used by the session layer."""

    is_recording: bool
    needs_cleanup: bool
    capture_failed: bool
    captured_only_silence: bool
    track_captured_only_silence: tuple[bool, ...]
    frames_recorded: int
    duration_seconds: float
    stream_formats: list[AudioStreamFormat]

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def wait_for_capture_failure(self, timeout: float | None = None) -> bool: ...


def _normalize_mute_behavior(mute: bool | TapMuteBehavior) -> TapMuteBehavior:
    """Map the public ``mute`` argument onto a tap mute behavior."""
    if isinstance(mute, TapMuteBehavior):
        return mute
    return TapMuteBehavior.MUTED if mute else TapMuteBehavior.UNMUTED


def _apply_common_tap_options(
    tap_description: TapDescription,
    name: str,
    *,
    mute: bool | TapMuteBehavior,
    visible: bool,
) -> TapDescription:
    """Apply the option set shared by every session tap description."""
    tap_description.name = name
    tap_description.is_private = not visible
    tap_description.mute_behavior = _normalize_mute_behavior(mute)
    return tap_description


class _SessionBackend(Protocol):
    """Operations the session layer needs from the Core Audio backend."""

    def find_process_by_name(self, name: str) -> AudioProcess | None: ...

    def find_audio_device(self, query: str) -> AudioDevice | None: ...

    def build_processes_tap_description(
        self,
        processes: Sequence[AudioProcess],
        *,
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        visible: bool = False,
    ) -> TapDescription: ...

    def build_system_tap_description(
        self,
        excluded: Sequence[AudioProcess] = (),
        *,
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        visible: bool = False,
    ) -> TapDescription: ...

    def build_device_tap_description(
        self,
        stream: AudioDeviceStream,
        *,
        included: Sequence[AudioProcess] = (),
        excluded: Sequence[AudioProcess] = (),
        mute: bool | TapMuteBehavior = False,
        visible: bool = False,
    ) -> TapDescription: ...

    def build_bundle_tap_description(
        self,
        bundle_ids: Sequence[str],
        *,
        restore: bool = True,
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        visible: bool = False,
    ) -> TapDescription: ...

    def get_tap_description(self, tap_id: int) -> TapDescription: ...

    def set_tap_description(self, tap_id: int, description: TapDescription) -> None: ...

    def create_process_tap(
        self,
        description: TapDescription,
        *,
        out: ctypes.c_uint32 | None = None,
    ) -> int: ...

    def destroy_process_tap(self, tap_id: int) -> None: ...

    def create_recorder(
        self,
        tap_id: int,
        output_path: Path | None,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        *,
        max_pending_buffers: int,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> _RecorderLike: ...

    def find_default_input_device(self) -> AudioDevice | None: ...

    def create_multitrack_recorder(
        self,
        tap_ids: Sequence[int],
        output_paths: Sequence[Path | None],
        on_track_buffer: Callable[[int, AudioBuffer], None] | None = None,
        *,
        max_pending_buffers: int,
        input_device_uid: str | None = None,
        input_stream_count: int = 0,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> _MultitrackRecorderLike: ...


class _CoreAudioSessionBackend:
    """Production backend for ``RecordingSession``."""

    def find_process_by_name(self, name: str) -> AudioProcess | None:
        return find_process_by_name(name)

    def find_audio_device(self, query: str) -> AudioDevice | None:
        device = find_audio_device_by_uid(query)
        if device is not None:
            return device
        return find_audio_device_by_name(query)

    def build_processes_tap_description(
        self,
        processes: Sequence[AudioProcess],
        *,
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        visible: bool = False,
    ) -> TapDescription:
        process_ids = [process.audio_object_id for process in processes]
        tap_description = (
            TapDescription.mono_mixdown_of_processes(process_ids)
            if mono
            else TapDescription.stereo_mixdown_of_processes(process_ids)
        )
        names = ", ".join(process.name for process in processes)
        return _apply_common_tap_options(
            tap_description,
            f"catap recording {names}",
            mute=mute,
            visible=visible,
        )

    def build_system_tap_description(
        self,
        excluded: Sequence[AudioProcess] = (),
        *,
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        visible: bool = False,
    ) -> TapDescription:
        excluded_ids = [process.audio_object_id for process in excluded]
        tap_description = (
            TapDescription.mono_global_tap_excluding(excluded_ids)
            if mono
            else TapDescription.stereo_global_tap_excluding(excluded_ids)
        )
        return _apply_common_tap_options(
            tap_description,
            "catap global recording",
            mute=mute,
            visible=visible,
        )

    def build_device_tap_description(
        self,
        stream: AudioDeviceStream,
        *,
        included: Sequence[AudioProcess] = (),
        excluded: Sequence[AudioProcess] = (),
        mute: bool | TapMuteBehavior = False,
        visible: bool = False,
    ) -> TapDescription:
        if included and excluded:
            raise ValueError(
                "Device taps accept an include list or an exclude list, not both"
            )
        if included:
            tap_description = TapDescription.of_processes_for_device_stream(
                [process.audio_object_id for process in included],
                stream,
            )
        else:
            tap_description = TapDescription.excluding_processes_for_device_stream(
                [process.audio_object_id for process in excluded],
                stream,
            )
        device_name = stream.device_name or stream.device_uid
        return _apply_common_tap_options(
            tap_description,
            f"catap recording device {device_name}",
            mute=mute,
            visible=visible,
        )

    def build_bundle_tap_description(
        self,
        bundle_ids: Sequence[str],
        *,
        restore: bool = True,
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        visible: bool = False,
    ) -> TapDescription:
        tap_description = (
            TapDescription.mono_mixdown_of_processes([])
            if mono
            else TapDescription.stereo_mixdown_of_processes([])
        )
        tap_description.bundle_ids = list(bundle_ids)
        tap_description.process_restore_enabled = restore
        names = ", ".join(bundle_ids)
        return _apply_common_tap_options(
            tap_description,
            f"catap recording {names}",
            mute=mute,
            visible=visible,
        )

    def get_tap_description(self, tap_id: int) -> TapDescription:
        return get_tap_description(tap_id)

    def set_tap_description(self, tap_id: int, description: TapDescription) -> None:
        set_tap_description(tap_id, description)

    def create_process_tap(
        self,
        description: TapDescription,
        *,
        out: ctypes.c_uint32 | None = None,
    ) -> int:
        return create_process_tap(description, out=out)

    def destroy_process_tap(self, tap_id: int) -> None:
        destroy_process_tap(tap_id)

    def create_recorder(
        self,
        tap_id: int,
        output_path: Path | None,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        *,
        max_pending_buffers: int,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> AudioRecorder:
        return AudioRecorder(
            tap_id,
            output_path,
            on_buffer=on_buffer,
            max_pending_buffers=max_pending_buffers,
            drift_compensation_quality=drift_compensation_quality,
        )

    def find_default_input_device(self) -> AudioDevice | None:
        for device in list_audio_devices():
            if device.is_default_input:
                return device
        return None

    def create_multitrack_recorder(
        self,
        tap_ids: Sequence[int],
        output_paths: Sequence[Path | None],
        on_track_buffer: Callable[[int, AudioBuffer], None] | None = None,
        *,
        max_pending_buffers: int,
        input_device_uid: str | None = None,
        input_stream_count: int = 0,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> MultitrackAudioRecorder:
        return MultitrackAudioRecorder(
            tap_ids,
            output_paths,
            on_track_buffer=on_track_buffer,
            max_pending_buffers=max_pending_buffers,
            input_device_uid=input_device_uid,
            input_stream_count=input_stream_count,
            drift_compensation_quality=drift_compensation_quality,
        )


_DEFAULT_SESSION_BACKEND: _SessionBackend = _CoreAudioSessionBackend()
