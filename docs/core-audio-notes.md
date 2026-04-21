# Core Audio Implementation Notes

## Core Audio Headers

The Core Audio Tap API is documented in Apple's SDK headers, which contain
more detail than the online docs:

```bash
# Location (with Xcode Command Line Tools)
$(xcrun --show-sdk-path)/System/Library/Frameworks/CoreAudio.framework/Headers/

# Key files:
# - AudioHardware.h          - Device APIs, aggregate devices, tap properties
# - AudioHardwareTapping.h   - AudioHardwareCreateProcessTap (requires __OBJC__)
```

## Undocumented / Hard-to-Find Details

The following are in the headers but not clearly explained in Apple's online
documentation:

### 1. Taps require an aggregate device to read audio

This is the most critical discovery. You cannot register an
`AudioDeviceIOProc` directly on a tap. You must:

1. Create tap with `AudioHardwareCreateProcessTap`
2. Get tap UID via `kAudioTapPropertyUID` (`'tuid'`)
3. Create aggregate device with tap in `kAudioAggregateDeviceTapListKey`
   (`"taps"`)
4. Register IOProc on the aggregate device

### 2. Tap property selectors (four-char codes)

```python
kAudioTapPropertyUID = int.from_bytes(b'tuid', 'big')         # Get tap's UUID string
kAudioTapPropertyFormat = int.from_bytes(b'tfmt', 'big')      # Get AudioStreamBasicDescription
kAudioTapPropertyDescription = int.from_bytes(b'tdsc', 'big') # Get/set CATapDescription
```

### 3. Aggregate device dictionary keys for taps

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

## References

- [Capturing system audio with Core Audio taps](https://developer.apple.com/documentation/CoreAudio/capturing-system-audio-with-core-audio-taps) — Apple's high-level guide
- [AudioCap](https://github.com/insidegui/AudioCap) — Sample Swift implementation
- [Core Audio Tap Example](https://gist.github.com/sudara/34f00efad69a7e8ceafa078ea0f76f6f) — Minimal Objective-C example
