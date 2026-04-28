# Mute Behavior

`record_process(..., mute=True)` uses `TapMuteBehavior.MUTED`, which keeps
the app muted for the lifetime of the tap. In the managed `RecordingSession`
APIs that is effectively "muted while recording", since the tap is created
on `start()` and destroyed on `stop()`.

The lower-level tap API decouples tap lifetime from recorder lifetime, and
the two mute modes behave differently across that gap:

- `MUTED` keeps playback muted as long as the tap exists, even if nothing is
  currently reading it.
- `MUTED_WHEN_TAPPED` mutes only while an audio client is actively reading
  the tap. In `catap` that's usually a running `AudioRecorder`, but any
  other client reading the same tap has the same effect.

## Probe results

Running `scripts/catap_mute_timing_probe.py` shows:

- `MUTED_WHEN_TAPPED` stays audible through `create_process_tap()`,
  aggregate-device creation, and IOProc registration. It goes silent when
  `AudioDeviceStart(...)` begins reading the tap, and audible again when
  `AudioDeviceStop(...)` stops the read.
- `MUTED` stays audible through `create_process_tap()` but goes silent as
  soon as the aggregate device containing the tap is created. It stays
  silent after the recorder stops and only becomes audible again when the
  tap itself is destroyed.

Neither `kAudioHardwarePropertyProcessIsAudible` nor
`kAudioDevicePropertyProcessMute` tracked these transitions during the
probe, so the audible result is the more trustworthy signal of the two.

To run the probe:

```bash
uv run python scripts/catap_mute_timing_probe.py --interactive --mute-behavior MUTED_WHEN_TAPPED
uv run python scripts/catap_mute_timing_probe.py --interactive --mute-behavior MUTED
```
