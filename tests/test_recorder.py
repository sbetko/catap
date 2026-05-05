"""Recorder behavior tests."""

from __future__ import annotations

import ctypes
import struct
import threading
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import catap._capture_engine as capture_module
import catap._recording_worker as worker_module
import catap.recorder as recorder_module
from catap.audio_buffer import (
    AudioBuffer as PublicAudioBuffer,
    AudioStreamFormat,
    SampleType,
    _format_id_to_fourcc,
)
from catap.bindings._audiotoolbox import (
    AudioStreamBasicDescription,
    kAudioFormatFlagIsFloat,
    kAudioFormatFlagIsPacked,
    kAudioFormatLinearPCM,
)
from catap.bindings.tap import AudioTapNotFoundError
from catap.recorder import AudioRecorder, UnsupportedTapFormatError


def _stub_tap_format(tap_id: int) -> AudioStreamBasicDescription:
    del tap_id
    asbd = AudioStreamBasicDescription()
    asbd.mSampleRate = 48_000
    asbd.mFormatID = kAudioFormatLinearPCM
    asbd.mChannelsPerFrame = 2
    asbd.mBitsPerChannel = 32
    asbd.mBytesPerFrame = 8
    asbd.mFormatFlags = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked
    return asbd


def _make_worker(
    *,
    record_accepted_frames: Callable[[int], None] | None = None,
    record_dropped_frames: Callable[[int], None] | None = None,
    consume_dropped_stats: Callable[[], tuple[int, int]] | None = None,
) -> worker_module._AudioWorker:
    return worker_module._AudioWorker(
        record_accepted_frames=(
            (lambda num_frames: None)
            if record_accepted_frames is None
            else record_accepted_frames
        ),
        record_dropped_frames=(
            (lambda num_frames: None)
            if record_dropped_frames is None
            else record_dropped_frames
        ),
        consume_dropped_stats=(
            (lambda: (0, 0)) if consume_dropped_stats is None else consume_dropped_stats
        ),
    )


def _make_worker_config(
    *,
    output_path: Path | None = None,
    on_buffer: Callable[[PublicAudioBuffer], None] | None = None,
    max_pending_buffers: int = 256,
    sample_rate: float = 44_100.0,
    num_channels: int = 2,
    bits_per_sample: int = 16,
    sample_type: SampleType | None = None,
    output_bits_per_sample: int | None = None,
    convert_float_output: bool = False,
) -> worker_module._WorkerConfig:
    if output_bits_per_sample is None:
        output_bits_per_sample = bits_per_sample
    if sample_type is None:
        sample_type = "float" if convert_float_output else "signed_integer"

    return worker_module._WorkerConfig(
        output_path=output_path,
        on_buffer=on_buffer,
        max_pending_buffers=max_pending_buffers,
        stream_format=AudioStreamFormat(
            sample_rate=sample_rate,
            num_channels=num_channels,
            bits_per_sample=bits_per_sample,
            sample_type=sample_type,
            format_id="lpcm",
        ),
        output_bits_per_sample=output_bits_per_sample,
        convert_float_output=convert_float_output,
    )


class _FakeNativeRingStats:
    def __init__(
        self,
        *,
        dropped_chunks: int = 0,
        dropped_frames: int = 0,
        oversized_chunks: int = 0,
    ) -> None:
        self.dropped_chunks = dropped_chunks
        self.dropped_frames = dropped_frames
        self.oversized_chunks = oversized_chunks


class _FakeNativeRecorderStats:
    def __init__(
        self,
        *,
        callback_failures: int = 0,
        last_error_status: int = 0,
        last_error_name: str = "OK",
        ring: _FakeNativeRingStats | None = None,
    ) -> None:
        self.callback_failures = callback_failures
        self.last_error_status = last_error_status
        self.last_error_name = last_error_name
        self.ring = _FakeNativeRingStats() if ring is None else ring


class _FakeNativeChunk:
    def __init__(
        self,
        data: bytes,
        frame_count: int,
        input_sample_time: float | None = None,
    ) -> None:
        self.data = data
        self.frame_count = frame_count
        self.input_sample_time = input_sample_time


@pytest.fixture(autouse=True)
def _fake_native_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeNativeRecorder:
        def __init__(
            self,
            *,
            slot_count: int,
            slot_capacity: int,
            expected_channel_count: int,
            bytes_per_frame: int,
        ) -> None:
            del slot_count, slot_capacity, expected_channel_count, bytes_per_frame
            self.io_proc_pointer = ctypes.c_void_p(456)
            self.handle = ctypes.c_void_p(789)
            self.closed = False

        def read(self) -> object | None:
            return None

        def stats(self) -> _FakeNativeRecorderStats:
            return _FakeNativeRecorderStats()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(recorder_module, "NativeCoreAudioRecorder", _FakeNativeRecorder)


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
    assert worker.enqueue_audio_bytes(data, 2) is True
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
    assert worker.enqueue_audio_bytes(data, 3) is True
    worker.stop()

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getframerate() == 44_100
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        samples = struct.unpack("<3h", wav_file.readframes(3))

    assert samples == (100, -200, 300)


def test_worker_queues_owned_audio_bytes_for_non_realtime_producers() -> None:
    seen: list[PublicAudioBuffer] = []
    worker = _make_worker()
    config = _make_worker_config(on_buffer=lambda buffer: seen.append(buffer))
    worker.start(config)

    assert worker.enqueue_audio_bytes(b"\x01\x02\x03\x04", 2, 123.5) is True

    worker.stop()

    assert len(seen) == 1
    assert seen[0].data == b"\x01\x02\x03\x04"
    assert seen[0].frame_count == 2
    assert seen[0].input_sample_time == 123.5


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
        match="output_path must be provided unless on_buffer is set for streaming mode",
    ):
        AudioRecorder(123)


def test_recorder_rejects_non_positive_max_pending_buffers() -> None:
    with pytest.raises(ValueError, match="max_pending_buffers must be greater than 0"):
        AudioRecorder(123, "recording.wav", max_pending_buffers=0)


@pytest.mark.parametrize("value", [True, 1.5, "8"])
def test_recorder_rejects_non_integer_max_pending_buffers(value: object) -> None:
    with pytest.raises(TypeError, match="max_pending_buffers must be an integer"):
        AudioRecorder(123, "recording.wav", max_pending_buffers=cast(Any, value))


def test_format_id_to_fourcc_decodes_core_audio_format_ids() -> None:
    assert _format_id_to_fourcc(int.from_bytes(b"lpcm", "big")) == "lpcm"
    assert _format_id_to_fourcc(int.from_bytes(b"alac", "big")) == "alac"


def test_recorder_stream_format_is_unknown_until_tap_is_described() -> None:
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)

    assert recorder.stream_format is None
    with pytest.raises(RuntimeError, match="Stream format is not known"):
        recorder._make_worker_config()

    recorder._apply_stream_format(
        capture_module._TapStreamFormat(
            sample_rate=48_000.0,
            num_channels=2,
            bits_per_sample=32,
            is_float=True,
            bytes_per_frame=8,
            format_id=kAudioFormatLinearPCM,
            is_signed_integer=False,
        )
    )

    stream_format = recorder.stream_format
    assert stream_format == AudioStreamFormat(
        sample_rate=48_000.0,
        num_channels=2,
        bits_per_sample=32,
        sample_type="float",
        format_id="lpcm",
    )


def test_worker_invokes_callback_off_thread() -> None:
    callback_threads: list[str] = []
    callback_event = threading.Event()

    def on_buffer(buffer: PublicAudioBuffer) -> None:
        callback_threads.append(threading.current_thread().name)
        assert buffer.data == b"\x01\x02"
        assert buffer.frame_count == 1
        assert buffer.byte_count == 2
        assert buffer.duration_seconds == pytest.approx(1 / 44_100)
        assert buffer.format.format_id == "lpcm"
        assert buffer.format.bytes_per_frame == 4
        assert buffer.format.is_signed_integer is True
        assert buffer.format.is_float is False
        assert buffer.input_sample_time is None
        callback_event.set()

    worker = _make_worker()
    config = _make_worker_config(on_buffer=on_buffer)
    worker.start(config)

    assert worker.enqueue_audio_bytes(b"\x01\x02", 1) is True
    assert callback_event.wait(timeout=1)

    worker.stop()

    assert callback_threads == ["catap-audio-worker"]


def test_worker_exposes_input_sample_time_and_reuses_stream_format() -> None:
    received: list[PublicAudioBuffer] = []
    callback_event = threading.Event()

    def on_buffer(buffer: PublicAudioBuffer) -> None:
        received.append(buffer)
        if len(received) == 2:
            callback_event.set()

    worker = _make_worker()
    config = _make_worker_config(
        on_buffer=on_buffer,
        sample_rate=48_000,
        bits_per_sample=32,
        sample_type="float",
        max_pending_buffers=4,
    )
    worker.start(config)

    for payload in (b"\x01\x02\x03\x04", b"\x05\x06\x07\x08"):
        assert worker.enqueue_audio_bytes(payload, 1, 200.5) is True

    assert callback_event.wait(timeout=1)
    worker.stop()

    assert [buffer.data for buffer in received] == [
        b"\x01\x02\x03\x04",
        b"\x05\x06\x07\x08",
    ]
    assert all(isinstance(buffer.data, bytes) for buffer in received)
    assert received[0].format is received[1].format
    assert received[0].format.format_id == "lpcm"
    assert received[0].format.is_float is True
    assert received[0].format.bytes_per_frame == 8
    assert received[0].input_sample_time == 200.5


def test_worker_thread_is_non_daemon() -> None:
    worker = _make_worker()
    config = _make_worker_config(on_buffer=lambda buffer: None)
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

    def on_buffer(buffer: PublicAudioBuffer) -> None:
        del buffer
        callback_started.set()
        assert allow_callback_to_finish.wait(timeout=1)

    worker = _make_worker(
        record_dropped_frames=dropped_frames.append,
        consume_dropped_stats=lambda: (len(dropped_frames), sum(dropped_frames)),
    )
    config = _make_worker_config(on_buffer=on_buffer, max_pending_buffers=1)
    worker.start(config)

    assert worker.enqueue_audio_bytes(b"\x00\x01", 1)
    assert callback_started.wait(timeout=1)
    assert not worker.enqueue_audio_bytes(b"\x02\x03", 2)

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

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
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


def test_failed_device_start_does_not_clobber_existing_output_file(tmp_path) -> None:
    output_path = tmp_path / "existing.wav"
    original_bytes = b"keep-this-audio"
    output_path.write_bytes(original_bytes)

    class _StartFailingCaptureEngine:
        def describe_tap_stream(
            self,
            tap_id: int,
        ) -> capture_module._TapStreamFormat:
            del tap_id
            return capture_module._TapStreamFormat(
                48_000.0,
                2,
                32,
                False,
                bytes_per_frame=8,
                is_interleaved=True,
            )

        def open_tap_capture(
            self,
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> capture_module._TapCaptureSession:
            del tap_id, callback, client_data
            return capture_module._TapCaptureSession(55, ctypes.c_void_p(77))

        def start(self, session: capture_module._TapCaptureSession) -> None:
            del session
            raise OSError("device start failed")

        def close(self, session: capture_module._TapCaptureSession) -> None:
            del session

    recorder = AudioRecorder(123, output_path)
    recorder._capture_engine = cast(Any, _StartFailingCaptureEngine())

    with pytest.raises(OSError, match="device start failed"):
        recorder.start()

    assert output_path.read_bytes() == original_bytes
    assert list(tmp_path.glob(".existing.wav.*.tmp")) == []
    assert recorder._lifecycle_state == "idle"


def test_start_uses_native_io_proc_when_dylib_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_recorders: list[Any] = []
    capture_calls: list[tuple[object, object | None]] = []

    class _FakeNativeRecorder:
        def __init__(
            self,
            *,
            slot_count: int,
            slot_capacity: int,
            expected_channel_count: int,
            bytes_per_frame: int,
        ) -> None:
            self.slot_count = slot_count
            self.slot_capacity = slot_capacity
            self.expected_channel_count = expected_channel_count
            self.bytes_per_frame = bytes_per_frame
            self.io_proc_pointer = ctypes.c_void_p(456)
            self.handle = ctypes.c_void_p(789)
            self.closed = False
            created_recorders.append(self)

        def read(self) -> object | None:
            return None

        def stats(self) -> _FakeNativeRecorderStats:
            return _FakeNativeRecorderStats()

        def close(self) -> None:
            self.closed = True

    class _NativeCaptureEngine:
        def describe_tap_stream(
            self,
            tap_id: int,
        ) -> capture_module._TapStreamFormat:
            del tap_id
            return capture_module._TapStreamFormat(
                48_000.0,
                2,
                32,
                True,
                bytes_per_frame=8,
                is_signed_integer=False,
            )

        def open_tap_capture(
            self,
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> capture_module._TapCaptureSession:
            del tap_id
            capture_calls.append((callback, client_data))
            return capture_module._TapCaptureSession(55, ctypes.c_void_p(77))

        def start(self, session: capture_module._TapCaptureSession) -> None:
            session.started = True

        def stop(self, session: capture_module._TapCaptureSession) -> None:
            session.started = False

        def close(self, session: capture_module._TapCaptureSession) -> None:
            del session

    monkeypatch.setattr(recorder_module, "NativeCoreAudioRecorder", _FakeNativeRecorder)

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._capture_engine = cast(Any, _NativeCaptureEngine())

    recorder.start()
    recorder.stop()

    native_recorder = created_recorders[0]
    assert native_recorder.slot_count == recorder.max_pending_buffers
    assert (
        native_recorder.slot_capacity
        == 8 * recorder_module._NATIVE_SLOT_FRAME_CAPACITY
    )
    assert native_recorder.expected_channel_count == 2
    assert native_recorder.bytes_per_frame == 8
    assert capture_calls == [(native_recorder.io_proc_pointer, native_recorder.handle)]
    assert native_recorder.closed is True
    assert recorder._native_recorder is None


def test_start_fails_when_native_recorder_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingNativeRecorder:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            raise RuntimeError("native dylib missing")

    class _CaptureEngine:
        def describe_tap_stream(
            self,
            tap_id: int,
        ) -> capture_module._TapStreamFormat:
            del tap_id
            return capture_module._TapStreamFormat(
                48_000.0,
                2,
                32,
                True,
                bytes_per_frame=8,
                is_signed_integer=False,
            )

        def open_tap_capture(
            self,
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> object:
            del tap_id, callback, client_data
            raise AssertionError("capture should not open without native recorder")

    monkeypatch.setattr(
        recorder_module,
        "NativeCoreAudioRecorder",
        _MissingNativeRecorder,
    )

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._capture_engine = cast(Any, _CaptureEngine())

    with pytest.raises(RuntimeError, match="native dylib missing"):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


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
        "_get_tap_format",
        lambda tap_id: (_ for _ in ()).throw(stale_error),
    )
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

    def on_buffer(buffer: PublicAudioBuffer) -> None:
        del buffer
        callback_seen.set()
        raise ValueError("boom")

    worker = _make_worker()
    config = _make_worker_config(on_buffer=on_buffer)
    worker.start(config)

    assert worker.enqueue_audio_bytes(b"\x01\x02", 1) is True
    assert callback_seen.wait(timeout=1)

    with pytest.raises(
        RuntimeError,
        match="Audio buffer callback failed: boom",
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

    assert worker.wav_file is not None

    def _fail_write(_data: object) -> None:
        raise ValueError("disk full")

    monkeypatch.setattr(worker.wav_file, "writeframesraw", _fail_write)

    data = struct.pack("<4f", 0.5, -0.5, 1.0, -1.0)
    assert worker.enqueue_audio_bytes(data, 2) is True

    with pytest.raises(
        OSError,
        match="Failed to write WAV data: disk full",
    ) as exc_info:
        worker.stop()

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert any(
        "Failed to finalize audio worker" in note for note in exc_info.value.__notes__
    )
    assert not output_path.exists()
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


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


def test_start_rejects_non_interleaved_tap_format() -> None:
    class _FakeCaptureEngine:
        def describe_tap_stream(
            self,
            tap_id: int,
        ) -> capture_module._TapStreamFormat:
            del tap_id
            return capture_module._TapStreamFormat(
                48_000.0,
                2,
                32,
                True,
                bytes_per_frame=4,
                is_interleaved=False,
                is_signed_integer=False,
            )

        def open_tap_capture(
            self,
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> object:
            del tap_id, callback, client_data
            raise AssertionError("capture should not open for unsupported formats")

    recorder = AudioRecorder(123, "recording.wav")
    recorder._capture_engine = cast(Any, _FakeCaptureEngine())

    with pytest.raises(UnsupportedTapFormatError, match="non-interleaved"):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


def test_start_rejects_non_linear_pcm_tap_format() -> None:
    class _FakeCaptureEngine:
        def describe_tap_stream(
            self,
            tap_id: int,
        ) -> capture_module._TapStreamFormat:
            del tap_id
            return capture_module._TapStreamFormat(
                48_000.0,
                2,
                32,
                True,
                bytes_per_frame=8,
                format_id=int.from_bytes(b"aac ", "big"),
                is_signed_integer=False,
            )

        def open_tap_capture(
            self,
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> object:
            del tap_id, callback, client_data
            raise AssertionError("capture should not open for unsupported formats")

    recorder = AudioRecorder(123, "recording.wav")
    recorder._capture_engine = cast(Any, _FakeCaptureEngine())

    with pytest.raises(UnsupportedTapFormatError, match="only linear PCM"):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


def test_start_rejects_big_endian_tap_format() -> None:
    class _FakeCaptureEngine:
        def describe_tap_stream(
            self,
            tap_id: int,
        ) -> capture_module._TapStreamFormat:
            del tap_id
            return capture_module._TapStreamFormat(
                48_000.0,
                2,
                16,
                False,
                bytes_per_frame=4,
                is_big_endian=True,
                is_signed_integer=True,
            )

        def open_tap_capture(
            self,
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> object:
            del tap_id, callback, client_data
            raise AssertionError("capture should not open for unsupported formats")

    recorder = AudioRecorder(123, "recording.wav")
    recorder._capture_engine = cast(Any, _FakeCaptureEngine())

    with pytest.raises(UnsupportedTapFormatError, match="big-endian"):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


def test_start_rejects_unsigned_integer_tap_format() -> None:
    class _FakeCaptureEngine:
        def describe_tap_stream(
            self,
            tap_id: int,
        ) -> capture_module._TapStreamFormat:
            del tap_id
            return capture_module._TapStreamFormat(
                48_000.0,
                2,
                16,
                False,
                bytes_per_frame=4,
                is_signed_integer=False,
            )

        def open_tap_capture(
            self,
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> object:
            del tap_id, callback, client_data
            raise AssertionError("capture should not open for unsupported formats")

    recorder = AudioRecorder(123, "recording.wav")
    recorder._capture_engine = cast(Any, _FakeCaptureEngine())

    with pytest.raises(UnsupportedTapFormatError, match="signed integer PCM"):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


def test_start_rejects_padded_tap_frames() -> None:
    class _FakeCaptureEngine:
        def describe_tap_stream(
            self,
            tap_id: int,
        ) -> capture_module._TapStreamFormat:
            del tap_id
            return capture_module._TapStreamFormat(
                48_000.0,
                2,
                24,
                False,
                bytes_per_frame=8,
                is_interleaved=True,
            )

        def open_tap_capture(
            self,
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> object:
            del tap_id, callback, client_data
            raise AssertionError("capture should not open for unsupported formats")

    recorder = AudioRecorder(123, "recording.wav")
    recorder._capture_engine = cast(Any, _FakeCaptureEngine())

    with pytest.raises(
        UnsupportedTapFormatError,
        match="expected packed interleaved 6-byte frames, got 8",
    ):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


def test_frames_recorded_is_monotonic_during_concurrent_updates() -> None:
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
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
