"""Audio recording from Core Audio taps."""
from __future__ import annotations

import ctypes
import struct
import threading
import uuid
import wave
from pathlib import Path
from typing import Callable

from Foundation import NSDictionary, NSNumber, NSString, NSArray

# Load CoreAudio framework
_CoreAudio = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
)


# =============================================================================
# Core Audio Type Definitions
# =============================================================================

class AudioTimeStamp(ctypes.Structure):
    """Core Audio AudioTimeStamp structure."""
    _fields_ = [
        ("mSampleTime", ctypes.c_double),
        ("mHostTime", ctypes.c_uint64),
        ("mRateScalar", ctypes.c_double),
        ("mWordClockTime", ctypes.c_uint64),
        ("mSMPTETime", ctypes.c_uint8 * 24),  # SMPTETime structure
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
    """
    Core Audio AudioBufferList structure.

    Note: This is a variable-length structure. The mBuffers array
    contains mNumberBuffers elements, but we define it with 1 for
    the base structure size.
    """
    _fields_ = [
        ("mNumberBuffers", ctypes.c_uint32),
        ("mBuffers", AudioBuffer * 1),  # Variable length array
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


# Format constants
kAudioFormatLinearPCM = int.from_bytes(b'lpcm', 'big')
kAudioFormatFlagIsFloat = 1 << 0
kAudioFormatFlagIsBigEndian = 1 << 1
kAudioFormatFlagIsPacked = 1 << 3
kAudioFormatFlagIsNonInterleaved = 1 << 5

# Property constants
kAudioObjectPropertyScopeGlobal = int.from_bytes(b'glob', 'big')
kAudioObjectPropertyScopeInput = int.from_bytes(b'inpt', 'big')
kAudioObjectPropertyElementMain = 0
kAudioDevicePropertyStreamFormat = int.from_bytes(b'sfmt', 'big')

# Tap property constants
kAudioTapPropertyUID = int.from_bytes(b'tuid', 'big')
kAudioTapPropertyFormat = int.from_bytes(b'tfmt', 'big')


# =============================================================================
# AudioDeviceIOProc callback type
# =============================================================================

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


# =============================================================================
# Core Audio Function Bindings
# =============================================================================

# Property access functions
_AudioObjectGetPropertyDataSize = _CoreAudio.AudioObjectGetPropertyDataSize
_AudioObjectGetPropertyDataSize.argtypes = [
    ctypes.c_uint32,  # inObjectID
    ctypes.POINTER(ctypes.c_uint32 * 3),  # inAddress
    ctypes.c_uint32,  # inQualifierDataSize
    ctypes.c_void_p,  # inQualifierData
    ctypes.POINTER(ctypes.c_uint32),  # outDataSize
]
_AudioObjectGetPropertyDataSize.restype = ctypes.c_int32

_AudioObjectGetPropertyData = _CoreAudio.AudioObjectGetPropertyData
_AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32,  # inObjectID
    ctypes.POINTER(ctypes.c_uint32 * 3),  # inAddress
    ctypes.c_uint32,  # inQualifierDataSize
    ctypes.c_void_p,  # inQualifierData
    ctypes.POINTER(ctypes.c_uint32),  # ioDataSize
    ctypes.c_void_p,  # outData
]
_AudioObjectGetPropertyData.restype = ctypes.c_int32

# Aggregate device functions
_AudioHardwareCreateAggregateDevice = _CoreAudio.AudioHardwareCreateAggregateDevice
_AudioHardwareCreateAggregateDevice.argtypes = [
    ctypes.c_void_p,  # CFDictionaryRef
    ctypes.POINTER(ctypes.c_uint32),  # AudioObjectID*
]
_AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32

_AudioHardwareDestroyAggregateDevice = _CoreAudio.AudioHardwareDestroyAggregateDevice
_AudioHardwareDestroyAggregateDevice.argtypes = [
    ctypes.c_uint32,  # AudioObjectID
]
_AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32

# IO Proc functions
_AudioDeviceCreateIOProcID = _CoreAudio.AudioDeviceCreateIOProcID
_AudioDeviceCreateIOProcID.argtypes = [
    ctypes.c_uint32,  # AudioObjectID
    AudioDeviceIOProcType,  # AudioDeviceIOProc
    ctypes.c_void_p,  # void* inClientData
    ctypes.POINTER(ctypes.c_void_p),  # AudioDeviceIOProcID*
]
_AudioDeviceCreateIOProcID.restype = ctypes.c_int32

_AudioDeviceDestroyIOProcID = _CoreAudio.AudioDeviceDestroyIOProcID
_AudioDeviceDestroyIOProcID.argtypes = [
    ctypes.c_uint32,  # AudioObjectID
    ctypes.c_void_p,  # AudioDeviceIOProcID
]
_AudioDeviceDestroyIOProcID.restype = ctypes.c_int32

_AudioDeviceStart = _CoreAudio.AudioDeviceStart
_AudioDeviceStart.argtypes = [
    ctypes.c_uint32,  # AudioObjectID
    ctypes.c_void_p,  # AudioDeviceIOProcID
]
_AudioDeviceStart.restype = ctypes.c_int32

_AudioDeviceStop = _CoreAudio.AudioDeviceStop
_AudioDeviceStop.argtypes = [
    ctypes.c_uint32,  # AudioObjectID
    ctypes.c_void_p,  # AudioDeviceIOProcID
]
_AudioDeviceStop.restype = ctypes.c_int32


# =============================================================================
# Helper Functions
# =============================================================================

def _get_tap_uid(tap_id: int) -> str:
    """
    Get the UID string for a tap.

    Args:
        tap_id: AudioObjectID of the tap

    Returns:
        UID string for the tap

    Raises:
        OSError: If UID cannot be retrieved
    """
    address = (ctypes.c_uint32 * 3)(
        kAudioTapPropertyUID,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain
    )

    # Get size - should be pointer size for CFStringRef
    size = ctypes.c_uint32(0)
    status = _AudioObjectGetPropertyDataSize(
        tap_id,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(size)
    )

    if status != 0:
        raise OSError(f"Failed to get tap UID size: status {status}")

    # Get the CFStringRef
    cf_string_ref = ctypes.c_void_p()
    actual_size = ctypes.c_uint32(ctypes.sizeof(cf_string_ref))

    status = _AudioObjectGetPropertyData(
        tap_id,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(actual_size),
        ctypes.byref(cf_string_ref)
    )

    if status != 0:
        raise OSError(f"Failed to get tap UID: status {status}")

    # Convert CFStringRef to Python string using PyObjC
    import objc
    ns_string = objc.objc_object(c_void_p=cf_string_ref)
    return str(ns_string)


def get_tap_format(tap_id: int) -> AudioStreamBasicDescription:
    """
    Get the audio format for a tap.

    Args:
        tap_id: AudioObjectID of the tap

    Returns:
        AudioStreamBasicDescription with format info

    Raises:
        OSError: If format cannot be retrieved
    """
    address = (ctypes.c_uint32 * 3)(
        kAudioTapPropertyFormat,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain
    )

    asbd = AudioStreamBasicDescription()
    size = ctypes.c_uint32(ctypes.sizeof(asbd))

    status = _AudioObjectGetPropertyData(
        tap_id,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(asbd)
    )

    if status != 0:
        raise OSError(f"Failed to get tap format: status {status}")

    return asbd


def _create_aggregate_device_for_tap(tap_uid: str, name: str) -> int:
    """
    Create an aggregate device that includes the specified tap.

    Args:
        tap_uid: UID of the tap to include
        name: Name for the aggregate device

    Returns:
        AudioObjectID of the created aggregate device

    Raises:
        OSError: If device creation fails
    """
    # Create unique UID for aggregate device
    agg_uid = f"io.github.catap.aggregate.{uuid.uuid4()}"

    # Build the tap list with the tap UID
    tap_entry = NSDictionary.dictionaryWithDictionary_({
        "uid": tap_uid,
        "drift": NSNumber.numberWithBool_(True),
    })
    tap_list = NSArray.arrayWithObject_(tap_entry)

    # Build aggregate device description
    description = NSDictionary.dictionaryWithDictionary_({
        "name": name,
        "uid": agg_uid,
        "private": NSNumber.numberWithBool_(True),
        "taps": tap_list,
        "tapautostart": NSNumber.numberWithBool_(False),
    })

    # Get the CFDictionary pointer
    import objc
    cf_dict_ptr = description.__c_void_p__()

    # Create the aggregate device
    device_id = ctypes.c_uint32(0)
    status = _AudioHardwareCreateAggregateDevice(
        cf_dict_ptr,
        ctypes.byref(device_id)
    )

    if status != 0:
        raise OSError(f"Failed to create aggregate device: status {status}")

    return device_id.value


def _destroy_aggregate_device(device_id: int) -> None:
    """
    Destroy an aggregate device.

    Args:
        device_id: AudioObjectID of the device to destroy

    Raises:
        OSError: If destruction fails
    """
    status = _AudioHardwareDestroyAggregateDevice(device_id)
    if status != 0:
        raise OSError(f"Failed to destroy aggregate device: status {status}")


# =============================================================================
# AudioRecorder Class
# =============================================================================

class AudioRecorder:
    """
    Records audio from a Core Audio tap to a WAV file.

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
        output_path: str | Path,
        on_data: Callable[[bytes, int], None] | None = None,
    ) -> None:
        """
        Initialize the recorder.

        Args:
            tap_id: AudioObjectID of the tap to record from
            output_path: Path to write the WAV file
            on_data: Optional callback for each audio buffer (bytes, num_frames)
        """
        self.tap_id = tap_id
        self.output_path = Path(output_path)
        self._on_data = on_data

        # Aggregate device (created on start)
        self._aggregate_device_id: int | None = None

        # Recording state
        self._io_proc_id: ctypes.c_void_p | None = None
        self._is_recording = False
        self._lock = threading.Lock()

        # Audio data accumulator
        self._audio_chunks: list[bytes] = []
        self._total_frames = 0

        # Stream format (populated on start)
        self._sample_rate = 44100.0
        self._num_channels = 2
        self._bits_per_sample = 32
        self._is_float = True

        # Keep reference to callback to prevent garbage collection
        self._callback = AudioDeviceIOProcType(self._io_proc)

    def _io_proc(
        self,
        device: int,
        now: ctypes.POINTER(AudioTimeStamp),
        input_data: ctypes.POINTER(AudioBufferList),
        input_time: ctypes.POINTER(AudioTimeStamp),
        output_data: ctypes.POINTER(AudioBufferList),
        output_time: ctypes.POINTER(AudioTimeStamp),
        client_data: ctypes.c_void_p,
    ) -> int:
        """
        Audio I/O callback - called on the audio thread.

        This is called by Core Audio when audio data is available.
        We copy the input data and return immediately to avoid blocking.
        """
        if not self._is_recording:
            return 0

        try:
            if not input_data:
                return 0

            buffer_list = input_data.contents
            num_buffers = buffer_list.mNumberBuffers

            if num_buffers == 0:
                return 0

            # Process all buffers (usually just one for stereo mixdown)
            for i in range(num_buffers):
                # Access buffer at index i
                # The AudioBufferList has a variable-length array, so we need
                # to calculate the offset manually
                buffer_offset = ctypes.sizeof(AudioBuffer) * i
                buffer_ptr = ctypes.cast(
                    ctypes.addressof(buffer_list.mBuffers) + buffer_offset,
                    ctypes.POINTER(AudioBuffer)
                )
                buffer = buffer_ptr.contents

                if buffer.mData and buffer.mDataByteSize > 0:
                    # Copy audio data from the buffer
                    data = ctypes.string_at(buffer.mData, buffer.mDataByteSize)

                    # Calculate number of frames
                    bytes_per_frame = buffer.mNumberChannels * (self._bits_per_sample // 8)
                    if bytes_per_frame > 0:
                        num_frames = buffer.mDataByteSize // bytes_per_frame
                    else:
                        num_frames = 0

                    with self._lock:
                        self._audio_chunks.append(data)
                        self._total_frames += num_frames

                    # Call user callback if provided
                    if self._on_data:
                        self._on_data(data, num_frames)

        except Exception:
            # Must not raise from callback
            pass

        return 0  # noErr

    def start(self) -> None:
        """
        Start recording audio.

        Raises:
            OSError: If recording cannot be started
            RuntimeError: If already recording
        """
        if self._is_recording:
            raise RuntimeError("Already recording")

        # Get tap UID
        tap_uid = _get_tap_uid(self.tap_id)

        # Get stream format from tap
        try:
            asbd = get_tap_format(self.tap_id)
            self._sample_rate = asbd.mSampleRate
            self._num_channels = asbd.mChannelsPerFrame
            self._bits_per_sample = asbd.mBitsPerChannel
            self._is_float = bool(asbd.mFormatFlags & kAudioFormatFlagIsFloat)
        except OSError:
            # Use defaults if we can't get format
            pass

        # Create aggregate device containing the tap
        self._aggregate_device_id = _create_aggregate_device_for_tap(
            tap_uid, "catap Recording Device"
        )

        # Create IO proc on the aggregate device
        io_proc_id = ctypes.c_void_p()
        status = _AudioDeviceCreateIOProcID(
            self._aggregate_device_id,
            self._callback,
            None,  # client data
            ctypes.byref(io_proc_id)
        )

        if status != 0:
            # Clean up aggregate device
            try:
                _destroy_aggregate_device(self._aggregate_device_id)
            except OSError:
                pass
            self._aggregate_device_id = None
            raise OSError(f"Failed to create IO proc: status {status}")

        self._io_proc_id = io_proc_id

        # Clear any previous data
        with self._lock:
            self._audio_chunks.clear()
            self._total_frames = 0

        # Start device
        self._is_recording = True
        status = _AudioDeviceStart(self._aggregate_device_id, self._io_proc_id)

        if status != 0:
            self._is_recording = False
            _AudioDeviceDestroyIOProcID(self._aggregate_device_id, self._io_proc_id)
            self._io_proc_id = None
            try:
                _destroy_aggregate_device(self._aggregate_device_id)
            except OSError:
                pass
            self._aggregate_device_id = None
            raise OSError(f"Failed to start audio device: status {status}")

    def stop(self) -> None:
        """
        Stop recording and write the WAV file.

        Raises:
            RuntimeError: If not recording
        """
        if not self._is_recording:
            raise RuntimeError("Not recording")

        self._is_recording = False

        # Stop device
        if self._io_proc_id and self._aggregate_device_id:
            _AudioDeviceStop(self._aggregate_device_id, self._io_proc_id)
            _AudioDeviceDestroyIOProcID(self._aggregate_device_id, self._io_proc_id)
            self._io_proc_id = None

        # Destroy aggregate device
        if self._aggregate_device_id:
            try:
                _destroy_aggregate_device(self._aggregate_device_id)
            except OSError:
                pass
            self._aggregate_device_id = None

        # Write WAV file
        self._write_wav()

    def _write_wav(self) -> None:
        """Write accumulated audio data to WAV file."""
        with self._lock:
            if not self._audio_chunks:
                # Write empty WAV file
                audio_data = b''
            else:
                audio_data = b''.join(self._audio_chunks)

        # Convert float32 to int16 for WAV compatibility
        if self._is_float and self._bits_per_sample == 32:
            audio_data = self._float32_to_int16(audio_data)
            output_bits = 16
        else:
            output_bits = self._bits_per_sample

        # Write WAV file
        with wave.open(str(self.output_path), 'wb') as wav:
            wav.setnchannels(self._num_channels)
            wav.setsampwidth(output_bits // 8)
            wav.setframerate(int(self._sample_rate))
            wav.writeframes(audio_data)

    def _float32_to_int16(self, data: bytes) -> bytes:
        """Convert 32-bit float audio to 16-bit integer."""
        # Parse as floats
        num_samples = len(data) // 4
        floats = struct.unpack(f'<{num_samples}f', data)

        # Convert to int16 with clipping
        int16_samples = []
        for f in floats:
            # Clip to [-1.0, 1.0]
            f = max(-1.0, min(1.0, f))
            # Scale to int16 range
            int16_samples.append(int(f * 32767))

        return struct.pack(f'<{num_samples}h', *int16_samples)

    @property
    def is_recording(self) -> bool:
        """True if currently recording."""
        return self._is_recording

    @property
    def frames_recorded(self) -> int:
        """Number of audio frames recorded so far."""
        with self._lock:
            return self._total_frames

    @property
    def duration_seconds(self) -> float:
        """Duration of recorded audio in seconds."""
        return self._total_frames / self._sample_rate
