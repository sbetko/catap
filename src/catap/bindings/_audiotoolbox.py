"""Minimal AudioToolbox bindings for PCM conversion and WAV experiments."""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any

from Foundation import NSURL  # ty: ignore[unresolved-import]

_AudioToolbox = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox"
)


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


class AudioBuffer(ctypes.Structure):
    """Single audio buffer within an AudioBufferList."""

    _fields_ = [
        ("mNumberChannels", ctypes.c_uint32),
        ("mDataByteSize", ctypes.c_uint32),
        ("mData", ctypes.c_void_p),
    ]


class AudioBufferList(ctypes.Structure):
    """Single-buffer AudioBufferList used for interleaved PCM I/O."""

    _fields_ = [
        ("mNumberBuffers", ctypes.c_uint32),
        ("mBuffers", AudioBuffer * 1),
    ]


kAudioFormatLinearPCM = int.from_bytes(b"lpcm", "big")
kAudioFileWAVEType = int.from_bytes(b"WAVE", "big")
kAudioFormatFlagIsFloat = 1 << 0
kAudioFormatFlagIsSignedInteger = 1 << 2
kAudioFormatFlagIsPacked = 1 << 3
kExtAudioFileProperty_ClientDataFormat = int.from_bytes(b"cfmt", "big")

_EMPTY_VIEW = memoryview(b"")

_AudioConverterNew = _AudioToolbox.AudioConverterNew
_AudioConverterNew.argtypes = [
    ctypes.POINTER(AudioStreamBasicDescription),
    ctypes.POINTER(AudioStreamBasicDescription),
    ctypes.POINTER(ctypes.c_void_p),
]
_AudioConverterNew.restype = ctypes.c_int32

_AudioConverterConvertBuffer = _AudioToolbox.AudioConverterConvertBuffer
_AudioConverterConvertBuffer.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_void_p,
]
_AudioConverterConvertBuffer.restype = ctypes.c_int32

_AudioConverterDispose = _AudioToolbox.AudioConverterDispose
_AudioConverterDispose.argtypes = [ctypes.c_void_p]
_AudioConverterDispose.restype = ctypes.c_int32

_ExtAudioFileCreateWithURL = _AudioToolbox.ExtAudioFileCreateWithURL
_ExtAudioFileCreateWithURL.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.POINTER(AudioStreamBasicDescription),
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_void_p),
]
_ExtAudioFileCreateWithURL.restype = ctypes.c_int32

_ExtAudioFileSetProperty = _AudioToolbox.ExtAudioFileSetProperty
_ExtAudioFileSetProperty.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
]
_ExtAudioFileSetProperty.restype = ctypes.c_int32

_ExtAudioFileWrite = _AudioToolbox.ExtAudioFileWrite
_ExtAudioFileWrite.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.POINTER(AudioBufferList),
]
_ExtAudioFileWrite.restype = ctypes.c_int32

_ExtAudioFileDispose = _AudioToolbox.ExtAudioFileDispose
_ExtAudioFileDispose.argtypes = [ctypes.c_void_p]
_ExtAudioFileDispose.restype = ctypes.c_int32


def _check_status(status: int, action: str) -> None:
    """Raise OSError when an AudioToolbox call fails."""
    if status != 0:
        raise OSError(f"{action}: status {status}")


def make_linear_pcm_asbd(
    sample_rate: float,
    num_channels: int,
    bits_per_sample: int,
    *,
    is_float: bool,
) -> AudioStreamBasicDescription:
    """Create an interleaved linear PCM AudioStreamBasicDescription."""
    bytes_per_sample = bits_per_sample // 8
    flags = kAudioFormatFlagIsPacked | (
        kAudioFormatFlagIsFloat
        if is_float
        else kAudioFormatFlagIsSignedInteger
    )
    bytes_per_frame = num_channels * bytes_per_sample
    return AudioStreamBasicDescription(
        sample_rate,
        kAudioFormatLinearPCM,
        flags,
        bytes_per_frame,
        1,
        bytes_per_frame,
        num_channels,
        bits_per_sample,
        0,
    )


def _coerce_input_buffer(
    data: Any, byte_count: int
) -> tuple[Any, ctypes.Array[ctypes.c_char] | None]:
    """Return a ctypes-friendly buffer plus an optional temporary owner."""
    if isinstance(data, bytes):
        temp = (ctypes.c_char * byte_count).from_buffer_copy(data[:byte_count])
        return temp, temp
    if isinstance(data, bytearray):
        temp = (ctypes.c_char * byte_count).from_buffer_copy(bytes(data[:byte_count]))
        return temp, temp
    if isinstance(data, memoryview):
        temp = (ctypes.c_char * byte_count).from_buffer_copy(data[:byte_count])
        return temp, temp
    return data, None


class PcmAudioConverter:
    """Reusable AudioConverter for fixed-ratio PCM conversions."""

    def __init__(
        self,
        source_format: AudioStreamBasicDescription,
        destination_format: AudioStreamBasicDescription,
    ) -> None:
        self._source_format = source_format
        self._destination_format = destination_format
        self._converter = ctypes.c_void_p()
        _check_status(
            _AudioConverterNew(
                ctypes.byref(self._source_format),
                ctypes.byref(self._destination_format),
                ctypes.byref(self._converter),
            ),
            "Failed to create AudioConverter",
        )
        self._source_bytes_per_frame = max(1, source_format.mBytesPerFrame)
        self._destination_bytes_per_frame = max(1, destination_format.mBytesPerFrame)
        self._output_capacity = 0
        self._output_buffer: ctypes.Array[ctypes.c_ubyte] | None = None
        self._last_output_size = 0

    def _ensure_output_capacity(self, needed: int) -> ctypes.Array[ctypes.c_ubyte]:
        if self._output_buffer is None or needed > self._output_capacity:
            self._output_buffer = (ctypes.c_ubyte * needed)()
            self._output_capacity = needed
        return self._output_buffer

    def convert(self, data: Any, size: int | None = None) -> int:
        """Convert one buffer and retain the result in an internal output buffer."""
        byte_count = len(data) if size is None else size
        if byte_count <= 0:
            self._last_output_size = 0
            return 0

        input_frames = byte_count // self._source_bytes_per_frame
        output_capacity = input_frames * self._destination_bytes_per_frame
        output_buffer = self._ensure_output_capacity(output_capacity)
        input_buffer, _ = _coerce_input_buffer(data, byte_count)
        output_size = ctypes.c_uint32(output_capacity)

        _check_status(
            _AudioConverterConvertBuffer(
                self._converter,
                byte_count,
                ctypes.cast(input_buffer, ctypes.c_void_p),
                ctypes.byref(output_size),
                ctypes.cast(output_buffer, ctypes.c_void_p),
            ),
            "Failed to convert PCM buffer",
        )
        self._last_output_size = output_size.value
        return self._last_output_size

    def output_view(self) -> memoryview:
        """Return a zero-copy view of the most recent conversion output."""
        if self._output_buffer is None or self._last_output_size == 0:
            return _EMPTY_VIEW
        return memoryview(self._output_buffer)[: self._last_output_size]

    def output_bytes(self) -> bytes:
        """Return the most recent conversion output as an owned bytes object."""
        return self.output_view().tobytes()

    @property
    def last_output_size(self) -> int:
        """Size in bytes of the most recent conversion output."""
        return self._last_output_size

    def close(self) -> None:
        """Dispose the underlying converter."""
        if self._converter is not None:
            status = _AudioConverterDispose(self._converter)
            self._converter = None
            _check_status(status, "Failed to dispose AudioConverter")

    def __enter__(self) -> PcmAudioConverter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.close()


class ExtAudioFileWavWriter:
    """Write interleaved PCM buffers to a WAV file via ExtAudioFile."""

    def __init__(
        self,
        output_path: str | Path,
        *,
        sample_rate: float,
        num_channels: int,
        client_bits_per_sample: int,
        client_is_float: bool,
        file_bits_per_sample: int = 16,
    ) -> None:
        self.output_path = Path(output_path)
        self._ext_audio_file = ctypes.c_void_p()
        self._client_format = make_linear_pcm_asbd(
            sample_rate,
            num_channels,
            client_bits_per_sample,
            is_float=client_is_float,
        )
        self._file_format = make_linear_pcm_asbd(
            sample_rate,
            num_channels,
            file_bits_per_sample,
            is_float=False,
        )
        self._buffer_list = AudioBufferList()
        self._buffer_list.mNumberBuffers = 1
        self._buffer_list.mBuffers[0].mNumberChannels = num_channels

        url = NSURL.fileURLWithPath_(str(self.output_path.resolve()))
        _check_status(
            _ExtAudioFileCreateWithURL(
                url.__c_void_p__(),
                kAudioFileWAVEType,
                ctypes.byref(self._file_format),
                None,
                0,
                ctypes.byref(self._ext_audio_file),
            ),
            "Failed to create ExtAudioFile",
        )
        try:
            _check_status(
                _ExtAudioFileSetProperty(
                    self._ext_audio_file,
                    kExtAudioFileProperty_ClientDataFormat,
                    ctypes.sizeof(AudioStreamBasicDescription),
                    ctypes.byref(self._client_format),
                ),
                "Failed to set ExtAudioFile client format",
            )
        except Exception:
            self.close()
            raise

    def write(self, data: Any, num_frames: int, size: int | None = None) -> None:
        """Write one client-format PCM buffer to the WAV file."""
        byte_count = len(data) if size is None else size
        if byte_count <= 0 or num_frames <= 0:
            return

        input_buffer, _ = _coerce_input_buffer(data, byte_count)
        buffer = self._buffer_list.mBuffers[0]
        buffer.mDataByteSize = byte_count
        buffer.mData = ctypes.cast(input_buffer, ctypes.c_void_p)

        _check_status(
            _ExtAudioFileWrite(
                self._ext_audio_file,
                num_frames,
                ctypes.byref(self._buffer_list),
            ),
            "Failed to write ExtAudioFile buffer",
        )

    def close(self) -> None:
        """Finalize and close the WAV file."""
        if self._ext_audio_file is not None:
            status = _ExtAudioFileDispose(self._ext_audio_file)
            self._ext_audio_file = None
            _check_status(status, "Failed to dispose ExtAudioFile")

    def __enter__(self) -> ExtAudioFileWavWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.close()
