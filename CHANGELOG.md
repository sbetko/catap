# Changelog

All notable changes to this project will be documented in this file.

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
