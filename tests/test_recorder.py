"""Recorder behavior tests."""

from __future__ import annotations

import ctypes
import os
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


class _InspectableNativeRecorder:
    def __init__(self) -> None:
        self.closed = False
        self.abandoned = False
        self.io_proc_pointer = ctypes.c_void_p(456)
        self.handle = ctypes.c_void_p(789)

    def read(self) -> object | None:
        return None

    def stats(self) -> _FakeNativeRecorderStats:
        return _FakeNativeRecorderStats()

    def close(self) -> None:
        self.closed = True

    def abandon(self) -> None:
        self.abandoned = True


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


def test_worker_rejects_stop_from_callback_without_poisoning_state() -> None:
    callback_finished = threading.Event()
    stop_errors: list[RuntimeError] = []
    worker = _make_worker()

    def on_buffer(buffer: PublicAudioBuffer) -> None:
        del buffer
        try:
            worker.stop()
        except RuntimeError as exc:
            stop_errors.append(exc)
        finally:
            callback_finished.set()

    config = _make_worker_config(on_buffer=on_buffer)
    worker.start(config)

    assert worker.enqueue_audio_bytes(b"\x00\x01", 1)
    assert callback_finished.wait(timeout=1)
    assert len(stop_errors) == 1
    assert "signal the owning thread" in str(stop_errors[0])
    assert worker.thread is not None
    assert worker.thread.is_alive()

    worker.stop()
    worker.start(config)
    worker.stop()


def test_recorder_rejects_stop_from_callback_before_lifecycle_mutation() -> None:
    callback_finished = threading.Event()
    stop_errors: list[RuntimeError] = []
    recorder: AudioRecorder

    def on_buffer(buffer: PublicAudioBuffer) -> None:
        del buffer
        try:
            recorder.stop()
        except RuntimeError as exc:
            stop_errors.append(exc)
        finally:
            callback_finished.set()

    recorder = AudioRecorder(123, on_buffer=on_buffer)
    recorder._apply_stream_format(
        capture_module._TapStreamFormat(
            44_100.0,
            2,
            16,
            False,
            bytes_per_frame=4,
            is_signed_integer=True,
        )
    )
    recorder._worker.start(recorder._make_worker_config())
    recorder._lifecycle_state = "recording"
    recorder._is_recording = True

    assert recorder._worker.enqueue_audio_bytes(b"\x00\x01\x02\x03", 1)
    assert callback_finished.wait(timeout=1)

    assert len(stop_errors) == 1
    assert "threading.Event" in str(stop_errors[0])
    assert recorder.is_recording is True
    assert recorder._lifecycle_state == "recording"

    recorder.stop()
    assert recorder.is_recording is False
    assert recorder._lifecycle_state == "idle"
    assert recorder._worker.thread is None


def test_worker_stop_finishes_cleanup_when_finalization_is_interrupted(
    tmp_path,
) -> None:
    consume_calls = 0
    interrupt = KeyboardInterrupt("finalization interrupted")

    def _consume_dropped_stats() -> tuple[int, int]:
        nonlocal consume_calls
        consume_calls += 1
        if consume_calls == 1:
            raise interrupt
        return (0, 0)

    worker = _make_worker(consume_dropped_stats=_consume_dropped_stats)
    worker.start(_make_worker_config(output_path=tmp_path / "recording.wav"))

    with pytest.raises(KeyboardInterrupt, match="finalization interrupted") as exc_info:
        worker.stop()

    assert exc_info.value is interrupt
    assert worker.thread is None
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_worker_stop_confirms_exit_after_join_is_interrupted(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("join interrupted")
    worker = _make_worker()
    worker.start(_make_worker_config(output_path=tmp_path / "recording.wav"))
    thread = worker.thread
    assert thread is not None
    real_join = thread.join
    join_calls = 0

    def _interrupt_first_join() -> None:
        nonlocal join_calls
        join_calls += 1
        if join_calls == 1:
            raise interrupt
        real_join()

    monkeypatch.setattr(thread, "join", _interrupt_first_join)

    with pytest.raises(KeyboardInterrupt, match="join interrupted") as exc_info:
        worker.stop()

    assert exc_info.value is interrupt
    assert join_calls == 1
    assert worker.thread is None
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_worker_stop_retries_sentinel_after_keyboard_interrupt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("queue put interrupted")
    real_queue_type = worker_module.queue.SimpleQueue
    put_calls = 0

    class _InterruptingQueue:
        def __init__(self) -> None:
            self._queue = real_queue_type()

        def put(self, item: object) -> None:
            nonlocal put_calls
            if item is None:
                put_calls += 1
                if put_calls == 1:
                    raise interrupt
            self._queue.put(item)

        def get(self) -> object:
            return self._queue.get()

    monkeypatch.setattr(worker_module.queue, "SimpleQueue", _InterruptingQueue)
    worker = _make_worker()
    worker.start(_make_worker_config(output_path=tmp_path / "recording.wav"))

    with pytest.raises(KeyboardInterrupt, match="queue put interrupted") as exc_info:
        worker.stop()

    assert exc_info.value is interrupt
    assert put_calls == 2
    assert worker.thread is None
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_worker_stop_retries_temp_discard_after_keyboard_interrupt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("discard interrupted")
    worker = _make_worker()
    worker.start(_make_worker_config(output_path=tmp_path / "recording.wav"))
    real_discard = worker._discard_temporary_output
    discard_calls = 0

    def _interrupt_first_discard(
        state: worker_module._WorkerState,
    ) -> OSError | None:
        nonlocal discard_calls
        discard_calls += 1
        if discard_calls == 1:
            raise interrupt
        return real_discard(state)

    monkeypatch.setattr(worker, "_discard_temporary_output", _interrupt_first_discard)

    with pytest.raises(KeyboardInterrupt, match="discard interrupted") as exc_info:
        worker.stop(publish=False)

    assert exc_info.value is interrupt
    assert discard_calls == 2
    assert worker.thread is None
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_worker_stop_retains_state_until_interrupted_discard_succeeds(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_interrupt = KeyboardInterrupt("first discard interrupted")
    second_interrupt = KeyboardInterrupt("second discard interrupted")
    worker = _make_worker()
    worker.start(_make_worker_config(output_path=tmp_path / "recording.wav"))
    real_discard = worker._discard_temporary_output
    discard_calls = 0

    def _interrupt_twice(
        state: worker_module._WorkerState,
    ) -> OSError | None:
        nonlocal discard_calls
        discard_calls += 1
        if discard_calls == 1:
            raise first_interrupt
        if discard_calls == 2:
            raise second_interrupt
        return real_discard(state)

    monkeypatch.setattr(worker, "_discard_temporary_output", _interrupt_twice)

    with pytest.raises(
        KeyboardInterrupt,
        match="first discard interrupted",
    ) as exc_info:
        worker.stop(publish=False)

    assert exc_info.value is first_interrupt
    assert worker.thread is not None
    assert list(tmp_path.glob(".recording.wav.*.tmp")) != []

    worker.stop(publish=False)

    assert discard_calls == 3
    assert worker.thread is None
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


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
    abandoned_captures: list[tuple[capture_module._TapCaptureSession, object]] = []

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
    monkeypatch.setattr(
        recorder_module,
        "_ABANDONED_NATIVE_CAPTURES",
        abandoned_captures,
    )

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    native_recorder = _InspectableNativeRecorder()
    capture_session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=True,
    )
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"
    recorder._native_recorder = cast(Any, native_recorder)
    recorder._capture_session = capture_session

    with pytest.raises(
        OSError,
        match="Failed to stop audio device: status 10",
    ) as exc_info:
        recorder.stop()

    assert calls == [
        "stop:55:77",
        "destroy-io:55:77",
        "destroy-device:55",
        "stop:55:77",
        "destroy-io:55:77",
        "destroy-device:55",
    ]
    assert recorder._aggregate_device_id == 55
    assert recorder._io_proc_id is capture_session.io_proc_id
    assert recorder.is_recording is False
    assert recorder.needs_cleanup is True
    assert recorder._lifecycle_state == "cleanup_failed"
    assert native_recorder.closed is False
    assert native_recorder.abandoned is True
    assert abandoned_captures == [(capture_session, native_recorder)]
    assert any(
        "Failed to stop recording cleanly" in note for note in exc_info.value.__notes__
    )
    assert any(
        "Failed to destroy IO proc: status 20" in note
        for note in exc_info.value.__notes__
    )
    assert any("aggregate cleanup failed" in note for note in exc_info.value.__notes__)


def test_stop_releases_native_recorder_after_io_proc_destruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    abandoned_captures: list[tuple[capture_module._TapCaptureSession, object]] = []
    monkeypatch.setattr(
        capture_module,
        "_AudioDeviceStop",
        lambda device_id, io_proc_id: 10,
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_io_proc",
        lambda device_id, io_proc_id: None,
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_aggregate_device",
        lambda device_id: (_ for _ in ()).throw(
            OSError("aggregate cleanup failed")
        ),
    )
    monkeypatch.setattr(
        recorder_module,
        "_ABANDONED_NATIVE_CAPTURES",
        abandoned_captures,
    )

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    native_recorder = _InspectableNativeRecorder()
    capture_session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=True,
    )
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"
    recorder._native_recorder = cast(Any, native_recorder)
    recorder._capture_session = capture_session

    with pytest.raises(OSError, match="Failed to stop audio device"):
        recorder.stop()

    assert capture_session.io_proc_destroyed is True
    assert capture_session.aggregate_device_destroyed is False
    assert native_recorder.closed is True
    assert native_recorder.abandoned is False
    assert abandoned_captures == []


def test_stop_finishes_cleanup_after_capture_interrupt() -> None:
    interrupt = KeyboardInterrupt("capture cleanup interrupted")

    class _InterruptingCaptureEngine:
        def close(self, session: capture_module._TapCaptureSession) -> None:
            session.started = False
            session.io_proc_destroyed = True
            session.aggregate_device_destroyed = True
            raise interrupt

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    native_recorder = _InspectableNativeRecorder()
    capture_session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=True,
    )
    recorder._capture_engine = cast(Any, _InterruptingCaptureEngine())
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"
    recorder._native_recorder = cast(Any, native_recorder)
    recorder._capture_session = capture_session

    with pytest.raises(
        KeyboardInterrupt,
        match="capture cleanup interrupted",
    ) as exc_info:
        recorder.stop()

    assert exc_info.value is interrupt
    assert native_recorder.closed is True
    assert native_recorder.abandoned is False
    assert recorder._capture_session is None
    assert recorder._native_recorder is None
    assert recorder.is_recording is False
    assert recorder._lifecycle_state == "idle"


def test_stop_retries_interrupted_lifecycle_publication() -> None:
    interrupt = KeyboardInterrupt("lifecycle publication interrupted")

    class _InterruptingLock:
        def __init__(self) -> None:
            self.enter_calls = 0

        def __enter__(self) -> None:
            self.enter_calls += 1
            if self.enter_calls == 2:
                raise interrupt

        def __exit__(self, *args: object) -> None:
            del args

    lifecycle_lock = _InterruptingLock()
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._lifecycle_lock = cast(Any, lifecycle_lock)
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"

    with pytest.raises(
        KeyboardInterrupt,
        match="lifecycle publication interrupted",
    ) as exc_info:
        recorder.stop()

    assert exc_info.value is interrupt
    assert lifecycle_lock.enter_calls == 3
    assert recorder.is_recording is False
    assert recorder.needs_cleanup is False
    assert recorder._lifecycle_state == "idle"


def test_stop_restores_terminal_state_after_lifecycle_claim_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("interrupted after stop claim")
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"
    monkeypatch.setattr(
        recorder,
        "_finish_stop",
        lambda: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(
        KeyboardInterrupt,
        match="interrupted after stop claim",
    ) as exc_info:
        recorder.stop()

    assert exc_info.value is interrupt
    assert recorder.is_recording is False
    assert recorder.needs_cleanup is False
    assert recorder._lifecycle_state == "idle"


def test_stop_abandons_native_state_when_drain_is_not_quiesced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("drain join interrupted")
    abandoned_captures: list[tuple[capture_module._TapCaptureSession, object]] = []
    drain_stop_calls = 0
    drain_remaining_values: list[bool] = []

    class _LiveDrain:
        @staticmethod
        def is_alive() -> bool:
            return True

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    native_recorder = _InspectableNativeRecorder()
    capture_session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        io_proc_destroyed=True,
        aggregate_device_destroyed=True,
    )
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"
    recorder._native_recorder = cast(Any, native_recorder)
    recorder._capture_session = capture_session
    recorder._native_drain_thread = cast(Any, _LiveDrain())
    recorder._native_drain_abort_event = threading.Event()
    recorder._native_drain_done_event = threading.Event()

    def _interrupt_drain_stop(**kwargs: object) -> None:
        nonlocal drain_stop_calls
        drain_remaining_values.append(cast(bool, kwargs["drain_remaining"]))
        drain_stop_calls += 1
        if drain_stop_calls <= 2:
            raise interrupt
        recorder._native_drain_thread = None
        recorder._native_drain_stop_event = None
        recorder._native_drain_abort_event = None
        recorder._native_drain_done_event = None

    monkeypatch.setattr(recorder, "_stop_native_drain", _interrupt_drain_stop)
    monkeypatch.setattr(
        recorder_module,
        "_ABANDONED_NATIVE_CAPTURES",
        abandoned_captures,
    )

    with pytest.raises(KeyboardInterrupt, match="drain join interrupted") as exc_info:
        recorder.stop()

    assert exc_info.value is interrupt
    assert native_recorder.closed is False
    assert native_recorder.abandoned is True
    assert abandoned_captures == [(capture_session, native_recorder)]
    assert recorder._native_recorder is None
    assert recorder._native_drain_abort_event is not None
    assert recorder._native_drain_abort_event.is_set()
    assert recorder.needs_cleanup is True
    assert recorder._lifecycle_state == "cleanup_failed"

    recorder.stop()

    assert drain_stop_calls == 3
    assert drain_remaining_values == [True, True, False]
    assert recorder.needs_cleanup is False
    assert recorder._lifecycle_state == "idle"


def test_stop_retries_interrupted_native_retention_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("retention interrupted")
    append_calls = 0

    class _InterruptingCaptureEngine:
        @staticmethod
        def close(session: capture_module._TapCaptureSession) -> None:
            session.started = False

    class _InterruptingList(list[tuple[object, object]]):
        def append(self, item: tuple[object, object]) -> None:
            nonlocal append_calls
            append_calls += 1
            if append_calls == 1:
                raise interrupt
            super().append(item)

    abandoned_captures = _InterruptingList()
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    native_recorder = _InspectableNativeRecorder()
    capture_session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=True,
    )
    recorder._capture_engine = cast(Any, _InterruptingCaptureEngine())
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"
    recorder._native_recorder = cast(Any, native_recorder)
    recorder._capture_session = capture_session
    monkeypatch.setattr(
        recorder_module,
        "_ABANDONED_NATIVE_CAPTURES",
        abandoned_captures,
    )

    with pytest.raises(KeyboardInterrupt, match="retention interrupted") as exc_info:
        recorder.stop()

    assert exc_info.value is interrupt
    assert append_calls == 2
    assert abandoned_captures == [(capture_session, native_recorder)]
    assert native_recorder.closed is False
    assert native_recorder.abandoned is True
    assert recorder._native_recorder is None
    assert recorder._capture_session is capture_session
    assert recorder.needs_cleanup is True
    assert recorder._lifecycle_state == "cleanup_failed"


def test_stop_finishes_lifetime_boundary_after_drain_check_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("drain state interrupted")
    drain_checks = 0
    drain_stop_calls = 0

    class _ClosingCaptureEngine:
        @staticmethod
        def close(session: capture_module._TapCaptureSession) -> None:
            session.started = False
            session.io_proc_destroyed = True
            session.aggregate_device_destroyed = True

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    native_recorder = _InspectableNativeRecorder()
    capture_session = capture_module._TapCaptureSession(
        aggregate_device_id=55,
        io_proc_id=ctypes.c_void_p(77),
        started=True,
    )
    recorder._capture_engine = cast(Any, _ClosingCaptureEngine())
    recorder._is_recording = True
    recorder._lifecycle_state = "recording"
    recorder._native_recorder = cast(Any, native_recorder)
    recorder._capture_session = capture_session
    recorder._native_drain_thread = cast(Any, object())

    def _finish_drain_on_retry(**kwargs: object) -> None:
        nonlocal drain_stop_calls
        del kwargs
        drain_stop_calls += 1
        if drain_stop_calls == 2:
            recorder._native_drain_thread = None

    def _interrupt_first_drain_check() -> bool:
        nonlocal drain_checks
        drain_checks += 1
        if drain_checks == 1:
            raise interrupt
        return True

    monkeypatch.setattr(
        recorder,
        "_native_drain_is_quiesced",
        _interrupt_first_drain_check,
    )
    monkeypatch.setattr(recorder, "_stop_native_drain", _finish_drain_on_retry)

    with pytest.raises(
        KeyboardInterrupt,
        match="drain state interrupted",
    ) as exc_info:
        recorder.stop()

    assert exc_info.value is interrupt
    assert drain_checks == 2
    assert drain_stop_calls == 2
    assert native_recorder.closed is True
    assert native_recorder.abandoned is False
    assert recorder.needs_cleanup is False
    assert recorder._lifecycle_state == "idle"


def test_interrupted_native_drain_start_publishes_cleanup_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_thread_type = threading.Thread
    native_recorder = _InspectableNativeRecorder()

    class _InterruptingThread:
        def __init__(
            self,
            *,
            target: Callable[..., None],
            args: tuple[object, ...],
            name: str,
            daemon: bool,
        ) -> None:
            self._thread = real_thread_type(
                target=target,
                args=args,
                name=name,
                daemon=daemon,
            )

        @property
        def ident(self) -> int | None:
            return self._thread.ident

        def start(self) -> None:
            self._thread.start()
            raise KeyboardInterrupt("interrupted after native drain start")

        def join(self) -> None:
            self._thread.join()

        def is_alive(self) -> bool:
            return self._thread.is_alive()

    monkeypatch.setattr(recorder_module.threading, "Thread", _InterruptingThread)
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)

    with pytest.raises(
        KeyboardInterrupt,
        match="interrupted after native drain start",
    ):
        recorder._start_native_drain(cast(Any, native_recorder))

    assert recorder._native_drain_thread is not None
    recorder._stop_native_drain()
    assert recorder._native_drain_thread is None
    assert recorder._native_drain_done_event is None


def test_native_drain_records_base_exception_and_signals_completion() -> None:
    interrupt = SystemExit("native read exited")

    class _FailingNativeRecorder(_InspectableNativeRecorder):
        def read(self) -> object | None:
            raise interrupt

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    done_event = threading.Event()
    recorder._native_drain_loop(
        cast(Any, _FailingNativeRecorder()),
        threading.Event(),
        threading.Event(),
        done_event,
    )

    assert done_event.is_set()
    assert len(recorder._native_drain_failures) == 1
    assert isinstance(recorder._native_drain_failures[0].__cause__, SystemExit)


def test_native_drain_abort_skips_remaining_native_reads() -> None:
    class _UnexpectedReadRecorder(_InspectableNativeRecorder):
        def read(self) -> object | None:
            raise AssertionError("native ring should not be read after abort")

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    abort_event = threading.Event()
    done_event = threading.Event()
    abort_event.set()

    recorder._native_drain_loop(
        cast(Any, _UnexpectedReadRecorder()),
        threading.Event(),
        abort_event,
        done_event,
    )

    assert done_event.is_set()
    assert recorder._native_drain_failures == []


def test_failed_start_does_not_clobber_existing_output_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "existing.wav"
    original_bytes = b"keep-this-audio"
    output_path.write_bytes(original_bytes)

    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(capture_module, "_get_tap_format", _stub_tap_format)
    def _fail_create_aggregate(tap_uid: str, name: str, out: object = None) -> int:
        raise OSError("aggregate failed")

    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        _fail_create_aggregate,
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
            session.started = False
            session.io_proc_destroyed = True
            session.aggregate_device_destroyed = True

    recorder = AudioRecorder(123, output_path)
    recorder._capture_engine = cast(Any, _StartFailingCaptureEngine())

    with pytest.raises(OSError, match="device start failed"):
        recorder.start()

    assert output_path.read_bytes() == original_bytes
    assert list(tmp_path.glob(".existing.wav.*.tmp")) == []
    assert recorder._lifecycle_state == "idle"


def test_failed_start_abandons_native_state_when_io_proc_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    abandoned_captures: list[tuple[capture_module._TapCaptureSession, object]] = []
    native_recorder = _InspectableNativeRecorder()
    capture_session = capture_module._TapCaptureSession(55, ctypes.c_void_p(77))

    class _CaptureEngine:
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
            )

        def open_tap_capture(
            self,
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> capture_module._TapCaptureSession:
            del tap_id, callback, client_data
            return capture_session

        def close(self, session: capture_module._TapCaptureSession) -> None:
            del session
            raise OSError("IOProc cleanup failed")

    monkeypatch.setattr(
        recorder_module,
        "NativeCoreAudioRecorder",
        lambda **kwargs: native_recorder,
    )
    monkeypatch.setattr(
        recorder_module,
        "_ABANDONED_NATIVE_CAPTURES",
        abandoned_captures,
    )

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._capture_engine = cast(Any, _CaptureEngine())
    monkeypatch.setattr(
        recorder._worker,
        "start",
        lambda config: (_ for _ in ()).throw(
            KeyboardInterrupt("worker start interrupted")
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="worker start interrupted") as exc_info:
        recorder.start()

    assert native_recorder.closed is False
    assert native_recorder.abandoned is True
    assert abandoned_captures == [(capture_session, native_recorder)]
    assert any(
        "Retained native recorder state" in note
        for note in exc_info.value.__notes__
    )


def test_failed_start_retains_recovered_capture_until_native_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_recorder = _InspectableNativeRecorder()
    capture_session = capture_module._TapCaptureSession(55, ctypes.c_void_p(77))
    append_calls = 0

    class _TwiceFailingList(list[tuple[object, object]]):
        def append(self, item: tuple[object, object]) -> None:
            nonlocal append_calls
            append_calls += 1
            if append_calls <= 2:
                raise RuntimeError("retention publication failed")
            super().append(item)

    abandoned_captures = _TwiceFailingList()

    class _CaptureEngine:
        failed_capture_session = capture_session

        @staticmethod
        def describe_tap_stream(tap_id: int) -> capture_module._TapStreamFormat:
            del tap_id
            return capture_module._TapStreamFormat(
                48_000.0,
                2,
                16,
                False,
                bytes_per_frame=4,
            )

        @staticmethod
        def open_tap_capture(
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> capture_module._TapCaptureSession:
            del tap_id, callback, client_data
            raise OSError("capture ownership publication failed")

        @staticmethod
        def close(session: capture_module._TapCaptureSession) -> None:
            session.started = False
            session.aggregate_device_destroyed = True

    monkeypatch.setattr(
        recorder_module,
        "NativeCoreAudioRecorder",
        lambda **kwargs: native_recorder,
    )
    monkeypatch.setattr(
        recorder_module,
        "_ABANDONED_NATIVE_CAPTURES",
        abandoned_captures,
    )

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._capture_engine = cast(Any, _CaptureEngine())

    with pytest.raises(OSError, match="capture ownership publication failed"):
        recorder.start()

    assert append_calls == 2
    assert native_recorder.closed is False
    assert native_recorder.abandoned is False
    assert recorder._native_recorder is native_recorder
    assert recorder._capture_session is capture_session
    assert recorder.needs_cleanup is True
    assert recorder._lifecycle_state == "cleanup_failed"

    with pytest.raises(RuntimeError, match="Retained native recorder state"):
        recorder.stop()

    assert append_calls == 3
    assert abandoned_captures == [(capture_session, native_recorder)]
    assert native_recorder.closed is False
    assert native_recorder.abandoned is True
    assert recorder._native_recorder is None


def test_start_cleans_capture_after_return_handoff_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("capture return handoff interrupted")
    native_recorder = _InspectableNativeRecorder()
    cleanup_calls: list[str] = []

    class _InterruptAfterReturnEngine(capture_module._TapCaptureEngine):
        def open_tap_capture(
            self,
            tap_id: int,
            callback: object,
            client_data: object | None = None,
        ) -> capture_module._TapCaptureSession:
            super().open_tap_capture(tap_id, callback, client_data)
            raise interrupt

    def _create_io_proc(
        device_id: int,
        callback: object,
        client_data: object,
        io_proc_id: object,
    ) -> int:
        del callback, client_data
        pointer = ctypes.cast(cast(Any, io_proc_id), ctypes.POINTER(ctypes.c_void_p))
        pointer[0] = ctypes.c_void_p(77)
        cleanup_calls.append(f"create:{device_id}")
        return 0

    monkeypatch.setattr(capture_module, "_get_tap_format", _stub_tap_format)
    monkeypatch.setattr(capture_module, "_get_tap_uid", lambda tap_id: "tap-uid")
    monkeypatch.setattr(
        capture_module,
        "_create_aggregate_device_for_tap",
        lambda tap_uid, name, out=None: 55,
    )
    monkeypatch.setattr(
        capture_module,
        "_AudioDeviceCreateIOProcID",
        _create_io_proc,
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_io_proc",
        lambda device_id, io_proc_id: cleanup_calls.append(
            f"destroy-io:{device_id}:{io_proc_id.value}"
        ),
    )
    monkeypatch.setattr(
        capture_module,
        "_destroy_aggregate_device",
        lambda device_id: cleanup_calls.append(f"destroy-device:{device_id}"),
    )
    monkeypatch.setattr(
        recorder_module,
        "NativeCoreAudioRecorder",
        lambda **kwargs: native_recorder,
    )

    engine = _InterruptAfterReturnEngine()
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._capture_engine = engine

    with pytest.raises(
        KeyboardInterrupt,
        match="capture return handoff interrupted",
    ) as exc_info:
        recorder.start()

    assert exc_info.value is interrupt
    assert cleanup_calls == ["create:55", "destroy-io:55:77", "destroy-device:55"]
    assert native_recorder.closed is True
    assert native_recorder.abandoned is False
    assert engine.failed_capture_session is None
    assert recorder.needs_cleanup is False
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
            session.started = False
            session.io_proc_destroyed = True
            session.aggregate_device_destroyed = True

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
        lambda tap_uid, name, out=None: 42,
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


def test_start_preserves_primary_when_lifecycle_publication_is_interrupted() -> None:
    primary = OSError("stream description failed")
    interrupt = KeyboardInterrupt("lifecycle publication interrupted")

    class _InterruptingLock:
        def __init__(self) -> None:
            self.enter_calls = 0

        def __enter__(self) -> None:
            self.enter_calls += 1
            if self.enter_calls == 2:
                raise interrupt

        def __exit__(self, *args: object) -> None:
            del args

    class _StartFailingCaptureEngine:
        @staticmethod
        def describe_tap_stream(tap_id: int) -> capture_module._TapStreamFormat:
            del tap_id
            raise primary

    lifecycle_lock = _InterruptingLock()
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._capture_engine = cast(Any, _StartFailingCaptureEngine())
    recorder._lifecycle_lock = cast(Any, lifecycle_lock)

    with pytest.raises(OSError, match="stream description failed") as exc_info:
        recorder.start()

    assert exc_info.value is primary
    assert lifecycle_lock.enter_calls == 3
    assert any(
        "lifecycle publication interrupted" in note
        for note in exc_info.value.__notes__
    )
    assert recorder.needs_cleanup is False
    assert recorder._lifecycle_state == "idle"


def test_start_restores_terminal_state_after_lifecycle_claim_interrupt() -> None:
    interrupt = KeyboardInterrupt("interrupted after start claim")

    class _InterruptingCaptureEngine:
        @staticmethod
        def describe_tap_stream(tap_id: int) -> capture_module._TapStreamFormat:
            del tap_id
            raise interrupt

    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._capture_engine = cast(Any, _InterruptingCaptureEngine())

    with pytest.raises(
        KeyboardInterrupt,
        match="interrupted after start claim",
    ) as exc_info:
        recorder.start()

    assert exc_info.value is interrupt
    assert recorder.is_recording is False
    assert recorder.needs_cleanup is False
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


def test_worker_start_closes_fd_and_temp_file_on_base_exception(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_fds: list[int] = []

    def _interrupt_fdopen(fd: int, mode: str) -> Any:
        del mode
        opened_fds.append(fd)
        raise KeyboardInterrupt("interrupted while opening output")

    monkeypatch.setattr(worker_module.os, "fdopen", _interrupt_fdopen)
    worker = _make_worker()
    config = _make_worker_config(output_path=tmp_path / "recording.wav")

    with pytest.raises(KeyboardInterrupt, match="interrupted while opening output"):
        worker.start(config)

    assert len(opened_fds) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened_fds[0])
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []
    assert worker.thread is None


def test_worker_start_preserves_primary_when_temp_unlink_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_fds: list[int] = []
    worker = _make_worker()

    def _interrupt_fdopen(fd: int, mode: str) -> Any:
        del mode
        opened_fds.append(fd)
        raise KeyboardInterrupt("fdopen interrupted")

    def _fail_unlink(path: Path) -> None:
        del path
        raise PermissionError("startup unlink denied")

    monkeypatch.setattr(worker_module.os, "fdopen", _interrupt_fdopen)
    monkeypatch.setattr(worker, "_unlink_path", _fail_unlink)

    with pytest.raises(KeyboardInterrupt, match="fdopen interrupted") as exc_info:
        worker.start(_make_worker_config(output_path=tmp_path / "recording.wav"))

    assert len(opened_fds) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened_fds[0])
    assert any(
        "Cleanup failure while starting audio worker" in note
        and "startup unlink denied" in note
        for note in exc_info.value.__notes__
    )

    temporary_files = list(tmp_path.glob(".recording.wav.*.tmp"))
    assert len(temporary_files) == 1
    temporary_files[0].unlink()


def test_recorder_retries_worker_state_after_startup_temp_cleanup_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingThread:
        ident = None

        def __init__(self, **kwargs: object) -> None:
            del kwargs

        @staticmethod
        def start() -> None:
            raise RuntimeError("thread start failed")

        @staticmethod
        def join() -> None:
            raise AssertionError("never-started thread should not be joined")

        @staticmethod
        def is_alive() -> bool:
            return False

    worker = _make_worker()
    real_unlink = worker._unlink_path

    def _fail_unlink(path: Path) -> None:
        del path
        raise PermissionError("startup unlink denied")

    monkeypatch.setattr(worker_module.threading, "Thread", _FailingThread)
    monkeypatch.setattr(worker, "_unlink_path", _fail_unlink)

    with pytest.raises(RuntimeError, match="thread start failed"):
        worker.start(_make_worker_config(output_path=tmp_path / "recording.wav"))

    assert worker.needs_cleanup is True
    assert len(list(tmp_path.glob(".recording.wav.*.tmp"))) == 1

    monkeypatch.setattr(worker, "_unlink_path", real_unlink)
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._worker = worker
    assert recorder.needs_cleanup is True

    recorder.stop()

    assert recorder.needs_cleanup is False
    assert recorder._lifecycle_state == "idle"
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_worker_start_preserves_primary_when_raw_fd_close_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_fds: list[int] = []
    real_close = os.close

    def _interrupt_fdopen(fd: int, mode: str) -> Any:
        del mode
        opened_fds.append(fd)
        raise KeyboardInterrupt("fdopen interrupted")

    def _fail_close(fd: int) -> None:
        del fd
        raise PermissionError("raw close denied")

    monkeypatch.setattr(worker_module.os, "fdopen", _interrupt_fdopen)
    monkeypatch.setattr(worker_module.os, "close", _fail_close)
    worker = _make_worker()

    with pytest.raises(KeyboardInterrupt, match="fdopen interrupted") as exc_info:
        worker.start(_make_worker_config(output_path=tmp_path / "recording.wav"))

    assert len(opened_fds) == 1
    assert any(
        "Cleanup failure while starting audio worker" in note
        and "raw close denied" in note
        for note in exc_info.value.__notes__
    )
    real_close(opened_fds[0])
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_worker_start_reclaims_thread_when_start_is_interrupted(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_thread_type = threading.Thread
    created_threads: list[Any] = []

    class _InterruptingThread:
        def __init__(
            self,
            *,
            target: Callable[..., None],
            args: tuple[object, ...],
            name: str,
            daemon: bool,
        ) -> None:
            self._thread = real_thread_type(
                target=target,
                args=args,
                name=name,
                daemon=daemon,
            )
            self.daemon = daemon
            created_threads.append(self)

        def start(self) -> None:
            self._thread.start()
            raise KeyboardInterrupt("interrupted after thread start")

        def join(self) -> None:
            self._thread.join()

        def is_alive(self) -> bool:
            return self._thread.is_alive()

    monkeypatch.setattr(worker_module.threading, "Thread", _InterruptingThread)
    worker = _make_worker()
    config = _make_worker_config(output_path=tmp_path / "recording.wav")

    with pytest.raises(KeyboardInterrupt, match="interrupted after thread start"):
        worker.start(config)

    assert len(created_threads) == 1
    interrupted_thread = created_threads[0]
    assert interrupted_thread.is_alive() is False
    assert worker.thread is None
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_worker_start_retries_interrupted_cleanup_sentinel(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_thread_type = threading.Thread
    real_queue_type = worker_module.queue.SimpleQueue
    cleanup_interrupt = KeyboardInterrupt("cleanup sentinel interrupted")
    created_threads: list[Any] = []
    sentinel_puts = 0

    class _FailingStartThread:
        def __init__(
            self,
            *,
            target: Callable[..., None],
            args: tuple[object, ...],
            name: str,
            daemon: bool,
        ) -> None:
            self._thread = real_thread_type(
                target=target,
                args=args,
                name=name,
                daemon=daemon,
            )
            created_threads.append(self)

        @property
        def ident(self) -> int | None:
            return self._thread.ident

        def start(self) -> None:
            self._thread.start()
            raise OSError("thread start wrapper failed")

        def join(self) -> None:
            self._thread.join()

        def is_alive(self) -> bool:
            return self._thread.is_alive()

    class _InterruptingQueue:
        def __init__(self) -> None:
            self._queue = real_queue_type()

        def put(self, item: object) -> None:
            nonlocal sentinel_puts
            if item is None:
                sentinel_puts += 1
                if sentinel_puts == 1:
                    raise cleanup_interrupt
            self._queue.put(item)

        def get(self) -> object:
            return self._queue.get()

    monkeypatch.setattr(worker_module.threading, "Thread", _FailingStartThread)
    monkeypatch.setattr(worker_module.queue, "SimpleQueue", _InterruptingQueue)
    worker = _make_worker()

    with pytest.raises(OSError, match="thread start wrapper failed") as exc_info:
        worker.start(_make_worker_config(output_path=tmp_path / "recording.wav"))

    assert sentinel_puts == 2
    assert len(created_threads) == 1
    assert created_threads[0].is_alive() is False
    assert any(
        "cleanup sentinel interrupted" in note
        for note in exc_info.value.__notes__
    )
    assert worker.thread is None
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_worker_start_cleans_state_after_ownership_handoff_interrupt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("worker ownership handoff interrupted")
    real_thread_type = threading.Thread
    real_pop_all = worker_module.contextlib.ExitStack.pop_all
    created_threads: list[threading.Thread] = []

    def _create_thread(**kwargs: object) -> threading.Thread:
        thread = real_thread_type(**cast(Any, kwargs))
        created_threads.append(thread)
        return thread

    def _interrupt_after_pop_all(
        stack: worker_module.contextlib.ExitStack,
    ) -> worker_module.contextlib.ExitStack:
        real_pop_all(stack)
        raise interrupt

    monkeypatch.setattr(worker_module.threading, "Thread", _create_thread)
    monkeypatch.setattr(
        worker_module.contextlib.ExitStack,
        "pop_all",
        _interrupt_after_pop_all,
    )
    worker = _make_worker()

    with pytest.raises(
        KeyboardInterrupt,
        match="worker ownership handoff interrupted",
    ) as exc_info:
        worker.start(_make_worker_config(output_path=tmp_path / "recording.wav"))

    assert exc_info.value is interrupt
    assert len(created_threads) == 1
    assert created_threads[0].is_alive() is False
    assert worker.thread is None
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_worker_records_system_exit_from_callback_and_discards_output(
    tmp_path,
) -> None:
    callback_seen = threading.Event()
    output_path = tmp_path / "recording.wav"

    def on_buffer(buffer: PublicAudioBuffer) -> None:
        del buffer
        callback_seen.set()
        raise SystemExit("callback requested exit")

    worker = _make_worker()
    worker.start(_make_worker_config(output_path=output_path, on_buffer=on_buffer))

    assert worker.enqueue_audio_bytes(b"\x00\x01\x02\x03", 1)
    assert callback_seen.wait(timeout=1)

    with pytest.raises(
        RuntimeError,
        match="Audio buffer callback failed: callback requested exit",
    ) as exc_info:
        worker.stop()

    assert isinstance(exc_info.value.__cause__, SystemExit)
    assert not output_path.exists()
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_worker_records_keyboard_interrupt_from_converter_and_discards_output(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "recording.wav"
    worker = _make_worker()
    worker.start(
        _make_worker_config(
            output_path=output_path,
            sample_rate=48_000,
            bits_per_sample=32,
            output_bits_per_sample=16,
            convert_float_output=True,
        )
    )
    converter = worker.pcm_converter
    assert converter is not None

    def _interrupt_conversion(data: object) -> None:
        del data
        raise KeyboardInterrupt("conversion interrupted")

    monkeypatch.setattr(converter, "convert", _interrupt_conversion)
    assert worker.enqueue_audio_bytes(struct.pack("<2f", 0.5, -0.5), 1)

    with pytest.raises(
        OSError,
        match="Failed to write WAV data: conversion interrupted",
    ) as exc_info:
        worker.stop()

    assert isinstance(exc_info.value.__cause__, KeyboardInterrupt)
    assert not output_path.exists()
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


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


def test_publish_failure_discards_temp_and_preserves_existing_output(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "recording.wav"
    output_path.write_bytes(b"existing audio")
    worker = _make_worker()
    worker.start(_make_worker_config(output_path=output_path))

    def _fail_replace(source: Path, destination: Path) -> Path:
        del source, destination
        raise PermissionError("publish denied")

    monkeypatch.setattr(Path, "replace", _fail_replace)

    with pytest.raises(
        OSError,
        match="Failed to publish WAV file: publish denied",
    ) as exc_info:
        worker.stop()

    assert isinstance(exc_info.value.__cause__, PermissionError)
    assert output_path.read_bytes() == b"existing audio"
    assert list(tmp_path.glob(".recording.wav.*.tmp")) == []


def test_temp_discard_failure_does_not_mask_callback_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_seen = threading.Event()

    def on_buffer(buffer: PublicAudioBuffer) -> None:
        del buffer
        callback_seen.set()
        raise ValueError("callback boom")

    worker = _make_worker()
    worker.start(
        _make_worker_config(
            output_path=tmp_path / "recording.wav",
            on_buffer=on_buffer,
        )
    )

    def _fail_unlink(path: Path) -> None:
        del path
        raise PermissionError("unlink denied")

    monkeypatch.setattr(worker, "_unlink_path", _fail_unlink)
    assert worker.enqueue_audio_bytes(b"\x00\x01\x02\x03", 1)
    assert callback_seen.wait(timeout=1)

    with pytest.raises(
        RuntimeError,
        match="Audio buffer callback failed: callback boom",
    ) as exc_info:
        worker.stop()

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert any(
        "Failed to discard temporary WAV file: unlink denied" in note
        for note in exc_info.value.__notes__
    )

    temporary_files = list(tmp_path.glob(".recording.wav.*.tmp"))
    assert len(temporary_files) == 1
    temporary_files[0].unlink()


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


class _FormatOnlyCaptureEngine:
    """Capture engine stub that reports one format and refuses to open."""

    def __init__(self, stream_format: capture_module._TapStreamFormat) -> None:
        self._stream_format = stream_format

    def describe_tap_stream(self, tap_id: int) -> capture_module._TapStreamFormat:
        del tap_id
        return self._stream_format

    def open_tap_capture(
        self,
        tap_id: int,
        callback: object,
        client_data: object | None = None,
    ) -> object:
        del tap_id, callback, client_data
        raise AssertionError("capture should not open for unsupported formats")


@pytest.mark.parametrize("sample_rate", [float("nan"), float("inf"), float("-inf")])
def test_start_rejects_non_finite_tap_sample_rate(sample_rate: float) -> None:
    recorder = AudioRecorder(123, "recording.wav")
    recorder._capture_engine = cast(
        Any,
        _FormatOnlyCaptureEngine(
            capture_module._TapStreamFormat(
                sample_rate,
                2,
                32,
                True,
                bytes_per_frame=8,
                is_signed_integer=False,
            )
        ),
    )

    with pytest.raises(UnsupportedTapFormatError, match="Unsupported tap sample rate"):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


def test_streaming_start_rejects_non_finite_tap_sample_rate() -> None:
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    recorder._capture_engine = cast(
        Any,
        _FormatOnlyCaptureEngine(
            capture_module._TapStreamFormat(
                float("nan"),
                2,
                32,
                True,
                bytes_per_frame=8,
                is_signed_integer=False,
            )
        ),
    )

    with pytest.raises(UnsupportedTapFormatError, match="Unsupported tap sample rate"):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


@pytest.mark.parametrize("bits_per_sample", [8, 40, 48, 64])
def test_start_rejects_unwritable_integer_bit_depths(bits_per_sample: int) -> None:
    recorder = AudioRecorder(123, "recording.wav")
    recorder._capture_engine = cast(
        Any,
        _FormatOnlyCaptureEngine(
            capture_module._TapStreamFormat(
                48_000.0,
                2,
                bits_per_sample,
                False,
                bytes_per_frame=2 * (bits_per_sample // 8),
                is_signed_integer=True,
            )
        ),
    )

    with pytest.raises(
        UnsupportedTapFormatError,
        match="only 16-, 24-, and 32-bit signed integer PCM",
    ):
        recorder.start()

    assert recorder._lifecycle_state == "idle"


def test_captured_only_silence_latches_on_nonzero_audio() -> None:
    recorder = AudioRecorder(123, on_buffer=lambda buffer: None)
    assert recorder.captured_only_silence is True

    chunks = [
        _FakeNativeChunk(b"\x00" * 16, 2),
        _FakeNativeChunk(b"\x00\x00\x01\x00" * 4, 4),
    ]

    class _ChunkNativeRecorder:
        def read(self) -> object | None:
            return chunks.pop(0) if chunks else None

    recorder._drain_native_recorder(
        cast(Any, _ChunkNativeRecorder()),
        threading.Event(),
    )
    assert recorder.captured_only_silence is False

    recorder._reset_counters()
    assert recorder.captured_only_silence is True


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
