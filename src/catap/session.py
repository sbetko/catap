"""High-level recording session API."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Self

from catap._recording_support import (
    _DEFAULT_MAX_PENDING_BUFFERS,
    _combine_errors,
    _validate_max_pending_buffers,
    _validate_recording_target,
)
from catap._session_backend import (
    _DEFAULT_SESSION_BACKEND,
    _RecorderLike,
    _SessionBackend,
)
from catap.bindings.process import (
    AmbiguousAudioProcessError,
    AudioProcess,
)
from catap.bindings.tap import AudioTap
from catap.bindings.tap_description import TapDescription


class AudioProcessNotFoundError(LookupError):
    """Raised when a named audio process cannot be found."""


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


def build_process_tap_description(
    process: AudioProcess, *, mute: bool = False
) -> TapDescription:
    """Build the private stereo-mixdown tap description catap uses for app capture."""
    return _DEFAULT_SESSION_BACKEND.build_process_tap_description(
        process,
        mute=mute,
    )


def build_system_tap_description(
    excluded: Sequence[AudioProcess] = (),
) -> TapDescription:
    """Build the private stereo global tap catap uses for system capture."""
    return _DEFAULT_SESSION_BACKEND.build_system_tap_description(excluded)


class RecordingSession:
    """
    Managed recording session that owns tap and recorder lifecycle.

    This is the higher-level API for common capture flows. It wraps the
    lower-level tap creation and AudioRecorder startup/shutdown steps so users
    can focus on what to record rather than which Core Audio objects need to be
    cleaned up.
    """

    def __init__(
        self,
        tap_description: TapDescription,
        output_path: str | Path | None = None,
        on_data: Callable[[bytes, int], None] | None = None,
        *,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        _backend: _SessionBackend | None = None,
    ) -> None:
        """
        Create a managed recording session.

        Args:
            tap_description: Tap description to create when recording starts
            output_path: Path to write a WAV file, or None for streaming mode
            on_data: Optional callback invoked with ``(raw_bytes, num_frames)``
                for each captured buffer. The bytes are the tap's native
                format (typically 32-bit float, little-endian, interleaved);
                inspect the session's ``sample_rate``, ``num_channels``, and
                ``is_float`` to interpret them. Runs on catap's background
                worker thread, not on Core Audio's real-time callback thread.
            max_pending_buffers: Maximum number of audio buffers to queue for
                the background worker before new buffers are dropped and the
                capture fails on stop. Higher values trade memory for tolerance
                of slow disk writes or ``on_data`` callbacks.
        Raises:
            ValueError: If neither ``output_path`` nor ``on_data`` is provided
        """
        self.tap_description = tap_description
        self.output_path = _validate_recording_target(output_path, on_data)
        self._on_data = on_data
        self._max_pending_buffers = _validate_max_pending_buffers(max_pending_buffers)
        self._backend = (
            _DEFAULT_SESSION_BACKEND if _backend is None else _backend
        )

        self.source_process: AudioProcess | None = None
        self.source_tap: AudioTap | None = None
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
        mute: bool = False,
        on_data: Callable[[bytes, int], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    ) -> Self:
        """
        Create a managed session for recording one application's audio.

        Args:
            process: Application name or AudioProcess to record
            output_path: Path to write a WAV file, or None for streaming mode
            mute: Mute app playback while still capturing audio
            on_data: Optional streaming callback. See ``RecordingSession`` for
                buffer format and threading details.
            max_pending_buffers: Queue bound for the background worker. See
                ``RecordingSession`` for details.

        Raises:
            AudioProcessNotFoundError: If the named app cannot be found
        """
        backend = _DEFAULT_SESSION_BACKEND
        resolved_process = _resolve_process(process, backend)
        tap_description = backend.build_process_tap_description(
            resolved_process,
            mute=mute,
        )

        session = cls(
            tap_description,
            output_path=output_path,
            on_data=on_data,
            max_pending_buffers=max_pending_buffers,
            _backend=backend,
        )
        session.source_process = resolved_process
        return session

    @classmethod
    def from_system_audio(
        cls,
        output_path: str | Path | None = None,
        *,
        exclude: Sequence[str | AudioProcess] = (),
        on_data: Callable[[bytes, int], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    ) -> Self:
        """
        Create a managed session for recording system audio.

        Args:
            output_path: Path to write a WAV file, or None for streaming mode
            exclude: Apps to exclude from the system capture
            on_data: Optional streaming callback. See ``RecordingSession`` for
                buffer format and threading details.
            max_pending_buffers: Queue bound for the background worker. See
                ``RecordingSession`` for details.

        Raises:
            AudioProcessNotFoundError: If an excluded app name cannot be found
        """
        backend = _DEFAULT_SESSION_BACKEND
        excluded_processes = _resolve_processes(exclude, backend)
        tap_description = backend.build_system_tap_description(excluded_processes)

        session = cls(
            tap_description,
            output_path=output_path,
            on_data=on_data,
            max_pending_buffers=max_pending_buffers,
            _backend=backend,
        )
        session.excluded_processes = tuple(excluded_processes)
        return session

    @classmethod
    def from_tap(
        cls,
        tap: int | AudioTap,
        output_path: str | Path | None = None,
        *,
        on_data: Callable[[bytes, int], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    ) -> Self:
        """Create a managed session that records from an existing tap."""
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
            on_data=on_data,
            max_pending_buffers=max_pending_buffers,
            _backend=backend,
        )
        session._existing_tap_id = tap_id
        session._owns_tap = False
        session.source_tap = source_tap
        return session

    @property
    def tap_id(self) -> int | None:
        """Current Core Audio tap ID, if the session is active."""
        return self._tap_id

    @property
    def is_recording(self) -> bool:
        """True while audio capture is active."""
        return self._recorder is not None and self._recorder.is_recording

    @property
    def frames_recorded(self) -> int:
        """Number of frames recorded in the current or most recent capture."""
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
    def sample_rate(self) -> float | None:
        """Sample rate in Hz, once known."""
        if self._recorder is None:
            return None
        return self._recorder.sample_rate

    @property
    def max_pending_buffers(self) -> int:
        """Maximum number of queued audio buffers before overflow."""
        return self._max_pending_buffers

    @property
    def num_channels(self) -> int | None:
        """Number of channels, once known."""
        if self._recorder is None:
            return None
        return self._recorder.num_channels

    @property
    def is_float(self) -> bool | None:
        """True if the audio format is float32, once known."""
        if self._recorder is None:
            return None
        return self._recorder.is_float

    def start(self) -> None:
        """
        Start recording.

        Raises:
            OSError: If the tap or recorder cannot be started
            RuntimeError: If already recording
        """
        if self.is_recording:
            raise RuntimeError("Already recording")

        tap_id = (
            self._existing_tap_id
            if self._existing_tap_id is not None
            else self._backend.create_process_tap(self.tap_description)
        )

        try:
            recorder = self._backend.create_recorder(
                tap_id,
                self.output_path,
                on_data=self._on_data,
                max_pending_buffers=self._max_pending_buffers,
            )
            self._tap_id = tap_id
            self._recorder = recorder
            recorder.start()
        except Exception:
            if self._owns_tap:
                with contextlib.suppress(OSError):
                    self._backend.destroy_process_tap(tap_id)
            self._tap_id = None
            self._recorder = None
            raise

    def stop(self) -> None:
        """
        Stop recording and destroy the tap.

        Raises:
            RuntimeError: If not recording
            OSError: If stopping or cleanup fails
        """
        if not self.is_recording or self._recorder is None:
            raise RuntimeError("Not recording")

        stop_error: OSError | RuntimeError | None = None
        try:
            self._recorder.stop()
        except (OSError, RuntimeError) as exc:
            stop_error = exc

        destroy_error = self._destroy_tap()

        errors = [
            error for error in (stop_error, destroy_error) if error is not None
        ]
        if errors:
            raise _combine_errors("Failed to stop recording session", errors)

    def close(self) -> None:
        """
        Close the session and release any active resources.

        This method is idempotent.
        """
        stop_error: OSError | RuntimeError | None = None
        if self.is_recording and self._recorder is not None:
            try:
                self._recorder.stop()
            except (OSError, RuntimeError) as exc:
                stop_error = exc

        destroy_error = self._destroy_tap()

        errors = [
            error for error in (stop_error, destroy_error) if error is not None
        ]
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
            ValueError: If duration is not positive
        """
        if duration <= 0:
            raise ValueError("duration must be greater than 0")

        self.start()
        try:
            time.sleep(duration)
        finally:
            self.close()

        return self

    def _destroy_tap(self) -> OSError | None:
        """Destroy the active tap, if any, and return any cleanup error."""
        if self._tap_id is None:
            return None

        tap_id = self._tap_id
        self._tap_id = None

        if not self._owns_tap:
            return None

        try:
            self._backend.destroy_process_tap(tap_id)
        except OSError as exc:
            return exc

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
        except Exception:
            if exc_type is None:
                raise
        return False


def record_process(
    process: str | AudioProcess,
    output_path: str | Path | None = None,
    *,
    mute: bool = False,
    on_data: Callable[[bytes, int], None] | None = None,
    max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
) -> RecordingSession:
    """
    Create a managed session for recording one application's audio.

    This is the quickest way to capture a single app without manually creating
    a tap or cleaning it up afterward. Pass ``max_pending_buffers`` to tune how
    much audio can be queued while the background worker catches up.
    """
    return RecordingSession.from_process(
        process,
        output_path=output_path,
        mute=mute,
        on_data=on_data,
        max_pending_buffers=max_pending_buffers,
    )


def record_system_audio(
    output_path: str | Path | None = None,
    *,
    exclude: Sequence[str | AudioProcess] = (),
    on_data: Callable[[bytes, int], None] | None = None,
    max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
) -> RecordingSession:
    """
    Create a managed session for recording system audio.

    This is the quickest way to capture the system mix without manually
    building a global tap description. Pass ``max_pending_buffers`` to tune how
    much audio can be queued while the background worker catches up.
    """
    return RecordingSession.from_system_audio(
        output_path=output_path,
        exclude=exclude,
        on_data=on_data,
        max_pending_buffers=max_pending_buffers,
    )


def record_tap(
    tap: int | AudioTap,
    output_path: str | Path | None = None,
    *,
    on_data: Callable[[bytes, int], None] | None = None,
    max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
) -> RecordingSession:
    """
    Create a managed session for recording from an existing visible tap.

    The tap itself is treated as externally owned and will not be destroyed
    when the session stops or closes.
    """
    return RecordingSession.from_tap(
        tap,
        output_path=output_path,
        on_data=on_data,
        max_pending_buffers=max_pending_buffers,
    )


__all__ = [
    "AmbiguousAudioProcessError",
    "AudioProcessNotFoundError",
    "RecordingSession",
    "record_process",
    "record_system_audio",
    "record_tap",
]
