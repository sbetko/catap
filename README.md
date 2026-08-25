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

`catap` is macOS-only and requires macOS 14.2 or later. Importing it on other
platforms raises `ImportError`. CI covers CPython 3.11 through 3.14, including
free-threaded 3.13t and 3.14t, on macOS 15 and 26.

Recording requires the bundled native Core Audio dylib. Wheels include a
universal2 build; source builds require the macOS command-line developer tools.

## Quick start

CLI:

```bash
catap record Safari -d 10 -o safari.wav    # record an app for 10 seconds
catap record --system -d 10 -o mix.wav     # record a global process-output mix
catap record-multitrack Safari Music -o session/   # one synced WAV per app
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

- Record one app, several apps mixed through one tap, a global
  process-output mix, one output device's stream, or an existing visible tap.
- Record each app as its own sample-synchronized track (multitrack), with one
  optional input-device track per input stream.
- Record apps by bundle ID on macOS 26+, surviving app restarts.
- Exclude selected apps from a global or device recording.
- Mute tapped apps always or only while the tap is read.
- Mix captures down to stereo or mono, and create private or visible taps.
- Retarget a live tap mid-capture: change its process set or mute behavior
  without interrupting the recording.
- Watch Core Audio changes (process list, tap list, devices, default output)
  through a property-listener event layer.
- Write WAV files, or stream PCM buffers to your own callback.
- Use bounded queues. Overflow fails the capture and reports dropped buffers
  and frames instead of growing memory without bound.

## Scope

`catap` is a process-output capture library built on the Core Audio tap API.
It is not a general microphone recorder, an AudioServerPlugIn implementation,
or a virtual audio driver. Its one input-device path is experimental
synchronized microphone capture in multitrack sessions.

The single-tap recorder path reads one tap through one private HAL aggregate
device and accepts packed, interleaved linear PCM formats. The multitrack
path reads several taps (and optionally one input device) through one
aggregate, one stream per track. Non-interleaved, compressed, padded, and
other unsupported stream formats fail at startup.

A format change or destruction of an externally owned tap fails capture and
discards file output. Equal-format output switches can continue without
recorder intervention. Seamless recovery after sleep/wake, format-changing
transitions, or loss of the aggregate or IOProc is not implemented. Known
background failures set `capture_failed` and wake
`wait_for_capture_failure()`. `stop()` performs final validation and cleanup.
See [`docs/core-audio-notes.md`](docs/core-audio-notes.md).

## Usage

### Command Line

Common commands:

```bash
catap list-apps
catap list-apps --all
catap list-taps
catap list-devices
catap record Safari -d 30 -o safari.wav
catap record Safari Music -d 30 -o both.wav        # two apps, one mixed tap
catap record Spotify --mute -d 60 -o spotify.wav
catap record Spotify --mute-when-tapped -o spotify.wav
catap record --system -e Music -e Zoom -d 30 -o system.wav
catap record --device "MacBook Pro Speakers" -d 30 -o speakers.wav
catap record --tap <uid> -o shared.wav             # an existing visible tap
catap record --bundle-id com.apple.Music -o music.wav   # macOS 26+
catap record-multitrack Safari Music -o session/   # one synced WAV per app
catap record-multitrack Safari --with-mic -o call/ # app + microphone tracks
catap watch                                        # stream Core Audio changes
```

Run `catap record --help` and `catap record-multitrack --help` for the full
option set.

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

Do not call `session.stop()` or `recorder.stop()` directly from `on_buffer`.
The callback runs on the worker that `stop()` must join and finalize. Signal the
owning thread instead, then let that thread stop the session:

```python
from threading import Event

from catap import AudioBuffer, record_process

stop_requested = Event()
frames_received = 0

def on_buffer(buffer: AudioBuffer) -> None:
    global frames_received
    frames_received += buffer.frame_count
    if frames_received >= buffer.format.sample_rate * 5:
        stop_requested.set()

session = record_process("Safari", on_buffer=on_buffer)
session.start()
try:
    stop_requested.wait()
finally:
    session.stop()
```

Once recording has started, `session.stream_format` exposes the callback
`AudioStreamFormat` without waiting for the next buffer.

Stopping waits for the callback: queued buffers are still delivered to
`on_buffer` during shutdown, and the in-flight callback is joined before
`stop()` returns. A callback that never returns blocks shutdown indefinitely,
so keep callbacks bounded and quick.

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

### Capture variants

Process, system-audio, and bundle-ID session builders accept `mute=` (a bool
or `TapMuteBehavior`), `mono=True`, and `visible=True` (create a tap other
audio clients can see). Device capture follows the device's native format, so
it accepts `mute=` and `visible=` but not `mono=`. Multitrack capture accepts
`mute=` and `mono=` and creates private taps; a session for an existing tap
uses that tap's already-configured options. The CLI exposes the applicable
creation options and intentionally limits `--mute` to app, bundle-ID, and
device targets:

```python
from catap import (
    TapMuteBehavior,
    record_bundle_ids,
    record_device,
    record_processes,
    record_system_audio,
)

record_processes(["Safari", "Music"], "both.wav")       # one mixed tap
record_system_audio("mix.wav", exclude=["Zoom"], mono=True)
record_device("MacBook Pro Speakers", "speakers.wav")   # one device stream
record_processes(["Safari"], "s.wav", mute=TapMuteBehavior.MUTED_WHEN_TAPPED)

# macOS 26+: tap by bundle ID; the tap re-attaches if the app restarts.
record_bundle_ids(["com.apple.Music"], "music.wav", restore=True)
```

Recorder and session paths accept `drift_compensation_quality=`. `None`
preserves Core Audio's default. Other values must be a
`DriftCompensationQuality` member:

```python
from catap import DriftCompensationQuality, record_system_audio

record_system_audio(
    "mix.wav",
    drift_compensation_quality=DriftCompensationQuality.HIGH,
)
```

The levels are `MINIMUM`, `LOW`, `MEDIUM`, `HIGH`, and `MAXIMUM`. Raw integers
and booleans are rejected.

### Multitrack capture

`record_multitrack` records each app as its own sample-synchronized track
through one aggregate device. Each app gets one WAV and shares one clock.
`microphone=True` adds one track per input-device stream (experimental;
requires microphone permission):

```python
from catap import record_multitrack

session = record_multitrack(["Safari", "Music"], "session/", microphone=True)
session.record_for(30)
print(session.track_labels, session.track_captured_only_silence)
```

The output directory is created automatically, including missing parents.
Explicit `output_paths=` entries must name distinct destinations; path aliases,
including relative-component, case-only, and canonically equivalent Unicode
spellings, are rejected before capture starts.

A dropped buffer group, vanished tap, format change, or other track failure
discards every track instead of publishing a desynchronized session.
Publication is transactional across errors and interruptions handled by the
running Python process. An abrupt process kill or power loss during the final
multi-file commit is not journal-recovered.

### Live tap retargeting

A running session's tap can be modified without interrupting the capture:

```python
session = record_processes(["Safari"], "out.wav")
session.start()
session.set_processes(["Safari", "Music"])   # add Music mid-capture
session.set_mute_behavior(True)              # mute the apps mid-capture
session.stop()
```

`set_processes` is for inclusive process-list taps. Global/exclusive taps use
their process list as exclusions, and bundle-ID taps have a separate target
list, so `set_processes` rejects both rather than silently reversing or mixing
their semantics.

Lower level, `get_tap_description(tap_id)` / `set_tap_description(tap_id,
description)` expose the same Core Audio property directly. Changing fields
that alter the tap's stream format (mono, mixdown, device) mid-capture makes
the recorder fail the capture rather than publish mixed-format output.

### Watching Core Audio changes

`catap.events` delivers property-change notifications on a catap-owned
dispatcher thread (never the HAL thread):

```python
from catap import events

def on_change(event: events.AudioPropertyEvent) -> None:
    print("audio processes changed", event.selector_fourcc)

with events.watch_audio_processes(on_change):
    ...  # also: watch_audio_taps, watch_audio_devices,
         # watch_default_output_device, watch_tap, watch_property
```

The CLI equivalent is `catap watch`.

### Mute Behavior

With `record_process(..., mute=True)`, the app stays muted for the lifetime
of the recording session. Pass `mute=TapMuteBehavior.MUTED_WHEN_TAPPED` to
mute only while the tap is actually being read. The lower-level mute modes
behave differently if the tap outlives the recorder; see
[`docs/mute-behavior.md`](docs/mute-behavior.md).

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
recorder = AudioRecorder(tap_id, "output.wav")
try:
    recorder.start()
    time.sleep(5)
finally:
    try:
        if recorder.is_recording or recorder.needs_cleanup:
            recorder.stop()
    finally:
        if not recorder.needs_cleanup:
            destroy_process_tap(tap_id)

print(f"Recorded {recorder.duration_seconds:.2f} seconds")
```

If `stop()` raises while `recorder.needs_cleanup` is still true, the recorder
kept the Core Audio objects it could not safely release. Call `stop()` again
to retry the teardown, and destroy the tap only once cleanup succeeds: a tap
created with mute enabled keeps its process muted for as long as the tap
exists. The high-level `RecordingSession` implements this retry contract for
you and exposes the same state as `session.needs_cleanup`; if a
`session.close()` fails, call it again to retry.

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

If permission is missing, macOS still delivers audio buffers — zeroed. A
capture that "succeeds" but contains only silence usually means the hosting
app was never granted access (or needs a restart after being granted).
Check `session.captured_only_silence` after recording; the CLI prints a
warning automatically. Permission changes take effect after the host app
restarts.

Modifying a live tap's description (`set_tap_description`,
`session.set_processes`) requires the same permission and raises
`PermissionError` without it.

Multitrack microphone tracks use the separate Microphone permission. Granting
one permission does not grant the other. If microphone tracks have audio while
every tap track reports silence, system-audio permission is missing. Check
`session.track_captured_only_silence`.

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
5. Worker output: a Python drain thread feeds one background worker per track.
   Workers write WAV data and invoke optional `on_buffer` callbacks outside
   the Core Audio real-time path.

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

### Integration tests

```bash
CATAP_RUN_INTEGRATION=1 uv run --group dev pytest -m integration
```

`CATAP_RUN_INTEGRATION=1` runs the structural Core Audio cases. Also set
`CATAP_RUN_TONE_INTEGRATION=1` for permissioned tone, live mutation,
microphone, and bundle-relaunch checks. See [`RELEASE.md`](RELEASE.md) for
host requirements and the release gate.

## License

MIT
