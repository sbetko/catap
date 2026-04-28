"""Audio recording from Core Audio taps."""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from pathlib import Path

from catap._capture_engine import (
    AudioBufferListPtr,
    AudioDeviceIOProcType,
    AudioTimeStampPtr,
    _TapCaptureEngine,
    _TapCaptureSession,
    _TapStreamFormat,
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
from catap.bindings._audiotoolbox import AudioBuffer, kAudioFormatLinearPCM


class UnsupportedTapFormatError(ValueError):
    """Raised when a tap exposes an audio layout catap cannot safely record."""


class AudioRecorder:
    """Record audio from a Core Audio tap.

    This recorder reads the tap through a private aggregate device and can
    write WAV output, call an ``on_data`` callback, or do both.

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
        on_data: Callable[[bytes, int], None] | None = None,
        *,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
    ) -> None:
        """Initialize the recorder.

        Args:
            tap_id: AudioObjectID of the tap to record from
            output_path: Path to write the WAV file, or None for streaming mode
            on_data: Optional callback invoked with ``(raw_bytes, num_frames)``
                for each captured buffer. The bytes are the tap's native format
                (typically 32-bit float, little-endian, interleaved); inspect
                ``sample_rate``, ``num_channels``, and ``is_float`` to interpret
                them. The callback runs on catap's background worker thread, so
                Core Audio's real-time callback stays lightweight.
            max_pending_buffers: Maximum number of audio buffers to queue for
                the background worker before new buffers are dropped and the
                capture fails on stop. Higher values trade memory for tolerance
                of slow disk writes or ``on_data`` callbacks.
        Raises:
            ValueError: If neither ``output_path`` nor ``on_data`` is provided
        """
        self.tap_id = tap_id
        self.output_path = _validate_recording_target(output_path, on_data)
        self._on_data = on_data

        self._capture_engine = _TapCaptureEngine()
        self._capture_session: _TapCaptureSession | None = None
        self._is_recording = False
        self._max_pending_buffers = _validate_max_pending_buffers(max_pending_buffers)
        self._worker = _AudioWorker(
            record_dropped_frames=self._record_dropped_frames,
            consume_dropped_stats=self._consume_dropped_stats,
        )
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_state = "idle"
        self._stats_lock = threading.Lock()

        self._total_frames = 0
        self._dropped_buffers = 0
        self._dropped_frames = 0
        self._io_proc_failure: RuntimeError | None = None
        self._io_proc_failure_count = 0

        # Stream format (populated on start).
        self._sample_rate = 44100.0
        self._num_channels = 2
        self._bits_per_sample = 32
        self._bytes_per_frame = 8
        self._output_bits_per_sample = 32
        self._convert_float_output = True
        self._is_float = True

        # Keep reference to callback to prevent garbage collection.
        self._callback = AudioDeviceIOProcType(self._io_proc)

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
        return _WorkerConfig(
            output_path=self.output_path,
            on_data=self._on_data,
            max_pending_buffers=self._max_pending_buffers,
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
            bits_per_sample=self._bits_per_sample,
            output_bits_per_sample=self._output_bits_per_sample,
            convert_float_output=self._convert_float_output,
        )

    def _reset_counters(self) -> None:
        with self._stats_lock:
            self._total_frames = 0
            self._dropped_buffers = 0
            self._dropped_frames = 0
            self._io_proc_failure = None
            self._io_proc_failure_count = 0

    def _record_accepted_frames(self, num_frames: int) -> None:
        with self._stats_lock:
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

    def _record_io_proc_failure(self, exc: Exception) -> None:
        """Remember callback failures without raising into Core Audio."""
        with self._stats_lock:
            self._io_proc_failure_count += 1
            if self._io_proc_failure is None:
                failure = _translate_exception(
                    RuntimeError,
                    f"Audio callback failed: {exc}",
                    exc,
                )
                assert isinstance(failure, RuntimeError)
                self._io_proc_failure = failure

    def _consume_io_proc_failure(self) -> RuntimeError | None:
        """Return and clear the first callback failure captured this run."""
        with self._stats_lock:
            failure = self._io_proc_failure
            failure_count = self._io_proc_failure_count
            self._io_proc_failure = None
            self._io_proc_failure_count = 0

        if failure is not None and failure_count > 1:
            failure.add_note(
                f"Audio callback failed {failure_count} times; first failure shown."
            )
        return failure

    def _io_proc(
        self,
        device: int,
        now: AudioTimeStampPtr,
        input_data: AudioBufferListPtr,
        input_time: AudioTimeStampPtr,
        output_data: AudioBufferListPtr,
        output_time: AudioTimeStampPtr,
        client_data: ctypes.c_void_p,
    ) -> int:
        """Audio I/O callback - called on the Core Audio real-time thread."""
        if not self._is_recording:
            return 0

        try:
            if not input_data:
                return 0

            buffer_list = input_data.contents
            num_buffers = buffer_list.mNumberBuffers

            if num_buffers == 0:
                return 0
            if num_buffers != 1:
                raise UnsupportedTapFormatError(
                    "Unsupported AudioBufferList layout: expected one "
                    f"interleaved buffer, got {num_buffers}"
                )

            for i in range(num_buffers):
                # AudioBufferList has a variable-length mBuffers array; index
                # past the first slot via pointer arithmetic.
                buffer_offset = ctypes.sizeof(AudioBuffer) * i
                buffer_ptr = ctypes.cast(
                    ctypes.addressof(buffer_list.mBuffers) + buffer_offset,
                    ctypes.POINTER(AudioBuffer),
                )
                buffer = buffer_ptr.contents

                byte_count = buffer.mDataByteSize
                if byte_count > 0:
                    if not buffer.mData:
                        raise UnsupportedTapFormatError(
                            "Audio buffer reported bytes without a data pointer"
                        )
                    if buffer.mNumberChannels != self._num_channels:
                        raise UnsupportedTapFormatError(
                            "Unsupported audio buffer channel count: expected "
                            f"{self._num_channels}, got {buffer.mNumberChannels}"
                        )

                    bytes_per_frame = self._bytes_per_frame
                    if bytes_per_frame > 0:
                        num_frames = byte_count // bytes_per_frame
                    else:
                        num_frames = 0
                    if num_frames == 0 or byte_count % bytes_per_frame:
                        raise UnsupportedTapFormatError(
                            "Audio buffer byte count is not a whole number of "
                            f"frames: {byte_count} bytes for "
                            f"{bytes_per_frame}-byte frames"
                        )

                    buf = self._worker.acquire_pool_buffer(byte_count)
                    if buf is None:
                        self._record_dropped_frames(num_frames)
                        continue

                    ctypes.memmove(buf, buffer.mData, byte_count)

                    if self._worker.enqueue_audio_data(
                        buf,
                        num_frames,
                        byte_count,
                    ):
                        self._record_accepted_frames(num_frames)

        except Exception as exc:
            # Must not raise from a Core Audio callback.
            self._record_io_proc_failure(exc)

        return 0  # noErr

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
                raise RuntimeError("Recorder lifecycle transition already in progress")
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
            try:
                capture_session = self._capture_engine.open_tap_capture(
                    self.tap_id,
                    self._callback,
                )
                self._capture_session = capture_session
                cleanup.append(lambda: self._capture_engine.close(capture_session))

                self._worker.start(self._make_worker_config())
                cleanup.append(lambda: self._worker.stop(publish=False))

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
                raise RuntimeError("Recorder lifecycle transition already in progress")

            self._lifecycle_state = "stopping"
            self._is_recording = False
            capture_session = self._capture_session

        cleanup_errors: list[OSError | RuntimeError] = []

        if capture_session is not None:
            try:
                self._capture_engine.stop(capture_session)
            except OSError as exc:
                cleanup_errors.append(exc)

            try:
                self._capture_engine.close(capture_session)
            except OSError as exc:
                cleanup_errors.append(exc)

        try:
            self._worker.stop()
        except (OSError, RuntimeError) as exc:
            cleanup_errors.append(exc)

        callback_failure = self._consume_io_proc_failure()
        if callback_failure is not None:
            cleanup_errors.append(callback_failure)

        self._capture_session = None

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
        """Number of audio frames accepted for processing so far."""
        with self._stats_lock:
            return self._total_frames

    @property
    def duration_seconds(self) -> float:
        """Duration of recorded audio in seconds."""
        with self._stats_lock:
            total_frames = self._total_frames
        return total_frames / self._sample_rate

    @property
    def sample_rate(self) -> float:
        """Sample rate in Hz."""
        return self._sample_rate

    @property
    def max_pending_buffers(self) -> int:
        """Maximum number of queued audio buffers before overflow."""
        return self._max_pending_buffers

    @property
    def num_channels(self) -> int:
        """Number of audio channels."""
        return self._num_channels

    @property
    def is_float(self) -> bool:
        """True if audio format is float32."""
        return self._is_float
