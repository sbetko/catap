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

## Next Steps

The core bindings are complete and working. Remaining tasks:

1. **Audio Recording Implementation** - Implement actual audio capture from taps
   - Use AudioDeviceCreateIOProcID to read from tap device
   - Handle AudioBufferList structures
   - Write to WAV/audio files

2. **Implement `catap record` command** - Wire up the bindings to the CLI
   - Bundle relaunch for permissions
   - Tap lifecycle management
   - Audio capture and file output

3. **Error Handling** - Add better error messages for common failure modes
   - Permission denied
   - Invalid process IDs
   - Tap creation failures

4. **Documentation** - Add API documentation and usage examples
   - README with examples
   - API reference
   - Tutorial for common use cases

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

**The Core Audio Tap bindings are production-ready for tap management. Audio recording implementation is the final piece.**
