"""Synchronized multi-track capture through one aggregate device.

One aggregate device combines several taps (and optionally one hardware
input device) so every track shares a single clock. The native IOProc
queues each callback's buffers as an atomic group tagged with a buffer
index, and one background worker per track turns its stream into a WAV
file or callback feed.

Track order matches the aggregate's input stream order: the input device's
streams first (when present), then taps in creation order.
"""

from __future__ import annotations

import contextlib
import ctypes
import math
import os
import shutil
import stat
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from catap import recorder as _recorder_module
from catap._capture_engine import (
    _TapCaptureEngine,
    _TapCaptureSession,
    _TapStreamFormat,
)
from catap._native_coreaudio import (
    CATAP_MAX_BUFFERS,
    NativeAudioChunk,
    NativeCoreAudioRecorder,
)
from catap._recording_support import (
    _DEFAULT_MAX_PENDING_BUFFERS,
    _add_secondary_failure,
    _combine_errors,
    _translate_exception,
    _validate_max_pending_buffers,
)
from catap._recording_worker import _AudioWorker, _WorkerConfig
from catap.audio_buffer import AudioBuffer, AudioStreamFormat, _format_id_to_fourcc
from catap.bindings._coreaudio import kAudioHardwareBadObjectError
from catap.bindings.tap import AudioTapNotFoundError
from catap.drift import (
    DriftCompensationQuality,
    _validate_drift_compensation_quality,
)
from catap.events import AudioPropertyEvent, kAudioTapPropertyFormat
from catap.recorder import (
    _NATIVE_DRAIN_IDLE_INTERVAL_SECONDS,
    _NATIVE_FAILURE_POLL_INTERVAL_SECONDS,
    _NATIVE_SLOT_FRAME_CAPACITY,
    _CompositeDriftWatch,
    _native_recorder_stat_errors,
    _validate_tap_stream_format,
)


@dataclass(slots=True)
class _OutputPublishEntry:
    """Retryable filesystem state for one track's final destination."""

    track_index: int
    final_path: Path
    original_existed: bool | None = None
    original_signature: tuple[int, int, int, int] | None = None
    backup_path: Path | None = None
    backup_ready: bool = False
    published_removed: bool = False
    restored: bool = False


@dataclass(slots=True)
class _OutputPublishTransaction:
    """In-memory transaction retained until commit or rollback completes."""

    entries: list[_OutputPublishEntry]
    phase: str = "preparing"
    attempted_tracks: set[int] = field(default_factory=set)
    published_tracks: set[int] = field(default_factory=set)


def _output_path_key(path: Path) -> str:
    """Return a conservative key for aliases on common macOS filesystems."""
    resolved = path.expanduser().resolve(strict=False)
    return unicodedata.normalize("NFC", str(resolved)).casefold()


def _validate_distinct_output_paths(paths: Sequence[Path | None]) -> None:
    """Reject destinations that can overwrite the same filesystem entry."""
    keyed_paths: dict[str, tuple[int, Path]] = {}
    concrete_paths: list[tuple[int, Path]] = []
    for index, path in enumerate(paths):
        if path is None:
            continue
        try:
            key = _output_path_key(path)
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"Could not resolve output path for track {index}: {path}: {exc}"
            ) from exc
        previous = keyed_paths.get(key)
        if previous is not None:
            previous_index, previous_path = previous
            raise ValueError(
                "Output paths for tracks "
                f"{previous_index} and {index} refer to the same destination: "
                f"{previous_path} and {path}"
            )
        for previous_index, previous_path in concrete_paths:
            try:
                aliases_existing_file = path.samefile(previous_path)
            except OSError:
                aliases_existing_file = False
            if aliases_existing_file:
                raise ValueError(
                    "Output paths for tracks "
                    f"{previous_index} and {index} refer to the same destination: "
                    f"{previous_path} and {path}"
                )
        keyed_paths[key] = (index, path)
        concrete_paths.append((index, path))


class MultitrackAudioRecorder:
    """Record several Core Audio taps (plus one optional input device) as
    sample-synchronized tracks.

    Compared to ``AudioRecorder`` this class trades some of the deep
    interruption-retry hardening for a straightforward list-based
    lifecycle; cleanup is still ordered, native memory is still retained
    when Core Audio may reference it, and any track's failure discards
    every track's output rather than publishing a desynchronized session.
    """

    def __init__(
        self,
        tap_ids: Sequence[int],
        output_paths: Sequence[str | Path | None],
        *,
        on_track_buffer: Callable[[int, AudioBuffer], None] | None = None,
        max_pending_buffers: int = _DEFAULT_MAX_PENDING_BUFFERS,
        input_device_uid: str | None = None,
        input_stream_count: int = 0,
        drift_compensation_quality: DriftCompensationQuality | None = None,
    ) -> None:
        """Initialize the recorder.

        Args:
            tap_ids: AudioObjectIDs of the taps to record
            output_paths: One WAV path (or None) per track, in track order:
                the input device's streams first when present, then taps
            on_track_buffer: Optional callback receiving
                ``(track_index, AudioBuffer)`` for every captured buffer.
                Each track has its own worker thread, so callbacks for
                different tracks can run concurrently; callers sharing state
                must synchronize it. If one invocation raises, the capture is
                discarded and no further callbacks begin, though invocations
                already in flight can finish. Required for any track whose
                output path is None
            max_pending_buffers: Per-track queue bound before buffers are
                dropped and the capture fails on stop
            input_device_uid: Optional hardware input device (for example a
                microphone) captured as extra tracks alongside the taps
            input_stream_count: Number of input streams the input device
                contributes; required when ``input_device_uid`` is set
            drift_compensation_quality: Optional aggregate-device resampling
                quality applied to every tap. ``None`` preserves Core Audio's
                default.
        """
        if not tap_ids:
            raise ValueError("At least one tap is required")
        if (input_device_uid is None) != (input_stream_count == 0):
            raise ValueError(
                "input_device_uid and input_stream_count must be provided together"
            )
        if input_stream_count < 0:
            raise ValueError("input_stream_count must not be negative")

        track_count = input_stream_count + len(tap_ids)
        if track_count > CATAP_MAX_BUFFERS:
            raise ValueError(
                f"Multitrack capture supports up to {CATAP_MAX_BUFFERS} "
                f"tracks, got {track_count}"
            )
        if len(output_paths) != track_count:
            raise ValueError(
                f"Expected {track_count} output paths (input streams first, "
                f"then taps), got {len(output_paths)}"
            )

        normalized_paths = [Path(path) if path else None for path in output_paths]
        _validate_distinct_output_paths(normalized_paths)
        if on_track_buffer is None:
            missing = [
                index for index, path in enumerate(normalized_paths) if path is None
            ]
            if missing:
                raise ValueError(
                    "Every track needs an output path unless on_track_buffer "
                    f"is set; track(s) {missing} have neither"
                )

        self.tap_ids = list(tap_ids)
        self.output_paths = normalized_paths
        self.input_device_uid = input_device_uid
        self.input_stream_count = input_stream_count
        self.drift_compensation_quality = _validate_drift_compensation_quality(
            drift_compensation_quality
        )
        self._on_track_buffer = on_track_buffer
        self._max_pending_buffers = _validate_max_pending_buffers(max_pending_buffers)

        self._capture_engine = _TapCaptureEngine()
        self._capture_session: _TapCaptureSession | None = None
        self._aggregate_device_id: int | None = None
        self._session_owns_aggregate = False
        self._native_recorder: NativeCoreAudioRecorder | None = None
        self._drift_watch: _CompositeDriftWatch | None = None
        self._capture_failure_event = threading.Event()

        self._lifecycle_lock = threading.Lock()
        self._lifecycle_state = "idle"
        self._is_recording = False
        self._reached_recording = False
        self._must_discard = False
        self._publish_transaction: _OutputPublishTransaction | None = None

        self._stats_lock = threading.Lock()
        self._accepted_frames = [0] * track_count
        self._dropped_buffers = [0] * track_count
        self._dropped_frames = [0] * track_count
        self._track_has_audio = [False] * track_count

        self._start_tap_formats: list[_TapStreamFormat] | None = None
        self._stream_formats: list[_TapStreamFormat] = []
        self._public_formats: list[AudioStreamFormat] = []
        self._mid_capture_drift_lock = threading.Lock()
        self._mid_capture_drift: list[str] = []
        self._callback_state_lock = threading.Lock()
        self._callbacks_enabled = True

        self._drain_thread: threading.Thread | None = None
        self._drain_stop_event: threading.Event | None = None
        self._drain_abort_event: threading.Event | None = None
        self._drain_done_event: threading.Event | None = None
        self._drain_failures: list[RuntimeError] = []

        self._workers = [
            _AudioWorker(
                record_accepted_frames=self._make_accepted_recorder(index),
                record_dropped_frames=self._make_dropped_recorder(index),
                consume_dropped_stats=self._make_dropped_consumer(index),
                capture_failure_event=self._capture_failure_event,
            )
            for index in range(track_count)
        ]

    @property
    def track_count(self) -> int:
        """Number of tracks this recorder captures."""
        return len(self._workers)

    def _make_accepted_recorder(self, track_index: int) -> Callable[[int], None]:
        def record(num_frames: int) -> None:
            with self._stats_lock:
                self._accepted_frames[track_index] += num_frames

        return record

    def _make_dropped_recorder(self, track_index: int) -> Callable[[int], None]:
        def record(num_frames: int) -> None:
            with self._stats_lock:
                self._dropped_buffers[track_index] += 1
                self._dropped_frames[track_index] += num_frames
                self._must_discard = True
            self._capture_failure_event.set()

        return record

    def _make_dropped_consumer(self, track_index: int) -> Callable[[], tuple[int, int]]:
        def consume() -> tuple[int, int]:
            with self._stats_lock:
                dropped_buffers = self._dropped_buffers[track_index]
                dropped_frames = self._dropped_frames[track_index]
                self._dropped_buffers[track_index] = 0
                self._dropped_frames[track_index] = 0
            return dropped_buffers, dropped_frames

        return consume

    def _mark_capture_invalid(self) -> None:
        """Latch the all-tracks discard decision through cleanup retries."""
        with self._stats_lock:
            self._must_discard = True
        self._capture_failure_event.set()

    def _capture_is_invalid(self) -> bool:
        with self._stats_lock:
            return self._must_discard

    def _make_worker_config(self, track_index: int) -> _WorkerConfig:
        stream_format = self._public_formats[track_index]
        convert_float_output = (
            stream_format.is_float and stream_format.bits_per_sample == 32
        )
        on_buffer: Callable[[AudioBuffer], None] | None = None
        if self._on_track_buffer is not None:
            user_callback = self._on_track_buffer

            def on_buffer(
                buffer: AudioBuffer,
                _track_index: int = track_index,
                _user_callback: Callable[[int, AudioBuffer], None] = user_callback,
            ) -> None:
                with self._callback_state_lock:
                    if not self._callbacks_enabled:
                        return
                try:
                    _user_callback(_track_index, buffer)
                except BaseException:
                    # Stop every track from starting further user callbacks
                    # after one shared callback fails. Calls already in flight
                    # on another track may finish, so the capture is also
                    # latched for all-track discard.
                    with self._callback_state_lock:
                        self._callbacks_enabled = False
                    self._mark_capture_invalid()
                    raise

        return _WorkerConfig(
            output_path=self.output_paths[track_index],
            on_buffer=on_buffer,
            max_pending_buffers=self._max_pending_buffers,
            stream_format=stream_format,
            output_bits_per_sample=(
                16 if convert_float_output else stream_format.bits_per_sample
            ),
            convert_float_output=convert_float_output,
        )

    def _apply_stream_formats(self, formats: list[_TapStreamFormat]) -> None:
        """Validate and store the aggregate's per-track stream formats."""
        expected = self.track_count
        if len(formats) != expected:
            raise OSError(
                "Aggregate device exposed an unexpected stream layout: "
                f"expected {expected} input stream(s) "
                f"({self.input_stream_count} device stream(s) plus "
                f"{len(self.tap_ids)} tap(s)), got {len(formats)}"
            )
        for stream_format in formats:
            _validate_tap_stream_format(stream_format)
        self._stream_formats = formats
        self._public_formats = [
            AudioStreamFormat(
                sample_rate=stream_format.sample_rate,
                num_channels=stream_format.num_channels,
                bits_per_sample=stream_format.bits_per_sample,
                sample_type="float" if stream_format.is_float else "signed_integer",
                format_id=_format_id_to_fourcc(stream_format.format_id),
            )
            for stream_format in formats
        ]

    def _record_mid_capture_drift(self, message: str) -> None:
        with self._mid_capture_drift_lock:
            if message not in self._mid_capture_drift:
                self._mid_capture_drift.append(message)
        # The property dispatcher only wakes the owning thread. Teardown must
        # remain on the thread that later calls stop().
        self._capture_failure_event.set()

    def _on_drift_event(self, event: AudioPropertyEvent) -> None:
        baselines = self._start_tap_formats
        if baselines is None:
            return
        tap_baselines = list(zip(self.tap_ids, baselines, strict=True))
        if (
            event.selector == kAudioTapPropertyFormat
            and event.object_id in self.tap_ids
        ):
            # A format event names the affected tap; only tap-list events
            # need the full sweep to find which tap vanished.
            tap_baselines = [
                (tap_id, expected)
                for tap_id, expected in tap_baselines
                if tap_id == event.object_id
            ]
        for tap_id, expected in tap_baselines:
            try:
                current = self._capture_engine.describe_tap_stream(tap_id)
            except AudioTapNotFoundError:
                self._record_mid_capture_drift(
                    f"Tap {tap_id} disappeared during capture"
                )
                continue
            except BaseException:
                continue
            if current != expected:
                self._record_mid_capture_drift(
                    f"Tap {tap_id} stream format changed during capture "
                    f"(started as {expected}, now {current})"
                )

    def _start_drift_watch(self) -> None:
        with self._mid_capture_drift_lock:
            self._mid_capture_drift = []
        try:
            self._drift_watch = _CompositeDriftWatch(self.tap_ids, self._on_drift_event)
        except (OSError, RuntimeError, ValueError):
            self._drift_watch = None

    def _close_drift_watch(self) -> list[BaseException]:
        watch = self._drift_watch
        if watch is None:
            return []
        try:
            watch.close()
        except BaseException as exc:
            return [exc]
        self._drift_watch = None
        return []

    def _detect_tap_drift(self) -> list[BaseException]:
        """Return drift failures gathered mid-capture and at stop time."""
        baselines = self._start_tap_formats
        if baselines is None:
            return []

        with self._mid_capture_drift_lock:
            messages = list(self._mid_capture_drift)
        errors: list[BaseException] = [
            RuntimeError(f"{message}; discarding output") for message in messages
        ]

        for tap_id, expected in zip(self.tap_ids, baselines, strict=True):
            try:
                current = self._capture_engine.describe_tap_stream(tap_id)
            except AudioTapNotFoundError as exc:
                errors.append(
                    _translate_exception(
                        OSError,
                        f"Tap {tap_id} disappeared during capture; "
                        f"discarding output: {exc}",
                        exc,
                    )
                )
                continue
            except BaseException as exc:
                errors.append(
                    _translate_exception(
                        OSError,
                        f"Could not verify tap {tap_id} stream format at "
                        f"stop; discarding output: {exc}",
                        exc,
                    )
                )
                continue
            if current != expected:
                errors.append(
                    RuntimeError(
                        f"Tap {tap_id} stream format changed during capture "
                        f"(started as {expected}, now {current}); "
                        "discarding output"
                    )
                )
        return errors

    @staticmethod
    def _stream_bytes_per_frame(stream_format: _TapStreamFormat) -> int:
        return stream_format.bytes_per_frame or (
            stream_format.num_channels * (stream_format.bits_per_sample // 8)
        )

    def _create_native_recorder(self) -> NativeCoreAudioRecorder:
        frame_sizes = [
            self._stream_bytes_per_frame(stream_format)
            for stream_format in self._stream_formats
        ]
        return NativeCoreAudioRecorder(
            # Each callback consumes one slot per track, so scale the shared
            # ring to keep max_pending_buffers meaningful per track.
            slot_count=self._max_pending_buffers * self.track_count,
            slot_capacity=_NATIVE_SLOT_FRAME_CAPACITY * max(frame_sizes),
            expected_channel_count=[
                stream_format.num_channels for stream_format in self._stream_formats
            ],
            bytes_per_frame=frame_sizes,
        )

    def _drain_native_recorder(
        self,
        native_recorder: NativeCoreAudioRecorder,
        abort_event: threading.Event,
    ) -> bool:
        drained = False
        while True:
            if abort_event.is_set():
                return drained
            first_chunk = native_recorder.read()
            if first_chunk is None:
                return drained
            drained = True
            group = [first_chunk]
            for _ in range(1, self.track_count):
                chunk = native_recorder.read()
                if chunk is None:
                    raise RuntimeError(
                        "Native recorder exposed an incomplete audio buffer group"
                    )
                group.append(chunk)
            self._validate_native_group(group)
            self._enqueue_native_group(group)

    def _validate_native_group(self, group: Sequence[NativeAudioChunk]) -> None:
        """Enforce the native all-buffers/same-timeline contract again."""
        if len(group) != self.track_count:
            raise RuntimeError(
                "Native recorder reported an unexpected audio buffer group "
                f"size: expected {self.track_count}, got {len(group)}"
            )
        frame_count = group[0].frame_count
        if frame_count <= 0:
            raise RuntimeError(
                "Native recorder reported a non-positive multitrack frame count"
            )
        sample_time = group[0].input_sample_time
        if sample_time is not None and not math.isfinite(sample_time):
            raise RuntimeError(
                "Native recorder reported a non-finite input sample time"
            )

        for track_index, chunk in enumerate(group):
            if chunk.buffer_index != track_index:
                raise RuntimeError(
                    "Native audio buffer group was torn or out of order: "
                    f"expected track {track_index}, got {chunk.buffer_index}"
                )
            if chunk.frame_count != frame_count:
                raise RuntimeError(
                    "Native audio buffer group reported unequal frame counts: "
                    f"track 0 has {frame_count}, track {track_index} has "
                    f"{chunk.frame_count}"
                )
            if chunk.input_sample_time != sample_time:
                raise RuntimeError(
                    "Native audio buffer group reported inconsistent input sample times"
                )
            expected_byte_count = frame_count * self._stream_bytes_per_frame(
                self._stream_formats[track_index]
            )
            if len(chunk.data) != expected_byte_count:
                raise RuntimeError(
                    "Native audio buffer size did not match its track format: "
                    f"track {track_index} has {len(chunk.data)} byte(s), "
                    f"expected {expected_byte_count}"
                )

    def _enqueue_native_group(self, group: Sequence[NativeAudioChunk]) -> None:
        """Reserve every worker queue before admitting any track in a group."""
        for worker_index, worker in enumerate(self._workers):
            if not worker.try_reserve_audio_slot():
                for reserved_worker in self._workers[:worker_index]:
                    reserved_worker.cancel_reserved_audio_slot()
                frame_count = group[0].frame_count
                for dropped_worker in self._workers:
                    dropped_worker.record_dropped_audio_buffer(frame_count)
                self._mark_capture_invalid()
                return

        attempted_count = 0
        try:
            for track_index, (worker, chunk) in enumerate(
                zip(self._workers, group, strict=True)
            ):
                attempted_count = track_index + 1
                worker.enqueue_reserved_audio_bytes(
                    chunk.data,
                    chunk.frame_count,
                    chunk.input_sample_time,
                )
        except BaseException:
            # The attempted queue operation has ambiguous completion, so keep
            # its reservation. Slots for workers not yet attempted are known
            # to be unused and can be returned safely.
            for reserved_worker in self._workers[attempted_count:]:
                reserved_worker.cancel_reserved_audio_slot()
            self._mark_capture_invalid()
            raise

        for track_index, chunk in enumerate(group):
            if not self._track_has_audio[track_index] and chunk.data.strip(b"\x00"):
                with self._stats_lock:
                    self._track_has_audio[track_index] = True

    def _drain_loop(
        self,
        native_recorder: NativeCoreAudioRecorder,
        stop_event: threading.Event,
        abort_event: threading.Event,
        done_event: threading.Event,
    ) -> None:
        next_failure_poll = 0.0
        try:
            while True:
                drained = self._drain_native_recorder(native_recorder, abort_event)
                if abort_event.is_set():
                    return
                if stop_event.is_set():
                    return
                now = time.monotonic()
                if now >= next_failure_poll:
                    self._poll_native_capture_failure(native_recorder)
                    next_failure_poll = now + _NATIVE_FAILURE_POLL_INTERVAL_SECONDS
                if not drained:
                    stop_event.wait(_NATIVE_DRAIN_IDLE_INTERVAL_SECONDS)
        except BaseException as exc:
            failure = _translate_exception(
                RuntimeError,
                f"Native recorder drain failed: {exc}",
                exc,
            )
            assert isinstance(failure, RuntimeError)
            self._drain_failures.append(failure)
            self._mark_capture_invalid()
        finally:
            done_event.set()

    def _poll_native_capture_failure(
        self,
        native_recorder: NativeCoreAudioRecorder,
    ) -> None:
        """Wake the owner when native group or ring counters turn nonzero."""
        errors = _native_recorder_stat_errors(
            native_recorder.stats(),
            rejected_unit="audio buffer group(s)",
            drop_destination="the track workers",
        )
        if errors:
            self._mark_capture_invalid()

    def _start_drain(self, native_recorder: NativeCoreAudioRecorder) -> None:
        self._drain_stop_event = threading.Event()
        self._drain_abort_event = threading.Event()
        self._drain_done_event = threading.Event()
        self._drain_failures = []
        thread = threading.Thread(
            target=self._drain_loop,
            args=(
                native_recorder,
                self._drain_stop_event,
                self._drain_abort_event,
                self._drain_done_event,
            ),
            name="catap-multitrack-drain",
            daemon=False,
        )
        self._drain_thread = thread
        thread.start()

    def _stop_drain(self, *, drain_remaining: bool) -> list[BaseException]:
        errors: list[BaseException] = []
        if self._drain_stop_event is not None:
            self._drain_stop_event.set()
        if not drain_remaining and self._drain_abort_event is not None:
            self._drain_abort_event.set()

        thread = self._drain_thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=30.0)
            except BaseException as exc:
                errors.append(exc)
            if thread.is_alive():
                errors.append(RuntimeError("Native recorder drain did not stop"))
                return errors

        self._drain_thread = None
        self._drain_stop_event = None
        self._drain_abort_event = None
        self._drain_done_event = None
        errors.extend(self._drain_failures)
        self._drain_failures = []
        return errors

    def _drain_is_quiesced(self) -> bool:
        thread = self._drain_thread
        if thread is None:
            return True
        done_event = self._drain_done_event
        if done_event is not None and done_event.is_set():
            return True
        return not thread.is_alive() and getattr(thread, "ident", None) is None

    def _release_native_recorder(self, drain_quiesced: bool) -> list[BaseException]:
        """Free native state only when no raw-pointer reader can remain."""
        native_recorder = self._native_recorder
        if native_recorder is None:
            return []
        capture_session = self._capture_session
        io_proc_destroyed = capture_session is None or capture_session.io_proc_destroyed
        safe_to_close = io_proc_destroyed and drain_quiesced
        errors: list[BaseException] = []
        handled = False
        for _ in range(2):
            try:
                if safe_to_close:
                    native_recorder.close()
                else:
                    if capture_session is not None:
                        # Publish a process-lifetime Python owner before
                        # clearing the ctypes handle. Identity de-duplication
                        # keeps an interrupted retry from adding the same pair
                        # twice.
                        with _recorder_module._ABANDONED_NATIVE_CAPTURES_LOCK:
                            abandoned = _recorder_module._ABANDONED_NATIVE_CAPTURES
                            if not any(
                                existing_session is capture_session
                                and existing_recorder is native_recorder
                                for existing_session, existing_recorder in abandoned
                            ):
                                abandoned.append((capture_session, native_recorder))
                    native_recorder.abandon()
            except BaseException as exc:
                errors.append(exc)
            else:
                handled = True
                break

        if handled:
            self._native_recorder = None
            if not safe_to_close:
                errors.append(
                    RuntimeError(
                        "Retained native recorder state because cleanup did "
                        "not confirm both IOProc destruction and drain "
                        "quiescence."
                    )
                )
        return errors

    def start(self) -> None:
        """Start the multi-track capture.

        Raises:
            OSError: If Core Audio objects cannot be created or started
            RuntimeError: If already recording or cleanup is pending
            UnsupportedTapFormatError: If any track's stream format is not
                a supported PCM layout
        """
        with self._lifecycle_lock:
            if self._lifecycle_state == "recording":
                raise RuntimeError("Already recording")
            if self.needs_cleanup:
                raise RuntimeError(
                    "Recorder has pending cleanup; call stop() before restarting"
                )
            if self._lifecycle_state != "idle":
                raise RuntimeError("Recorder is already starting or stopping")
            self._lifecycle_state = "starting"

        # Keep the previous capture's failure query sticky until a new start
        # has successfully claimed the idle lifecycle.
        self._capture_failure_event.clear()
        with self._stats_lock:
            track_count = self.track_count
            self._accepted_frames = [0] * track_count
            self._dropped_buffers = [0] * track_count
            self._dropped_frames = [0] * track_count
            self._track_has_audio = [False] * track_count
            self._must_discard = False
        with self._callback_state_lock:
            self._callbacks_enabled = True

        cleanup: list[Callable[[], None]] = []
        aggregate_box = ctypes.c_uint32(0)
        start_succeeded = False
        self._session_owns_aggregate = False
        try:
            self._start_tap_formats = [
                self._capture_engine.describe_tap_stream(tap_id)
                for tap_id in self.tap_ids
            ]

            input_device_uids = (
                (self.input_device_uid,) if self.input_device_uid else ()
            )
            if self.drift_compensation_quality is None:
                aggregate_id = self._capture_engine.create_aggregate_for_taps(
                    self.tap_ids,
                    input_device_uids=input_device_uids,
                    out=aggregate_box,
                )
            else:
                aggregate_id = self._capture_engine.create_aggregate_for_taps(
                    self.tap_ids,
                    input_device_uids=input_device_uids,
                    drift_compensation_quality=self.drift_compensation_quality,
                    out=aggregate_box,
                )
            self._aggregate_device_id = aggregate_id
            self._session_owns_aggregate = False
            cleanup.append(self._cleanup_aggregate_step)

            self._apply_stream_formats(
                self._capture_engine.describe_aggregate_input_streams(aggregate_id)
            )

            native_recorder = self._create_native_recorder()
            self._native_recorder = native_recorder

            capture_session = self._capture_engine.attach_io_proc(
                aggregate_id,
                native_recorder.io_proc_pointer,
                native_recorder.handle,
            )
            self._capture_session = capture_session
            self._capture_engine.acknowledge_capture_session(capture_session)
            # The session's close() now owns both the IOProc and the
            # aggregate device.
            self._session_owns_aggregate = True

            # Registered before the first worker starts, so a failure starting
            # worker N still stops (and discards) workers 0..N-1; stopping a
            # never-started worker is a no-op.
            cleanup.append(self._cleanup_workers_step)
            for index, worker in enumerate(self._workers):
                worker.start(self._make_worker_config(index))

            self._start_drain(native_recorder)
            cleanup.append(self._cleanup_drain_step)

            self._start_drift_watch()
            cleanup.append(self._cleanup_drift_watch_step)

            with self._lifecycle_lock:
                self._is_recording = True

            self._capture_engine.start(capture_session)
            start_succeeded = True
            self._reached_recording = True
        except BaseException as exc:
            with self._lifecycle_lock:
                self._is_recording = False
            # Recover a session lost to an interruption between attach_io_proc
            # returning and the assignment above.
            failed_session = self._capture_engine.failed_capture_session
            if self._capture_session is None and isinstance(
                failed_session, _TapCaptureSession
            ):
                self._capture_session = failed_session
                self._session_owns_aggregate = True
            for step in reversed(cleanup):
                try:
                    step()
                except BaseException as cleanup_exc:
                    _add_secondary_failure(
                        exc,
                        "Cleanup failure during multitrack startup",
                        cleanup_exc,
                    )
            if self._aggregate_device_id is None and aggregate_box.value:
                self._aggregate_device_id = aggregate_box.value
            close_errors = self._close_capture_session()
            for cleanup_exc in close_errors:
                _add_secondary_failure(
                    exc,
                    "Cleanup failure during multitrack startup",
                    cleanup_exc,
                )
            for cleanup_exc in self._release_native_recorder(self._drain_is_quiesced()):
                _add_secondary_failure(
                    exc,
                    "Cleanup failure during multitrack startup",
                    cleanup_exc,
                )
            raise
        finally:
            with self._lifecycle_lock:
                if start_succeeded:
                    self._lifecycle_state = "recording"
                elif self.needs_cleanup:
                    self._lifecycle_state = "cleanup_failed"
                else:
                    self._lifecycle_state = "idle"

    def _cleanup_aggregate_step(self) -> None:
        """Destroy a bare aggregate the capture session does not own yet."""
        if self._session_owns_aggregate or self._capture_session is not None:
            return
        aggregate_id = self._aggregate_device_id
        if aggregate_id is not None:
            try:
                self._capture_engine.destroy_aggregate_device(aggregate_id)
            except OSError as exc:
                if getattr(exc, "status", None) != kAudioHardwareBadObjectError:
                    raise
            self._aggregate_device_id = None

    def _cleanup_workers_step(self) -> None:
        errors: list[BaseException] = []
        for worker in self._workers:
            try:
                worker.stop(publish=False)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise _combine_errors("Failed to stop track workers during cleanup", errors)

    def _cleanup_drain_step(self) -> None:
        errors = self._stop_drain(drain_remaining=False)
        if errors:
            raise _combine_errors("Failed to stop native drain during cleanup", errors)

    def _cleanup_drift_watch_step(self) -> None:
        errors = self._close_drift_watch()
        if errors:
            raise _combine_errors("Failed to close drift watch during cleanup", errors)

    def _close_capture_session(self) -> list[BaseException]:
        """Close the capture session (IOProc plus aggregate), if any."""
        capture_session = self._capture_session
        if capture_session is None:
            # A bare aggregate may still exist if IOProc attachment failed.
            try:
                self._cleanup_aggregate_step()
            except BaseException as exc:
                return [exc]
            return []
        try:
            self._capture_engine.close(capture_session)
        except BaseException as exc:
            return [exc]
        self._capture_session = None
        self._aggregate_device_id = None
        self._session_owns_aggregate = False
        return []

    @staticmethod
    def _path_exists_including_symlink(path: Path) -> bool:
        return os.path.lexists(path)

    @staticmethod
    def _path_signature(path: Path) -> tuple[int, int, int, int]:
        path_stat = path.lstat()
        return (
            path_stat.st_dev,
            path_stat.st_ino,
            path_stat.st_size,
            path_stat.st_mtime_ns,
        )

    def _prepare_publication_backups(
        self,
        transaction: _OutputPublishTransaction,
    ) -> None:
        """Snapshot every existing destination before replacing any of them."""
        for entry in transaction.entries:
            if entry.original_existed is None:
                if self._path_exists_including_symlink(entry.final_path):
                    path_stat = entry.final_path.lstat()
                    if not stat.S_ISREG(path_stat.st_mode):
                        raise OSError(
                            "Refusing to replace a non-regular multitrack "
                            f"output destination: {entry.final_path}"
                        )
                    entry.original_existed = True
                    entry.original_signature = self._path_signature(entry.final_path)
                else:
                    entry.original_existed = False

            if not entry.original_existed or entry.backup_ready:
                continue
            if entry.backup_path is not None:
                entry.backup_path.unlink(missing_ok=True)
            file_descriptor, backup_name = tempfile.mkstemp(
                dir=entry.final_path.parent,
                prefix=f".{entry.final_path.name}.",
                suffix=".catap-backup",
            )
            entry.backup_path = Path(backup_name)
            try:
                os.close(file_descriptor)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(file_descriptor)
                raise
            shutil.copy2(entry.final_path, entry.backup_path)
            entry.backup_ready = True

        transaction.phase = "publishing"

    def _verify_unchanged_destination(self, entry: _OutputPublishEntry) -> None:
        """Refuse to overwrite a destination changed after its snapshot."""
        exists = self._path_exists_including_symlink(entry.final_path)
        if entry.original_existed:
            if (
                not exists
                or self._path_signature(entry.final_path) != entry.original_signature
            ):
                raise RuntimeError(
                    "Multitrack output destination changed while recording "
                    f"was being published: {entry.final_path}"
                )
        elif exists:
            raise RuntimeError(
                "Multitrack output destination appeared while recording was "
                f"being published: {entry.final_path}"
            )

    def _commit_publication(
        self,
        transaction: _OutputPublishTransaction,
    ) -> list[BaseException]:
        """Remove snapshots and release worker state after every replace."""
        errors: list[BaseException] = []
        for entry in transaction.entries:
            backup_path = entry.backup_path
            if backup_path is None:
                continue
            try:
                backup_path.unlink(missing_ok=True)
            except BaseException as exc:
                errors.append(exc)
            else:
                entry.backup_path = None
                entry.backup_ready = False
        if errors:
            return errors

        for worker in self._workers:
            try:
                worker.complete_published_output()
            except BaseException as exc:
                errors.append(exc)
        if not errors:
            self._publish_transaction = None
        return errors

    def _rollback_publication(
        self,
        transaction: _OutputPublishTransaction,
    ) -> list[BaseException]:
        """Restore every original destination and discard all staged tracks."""
        errors: list[BaseException] = []
        for entry in transaction.entries:
            if entry.restored:
                continue
            worker = self._workers[entry.track_index]
            published = entry.track_index in transaction.published_tracks
            if (
                not published
                and entry.track_index in transaction.attempted_tracks
                and worker.output_was_published
            ):
                transaction.published_tracks.add(entry.track_index)
                published = True

            try:
                if published and not entry.published_removed:
                    entry.final_path.unlink(missing_ok=True)
                    entry.published_removed = True

                backup_path = entry.backup_path
                if entry.original_existed and published:
                    if backup_path is not None and backup_path.exists():
                        backup_path.replace(entry.final_path)
                        entry.backup_path = None
                        entry.backup_ready = False
                    elif not self._path_exists_including_symlink(entry.final_path):
                        raise OSError(
                            "Original multitrack output backup disappeared: "
                            f"{entry.final_path}"
                        )
                    entry.restored = True
                else:
                    if backup_path is not None:
                        backup_path.unlink(missing_ok=True)
                        entry.backup_path = None
                        entry.backup_ready = False
                    entry.restored = True
            except BaseException as exc:
                errors.append(exc)

        for worker in self._workers:
            try:
                worker.finalize(publish=False)
            except BaseException as exc:
                errors.append(exc)
        if not errors and all(entry.restored for entry in transaction.entries):
            self._publish_transaction = None
        return errors

    def _finalize_workers_transactionally(
        self,
        publish: bool,
    ) -> list[BaseException]:
        """Publish every track as one retryable transaction, or discard all."""
        transaction = self._publish_transaction
        if transaction is not None and transaction.phase == "committing":
            return self._commit_publication(transaction)
        if transaction is not None and transaction.phase == "rolling_back":
            return self._rollback_publication(transaction)

        if not publish:
            errors: list[BaseException] = []
            for worker in self._workers:
                try:
                    worker.finalize(publish=False)
                except BaseException as exc:
                    errors.append(exc)
            return errors

        if transaction is None:
            transaction = _OutputPublishTransaction(
                entries=[
                    _OutputPublishEntry(index, path)
                    for index, path in enumerate(self.output_paths)
                    if path is not None
                ]
            )
            self._publish_transaction = transaction

        try:
            if transaction.phase == "preparing":
                self._prepare_publication_backups(transaction)

            entries_by_track = {
                entry.track_index: entry for entry in transaction.entries
            }
            for track_index, worker in enumerate(self._workers):
                entry = entries_by_track.get(track_index)
                if entry is not None:
                    self._verify_unchanged_destination(entry)
                transaction.attempted_tracks.add(track_index)
                worker.publish_staged_output()
                transaction.published_tracks.add(track_index)
        except BaseException as exc:
            self._mark_capture_invalid()
            transaction.phase = "rolling_back"
            return [exc, *self._rollback_publication(transaction)]

        transaction.phase = "committing"
        return self._commit_publication(transaction)

    def _frame_total_errors(self) -> list[RuntimeError]:
        """Reject any cross-track divergence that escaped group admission."""
        with self._stats_lock:
            frame_totals = tuple(self._accepted_frames)
        if not frame_totals or len(set(frame_totals)) == 1:
            return []
        return [
            RuntimeError(
                "Multitrack workers accepted unequal frame totals "
                f"{frame_totals}; discarding output"
            )
        ]

    def stop(self) -> None:
        """Stop the capture, finalizing every track's output together.

        Any failure — a dropped buffer group, a tap format change, a native
        callback rejection, a worker error — discards every track instead
        of publishing a desynchronized or partial session.

        Raises:
            OSError: If Core Audio cleanup fails
            RuntimeError: If not recording, or the capture must be discarded
        """
        for worker in self._workers:
            if worker.is_worker_thread:
                raise RuntimeError(
                    "Cannot call stop() from an on_track_buffer callback; "
                    "signal the owning thread with threading.Event and call "
                    "stop() there instead"
                )

        with self._lifecycle_lock:
            if self._lifecycle_state == "idle" and not self.needs_cleanup:
                raise RuntimeError("Not recording")
            if self._lifecycle_state not in {
                "idle",
                "recording",
                "cleanup_failed",
            }:
                raise RuntimeError("Recorder is already starting or stopping")
            self._lifecycle_state = "stopping"
            self._is_recording = False

        errors: list[BaseException] = []
        # A capture that never reached the recording state has nothing worth
        # publishing; without this, retried cleanup after a failed start()
        # would publish empty or partial track files.
        publish = self._reached_recording and not self._capture_is_invalid()
        try:
            capture_session = self._capture_session
            native_recorder = self._native_recorder

            if capture_session is not None and capture_session.started:
                try:
                    self._capture_engine.stop(capture_session)
                except BaseException as exc:
                    errors.append(exc)
                    publish = False
                    self._mark_capture_invalid()

            io_stopped = capture_session is None or not capture_session.started
            drain_errors = self._stop_drain(drain_remaining=io_stopped)
            if drain_errors:
                errors.extend(drain_errors)
                publish = False
                self._mark_capture_invalid()
            drain_quiesced = self._drain_is_quiesced()
            if not drain_quiesced:
                publish = False
                self._mark_capture_invalid()

            drift_watch_errors = self._close_drift_watch()
            if drift_watch_errors:
                errors.extend(drift_watch_errors)
                publish = False
                self._mark_capture_invalid()

            drift_errors = self._detect_tap_drift()
            if drift_errors:
                errors.extend(drift_errors)
                publish = False
                self._mark_capture_invalid()

            # Tap drift is watched live, but the input-device track has no
            # per-track format property to listen on; re-read the aggregate's
            # stream layout before it is destroyed so a mid-capture change
            # (for example a microphone sample-rate switch) discards the
            # session instead of publishing silently drifted tracks.
            if (
                publish
                and self._aggregate_device_id is not None
                and self._stream_formats
            ):
                try:
                    current_streams = (
                        self._capture_engine.describe_aggregate_input_streams(
                            self._aggregate_device_id
                        )
                    )
                except BaseException as exc:
                    errors.append(
                        _translate_exception(
                            OSError,
                            "Could not verify the aggregate stream layout at "
                            f"stop; discarding output: {exc}",
                            exc,
                        )
                    )
                    publish = False
                    self._mark_capture_invalid()
                else:
                    if current_streams != self._stream_formats:
                        errors.append(
                            RuntimeError(
                                "Aggregate input streams changed during "
                                f"capture (started as {self._stream_formats}, "
                                f"now {current_streams}); discarding output"
                            )
                        )
                        publish = False
                        self._mark_capture_invalid()

            if native_recorder is not None:
                try:
                    stats = native_recorder.stats()
                except BaseException as exc:
                    errors.append(exc)
                    publish = False
                    self._mark_capture_invalid()
                else:
                    native_errors = _native_recorder_stat_errors(
                        stats,
                        rejected_unit="audio buffer group(s)",
                        drop_destination="the track workers",
                    )
                    if native_errors:
                        errors.extend(native_errors)
                        publish = False
                        self._mark_capture_invalid()

            capture_close_errors = self._close_capture_session()
            if capture_close_errors:
                errors.extend(capture_close_errors)
                publish = False
                self._mark_capture_invalid()
            native_release_errors = self._release_native_recorder(drain_quiesced)
            if native_release_errors:
                errors.extend(native_release_errors)
                publish = False
                self._mark_capture_invalid()

            worker_errors: list[BaseException] = []
            for worker in self._workers:
                worker_errors.extend(worker.drain_and_collect())
            worker_capture_invalid = any(
                worker.capture_is_invalid for worker in self._workers
            )
            if worker_errors:
                errors.extend(worker_errors)
            if worker_errors or worker_capture_invalid:
                publish = False
                self._mark_capture_invalid()

            frame_total_errors = self._frame_total_errors()
            if frame_total_errors:
                errors.extend(frame_total_errors)
                publish = False
                self._mark_capture_invalid()

            finalize_errors = self._finalize_workers_transactionally(publish)
            if finalize_errors:
                errors.extend(finalize_errors)
                transaction = self._publish_transaction
                if transaction is None or transaction.phase != "committing":
                    self._mark_capture_invalid()
        finally:
            with self._lifecycle_lock:
                still_needs_cleanup = self.needs_cleanup
                self._lifecycle_state = (
                    "cleanup_failed" if still_needs_cleanup else "idle"
                )
                if not still_needs_cleanup:
                    self._reached_recording = False
                    with self._stats_lock:
                        self._must_discard = False

        if errors:
            raise _combine_errors("Failed to stop multitrack recording cleanly", errors)

    @property
    def is_recording(self) -> bool:
        """True if currently recording."""
        return self._is_recording

    @property
    def needs_cleanup(self) -> bool:
        """True when a failed teardown still owns retryable resources."""
        return not self._is_recording and (
            self._capture_session is not None
            or self._aggregate_device_id is not None
            or self._native_recorder is not None
            or self._drain_thread is not None
            or self._drift_watch is not None
            or self._publish_transaction is not None
            or any(worker.needs_cleanup for worker in self._workers)
        )

    @property
    def capture_failed(self) -> bool:
        """True once a live or background capture failure was detected."""
        return self._capture_failure_event.is_set()

    def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
        """Wait for failure without stopping or tearing down any capture state."""
        return self._capture_failure_event.wait(timeout)

    @property
    def frames_recorded(self) -> int:
        """Frames delivered to the first track's worker so far."""
        with self._stats_lock:
            return self._accepted_frames[0]

    @property
    def duration_seconds(self) -> float:
        """Duration of recorded audio in seconds."""
        if not self._public_formats:
            return 0.0
        return self.frames_recorded / self._public_formats[0].sample_rate

    @property
    def stream_formats(self) -> list[AudioStreamFormat]:
        """Per-track stream formats, once the capture has started."""
        return list(self._public_formats)

    @property
    def captured_only_silence(self) -> bool:
        """True while no track has produced a nonzero audio sample."""
        return not any(self._track_has_audio)

    @property
    def track_captured_only_silence(self) -> tuple[bool, ...]:
        """Per-track silence flags, in track order.

        A silent tap track alongside a live microphone track usually means
        System Audio Recording permission is missing while microphone
        permission is granted.
        """
        return tuple(not has_audio for has_audio in self._track_has_audio)

    @property
    def max_pending_buffers(self) -> int:
        """Per-track queue bound before overflow."""
        return self._max_pending_buffers

    def close(self) -> None:
        """Stop if needed and release resources. Idempotent."""
        if self._is_recording or self.needs_cleanup:
            self.stop()

    def __enter__(self) -> MultitrackAudioRecorder:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc is None:
            self.close()
            return
        with contextlib.suppress(BaseException):
            self.close()
