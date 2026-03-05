"""Streaming utilities for audio output."""

from __future__ import annotations

import struct
import sys
from typing import BinaryIO, Literal

StreamFormat = Literal["f32le", "s16le", "wav"]


def write_wav_header(
    output: BinaryIO,
    sample_rate: int,
    num_channels: int,
    bits_per_sample: int = 16,
    data_size: int = 0x7FFFFFFF,
) -> None:
    """
    Write a WAV header to the output stream.

    For streaming with unknown length, data_size defaults to max int32.
    Some players handle this gracefully for streaming.

    Args:
        output: Binary output stream
        sample_rate: Sample rate in Hz
        num_channels: Number of audio channels
        bits_per_sample: Bits per sample (default 16)
        data_size: Size of audio data in bytes (default max for streaming)
    """
    byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
    block_align = num_channels * (bits_per_sample // 8)

    # RIFF header
    output.write(b"RIFF")
    output.write(struct.pack("<I", data_size + 36))  # File size - 8
    output.write(b"WAVE")

    # fmt chunk
    output.write(b"fmt ")
    output.write(struct.pack("<I", 16))  # Chunk size
    output.write(struct.pack("<H", 1))  # Audio format (1 = PCM)
    output.write(struct.pack("<H", num_channels))
    output.write(struct.pack("<I", sample_rate))
    output.write(struct.pack("<I", byte_rate))
    output.write(struct.pack("<H", block_align))
    output.write(struct.pack("<H", bits_per_sample))

    # data chunk header
    output.write(b"data")
    output.write(struct.pack("<I", data_size))


def float32_to_int16(data: bytes) -> bytes:
    """
    Convert float32 PCM audio to int16 PCM.

    Args:
        data: Raw float32 audio data (little-endian)

    Returns:
        Raw int16 audio data (little-endian)
    """
    num_samples = len(data) // 4
    floats = struct.unpack(f"<{num_samples}f", data)

    # Convert to int16 with clipping
    int16_samples = []
    for f in floats:
        # Clip to [-1.0, 1.0]
        f = max(-1.0, min(1.0, f))
        # Scale to int16 range
        int16_samples.append(int(f * 32767))

    return struct.pack(f"<{num_samples}h", *int16_samples)


class AudioStreamer:
    """
    Streams audio data to stdout in various formats.

    Use as the on_data callback for AudioRecorder to stream audio
    in real-time instead of accumulating to a file.

    Usage:
        streamer = AudioStreamer(format="s16le", sample_rate=44100, num_channels=2)
        recorder = AudioRecorder(tap_id, output_path=None, on_data=streamer.write)
        recorder.start()
        # Audio streams to stdout...
        recorder.stop()

    Formats:
        - f32le: Raw 32-bit float little-endian (native Core Audio format)
        - s16le: Raw 16-bit signed integer little-endian
        - wav: WAV header followed by s16le data
    """

    def __init__(
        self,
        format: StreamFormat = "f32le",
        sample_rate: int = 44100,
        num_channels: int = 2,
        output: BinaryIO | None = None,
    ) -> None:
        """
        Initialize the audio streamer.

        Args:
            format: Output format (f32le, s16le, or wav)
            sample_rate: Sample rate in Hz (used for WAV header)
            num_channels: Number of audio channels (used for WAV header)
            output: Output stream (default: stdout binary)
        """
        self.format = format
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self._output = output or sys.stdout.buffer
        self._header_written = False

    def write(self, data: bytes, num_frames: int) -> None:
        """
        Write audio chunk to output stream.

        Called as the on_data callback from AudioRecorder.

        Args:
            data: Raw audio data (float32)
            num_frames: Number of audio frames in data
        """
        # Write WAV header on first chunk if needed
        if self.format == "wav" and not self._header_written:
            write_wav_header(
                self._output,
                self.sample_rate,
                self.num_channels,
                bits_per_sample=16,
            )
            self._header_written = True

        # Convert format if needed
        if self.format in ("s16le", "wav"):
            data = float32_to_int16(data)

        # Write to output
        try:
            self._output.write(data)
            self._output.flush()
        except BrokenPipeError:
            # Consumer closed the pipe, stop gracefully
            pass
