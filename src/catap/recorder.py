"""Audio recording from Core Audio taps."""

from __future__ import annotations

import ctypes
import math
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

# Core Audio retains the IOProc and its client-data pointer until a matching
# AudioDeviceDestroyIOProcID succeeds. If teardown cannot confirm that boundary,
# retain both owners for process lifetime rather than letting ctypes free memory
# that Core Audio may still access.
_ABANDONED_NATIVE_CAPTURES: list[
    tuple[_TapCaptureSession, NativeCoreAudioRecorder]
] = []
_ABANDONED_NATIVE_CAPTURES_LOCK = threading.Lock()


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
        recorder = AudioRecorder(tap_id, "output.wav")
        try:
            recorder.start()
            time.sleep(5)
        finally:
            try:
                if recorder.is_recording or recorder.needs_cleanup:
                    recorder.stop()
            finally:
                if not recorder.needs_cleanup:
                    destroy_process_tap(tap_id)

    If ``stop()`` raises while ``needs_cleanup`` is still true, the recorder
    kept the Core Audio objects it could not safely release. Call ``stop()``
    again to retry the teardown, and destroy the tap only once cleanup
    succeeds: a tap with mute enabled keeps its process muted for as long as
    the tap exists. The higher-level ``RecordingSession`` implements this
    retry contract automatically.
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
        self._native_drain_abort_event: threading.Event | None = None
        self._native_drain_done_event: threading.Event | None = None
        self._native_drain_failures: list[RuntimeError] = []

        self._total_frames = 0
        self._dropped_buffers = 0
        self._dropped_frames = 0
        self._nonzero_audio_seen = False

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
        if (
            not math.isfinite(stream_format.sample_rate)
            or stream_format.sample_rate <= 0
        ):
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
        if not stream_format.is_float and stream_format.bits_per_sample not in {
            16,
            24,
            32,
        }:
            raise UnsupportedTapFormatError(
                "Unsupported integer tap bit depth: only 16-, 24-, and 32-bit "
                "signed integer PCM is currently supported, got "
                f"{stream_format.bits_per_sample}-bit"
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
        self._nonzero_audio_seen = False

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
        existing_thread = self._native_drain_thread
        if existing_thread is not None:
            if not self._native_drain_is_quiesced():
                raise RuntimeError("Native recorder drain already started")
            self._native_drain_thread = None
            self._native_drain_stop_event = None
            self._native_drain_abort_event = None
            self._native_drain_done_event = None
            self._native_drain_failures = []

        stop_event = threading.Event()
        abort_event = threading.Event()
        done_event = threading.Event()
        self._native_drain_stop_event = stop_event
        self._native_drain_abort_event = abort_event
        self._native_drain_done_event = done_event
        self._native_drain_failures = []
        thread = threading.Thread(
            target=self._native_drain_loop,
            args=(native_recorder, stop_event, abort_event, done_event),
            name="catap-native-audio-drain",
            daemon=False,
        )
        self._native_drain_thread = thread
        thread.start()

    def _stop_native_drain(self, *, drain_remaining: bool = True) -> None:
        cleanup_errors: list[BaseException] = []
        stop_event = self._native_drain_stop_event
        if stop_event is not None:
            self._set_cleanup_event_with_retry(stop_event, cleanup_errors)
        if not drain_remaining:
            abort_event = self._native_drain_abort_event
            if abort_event is not None:
                self._set_cleanup_event_with_retry(abort_event, cleanup_errors)

        thread = self._native_drain_thread
        if thread is not None:
            try:
                if thread.is_alive():
                    thread.join()
            except BaseException as exc:
                cleanup_errors.append(exc)
                try:
                    done_event = self._native_drain_done_event
                    if done_event is not None and not done_event.is_set():
                        done_event.wait()
                except BaseException as cleanup_exc:
                    cleanup_errors.append(cleanup_exc)

            try:
                thread_is_alive = thread.is_alive()
            except BaseException as exc:
                cleanup_errors.append(exc)
                thread_is_alive = True
            done_event = self._native_drain_done_event
            thread_never_started = (
                not thread_is_alive and getattr(thread, "ident", None) is None
            )
            drain_is_done = (
                thread_never_started
                or (done_event is not None and done_event.is_set())
            )
            if not drain_is_done:
                if not cleanup_errors:
                    cleanup_errors.append(
                        RuntimeError("Native recorder drain did not stop")
                    )
                raise _combine_errors(
                    "Failed to stop native recorder drain",
                    cleanup_errors,
                )

        self._native_drain_thread = None
        self._native_drain_stop_event = None
        self._native_drain_abort_event = None
        self._native_drain_done_event = None

        failures = self._native_drain_failures
        self._native_drain_failures = []
        cleanup_errors.extend(failures)
        if cleanup_errors:
            raise _combine_errors(
                "Failed to drain native recorder",
                cleanup_errors,
            )

    @staticmethod
    def _set_cleanup_event_with_retry(
        event: threading.Event,
        cleanup_errors: list[BaseException],
    ) -> None:
        """Set a cleanup event, retrying once after an interruption."""
        try:
            event.set()
        except BaseException as exc:
            cleanup_errors.append(exc)
            try:
                event.set()
            except BaseException as cleanup_exc:
                cleanup_errors.append(cleanup_exc)

    def _native_drain_is_quiesced(self) -> bool:
        """Return whether no native reader can still access recorder memory."""
        thread = self._native_drain_thread
        if thread is None:
            return True
        done_event = self._native_drain_done_event
        if done_event is not None and done_event.is_set():
            return True
        try:
            return not thread.is_alive() and getattr(thread, "ident", None) is None
        except BaseException:
            return False

    def _native_drain_loop(
        self,
        native_recorder: NativeCoreAudioRecorder,
        stop_event: threading.Event,
        abort_event: threading.Event,
        done_event: threading.Event,
    ) -> None:
        try:
            while True:
                drained = self._drain_native_recorder(
                    native_recorder,
                    abort_event,
                )
                if abort_event.is_set():
                    return
                if stop_event.is_set():
                    return
                if not drained:
                    stop_event.wait(_NATIVE_DRAIN_IDLE_INTERVAL_SECONDS)
        except BaseException as exc:
            failure = _translate_exception(
                RuntimeError,
                f"Native recorder drain failed: {exc}",
                exc,
            )
            assert isinstance(failure, RuntimeError)
            self._native_drain_failures.append(failure)
        finally:
            done_event.set()

    def _drain_native_recorder(
        self,
        native_recorder: NativeCoreAudioRecorder,
        abort_event: threading.Event,
    ) -> bool:
        drained = False
        while True:
            if abort_event.is_set():
                return drained
            chunk = native_recorder.read()
            if chunk is None:
                return drained
            drained = True
            if not self._nonzero_audio_seen and chunk.data.strip(b"\x00"):
                self._nonzero_audio_seen = True
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

    @staticmethod
    def _release_native_recorder(
        native_recorder: NativeCoreAudioRecorder,
        capture_session: _TapCaptureSession | None,
        *,
        drain_quiesced: bool,
    ) -> bool:
        """Release native state only after every raw-pointer reader has stopped."""
        io_proc_destroyed = (
            capture_session is None or capture_session.io_proc_destroyed
        )
        if io_proc_destroyed and drain_quiesced:
            native_recorder.close()
            return True

        if capture_session is not None:
            with _ABANDONED_NATIVE_CAPTURES_LOCK:
                _ABANDONED_NATIVE_CAPTURES.append((capture_session, native_recorder))
        native_recorder.abandon()
        return False

    def _release_native_recorder_with_retry(
        self,
        native_recorder: NativeCoreAudioRecorder,
        capture_session: _TapCaptureSession | None,
        *,
        drain_quiesced: bool,
    ) -> tuple[bool, bool, list[BaseException]]:
        """Complete the native lifetime boundary after one interruption."""
        errors: list[BaseException] = []
        handled = False
        released = False

        try:
            try:
                released = self._release_native_recorder(
                    native_recorder,
                    capture_session,
                    drain_quiesced=drain_quiesced,
                )
            except BaseException as exc:
                errors.append(exc)
            else:
                handled = True
        finally:
            if not handled:
                try:
                    released = self._release_native_recorder(
                        native_recorder,
                        capture_session,
                        drain_quiesced=drain_quiesced,
                    )
                except BaseException as exc:
                    errors.append(exc)
                else:
                    handled = True

        return handled, released, errors

    def _publish_lifecycle_state_with_retry(
        self,
        state: str,
    ) -> tuple[bool, list[BaseException]]:
        """Publish terminal lifecycle state after one interruption."""
        errors: list[BaseException] = []
        published = False

        try:
            try:
                with self._lifecycle_lock:
                    self._lifecycle_state = state
            except BaseException as exc:
                errors.append(exc)
            else:
                published = True
        finally:
            if not published:
                try:
                    with self._lifecycle_lock:
                        self._lifecycle_state = state
                except BaseException as exc:
                    errors.append(exc)
                else:
                    published = True

        return published, errors

    def start(self) -> None:
        """Start recording audio.

        Raises:
            OSError: If recording cannot be started
            RuntimeError: If already recording
        """
        start_succeeded = False
        start_error: BaseException | None = None
        lifecycle_claimed = False
        try:
            with self._lifecycle_lock:
                if self._lifecycle_state == "recording":
                    raise RuntimeError("Already recording")
                if self.needs_cleanup:
                    raise RuntimeError(
                        "Recorder has pending cleanup; call stop() before restarting"
                    )
                if self._lifecycle_state != "idle":
                    raise RuntimeError("Recorder is already starting or stopping")
                lifecycle_claimed = True
                self._lifecycle_state = "starting"

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
            capture_cleanup_registered = False
            native_recorder: NativeCoreAudioRecorder | None = None
            try:
                native_recorder = self._create_native_recorder()
                self._native_recorder = native_recorder
                capture_session = self._capture_engine.open_tap_capture(
                    self.tap_id,
                    native_recorder.io_proc_pointer,
                    native_recorder.handle,
                )
                self._capture_session = capture_session
                acknowledge_capture_session = getattr(
                    self._capture_engine,
                    "acknowledge_capture_session",
                    None,
                )
                if acknowledge_capture_session is not None:
                    acknowledge_capture_session(capture_session)
                cleanup.append(lambda: self._capture_engine.close(capture_session))
                capture_cleanup_registered = True

                self._worker.start(self._make_worker_config())
                cleanup.append(lambda: self._worker.stop(publish=False))
                cleanup.append(
                    lambda: self._stop_native_drain(drain_remaining=False)
                )
                self._start_native_drain(native_recorder)

                with self._lifecycle_lock:
                    self._is_recording = True

                self._capture_engine.start(capture_session)
                cleanup.append(lambda: self._capture_engine.stop(capture_session))
            except BaseException as exc:
                native_handled = native_recorder is None
                try:
                    try:
                        failed_capture_session = getattr(
                            self._capture_engine,
                            "failed_capture_session",
                            None,
                        )
                        if capture_session is None and isinstance(
                            failed_capture_session,
                            _TapCaptureSession,
                        ):
                            capture_session = failed_capture_session
                            self._capture_session = capture_session
                        if (
                            capture_session is not None
                            and not capture_cleanup_registered
                        ):
                            cleanup.append(
                                lambda: self._capture_engine.close(capture_session)
                            )
                            capture_cleanup_registered = True
                        self._is_recording = False
                        for step in reversed(cleanup):
                            try:
                                step()
                            except BaseException as cleanup_exc:
                                _add_secondary_failure(
                                    exc,
                                    "Cleanup failure during recorder startup",
                                    cleanup_exc,
                                )
                    except BaseException as cleanup_exc:
                        _add_secondary_failure(
                            exc,
                            "Cleanup interruption during recorder startup",
                            cleanup_exc,
                        )
                finally:
                    try:
                        if native_recorder is not None:
                            try:
                                drain_quiesced = self._native_drain_is_quiesced()
                            except BaseException as cleanup_exc:
                                drain_quiesced = False
                                _add_secondary_failure(
                                    exc,
                                    "Cleanup failure while checking the native "
                                    "drain",
                                    cleanup_exc,
                                )
                            if (
                                not drain_quiesced
                                and self._native_drain_abort_event is not None
                            ):
                                abort_errors: list[BaseException] = []
                                self._set_cleanup_event_with_retry(
                                    self._native_drain_abort_event,
                                    abort_errors,
                                )
                                for cleanup_exc in abort_errors:
                                    _add_secondary_failure(
                                        exc,
                                        "Cleanup failure while aborting the native "
                                        "drain",
                                        cleanup_exc,
                                    )
                            (
                                native_handled,
                                native_was_released,
                                release_errors,
                            ) = self._release_native_recorder_with_retry(
                                native_recorder,
                                capture_session,
                                drain_quiesced=drain_quiesced,
                            )
                            for cleanup_exc in release_errors:
                                _add_secondary_failure(
                                    exc,
                                    "Cleanup failure while releasing native "
                                    "recorder state",
                                    cleanup_exc,
                                )
                            if native_handled and not native_was_released:
                                exc.add_note(
                                    "Retained native recorder state because startup "
                                    "cleanup did not confirm both IOProc destruction "
                                    "and drain quiescence."
                                )
                            if not native_handled:
                                exc.add_note(
                                    "Native recorder ownership remains attached to "
                                    "this recorder; call stop() to retry cleanup."
                                )
                    except BaseException as cleanup_exc:
                        _add_secondary_failure(
                            exc,
                            "Cleanup failure at native recorder lifetime boundary",
                            cleanup_exc,
                        )
                    finally:
                        if native_handled:
                            self._native_recorder = None
                        capture_cleanup_finished = capture_session is None or (
                            not capture_session.started
                            and capture_session.io_proc_destroyed
                            and capture_session.aggregate_device_destroyed
                        )
                        if capture_cleanup_finished:
                            self._capture_session = None
                raise
            start_succeeded = True
        except BaseException as exc:
            start_error = exc
            raise
        finally:
            if lifecycle_claimed:
                if not start_succeeded:
                    self._is_recording = False
                if start_succeeded:
                    terminal_state = "recording"
                elif self.needs_cleanup:
                    terminal_state = "cleanup_failed"
                else:
                    terminal_state = "idle"
                lifecycle_published, lifecycle_errors = (
                    self._publish_lifecycle_state_with_retry(terminal_state)
                )
                if not lifecycle_published:
                    lifecycle_errors.append(
                        RuntimeError(
                            "Failed to publish recorder lifecycle state "
                            f"{terminal_state!r}"
                        )
                    )
                if lifecycle_errors:
                    if start_error is None:
                        raise _combine_errors(
                            "Failed to finalize recorder lifecycle",
                            lifecycle_errors,
                        )
                    for lifecycle_error in lifecycle_errors:
                        _add_secondary_failure(
                            start_error,
                            "Cleanup failure while publishing recorder lifecycle",
                            lifecycle_error,
                        )

    def stop(self) -> None:
        """Stop recording and finalize any WAV output.

        Queued audio is still delivered to ``on_buffer`` during shutdown, and
        the in-flight callback is waited on, so a callback that never returns
        blocks this method indefinitely.

        If this method raises while ``needs_cleanup`` is still true, call it
        again to retry the failed teardown.

        Raises:
            OSError: If Core Audio cleanup fails
            RuntimeError: If not recording
        """
        if self._worker.is_worker_thread:
            raise RuntimeError(
                "Cannot call AudioRecorder.stop() from an on_buffer callback; "
                "signal the owning thread with threading.Event and call stop() "
                "there instead"
            )

        lifecycle_claimed = False
        stop_error: BaseException | None = None
        try:
            with self._lifecycle_lock:
                if self._lifecycle_state == "idle" and not self.needs_cleanup:
                    raise RuntimeError("Not recording")
                if self._lifecycle_state not in {
                    "idle",
                    "recording",
                    "cleanup_failed",
                }:
                    raise RuntimeError("Recorder is already starting or stopping")

                lifecycle_claimed = True
                self._lifecycle_state = "stopping"
                self._is_recording = False

            self._finish_stop()
        except BaseException as exc:
            stop_error = exc
            raise
        finally:
            if lifecycle_claimed:
                self._is_recording = False
                terminal_state = "cleanup_failed" if self.needs_cleanup else "idle"
                if self._lifecycle_state != terminal_state:
                    lifecycle_published, lifecycle_errors = (
                        self._publish_lifecycle_state_with_retry(terminal_state)
                    )
                    if not lifecycle_published:
                        lifecycle_errors.append(
                            RuntimeError(
                                "Failed to publish recorder lifecycle state "
                                f"{terminal_state!r}"
                            )
                        )
                    if lifecycle_errors:
                        if stop_error is None:
                            raise _combine_errors(
                                "Failed to finalize recorder lifecycle",
                                lifecycle_errors,
                            )
                        for lifecycle_error in lifecycle_errors:
                            _add_secondary_failure(
                                stop_error,
                                "Cleanup failure while publishing recorder "
                                "lifecycle",
                                lifecycle_error,
                            )

    def _finish_stop(self) -> None:
        """Finish teardown after the public lifecycle claim is recoverable."""
        capture_session = self._capture_session
        native_recorder = self._native_recorder

        cleanup_errors: list[BaseException] = []
        publish_worker_output = True
        drain_quiesced = self._native_drain_thread is None
        native_handled = native_recorder is None

        def record_cleanup_error(error: BaseException) -> None:
            if all(error is not existing for existing in cleanup_errors):
                cleanup_errors.append(error)

        try:
            try:
                if capture_session is not None:
                    try:
                        self._capture_engine.close(capture_session)
                    except BaseException as exc:
                        record_cleanup_error(exc)
                        if capture_session.started:
                            publish_worker_output = False

                if self._native_drain_thread is not None:
                    try:
                        self._stop_native_drain(
                            drain_remaining=(
                                native_recorder is not None
                                and (
                                    capture_session is None
                                    or not capture_session.started
                                )
                            )
                        )
                    except BaseException as exc:
                        record_cleanup_error(exc)
                        publish_worker_output = False
                    finally:
                        try:
                            drain_quiesced = self._native_drain_is_quiesced()
                        except BaseException as exc:
                            record_cleanup_error(exc)
                            drain_quiesced = False
                        if not drain_quiesced:
                            publish_worker_output = False

                if native_recorder is not None:
                    try:
                        native_errors = self._native_recorder_errors(
                            native_recorder.stats()
                        )
                    except BaseException as exc:
                        record_cleanup_error(exc)
                        publish_worker_output = False
                    else:
                        if native_errors:
                            cleanup_errors.extend(native_errors)
                            publish_worker_output = False

                try:
                    self._worker.stop(publish=publish_worker_output)
                except BaseException as exc:
                    record_cleanup_error(exc)
                    try:
                        self._worker.stop(publish=False)
                    except BaseException as cleanup_exc:
                        record_cleanup_error(cleanup_exc)
            except BaseException as exc:
                # Defer asynchronous exceptions until the native ownership
                # boundary has completed.
                record_cleanup_error(exc)
            finally:
                try:
                    if capture_session is not None and (
                        capture_session.started
                        or not capture_session.io_proc_destroyed
                        or not capture_session.aggregate_device_destroyed
                    ):
                        try:
                            self._capture_engine.close(capture_session)
                        except BaseException as exc:
                            record_cleanup_error(exc)
                finally:
                    try:
                        if (
                            self._native_drain_thread is not None
                            and not drain_quiesced
                        ):
                            try:
                                self._stop_native_drain(
                                    drain_remaining=(
                                        native_recorder is not None
                                        and (
                                            capture_session is None
                                            or not capture_session.started
                                        )
                                    )
                                )
                            except BaseException as exc:
                                record_cleanup_error(exc)
                            finally:
                                try:
                                    drain_quiesced = (
                                        self._native_drain_is_quiesced()
                                    )
                                except BaseException as exc:
                                    record_cleanup_error(exc)
                                    drain_quiesced = False
                    finally:
                        try:
                            self._worker.stop(publish=False)
                        except BaseException as exc:
                            record_cleanup_error(exc)
        finally:
            try:
                if native_recorder is not None:
                    if (
                        not drain_quiesced
                        and self._native_drain_abort_event is not None
                    ):
                        abort_errors: list[BaseException] = []
                        self._set_cleanup_event_with_retry(
                            self._native_drain_abort_event,
                            abort_errors,
                        )
                        for abort_error in abort_errors:
                            record_cleanup_error(abort_error)
                    (
                        native_handled,
                        native_was_released,
                        release_errors,
                    ) = self._release_native_recorder_with_retry(
                        native_recorder,
                        capture_session,
                        drain_quiesced=drain_quiesced,
                    )
                    for release_error in release_errors:
                        record_cleanup_error(release_error)
                    if native_handled and not native_was_released:
                        record_cleanup_error(
                            RuntimeError(
                                "Retained native recorder state because cleanup did "
                                "not confirm both IOProc destruction and drain "
                                "quiescence."
                            )
                        )
                    if not native_handled:
                        record_cleanup_error(
                            RuntimeError(
                                "Native recorder ownership remains attached; call "
                                "stop() to retry cleanup."
                            )
                        )
            except BaseException as exc:
                record_cleanup_error(exc)
            finally:
                if native_handled:
                    self._native_recorder = None
                capture_cleanup_finished = capture_session is None or (
                    not capture_session.started
                    and capture_session.io_proc_destroyed
                    and capture_session.aggregate_device_destroyed
                )
                if capture_cleanup_finished:
                    self._capture_session = None
                else:
                    record_cleanup_error(
                        RuntimeError("Core Audio capture cleanup remains pending")
                    )

                terminal_state = "cleanup_failed" if self.needs_cleanup else "idle"
                lifecycle_published, lifecycle_errors = (
                    self._publish_lifecycle_state_with_retry(terminal_state)
                )
                for lifecycle_error in lifecycle_errors:
                    record_cleanup_error(lifecycle_error)
                if not lifecycle_published:
                    record_cleanup_error(
                        RuntimeError(
                            "Failed to publish recorder lifecycle state "
                            f"{terminal_state!r}"
                        )
                    )

        if cleanup_errors:
            raise _combine_errors(
                "Failed to stop recording cleanly",
                cleanup_errors,
            )

    @property
    def is_recording(self) -> bool:
        """True if currently recording."""
        return self._is_recording

    @property
    def needs_cleanup(self) -> bool:
        """True when a failed teardown still owns retryable resources.

        False during active recording; use ``is_recording`` for capture
        state. After a failed ``stop()``, stays true until a retried
        ``stop()`` succeeds, matching ``RecordingSession.needs_cleanup``.
        """
        return not self._is_recording and (
            self._capture_session is not None
            or self._native_recorder is not None
            or self._native_drain_thread is not None
            or self._worker.needs_cleanup
        )

    @property
    def captured_only_silence(self) -> bool:
        """True while no nonzero audio sample has been captured.

        macOS delivers zeroed tap audio when the recording process lacks
        system-audio permission, so a completed capture that reports only
        silence while audio was playing usually means the app hosting this
        process was never granted System Audio Recording access.
        """
        return not self._nonzero_audio_seen

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
