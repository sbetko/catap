# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.6.0] - 2026-08-25

Feature completeness against the Core Audio tap API surface from macOS 14.2
through macOS 26.

### Tap API surface

- Added the macOS 26 `CATapDescription` fields: `TapDescription.bundle_ids`
  (tap or exclude by bundle ID) and `process_restore_enabled` (tapped apps
  rejoin the capture when they restart), plus `bundle_id_taps_supported()`
  and a `TapDescription.uuid` setter. On earlier macOS the getters report
  empty/false and the setters raise with a clear availability message.
- Added `get_tap_format(tap_id)` returning a new public `TapStreamFormat`,
  and `set_tap_description(tap_id, description)` for retargeting a live tap
  (`kAudioTapPropertyDescription` is settable; requires System Audio
  Recording permission, surfaced as `PermissionError` without it).
- `find_tap_by_uid` now resolves through
  `kAudioHardwarePropertyTranslateUIDToTap` with the listing as fallback,
  and `find_process_by_pid` resolves through
  `kAudioHardwarePropertyTranslatePIDToProcessObject`.
- `AudioProcess` gained `is_running` and `is_inputting`.
- `create_process_tap` now raises when Core Audio reports success but
  returns tap ID 0 (its signal for an unsatisfiable description, for
  example a global non-mixdown tap).

### Event layer

- Added `catap.events`: `AudioPropertyWatch` over
  `AudioObjectAddPropertyListener`, with `watch_audio_processes`,
  `watch_audio_taps`, `watch_audio_devices`,
  `watch_default_output_device`, `watch_tap`, and `watch_property`.
  Callbacks run on a catap-owned dispatcher thread, never the HAL thread.
- Recorders now watch the tap's format and the tap list during capture, so a
  format change or vanished tap, even if recreated identically, is caught as
  it happens. The stop-time re-read remains as the backstop. Failures still
  discard the output.

### Capture variants

- Sessions and the CLI can record several apps mixed through one tap
  (`record_processes`, multiple CLI app names), one output device stream
  (`record_device`, `--device`/`--stream`), and bundle IDs on macOS 26
  (`record_bundle_ids`, `--bundle-id`, `--no-restore`).
- Process, system-audio, and bundle-ID session builders accept the full mute
  set (`mute=` takes a bool or `TapMuteBehavior`), `mono=True`, and
  `visible=True`. Device taps keep their native format and therefore omit
  `mono`; multitrack omits `visible`; existing-tap sessions reuse the tap's
  configuration. The CLI exposes the applicable creation options and limits
  mute options to app, bundle-ID, and device targets.
- `RecordingSession.set_processes` retargets inclusive process-list taps and
  rejects global/exclusive and bundle-ID taps, whose target lists have
  different semantics. `set_mute_behavior` changes mute behavior on any live
  tap without interrupting the recording.
- CLI parity: `list-taps`, `list-devices`, `record --tap UID`, and
  `catap watch` for streaming Core Audio change events.

### Multitrack capture

- Added `record_multitrack` / `MultitrackRecordingSession` /
  `MultitrackAudioRecorder`: one tap per app captured through a single
  aggregate device as sample-synchronized tracks, one WAV (or callback
  feed) per track; CLI `record-multitrack App1 App2 -o dir/`.
- Output-directory mode creates missing parent directories. Explicit and
  derived track destinations are checked for relative, case-only, and Unicode
  aliases before recording so two tracks cannot publish to the same path.
- Track files publish as one retryable in-process transaction that restores
  pre-existing destinations after an error or interruption. Abrupt process
  termination during the final multi-file commit is not journal-recovered.
- Optional experimental microphone track (`microphone=True` /
  `--with-mic`): one hardware input device records alongside the taps in
  the same aggregate, sample-locked to the app tracks. Requires the
  separate Microphone permission.
- Native ABI v2: the IOProc accepts one buffer per aggregate input stream
  and queues each callback's buffers as an atomic all-or-nothing group
  tagged with a buffer index, so per-track streams can never tear against
  each other; any drop or failure discards every track on stop.
- Per-track diagnostics: `track_captured_only_silence` distinguishes a
  silent (permission-zeroed) tap track from a live microphone track.

### Capture configuration

- Added `DriftCompensationQuality` and an opt-in
  `drift_compensation_quality=` argument across single-track, existing-tap,
  bundle-ID, device, and multitrack recorder/session paths. Omitting it keeps
  Core Audio's operating-system default; raw integers and booleans are
  rejected instead of forwarding unchecked values.
- Expanded the permissioned release gate with real microphone-plus-process
  alignment, bundle-ID restoration across a QuickTime Player quit/relaunch,
  and prompt fixed-duration wakeup after an external tap is destroyed.

### Capture reliability

- Added sticky `capture_failed` and `wait_for_capture_failure()` APIs to
  recorders and sessions. Live tap drift/disappearance, native ring or
  callback failures, worker failures, and multitrack group failures now wake
  fixed-duration and indefinite capture owners early; the CLI then follows
  its existing stop, discard, cleanup, and error-reporting path.
- Failure notification is passive: producer and property-listener threads do
  not tear down capture state. The signal remains set through shutdown and
  resets on the next start.

## [0.5.2] - 2026-08-24

- Re-checked the tap stream format at stop: if it changed mid-capture (for
  example after a default-device change) or the tap was destroyed by its
  owner, `stop()` now fails and discards the output instead of publishing a
  corrupt or truncated WAV.
- Changed `AudioRecorder.needs_cleanup` to report false during active
  recording, matching `RecordingSession.needs_cleanup` and its documented
  "failed teardown pending" meaning. Use `is_recording` for capture state.
- Added `captured_only_silence` to `AudioRecorder` and `RecordingSession`,
  and a CLI warning after all-zero recordings. macOS delivers zeroed tap
  audio when the hosting app lacks system-audio permission, so silent
  "successful" captures are now diagnosable instead of indistinguishable
  from success.
- Required setuptools 83+ for sdist builds and the locked development
  environment, picking up the fix for CVE-2026-59890 (MANIFEST.in exclusions
  bypassed by Unicode normalization on macOS filesystems). catap's ASCII-only
  tree was not exposed.
- CI now installs the built wheel into a clean environment and verifies the
  package version, native dylib load and ABI, and the `catap` entry point
  before uploading artifacts, in the CI and both publish workflows.
- CI now executes the x86_64 slice of the universal2 dylib by running the
  test suite under an x86_64 CPython through Rosetta 2 on Apple Silicon
  runners.
- Added an opt-in known-tone acceptance gate
  (`CATAP_RUN_TONE_INTEGRATION=1`): it plays a 1 kHz tone through the default
  output device and fails unless the capture actually contains it,
  distinguishing real audio delivery from permission-zeroed silence. The
  existing smoke test now also requires the WAV frame count to match
  `frames_recorded`.
- Documented the retryable cleanup contract in the README, the low-level
  `AudioRecorder` example, and the mute-behavior notes, and documented that
  shutdown drains queued buffers through `on_buffer` and waits on the
  in-flight callback.
- Added `RecordingSession.needs_cleanup`, making retryable cleanup an explicit
  public contract: after a failed `stop()` or `close()`, the property stays
  true until a retried `close()` succeeds, and a tap created with `mute=True`
  keeps its process muted until then.
- Closed the interruption windows during tap and aggregate-device creation:
  Core Audio now writes new object IDs into caller-owned storage, so a
  `KeyboardInterrupt` between creation and Python storing the ID no longer
  leaks the object. `create_process_tap` accepts an optional ``out``
  parameter for low-level callers who want the same recovery guarantee.
- Rejected non-finite tap sample rates and integer tap bit depths other than
  16, 24, and 32 bits up front with `UnsupportedTapFormatError`, instead of
  failing later in WAV setup or silently passing in streaming-only mode.

## [0.5.1] - 2026-08-24

- Hardened Core Audio teardown so native callback state is never released until
  IOProc deregistration and background draining are confirmed safe, including
  cleanup paths interrupted by `KeyboardInterrupt` or `SystemExit`.
- Rejected recorder and session stops from inside `on_buffer` callbacks before
  lifecycle state changes, and documented the owning-thread event pattern.
- Made session tap cleanup retryable and preserved primary exceptions when
  context-manager, fixed-duration, or startup cleanup also fails.
- Kept WAV publication transactional across callback, conversion, publish, and
  temporary-file cleanup failures.
- Reported CLI runtime failures without tracebacks or misleading permission
  hints, and rejected empty output paths and non-finite durations.
- Built the native Core Audio library correctly for editable installs, kept
  generated binaries out of source distributions, and made uv invalidate
  cached editable builds when native sources or headers change.

## [0.5.0] - 2026-05-05

- BREAKING: Replaced streaming callbacks from `on_data(data, num_frames)` with
  `on_buffer(buffer: AudioBuffer)`.
- Added callback metadata types: `AudioBuffer` and `AudioStreamFormat`.
- Added `stream_format` accessors on `AudioRecorder` and `RecordingSession`;
  use fields such as `stream_format.sample_rate` instead of scalar format
  convenience properties.
- Added a native macOS Core Audio dylib for the recorder IOProc and made it
  required for recording. The old pure-Python IOProc fallback has been removed.

  ```python
  # Before
  def on_data(data: bytes, num_frames: int) -> None:
      ...

  # After
  def on_buffer(buffer: AudioBuffer) -> None:
      data = buffer.data
      num_frames = buffer.frame_count
  ```

## [0.4.3] - 2026-04-28

- Tightened README, CLI help, and package metadata around supported process-tap
  scenarios and known gaps.

## [0.4.2] - 2026-04-27

- Added CI coverage for free-threaded CPython 3.13t and 3.14t on macOS.
- Replaced the recorder buffer-pool `deque` with `queue.SimpleQueue` to avoid
  relying on CPython deque atomicity in free-threaded builds.
- Documented local free-threaded test commands and the opt-in real-recording
  smoke check.

## [0.4.1] - 2026-04-26

- Added Python 3.11 support by replacing 3.12-only type-alias syntax and
  broadening package metadata.

## [0.4.0] - 2026-04-24

- Added strict tap stream format validation before capture starts. Unsupported
  Core Audio layouts now raise `UnsupportedTapFormatError` instead of risking
  plausible but corrupt WAV output.
- Reject non-linear PCM, big-endian PCM, non-packed PCM, non-interleaved audio,
  unsigned integer PCM, padded frames, invalid rates/channels/bit depths, and
  floating-point formats other than packed float32.
- Treat malformed callback buffers as capture failures instead of guessing:
  multi-buffer `AudioBufferList` layouts, missing data pointers, mismatched or
  missing channel counts, and partial-frame byte counts are surfaced on stop.
- Make WAV output transactional. Recorders now write to a sibling temporary file
  and publish it only after clean shutdown, preserving existing output files on
  failed startup or failed writes.
- Stop active capture sessions before destroying Core Audio resources and clear
  started state after stop attempts so cleanup paths are more deterministic.
- Reject non-integer `max_pending_buffers` values such as `True`, floats, and
  strings.
- Expanded pytest coverage around Core Audio lifecycle cleanup, recorder format
  handling, malformed callback buffers, output-file safety, public exports, and
  session queue-bound validation.

## [0.3.0] - 2026-04-23

- Refactored the recorder into focused internal capture-engine, worker, support,
  and session-backend modules.
- Preserved recorder cleanup failures across stop and close paths so secondary
  teardown errors are not lost.
- Surfaced Core Audio callback failures on recorder stop instead of silently
  swallowing them.
- Added synthetic and live profiling probes for worker throughput, conversion
  cost, callback timing, queue depth, and dropped-buffer behavior.
- Replaced the old private-internals profiling scripts with the new profiling
  harnesses.
- Added performance and real-time notes for the recorder callback, queueing
  model, CPython deque assumptions, and known tradeoffs.
- Tightened README wording and added the current tested macOS hardware/version.
- Consolidated Core Audio binding discovery helpers and expanded related tests.
- Updated locked development dependencies.

## [0.2.0] - 2026-04-21

- Initial public release of `catap`.
- Added device stream discovery so taps can target a specific input/output stream.
- Added discovery of existing process taps and the ability to record from them.
- Added shared-tap support: create, extend, and delete shared taps from the core lab demo.
- Raised clearer errors for stale shared taps and preserved zero-tap device stream metadata.
- Hardened audio recorder concurrency and cleanup lifecycle for free-threaded Python builds.
- Consolidated recorder structs and unified the cleanup cascade.
- Moved helper tone tooling into an internal devtools package and added regression coverage.
- Added worker queue latency profiling and restored synthetic profiler compatibility.
- Expanded the core lab demo with recording playback controls, helper tone device selection, shared-tap workflows, and bench-style chrome.
- Bumped supported Python floor metadata to include 3.14.
- Slimmed the README and split implementation notes into `docs/`.

## [0.1.0] - 2026-04-17

- Initial private release of `catap`.
- Added a CLI for listing audio processes and recording app or system audio.
- Added a Python API for process taps and WAV recording.
- Streamed WAV output during recording so long captures do not accumulate unbounded RAM.
- Improved macOS-only runtime errors and permission guidance.
- `find_process_by_name` now prefers exact application-name and bundle-ID matches over partial matches, and raises `AmbiguousAudioProcessError` (new public export) when a query matches multiple processes.
- `list_audio_processes` propagates Core Audio failures instead of silently returning an empty list.
- Recorder now uses a bounded work queue so the Core Audio callback no longer blocks or grows memory without bound; dropped buffers are surfaced as a `RuntimeError` on stop.
- Recorder queue bounds are now configurable via `max_pending_buffers` on the low-level recorder and the high-level session helpers.
- Recorder output-file lifecycle is hardened: failed WAV setup closes the underlying file, and the output file is closed in the worker's teardown.
- Recorder startup no longer touches the destination WAV path until Core Audio startup succeeds, preventing failed starts from clobbering existing files.
- Session and recorder setup now reject target-less "streaming" configurations unless an `on_data` callback is supplied.
- Recorder now uses Core Audio `AudioConverter` for the float32 -> int16 WAV path, improving worker throughput while changing the exact int16 rounding/clipping behavior to match Core Audio.
- CLI distinguishes output-file errors (bad path, unwritable directory) from permission errors when a recording fails to start.
- Added an opt-in integration smoke test that performs a short real recording and validates the resulting WAV file.
- Consolidated internal Core Audio bindings into a single `_coreaudio` module for easier maintenance.
- Added internal AudioToolbox bindings and synthetic profiling coverage for `AudioConverter` / `ExtAudioFile` comparisons.
- Flattened the package layout by moving `AudioRecorder` to `catap.recorder` and removing the one-file `catap.core` package.
- Added a Tkinter demo app for manually exercising the browser, high-level recording flows, callback streaming, and low-level tap/recorder APIs.
