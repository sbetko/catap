"""Internal queueing and sink pipeline for audio recorders."""

from __future__ import annotations

import contextlib
import ctypes
import queue
import threading
import traceback
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from catap.bindings._audiotoolbox import (
    PcmAudioConverter,
    make_linear_pcm_asbd,
)

# Pool buffers are ctypes char arrays rather than bytearrays because
# ``ctypes.memmove`` rejects bytearray/memoryview as source or destination,
# and the worker's AudioConverter path can consume the ctypes buffers directly
# without an extra copy.
type _PoolBuffer = ctypes.Array  # ctypes.c_char * N instance
type _WorkerItem = tuple[_PoolBuffer, int, int] | None
type _WorkerFailure = OSError | RuntimeError

_DEFAULT_POOL_BUFFER_SIZE = 4096


def _combine_errors(
    summary: str, errors: list[_WorkerFailure]
) -> _WorkerFailure:
    """Annotate the primary error with summary and secondary tracebacks."""
    primary = errors[0]
    primary.add_note(summary)

    for error in errors[1:]:
        primary.add_note(
            "Additional cleanup failure:\n"
            f"{''.join(traceback.format_exception(error)).rstrip()}"
        )

    return primary


def _translate_exception(
    error_type: type[OSError] | type[RuntimeError],
    message: str,
    cause: Exception,
) -> _WorkerFailure:
    """Create an exception with an explicit cause chain."""
    try:
        raise error_type(message) from cause
    except error_type as wrapped:
        return wrapped


@dataclass(slots=True)
class _WorkerConfig:
    """Immutable worker configuration derived from recorder stream state."""

    output_path: Path | None
    on_data: Callable[[bytes, int], None] | None
    max_pending_buffers: int
    sample_rate: float
    num_channels: int
    bits_per_sample: int
    output_bits_per_sample: int
    convert_float_output: bool


@dataclass(slots=True)
class _WorkerState:
    """Worker state shared by the RT callback and background thread."""

    buffer_pool: deque[_PoolBuffer]
    work_queue: queue.Queue[_WorkerItem] | None
    output_file: BinaryIO | None = None
    wav_file: wave.Wave_write | None = None
    pcm_converter: PcmAudioConverter | None = None
    thread: threading.Thread | None = None
    failures: list[_WorkerFailure] = field(default_factory=list)
    callback_failed: bool = False
    writer_failed: bool = False


class _AudioWorker:
    """Owns the non-real-time recording pipeline and its resources."""

    def __init__(
        self,
        *,
        record_dropped_frames: Callable[[int], None],
        consume_dropped_stats: Callable[[], tuple[int, int]],
    ) -> None:
        self._record_dropped_frames = record_dropped_frames
        self._consume_dropped_stats = consume_dropped_stats
        self._state: _WorkerState | None = None

    @property
    def state(self) -> _WorkerState | None:
        return self._state

    @state.setter
    def state(self, state: _WorkerState | None) -> None:
        self._state = state

    @property
    def thread(self) -> threading.Thread | None:
        state = self._state
        return None if state is None else state.thread

    @property
    def work_queue(self) -> queue.Queue[_WorkerItem] | None:
        state = self._state
        return None if state is None else state.work_queue

    @property
    def buffer_pool(self) -> deque[_PoolBuffer] | None:
        state = self._state
        return None if state is None else state.buffer_pool

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

    def start(self, config: _WorkerConfig) -> _WorkerState:
        """Start the background worker for file writes and user callbacks."""
        if self._state is not None:
            return self._state

        state = self._create_state(config, start_thread=True)
        self._state = state
        return state

    def stop(self, state: _WorkerState | None = None) -> None:
        """Flush and stop the background worker."""
        if state is None:
            state = self._state
        if state is None:
            return

        if (
            state.work_queue is not None
            and state.thread is not None
            and state.thread.is_alive()
        ):
            state.work_queue.put(None)

        if state.thread is not None:
            state.thread.join()

        if self._state is state:
            self._state = None

        worker_errors = list(state.failures)
        dropped_buffers, dropped_frames = self._consume_dropped_stats()
        if dropped_buffers > 0:
            worker_errors.append(
                RuntimeError(
                    "Dropped "
                    f"{dropped_buffers} audio buffer(s) "
                    f"({dropped_frames} frame(s)) because the background worker "
                    "fell behind. Try a faster output path or a lighter on_data "
                    "callback."
                )
            )

        if worker_errors:
            raise _combine_errors("Failed to finalize audio worker", worker_errors)

    def acquire_pool_buffer(
        self,
        needed: int,
        state: _WorkerState | None = None,
    ) -> _PoolBuffer | None:
        """Return a ctypes buffer sized for ``needed`` bytes, or None if exhausted."""
        if state is None:
            state = self._state
        if state is None:
            return None

        try:
            buf = state.buffer_pool.pop()
        except IndexError:
            return None

        if len(buf) < needed:
            # Resize is rare (only on buffer-size change). Still technically an
            # allocation on the RT thread, but bounded to a few occurrences.
            buf = (ctypes.c_char * needed)()

        return buf

    def enqueue_audio_data(
        self,
        buf: _PoolBuffer,
        num_frames: int,
        byte_count: int,
        state: _WorkerState | None = None,
    ) -> bool:
        """Queue audio work without blocking the Core Audio callback thread."""
        if state is None:
            state = self._state
        if state is None:
            return True

        work_queue = state.work_queue
        if work_queue is None:
            state.buffer_pool.append(buf)
            return True

        try:
            work_queue.put_nowait((buf, num_frames, byte_count))
        except queue.Full:
            self._record_dropped_frames(num_frames)
            state.buffer_pool.append(buf)
            return False

        return True

    def _create_state(
        self,
        config: _WorkerConfig,
        *,
        start_thread: bool,
        include_queue: bool = True,
        pool_depth: int | None = None,
        queue_maxsize: int | None = None,
        buffer_bytes: int | None = None,
    ) -> _WorkerState:
        """Create worker-owned queueing state and optionally start the worker."""
        if start_thread and not include_queue:
            raise ValueError("start_thread requires include_queue=True")

        bytes_per_frame = max(1, config.num_channels * (config.bits_per_sample // 8))
        pool_buffer_size = max(
            _DEFAULT_POOL_BUFFER_SIZE,
            bytes_per_frame * 1024,
        )
        if buffer_bytes is not None:
            pool_buffer_size = buffer_bytes

        depth = config.max_pending_buffers if pool_depth is None else pool_depth
        queue_bound = (
            config.max_pending_buffers if queue_maxsize is None else queue_maxsize
        )
        pool_type = ctypes.c_char * pool_buffer_size
        state = _WorkerState(
            buffer_pool=deque(pool_type() for _ in range(depth)),
            work_queue=queue.Queue(maxsize=queue_bound) if include_queue else None,
        )

        with contextlib.ExitStack() as stack:
            if config.output_path is not None:
                output_file = stack.enter_context(config.output_path.open("wb"))
                wav_file = wave.open(output_file, "wb")  # noqa: SIM115
                stack.callback(wav_file.close)
                wav_file.setnchannels(config.num_channels)
                wav_file.setsampwidth(config.output_bits_per_sample // 8)
                wav_file.setframerate(int(config.sample_rate))

                pcm_converter: PcmAudioConverter | None = None
                if config.convert_float_output:
                    pcm_converter = PcmAudioConverter(
                        make_linear_pcm_asbd(
                            config.sample_rate,
                            config.num_channels,
                            config.bits_per_sample,
                            is_float=True,
                        ),
                        make_linear_pcm_asbd(
                            config.sample_rate,
                            config.num_channels,
                            config.output_bits_per_sample,
                            is_float=False,
                        ),
                    )
                    stack.callback(pcm_converter.close)

                state.output_file = output_file
                state.wav_file = wav_file
                state.pcm_converter = pcm_converter

            if start_thread:
                thread = threading.Thread(
                    target=self._worker_loop,
                    args=(state, config.on_data),
                    name="catap-audio-worker",
                    daemon=False,
                )
                thread.start()
                state.thread = thread

            stack.pop_all()

        return state

    def _worker_loop(
        self,
        state: _WorkerState,
        on_data: Callable[[bytes, int], None] | None,
    ) -> None:
        """Drain queued audio outside the Core Audio callback thread."""
        assert state.work_queue is not None

        try:
            while True:
                item = state.work_queue.get()
                if item is None:
                    break

                buf, num_frames, byte_count = item

                try:
                    if on_data is not None and not state.callback_failed:
                        # User may stash the buffer, so hand them a private copy
                        # rather than a pool-owned view.
                        try:
                            on_data(bytes(memoryview(buf)[:byte_count]), num_frames)
                        except Exception as exc:
                            state.callback_failed = True
                            state.failures.append(
                                _translate_exception(
                                    RuntimeError,
                                    f"Audio data callback failed: {exc}",
                                    exc,
                                )
                            )

                    if state.wav_file is not None and not state.writer_failed:
                        try:
                            if state.pcm_converter is not None:
                                state.pcm_converter.convert(buf, byte_count)
                                output_data = state.pcm_converter.output_view()
                            else:
                                output_data = memoryview(buf)[:byte_count]
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
                    state.buffer_pool.append(buf)
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
