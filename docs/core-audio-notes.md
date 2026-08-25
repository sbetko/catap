# Core Audio Notes

## Implemented

- List audio processes (with running/input/output state), devices, streams,
  and visible taps.
- Build every public `CATapDescription` variant: process mixdowns (stereo or
  mono), global taps with exclusions, device-stream taps, and macOS 26
  bundle-ID taps with process restore.
- Create and destroy process taps; read and set tap descriptions on live
  taps (`kAudioTapPropertyDescription` is settable, TCC-gated); read tap
  stream formats.
- Translate PIDs to process objects and UIDs to taps through the dedicated
  translation properties instead of scanning.
- Watch property changes (process list, tap list, device list, default
  output device, tap format) through `AudioObjectAddPropertyListener`,
  delivered on a catap dispatcher thread.
- Record one tap through one private aggregate device, or several taps
  (plus optionally one input device) through one aggregate as
  sample-synchronized tracks. The IOProc receives one buffer per aggregate
  input stream and queues each callback as an atomic group.
- Record packed, interleaved linear PCM through the bundled native IOProc.
- Optionally select Core Audio's minimum, low, medium, high, or maximum
  drift-compensation quality for every tap in a capture aggregate; omitting
  the option leaves the operating-system default unchanged.
- Detect mid-capture tap format changes and tap destruction through
  listeners, re-verified at stop; failures discard the output.
- Drop and report audio when the native ring or Python worker falls behind;
  in multitrack, whole callback groups drop together so tracks never tear.
- `capture_failed` and `wait_for_capture_failure()` expose a sticky failure
  signal. `record_for()` and CLI waits wake early. Teardown stays on the owner
  thread.

Unsupported tap formats fail at startup. Core Audio returns tap ID 0 for an
unsatisfiable description such as a global non-mixdown tap.
`create_process_tap` turns that into an error.

## Unsupported

- Automatic continuity across sleep/wake, a vanished aggregate or IOProc, or
  format-changing device transitions. Equal-format default-output switches
  continued transparently in qualification; detectable faults fail capture
  and discard output, but catap does not reconnect them.
- Non-interleaved, compressed, padded, or unusual stream formats.
- Live mutation of a running aggregate's tap list
  (`kAudioAggregateDevicePropertyTapList`); sessions recreate instead.
- Core Audio areas outside process taps and simple device metadata:
  controls, clocks, plug-ins, and AudioServerPlugIn drivers.

## Empirical behaviors (macOS 26.2)

- A multi-tap aggregate delivers exactly one IOProc buffer per tap, in
  tap-list order; with a hardware input subdevice, the subdevice's input
  streams come first, then the taps.
- Non-mixdown taps require a device UID and stream; a global non-mixdown
  description "succeeds" with tap ID 0.
- `CATapDescription.processRestoreEnabled` defaults to true on macOS 26.
- Setting `kAudioTapPropertyDescription` without System Audio Recording
  permission fails with `!hog` (kAudioDevicePermissionsError).

## Checks

```bash
uv run --group dev pytest
CATAP_RUN_INTEGRATION=1 CATAP_RUN_TONE_INTEGRATION=1 \
  uv run --group dev pytest -m integration
```
