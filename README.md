# catap

[![CI](https://github.com/sbetko/catap/actions/workflows/ci.yml/badge.svg)](https://github.com/sbetko/catap/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/catap.svg)](https://pypi.org/project/catap/)
[![Python versions](https://img.shields.io/pypi/pyversions/catap.svg)](https://pypi.org/project/catap/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Python bindings and recording utilities for Apple's Core Audio process-tap API
(macOS 14.2+). `catap` captures outgoing process audio through Core Audio taps,
without installing or selecting a third-party loopback driver.

## Install

```bash
pip install catap
```

`catap` is macOS-only; importing it on other platforms raises `ImportError`.
It targets macOS 14.2 and newer. CI covers CPython 3.11 through 3.14 plus
free-threaded CPython 3.13t and 3.14t on macOS. Current development is on
Apple Silicon and macOS 26.2.

Recording requires the bundled native Core Audio dylib. Wheels include a
universal2 build; source builds require the macOS command-line developer tools.

## Quick start

CLI:

```bash
catap record Safari -d 10 -o safari.wav    # record an app for 10 seconds
catap record --system -d 10 -o mix.wav     # record a global process-output mix
catap list-apps                            # see what's producing audio
```

Python:

```python
from catap import record_process

session = record_process("Safari", output_path="safari.wav")
session.record_for(10)

print(f"Recorded {session.duration_seconds:.2f} seconds")
```

## What it does

- Record a single app, a global process-output mix, or an existing visible tap.
- Exclude selected apps from a global recording.
- Mute an app while recording it, so it's captured but not played aloud.
- Target a specific output device stream when building a tap.
- Write WAV files, or stream PCM buffers to your own callback.
- Use a bounded audio queue: if the worker falls behind, buffers are dropped
  and the count is reported on stop, instead of growing memory without bound.

## Scope

`catap` is a process-output capture library. It is not a microphone/input-device
recorder, an AudioServerPlugIn implementation, or a virtual audio driver.

The current recorder path reads one tap through one private HAL aggregate
device. It accepts packed, interleaved linear PCM tap formats and rejects
non-interleaved, multi-buffer, compressed, padded, or otherwise unusual formats.

The `--system` and `record_system_audio()` paths build a global Core Audio tap:
they capture process output that Core Audio exposes to taps. Long-running
captures across sleep/wake, route changes, source-process restarts, and default
output-device changes are not covered yet. See
[`docs/core-audio-notes.md`](docs/core-audio-notes.md) for the short Core Audio
notes.

## Usage

### Command Line

Common commands:

```bash
catap list-apps
catap list-apps --all
catap record Safari -d 30 -o safari.wav
catap record Spotify --mute -d 60 -o spotify.wav
catap record --system -e Music -e Zoom -d 30 -o system.wav
```

Run `catap record --help` for the full set of recording options.

### Python API

```python
from catap import record_process

# High-level API: catap manages tap creation, startup, shutdown, and cleanup.
session = record_process("Safari", output_path="output.wav")
session.record_for(5)

print(f"Recorded {session.duration_seconds:.2f} seconds")
```

If you pass `on_buffer=...`, the callback runs on `catap`'s background worker
thread so the Core Audio callback stays lightweight. The callback receives an
`AudioBuffer` with bytes that are safe to keep, frame count, stream format
metadata, and Core Audio timing metadata:

```python
from catap import AudioBuffer, record_process

def on_buffer(buffer: AudioBuffer) -> None:
    print(buffer.frame_count, buffer.format.sample_rate, buffer.input_sample_time)

session = record_process("Safari", on_buffer=on_buffer)
session.record_for(5)
```

Once recording has started, `session.stream_format` exposes the callback
`AudioStreamFormat` without waiting for the next buffer.

To control the recording lifetime yourself, use the session as a context
manager:

```python
import time
from catap import record_process

with record_process("Safari", output_path="output.wav", mute=False) as session:
    time.sleep(5)

print(f"Recorded {session.duration_seconds:.2f} seconds")
```

For streaming-only mode, pass `on_buffer=...` and omit `output_path`.

By default, `catap` queues up to 256 pending audio buffers before treating a
slow writer or callback as a capture failure. Tune this with
`max_pending_buffers=...` on `record_process`, `record_system_audio`,
`RecordingSession`, or `AudioRecorder`.

A name query that matches multiple processes raises with the candidates in
the error rather than picking one arbitrarily.

### Mute Behavior

With `record_process(..., mute=True)`, the app stays muted for the lifetime
of the recording session. The lower-level mute modes behave differently if the
tap outlives the recorder; see [`docs/mute-behavior.md`](docs/mute-behavior.md).

### Low-level API

```python
from catap import (
    AudioRecorder,
    TapDescription,
    TapMuteBehavior,
    create_process_tap,
    destroy_process_tap,
    find_process_by_name,
    list_audio_taps,
    record_tap,
)

process = find_process_by_name("Safari")
print(f"Found: {process.name} (PID: {process.pid})")

tap_desc = TapDescription.stereo_mixdown_of_processes([process.audio_object_id])
tap_desc.name = "My Recording"
tap_desc.mute_behavior = TapMuteBehavior.UNMUTED  # or MUTED

import time

tap_id = create_process_tap(tap_desc)
try:
    recorder = AudioRecorder(tap_id, "output.wav")
    recorder.start()
    try:
        time.sleep(5)
    finally:
        recorder.stop()
    print(f"Recorded {recorder.duration_seconds:.2f} seconds")
finally:
    destroy_process_tap(tap_id)
```

If another app has already created a non-private tap, you can discover it and
attach a recorder without taking ownership of the tap itself:

```python
from catap import list_audio_taps, record_tap

tap = next(tap for tap in list_audio_taps() if tap.name == "Shared Mix")
session = record_tap(tap, output_path="shared-mix.wav")
session.record_for(5)
```

Device-targeted taps can be built directly from discovered output streams:

```python
from catap import TapDescription, find_process_by_name, list_audio_devices

process = find_process_by_name("Safari")
device = next(device for device in list_audio_devices() if device.is_default_output)
stream = device.output_streams[0]

tap_desc = TapDescription.of_processes_for_device_stream(
    [process.audio_object_id],
    stream,
)
tap_desc.name = "Safari on default speakers"
```

## Permissions

Core Audio taps require system-audio recording permission. macOS prompts the
first time an app starts recording from an aggregate device that contains a tap;
if access was previously denied, enable it in System Settings.

When you run from a terminal (for example `uv run catap record Spotify`),
macOS attributes capture to the terminal app, so grant permission to
Terminal, iTerm, or whichever host is launching `catap`.

App bundles using Core Audio taps should include
`NSAudioCaptureUsageDescription` in their `Info.plist`. Sandboxed apps still
need their normal sandbox configuration; Core Audio taps do not add a separate
system-audio-capture entitlement.

## How it works

1. Process enumeration: reads
   `kAudioHardwarePropertyProcessObjectList` to find audio processes.
2. Tap creation: builds a `CATapDescription` through PyObjC and calls
   `AudioHardwareCreateProcessTap`.
3. Aggregate device: creates a private Core Audio aggregate device containing
   the tap, matching Apple's documented tap-capture path. `catap` destroys the
   aggregate when recording stops.
4. Audio capture: registers the bundled native dylib's `AudioDeviceIOProc`
   and copies tap audio into a preallocated native ring.
5. Worker output: a Python drain thread feeds the background worker, which
   writes WAV data and invokes optional `on_buffer` callbacks outside the
   Core Audio real-time path.

The Core Audio notes live in [`docs/core-audio-notes.md`](docs/core-audio-notes.md).
Recorder callback and queueing design is in
[`docs/performance.md`](docs/performance.md).

## Development

```bash
git clone https://github.com/sbetko/catap.git
cd catap
uv sync --group dev
```

### Quality checks

```bash
uv run --group dev ruff check .
uv run --group dev ty check --error-on-warning src tests
uv run --group dev pytest
uv run --group dev python -m build
uv run --group dev twine check dist/*
```

Free-threaded checks:

```bash
uv python install 3.13t 3.14t
uv run --python 3.13t --group dev pytest
uv run --python 3.14t --group dev pytest
CATAP_RUN_INTEGRATION=1 uv run --python 3.14t --group dev pytest \
  tests/test_integration.py::test_record_system_audio_smoke
```

### Integration smoke test

```bash
CATAP_RUN_INTEGRATION=1 uv run --group dev pytest -m integration
```

Opt-in. Exercises the real macOS Core Audio bridge: process enumeration and
a short recording that covers tap startup, shutdown, and WAV finalization.

See [`RELEASE.md`](RELEASE.md) for the release checklist.

## License

MIT
