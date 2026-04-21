# Mute Behavior

For the high-level `record_process(..., mute=True)` flow, `catap` uses
`TapMuteBehavior.MUTED`, which means the app stays muted for the lifetime of
the tap. In the managed `RecordingSession` APIs that is usually equivalent to
"muted while recording" because the tap is created on `start()` and destroyed
on `stop()`.

At the lower-level tap API, the tap lifetime and the recorder lifetime are
separate. This matters for the two mute modes:

- `MUTED` keeps playback muted as long as the tap exists, even if no recorder
  is currently reading from it.
- `MUTED_WHEN_TAPPED` is closer to "muted while the tap is actively being read
  by an audio client." In `catap`, that usually means while an `AudioRecorder`
  is running, but another client reading the same tap would have the same
  effect.

## Empirical phase probe

Empirical phase testing with `scripts/catap_mute_timing_probe.py` found the
following behavior:

- `MUTED_WHEN_TAPPED` stays audible through `create_process_tap()`,
  aggregate-device creation, and IO-proc registration. It becomes inaudible
  when `AudioDeviceStart(...)` begins actively reading the tap, and becomes
  audible again when `AudioDeviceStop(...)` stops that read activity.
- `MUTED` stays audible through `create_process_tap()`, but becomes inaudible
  once the aggregate device containing the tap is created. It stays inaudible
  after the recorder stops and only becomes audible again when the tap itself
  is destroyed.

The same probe also found that Core Audio properties such as
`kAudioHardwarePropertyProcessIsAudible` and
`kAudioDevicePropertyProcessMute` did not reflect these transitions, so the
interactive audible result from the probe is currently the more trustworthy
signal than those properties.

To run the manual phase probe:

```bash
uv run python scripts/catap_mute_timing_probe.py --interactive --mute-behavior MUTED_WHEN_TAPPED
uv run python scripts/catap_mute_timing_probe.py --interactive --mute-behavior MUTED
```
