# Core Audio Support Matrix

`catap`'s public support boundary: what the project claims about Core Audio,
where each claim is grounded, and what is still open.

Evidence labels:

- Apple sample/docs: Apple's tap sample or Developer Documentation symbol pages.
- SDK header: declarations and comments in the installed macOS SDK headers.
- Runtime audit: local checks against the real macOS Core Audio stack.
- Unit test: normal test-suite coverage with mocked or synthetic boundaries.

| Area | catap behavior | Evidence | Status | Remaining gaps |
| --- | --- | --- | --- | --- |
| Process enumeration | Reads Core Audio process objects and resolves names, PIDs, bundle IDs, and output state. | SDK header selectors; unit tests; integration smoke test. | Supported for the public enumeration API. | Process names come from AppKit when available and may be `"Unknown"` for helper or headless processes. |
| Device enumeration | Lists devices and input/output streams, including default-device markers. | SDK header selectors; unit tests; integration smoke test. | Supported for static snapshots. | Device changes during active capture need dedicated runtime coverage. |
| Tap descriptions | Builds process, global, and device-stream `CATapDescription` values through PyObjC. | Apple sample/docs; SDK header; unit tests. | Supported for the constructors `catap` exposes. | Not every mutable `CATapDescription` property has a high-level Python property. macOS 26 `bundleIDs` and `processRestoreEnabled` are not exposed yet. |
| Process tap lifecycle | Creates taps with `AudioHardwareCreateProcessTap` and destroys owned taps with `AudioHardwareDestroyProcessTap`. | Apple sample/docs; SDK header; unit tests; runtime audit. | Supported for taps owned by a `catap` recording session. | More route-change and source-exit cases remain to be tested. |
| Tap UID lookup | Reads `kAudioTapPropertyUID` and uses the UID to attach the tap to an aggregate device. | Apple sample/docs; SDK header; runtime audit. | Supported for the recorder path. | None known for the current one-tap path. |
| Tap format lookup | Reads `kAudioTapPropertyFormat` before recording and refuses to guess a fallback format. | Apple sample/docs; SDK header; unit tests; runtime audit. | Supported for reported linear PCM formats. | Formats beyond the tested interleaved PCM path stay fail-closed until exercised. |
| Aggregate device creation | Creates a private aggregate device containing one tap using `kAudioAggregateDeviceTapListKey`. | SDK header; runtime audit; public recorder integration tests. | Supported for one private aggregate with one tap. | Multi-tap and subdevice-plus-tap aggregates are not claimed. |
| Property-based tap-list path | Setting `kAudioAggregateDevicePropertyTapList` after the aggregate exists, as in Apple's sample. | Apple sample/docs; SDK header; runtime audit. | Known to work; not the production path. | Held in reserve as a fallback if the create-time path becomes brittle. |
| IOProc lifecycle | Registers an `AudioDeviceIOProc`, starts and stops it with `AudioDeviceStart` / `AudioDeviceStop`, then destroys the IOProc. | Apple sample/docs; SDK header; runtime audit; unit tests. | Supported for normal start/stop. | Start/stop races and abnormal device-stop notifications need more probing. |
| IOProc callback shape | Expects a single interleaved input buffer for the one-tap aggregate path. | Runtime audit; unit tests around unsupported layouts. | Supported for the verified path. | Multi-buffer and non-interleaved layouts are rejected. |
| IOProc stream usage | Does not set `kAudioDevicePropertyIOProcStreamUsage` for the one-stream aggregate path. | SDK header; runtime audit. | Supported for the current one-stream aggregate. | Revisit before supporting multi-stream aggregates. |
| Worker boundary | Copies callback data into pool-owned buffers, then hands work to a background thread for WAV output or user callbacks. | Unit tests; synthetic profile; live probe. | Supported. | Callback hot-path allocations under changing buffer sizes should be watched in longer traces. |
| Bounded backpressure | Drops buffers and reports the failure on stop when the worker falls behind. | Unit tests; synthetic profile; live slow-callback probe. | Supported. | Defaults need tuning against longer real captures and slower disks. |

## Explicit Non-Claims

These areas need more work before `catap` makes any claim about them:

- Multi-tap aggregate devices.
- Aggregate devices that combine hardware subdevices and taps.
- Long-running captures across sleep/wake or default-output-device changes.
- Captures where the source process exits, restarts, or changes route mid-run.
- Exhaustive mute semantics across every `CATapMuteBehavior`.
- Non-interleaved, multi-buffer, compressed, or unusual tap formats.
- `CATapDescription.bundleIDs` and process-restore behavior added in macOS 26.
- Automated analysis of Instruments trace payloads.
- Core Audio surface area outside what's listed above — devices, streams,
  controls, plug-ins, clocks, or AudioServerPlugIn drivers.

## Maintainer Checks

Run these to keep the supported path honest:

```bash
uv run pytest
CATAP_RUN_INTEGRATION=1 uv run pytest -m integration
CATAP_RUN_TONE_INTEGRATION=1 \
  uv run pytest tests/test_integration.py::test_cli_records_headless_tone_by_audio_object_id
uv run python scripts/catap_live_probe.py --seconds 2
```

When changing recorder internals, also profile manually with Instruments'
`Audio System Trace` template.
