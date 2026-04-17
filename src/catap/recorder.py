"""Audio recording from Core Audio taps."""

from __future__ import annotations

import contextlib
import ctypes
import queue
import threading
import uuid
import wave
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from Foundation import NSArray, NSDictionary, NSNumber  # ty: ignore[unresolved-import]

from catap.bindings._accelerate import float32_to_int16 as _float32_to_int16
from catap.bindings._coreaudio import (
    _CoreAudio,
    get_property_cfstring,
    get_property_struct,
    kAudioObjectPropertyElementMain,
    kAudioObjectPropertyScopeGlobal,
)

# Pool buffers are ctypes char arrays rather than bytearrays because
# ``ctypes.memmove`` rejects bytearray/memoryview as source or destination -
# both the RT-thread ingest memmove and the vDSP conversion's memmove need a
# ctypes-native buffer.
type _PoolBuffer = ctypes.Array  # ctypes.c_char * N instance
type _WorkerItem = tuple[_PoolBuffer, int, int] | None

_DEFAULT_MAX_PENDING_BUFFERS = 256
_DEFAULT_POOL_BUFFER_SIZE = 4096


def _validate_max_pending_buffers(value: int) -> int:
    """Validate and normalize the recorder queue bound."""
    if value <= 0:
        raise ValueError("max_pending_buffers must be greater than 0")
    return value


def _combine_errors(
    summary: str, errors: list[OSError | RuntimeError]
) -> OSError | RuntimeError:
    """Collapse multiple cleanup failures into one exception."""
    primary = errors[0]
    combined: OSError | RuntimeError

    if isinstance(primary, RuntimeError):
        combined = RuntimeError(f"{summary}: {primary}")
    else:
        combined = OSError(f"{summary}: {primary}")

    for error in errors[1:]:
        combined.add_note(str(error))

    return combined


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


class AudioBuffer(ctypes.Structure):
    """Single audio buffer within an AudioBufferList."""

    _fields_ = [
        ("mNumberChannels", ctypes.c_uint32),
        ("mDataByteSize", ctypes.c_uint32),
        ("mData", ctypes.c_void_p),
    ]


class AudioBufferList(ctypes.Structure):
    """Core Audio AudioBufferList.

    The trailing ``mBuffers`` array is variable-length; the struct is defined
    with one slot so its base size is correct, and extra buffers are reached
    via pointer arithmetic from ``_io_proc``.
    """

    _fields_ = [
        ("mNumberBuffers", ctypes.c_uint32),
        ("mBuffers", AudioBuffer * 1),
    ]


class AudioStreamBasicDescription(ctypes.Structure):
    """Core Audio stream format description."""

    _fields_ = [
        ("mSampleRate", ctypes.c_double),
        ("mFormatID", ctypes.c_uint32),
        ("mFormatFlags", ctypes.c_uint32),
        ("mBytesPerPacket", ctypes.c_uint32),
        ("mFramesPerPacket", ctypes.c_uint32),
        ("mBytesPerFrame", ctypes.c_uint32),
        ("mChannelsPerFrame", ctypes.c_uint32),
        ("mBitsPerChannel", ctypes.c_uint32),
        ("mReserved", ctypes.c_uint32),
    ]


if TYPE_CHECKING:
    type AudioTimeStampPtr = ctypes._Pointer[AudioTimeStamp]
    type AudioBufferListPtr = ctypes._Pointer[AudioBufferList]
else:
    AudioTimeStampPtr = ctypes.c_void_p
    AudioBufferListPtr = ctypes.c_void_p


# Format flags
kAudioFormatFlagIsFloat = 1 << 0

# Tap property selectors
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
        """
        self.tap_id = tap_id
        self.output_path = Path(output_path) if output_path else None
        self._on_data = on_data

        self._aggregate_device_id: int | None = None

        self._io_proc_id: ctypes.c_void_p | None = None
        self._is_recording = False
        self._max_pending_buffers = _validate_max_pending_buffers(max_pending_buffers)
        self._worker_thread: threading.Thread | None = None
        self._work_queue: queue.Queue[_WorkerItem] | None = None
        # Buffer pool recycled between the Core Audio callback and the worker.
        # RT thread pops; worker appends. CPython's deque makes single-item ops
        # atomic, so the SPSC path needs no explicit lock.
        self._buffer_pool: deque[_PoolBuffer] | None = None
        self._writer_error: OSError | None = None
        self._callback_error: RuntimeError | None = None
        self._output_file: BinaryIO | None = None
        self._wav_file: wave.Wave_write | None = None

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

                    buf = self._acquire_pool_buffer(byte_count)
                    if buf is None:
                        # Only this RT thread writes these counters, and the
                        # ``frames_recorded`` reader tolerates a momentary
                        # stale value, so the increments skip the mutex the
                        # old code used.
                        self._dropped_buffers += 1
                        self._dropped_frames += num_frames
                        continue

                    ctypes.memmove(buf, buffer.mData, byte_count)

                    if self._enqueue_audio_data(buf, num_frames, byte_count):
                        self._total_frames += num_frames

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
        if self._is_recording:
            raise RuntimeError("Already recording")

        tap_uid = _get_tap_uid(self.tap_id)

        try:
            asbd = get_tap_format(self.tap_id)
            self._sample_rate = asbd.mSampleRate
            self._num_channels = asbd.mChannelsPerFrame
            self._bits_per_sample = asbd.mBitsPerChannel
            self._is_float = bool(asbd.mFormatFlags & kAudioFormatFlagIsFloat)
        except OSError:
            # Use defaults if we can't get format.
            pass

        self._output_bits_per_sample = (
            16
            if self._is_float and self._bits_per_sample == 32
            else self._bits_per_sample
        )
        self._convert_float_output = self._is_float and self._bits_per_sample == 32

        if self.output_path is not None or self._on_data is not None:
            self._start_worker()

        try:
            self._aggregate_device_id = _create_aggregate_device_for_tap(
                tap_uid, "catap Recording Device"
            )
        except Exception:
            self._stop_worker()
            raise

        io_proc_id = ctypes.c_void_p()
        status = _AudioDeviceCreateIOProcID(
            self._aggregate_device_id,
            self._callback,
            None,
            ctypes.byref(io_proc_id),
        )

        if status != 0:
            cleanup_errors: list[OSError | RuntimeError] = []
            try:
                _destroy_aggregate_device(self._aggregate_device_id)
            except OSError as exc:
                cleanup_errors.append(exc)

            try:
                self._stop_worker()
            except (OSError, RuntimeError) as exc:
                cleanup_errors.append(exc)

            self._aggregate_device_id = None
            error = OSError(f"Failed to create IO proc: status {status}")
            for cleanup_error in cleanup_errors:
                error.add_note(str(cleanup_error))
            raise error

        self._io_proc_id = io_proc_id

        self._total_frames = 0

        self._is_recording = True
        status = _AudioDeviceStart(self._aggregate_device_id, self._io_proc_id)

        if status != 0:
            cleanup_errors: list[OSError | RuntimeError] = []
            self._is_recording = False
            destroy_status = _AudioDeviceDestroyIOProcID(
                self._aggregate_device_id, self._io_proc_id
            )
            if destroy_status != 0:
                cleanup_errors.append(
                    OSError(f"Failed to destroy IO proc: status {destroy_status}")
                )
            self._io_proc_id = None

            try:
                _destroy_aggregate_device(self._aggregate_device_id)
            except OSError as exc:
                cleanup_errors.append(exc)

            try:
                self._stop_worker()
            except (OSError, RuntimeError) as exc:
                cleanup_errors.append(exc)

            self._aggregate_device_id = None
            error = OSError(f"Failed to start audio device: status {status}")
            for cleanup_error in cleanup_errors:
                error.add_note(str(cleanup_error))
            raise error

    def stop(self) -> None:
        """Stop recording and finalize any WAV output.

        Raises:
            OSError: If Core Audio cleanup fails
            RuntimeError: If not recording
        """
        if not self._is_recording:
            raise RuntimeError("Not recording")

        self._is_recording = False
        cleanup_errors: list[OSError | RuntimeError] = []

        if self._io_proc_id and self._aggregate_device_id:
            stop_status = _AudioDeviceStop(self._aggregate_device_id, self._io_proc_id)
            if stop_status != 0:
                cleanup_errors.append(
                    OSError(f"Failed to stop audio device: status {stop_status}")
                )

            destroy_status = _AudioDeviceDestroyIOProcID(
                self._aggregate_device_id, self._io_proc_id
            )
            if destroy_status != 0:
                cleanup_errors.append(
                    OSError(f"Failed to destroy IO proc: status {destroy_status}")
                )

            self._io_proc_id = None

        if self._aggregate_device_id:
            try:
                _destroy_aggregate_device(self._aggregate_device_id)
            except OSError as exc:
                cleanup_errors.append(exc)
            self._aggregate_device_id = None

        try:
            self._stop_worker()
        except (OSError, RuntimeError) as exc:
            cleanup_errors.append(exc)

        if cleanup_errors:
            raise _combine_errors("Failed to stop recording cleanly", cleanup_errors)

    def _start_worker(self) -> None:
        """Start the background worker for file writes and user callbacks."""
        if self.output_path is None and self._on_data is None:
            return

        self._writer_error = None
        self._callback_error = None
        self._dropped_buffers = 0
        self._dropped_frames = 0

        if self.output_path is not None:
            output_file = self.output_path.open("wb")
            wav_file: wave.Wave_write | None = None
            try:
                # The wave file outlives this function; it is closed by the
                # worker thread's finally block in _worker_loop.
                wav_file = wave.open(output_file, "wb")  # noqa: SIM115
                wav_file.setnchannels(self._num_channels)
                wav_file.setsampwidth(self._output_bits_per_sample // 8)
                wav_file.setframerate(int(self._sample_rate))
            except Exception:
                if wav_file is not None:
                    with contextlib.suppress(Exception):
                        wav_file.close()
                output_file.close()
                raise
            self._output_file = output_file
            self._wav_file = wav_file
        else:
            self._output_file = None
            self._wav_file = None

        bytes_per_frame = max(
            1, self._num_channels * (self._bits_per_sample // 8)
        )
        pool_buffer_size = max(_DEFAULT_POOL_BUFFER_SIZE, bytes_per_frame * 1024)
        pool_type = ctypes.c_char * pool_buffer_size
        self._buffer_pool = deque(
            pool_type() for _ in range(self._max_pending_buffers)
        )

        self._work_queue = queue.Queue(maxsize=self._max_pending_buffers)
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="catap-audio-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def _acquire_pool_buffer(self, needed: int) -> _PoolBuffer | None:
        """Return a ctypes buffer sized for ``needed`` bytes, or None if exhausted.

        Called from the Core Audio real-time thread. In steady state the pool
        is non-empty and buffers are already large enough, so the hot path
        skips both the allocator and any lock.
        """
        pool = self._buffer_pool
        if pool is None:
            return None
        try:
            buf = pool.pop()
        except IndexError:
            return None
        if len(buf) < needed:
            # Resize is rare (only on buffer-size change). Still technically an
            # allocation on the RT thread, but bounded to a few occurrences.
            buf = (ctypes.c_char * needed)()
        return buf

    def _release_pool_buffer(self, buf: _PoolBuffer) -> None:
        """Return a buffer to the pool. Safe to call after _stop_worker."""
        pool = self._buffer_pool
        if pool is not None:
            pool.append(buf)

    def _enqueue_audio_data(
        self, buf: _PoolBuffer, num_frames: int, byte_count: int
    ) -> bool:
        """Queue audio work without blocking the Core Audio callback thread."""
        if self._work_queue is None:
            self._release_pool_buffer(buf)
            return True

        try:
            self._work_queue.put_nowait((buf, num_frames, byte_count))
        except queue.Full:
            self._dropped_buffers += 1
            self._dropped_frames += num_frames
            self._release_pool_buffer(buf)
            return False

        return True

    def _stop_worker(self) -> None:
        """Flush and stop the background worker."""
        if self._work_queue is not None:
            self._work_queue.put(None)

        if self._worker_thread is not None:
            self._worker_thread.join()

        self._work_queue = None
        self._worker_thread = None
        self._buffer_pool = None

        worker_errors: list[OSError | RuntimeError] = []
        if self._writer_error is not None:
            worker_errors.append(self._writer_error)
            self._writer_error = None
        if self._callback_error is not None:
            worker_errors.append(self._callback_error)
            self._callback_error = None
        # The RT thread has already been stopped via ``_AudioDeviceStop`` by
        # the time this runs, so the dropped counters are no longer mutated
        # concurrently.
        dropped_buffers = self._dropped_buffers
        dropped_frames = self._dropped_frames
        self._dropped_buffers = 0
        self._dropped_frames = 0
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

    def _worker_loop(self) -> None:
        """Drain queued audio outside the Core Audio callback thread."""
        assert self._work_queue is not None

        try:
            while True:
                item = self._work_queue.get()
                if item is None:
                    break

                buf, num_frames, byte_count = item

                try:
                    if self._on_data is not None and self._callback_error is None:
                        # User may stash the buffer, so hand them a private copy
                        # rather than a pool-owned view.
                        try:
                            self._on_data(
                                bytes(memoryview(buf)[:byte_count]), num_frames
                            )
                        except Exception as exc:
                            self._callback_error = RuntimeError(
                                f"Audio data callback failed: {exc}"
                            )

                    if self._wav_file is not None and self._writer_error is None:
                        try:
                            if self._convert_float_output:
                                output_data = _float32_to_int16(buf, byte_count)
                            else:
                                output_data = memoryview(buf)[:byte_count]
                            self._wav_file.writeframesraw(output_data)
                        except Exception as exc:
                            self._writer_error = OSError(
                                f"Failed to write WAV data: {exc}"
                            )
                finally:
                    self._release_pool_buffer(buf)
        finally:
            if self._wav_file is not None:
                try:
                    self._wav_file.close()
                except Exception as exc:
                    if self._writer_error is None:
                        self._writer_error = OSError(
                            f"Failed to finalize WAV file: {exc}"
                        )
                finally:
                    self._wav_file = None
            if self._output_file is not None:
                try:
                    self._output_file.close()
                except Exception as exc:
                    if self._writer_error is None:
                        self._writer_error = OSError(
                            f"Failed to close output file: {exc}"
                        )
                finally:
                    self._output_file = None

    @property
    def is_recording(self) -> bool:
        """True if currently recording."""
        return self._is_recording

    @property
    def frames_recorded(self) -> int:
        """Number of audio frames recorded so far.

        A single attribute load is atomic under CPython's GIL, so no mutex is
        needed to read the counter the RT callback increments.
        """
        return self._total_frames

    @property
    def duration_seconds(self) -> float:
        """Duration of recorded audio in seconds."""
        return self._total_frames / self._sample_rate

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
