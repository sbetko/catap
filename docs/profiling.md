# Profiling Notes

This project was profiled on April 17, 2026 on `Darwin 25.2.0 arm64` with
`Python 3.12.12`.

Reproduce the measurements with:

```bash
uv run python scripts/profile_catap.py
```

If you want the repeatable synthetic measurements without touching live macOS
audio capture, run:

```bash
uv run python scripts/profile_catap.py --skip-live
```

## Current Results

### 1. Float conversion is the dominant CPU hotspot

Synthetic conversion of the current 48 kHz stereo float32 buffers measured:

| Workload | Input throughput | Realtime factor |
| --- | ---: | ---: |
| 512-frame buffers x 20,000 | about 85 MB/s | about 231x |
| 4096-frame buffers x 3,000 | about 86 MB/s | about 236x |

`cProfile` attributes almost all of that time to
[`_float32_to_int16`](../src/catap/recorder.py), but the hot path is now much
smaller than before because the conversion no longer copies the input into an
intermediate `array('f')` and no longer pays generator overhead.

### 2. The real-time callback has a large safety margin

On this machine, a live `record_system_audio(..., on_data=...)` sample produced
exactly 512-frame callbacks at a 10.67 ms cadence (48 kHz stereo).

The synthetic `_io_proc` benchmark measured:

| Path | Cost per callback |
| --- | ---: |
| Copy + frame counting, no queue | about 0.8 us |
| Copy + queue put | about 1.6 us |

That is well under 1% of the 10.67 ms callback budget. The callback path is
not currently the bottleneck.

### 3. The worker thread is limited by conversion, not disk I/O

Feeding the worker 12,000 synthetic 512-frame buffers (128 seconds of audio)
completed in about 0.63 seconds, or about 204x realtime.

`cProfile` for the worker loop shows:

- `_float32_to_int16`: ~93% of cumulative time
- `wave.writeframesraw`: low single-digit percentage
- queue bookkeeping: low single-digit percentage

This means the WAV output path is CPU-bound in Python sample conversion, not in
the file write itself.

### 4. Process listing is fast once the interpreter is warm

Hot `list_audio_processes()` calls measured:

| Metric | Time |
| --- | ---: |
| Mean | about 7.8 ms |
| Median | about 5.7 ms |
| P95 | about 8.0 ms |

The hot-path profile is dominated by Core Audio property lookups in
[`get_property_bytes`](../src/catap/bindings/_coreaudio.py). Python-side sorting
and unpacking are negligible compared with the bridge calls.

### 5. Cold-start latency is mostly import cost

Subprocess timings:

| Command | Mean wall time |
| --- | ---: |
| `python -c 'import catap'` | about 71 ms |
| `python -m catap list-apps` | about 119 ms |

`python -X importtime -c 'import catap'` showed the heaviest imports were:

- `AppKit`: ~27 ms
- `Foundation`: ~15 ms
- `objc`: ~13 ms

For short-lived CLI commands, import cost matters more than the steady-state
library hot path.

### 6. Live recording startup is much more expensive than shutdown

Five short live captures (`session.start()`, 50 ms hold, `session.stop()`)
measured:

| Phase | Mean |
| --- | ---: |
| Start | about 30 ms |
| Stop | about 2.8 ms |

This is expected for the current architecture because startup has to create the
tap wrapper aggregate device, query format, register the IO proc, and start the
device.

## Optimization Candidates

1. Replace or accelerate `_float32_to_int16`.
   This is the clearest optimization target. The current pure-Python loop is
   the main cost in both synthetic and end-to-end worker profiling.

2. Reduce cold import overhead for CLI use cases.
   `catap.__init__` eagerly imports process, tap, recorder, and session modules,
   which pulls in AppKit, Foundation, and objc before every CLI subcommand.
   Lazy imports would improve short-lived command latency more than tuning
   `list_audio_processes()`, but the likely gain is only on the order of a few
   tens of milliseconds for `list-apps`, so this is a secondary option rather
   than a must-do.

3. Leave `_io_proc` alone unless regression data says otherwise.
   It is already comfortably below the callback time budget, so changes there
   are higher risk than reward.

4. Treat process-list optimization as optional.
   Once warm, the library already lists audio processes in single-digit
   milliseconds. Any improvement here is secondary to conversion and import
   costs.

## Memory Notes

The live callback shape on this machine was 512 frames, 2 channels, float32:

- bytes per callback buffer: `512 * 2 * 4 = 4096`
- default `max_pending_buffers=256` raw audio queue capacity: about `1.0 MiB`
- integration test `max_pending_buffers=64` raw audio queue capacity: about
  `256 KiB`

That queue sizing looks reasonable for the current worker throughput.
