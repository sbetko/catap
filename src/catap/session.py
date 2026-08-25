"""High-level recording session API."""

from __future__ import annotations

import ctypes
import math
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Self

from catap._recording_support import (
    _DEFAULT_MAX_PENDING_BUFFERS,
    _add_secondary_failure,
    _combine_errors,
    _validate_max_pending_buffers,
    _validate_recording_target,
)
from catap._session_backend import (
    _DEFAULT_SESSION_BACKEND,
    _MultitrackRecorderLike,
    _normalize_mute_behavior,
    _RecorderLike,
    _SessionBackend,
)
from catap.audio_buffer import AudioBuffer, AudioStreamFormat
from catap.bindings._coreaudio import kAudioHardwareBadObjectError
from catap.bindings.device import AudioDevice, AudioDeviceStream
from catap.bindings.process import (
    AmbiguousAudioProcessError,
    AudioProcess,
)
from catap.bindings.tap import AudioTap
from catap.bindings.tap_description import TapDescription, TapMuteBehavior
from catap.drift import (
    DriftCompensationQuality,
    _validate_drift_compensation_quality,
)


class AudioProcessNotFoundError(LookupError):
    """Raised when a named audio process cannot be found."""


class AudioDeviceNotFoundError(LookupError):
    """Raised when a named audio device cannot be found."""


def _resolve_process(
    process: str | AudioProcess,
    backend: _SessionBackend,
) -> AudioProcess:
    """Resolve a process name into an AudioProcess."""
    if isinstance(process, AudioProcess):
        return process

    resolved = backend.find_process_by_name(process)
    if resolved is None:
        raise AudioProcessNotFoundError(f"No audio process found matching '{process}'")

    return resolved


def _resolve_processes(
    processes: Sequence[str | AudioProcess],
    backend: _SessionBackend,
) -> list[AudioProcess]:
    """Resolve a list of process specifiers into AudioProcess objects."""
    return [_resolve_process(process, backend) for process in processes]


def _resolve_device_stream(
    device: str | AudioDevice | AudioDeviceStream,
    stream: int | None,
    backend: _SessionBackend,
) -> AudioDeviceStream:
    """Resolve a device specifier into one output device stream."""
    if isinstance(device, AudioDeviceStream):
        if stream is not None and stream != device.stream_index:
            raise ValueError(
                f"stream={stream} conflicts with the provided stream's index "
                f"{device.stream_index}"
            )
        if device.direction != "output":
            raise ValueError(
                "Device taps require an output stream; got "
                f"{device.direction!r} stream {device.stream_index} on "
                f"{device.device_name or device.device_uid!r}"
            )
        return device

    if isinstance(device, AudioDevice):
        resolved_device = device
    else:
        found = backend.find_audio_device(device)
        if found is None:
            raise AudioDeviceNotFoundError(f"No audio device found matching '{device}'")
        resolved_device = found

    output_streams = resolved_device.output_streams
    if not output_streams:
        raise ValueError(
            f"Audio device {resolved_device.name!r} has no output streams to tap"
        )
    stream_index = 0 if stream is None else stream
    for output_stream in output_streams:
        if output_stream.stream_index == stream_index:
            return output_stream
    raise ValueError(
        f"Audio device {resolved_device.name!r} has no output stream "
        f"{stream_index}; available: "
        f"{[output_stream.stream_index for output_stream in output_streams]}"
    )


def _recorder_requires_cleanup(
    recorder: _RecorderLike | _MultitrackRecorderLike | None,
) -> bool:
    """Return whether a recorder is active or owns retryable cleanup state."""
    return recorder is not None and (recorder.is_recording or recorder.needs_cleanup)


def build_process_tap_description(
    process: AudioProcess,
    *,
    mute: bool | TapMuteBehavior = False,
    mono: bool = False,
    visible: bool = False,
) -> TapDescription:
    """Build the mixdown tap description for one process."""
    return _DEFAULT_SESSION_BACKEND.build_processes_tap_description(
        [process],
        mute=mute,
        mono=mono,
        visible=visible,
    )


def build_processes_tap_description(
    processes: Sequence[AudioProcess],
    *,
    mute: bool | TapMuteBehavior = False,
    mono: bool = False,
    visible: bool = False,
) -> TapDescription:
    """Build one mixdown tap description covering several processes."""
    return _DEFAULT_SESSION_BACKEND.build_processes_tap_description(
        processes,
        mute=mute,
        mono=mono,
        visible=visible,
    )


def build_system_tap_description(
    excluded: Sequence[AudioProcess] = (),
    *,
    mute: bool | TapMuteBehavior = False,
    mono: bool = False,
    visible: bool = False,
) -> TapDescription:
    """Build the global tap description for process output."""
    return _DEFAULT_SESSION_BACKEND.build_system_tap_description(
        excluded,
        mute=mute,
        mono=mono,
        visible=visible,
    )


def build_device_tap_description(
    stream: AudioDeviceStream,
    *,
    included: Sequence[AudioProcess] = (),
    excluded: Sequence[AudioProcess] = (),
    mute: bool | TapMuteBehavior = False,
    visible: bool = False,
) -> TapDescription:
    """Build a tap description for one output device stream."""
    return _DEFAULT_SESSION_BACKEND.build_device_tap_description(
        stream,
        included=included,
        excluded=excluded,
        mute=mute,
        visible=visible,
    )


def build_bundle_tap_description(
    bundle_ids: Sequence[str],
    *,
    restore: bool = True,
    mute: bool | TapMuteBehavior = False,
    mono: bool = False,
    visible: bool = False,
) -> TapDescription:
    """Build a bundle-ID tap description (macOS 26 and later)."""
    return _DEFAULT_SESSION_BACKEND.build_bundle_tap_description(
        bundle_ids,
        restore=restore,
        mute=mute,
        mono=mono,
        visible=visible,
    )


class RecordingSession:
    """
    Recording session that owns tap and recorder cleanup.

    This is the higher-level API for common capture flows. It wraps the
    lower-level tap creation and AudioRecorder startup/shutdown steps so users
    can focus on what to record rather than which Core Audio objects need to be
    cleaned up.
    """

    def __init__(
        self,
        tap_description: TapDescription,
        output_path: str | Path | None = None,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        *,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        drift_compensation_quality: DriftCompensationQuality | None = None,
        _backend: _SessionBackend | None = None,
    ) -> None:
        """
        Create a recording session.

        Args:
            tap_description: Tap description to create when recording starts
            output_path: Path to write a WAV file, or None for streaming mode
            on_buffer: Optional callback invoked with an ``AudioBuffer`` for
                each captured buffer. The buffer's data is safe to retain.
                Runs on catap's background worker thread, not on Core Audio's
                real-time callback thread.
            max_pending_buffers: Maximum number of audio buffers to queue for
                the background worker before new buffers are dropped and the
                capture fails on stop. Higher values trade memory for tolerance
                of slow disk writes or ``on_buffer`` callbacks.
            drift_compensation_quality: Optional aggregate-device resampling
                quality. ``None`` preserves Core Audio's default.
        Raises:
            ValueError: If neither ``output_path`` nor ``on_buffer`` is provided
        """
        self.tap_description = tap_description
        self.output_path = _validate_recording_target(output_path, on_buffer)
        self._on_buffer = on_buffer
        self._max_pending_buffers = _validate_max_pending_buffers(max_pending_buffers)
        self.drift_compensation_quality = _validate_drift_compensation_quality(
            drift_compensation_quality
        )
        self._backend = _DEFAULT_SESSION_BACKEND if _backend is None else _backend

        self.source_process: AudioProcess | None = None
        self.source_processes: tuple[AudioProcess, ...] = ()
        self.source_tap: AudioTap | None = None
        self.source_device_stream: AudioDeviceStream | None = None
        self.excluded_processes: tuple[AudioProcess, ...] = ()

        self._existing_tap_id: int | None = None
        self._owns_tap = True
        self._tap_id: int | None = None
        self._recorder: _RecorderLike | None = None

    @classmethod
    def from_process(
        cls,
        process: str | AudioProcess,
        output_path: str | Path | None = None,
        *,
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        visible: bool = False,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> Self:
        """
        Create a session for recording one application's audio.

        Args:
            process: Application name or AudioProcess to record
            output_path: Path to write a WAV file, or None for streaming mode
            mute: Mute app playback while still capturing audio. Pass a
                ``TapMuteBehavior`` for the full behavior set, including
                ``MUTED_WHEN_TAPPED``.
            mono: Mix the capture down to mono instead of stereo
            visible: Make the tap visible to other audio clients instead of
                private to this process
            on_buffer: Optional streaming callback. See ``RecordingSession`` for
                buffer format and threading details.
            max_pending_buffers: Queue bound for the background worker. See
                ``RecordingSession`` for details.
            drift_compensation_quality: Optional aggregate-device resampling
                quality. ``None`` preserves Core Audio's default.

        Raises:
            AudioProcessNotFoundError: If the named app cannot be found
        """
        session = cls.from_processes(
            [process],
            output_path=output_path,
            mute=mute,
            mono=mono,
            visible=visible,
            on_buffer=on_buffer,
            max_pending_buffers=max_pending_buffers,
            drift_compensation_quality=drift_compensation_quality,
        )
        return session

    @classmethod
    def from_processes(
        cls,
        processes: Sequence[str | AudioProcess],
        output_path: str | Path | None = None,
        *,
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        visible: bool = False,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> Self:
        """
        Create a session recording several applications through one tap.

        The tapped applications are mixed into a single stereo (or mono)
        capture. See ``from_process`` for the option set.

        Raises:
            AudioProcessNotFoundError: If a named app cannot be found
            ValueError: If ``processes`` is empty
        """
        if not processes:
            raise ValueError("At least one process is required")

        backend = _DEFAULT_SESSION_BACKEND
        resolved_processes = _resolve_processes(processes, backend)
        tap_description = backend.build_processes_tap_description(
            resolved_processes,
            mute=mute,
            mono=mono,
            visible=visible,
        )

        session = cls(
            tap_description,
            output_path=output_path,
            on_buffer=on_buffer,
            max_pending_buffers=max_pending_buffers,
            drift_compensation_quality=drift_compensation_quality,
            _backend=backend,
        )
        session.source_processes = tuple(resolved_processes)
        session.source_process = resolved_processes[0]
        return session

    @classmethod
    def from_system_audio(
        cls,
        output_path: str | Path | None = None,
        *,
        exclude: Sequence[str | AudioProcess] = (),
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        visible: bool = False,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> Self:
        """
        Create a session for recording a global process-output mix.

        Args:
            output_path: Path to write a WAV file, or None for streaming mode
            exclude: Apps to exclude from the global tap
            mute: Mute behavior for the tapped audio. ``True`` silences every
                tapped process while recording, so the system plays no audio
                aloud until the tap is destroyed.
            mono: Mix the capture down to mono instead of stereo
            visible: Make the tap visible to other audio clients instead of
                private to this process
            on_buffer: Optional streaming callback. See ``RecordingSession`` for
                buffer format and threading details.
            max_pending_buffers: Queue bound for the background worker. See
                ``RecordingSession`` for details.

        Raises:
            AudioProcessNotFoundError: If an excluded app name cannot be found
        """
        backend = _DEFAULT_SESSION_BACKEND
        excluded_processes = _resolve_processes(exclude, backend)
        tap_description = backend.build_system_tap_description(
            excluded_processes,
            mute=mute,
            mono=mono,
            visible=visible,
        )

        session = cls(
            tap_description,
            output_path=output_path,
            on_buffer=on_buffer,
            max_pending_buffers=max_pending_buffers,
            drift_compensation_quality=drift_compensation_quality,
            _backend=backend,
        )
        session.excluded_processes = tuple(excluded_processes)
        return session

    @classmethod
    def from_device(
        cls,
        device: str | AudioDevice | AudioDeviceStream,
        output_path: str | Path | None = None,
        *,
        stream: int | None = None,
        include: Sequence[str | AudioProcess] = (),
        exclude: Sequence[str | AudioProcess] = (),
        mute: bool | TapMuteBehavior = False,
        visible: bool = False,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> Self:
        """
        Create a session recording audio routed to one output device stream.

        The tap format follows the device stream instead of a stereo mixdown.

        Args:
            device: Device name, UID, ``AudioDevice``, or ``AudioDeviceStream``
            output_path: Path to write a WAV file, or None for streaming mode
            stream: Output stream index on the device (default: first output
                stream; ignored when ``device`` is already a stream)
            include: Record only these apps' audio on the device
            exclude: Record everything on the device except these apps
                (mutually exclusive with ``include``)
            mute: Mute behavior for the tapped audio
            visible: Make the tap visible to other audio clients
            on_buffer: Optional streaming callback. See ``RecordingSession``
                for buffer format and threading details.
            max_pending_buffers: Queue bound for the background worker

        Raises:
            AudioDeviceNotFoundError: If a named device cannot be found
            AudioProcessNotFoundError: If a named app cannot be found
            ValueError: If both ``include`` and ``exclude`` are given, or the
                requested stream does not exist
        """
        backend = _DEFAULT_SESSION_BACKEND
        device_stream = _resolve_device_stream(device, stream, backend)
        included_processes = _resolve_processes(include, backend)
        excluded_processes = _resolve_processes(exclude, backend)
        tap_description = backend.build_device_tap_description(
            device_stream,
            included=included_processes,
            excluded=excluded_processes,
            mute=mute,
            visible=visible,
        )

        session = cls(
            tap_description,
            output_path=output_path,
            on_buffer=on_buffer,
            max_pending_buffers=max_pending_buffers,
            drift_compensation_quality=drift_compensation_quality,
            _backend=backend,
        )
        session.source_device_stream = device_stream
        session.source_processes = tuple(included_processes)
        session.excluded_processes = tuple(excluded_processes)
        return session

    @classmethod
    def from_bundle_ids(
        cls,
        bundle_ids: Sequence[str],
        output_path: str | Path | None = None,
        *,
        restore: bool = True,
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        visible: bool = False,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> Self:
        """
        Create a session recording applications by bundle ID (macOS 26+).

        Unlike process-object taps, bundle-ID taps do not require the target
        applications to be running audio clients yet, and with ``restore``
        enabled the tap re-attaches to applications that exit and restart
        during the capture.

        Args:
            bundle_ids: Bundle identifiers to tap (for example
                ``"com.apple.Music"``)
            output_path: Path to write a WAV file, or None for streaming mode
            restore: Re-attach tapped applications when they restart
            mute: Mute behavior for the tapped audio
            mono: Mix the capture down to mono instead of stereo
            visible: Make the tap visible to other audio clients
            on_buffer: Optional streaming callback. See ``RecordingSession``
                for buffer format and threading details.
            max_pending_buffers: Queue bound for the background worker

        Raises:
            RuntimeError: On macOS versions before 26, which lack bundle-ID
                tap support
            ValueError: If ``bundle_ids`` is empty
        """
        if not bundle_ids:
            raise ValueError("At least one bundle ID is required")

        backend = _DEFAULT_SESSION_BACKEND
        tap_description = backend.build_bundle_tap_description(
            bundle_ids,
            restore=restore,
            mute=mute,
            mono=mono,
            visible=visible,
        )

        return cls(
            tap_description,
            output_path=output_path,
            on_buffer=on_buffer,
            max_pending_buffers=max_pending_buffers,
            drift_compensation_quality=drift_compensation_quality,
            _backend=backend,
        )

    @classmethod
    def from_tap(
        cls,
        tap: int | AudioTap,
        output_path: str | Path | None = None,
        *,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> Self:
        """Create a session that records from an existing tap."""
        backend = _DEFAULT_SESSION_BACKEND
        source_tap = tap if isinstance(tap, AudioTap) else None
        tap_id = tap.audio_object_id if isinstance(tap, AudioTap) else tap
        tap_description = (
            source_tap.description
            if source_tap
            else backend.get_tap_description(tap_id)
        )

        session = cls(
            tap_description,
            output_path=output_path,
            on_buffer=on_buffer,
            max_pending_buffers=max_pending_buffers,
            drift_compensation_quality=drift_compensation_quality,
            _backend=backend,
        )
        session._existing_tap_id = tap_id
        session._owns_tap = False
        session.source_tap = source_tap
        return session

    @property
    def tap_id(self) -> int | None:
        """Current Core Audio tap ID, including one awaiting cleanup."""
        return self._tap_id

    @property
    def is_recording(self) -> bool:
        """True while audio capture is active."""
        return self._recorder is not None and self._recorder.is_recording

    @property
    def needs_cleanup(self) -> bool:
        """True when a failed stop or close still owns retryable resources.

        Call ``close()`` again to retry the teardown. The tap survives until
        cleanup succeeds, so a tap created with ``mute=True`` keeps its
        process muted until a retry completes.
        """
        return not self.is_recording and (
            (self._recorder is not None and self._recorder.needs_cleanup)
            or self._tap_id is not None
        )

    @property
    def capture_failed(self) -> bool:
        """True once a live or background capture failure was detected."""
        return self._recorder is not None and self._recorder.capture_failed

    def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
        """Wait for a passive failure signal from the active recorder.

        The wait never stops the session. The owning thread must still call
        :meth:`stop` or :meth:`close` to report the detailed failure, discard
        unsafe output, and release Core Audio objects.
        """
        recorder = self._recorder
        if recorder is None:
            raise RuntimeError("Recording has not started")
        if not recorder.is_recording:
            raise RuntimeError("Not recording")
        return recorder.wait_for_capture_failure(timeout)

    @property
    def frames_recorded(self) -> int:
        """Number of frames recorded in the current or most recent capture."""
        if self._recorder is None:
            return 0
        return self._recorder.frames_recorded

    @property
    def captured_only_silence(self) -> bool:
        """True while the current or most recent capture holds only zeros.

        An all-zero capture while audio was playing usually means macOS never
        granted system-audio recording permission to the app hosting this
        process, so Core Audio delivered zeroed buffers.
        """
        if self._recorder is None:
            return True
        return self._recorder.captured_only_silence

    @property
    def duration_seconds(self) -> float:
        """Recorded duration in seconds."""
        if self._recorder is None:
            return 0.0
        return self._recorder.duration_seconds

    @property
    def stream_format(self) -> AudioStreamFormat | None:
        """Native callback stream format, once known."""
        if self._recorder is None:
            return None
        return self._recorder.stream_format

    @property
    def max_pending_buffers(self) -> int:
        """Maximum number of queued audio buffers before overflow."""
        return self._max_pending_buffers

    def start(self) -> None:
        """
        Start recording.

        Raises:
            OSError: If the tap or recorder cannot be started
            RuntimeError: If already recording
        """
        if self.is_recording:
            raise RuntimeError("Already recording")
        if self._tap_id is not None:
            raise RuntimeError(
                "Session has pending tap cleanup; call close() before restarting"
            )

        # Core Audio writes the new tap's ID into this caller-owned box, so an
        # interruption between tap creation and storing self._tap_id cannot
        # lose the only reference to the tap.
        created_tap_id = ctypes.c_uint32(0)
        try:
            if self._existing_tap_id is not None:
                tap_id = self._existing_tap_id
            else:
                tap_id = self._backend.create_process_tap(
                    self.tap_description,
                    out=created_tap_id,
                )
            self._tap_id = tap_id
            if self.drift_compensation_quality is None:
                recorder = self._backend.create_recorder(
                    tap_id,
                    self.output_path,
                    on_buffer=self._on_buffer,
                    max_pending_buffers=self._max_pending_buffers,
                )
            else:
                recorder = self._backend.create_recorder(
                    tap_id,
                    self.output_path,
                    on_buffer=self._on_buffer,
                    max_pending_buffers=self._max_pending_buffers,
                    drift_compensation_quality=self.drift_compensation_quality,
                )
            self._recorder = recorder
            recorder.start()
        except BaseException as exc:
            if self._tap_id is None and created_tap_id.value:
                self._tap_id = created_tap_id.value
            if not _recorder_requires_cleanup(self._recorder):
                try:
                    cleanup_error = self._destroy_tap()
                except BaseException as cleanup_exc:
                    _add_secondary_failure(
                        exc,
                        "Cleanup failure during session startup",
                        cleanup_exc,
                    )
                else:
                    if cleanup_error is not None:
                        _add_secondary_failure(
                            exc,
                            "Cleanup failure during session startup",
                            cleanup_error,
                        )
                finally:
                    self._recorder = None
            raise

    def stop(self) -> None:
        """
        Stop recording and destroy the tap.

        Raises:
            RuntimeError: If not recording
            OSError: If stopping or cleanup fails
        """
        recorder = self._recorder
        recorder_requires_cleanup = _recorder_requires_cleanup(recorder)
        if not recorder_requires_cleanup and self._tap_id is None:
            raise RuntimeError("Not recording")

        stop_error: BaseException | None = None
        if recorder_requires_cleanup:
            assert recorder is not None
            try:
                recorder.stop()
            except BaseException as exc:
                stop_error = exc
            if _recorder_requires_cleanup(recorder) and stop_error is None:
                stop_error = RuntimeError("Recorder cleanup remained pending")

        destroy_error: BaseException | None = None
        if not _recorder_requires_cleanup(recorder):
            try:
                destroy_error = self._destroy_tap()
            except BaseException as exc:
                destroy_error = exc

        errors = [error for error in (stop_error, destroy_error) if error is not None]
        if errors:
            raise _combine_errors("Failed to stop recording session", errors)

    def close(self) -> None:
        """
        Close the session and release any active resources.

        This method is idempotent.
        """
        recorder = self._recorder
        stop_error: BaseException | None = None
        if _recorder_requires_cleanup(recorder):
            assert recorder is not None
            try:
                recorder.stop()
            except BaseException as exc:
                stop_error = exc
            if _recorder_requires_cleanup(recorder) and stop_error is None:
                stop_error = RuntimeError("Recorder cleanup remained pending")

        destroy_error: BaseException | None = None
        if not _recorder_requires_cleanup(recorder):
            try:
                destroy_error = self._destroy_tap()
            except BaseException as exc:
                destroy_error = exc

        errors = [error for error in (stop_error, destroy_error) if error is not None]
        if errors:
            raise _combine_errors("Failed to close recording session", errors)

    def record_for(self, duration: float) -> Self:
        """
        Record for a fixed amount of time.

        Args:
            duration: Recording duration in seconds

        Returns:
            This session instance

        Raises:
            ValueError: If duration is not finite and positive
        """
        if not math.isfinite(duration):
            raise ValueError("duration must be finite")
        if duration <= 0:
            raise ValueError("duration must be greater than 0")

        self.start()
        try:
            self.wait_for_capture_failure(duration)
        except BaseException as exc:
            try:
                self.close()
            except BaseException as cleanup_exc:
                _add_secondary_failure(
                    exc,
                    "Cleanup failure while recording for a fixed duration",
                    cleanup_exc,
                )
            raise
        else:
            self.close()

        return self

    def _require_live_tap(self) -> int:
        """Return the live tap's ID or explain why there is none."""
        if self._tap_id is None:
            raise RuntimeError("Session has no live tap to modify; call start() first")
        return self._tap_id

    def set_processes(self, processes: Sequence[str | AudioProcess]) -> None:
        """Retarget the live tap at a different set of processes.

        The tap keeps flowing while it is retargeted, so the capture
        continues seamlessly with the new process set. This operation is
        limited to inclusive process-list taps: on an exclusive tap the
        process list means exclusions, while bundle-ID taps use a separate
        target list. Requires System Audio Recording permission.

        Raises:
            RuntimeError: If the session has no live tap
            ValueError: If the live tap is exclusive or targets bundle IDs
            AudioProcessNotFoundError: If a named app cannot be found
            PermissionError: If Core Audio refuses the modification
        """
        tap_id = self._require_live_tap()
        description = self._backend.get_tap_description(tap_id)
        if description.bundle_ids:
            raise ValueError(
                "set_processes() cannot retarget a bundle-ID tap; create a "
                "new bundle-ID session instead"
            )
        if description.is_exclusive:
            raise ValueError(
                "set_processes() only supports inclusive taps; this tap's "
                "process list represents exclusions"
            )

        resolved = _resolve_processes(processes, self._backend)
        description.processes = [process.audio_object_id for process in resolved]
        self._backend.set_tap_description(tap_id, description)
        self.tap_description = description
        self.source_processes = tuple(resolved)
        self.source_process = resolved[0] if resolved else None

    def set_mute_behavior(self, mute: bool | TapMuteBehavior) -> None:
        """Change the live tap's mute behavior mid-capture.

        Raises:
            RuntimeError: If the session has no live tap
            PermissionError: If Core Audio refuses the modification
        """
        tap_id = self._require_live_tap()
        description = self._backend.get_tap_description(tap_id)
        description.mute_behavior = _normalize_mute_behavior(mute)
        self._backend.set_tap_description(tap_id, description)
        self.tap_description = description

    def _destroy_tap(self) -> OSError | None:
        """Destroy the active tap, if any, and return any cleanup error."""
        if self._tap_id is None:
            return None

        tap_id = self._tap_id

        if not self._owns_tap:
            self._tap_id = None
            return None

        try:
            self._backend.destroy_process_tap(tap_id)
        except OSError as exc:
            if getattr(exc, "status", None) != kAudioHardwareBadObjectError:
                return exc

        self._tap_id = None
        return None

    def __enter__(self) -> Self:
        """Start recording when entering a context manager."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool:
        """Always close the session when leaving a context manager.

        If the ``with`` block raised, we suppress close() errors so the
        original exception isn't masked.
        """
        try:
            self.close()
        except BaseException as cleanup_exc:
            if exc is None:
                raise
            _add_secondary_failure(
                exc,
                "Cleanup failure while exiting recording session",
                cleanup_exc,
            )
        return False


def _sanitize_track_filename(name: str) -> str:
    """Turn a process or device name into a safe WAV filename stem."""
    cleaned = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in name.strip()
    ).strip("_")
    return cleaned or "track"


def _output_path_alias_key(path: Path) -> str:
    """Return a conservative destination key for macOS output paths.

    ``resolve(strict=False)`` collapses relative components and existing
    symlinked parents. APFS commonly compares names case-insensitively and,
    like HFS+, treats canonically equivalent Unicode spellings as the same
    name, so normalize and case-fold as well. Rejecting a case-only pair on a
    case-sensitive volume is deliberately conservative: catap targets macOS
    and must never publish two tracks to one destination on the usual volume.
    """
    resolved = path.resolve(strict=False)
    return unicodedata.normalize("NFC", str(resolved).casefold())


def _normalize_track_output_paths(
    output_paths: Sequence[str | Path | None],
    *,
    on_track_buffer: Callable[[int, AudioBuffer], None] | None,
) -> list[Path | None]:
    """Normalize multitrack destinations and reject unsafe aliases."""
    normalized: list[Path | None] = []
    destinations: dict[str, tuple[int, Path]] = {}
    missing: list[int] = []

    for index, raw_path in enumerate(output_paths):
        if raw_path is None:
            normalized.append(None)
            missing.append(index)
            continue
        if isinstance(raw_path, str) and not raw_path:
            raise ValueError(f"output_paths[{index}] must not be empty")

        path = Path(raw_path)
        key = _output_path_alias_key(path)
        prior = destinations.get(key)
        if prior is not None:
            prior_index, prior_path = prior
            raise ValueError(
                f"output_paths[{prior_index}] ({prior_path}) and "
                f"output_paths[{index}] ({path}) refer to the same "
                "destination"
            )
        destinations[key] = (index, path)
        normalized.append(path)

    if missing and on_track_buffer is None:
        raise ValueError(
            "Every track needs an output path unless on_track_buffer is set; "
            f"track(s) {missing} have neither"
        )

    return normalized


def _derive_track_paths(
    output_dir: str | Path,
    labels: Sequence[str],
) -> list[Path]:
    """Build unique per-track WAV paths inside an output directory."""
    directory = Path(output_dir)
    paths: list[Path] = []
    used_stems: set[str] = set()
    for label in labels:
        stem = _sanitize_track_filename(label)
        # Suffix until unique so a deduped name can never collide with a
        # distinct label whose sanitized stem already carries that suffix
        # (for example "Music", "Music", "Music-2").
        candidate = stem
        counter = 2
        candidate_key = unicodedata.normalize("NFC", candidate.casefold())
        while candidate_key in used_stems:
            candidate = f"{stem}-{counter}"
            candidate_key = unicodedata.normalize("NFC", candidate.casefold())
            counter += 1
        used_stems.add(candidate_key)
        paths.append(directory / f"{candidate}.wav")
    return paths


class MultitrackRecordingSession:
    """Record several sources as sample-synchronized tracks.

    One tap is created per source and every tap is captured through a
    single aggregate device, so all tracks share one clock. Optionally a
    hardware input device (microphone) records alongside the taps as extra
    tracks. Track order is the aggregate's stream order: input-device
    tracks first, then taps.

    Any track's failure — a dropped buffer group, a vanished tap, a format
    change — discards every track's output rather than publishing a
    desynchronized session.
    """

    def __init__(
        self,
        tap_descriptions: Sequence[TapDescription],
        output_paths: Sequence[str | Path | None],
        *,
        track_labels: Sequence[str] | None = None,
        on_track_buffer: Callable[[int, AudioBuffer], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        input_device_uid: str | None = None,
        input_stream_count: int = 0,
        drift_compensation_quality: DriftCompensationQuality | None = None,
        _backend: _SessionBackend | None = None,
    ) -> None:
        """Create a multitrack session from prepared tap descriptions.

        ``output_paths`` covers every track in track order: the input
        device's streams first (when ``input_device_uid`` is set), then one
        entry per tap description.
        """
        if not tap_descriptions:
            raise ValueError("At least one tap description is required")

        expected_tracks = input_stream_count + len(tap_descriptions)
        if len(output_paths) != expected_tracks:
            raise ValueError(
                f"Expected {expected_tracks} output paths (input streams "
                f"first, then taps), got {len(output_paths)}"
            )
        if track_labels is not None and len(track_labels) != expected_tracks:
            raise ValueError(
                f"Expected {expected_tracks} track labels, got {len(track_labels)}"
            )

        self.tap_descriptions = list(tap_descriptions)
        self.output_paths = _normalize_track_output_paths(
            output_paths,
            on_track_buffer=on_track_buffer,
        )
        self.track_labels: tuple[str, ...] = (
            tuple(track_labels)
            if track_labels is not None
            else tuple(f"track {index}" for index in range(expected_tracks))
        )
        self._on_track_buffer = on_track_buffer
        self._max_pending_buffers = _validate_max_pending_buffers(max_pending_buffers)
        self._input_device_uid = input_device_uid
        self._input_stream_count = input_stream_count
        self.drift_compensation_quality = _validate_drift_compensation_quality(
            drift_compensation_quality
        )
        self._backend = _DEFAULT_SESSION_BACKEND if _backend is None else _backend

        self._tap_ids: list[int] = []
        self._recorder: _MultitrackRecorderLike | None = None
        self.source_processes: tuple[AudioProcess, ...] = ()
        self.input_device: AudioDevice | None = None

    @classmethod
    def from_processes(
        cls,
        processes: Sequence[str | AudioProcess],
        output_dir: str | Path | None = None,
        *,
        output_paths: Sequence[str | Path | None] | None = None,
        microphone: bool | str | AudioDevice = False,
        mute: bool | TapMuteBehavior = False,
        mono: bool = False,
        on_track_buffer: Callable[[int, AudioBuffer], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> MultitrackRecordingSession:
        """Create a session recording one track per application.

        Args:
            processes: Application names or AudioProcess objects; each
                becomes its own tap and track
            output_dir: Directory to write one WAV per track, named after
                each source (pass this or ``output_paths``)
            output_paths: Explicit per-track paths in track order
                (microphone streams first when enabled, then apps)
            microphone: Also record an input device as extra track(s):
                ``True`` for the default input device, or a device name,
                UID, or ``AudioDevice``. Requires microphone permission.
            mute: Mute behavior applied to every tapped app
            mono: Mix each app's track down to mono instead of stereo
            on_track_buffer: Optional callback receiving
                ``(track_index, AudioBuffer)`` per captured buffer
            max_pending_buffers: Per-track queue bound for the background
                workers
            drift_compensation_quality: Optional aggregate-device resampling
                quality applied to every tap. ``None`` preserves Core Audio's
                default.

        Raises:
            AudioProcessNotFoundError: If a named app cannot be found
            AudioDeviceNotFoundError: If the microphone device cannot be
                found
            ValueError: If no output target is provided
        """
        if not processes:
            raise ValueError("At least one process is required")

        backend = _DEFAULT_SESSION_BACKEND
        resolved = _resolve_processes(processes, backend)

        input_device: AudioDevice | None = None
        if microphone:
            if isinstance(microphone, AudioDevice):
                input_device = microphone
            elif microphone is True:
                input_device = backend.find_default_input_device()
                if input_device is None:
                    raise AudioDeviceNotFoundError(
                        "No default input device is available for the microphone track"
                    )
            else:
                input_device = backend.find_audio_device(microphone)
                if input_device is None:
                    raise AudioDeviceNotFoundError(
                        f"No audio device found matching '{microphone}'"
                    )
            if not input_device.input_streams:
                raise ValueError(
                    f"Audio device {input_device.name!r} has no input streams to record"
                )

        input_stream_count = len(input_device.input_streams) if input_device else 0
        labels = [
            (
                input_device.name  # type: ignore[union-attr]
                if input_stream_count == 1
                else f"{input_device.name} {index}"  # type: ignore[union-attr]
            )
            for index in range(input_stream_count)
        ] + [process.name for process in resolved]

        track_count = input_stream_count + len(resolved)
        output_directory: Path | None = None
        if output_paths is not None:
            resolved_paths: Sequence[str | Path | None] = output_paths
        elif output_dir is not None:
            output_directory = Path(output_dir)
            resolved_paths = _derive_track_paths(output_directory, labels)
        elif on_track_buffer is not None:
            resolved_paths = [None] * track_count
        else:
            raise ValueError("Provide output_dir, output_paths, or on_track_buffer")

        tap_descriptions = [
            backend.build_processes_tap_description(
                [process],
                mute=mute,
                mono=mono,
            )
            for process in resolved
        ]

        session = cls(
            tap_descriptions,
            resolved_paths,
            track_labels=labels,
            on_track_buffer=on_track_buffer,
            max_pending_buffers=max_pending_buffers,
            input_device_uid=input_device.uid if input_device else None,
            input_stream_count=input_stream_count,
            drift_compensation_quality=drift_compensation_quality,
            _backend=backend,
        )
        session.source_processes = tuple(resolved)
        session.input_device = input_device
        if output_directory is not None:
            output_directory.mkdir(parents=True, exist_ok=True)
        return session

    @property
    def tap_ids(self) -> list[int]:
        """Live Core Audio tap IDs, including any awaiting cleanup."""
        return list(self._tap_ids)

    @property
    def track_count(self) -> int:
        """Number of tracks this session records."""
        return self._input_stream_count + len(self.tap_descriptions)

    @property
    def is_recording(self) -> bool:
        """True while audio capture is active."""
        return self._recorder is not None and self._recorder.is_recording

    @property
    def needs_cleanup(self) -> bool:
        """True when a failed stop or close still owns retryable resources."""
        return not self.is_recording and (
            (self._recorder is not None and self._recorder.needs_cleanup)
            or bool(self._tap_ids)
        )

    @property
    def capture_failed(self) -> bool:
        """True once a live or background capture failure was detected."""
        return self._recorder is not None and self._recorder.capture_failed

    def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
        """Wait for failure without stopping or tearing down the session."""
        recorder = self._recorder
        if recorder is None:
            raise RuntimeError("Recording has not started")
        if not recorder.is_recording:
            raise RuntimeError("Not recording")
        return recorder.wait_for_capture_failure(timeout)

    @property
    def frames_recorded(self) -> int:
        """Frames recorded on the first track."""
        if self._recorder is None:
            return 0
        return self._recorder.frames_recorded

    @property
    def duration_seconds(self) -> float:
        """Recorded duration in seconds."""
        if self._recorder is None:
            return 0.0
        return self._recorder.duration_seconds

    @property
    def stream_formats(self) -> list[AudioStreamFormat]:
        """Per-track stream formats, once known."""
        if self._recorder is None:
            return []
        return self._recorder.stream_formats

    @property
    def captured_only_silence(self) -> bool:
        """True while every track holds only zeros."""
        if self._recorder is None:
            return True
        return self._recorder.captured_only_silence

    @property
    def track_captured_only_silence(self) -> tuple[bool, ...]:
        """Per-track silence flags, in track order."""
        if self._recorder is None:
            return tuple(True for _ in range(self.track_count))
        return self._recorder.track_captured_only_silence

    @property
    def max_pending_buffers(self) -> int:
        """Per-track queue bound before overflow."""
        return self._max_pending_buffers

    def start(self) -> None:
        """Create every tap and start the multitrack capture.

        Raises:
            OSError: If the taps or recorder cannot be started
            RuntimeError: If already recording or cleanup is pending
        """
        if self.is_recording:
            raise RuntimeError("Already recording")
        if self._tap_ids:
            raise RuntimeError(
                "Session has pending tap cleanup; call close() before restarting"
            )

        boxes: list[ctypes.c_uint32] = []
        try:
            for tap_description in self.tap_descriptions:
                box = ctypes.c_uint32(0)
                boxes.append(box)
                self._backend.create_process_tap(tap_description, out=box)
            self._tap_ids = [box.value for box in boxes]

            if self.drift_compensation_quality is None:
                recorder = self._backend.create_multitrack_recorder(
                    self._tap_ids,
                    self.output_paths,
                    on_track_buffer=self._on_track_buffer,
                    max_pending_buffers=self._max_pending_buffers,
                    input_device_uid=self._input_device_uid,
                    input_stream_count=self._input_stream_count,
                )
            else:
                recorder = self._backend.create_multitrack_recorder(
                    self._tap_ids,
                    self.output_paths,
                    on_track_buffer=self._on_track_buffer,
                    max_pending_buffers=self._max_pending_buffers,
                    input_device_uid=self._input_device_uid,
                    input_stream_count=self._input_stream_count,
                    drift_compensation_quality=self.drift_compensation_quality,
                )
            self._recorder = recorder
            recorder.start()
        except BaseException as exc:
            self._tap_ids = [box.value for box in boxes if box.value]
            if not _recorder_requires_cleanup(self._recorder):
                for destroy_error in self._destroy_taps():
                    _add_secondary_failure(
                        exc,
                        "Cleanup failure during multitrack session startup",
                        destroy_error,
                    )
                self._recorder = None
            raise

    def stop(self) -> None:
        """Stop recording and destroy every tap.

        Raises:
            RuntimeError: If not recording
            OSError: If stopping or cleanup fails
        """
        recorder = self._recorder
        recorder_requires_cleanup = _recorder_requires_cleanup(recorder)
        if not recorder_requires_cleanup and not self._tap_ids:
            raise RuntimeError("Not recording")
        self._shutdown()

    def close(self) -> None:
        """Close the session and release any active resources. Idempotent."""
        self._shutdown()

    def _shutdown(self) -> None:
        recorder = self._recorder
        errors: list[BaseException] = []

        if _recorder_requires_cleanup(recorder):
            assert recorder is not None
            try:
                recorder.stop()
            except BaseException as exc:
                errors.append(exc)
            if _recorder_requires_cleanup(recorder) and not errors:
                errors.append(RuntimeError("Recorder cleanup remained pending"))

        if not _recorder_requires_cleanup(recorder):
            errors.extend(self._destroy_taps())

        if errors:
            raise _combine_errors("Failed to stop multitrack recording session", errors)

    def _destroy_taps(self) -> list[BaseException]:
        """Destroy live taps, keeping any that fail for a retried close."""
        errors: list[BaseException] = []
        remaining = list(self._tap_ids)
        for tap_id in list(remaining):
            destroyed = False
            try:
                try:
                    self._backend.destroy_process_tap(tap_id)
                except OSError as exc:
                    if getattr(exc, "status", None) == kAudioHardwareBadObjectError:
                        destroyed = True
                    else:
                        errors.append(exc)
                else:
                    destroyed = True
            finally:
                if destroyed:
                    remaining.remove(tap_id)
                # Publish progress after every call. If an asynchronous
                # exception interrupts a later destroy, already-destroyed tap
                # IDs must not be retried as if they were still live.
                self._tap_ids = list(remaining)
        return errors

    def record_for(self, duration: float) -> MultitrackRecordingSession:
        """Record for a fixed amount of time.

        Raises:
            ValueError: If duration is not finite and positive
        """
        if not math.isfinite(duration):
            raise ValueError("duration must be finite")
        if duration <= 0:
            raise ValueError("duration must be greater than 0")

        self.start()
        try:
            self.wait_for_capture_failure(duration)
        except BaseException as exc:
            try:
                self.close()
            except BaseException as cleanup_exc:
                _add_secondary_failure(
                    exc,
                    "Cleanup failure while recording for a fixed duration",
                    cleanup_exc,
                )
            raise
        else:
            self.close()

        return self

    def __enter__(self) -> MultitrackRecordingSession:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool:
        try:
            self.close()
        except BaseException as cleanup_exc:
            if exc is None:
                raise
            _add_secondary_failure(
                exc,
                "Cleanup failure while exiting multitrack session",
                cleanup_exc,
            )
        return False


def record_process(
    process: str | AudioProcess,
    output_path: str | Path | None = None,
    *,
    mute: bool | TapMuteBehavior = False,
    mono: bool = False,
    visible: bool = False,
    on_buffer: Callable[[AudioBuffer], None] | None = None,
    max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    drift_compensation_quality: DriftCompensationQuality | None = None,
) -> RecordingSession:
    """
    Create a session for recording one application's audio.

    The session owns tap creation, recorder startup, shutdown, and tap cleanup.
    Pass ``max_pending_buffers`` to tune how much audio can be queued while the
    background worker catches up.
    """
    return RecordingSession.from_process(
        process,
        output_path=output_path,
        mute=mute,
        mono=mono,
        visible=visible,
        on_buffer=on_buffer,
        max_pending_buffers=max_pending_buffers,
        drift_compensation_quality=drift_compensation_quality,
    )


def record_processes(
    processes: Sequence[str | AudioProcess],
    output_path: str | Path | None = None,
    *,
    mute: bool | TapMuteBehavior = False,
    mono: bool = False,
    visible: bool = False,
    on_buffer: Callable[[AudioBuffer], None] | None = None,
    max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    drift_compensation_quality: DriftCompensationQuality | None = None,
) -> RecordingSession:
    """
    Create a session recording several applications mixed through one tap.

    See ``record_process`` for the option set.
    """
    return RecordingSession.from_processes(
        processes,
        output_path=output_path,
        mute=mute,
        mono=mono,
        visible=visible,
        on_buffer=on_buffer,
        max_pending_buffers=max_pending_buffers,
        drift_compensation_quality=drift_compensation_quality,
    )


def record_system_audio(
    output_path: str | Path | None = None,
    *,
    exclude: Sequence[str | AudioProcess] = (),
    mute: bool | TapMuteBehavior = False,
    mono: bool = False,
    visible: bool = False,
    on_buffer: Callable[[AudioBuffer], None] | None = None,
    max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    drift_compensation_quality: DriftCompensationQuality | None = None,
) -> RecordingSession:
    """
    Create a session for recording a global process-output mix.

    The session owns tap creation, recorder startup, shutdown, and tap cleanup.
    Pass ``max_pending_buffers`` to tune how much audio can be queued while the
    background worker catches up.
    """
    return RecordingSession.from_system_audio(
        output_path=output_path,
        exclude=exclude,
        mute=mute,
        mono=mono,
        visible=visible,
        on_buffer=on_buffer,
        max_pending_buffers=max_pending_buffers,
        drift_compensation_quality=drift_compensation_quality,
    )


def record_device(
    device: str | AudioDevice | AudioDeviceStream,
    output_path: str | Path | None = None,
    *,
    stream: int | None = None,
    include: Sequence[str | AudioProcess] = (),
    exclude: Sequence[str | AudioProcess] = (),
    mute: bool | TapMuteBehavior = False,
    visible: bool = False,
    on_buffer: Callable[[AudioBuffer], None] | None = None,
    max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    drift_compensation_quality: DriftCompensationQuality | None = None,
) -> RecordingSession:
    """
    Create a session recording audio routed to one output device stream.

    See ``RecordingSession.from_device`` for details.
    """
    return RecordingSession.from_device(
        device,
        output_path=output_path,
        stream=stream,
        include=include,
        exclude=exclude,
        mute=mute,
        visible=visible,
        on_buffer=on_buffer,
        max_pending_buffers=max_pending_buffers,
        drift_compensation_quality=drift_compensation_quality,
    )


def record_multitrack(
    processes: Sequence[str | AudioProcess],
    output_dir: str | Path | None = None,
    *,
    output_paths: Sequence[str | Path | None] | None = None,
    microphone: bool | str | AudioDevice = False,
    mute: bool | TapMuteBehavior = False,
    mono: bool = False,
    on_track_buffer: Callable[[int, AudioBuffer], None] | None = None,
    max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    drift_compensation_quality: DriftCompensationQuality | None = None,
) -> MultitrackRecordingSession:
    """
    Create a session recording each application as its own synchronized track.

    Optionally records an input device (microphone) alongside the apps.
    See ``MultitrackRecordingSession.from_processes`` for details.
    """
    return MultitrackRecordingSession.from_processes(
        processes,
        output_dir=output_dir,
        output_paths=output_paths,
        microphone=microphone,
        mute=mute,
        mono=mono,
        on_track_buffer=on_track_buffer,
        max_pending_buffers=max_pending_buffers,
        drift_compensation_quality=drift_compensation_quality,
    )


def record_bundle_ids(
    bundle_ids: Sequence[str],
    output_path: str | Path | None = None,
    *,
    restore: bool = True,
    mute: bool | TapMuteBehavior = False,
    mono: bool = False,
    visible: bool = False,
    on_buffer: Callable[[AudioBuffer], None] | None = None,
    max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    drift_compensation_quality: DriftCompensationQuality | None = None,
) -> RecordingSession:
    """
    Create a session recording applications by bundle ID (macOS 26+).

    See ``RecordingSession.from_bundle_ids`` for details.
    """
    return RecordingSession.from_bundle_ids(
        bundle_ids,
        output_path=output_path,
        restore=restore,
        mute=mute,
        mono=mono,
        visible=visible,
        on_buffer=on_buffer,
        max_pending_buffers=max_pending_buffers,
        drift_compensation_quality=drift_compensation_quality,
    )


def record_tap(
    tap: int | AudioTap,
    output_path: str | Path | None = None,
    *,
    on_buffer: Callable[[AudioBuffer], None] | None = None,
    max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    drift_compensation_quality: DriftCompensationQuality | None = None,
) -> RecordingSession:
    """
    Create a session for recording from an existing visible tap.

    The tap was created elsewhere, so the session will not destroy it when
    recording stops. If the tap's owner destroys it mid-capture, ``stop()``
    fails and discards the output instead of publishing a truncated file.
    """
    return RecordingSession.from_tap(
        tap,
        output_path=output_path,
        on_buffer=on_buffer,
        max_pending_buffers=max_pending_buffers,
        drift_compensation_quality=drift_compensation_quality,
    )


__all__ = [
    "AmbiguousAudioProcessError",
    "AudioDeviceNotFoundError",
    "AudioProcessNotFoundError",
    "MultitrackRecordingSession",
    "RecordingSession",
    "record_bundle_ids",
    "record_device",
    "record_multitrack",
    "record_process",
    "record_processes",
    "record_system_audio",
    "record_tap",
]
