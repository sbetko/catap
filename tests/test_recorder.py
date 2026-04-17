"""Recorder behavior tests."""

from __future__ import annotations

import ctypes
import queue
import struct
import threading
import wave

import pytest

import catap.core.recorder as recorder_module
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

    recorder._start_worker()
    assert recorder._work_queue is not None
    recorder._work_queue.put((struct.pack("<4f", 0.5, -0.5, 1.0, -1.0), 2))
    recorder._stop_worker()

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

    recorder._start_worker()
    assert recorder._work_queue is not None
    recorder._work_queue.put((struct.pack("<3h", 100, -200, 300), 3))
    recorder._stop_worker()

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getframerate() == 44_100
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        samples = struct.unpack("<3h", wav_file.readframes(3))

    assert samples == (100, -200, 300)


def test_start_worker_raises_cleanly_for_missing_output_directory(tmp_path) -> None:
    output_path = tmp_path / "missing" / "recording.wav"
    recorder = AudioRecorder(123, output_path)

    with pytest.raises(FileNotFoundError):
        recorder._start_worker()

    assert recorder._wav_file is None
    assert recorder._output_file is None


def test_worker_invokes_callback_off_thread() -> None:
    callback_threads: list[str] = []
    callback_event = threading.Event()

    def on_data(data: bytes, num_frames: int) -> None:
        callback_threads.append(threading.current_thread().name)
        assert data == b"\x01\x02"
        assert num_frames == 1
        callback_event.set()

    recorder = AudioRecorder(123, on_data=on_data)
    recorder._start_worker()

    assert recorder._work_queue is not None
    recorder._work_queue.put((b"\x01\x02", 1))
    assert callback_event.wait(timeout=1)

    recorder._stop_worker()

    assert callback_threads == ["catap-audio-worker"]


def test_stop_reports_dropped_audio_when_worker_queue_overflows() -> None:
    callback_started = threading.Event()
    allow_callback_to_finish = threading.Event()

    def on_data(data: bytes, num_frames: int) -> None:
        del data, num_frames
        callback_started.set()
        assert allow_callback_to_finish.wait(timeout=1)

    recorder = AudioRecorder(123, on_data=on_data)
    recorder._max_pending_buffers = 1
    recorder._is_recording = True
    recorder._work_queue = queue.Queue(maxsize=recorder._max_pending_buffers)
    recorder._worker_thread = threading.Thread(
        target=recorder._worker_loop,
        name="catap-audio-worker",
        daemon=True,
    )
    recorder._worker_thread.start()

    assert recorder._enqueue_audio_data(b"\x00\x01", 1) is True
    assert callback_started.wait(timeout=1)
    assert recorder._enqueue_audio_data(b"\x02\x03", 1) is True
    assert recorder._enqueue_audio_data(b"\x04\x05", 2) is False

    allow_callback_to_finish.set()

    with pytest.raises(RuntimeError, match="Dropped 1 audio buffer") as exc_info:
        recorder._stop_worker()

    assert "2 frame(s)" in str(exc_info.value)


def test_stop_reports_core_audio_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def stop_device(device_id: int, io_proc_id: ctypes.c_void_p) -> int:
        calls.append(f"stop:{device_id}:{io_proc_id.value}")
        return 10

    def destroy_io_proc(device_id: int, io_proc_id: ctypes.c_void_p) -> int:
        calls.append(f"destroy-io:{device_id}:{io_proc_id.value}")
        return 20

    def destroy_aggregate_device(device_id: int) -> None:
        calls.append(f"destroy-device:{device_id}")
        raise OSError("aggregate cleanup failed")

    monkeypatch.setattr(recorder_module, "_AudioDeviceStop", stop_device)
    monkeypatch.setattr(recorder_module, "_AudioDeviceDestroyIOProcID", destroy_io_proc)
    monkeypatch.setattr(
        recorder_module, "_destroy_aggregate_device", destroy_aggregate_device
    )

    recorder = AudioRecorder(123)
    recorder._is_recording = True
    recorder._aggregate_device_id = 55
    recorder._io_proc_id = ctypes.c_void_p(77)

    with pytest.raises(OSError, match="Failed to stop recording cleanly") as exc_info:
        recorder.stop()

    message = str(exc_info.value)
    assert "Failed to stop audio device: status 10" in message
    assert calls == ["stop:55:77", "destroy-io:55:77", "destroy-device:55"]
    assert recorder._aggregate_device_id is None
    assert recorder._io_proc_id is None
    assert recorder.is_recording is False
    assert any(
        "Failed to destroy IO proc: status 20" in note
        for note in exc_info.value.__notes__
    )
    assert any("aggregate cleanup failed" in note for note in exc_info.value.__notes__)
