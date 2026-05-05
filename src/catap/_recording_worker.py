"""Internal queueing and sink pipeline for audio recorders."""

from __future__ import annotations

import contextlib
import os
import queue
import tempfile
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, TypeAlias

from catap._recording_support import _combine_errors, _translate_exception
from catap.audio_buffer import (
    AudioBuffer,
    AudioStreamFormat,
)
from catap.bindings._audiotoolbox import (
    PcmAudioConverter,
    make_linear_pcm_asbd,
)

_WorkerFailure: TypeAlias = OSError | RuntimeError


@dataclass(slots=True)
class _AudioWorkItem:
    """Owned audio bytes queued for the background worker."""

    data: bytes
    num_frames: int
    input_sample_time: float | None


_WorkerItem: TypeAlias = _AudioWorkItem | None


@dataclass(slots=True)
class _WorkerConfig:
    """Immutable worker configuration derived from recorder stream state."""

    output_path: Path | None
    on_buffer: Callable[[AudioBuffer], None] | None
    max_pending_buffers: int
    stream_format: AudioStreamFormat
    output_bits_per_sample: int
    convert_float_output: bool


@dataclass(slots=True)
class _WorkerState:
    """Worker state owned by the native drain and background thread."""

    work_queue: queue.SimpleQueue[_WorkerItem]
    pending_slots: threading.BoundedSemaphore
    output_file: BinaryIO | None = None
    wav_file: wave.Wave_write | None = None
    pcm_converter: PcmAudioConverter | None = None
    thread: threading.Thread | None = None
    final_output_path: Path | None = None
    temporary_output_path: Path | None = None
    failures: list[_WorkerFailure] = field(default_factory=list)
    # Keep the two sink failures independent so one broken sink does not
    # silence the other for the rest of the capture.
    callback_failed: bool = False
    writer_failed: bool = False


class _AudioWorker:
    """Owns the non-real-time recording pipeline and its resources."""

    def __init__(
        self,
        *,
        record_accepted_frames: Callable[[int], None],
        record_dropped_frames: Callable[[int], None],
        consume_dropped_stats: Callable[[], tuple[int, int]],
    ) -> None:
        self._record_accepted_frames = record_accepted_frames
        self._record_dropped_frames = record_dropped_frames
        self._consume_dropped_stats = consume_dropped_stats
        self._state: _WorkerState | None = None

    @property
    def thread(self) -> threading.Thread | None:
        state = self._state
        return None if state is None else state.thread

    @property
    def output_file(self) -> BinaryIO | None:
        state = self._state
        return None if state is None else state.output_file

    @property
    def wav_file(self) -> wave.Wave_write | None:
        state = self._state
        return None if state is None else state.wav_file

    @property
    def pcm_converter(self) -> PcmAudioConverter | None:
        state = self._state
        return None if state is None else state.pcm_converter

    def start(self, config: _WorkerConfig) -> None:
        """Start the background worker for file writes and user callbacks."""
        if self._state is not None:
            raise RuntimeError("Audio worker already started")

        state = self._create_state(config)
        self._state = state

    def stop(self, *, publish: bool = True) -> None:
        """Flush and stop the background worker."""
        state = self._state
        if state is None:
            return

        if state.thread is not None and state.thread.is_alive():
            state.work_queue.put(None)

        if state.thread is not None:
            state.thread.join()

        self._state = None

        worker_errors = list(state.failures)
        dropped_buffers, dropped_frames = self._consume_dropped_stats()
        if dropped_buffers > 0:
            worker_errors.append(
                RuntimeError(
                    "Dropped "
                    f"{dropped_buffers} audio buffer(s) "
                    f"({dropped_frames} frame(s)) because the background worker "
                    "fell behind. Try a faster output path or a lighter on_buffer "
                    "callback."
                )
            )

        if state.temporary_output_path is not None:
            if worker_errors or not publish:
                self._discard_temporary_output(state)
            else:
                try:
                    assert state.final_output_path is not None
                    state.temporary_output_path.replace(state.final_output_path)
                except OSError as exc:
                    worker_errors.append(
                        _translate_exception(
                            OSError,
                            f"Failed to publish WAV file: {exc}",
                            exc,
                        )
                    )

        if worker_errors:
            raise _combine_errors("Failed to finalize audio worker", worker_errors)

    def enqueue_audio_bytes(
        self,
        data: bytes,
        num_frames: int,
        input_sample_time: float | None = None,
    ) -> bool:
        """Queue owned audio bytes from a non-real-time producer."""
        state = self._state
        if state is None:
            return True

        if not state.pending_slots.acquire(blocking=False):
            self._record_dropped_frames(num_frames)
            return False

        item = _AudioWorkItem(
            data=data,
            num_frames=num_frames,
            input_sample_time=input_sample_time,
        )
        state.work_queue.put(item)
        return True

    def _create_state(self, config: _WorkerConfig) -> _WorkerState:
        """Create worker-owned queueing state and start the worker thread."""
        stream_format = config.stream_format
        state = _WorkerState(
            work_queue=queue.SimpleQueue(),
            pending_slots=threading.BoundedSemaphore(config.max_pending_buffers),
        )

        with contextlib.ExitStack() as stack:
            if config.output_path is not None:
                fd, temporary_name = tempfile.mkstemp(
                    dir=config.output_path.parent,
                    prefix=f".{config.output_path.name}.",
                    suffix=".tmp",
                )
                try:
                    output_file = stack.enter_context(os.fdopen(fd, "wb"))
                except Exception:
                    os.close(fd)
                    raise
                temporary_output_path = Path(temporary_name)
                stack.callback(self._unlink_path, temporary_output_path)

                wav_file = wave.open(output_file, "wb")  # noqa: SIM115
                stack.callback(wav_file.close)
                wav_file.setnchannels(stream_format.num_channels)
                wav_file.setsampwidth(config.output_bits_per_sample // 8)
                wav_file.setframerate(int(stream_format.sample_rate))

                pcm_converter: PcmAudioConverter | None = None
                if config.convert_float_output:
                    pcm_converter = PcmAudioConverter(
                        make_linear_pcm_asbd(
                            stream_format.sample_rate,
                            stream_format.num_channels,
                            stream_format.bits_per_sample,
                            is_float=True,
                        ),
                        make_linear_pcm_asbd(
                            stream_format.sample_rate,
                            stream_format.num_channels,
                            config.output_bits_per_sample,
                            is_float=False,
                        ),
                    )
                    stack.callback(pcm_converter.close)

                state.output_file = output_file
                state.wav_file = wav_file
                state.pcm_converter = pcm_converter
                state.final_output_path = config.output_path
                state.temporary_output_path = temporary_output_path

            thread = threading.Thread(
                target=self._worker_loop,
                args=(state, config.on_buffer, stream_format),
                name="catap-audio-worker",
                daemon=False,
            )
            thread.start()
            state.thread = thread

            stack.pop_all()

        return state

    @staticmethod
    def _unlink_path(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    def _discard_temporary_output(self, state: _WorkerState) -> None:
        if state.temporary_output_path is not None:
            self._unlink_path(state.temporary_output_path)

    def _worker_loop(
        self,
        state: _WorkerState,
        on_buffer: Callable[[AudioBuffer], None] | None,
        stream_format: AudioStreamFormat,
    ) -> None:
        """Drain queued audio outside the Core Audio callback thread."""
        try:
            while True:
                item = state.work_queue.get()
                if item is None:
                    break

                data = item.data
                num_frames = item.num_frames
                self._record_accepted_frames(num_frames)

                try:
                    if on_buffer is not None and not state.callback_failed:
                        try:
                            on_buffer(
                                AudioBuffer(
                                    data=data,
                                    frame_count=num_frames,
                                    format=stream_format,
                                    input_sample_time=item.input_sample_time,
                                )
                            )
                        except Exception as exc:
                            state.callback_failed = True
                            state.failures.append(
                                _translate_exception(
                                    RuntimeError,
                                    f"Audio buffer callback failed: {exc}",
                                    exc,
                                )
                            )

                    if state.wav_file is not None and not state.writer_failed:
                        try:
                            if state.pcm_converter is not None:
                                state.pcm_converter.convert(data)
                                output_data = state.pcm_converter.output_view()
                            else:
                                output_data = data
                            state.wav_file.writeframesraw(output_data)
                        except Exception as exc:
                            state.writer_failed = True
                            state.failures.append(
                                _translate_exception(
                                    OSError,
                                    f"Failed to write WAV data: {exc}",
                                    exc,
                                )
                            )
                finally:
                    state.pending_slots.release()
        finally:
            self._close_resources(state)

    def _close_resources(self, state: _WorkerState) -> None:
        """Close worker-owned resources and retain any failures."""
        if state.wav_file is not None:
            try:
                state.wav_file.close()
            except Exception as exc:
                state.failures.append(
                    _translate_exception(
                        OSError,
                        f"Failed to finalize WAV file: {exc}",
                        exc,
                    )
                )
            finally:
                state.wav_file = None

        if state.output_file is not None:
            try:
                state.output_file.close()
            except Exception as exc:
                state.failures.append(
                    _translate_exception(
                        OSError,
                        f"Failed to close output file: {exc}",
                        exc,
                    )
                )
            finally:
                state.output_file = None

        if state.pcm_converter is not None:
            try:
                state.pcm_converter.close()
            except Exception as exc:
                state.failures.append(
                    _translate_exception(
                        OSError,
                        f"Failed to dispose PCM converter: {exc}",
                        exc,
                    )
                )
            finally:
                state.pcm_converter = None
