"""High-level recording session API."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Self

from catap.bindings.hardware import create_process_tap, destroy_process_tap
from catap.bindings.process import AudioProcess, find_process_by_name
from catap.bindings.tap_description import TapDescription, TapMuteBehavior
from catap.core.recorder import AudioRecorder


class AudioProcessNotFoundError(LookupError):
    """Raised when a named audio process cannot be found."""


def _resolve_process(process: str | AudioProcess) -> AudioProcess:
    """Resolve a process name into an AudioProcess."""
    if isinstance(process, AudioProcess):
        return process

    resolved = find_process_by_name(process)
    if resolved is None:
        raise AudioProcessNotFoundError(f"No audio process found matching '{process}'")

    return resolved


def _resolve_processes(processes: Sequence[str | AudioProcess]) -> list[AudioProcess]:
    """Resolve a list of process specifiers into AudioProcess objects."""
    return [_resolve_process(process) for process in processes]


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
    ) -> None:
        """
        Create a managed recording session.

        Args:
            tap_description: Tap description to create when recording starts
            output_path: Path to write a WAV file, or None for streaming mode
            on_data: Optional callback for each audio buffer (bytes, num_frames).
                The callback runs on a background worker thread.
        """
        self.tap_description = tap_description
        self.output_path = Path(output_path) if output_path else None
        self._on_data = on_data

        self.source_process: AudioProcess | None = None
        self.excluded_processes: tuple[AudioProcess, ...] = ()

        self._tap_id: int | None = None
        self._recorder: AudioRecorder | None = None

    @classmethod
    def from_process(
        cls,
        process: str | AudioProcess,
        output_path: str | Path | None = None,
        *,
        mute: bool = False,
        on_data: Callable[[bytes, int], None] | None = None,
    ) -> RecordingSession:
        """
        Create a managed session for recording one application's audio.

        Args:
            process: Application name or AudioProcess to record
            output_path: Path to write a WAV file, or None for streaming mode
            mute: Mute app playback while still capturing audio
            on_data: Optional callback for each audio buffer (bytes, num_frames).
                The callback runs on a background worker thread.

        Raises:
            AudioProcessNotFoundError: If the named app cannot be found
        """
        resolved_process = _resolve_process(process)

        tap_description = TapDescription.stereo_mixdown_of_processes(
            [resolved_process.audio_object_id]
        )
        tap_description.name = f"catap recording {resolved_process.name}"
        tap_description.is_private = True
        tap_description.mute_behavior = (
            TapMuteBehavior.MUTED if mute else TapMuteBehavior.UNMUTED
        )

        session = cls(tap_description, output_path=output_path, on_data=on_data)
        session.source_process = resolved_process
        return session

    @classmethod
    def from_system_audio(
        cls,
        output_path: str | Path | None = None,
        *,
        exclude: Sequence[str | AudioProcess] = (),
        on_data: Callable[[bytes, int], None] | None = None,
    ) -> RecordingSession:
        """
        Create a managed session for recording system audio.

        Args:
            output_path: Path to write a WAV file, or None for streaming mode
            exclude: Apps to exclude from the system capture
            on_data: Optional callback for each audio buffer (bytes, num_frames).
                The callback runs on a background worker thread.

        Raises:
            AudioProcessNotFoundError: If an excluded app name cannot be found
        """
        excluded_processes = _resolve_processes(exclude)
        excluded_ids = [process.audio_object_id for process in excluded_processes]

        tap_description = TapDescription.stereo_global_tap_excluding(excluded_ids)
        tap_description.name = "catap system recording"
        tap_description.is_private = True
        tap_description.mute_behavior = TapMuteBehavior.UNMUTED

        session = cls(tap_description, output_path=output_path, on_data=on_data)
        session.excluded_processes = tuple(excluded_processes)
        return session

    @property
    def tap_id(self) -> int | None:
        """Current Core Audio tap ID, if the session is active."""
        return self._tap_id

    @property
    def recorder(self) -> AudioRecorder | None:
        """Current or most recent low-level recorder."""
        return self._recorder

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

        tap_id = create_process_tap(self.tap_description)

        try:
            recorder = AudioRecorder(tap_id, self.output_path, on_data=self._on_data)
            self._tap_id = tap_id
            self._recorder = recorder
            recorder.start()
        except Exception:
            try:
                destroy_process_tap(tap_id)
            except OSError:
                pass
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

        if stop_error is not None:
            raise stop_error
        if destroy_error is not None:
            raise destroy_error

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

        if stop_error is not None:
            raise stop_error
        if destroy_error is not None:
            raise destroy_error

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

        try:
            destroy_process_tap(tap_id)
        except OSError as exc:
            return exc

        return None

    def __enter__(self) -> RecordingSession:
        """Start recording when entering a context manager."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool:
        """Always close the session when leaving a context manager."""
        if exc_type is None:
            self.close()
        else:
            try:
                self.close()
            except Exception:
                pass

        return False


def record_process(
    process: str | AudioProcess,
    output_path: str | Path | None = None,
    *,
    mute: bool = False,
    on_data: Callable[[bytes, int], None] | None = None,
) -> RecordingSession:
    """
    Create a managed session for recording one application's audio.

    This is the quickest way to capture a single app without manually creating
    a tap or cleaning it up afterward.
    """
    return RecordingSession.from_process(
        process,
        output_path=output_path,
        mute=mute,
        on_data=on_data,
    )


def record_system_audio(
    output_path: str | Path | None = None,
    *,
    exclude: Sequence[str | AudioProcess] = (),
    on_data: Callable[[bytes, int], None] | None = None,
) -> RecordingSession:
    """
    Create a managed session for recording system audio.

    This is the quickest way to capture the system mix without manually
    building a global tap description.
    """
    return RecordingSession.from_system_audio(
        output_path=output_path,
        exclude=exclude,
        on_data=on_data,
    )


__all__ = [
    "AudioProcessNotFoundError",
    "RecordingSession",
    "record_process",
    "record_system_audio",
]
