"""AudioToolbox helper behavior tests."""

from __future__ import annotations

import ctypes
import struct
import wave

from catap.bindings._audiotoolbox import (
    ExtAudioFileWavWriter,
    PcmAudioConverter,
    make_linear_pcm_asbd,
)


def test_audio_converter_rounds_to_core_audio_pcm_contract() -> None:
    source_format = make_linear_pcm_asbd(48_000, 2, 32, is_float=True)
    destination_format = make_linear_pcm_asbd(48_000, 2, 16, is_float=False)
    data = struct.pack("<4f", 0.5, -0.5, 1.0, -1.0)
    buffer = (ctypes.c_char * len(data)).from_buffer_copy(data)

    with PcmAudioConverter(source_format, destination_format) as converter:
        output_size = converter.convert(buffer, len(data))
        assert output_size == 8
        samples = struct.unpack("<4h", converter.output_bytes())

    assert samples == (16384, -16384, 32767, -32768)


def test_ext_audio_file_writer_writes_pcm_wav(tmp_path) -> None:
    output_path = tmp_path / "core-audio.wav"
    data = struct.pack("<4f", 0.5, -0.5, 1.0, -1.0)
    buffer = (ctypes.c_char * len(data)).from_buffer_copy(data)

    with ExtAudioFileWavWriter(
        output_path,
        sample_rate=48_000,
        num_channels=2,
        client_bits_per_sample=32,
        client_is_float=True,
    ) as writer:
        writer.write(buffer, 2, len(data))

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getframerate() == 48_000
        assert wav_file.getnchannels() == 2
        assert wav_file.getsampwidth() == 2
        samples = struct.unpack("<4h", wav_file.readframes(2))

    assert samples == (16384, -16384, 32767, -32768)
