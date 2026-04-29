"""Recorder behavior tests."""

from __future__ import annotations

import ctypes
import queue
import struct
import threading
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import catap._capture_engine as capture_module
import catap._recording_worker as worker_module
from catap.audio_buffer import (
    AudioBuffer as PublicAudioBuffer,
    AudioStreamFormat,
    SampleType,
    _format_id_to_fourcc,
)
from catap.bindings._audiotoolbox import (
    AudioBuffer as CoreAudioBuffer,
    AudioBufferList,
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


def _audio_buffer_list_pointer(
    *buffers: tuple[bytes, int],
) -> tuple[Any, list[object]]:
    class _TestAudioBufferList(ctypes.Structure):
        _fields_ = [
            ("mNumberBuffers", ctypes.c_uint32),
            ("mBuffers", CoreAudioBuffer * len(buffers)),
        ]

    buffer_list = _TestAudioBufferList()
    buffer_list.mNumberBuffers = len(buffers)
    keepalive: list[object] = [buffer_list]

    for index, (data, channels) in enumerate(buffers):
        data_buffer = (ctypes.c_char * len(data)).from_buffer_copy(data)
        keepalive.append(data_buffer)
        buffer_list.mBuffers[index].mNumberChannels = channels
        buffer_list.mBuffers[index].mDataByteSize = len(data)
        buffer_list.mBuffers[index].mData = ctypes.cast(data_buffer, ctypes.c_void_p)

    return (
        ctypes.cast(ctypes.pointer(buffer_list), ctypes.POINTER(AudioBufferList)),
        keepalive,
    )


def _timestamp_pointer(
    *,
    flags: int,
    sample_time: float = 10.0,
    host_time: int = 20,
    rate_scalar: float = 1.0,
    word_clock_time: int = 30,
) -> tuple[Any, object]:
    timestamp = capture_module.AudioTimeStamp()
    timestamp.mSampleTime = sample_time
    timestamp.mHostTime = host_time
    timestamp.mRateScalar = rate_scalar
    timestamp.mWordClockTime = word_clock_time
    timestamp.mFlags = flags
    return ctypes.pointer(timestamp), timestamp


class _CapturingWorker:
    def __init__(self) -> None:
        self.enqueued: list[tuple[bytes, int, int]] = []
        self.timings: list[worker_module._AudioBufferTimingSnapshot] = []
        self.pool_buffers: list[ctypes.Array] = []

    def acquire_pool_buffer(self, needed: int) -> ctypes.Array:
        buf = (ctypes.c_char * needed)()
        self.pool_buffers.append(buf)
        return buf

    def enqueue_audio_data(
        self,
        buf: ctypes.Array,
        num_frames: int,
        byte_count: int,
        timing: worker_module._AudioBufferTimingSnapshot,
    ) -> bool:
        self.enqueued.append(
            (bytes(memoryview(buf)[:byte_count]), num_frames, byte_count)
        )
        self.timings.append(timing)
        return True


def _recording_recorder_with_worker(
    fake_worker: _CapturingWorker,
) -> AudioRecorder:
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._worker = cast(Any, fake_worker)
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"
    recorder._num_channels = 2
    recorder._bits_per_sample = 16
    recorder._bytes_per_frame = 4
    return recorder


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


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (
            capture_module.kAudioTimeStampSampleTimeValid,
            (10.0, None, None, None),
        ),
        (
            capture_module.kAudioTimeStampHostTimeValid,
            (None, 20, None, None),
        ),
        (0, (None, None, None, None)),
        (
            capture_module.kAudioTimeStampSampleTimeValid
            | capture_module.kAudioTimeStampHostTimeValid
            | capture_module.kAudioTimeStampRateScalarValid
            | capture_module.kAudioTimeStampWordClockTimeValid,
            (10.0, 20, 1.0, 30),
        ),
    ],
)
def test_timestamp_snapshot_decodes_validity_flags(
    flags: int,
    expected: tuple[float | None, int | None, float | None, int | None],
) -> None:
    timestamp, _keepalive = _timestamp_pointer(flags=flags)

    snapshot = AudioRecorder._timestamp_snapshot(timestamp)

    assert (
        snapshot.sample_time,
        snapshot.host_time,
        snapshot.rate_scalar,
        snapshot.word_clock_time,
    ) == expected


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
        assert buffer.timing.input_time.sample_time is None
        callback_event.set()

    worker = _make_worker()
    config = _make_worker_config(on_buffer=on_buffer)
    worker.start(config)

    buf = (ctypes.c_char * 2).from_buffer_copy(b"\x01\x02")
    assert worker.enqueue_audio_data(buf, 1, 2) is True
    assert callback_event.wait(timeout=1)

    worker.stop()

    assert callback_threads == ["catap-audio-worker"]


def test_worker_exposes_buffer_timing_and_reuses_stream_format() -> None:
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

    timing = worker_module._AudioBufferTimingSnapshot(
        now=worker_module._AudioTimestampSnapshot(
            sample_time=None,
            host_time=100,
            rate_scalar=None,
            word_clock_time=None,
        ),
        input_time=worker_module._AudioTimestampSnapshot(
            sample_time=200.5,
            host_time=300,
            rate_scalar=1.0,
            word_clock_time=400,
        ),
        output_time=worker_module._AudioTimestampSnapshot(
            sample_time=None,
            host_time=None,
            rate_scalar=None,
            word_clock_time=None,
        ),
    )
    for payload in (b"\x01\x02\x03\x04", b"\x05\x06\x07\x08"):
        buf = (ctypes.c_char * len(payload)).from_buffer_copy(payload)
        assert worker.enqueue_audio_data(buf, 1, len(payload), timing) is True

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
    assert received[0].timing.now.host_time == 100
    assert received[0].timing.input_time.sample_time == 200.5
    assert received[0].timing.input_time.host_time == 300
    assert received[0].timing.input_time.rate_scalar == 1.0
    assert received[0].timing.input_time.word_clock_time == 400
    assert received[0].timing.output_time.host_time is None


def test_worker_thread_is_non_daemon() -> None:
    worker = _make_worker()
    config = _make_worker_config(on_buffer=lambda buffer: None)
    worker.start(config)

    assert worker.thread is not None
    assert worker.thread.daemon is False

    worker.stop()


def test_worker_buffer_pool_handles_concurrent_producers() -> None:
    producer_count = 8
    items_per_producer = 128
    expected_frames = producer_count * items_per_producer
    payload = b"\x01\x02\x03\x04"
    received_frames = 0
    received_lock = threading.Lock()
    producer_errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
    dropped_frames: list[int] = []

    def on_buffer(buffer: PublicAudioBuffer) -> None:
        nonlocal received_frames
        assert buffer.data == payload
        assert buffer.frame_count == 1
        with received_lock:
            received_frames += buffer.frame_count

    worker = _make_worker(
        record_dropped_frames=dropped_frames.append,
        consume_dropped_stats=lambda: (len(dropped_frames), sum(dropped_frames)),
    )
    config = _make_worker_config(
        on_buffer=on_buffer,
        max_pending_buffers=expected_frames,
    )
    worker.start(config)
    barrier = threading.Barrier(producer_count)

    def produce() -> None:
        try:
            barrier.wait(timeout=1)
            for _ in range(items_per_producer):
                buf = worker.acquire_pool_buffer(len(payload))
                assert buf is not None
                ctypes.memmove(buf, payload, len(payload))
                assert worker.enqueue_audio_data(buf, 1, len(payload)) is True
        except BaseException as exc:
            producer_errors.put(exc)

    producers = [threading.Thread(target=produce) for _ in range(producer_count)]
    for producer in producers:
        producer.start()
    for producer in producers:
        producer.join()

    worker.stop()

    if not producer_errors.empty():
        raise producer_errors.get()

    assert received_frames == expected_frames
    assert dropped_frames == []


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

    buf_type = ctypes.c_char * 2
    assert (
        worker.enqueue_audio_data(buf_type.from_buffer_copy(b"\x00\x01"), 1, 2) is True
    )
    assert callback_started.wait(timeout=1)
    assert (
        worker.enqueue_audio_data(buf_type.from_buffer_copy(b"\x02\x03"), 1, 2) is True
    )
    assert (
        worker.enqueue_audio_data(buf_type.from_buffer_copy(b"\x04\x05"), 2, 2) is False
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
        ) -> capture_module._TapCaptureSession:
            del tap_id, callback
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

    buf = (ctypes.c_char * 2).from_buffer_copy(b"\x01\x02")
    assert worker.enqueue_audio_data(buf, 1, 2) is True
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


def test_io_proc_failure_is_reported_on_stop() -> None:
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"

    status = recorder._io_proc(
        0,
        cast(Any, None),
        cast(Any, object()),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
    )

    assert status == 0

    with pytest.raises(RuntimeError, match="Audio callback failed") as exc_info:
        recorder.stop()

    assert isinstance(exc_info.value.__cause__, AttributeError)
    assert any(
        "Failed to stop recording cleanly" in note for note in exc_info.value.__notes__
    )
    assert recorder._lifecycle_state == "idle"


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

        def open_tap_capture(self, tap_id: int, callback: object) -> object:
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

        def open_tap_capture(self, tap_id: int, callback: object) -> object:
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

        def open_tap_capture(self, tap_id: int, callback: object) -> object:
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

        def open_tap_capture(self, tap_id: int, callback: object) -> object:
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

        def open_tap_capture(self, tap_id: int, callback: object) -> object:
            raise AssertionError("capture should not open for unsupported formats")

    recorder = AudioRecorder(123, "recording.wav")
    recorder._capture_engine = cast(Any, _FakeCaptureEngine())

    with pytest.raises(
        UnsupportedTapFormatError,
        match="expected packed interleaved 6-byte frames, got 8",
    ):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


def test_io_proc_copies_single_interleaved_buffer_to_worker() -> None:
    fake_worker = _CapturingWorker()
    recorder = _recording_recorder_with_worker(fake_worker)
    data = struct.pack("<4h", 1, 2, 3, 4)
    input_data, _keepalive = _audio_buffer_list_pointer((data, 2))

    status = recorder._io_proc(
        0,
        cast(Any, None),
        input_data,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
    )

    assert status == 0
    assert fake_worker.enqueued == [(data, 2, len(data))]
    assert recorder.frames_recorded == 2


def test_io_proc_queues_decoded_timing_metadata() -> None:
    fake_worker = _CapturingWorker()
    recorder = _recording_recorder_with_worker(fake_worker)
    data = struct.pack("<2h", 1, 2)
    input_data, keepalive = _audio_buffer_list_pointer((data, 2))
    now, now_keepalive = _timestamp_pointer(
        flags=capture_module.kAudioTimeStampHostTimeValid,
        host_time=111,
    )
    input_time, input_keepalive = _timestamp_pointer(
        flags=(
            capture_module.kAudioTimeStampSampleTimeValid
            | capture_module.kAudioTimeStampHostTimeValid
            | capture_module.kAudioTimeStampRateScalarValid
            | capture_module.kAudioTimeStampWordClockTimeValid
        ),
        sample_time=222.5,
        host_time=333,
        rate_scalar=1.25,
        word_clock_time=444,
    )
    output_time, output_keepalive = _timestamp_pointer(flags=0)
    keepalive.extend([now_keepalive, input_keepalive, output_keepalive])

    status = recorder._io_proc(
        0,
        now,
        input_data,
        input_time,
        cast(Any, None),
        output_time,
        cast(Any, None),
    )

    assert status == 0
    timing = fake_worker.timings[0]
    assert timing.now.host_time == 111
    assert timing.now.sample_time is None
    assert timing.input_time.sample_time == 222.5
    assert timing.input_time.host_time == 333
    assert timing.input_time.rate_scalar == 1.25
    assert timing.input_time.word_clock_time == 444
    assert timing.output_time.sample_time is None
    assert timing.output_time.host_time is None


def test_io_proc_reports_multi_buffer_layout_instead_of_writing_planar_audio() -> None:
    fake_worker = _CapturingWorker()
    recorder = _recording_recorder_with_worker(fake_worker)
    left = struct.pack("<2h", 1, 3)
    right = struct.pack("<2h", 2, 4)
    input_data, _keepalive = _audio_buffer_list_pointer((left, 1), (right, 1))

    status = recorder._io_proc(
        0,
        cast(Any, None),
        input_data,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
    )

    assert status == 0
    assert fake_worker.enqueued == []
    failure = recorder._consume_io_proc_failure()
    assert failure is not None
    assert isinstance(failure.__cause__, UnsupportedTapFormatError)
    assert "expected one interleaved buffer, got 2" in str(failure)


def test_io_proc_reports_partial_frame_instead_of_writing_truncated_audio() -> None:
    fake_worker = _CapturingWorker()
    recorder = _recording_recorder_with_worker(fake_worker)
    input_data, _keepalive = _audio_buffer_list_pointer((b"\x01\x02\x03", 2))

    status = recorder._io_proc(
        0,
        cast(Any, None),
        input_data,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
    )

    assert status == 0
    assert fake_worker.enqueued == []
    failure = recorder._consume_io_proc_failure()
    assert failure is not None
    assert isinstance(failure.__cause__, UnsupportedTapFormatError)
    assert "not a whole number of frames" in str(failure)


def test_io_proc_reports_missing_data_pointer_instead_of_ignoring_bytes() -> None:
    fake_worker = _CapturingWorker()
    recorder = _recording_recorder_with_worker(fake_worker)
    input_data, _keepalive = _audio_buffer_list_pointer((b"\x00\x01\x02\x03", 2))
    input_data.contents.mBuffers[0].mData = None

    status = recorder._io_proc(
        0,
        cast(Any, None),
        input_data,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
    )

    assert status == 0
    assert fake_worker.enqueued == []
    failure = recorder._consume_io_proc_failure()
    assert failure is not None
    assert isinstance(failure.__cause__, UnsupportedTapFormatError)
    assert "without a data pointer" in str(failure)


def test_io_proc_reports_missing_channel_count_instead_of_guessing() -> None:
    fake_worker = _CapturingWorker()
    recorder = _recording_recorder_with_worker(fake_worker)
    input_data, _keepalive = _audio_buffer_list_pointer((b"\x00\x01\x02\x03", 0))

    status = recorder._io_proc(
        0,
        cast(Any, None),
        input_data,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
    )

    assert status == 0
    assert fake_worker.enqueued == []
    failure = recorder._consume_io_proc_failure()
    assert failure is not None
    assert isinstance(failure.__cause__, UnsupportedTapFormatError)
    assert "expected 2, got 0" in str(failure)


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
