"""Audio recording from Core Audio taps."""

from __future__ import annotations

import ctypes
import queue
import threading
import traceback
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
from catap._recording_worker import (
    _AudioWorker,
    _combine_errors,
    _PoolBuffer,
    _WorkerConfig,
    _WorkerItem,
    _WorkerState,
)
from catap.bindings._audiotoolbox import AudioBuffer

_DEFAULT_MAX_PENDING_BUFFERS = 256


def _validate_recording_target(
    output_path: str | Path | None,
    on_data: Callable[[bytes, int], None] | None,
) -> Path | None:
    """Normalize the recording target and reject target-less captures."""
    normalized_output_path = Path(output_path) if output_path else None
    if normalized_output_path is None and on_data is None:
        raise ValueError(
            "output_path must be provided unless on_data is set for streaming mode"
        )
    return normalized_output_path


def _validate_max_pending_buffers(value: int) -> int:
    """Validate and normalize the recorder queue bound."""
    if value <= 0:
        raise ValueError("max_pending_buffers must be greater than 0")
    return value


def _add_secondary_failure(
    primary: BaseException, summary: str, secondary: BaseException
) -> None:
    """Attach a secondary failure's traceback to ``primary`` as a note."""
    primary.add_note(
        f"{summary}:\n{''.join(traceback.format_exception(secondary)).rstrip()}"
    )


class AudioRecorder:
    """Records audio from a Core Audio tap to a WAV file.

    This recorder creates an aggregate device containing the tap,
    which is required by Core Audio to read audio data from taps.

    Usage:
        from catap import TapDescription, create_process_tap, destroy_process_tap

        tap_desc = TapDescription.stereo_mixdown_of_processes([process_id])
        tap_id = create_process_tap(tap_desc)

        recorder = AudioRecorder(tap_id, "output.wav")
        recorder.start()
        time.sleep(5)  # Record for 5 seconds
        recorder.stop()

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

        # Stream format (populated on start).
        self._sample_rate = 44100.0
        self._num_channels = 2
        self._bits_per_sample = 32
        self._output_bits_per_sample = 32
        self._convert_float_output = True
        self._is_float = True

        # Keep reference to callback to prevent garbage collection.
        self._callback = AudioDeviceIOProcType(self._io_proc)

    def _default_stream_format(self) -> _TapStreamFormat:
        """Build the fallback stream format used before tap metadata is known."""
        return _TapStreamFormat(
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
            bits_per_sample=self._bits_per_sample,
            is_float=self._is_float,
        )

    def _apply_stream_format(self, stream_format: _TapStreamFormat) -> None:
        """Apply tap stream metadata to recorder state."""
        self._sample_rate = stream_format.sample_rate
        self._num_channels = stream_format.num_channels
        self._bits_per_sample = stream_format.bits_per_sample
        self._is_float = stream_format.is_float

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

    @property
    def _worker_state(self) -> _WorkerState | None:
        return self._worker.state

    @_worker_state.setter
    def _worker_state(self, state: _WorkerState | None) -> None:
        self._worker.state = state

    @property
    def _worker_thread(self) -> threading.Thread | None:
        return self._worker.thread

    @property
    def _work_queue(self) -> queue.Queue[_WorkerItem] | None:
        return self._worker.work_queue

    @property
    def _output_file(self):
        return self._worker.output_file

    @property
    def _wav_file(self):
        return self._worker.wav_file

    @property
    def _pcm_converter(self):
        return self._worker.pcm_converter

    def _reset_counters(self) -> None:
        with self._stats_lock:
            self._total_frames = 0
            self._dropped_buffers = 0
            self._dropped_frames = 0

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

            worker_state = self._worker_state
            for i in range(num_buffers):
                # AudioBufferList has a variable-length mBuffers array; index
                # past the first slot via pointer arithmetic.
                buffer_offset = ctypes.sizeof(AudioBuffer) * i
                buffer_ptr = ctypes.cast(
                    ctypes.addressof(buffer_list.mBuffers) + buffer_offset,
                    ctypes.POINTER(AudioBuffer),
                )
                buffer = buffer_ptr.contents

                if buffer.mData and buffer.mDataByteSize > 0:
                    byte_count = buffer.mDataByteSize

                    bytes_per_frame = buffer.mNumberChannels * (
                        self._bits_per_sample // 8
                    )
                    if bytes_per_frame > 0:
                        num_frames = byte_count // bytes_per_frame
                    else:
                        num_frames = 0

                    buf = self._acquire_pool_buffer(byte_count, worker_state)
                    if buf is None:
                        self._record_dropped_frames(num_frames)
                        continue

                    ctypes.memmove(buf, buffer.mData, byte_count)

                    if self._enqueue_audio_data(
                        buf,
                        num_frames,
                        byte_count,
                        worker_state,
                    ):
                        self._record_accepted_frames(num_frames)

        except Exception:
            # Must not raise from a Core Audio callback.
            pass

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
            stream_format = self._capture_engine.describe_tap_stream(
                self.tap_id,
                default=self._default_stream_format(),
            )
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
            worker_state: _WorkerState | None = None
            try:
                capture_session = self._capture_engine.open_tap_capture(
                    self.tap_id,
                    self._callback,
                )
                self._capture_session = capture_session
                cleanup.append(lambda: self._capture_engine.close(capture_session))

                worker_state = self._start_worker()
                cleanup.append(lambda: self._stop_worker(worker_state))

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
                if self._worker_state is worker_state:
                    self._worker_state = None
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
            worker_state = self._worker_state

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

        if worker_state is not None:
            try:
                self._stop_worker(worker_state)
            except (OSError, RuntimeError) as exc:
                cleanup_errors.append(exc)

        self._capture_session = None

        with self._lifecycle_lock:
            self._lifecycle_state = "idle"

        if cleanup_errors:
            raise _combine_errors("Failed to stop recording cleanly", cleanup_errors)

    def _start_worker(self) -> _WorkerState:
        """Start the background worker for file writes and user callbacks."""
        return self._worker.start(self._make_worker_config())

    def _acquire_pool_buffer(
        self,
        needed: int,
        state: _WorkerState | None = None,
    ) -> _PoolBuffer | None:
        """Return a ctypes buffer sized for ``needed`` bytes, or None if exhausted.

        Called from the Core Audio real-time thread. In steady state the pool
        is non-empty and buffers are already large enough, so the hot path
        skips both the allocator and any lock.
        """
        return self._worker.acquire_pool_buffer(needed, state)

    def _enqueue_audio_data(
        self,
        buf: _PoolBuffer,
        num_frames: int,
        byte_count: int,
        state: _WorkerState | None = None,
    ) -> bool:
        """Queue audio work without blocking the Core Audio callback thread."""
        return self._worker.enqueue_audio_data(buf, num_frames, byte_count, state)

    def _stop_worker(self, state: _WorkerState | None = None) -> None:
        """Flush and stop the background worker."""
        self._worker.stop(state)

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
