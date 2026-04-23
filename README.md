# catap

A Python wrapper for Apple's Core Audio Tap API (macOS 14.2+). Capture audio
from any application without loopback drivers or virtual audio devices.

## Install

```bash
pip install catap            # macOS 14.2+, Python 3.12+
```

`catap` is macOS-only. On other platforms, imports raise an `ImportError`.

Tested by the author on an Apple Silicon M5 MacBook Pro running macOS Tahoe
26.2. The package is intended for macOS 14.2 and newer, but Core Audio tap
behavior is still under-documented by Apple. Reports from other Macs and
macOS versions are welcome.

## Quick start

CLI:

```bash
catap record Safari -d 10 -o safari.wav    # record an app for 10 seconds
catap record --system -d 10 -o mix.wav     # record the full system mix
catap list-apps                            # see what's producing audio
```

Python:

```python
from catap import record_process

session = record_process("Safari", output_path="safari.wav")
session.record_for(10)

print(f"Recorded {session.duration_seconds:.2f} seconds")
```

## What It Can Do

- Record a single app, the full system mix, or an existing visible tap.
- Exclude selected apps from a system recording.
- Mute an app while recording it, so it is captured but not played aloud.
- Build taps for a specific hardware device stream.
- Write WAV files, or stream PCM buffers to your own callback.
- Keep the audio queue bounded. If the worker falls behind, dropped buffers are
  reported when recording stops.

## Usage

### Command Line

```bash
# List applications producing audio
catap list-apps

# List all audio processes (including idle ones)
catap list-apps --all

# Record from an application by exact or unambiguous partial name
catap record Safari -o ~/safari_audio.wav

# Record for a specific duration
catap record Spotify -d 30 -o ~/song.wav

# Record with app muted (capture only, no playback)
catap record Spotify --mute -d 60 -o ~/silent_capture.wav

# Record system audio
catap record --system -d 30 -o ~/system_audio.wav

# Record system audio (optionally excluding apps)
catap record --system -e Music -e Zoom -d 30 -o ~/system_audio.wav
```

### Python API

```python
from catap import record_process

# High-level API: catap manages tap creation, startup, shutdown, and cleanup.
session = record_process("Safari", output_path="output.wav")
session.record_for(5)

print(f"Recorded {session.duration_seconds:.2f} seconds")
```

If you use `on_data=...`, the callback runs on catap's background worker
thread so the Core Audio callback can stay lightweight.

If you want to control the recording lifetime yourself, use the session as a
context manager:

```python
import time
from catap import record_process

with record_process("Safari", output_path="output.wav", mute=False) as session:
    time.sleep(5)

print(f"Recorded {session.duration_seconds:.2f} seconds")
```

If you want streaming-only mode, pass `on_data=...` and omit `output_path`.

By default, `catap` queues up to 256 pending audio buffers before treating a
slow writer or callback as a capture failure. You can tune this with
`max_pending_buffers=...` on `record_process`, `record_system_audio`,
`RecordingSession`, or `AudioRecorder`.

If a process query matches more than one audio process, `catap` reports the
candidate processes instead of picking one arbitrarily.

### Mute Behavior

For `record_process(..., mute=True)`, the app stays muted for the lifetime of
the recording session. The two underlying modes (`MUTED` and
`MUTED_WHEN_TAPPED`) have different lifecycle semantics. See
[`docs/mute-behavior.md`](docs/mute-behavior.md) for empirical probe results
and when each mode transitions between audible and inaudible.

### Low-level API

For advanced use cases, the low-level API is still available:

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

tap_id = create_process_tap(tap_desc)

recorder = AudioRecorder(tap_id, "output.wav")
recorder.start()

import time
time.sleep(5)

recorder.stop()
print(f"Recorded {recorder.duration_seconds:.2f} seconds")

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

Device-targeted taps can be built directly from discovered hardware streams:

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

Core Audio Tap requires audio capture permissions. The first time you record,
macOS will prompt for permission.

If you run from a terminal (for example `uv run catap record Spotify`), macOS
attributes audio capture to that terminal app. Grant permission to Terminal,
iTerm, or whichever host app is launching `catap`.

### Permission Troubleshooting

If recording fails with permission errors:

1. Check System Settings > Privacy & Security > Screen & System Audio Recording
2. Ensure the app launching `catap` has permission (Terminal, iTerm, etc.)
3. Retry recording from the same terminal app after granting access

## How It Works

1. Process enumeration: uses Core Audio's `kAudioHardwarePropertyProcessObjectList`
   to find audio processes.
2. Tap creation: creates a `CATapDescription` via PyObjC and calls
   `AudioHardwareCreateProcessTap`.
3. Aggregate device setup: wraps the tap in an aggregate device, which Core
   Audio requires before a tap can be read.
4. Audio capture: registers an `AudioDeviceIOProc` callback to receive audio
   buffers.
5. WAV output: uses Core Audio `AudioConverter` to convert float32 audio to
   16-bit PCM before writing WAV output.

For Core Audio implementation notes (header locations, tap property codes,
aggregate-device dictionary keys, references), see
[`docs/core-audio-notes.md`](docs/core-audio-notes.md).

For the recorder's callback and queueing design, see
[`docs/performance.md`](docs/performance.md).

## Interactive lab

For a Tkinter lab that exercises process browsing, mute modes, callback
streaming, shared-tap attachment, device-stream-targeted taps, and a built-in
helper tone launcher, run:

```bash
uv sync --group dev
uv run python scripts/catap_core_lab.py
```

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

### Optional integration smoke test

```bash
CATAP_RUN_INTEGRATION=1 uv run --group dev pytest -m integration
```

This opt-in smoke test exercises the real macOS Core Audio bridge without
making the default test suite flaky. It covers both process enumeration and a
short real recording that verifies tap startup, shutdown, and WAV finalization.

See [`RELEASE.md`](RELEASE.md) for the release checklist.

## License

MIT
