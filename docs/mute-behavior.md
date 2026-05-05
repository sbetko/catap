# Mute Behavior

`record_process(..., mute=True)` uses `TapMuteBehavior.MUTED`. The app is
muted while the recording session is active because the session creates the tap
on `start()` and destroys it on `stop()`.

If you use the lower-level tap API directly, the tap can outlive the recorder.
The two mute modes behave differently in that case:

- `MUTED` keeps playback muted as long as the tap exists, even if nothing is
  currently reading it.
- `MUTED_WHEN_TAPPED` mutes only while an audio client is actively reading
  the tap. In `catap` that's usually a running `AudioRecorder`, but any
  other client reading the same tap has the same effect.

## Timing

Local listening tests showed:

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
tests, so the audible behavior is the useful signal.
