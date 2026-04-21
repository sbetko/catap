"""Audio recording from Core Audio taps."""

from __future__ import annotations

import contextlib
import ctypes
import queue
import threading
import traceback
import uuid
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from Foundation import NSArray, NSDictionary, NSNumber  # ty: ignore[unresolved-import]

from catap.bindings._audiotoolbox import (
    AudioBuffer,
    AudioBufferList,
    AudioStreamBasicDescription,
    PcmAudioConverter,
    kAudioFormatFlagIsFloat,
    make_linear_pcm_asbd,
)
from catap.bindings._coreaudio import (
    _CoreAudio,
    get_property_cfstring,
    get_property_struct,
    kAudioObjectPropertyElementMain,
    kAudioObjectPropertyScopeGlobal,
)
from catap.bindings.tap import _raise_if_missing_tap

# Pool buffers are ctypes char arrays rather than bytearrays because
# ``ctypes.memmove`` rejects bytearray/memoryview as source or destination,
# and the worker's AudioConverter path can consume the ctypes buffers directly
# without an extra copy.
type _PoolBuffer = ctypes.Array  # ctypes.c_char * N instance
type _WorkerItem = tuple[_PoolBuffer, int, int] | None
type _WorkerFailure = OSError | RuntimeError

_DEFAULT_MAX_PENDING_BUFFERS = 256
_DEFAULT_POOL_BUFFER_SIZE = 4096


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


def _combine_errors(
    summary: str, errors: list[_WorkerFailure]
) -> _WorkerFailure:
    """Annotate the primary error with summary and secondary tracebacks."""
    primary = errors[0]
    primary.add_note(summary)

    for error in errors[1:]:
        primary.add_note(
            "Additional cleanup failure:\n"
            f"{''.join(traceback.format_exception(error)).rstrip()}"
        )

    return primary


def _add_secondary_failure(
    primary: BaseException, summary: str, secondary: BaseException
) -> None:
    """Attach a secondary failure's traceback to ``primary`` as a note."""
    primary.add_note(
        f"{summary}:\n{''.join(traceback.format_exception(secondary)).rstrip()}"
    )


def _translate_exception(
    error_type: type[OSError] | type[RuntimeError],
    message: str,
    cause: Exception,
) -> _WorkerFailure:
    """Create an exception with an explicit cause chain."""
    try:
        raise error_type(message) from cause
    except error_type as wrapped:
        return wrapped


@dataclass(slots=True)
class _WorkerState:
    """Recorder-owned worker state shared by the RT callback and worker thread."""

    buffer_pool: deque[_PoolBuffer]
    work_queue: queue.Queue[_WorkerItem] | None
    output_file: BinaryIO | None = None
    wav_file: wave.Wave_write | None = None
    pcm_converter: PcmAudioConverter | None = None
    thread: threading.Thread | None = None
    failures: list[_WorkerFailure] = field(default_factory=list)
    callback_failed: bool = False
    writer_failed: bool = False


class AudioTimeStamp(ctypes.Structure):
    """Core Audio AudioTimeStamp structure."""

    _fields_ = [
        ("mSampleTime", ctypes.c_double),
        ("mHostTime", ctypes.c_uint64),
        ("mRateScalar", ctypes.c_double),
        ("mWordClockTime", ctypes.c_uint64),
        ("mSMPTETime", ctypes.c_uint8 * 24),
        ("mFlags", ctypes.c_uint32),
        ("mReserved", ctypes.c_uint32),
    ]


if TYPE_CHECKING:
    type AudioTimeStampPtr = ctypes._Pointer[AudioTimeStamp]
    type AudioBufferListPtr = ctypes._Pointer[AudioBufferList]
else:
    AudioTimeStampPtr = ctypes.c_void_p
    AudioBufferListPtr = ctypes.c_void_p


kAudioTapPropertyUID = int.from_bytes(b"tuid", "big")
kAudioTapPropertyFormat = int.from_bytes(b"tfmt", "big")


AudioDeviceIOProcType = ctypes.CFUNCTYPE(
    ctypes.c_int32,  # OSStatus return
    ctypes.c_uint32,  # AudioObjectID inDevice
    ctypes.POINTER(AudioTimeStamp),  # const AudioTimeStamp* inNow
    ctypes.POINTER(AudioBufferList),  # const AudioBufferList* inInputData
    ctypes.POINTER(AudioTimeStamp),  # const AudioTimeStamp* inInputTime
    ctypes.POINTER(AudioBufferList),  # AudioBufferList* outOutputData
    ctypes.POINTER(AudioTimeStamp),  # const AudioTimeStamp* inOutputTime
    ctypes.c_void_p,  # void* inClientData
)


_AudioHardwareCreateAggregateDevice = _CoreAudio.AudioHardwareCreateAggregateDevice
_AudioHardwareCreateAggregateDevice.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
]
_AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32

_AudioHardwareDestroyAggregateDevice = _CoreAudio.AudioHardwareDestroyAggregateDevice
_AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
_AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32

_AudioDeviceCreateIOProcID = _CoreAudio.AudioDeviceCreateIOProcID
_AudioDeviceCreateIOProcID.argtypes = [
    ctypes.c_uint32,
    AudioDeviceIOProcType,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]
_AudioDeviceCreateIOProcID.restype = ctypes.c_int32

_AudioDeviceDestroyIOProcID = _CoreAudio.AudioDeviceDestroyIOProcID
_AudioDeviceDestroyIOProcID.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_AudioDeviceDestroyIOProcID.restype = ctypes.c_int32

_AudioDeviceStart = _CoreAudio.AudioDeviceStart
_AudioDeviceStart.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_AudioDeviceStart.restype = ctypes.c_int32

_AudioDeviceStop = _CoreAudio.AudioDeviceStop
_AudioDeviceStop.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_AudioDeviceStop.restype = ctypes.c_int32


def _get_tap_uid(tap_id: int) -> str:
    """Return the UID string for a tap."""
    uid = get_property_cfstring(
        tap_id,
        kAudioTapPropertyUID,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    if uid is None:
        raise OSError(f"Tap {tap_id} reported an empty UID")
    return uid


def get_tap_format(tap_id: int) -> AudioStreamBasicDescription:
    """Return the audio format for a tap."""
    result = get_property_struct(
        tap_id,
        kAudioTapPropertyFormat,
        AudioStreamBasicDescription,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    # get_property_struct returns ctypes.Structure; narrow for callers.
    assert isinstance(result, AudioStreamBasicDescription)
    return result


def _create_aggregate_device_for_tap(tap_uid: str, name: str) -> int:
    """Create an aggregate device that includes the specified tap."""
    agg_uid = f"io.github.catap.aggregate.{uuid.uuid4()}"

    tap_entry = NSDictionary.dictionaryWithDictionary_(
        {
            "uid": tap_uid,
            "drift": NSNumber.numberWithBool_(True),
        }
    )
    tap_list = NSArray.arrayWithObject_(tap_entry)

    description = NSDictionary.dictionaryWithDictionary_(
        {
            "name": name,
            "uid": agg_uid,
            "private": NSNumber.numberWithBool_(True),
            "taps": tap_list,
            "tapautostart": NSNumber.numberWithBool_(False),
        }
    )

    cf_dict_ptr = description.__c_void_p__()
    device_id = ctypes.c_uint32(0)
    status = _AudioHardwareCreateAggregateDevice(cf_dict_ptr, ctypes.byref(device_id))
    if status != 0:
        raise OSError(f"Failed to create aggregate device: status {status}")

    return device_id.value


def _destroy_aggregate_device(device_id: int) -> None:
    """Destroy an aggregate device."""
    status = _AudioHardwareDestroyAggregateDevice(device_id)
    if status != 0:
        raise OSError(f"Failed to destroy aggregate device: status {status}")


def _destroy_io_proc(device_id: int, io_proc_id: ctypes.c_void_p) -> None:
    """Destroy a Core Audio IO proc."""
    status = _AudioDeviceDestroyIOProcID(device_id, io_proc_id)
    if status != 0:
        raise OSError(f"Failed to destroy IO proc: status {status}")


def _stop_audio_device(device_id: int, io_proc_id: ctypes.c_void_p) -> None:
    """Stop a Core Audio device IO proc."""
    status = _AudioDeviceStop(device_id, io_proc_id)
    if status != 0:
        raise OSError(f"Failed to stop audio device: status {status}")


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

        self._aggregate_device_id: int | None = None

        self._io_proc_id: ctypes.c_void_p | None = None
        self._is_recording = False
        self._max_pending_buffers = _validate_max_pending_buffers(max_pending_buffers)
        self._worker_state: _WorkerState | None = None
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

    @property
    def _worker_thread(self) -> threading.Thread | None:
        state = self._worker_state
        return None if state is None else state.thread

    @property
    def _work_queue(self) -> queue.Queue[_WorkerItem] | None:
        state = self._worker_state
        return None if state is None else state.work_queue

    @property
    def _buffer_pool(self) -> deque[_PoolBuffer] | None:
        state = self._worker_state
        return None if state is None else state.buffer_pool

    @property
    def _output_file(self) -> BinaryIO | None:
        state = self._worker_state
        return None if state is None else state.output_file

    @property
    def _wav_file(self) -> wave.Wave_write | None:
        state = self._worker_state
        return None if state is None else state.wav_file

    @property
    def _pcm_converter(self) -> PcmAudioConverter | None:
        state = self._worker_state
        return None if state is None else state.pcm_converter

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
            try:
                tap_uid = _get_tap_uid(self.tap_id)
            except OSError as exc:
                _raise_if_missing_tap(self.tap_id, exc)
                raise

            try:
                asbd = get_tap_format(self.tap_id)
                self._sample_rate = asbd.mSampleRate
                self._num_channels = asbd.mChannelsPerFrame
                self._bits_per_sample = asbd.mBitsPerChannel
                self._is_float = bool(asbd.mFormatFlags & kAudioFormatFlagIsFloat)
            except OSError as exc:
                _raise_if_missing_tap(self.tap_id, exc)
                # Use defaults if we can't get format.
                pass

            self._output_bits_per_sample = (
                16
                if self._is_float and self._bits_per_sample == 32
                else self._bits_per_sample
            )
            self._convert_float_output = self._is_float and self._bits_per_sample == 32

            self._reset_counters()

            cleanup: list[Callable[[], None]] = []
            worker_state: _WorkerState | None = None
            try:
                agg_id = _create_aggregate_device_for_tap(
                    tap_uid, "catap Recording Device"
                )
                self._aggregate_device_id = agg_id
                cleanup.append(lambda: _destroy_aggregate_device(agg_id))

                io_proc_id = ctypes.c_void_p()
                status = _AudioDeviceCreateIOProcID(
                    agg_id, self._callback, None, ctypes.byref(io_proc_id)
                )
                if status != 0:
                    raise OSError(f"Failed to create IO proc: status {status}")
                self._io_proc_id = io_proc_id
                cleanup.append(lambda: _destroy_io_proc(agg_id, io_proc_id))

                worker_state = self._start_worker()
                cleanup.append(lambda: self._stop_worker(worker_state))

                with self._lifecycle_lock:
                    self._is_recording = True

                status = _AudioDeviceStart(agg_id, io_proc_id)
                if status != 0:
                    raise OSError(f"Failed to start audio device: status {status}")
                cleanup.append(lambda: _stop_audio_device(agg_id, io_proc_id))
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
                self._aggregate_device_id = None
                self._io_proc_id = None
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
            aggregate_device_id = self._aggregate_device_id
            io_proc_id = self._io_proc_id
            worker_state = self._worker_state

        cleanup_errors: list[OSError | RuntimeError] = []

        if io_proc_id and aggregate_device_id:
            try:
                _stop_audio_device(aggregate_device_id, io_proc_id)
            except OSError as exc:
                cleanup_errors.append(exc)

            try:
                _destroy_io_proc(aggregate_device_id, io_proc_id)
            except OSError as exc:
                cleanup_errors.append(exc)

        if aggregate_device_id:
            try:
                _destroy_aggregate_device(aggregate_device_id)
            except OSError as exc:
                cleanup_errors.append(exc)

        if worker_state is not None:
            try:
                self._stop_worker(worker_state)
            except (OSError, RuntimeError) as exc:
                cleanup_errors.append(exc)

        self._aggregate_device_id = None
        self._io_proc_id = None
        if self._worker_state is worker_state:
            self._worker_state = None

        with self._lifecycle_lock:
            self._lifecycle_state = "idle"

        if cleanup_errors:
            raise _combine_errors("Failed to stop recording cleanly", cleanup_errors)

    def _create_worker_state(
        self,
        *,
        start_thread: bool,
        include_queue: bool = True,
        pool_depth: int | None = None,
        queue_maxsize: int | None = None,
        buffer_bytes: int | None = None,
    ) -> _WorkerState:
        """Create worker-owned queueing state and optionally start the worker."""
        if start_thread and not include_queue:
            raise ValueError("start_thread requires include_queue=True")

        bytes_per_frame = max(1, self._num_channels * (self._bits_per_sample // 8))
        pool_buffer_size = max(
            _DEFAULT_POOL_BUFFER_SIZE,
            bytes_per_frame * 1024,
        )
        if buffer_bytes is not None:
            pool_buffer_size = buffer_bytes

        depth = self._max_pending_buffers if pool_depth is None else pool_depth
        queue_bound = (
            self._max_pending_buffers
            if queue_maxsize is None
            else queue_maxsize
        )
        pool_type = ctypes.c_char * pool_buffer_size
        state = _WorkerState(
            buffer_pool=deque(pool_type() for _ in range(depth)),
            work_queue=queue.Queue(maxsize=queue_bound) if include_queue else None,
        )

        with contextlib.ExitStack() as stack:
            if self.output_path is not None:
                output_file = stack.enter_context(self.output_path.open("wb"))
                wav_file = wave.open(output_file, "wb")  # noqa: SIM115
                stack.callback(wav_file.close)
                wav_file.setnchannels(self._num_channels)
                wav_file.setsampwidth(self._output_bits_per_sample // 8)
                wav_file.setframerate(int(self._sample_rate))

                pcm_converter: PcmAudioConverter | None = None
                if self._convert_float_output:
                    pcm_converter = PcmAudioConverter(
                        make_linear_pcm_asbd(
                            self._sample_rate,
                            self._num_channels,
                            self._bits_per_sample,
                            is_float=True,
                        ),
                        make_linear_pcm_asbd(
                            self._sample_rate,
                            self._num_channels,
                            self._output_bits_per_sample,
                            is_float=False,
                        ),
                    )
                    stack.callback(pcm_converter.close)

                state.output_file = output_file
                state.wav_file = wav_file
                state.pcm_converter = pcm_converter

            if start_thread:
                thread = threading.Thread(
                    target=self._worker_loop,
                    args=(state,),
                    name="catap-audio-worker",
                    daemon=False,
                )
                thread.start()
                state.thread = thread

            stack.pop_all()

        return state

    def _install_synthetic_worker_state(
        self,
        *,
        include_queue: bool = True,
        pool_depth: int | None = None,
        queue_maxsize: int | None = None,
        buffer_bytes: int | None = None,
    ) -> _WorkerState:
        """Install worker state for tests/profiling without starting a thread."""
        if self._worker_state is not None:
            raise RuntimeError("Worker state is already installed")
        state = self._create_worker_state(
            start_thread=False,
            include_queue=include_queue,
            pool_depth=pool_depth,
            queue_maxsize=queue_maxsize,
            buffer_bytes=buffer_bytes,
        )
        self._worker_state = state
        return state

    def _start_worker(self) -> _WorkerState:
        """Start the background worker for file writes and user callbacks."""
        if self._worker_state is not None:
            return self._worker_state
        state = self._create_worker_state(start_thread=True)
        self._worker_state = state
        return state

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
        if state is None:
            state = self._worker_state
        if state is None:
            return None
        try:
            buf = state.buffer_pool.pop()
        except IndexError:
            return None
        if len(buf) < needed:
            # Resize is rare (only on buffer-size change). Still technically an
            # allocation on the RT thread, but bounded to a few occurrences.
            buf = (ctypes.c_char * needed)()
        return buf

    def _release_pool_buffer(
        self,
        buf: _PoolBuffer,
        state: _WorkerState | None = None,
    ) -> None:
        """Return a buffer to the pool. Safe to call after _stop_worker."""
        if state is None:
            state = self._worker_state
        if state is not None:
            state.buffer_pool.append(buf)

    def _enqueue_audio_data(
        self,
        buf: _PoolBuffer,
        num_frames: int,
        byte_count: int,
        state: _WorkerState | None = None,
    ) -> bool:
        """Queue audio work without blocking the Core Audio callback thread."""
        if state is None:
            state = self._worker_state
        if state is None:
            return True

        work_queue = state.work_queue
        if work_queue is None:
            state.buffer_pool.append(buf)
            return True

        try:
            work_queue.put_nowait((buf, num_frames, byte_count))
        except queue.Full:
            self._record_dropped_frames(num_frames)
            state.buffer_pool.append(buf)
            return False

        return True

    def _stop_worker(self, state: _WorkerState | None = None) -> None:
        """Flush and stop the background worker."""
        if state is None:
            state = self._worker_state
        if state is None:
            return

        if (
            state.work_queue is not None
            and state.thread is not None
            and state.thread.is_alive()
        ):
            state.work_queue.put(None)

        if state.thread is not None:
            state.thread.join()

        if self._worker_state is state:
            self._worker_state = None

        worker_errors = list(state.failures)
        dropped_buffers, dropped_frames = self._consume_dropped_stats()
        if dropped_buffers > 0:
            worker_errors.append(
                RuntimeError(
                    "Dropped "
                    f"{dropped_buffers} audio buffer(s) "
                    f"({dropped_frames} frame(s)) because the background worker "
                    "fell behind. Try a faster output path or a lighter on_data "
                    "callback."
                )
            )

        if worker_errors:
            raise _combine_errors("Failed to finalize audio worker", worker_errors)

    def _worker_loop(self, state: _WorkerState | None = None) -> None:
        """Drain queued audio outside the Core Audio callback thread."""
        if state is None:
            state = self._worker_state
        assert state is not None
        assert state.work_queue is not None

        try:
            while True:
                item = state.work_queue.get()
                if item is None:
                    break

                buf, num_frames, byte_count = item

                try:
                    if self._on_data is not None and not state.callback_failed:
                        # User may stash the buffer, so hand them a private copy
                        # rather than a pool-owned view.
                        try:
                            self._on_data(
                                bytes(memoryview(buf)[:byte_count]), num_frames
                            )
                        except Exception as exc:
                            state.callback_failed = True
                            state.failures.append(
                                _translate_exception(
                                    RuntimeError,
                                    f"Audio data callback failed: {exc}",
                                    exc,
                                )
                            )

                    if state.wav_file is not None and not state.writer_failed:
                        try:
                            if state.pcm_converter is not None:
                                state.pcm_converter.convert(buf, byte_count)
                                output_data = state.pcm_converter.output_view()
                            else:
                                output_data = memoryview(buf)[:byte_count]
                            state.wav_file.writeframesraw(output_data)
                        except Exception as exc:
                            state.writer_failed = True
                            state.failures.append(
                                _translate_exception(
                                    OSError,
                                    f"Failed to write WAV data: {exc}",
                                    exc,
                                )
                            )
                finally:
                    state.buffer_pool.append(buf)
        finally:
            self._close_worker_resources(state)

    def _close_worker_resources(self, state: _WorkerState) -> None:
        """Close worker-owned resources and retain any failures."""
        if state.wav_file is not None:
            try:
                state.wav_file.close()
            except Exception as exc:
                state.failures.append(
                    _translate_exception(
                        OSError,
                        f"Failed to finalize WAV file: {exc}",
                        exc,
                    )
                )
            finally:
                state.wav_file = None

        if state.output_file is not None:
            try:
                state.output_file.close()
            except Exception as exc:
                state.failures.append(
                    _translate_exception(
                        OSError,
                        f"Failed to close output file: {exc}",
                        exc,
                    )
                )
            finally:
                state.output_file = None

        if state.pcm_converter is not None:
            try:
                state.pcm_converter.close()
            except Exception as exc:
                state.failures.append(
                    _translate_exception(
                        OSError,
                        f"Failed to dispose PCM converter: {exc}",
                        exc,
                    )
                )
            finally:
                state.pcm_converter = None

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
