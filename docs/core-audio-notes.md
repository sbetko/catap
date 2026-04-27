# Core Audio Implementation Notes

This file records the Core Audio assumptions that `catap` relies on. Apple now
publishes a Core Audio tap sample and symbol reference; the SDK headers remain
useful for exact selector values, dictionary key strings, ownership notes, and
details that are terse in the web docs.

## Primary Sources

- Apple's sample: [Capturing system audio with Core Audio taps](https://developer.apple.com/documentation/CoreAudio/capturing-system-audio-with-core-audio-taps)
- Apple's profiling guide: [Analyzing audio performance with Instruments](https://developer.apple.com/documentation/audiotoolbox/analyzing-audio-performance-with-instruments)
- SDK headers:

```bash
$(xcrun --show-sdk-path)/System/Library/Frameworks/CoreAudio.framework/Headers/
```

The most relevant headers are `AudioHardware.h`, `AudioHardwareTapping.h`, and
`CATapDescription.h`.

## Capture Flow

`catap` follows the same object model as Apple's sample:

1. Build a `CATapDescription`.
2. Create a tap with `AudioHardwareCreateProcessTap`.
3. Create a private aggregate device that contains the tap.
4. Register an `AudioDeviceIOProc` on the aggregate device, not on the tap.
5. Start and stop the IOProc with `AudioDeviceStart` / `AudioDeviceStop`.
6. Destroy the IOProc, aggregate device, and any tap owned by the session.

Apple's sample creates the aggregate device first and then sets
`kAudioAggregateDevicePropertyTapList`. `catap` currently supplies
`kAudioAggregateDeviceTapListKey` in the create-description dictionary. Both
surfaces are public: the sample documents the property path, while the header
documents the create-time dictionary key.

## Tap Properties

The tap object has global-scope properties only. `catap` uses:

```python
kAudioTapPropertyUID = int.from_bytes(b"tuid", "big")
kAudioTapPropertyFormat = int.from_bytes(b"tfmt", "big")
kAudioTapPropertyDescription = int.from_bytes(b"tdsc", "big")
```

`kAudioTapPropertyFormat` returns the `AudioStreamBasicDescription` for audio
that will be visible through an aggregate device containing the tap. Recording
should fail if this property cannot be read; guessing a default format risks
corrupt output.

## Aggregate Device Keys

The aggregate-device create dictionary uses string keys from `AudioHardware.h`:

```python
"name"          # kAudioAggregateDeviceNameKey
"uid"           # kAudioAggregateDeviceUIDKey
"private"       # kAudioAggregateDeviceIsPrivateKey
"taps"          # kAudioAggregateDeviceTapListKey
"tapautostart"  # kAudioAggregateDeviceTapAutoStartKey
```

Each tap entry uses:

```python
"uid"    # kAudioSubTapUIDKey
"drift"  # kAudioSubTapDriftCompensationKey
```

Private aggregate devices are scoped to the creating process and are not
persistent across launches.

## Device-Stream Taps

`CATapDescription` has initializers for processes routed to a selected device
stream. The header describes this as an output-device stream: the selected
device UID and stream index identify the destination stream whose process audio
will be captured. `catap` rejects discovered input streams for these helpers.

## Permissions

For app bundles, include `NSAudioCaptureUsageDescription` in `Info.plist` so
macOS can present the audio-capture permission prompt. Apple's tap sample uses
that usage-description key and normal sandbox entitlements; it does not include
a separate system-audio-capture entitlement.

When running from a terminal, macOS attributes capture to the terminal app, so
Terminal, iTerm, or the launching host needs permission under System Settings >
Privacy & Security > Screen & System Audio Recording.

## Profiling

Use Instruments' `Audio System Trace` template for live Core Audio runs. The
tracks most relevant to `catap` are `Audio Client`, `Audio Statistics`, and
`Audio Server`; they show IOProc timing, engine jitter, I/O cycle load,
overloads, and related points of interest.

## Empirical Notes

Mute lifecycle behavior is still tracked in [mute-behavior.md](mute-behavior.md)
because the observable transitions depend on tap lifetime, aggregate-device
creation, and whether a client is actively reading the tap.

Future audit targets:

- Consider a fallback path that attaches taps with
  `kAudioAggregateDevicePropertyTapList`, matching Apple's sample exactly, if
  create-time tap dictionaries prove brittle across macOS releases.
- Revisit `kAudioDevicePropertyIOProcStreamUsage` only if `catap` starts
  building aggregate devices with multiple active streams.
- Review CF/Objective-C ownership for object-valued Core Audio properties if
  long-running tap discovery becomes a hot path.
