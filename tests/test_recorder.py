"""Recorder behavior tests."""

from __future__ import annotations

import ctypes
import struct
import threading
import wave
from collections.abc import Callable
from pathlib import Path

import pytest

import catap._capture_engine as capture_module
import catap._recording_worker as worker_module
from catap.bindings._audiotoolbox import AudioStreamBasicDescription
from catap.bindings.tap import AudioTapNotFoundError
from catap.recorder import AudioRecorder


def _stub_tap_format(tap_id: int) -> AudioStreamBasicDescription:
    del tap_id
    asbd = AudioStreamBasicDescription()
    asbd.mSampleRate = 48_000
    asbd.mChannelsPerFrame = 2
    asbd.mBitsPerChannel = 32
    asbd.mFormatFlags = 0
    return asbd


def _make_worker(
    *,
    record_dropped_frames: Callable[[int], None] | None = None,
    consume_dropped_stats: Callable[[], tuple[int, int]] | None = None,
) -> worker_module._AudioWorker:
    return worker_module._AudioWorker(
        record_dropped_frames=(
            (lambda num_frames: None)
            if record_dropped_frames is None
            else record_dropped_frames
        ),
        consume_dropped_stats=(
            (lambda: (0, 0))
            if consume_dropped_stats is None
            else consume_dropped_stats
        ),
    )


def _make_worker_config(
    *,
    output_path: Path | None = None,
    on_data: Callable[[bytes, int], None] | None = None,
    max_pending_buffers: int = 256,
    sample_rate: float = 44_100.0,
    num_channels: int = 2,
    bits_per_sample: int = 16,
    output_bits_per_sample: int | None = None,
    convert_float_output: bool = False,
) -> worker_module._WorkerConfig:
    if output_bits_per_sample is None:
        output_bits_per_sample = bits_per_sample

    return worker_module._WorkerConfig(
        output_path=output_path,
        on_data=on_data,
        max_pending_buffers=max_pending_buffers,
        sample_rate=sample_rate,
        num_channels=num_channels,
        bits_per_sample=bits_per_sample,
        output_bits_per_sample=output_bits_per_sample,
        convert_float_output=convert_float_output,
    )


def test_writer_streams_float_audio_to_wav(tmp_path) -> None:
    output_path = tmp_path / "recording.wav"
    worker = _make_worker()
    config = _make_worker_config(
        output_path=output_path,
        sample_rate=48_000,
        num_channels=2,
        bits_per_sample=32,
        output_bits_per_sample=16,
        convert_float_output=True,
    )

    worker.start(config)
    data = struct.pack("<4f", 0.5, -0.5, 1.0, -1.0)
    buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
    assert worker.enqueue_audio_data(buf, 2, len(data)) is True
    worker.stop()

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getframerate() == 48_000
        assert wav_file.getnchannels() == 2
        assert wav_file.getsampwidth() == 2
        samples = struct.unpack("<4h", wav_file.readframes(2))

    assert samples == (16384, -16384, 32767, -32768)


def test_writer_preserves_int16_audio(tmp_path) -> None:
    output_path = tmp_path / "recording.wav"
    worker = _make_worker()
    config = _make_worker_config(
        output_path=output_path,
        sample_rate=44_100,
        num_channels=1,
        bits_per_sample=16,
    )

    worker.start(config)
    data = struct.pack("<3h", 100, -200, 300)
    buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
    assert worker.enqueue_audio_data(buf, 3, len(data)) is True
    worker.stop()

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getframerate() == 44_100
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        samples = struct.unpack("<3h", wav_file.readframes(3))

    assert samples == (100, -200, 300)


def test_start_worker_raises_cleanly_for_missing_output_directory(tmp_path) -> None:
    output_path = tmp_path / "missing" / "recording.wav"
    worker = _make_worker()
    config = _make_worker_config(output_path=output_path)

    with pytest.raises(FileNotFoundError):
        worker.start(config)

    assert worker.wav_file is None
    assert worker.output_file is None


def test_recorder_requires_output_path_or_callback() -> None:
    with pytest.raises(
        ValueError,
        match="output_path must be provided unless on_data is set for streaming mode",
    ):
        AudioRecorder(123)


def test_recorder_rejects_non_positive_max_pending_buffers() -> None:
    with pytest.raises(ValueError, match="max_pending_buffers must be greater than 0"):
        AudioRecorder(123, "recording.wav", max_pending_buffers=0)


def test_worker_invokes_callback_off_thread() -> None:
    callback_threads: list[str] = []
    callback_event = threading.Event()

    def on_data(data: bytes, num_frames: int) -> None:
        callback_threads.append(threading.current_thread().name)
        assert data == b"\x01\x02"
        assert num_frames == 1
        callback_event.set()

    worker = _make_worker()
    config = _make_worker_config(on_data=on_data)
    worker.start(config)

    buf = (ctypes.c_char * 2).from_buffer_copy(b"\x01\x02")
    assert worker.enqueue_audio_data(buf, 1, 2) is True
    assert callback_event.wait(timeout=1)

    worker.stop()

    assert callback_threads == ["catap-audio-worker"]


def test_worker_thread_is_non_daemon() -> None:
    worker = _make_worker()
    config = _make_worker_config(on_data=lambda data, num_frames: None)
    worker.start(config)

    assert worker.thread is not None
    assert worker.thread.daemon is False

    worker.stop()


def test_worker_rejects_double_start(tmp_path) -> None:
    worker = _make_worker()
    config = _make_worker_config(output_path=tmp_path / "recording.wav")
    worker.start(config)

    with pytest.raises(RuntimeError, match="Audio worker already started"):
        worker.start(config)

    worker.stop()


def test_stop_reports_dropped_audio_when_worker_queue_overflows() -> None:
    callback_started = threading.Event()
    allow_callback_to_finish = threading.Event()
    dropped_frames: list[int] = []

    def on_data(data: bytes, num_frames: int) -> None:
        del data, num_frames
        callback_started.set()
        assert allow_callback_to_finish.wait(timeout=1)

    worker = _make_worker(
        record_dropped_frames=dropped_frames.append,
        consume_dropped_stats=lambda: (len(dropped_frames), sum(dropped_frames)),
    )
    config = _make_worker_config(on_data=on_data, max_pending_buffers=1)
    worker.start(config)

    buf_type = ctypes.c_char * 2
    assert (
        worker.enqueue_audio_data(buf_type.from_buffer_copy(b"\x00\x01"), 1, 2)
        is True
    )
    assert callback_started.wait(timeout=1)
    assert (
        worker.enqueue_audio_data(buf_type.from_buffer_copy(b"\x02\x03"), 1, 2)
        is True
    )
    assert (
        worker.enqueue_audio_data(buf_type.from_buffer_copy(b"\x04\x05"), 2, 2)
        is False
    )

    allow_callback_to_finish.set()

    with pytest.raises(RuntimeError, match="Dropped 1 audio buffer") as exc_info:
        worker.stop()

    assert "2 frame(s)" in str(exc_info.value)
    assert config.max_pending_buffers == 1


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

    monkeypatch.setattr(capture_module, "_AudioDeviceStop", stop_device)
    monkeypatch.setattr(capture_module, "_AudioDeviceDestroyIOProcID", destroy_io_proc)
    monkeypatch.setattr(
        capture_module, "_destroy_aggregate_device", destroy_aggregate_device
    )

    recorder = AudioRecorder(123, on_data=lambda data, num_frames: None)
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"
    recorder._capture_session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=True,
    )

    with pytest.raises(
        OSError,
        match="Failed to stop audio device: status 10",
    ) as exc_info:
        recorder.stop()

    assert calls == ["stop:55:77", "destroy-io:55:77", "destroy-device:55"]
    assert recorder._aggregate_device_id is None
    assert recorder._io_proc_id is None
    assert recorder.is_recording is False
    assert recorder._lifecycle_state == "idle"
    assert any(
        "Failed to stop recording cleanly" in note for note in exc_info.value.__notes__
    )
    assert any(
        "Failed to destroy IO proc: status 20" in note
        for note in exc_info.value.__notes__
    )
    assert any("aggregate cleanup failed" in note for note in exc_info.value.__notes__)


def test_failed_start_does_not_clobber_existing_output_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "existing.wav"
    original_bytes = b"keep-this-audio"
    output_path.write_bytes(original_bytes)

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(capture_module, "_get_tap_format", _stub_tap_format)
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name: (_ for _ in ()).throw(OSError("aggregate failed")),
    )

    recorder = AudioRecorder(123, output_path)

    with pytest.raises(OSError, match="aggregate failed"):
        recorder.start()

    assert output_path.read_bytes() == original_bytes


def test_failed_start_unwinds_cleanup_for_non_oserror_exceptions(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed: list[int] = []

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(capture_module, "_get_tap_format", _stub_tap_format)
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name: 42,
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_aggregate_device",
        lambda device_id: destroyed.append(device_id),
    )

    def _fail_create_io_proc(*args, **kwargs):
        del args, kwargs
        raise wave.Error("unsupported format")

    monkeypatch.setattr(
        capture_module, "_AudioDeviceCreateIOProcID", _fail_create_io_proc
    )

    recorder = AudioRecorder(123, tmp_path / "recording.wav")

    with pytest.raises(wave.Error, match="unsupported format"):
        recorder.start()

    assert destroyed == [42]
    assert recorder._aggregate_device_id is None
    assert recorder._lifecycle_state == "idle"


def test_start_raises_audio_tap_not_found_error_for_stale_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_error = OSError("tap disappeared")
    stale_error.status = int.from_bytes(b"!obj", "big")  # type: ignore[attr-defined]

    monkeypatch.setattr(
        capture_module,
        "_get_tap_uid",
        lambda tap_id: (_ for _ in ()).throw(stale_error),
    )

    recorder = AudioRecorder(123, "recording.wav")

    with pytest.raises(
        AudioTapNotFoundError,
        match="Audio tap 123 is no longer available",
    ):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


def test_start_worker_failure_closes_resources_without_join(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _FailingThread:
        def __init__(
            self,
            *,
            target: object,
            args: tuple[object, ...],
            name: str,
            daemon: bool,
        ) -> None:
            del target, args, name
            calls.append("init")
            self.daemon = daemon

        def start(self) -> None:
            calls.append("start")
            raise RuntimeError("thread start failed")

        def join(self) -> None:
            raise AssertionError("join should not be called")

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(worker_module.threading, "Thread", _FailingThread)

    worker = _make_worker()
    config = _make_worker_config(output_path=tmp_path / "recording.wav")

    with pytest.raises(RuntimeError, match="thread start failed"):
        worker.start(config)

    assert calls == ["init", "start"]
    assert worker.thread is None
    assert worker.output_file is None
    assert worker.wav_file is None
    assert worker.pcm_converter is None


def test_stop_preserves_callback_failure_cause() -> None:
    callback_seen = threading.Event()

    def on_data(data: bytes, num_frames: int) -> None:
        del data, num_frames
        callback_seen.set()
        raise ValueError("boom")

    worker = _make_worker()
    config = _make_worker_config(on_data=on_data)
    worker.start(config)

    buf = (ctypes.c_char * 2).from_buffer_copy(b"\x01\x02")
    assert worker.enqueue_audio_data(buf, 1, 2) is True
    assert callback_seen.wait(timeout=1)

    with pytest.raises(
        RuntimeError,
        match="Audio data callback failed: boom",
    ) as exc_info:
        worker.stop()

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert any(
        "Failed to finalize audio worker" in note for note in exc_info.value.__notes__
    )


def test_stop_preserves_write_failure_cause(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _make_worker()
    config = _make_worker_config(
        output_path=tmp_path / "recording.wav",
        sample_rate=48_000,
        num_channels=2,
        bits_per_sample=32,
        output_bits_per_sample=16,
        convert_float_output=True,
    )
    worker.start(config)

    assert worker.wav_file is not None

    def _fail_write(_data: object) -> None:
        raise ValueError("disk full")

    monkeypatch.setattr(worker.wav_file, "writeframesraw", _fail_write)

    data = struct.pack("<4f", 0.5, -0.5, 1.0, -1.0)
    buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
    assert worker.enqueue_audio_data(buf, 2, len(data)) is True

    with pytest.raises(
        OSError,
        match="Failed to write WAV data: disk full",
    ) as exc_info:
        worker.stop()

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert any(
        "Failed to finalize audio worker" in note for note in exc_info.value.__notes__
    )


def test_stop_preserves_finalize_failure_cause(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _make_worker()
    config = _make_worker_config(output_path=tmp_path / "recording.wav")
    worker.start(config)

    assert worker.wav_file is not None
    wav_file = worker.wav_file

    def _fail_close() -> None:
        raise ValueError("close failed")

    monkeypatch.setattr(wav_file, "close", _fail_close)

    with pytest.raises(
        OSError,
        match="Failed to finalize WAV file: close failed",
    ) as exc_info:
        worker.stop()

    # Prevent pytest teardown from re-invoking the patched failing close.
    wav_file._file = None  # ty: ignore[unresolved-attribute]
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert any(
        "Failed to finalize audio worker" in note for note in exc_info.value.__notes__
    )


def test_frames_recorded_is_monotonic_during_concurrent_updates() -> None:
    recorder = AudioRecorder(123, on_data=lambda data, num_frames: None)
    total_updates = 2_000
    started = threading.Event()

    def _writer() -> None:
        started.set()
        for _ in range(total_updates):
            recorder._record_accepted_frames(1)

    worker = threading.Thread(target=_writer)
    worker.start()
    assert started.wait(timeout=1)

    observed: list[int] = []
    while worker.is_alive():
        observed.append(recorder.frames_recorded)
    worker.join()
    observed.append(recorder.frames_recorded)

    assert observed == sorted(observed)
    assert observed[-1] == total_updates
