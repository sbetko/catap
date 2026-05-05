# Core Audio Notes

What works today:

- List audio processes, devices, streams, and visible taps.
- Build process, system, and device-stream tap descriptions.
- Create and destroy process taps for `RecordingSession`.
- Record one tap through one private aggregate device.
- Record packed, interleaved linear PCM through the bundled native IOProc.
- Drop and report audio when the native ring or Python worker falls behind.

The recorder rejects tap formats and layouts it has not exercised yet instead
of guessing.

## Not Covered Yet

- Multi-tap aggregates.
- Aggregates that combine hardware subdevices and taps.
- Long captures across sleep/wake, source-process restarts, route changes, or
  default-output-device changes.
- Non-interleaved, multi-buffer, compressed, padded, or unusual tap formats.
- macOS 26-only `CATapDescription` fields such as `bundleIDs` and
  process-restore behavior.
- Core Audio areas outside process taps and simple output-device metadata:
  controls, clocks, plug-ins, and AudioServerPlugIn drivers.

## Checks

```bash
uv run pytest
CATAP_RUN_INTEGRATION=1 uv run pytest -m integration
```
