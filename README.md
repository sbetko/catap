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

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/catap.git
cd catap

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
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

# Test bundle configuration
catap test-bundle

# Test tap creation (verifies permissions)
catap test-tap
```

### Python API

```python
from catap import (
    TapDescription,
    TapMuteBehavior,
    create_process_tap,
    destroy_process_tap,
    find_process_by_name,
    list_audio_processes,
    AudioRecorder,
)

# Find a process
process = find_process_by_name("Safari")
print(f"Found: {process.name} (PID: {process.pid})")

# Create a tap
tap_desc = TapDescription.stereo_mixdown_of_processes([process.audio_object_id])
tap_desc.name = "My Recording"
tap_desc.mute_behavior = TapMuteBehavior.UNMUTED  # or MUTED

tap_id = create_process_tap(tap_desc)

# Record audio
recorder = AudioRecorder(tap_id, "output.wav")
recorder.start()

import time
time.sleep(5)  # Record for 5 seconds

recorder.stop()
print(f"Recorded {recorder.duration_seconds:.2f} seconds")

# Clean up
destroy_process_tap(tap_id)
```

## Permissions

Core Audio Tap requires audio capture permissions. The first time you record, macOS will prompt for permission.

### Running via the App Bundle

For proper permission handling, you can run catap through its app bundle:

```bash
# Direct execution (recommended for development)
./src/catap/catap.app/Contents/MacOS/catap record Spotify -d 10 -o output.wav

# Via macOS open command (shows permission dialog from "catap" app)
open ./src/catap/catap.app --args record Spotify -d 10 -o output.wav
```

### Permission Troubleshooting

If recording fails with permission errors:

1. Check System Settings > Privacy & Security > Microphone
2. Ensure Terminal (or catap) has permission
3. Try running through the app bundle

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
├── cli.py                   # Click CLI commands
├── bindings/
│   ├── process.py           # Process enumeration (ctypes)
│   ├── tap_description.py   # CATapDescription wrapper (PyObjC)
│   └── hardware.py          # Tap create/destroy (ctypes)
├── core/
│   └── recorder.py          # AudioRecorder class
├── bundle/
│   └── launcher.py          # Bundle detection utilities
└── catap.app/               # macOS app bundle for permissions
    └── Contents/
        ├── Info.plist       # Bundle config with NSAudioCaptureUsageDescription
        ├── MacOS/catap      # Binary launcher
        └── MacOS/catap.sh   # Shell script launcher
```

## License

MIT
