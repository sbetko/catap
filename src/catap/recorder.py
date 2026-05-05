"""Audio recording from Core Audio taps."""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from pathlib import Path

from catap._capture_engine import (
    _TapCaptureEngine,
    _TapCaptureSession,
    _TapStreamFormat,
)
from catap._native_coreaudio import (
    NativeCoreAudioRecorder,
    NativeCoreAudioRecorderStats,
)
from catap._recording_support import (
    _DEFAULT_MAX_PENDING_BUFFERS,
    _add_secondary_failure,
    _combine_errors,
    _translate_exception,
    _validate_max_pending_buffers,
    _validate_recording_target,
)
from catap._recording_worker import (
    _AudioWorker,
    _WorkerConfig,
)
from catap.audio_buffer import (
    AudioBuffer,
    AudioStreamFormat,
    _format_id_to_fourcc,
)
from catap.bindings._audiotoolbox import kAudioFormatLinearPCM

_NATIVE_DRAIN_IDLE_INTERVAL_SECONDS = 0.001
_NATIVE_SLOT_FRAME_CAPACITY = 16_384


class UnsupportedTapFormatError(ValueError):
    """Raised when a tap exposes an audio layout catap cannot safely record."""


class AudioRecorder:
    """Record audio from a Core Audio tap.

    This recorder reads the tap through a private aggregate device and can
    write WAV output, call an ``on_buffer`` callback, or do both.

    Usage:
        import time

        from catap import TapDescription, create_process_tap, destroy_process_tap

        tap_desc = TapDescription.stereo_mixdown_of_processes([process_id])
        tap_id = create_process_tap(tap_desc)
        try:
            recorder = AudioRecorder(tap_id, "output.wav")
            recorder.start()
            try:
                time.sleep(5)
            finally:
                recorder.stop()
        finally:
            destroy_process_tap(tap_id)

    """

    def __init__(
        self,
        tap_id: int,
        output_path: str | Path | None = None,
        on_buffer: Callable[[AudioBuffer], None] | None = None,
        *,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    ) -> None:
        """Initialize the recorder.

        Args:
            tap_id: AudioObjectID of the tap to record from
            output_path: Path to write the WAV file, or None for streaming mode
            on_buffer: Optional callback invoked with an ``AudioBuffer`` for
                each captured buffer. The bytes are safe to retain.
                The callback runs on catap's background worker thread, so
                Core Audio's real-time callback stays lightweight.
            max_pending_buffers: Maximum number of audio buffers to queue for
                the background worker before new buffers are dropped and the
                capture fails on stop. Higher values trade memory for tolerance
                of slow disk writes or ``on_buffer`` callbacks.
        Raises:
            ValueError: If neither ``output_path`` nor ``on_buffer`` is provided
        """
        self.tap_id = tap_id
        self.output_path = _validate_recording_target(output_path, on_buffer)
        self._on_buffer = on_buffer

        self._capture_engine = _TapCaptureEngine()
        self._capture_session: _TapCaptureSession | None = None
        self._is_recording = False
        self._max_pending_buffers = _validate_max_pending_buffers(max_pending_buffers)
        self._worker = _AudioWorker(
            record_accepted_frames=self._record_accepted_frames,
            record_dropped_frames=self._record_dropped_frames,
            consume_dropped_stats=self._consume_dropped_stats,
        )
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_state = "idle"
        self._stats_lock = threading.Lock()
        self._native_recorder: NativeCoreAudioRecorder | None = None
        self._native_drain_thread: threading.Thread | None = None
        self._native_drain_stop_event: threading.Event | None = None
        self._native_drain_failures: list[RuntimeError] = []

        self._total_frames = 0
        self._dropped_buffers = 0
        self._dropped_frames = 0

        # Stream format (populated on start).
        self._sample_rate = 44100.0
        self._num_channels = 2
        self._bits_per_sample = 32
        self._bytes_per_frame = 8
        self._output_bits_per_sample = 32
        self._convert_float_output = True
        self._is_float = True
        self._stream_format: AudioStreamFormat | None = None

    def _apply_stream_format(self, stream_format: _TapStreamFormat) -> None:
        """Apply tap stream metadata to recorder state."""
        self._validate_stream_format(stream_format)
        self._sample_rate = stream_format.sample_rate
        self._num_channels = stream_format.num_channels
        self._bits_per_sample = stream_format.bits_per_sample
        self._bytes_per_frame = (
            stream_format.bytes_per_frame
            if stream_format.bytes_per_frame is not None
            else self._packed_bytes_per_frame(
                stream_format.num_channels,
                stream_format.bits_per_sample,
            )
        )
        self._is_float = stream_format.is_float
        self._stream_format = AudioStreamFormat(
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
            bits_per_sample=self._bits_per_sample,
            sample_type="float" if self._is_float else "signed_integer",
            format_id=_format_id_to_fourcc(stream_format.format_id),
        )

    @staticmethod
    def _packed_bytes_per_frame(num_channels: int, bits_per_sample: int) -> int:
        return num_channels * (bits_per_sample // 8)

    def _validate_stream_format(self, stream_format: _TapStreamFormat) -> None:
        """Reject tap formats that would otherwise produce corrupt output."""
        if stream_format.format_id != kAudioFormatLinearPCM:
            raise UnsupportedTapFormatError(
                "Unsupported tap format: only linear PCM streams are currently "
                f"supported, got format id {stream_format.format_id}"
            )
        if stream_format.sample_rate <= 0:
            raise UnsupportedTapFormatError(
                f"Unsupported tap sample rate: {stream_format.sample_rate!r}"
            )
        if stream_format.num_channels <= 0:
            raise UnsupportedTapFormatError(
                f"Unsupported tap channel count: {stream_format.num_channels}"
            )
        if stream_format.bits_per_sample <= 0 or stream_format.bits_per_sample % 8:
            raise UnsupportedTapFormatError(
                f"Unsupported tap bit depth: {stream_format.bits_per_sample}"
            )
        if stream_format.is_big_endian:
            raise UnsupportedTapFormatError(
                "Unsupported tap byte order: big-endian PCM is not currently supported"
            )
        if not stream_format.is_packed:
            raise UnsupportedTapFormatError(
                "Unsupported tap format: non-packed PCM is not currently supported"
            )
        if stream_format.is_float and stream_format.bits_per_sample != 32:
            raise UnsupportedTapFormatError(
                "Unsupported floating-point tap format: only packed float32 is "
                "currently supported"
            )
        if not stream_format.is_float and not stream_format.is_signed_integer:
            raise UnsupportedTapFormatError(
                "Unsupported integer tap format: only signed integer PCM is "
                "currently supported"
            )
        if not stream_format.is_interleaved:
            raise UnsupportedTapFormatError(
                "Unsupported tap layout: non-interleaved audio buffers are not "
                "currently supported"
            )

        bytes_per_frame = (
            stream_format.bytes_per_frame
            if stream_format.bytes_per_frame is not None
            else self._packed_bytes_per_frame(
                stream_format.num_channels,
                stream_format.bits_per_sample,
            )
        )
        expected_bytes_per_frame = self._packed_bytes_per_frame(
            stream_format.num_channels,
            stream_format.bits_per_sample,
        )
        if bytes_per_frame != expected_bytes_per_frame:
            raise UnsupportedTapFormatError(
                "Unsupported tap format: expected packed interleaved "
                f"{expected_bytes_per_frame}-byte frames, got {bytes_per_frame}"
            )

    @property
    def _aggregate_device_id(self) -> int | None:
        session = self._capture_session
        return None if session is None else session.aggregate_device_id

    @property
    def _io_proc_id(self) -> ctypes.c_void_p | None:
        session = self._capture_session
        return None if session is None else session.io_proc_id

    def _make_worker_config(self) -> _WorkerConfig:
        """Build worker configuration from the current stream format."""
        stream_format = self._stream_format
        if stream_format is None:
            raise RuntimeError("Stream format is not known until recording starts")

        return _WorkerConfig(
            output_path=self.output_path,
            on_buffer=self._on_buffer,
            max_pending_buffers=self._max_pending_buffers,
            stream_format=stream_format,
            output_bits_per_sample=self._output_bits_per_sample,
            convert_float_output=self._convert_float_output,
        )

    def _reset_counters(self) -> None:
        with self._stats_lock:
            self._total_frames = 0
            self._dropped_buffers = 0
            self._dropped_frames = 0

    def _record_accepted_frames(self, num_frames: int) -> None:
        self._total_frames += num_frames

    def _record_dropped_frames(self, num_frames: int) -> None:
        with self._stats_lock:
            self._dropped_buffers += 1
            self._dropped_frames += num_frames

    def _consume_dropped_stats(self) -> tuple[int, int]:
        with self._stats_lock:
            dropped_buffers = self._dropped_buffers
            dropped_frames = self._dropped_frames
            self._dropped_buffers = 0
            self._dropped_frames = 0
        return dropped_buffers, dropped_frames

    def _create_native_recorder(self) -> NativeCoreAudioRecorder:
        """Create the native IOProc recorder."""
        return NativeCoreAudioRecorder(
            slot_count=self._max_pending_buffers,
            slot_capacity=self._native_slot_capacity(),
            expected_channel_count=self._num_channels,
            bytes_per_frame=self._bytes_per_frame,
        )

    def _native_slot_capacity(self) -> int:
        return self._bytes_per_frame * _NATIVE_SLOT_FRAME_CAPACITY

    def _start_native_drain(self, native_recorder: NativeCoreAudioRecorder) -> None:
        if self._native_drain_thread is not None:
            raise RuntimeError("Native recorder drain already started")

        stop_event = threading.Event()
        self._native_drain_stop_event = stop_event
        self._native_drain_failures = []
        thread = threading.Thread(
            target=self._native_drain_loop,
            args=(native_recorder, stop_event),
            name="catap-native-audio-drain",
            daemon=False,
        )
        thread.start()
        self._native_drain_thread = thread

    def _stop_native_drain(self) -> None:
        stop_event = self._native_drain_stop_event
        if stop_event is not None:
            stop_event.set()

        thread = self._native_drain_thread
        if thread is not None:
            thread.join()

        self._native_drain_thread = None
        self._native_drain_stop_event = None

        failures = self._native_drain_failures
        self._native_drain_failures = []
        if failures:
            raise _combine_errors("Failed to drain native recorder", failures)

    def _native_drain_loop(
        self,
        native_recorder: NativeCoreAudioRecorder,
        stop_event: threading.Event,
    ) -> None:
        try:
            while True:
                drained = self._drain_native_recorder(native_recorder)
                if stop_event.is_set():
                    return
                if not drained:
                    stop_event.wait(_NATIVE_DRAIN_IDLE_INTERVAL_SECONDS)
        except Exception as exc:
            failure = _translate_exception(
                RuntimeError,
                f"Native recorder drain failed: {exc}",
                exc,
            )
            assert isinstance(failure, RuntimeError)
            self._native_drain_failures.append(failure)

    def _drain_native_recorder(
        self,
        native_recorder: NativeCoreAudioRecorder,
    ) -> bool:
        drained = False
        while True:
            chunk = native_recorder.read()
            if chunk is None:
                return drained
            drained = True
            self._worker.enqueue_audio_bytes(
                chunk.data,
                chunk.frame_count,
                chunk.input_sample_time,
            )

    def _native_recorder_errors(
        self,
        stats: NativeCoreAudioRecorderStats,
    ) -> list[RuntimeError]:
        errors: list[RuntimeError] = []
        if stats.callback_failures:
            errors.append(
                RuntimeError(
                    "Native CoreAudio callback rejected "
                    f"{stats.callback_failures} audio buffer(s); last error "
                    f"{stats.last_error_name} ({stats.last_error_status})."
                )
            )

        if stats.ring.dropped_chunks:
            message = (
                "Dropped "
                f"{stats.ring.dropped_chunks} native audio buffer(s) "
                f"({stats.ring.dropped_frames} frame(s)) before they reached "
                "the background worker."
            )
            if stats.ring.oversized_chunks:
                message += (
                    " "
                    f"{stats.ring.oversized_chunks} buffer(s) exceeded the "
                    "native ring slot capacity."
                )
            errors.append(RuntimeError(message))

        return errors

    def start(self) -> None:
        """Start recording audio.

        Raises:
            OSError: If recording cannot be started
            RuntimeError: If already recording
        """
        with self._lifecycle_lock:
            if self._lifecycle_state == "recording":
                raise RuntimeError("Already recording")
            if self._lifecycle_state != "idle":
                raise RuntimeError("Recorder is already starting or stopping")
            self._lifecycle_state = "starting"

        try:
            stream_format = self._capture_engine.describe_tap_stream(self.tap_id)
            self._apply_stream_format(stream_format)

            self._output_bits_per_sample = (
                16
                if self._is_float and self._bits_per_sample == 32
                else self._bits_per_sample
            )
            self._convert_float_output = self._is_float and self._bits_per_sample == 32

            self._reset_counters()

            cleanup: list[Callable[[], None]] = []
            capture_session: _TapCaptureSession | None = None
            native_recorder: NativeCoreAudioRecorder | None = None
            try:
                native_recorder = self._create_native_recorder()
                self._native_recorder = native_recorder
                cleanup.append(native_recorder.close)
                capture_session = self._capture_engine.open_tap_capture(
                    self.tap_id,
                    native_recorder.io_proc_pointer,
                    native_recorder.handle,
                )
                self._capture_session = capture_session
                cleanup.append(lambda: self._capture_engine.close(capture_session))

                self._worker.start(self._make_worker_config())
                cleanup.append(lambda: self._worker.stop(publish=False))
                self._start_native_drain(native_recorder)
                cleanup.append(self._stop_native_drain)

                with self._lifecycle_lock:
                    self._is_recording = True

                self._capture_engine.start(capture_session)
                cleanup.append(lambda: self._capture_engine.stop(capture_session))
            except Exception as exc:
                with self._lifecycle_lock:
                    self._is_recording = False
                for step in reversed(cleanup):
                    try:
                        step()
                    except Exception as cleanup_exc:
                        _add_secondary_failure(
                            exc,
                            "Cleanup failure during recorder startup",
                            cleanup_exc,
                        )
                self._capture_session = None
                self._native_recorder = None
                raise
        except Exception:
            with self._lifecycle_lock:
                self._lifecycle_state = "idle"
            raise
        else:
            with self._lifecycle_lock:
                self._lifecycle_state = "recording"

    def stop(self) -> None:
        """Stop recording and finalize any WAV output.

        Raises:
            OSError: If Core Audio cleanup fails
            RuntimeError: If not recording
        """
        with self._lifecycle_lock:
            if self._lifecycle_state == "idle":
                raise RuntimeError("Not recording")
            if self._lifecycle_state != "recording":
                raise RuntimeError("Recorder is already starting or stopping")

            self._lifecycle_state = "stopping"
            self._is_recording = False
            capture_session = self._capture_session
            native_recorder = self._native_recorder

        cleanup_errors: list[OSError | RuntimeError] = []
        publish_worker_output = True

        if capture_session is not None:
            try:
                self._capture_engine.stop(capture_session)
            except OSError as exc:
                cleanup_errors.append(exc)

        if native_recorder is not None:
            try:
                self._stop_native_drain()
            except RuntimeError as exc:
                cleanup_errors.append(exc)
                publish_worker_output = False

            try:
                native_errors = self._native_recorder_errors(native_recorder.stats())
            except RuntimeError as exc:
                cleanup_errors.append(exc)
                publish_worker_output = False
            else:
                if native_errors:
                    cleanup_errors.extend(native_errors)
                    publish_worker_output = False

        if capture_session is not None:
            try:
                self._capture_engine.close(capture_session)
            except OSError as exc:
                cleanup_errors.append(exc)

        try:
            self._worker.stop(publish=publish_worker_output)
        except (OSError, RuntimeError) as exc:
            cleanup_errors.append(exc)

        self._capture_session = None
        if native_recorder is not None:
            native_recorder.close()
            self._native_recorder = None

        with self._lifecycle_lock:
            self._lifecycle_state = "idle"

        if cleanup_errors:
            raise _combine_errors("Failed to stop recording cleanly", cleanup_errors)

    @property
    def is_recording(self) -> bool:
        """True if currently recording."""
        return self._is_recording

    @property
    def frames_recorded(self) -> int:
        """Number of queued audio frames drained by the worker so far."""
        return self._total_frames

    @property
    def duration_seconds(self) -> float:
        """Duration of recorded audio in seconds."""
        return self._total_frames / self._sample_rate

    @property
    def stream_format(self) -> AudioStreamFormat | None:
        """Native callback stream format, once the tap has been described."""
        return self._stream_format

    @property
    def max_pending_buffers(self) -> int:
        """Maximum number of queued audio buffers before overflow."""
        return self._max_pending_buffers
