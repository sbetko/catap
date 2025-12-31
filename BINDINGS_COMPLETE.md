# Core Audio Tap Bindings - Implementation Complete

## Summary

All core bindings for Apple's Core Audio Tap API have been successfully implemented and tested. The Python wrapper provides full access to process enumeration, tap description creation, and tap lifecycle management.

## Implemented Components

### 1. Process Enumeration (`bindings/process.py`) ✓

**Status:** Working

**Features:**
- List all audio-registered processes
- Get process details (PID, bundle ID, name, audio object ID)
- Check if process is actively outputting audio
- Find processes by name (partial match)

**Implementation:**
- Uses ctypes to call Core Audio framework functions directly
- `AudioObjectGetPropertyDataSize` and `AudioObjectGetPropertyData` for property access
- Integrates with NSWorkspace/NSRunningApplication for app name resolution

**Test Results:**
```bash
$ uv run catap list-apps
Status Name                           Bundle ID                                Audio ID   PID
--------------------------------------------------------------------------------------------
♪  Safari Graphics and Media      com.apple.WebKit.GPU                     120        837
♪  Spotify                        com.spotify.client                       133        18258
```

### 2. Tap Description Wrapper (`bindings/tap_description.py`) ✓

**Status:** Working

**Features:**
- Full Python wrapper for Objective-C `CATapDescription` class
- Factory methods for common tap configurations:
  - `stereo_mixdown_of_processes()` - Mix specific processes to stereo
  - `stereo_global_tap_excluding()` - Global tap excluding processes
  - `mono_mixdown_of_processes()` - Mix to mono
  - `mono_global_tap_excluding()` - Global mono tap
- Properties: name, UUID, processes, mute_behavior, is_private, etc.
- `TapMuteBehavior` enum (UNMUTED, MUTED, MUTED_WHEN_TAPPED)

**Implementation:**
- Uses PyObjC's `objc.lookUpClass()` to access CATapDescription
- Python properties map to Objective-C properties
- Handles NSArray/NSNumber conversions

**Example Usage:**
```python
from catap import TapDescription, TapMuteBehavior

tap_desc = TapDescription.stereo_mixdown_of_processes([120])
tap_desc.name = "My Tap"
tap_desc.is_private = True
tap_desc.mute_behavior = TapMuteBehavior.UNMUTED
```

### 3. Hardware Functions (`bindings/hardware.py`) ✓

**Status:** Working

**Features:**
- `create_process_tap(description)` - Create a new tap
- `destroy_process_tap(tap_id)` - Destroy an existing tap

**Implementation:**
- ctypes bindings for C functions:
  - `AudioHardwareCreateProcessTap`
  - `AudioHardwareDestroyProcessTap`
- Proper PyObjC pointer extraction using `__c_void_p__()`
- Returns AudioObjectID for created taps

**Test Results:**
```bash
$ uv run python -c "from catap import *; ..."
Found 27 audio processes
Testing tap for: Unknown (ID: 108)
Created tap description: TapDescription(...)
✓ Created tap with ID: 135
✓ Destroyed tap 135

SUCCESS: Tap creation and destruction working!
```

## Package Structure

```
src/catap/
├── __init__.py           # Exports: TapDescription, TapMuteBehavior,
│                         #         create_process_tap, destroy_process_tap,
│                         #         AudioProcess, list_audio_processes, find_process_by_name
├── cli.py                # Commands: test-bundle, list-apps, record (skeleton)
├── __main__.py           # python -m catap entry point
├── bindings/
│   ├── __init__.py
│   ├── process.py        # Process enumeration (ctypes)
│   ├── tap_description.py # CATapDescription wrapper (PyObjC)
│   └── hardware.py       # Tap create/destroy functions (ctypes)
├── bundle/
│   ├── __init__.py
│   └── launcher.py       # Bundle detection and relaunch
└── catap.app/            # App bundle stub for permissions
    └── Contents/
        ├── Info.plist
        ├── PkgInfo
        └── MacOS/catap
```

## CLI Commands

### `catap list-apps`
List applications producing audio

```bash
$ uv run catap list-apps
# Shows only apps currently outputting audio

$ uv run catap list-apps --all
# Shows all registered audio processes
```

### `catap test-bundle`
Verify app bundle configuration

```bash
$ uv run catap test-bundle
# Tests bundle structure and permissions setup
```

### `catap record` (Skeleton)
Record audio from an application

*Note: Not yet implemented - requires audio recording logic*

## Technical Details

### PyObjC Integration
- CATapDescription accessed via `objc.lookUpClass('CATapDescription')`
- Objective-C object pointers extracted using `obj.__c_void_p__()`
- NSArray/NSNumber conversions handled transparently

### ctypes Integration
- Direct C function calls to CoreAudio framework
- Property addresses as `(selector, scope, element)` tuples
- Four-char codes converted to integers: `int.from_bytes(b'prs#', 'big')`

### Property Constants
```python
kAudioObjectSystemObject = 1
kAudioObjectPropertyScopeGlobal = int.from_bytes(b'glob', 'big')
kAudioHardwarePropertyProcessObjectList = int.from_bytes(b'prs#', 'big')
kAudioProcessPropertyPID = int.from_bytes(b'ppid', 'big')
kAudioProcessPropertyBundleID = int.from_bytes(b'pbid', 'big')
kAudioProcessPropertyIsRunningOutput = int.from_bytes(b'piro', 'big')
```

## Audio Recording Implementation (Complete)

### AudioRecorder Class (`core/recorder.py`)

**Status:** Working

**Key Discovery:** Core Audio taps cannot be read from directly - they must be wrapped in an aggregate device.

**Implementation Flow:**
1. Get tap UID using `kAudioTapPropertyUID`
2. Create aggregate device with `AudioHardwareCreateAggregateDevice` containing the tap
3. Register `AudioDeviceIOProc` callback with the aggregate device
4. Start device with `AudioDeviceStart`
5. Receive audio buffers in callback, accumulate data
6. Stop device and destroy aggregate device on completion
7. Convert float32 to int16 PCM and write WAV file

**Test Results:**
```bash
$ catap record Spotify -d 3 -o output.wav
Recording from: Spotify (PID: 24007)
Output: output.wav
Created tap (ID: 136)
Recording for 3.0 seconds... (Ctrl+C to stop early)
Recorded 3.00 seconds
Saved to: output.wav
```

### Bundle Binary Wrapper

The app bundle now includes a compiled Mach-O binary wrapper that executes the shell script launcher. This is required for `open` command compatibility:

```bash
# All three methods work:
./src/catap/catap.app/Contents/MacOS/catap record Spotify -d 5 -o out.wav
uv run catap record Spotify -d 5 -o out.wav
open ./src/catap/catap.app --args record Spotify -d 5 -o out.wav
```

## Potential Future Enhancements

1. **Streaming output** - Write audio to file in real-time instead of buffering
2. **Multiple format support** - Add MP3, FLAC, AAC output options
3. **Real-time monitoring** - Add audio level meters during recording
4. **Multiple process capture** - Record from multiple apps simultaneously

## Dependencies

- `pyobjc-core>=11.0` - Objective-C bridge
- `pyobjc-framework-Cocoa>=11.0` - NSWorkspace, NSRunningApplication
- `pyobjc-framework-CoreAudio>=11.0` - CATapDescription class
- `click>=8.0` - CLI framework

## System Requirements

- macOS 14.2+ (for AudioHardwareCreateProcessTap)
- macOS 12.0+ (for CATapDescription)
- Python 3.12+

## Success Metrics

- ✓ Process enumeration works (27 processes found)
- ✓ list-apps command shows audio-producing apps
- ✓ CATapDescription wrapper creates valid tap descriptions
- ✓ Tap creation succeeds (tap ID 135 created)
- ✓ Tap destruction succeeds (tap 135 destroyed)
- ✓ Bundle stub validated and working
- ✓ All core APIs accessible from Python
- ✓ Audio recording works via aggregate device
- ✓ WAV file output with float32→int16 conversion
- ✓ CLI record command fully functional
- ✓ Bundle binary wrapper enables `open` command

**catap is fully functional for capturing audio from any macOS application.**
