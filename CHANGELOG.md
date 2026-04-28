# Changelog

All notable changes to this project will be documented in this file.

## [0.4.3] - 2026-04-28

- Tightened README, CLI help, and package metadata around the process-tap support
  boundary and unsupported capture scenarios.

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
- Recorder now uses Core Audio `AudioConverter` for the float32 -> int16 WAV path, improving worker throughput while changing the exact int16 rounding/clipping semantics to match Core Audio.
- CLI distinguishes output-file errors (bad path, unwritable directory) from permission errors when a recording fails to start.
- Added an opt-in integration smoke test that performs a short real recording and validates the resulting WAV file.
- Consolidated internal Core Audio bindings into a single `_coreaudio` module for easier maintenance.
- Added internal AudioToolbox bindings and synthetic profiling coverage for `AudioConverter` / `ExtAudioFile` comparisons.
- Flattened the package layout by moving `AudioRecorder` to `catap.recorder` and removing the one-file `catap.core` package.
- Added a Tkinter demo app for manually exercising the browser, high-level recording flows, callback streaming, and low-level tap/recorder APIs.
