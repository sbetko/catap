"""Recorder behavior tests."""

from __future__ import annotations

import struct
import wave

from catap.core.recorder import AudioRecorder


def test_writer_streams_float_audio_to_wav(tmp_path) -> None:
    output_path = tmp_path / "recording.wav"
    recorder = AudioRecorder(123, output_path)
    recorder._sample_rate = 48_000
    recorder._num_channels = 2
    recorder._bits_per_sample = 32
    recorder._is_float = True
    recorder._output_bits_per_sample = 16
    recorder._convert_float_output = True

    recorder._start_writer()
    assert recorder._write_queue is not None
    recorder._write_queue.put(struct.pack("<4f", 0.5, -0.5, 1.0, -1.0))
    recorder._stop_writer()

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getframerate() == 48_000
        assert wav_file.getnchannels() == 2
        assert wav_file.getsampwidth() == 2
        samples = struct.unpack("<4h", wav_file.readframes(2))

    assert samples == (16383, -16383, 32767, -32767)


def test_writer_preserves_int16_audio(tmp_path) -> None:
    output_path = tmp_path / "recording.wav"
    recorder = AudioRecorder(123, output_path)
    recorder._sample_rate = 44_100
    recorder._num_channels = 1
    recorder._bits_per_sample = 16
    recorder._is_float = False
    recorder._output_bits_per_sample = 16
    recorder._convert_float_output = False

    recorder._start_writer()
    assert recorder._write_queue is not None
    recorder._write_queue.put(struct.pack("<3h", 100, -200, 300))
    recorder._stop_writer()

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getframerate() == 44_100
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        samples = struct.unpack("<3h", wav_file.readframes(3))

    assert samples == (100, -200, 300)
