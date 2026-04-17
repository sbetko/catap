# catap

Python wrapper for Apple's Core Audio Tap API (macOS 14.2+). Capture audio from any application.

## Features

- Record audio from any macOS application
- List all audio-producing processes
- Mute apps while recording (capture only, no playback)
- Simple CLI interface
- Python API for programmatic access

## Requirements

- macOS 14.2 or later (for Core Audio Tap API)
- Python 3.12+

`catap` is macOS-only. On unsupported platforms, imports fail with a clear
`ImportError` before touching the low-level macOS bindings.

## Installation

### From PyPI

```bash
# On macOS 14.2+ with Python 3.12+
pip install catap
```

### From source

```bash
git clone https://github.com/sbetko/catap.git
cd catap

uv sync --group dev
```

## Usage

### Command Line

```bash
# List applications producing audio
catap list-apps

# List all audio processes (including idle ones)
catap list-apps --all

# Record from an application (partial name match, case-insensitive)
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
with record_process("Safari", output_path="output.wav", mute=False) as session:
    import time

    time.sleep(5)

print(f"Recorded {session.duration_seconds:.2f} seconds")
```

If you use `on_data=...`, the callback runs on catap's background worker
thread so the Core Audio callback can stay lightweight.

You can also record for a fixed duration without managing the context yourself:

```python
from catap import record_process

session = record_process("Safari", output_path="output.wav")
session.record_for(5)
print(f"Recorded {session.duration_seconds:.2f} seconds")
```

For advanced use cases, the low-level API is still available:

```python
from catap import (
    AudioRecorder,
    TapDescription,
    TapMuteBehavior,
    create_process_tap,
    destroy_process_tap,
    find_process_by_name,
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

## Permissions

Core Audio Tap requires audio capture permissions. The first time you record, macOS will prompt for permission.

If you run from a terminal (for example `uv run catap record Spotify`), macOS attributes audio capture to that terminal app.
Grant permission to Terminal, iTerm, or whichever host app is launching `catap`.

### Running with uv

```bash
uv run catap record Spotify -d 10 -o output.wav
```

### Permission Troubleshooting

If recording fails with permission errors:

1. Check System Settings > Privacy & Security > Screen & System Audio Recording
2. Ensure the app launching `catap` has permission (Terminal, iTerm, etc.)
3. Retry recording from the same terminal app after granting access

## How It Works

1. **Process Enumeration**: Uses Core Audio's `kAudioHardwarePropertyProcessObjectList` to find audio processes
2. **Tap Creation**: Creates a `CATapDescription` via PyObjC and calls `AudioHardwareCreateProcessTap`
3. **Aggregate Device**: Wraps the tap in an aggregate device (required by Core Audio to read audio data)
4. **Audio Capture**: Registers an `AudioDeviceIOProc` callback to receive audio buffers
5. **WAV Output**: Converts float32 audio to 16-bit PCM and writes to WAV format

## Project Structure

```
src/catap/
├── __init__.py              # Package exports
├── __main__.py              # python -m catap entry point
├── cli.py                   # argparse CLI commands
├── bindings/
│   ├── process.py           # Process enumeration (ctypes)
│   ├── tap_description.py   # CATapDescription wrapper (PyObjC)
│   └── hardware.py          # Tap create/destroy (ctypes)
├── core/
│   └── recorder.py          # AudioRecorder class
```

## Development

### Quality checks

```bash
uv sync --group dev
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
making the default test suite flaky.

See [`RELEASE.md`](RELEASE.md) for the release checklist.

### Core Audio Headers

The Core Audio Tap API is documented in Apple's SDK headers, which contain more detail than the online docs:

```bash
# Location (with Xcode Command Line Tools)
$(xcrun --show-sdk-path)/System/Library/Frameworks/CoreAudio.framework/Headers/

# Key files:
# - AudioHardware.h          - Device APIs, aggregate devices, tap properties
# - AudioHardwareTapping.h   - AudioHardwareCreateProcessTap (requires __OBJC__)
```

### Undocumented / Hard-to-Find Details

The following are in the headers but not clearly explained in Apple's online documentation:

**1. Taps require an aggregate device to read audio**

This is the most critical discovery. You cannot register an `AudioDeviceIOProc` directly on a tap. You must:
1. Create tap with `AudioHardwareCreateProcessTap`
2. Get tap UID via `kAudioTapPropertyUID` (`'tuid'`)
3. Create aggregate device with tap in `kAudioAggregateDeviceTapListKey` (`"taps"`)
4. Register IOProc on the aggregate device

**2. Tap property selectors (four-char codes)**

```python
kAudioTapPropertyUID = int.from_bytes(b'tuid', 'big')         # Get tap's UUID string
kAudioTapPropertyFormat = int.from_bytes(b'tfmt', 'big')      # Get AudioStreamBasicDescription
kAudioTapPropertyDescription = int.from_bytes(b'tdsc', 'big') # Get/set CATapDescription
```

**3. Aggregate device dictionary keys for taps**

```python
# Keys for AudioHardwareCreateAggregateDevice dictionary
"name"          # kAudioAggregateDeviceNameKey
"uid"           # kAudioAggregateDeviceUIDKey
"private"       # kAudioAggregateDeviceIsPrivateKey (1 = not visible system-wide)
"taps"          # kAudioAggregateDeviceTapListKey (array of tap dictionaries)
"tapautostart"  # kAudioAggregateDeviceTapAutoStartKey

# Keys for each tap in the "taps" array
"uid"           # kAudioSubTapUIDKey - the tap's UUID from kAudioTapPropertyUID
"drift"         # kAudioSubTapDriftCompensationKey (1 = enable)
```

### References

- [Capturing system audio with Core Audio taps](https://developer.apple.com/documentation/CoreAudio/capturing-system-audio-with-core-audio-taps) - Apple's high-level guide
- [AudioCap](https://github.com/insidegui/AudioCap) - Sample Swift implementation
- [Core Audio Tap Example](https://gist.github.com/sudara/34f00efad69a7e8ceafa078ea0f76f6f) - Minimal Objective-C example

## License

MIT
