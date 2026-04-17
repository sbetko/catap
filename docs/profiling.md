# Profiling Notes

This project was profiled on April 17, 2026 on `macOS-26.2-arm64-arm-64bit` with
`Python 3.12.12`. These numbers reflect the current implementation after the
AudioConverter-backed float32 -> int16 WAV conversion, the retained standalone
Accelerate/vDSP helper for comparison, the ctypes buffer pool shared between
the Core Audio callback and the worker thread, and the removal of the hot-path
mutex. The synthetic sections below were refreshed after the AudioConverter
integration; the live sections later in the document were not re-run because
the change did not affect startup, callback shape, or process enumeration.

Reproduce the measurements with:

```bash
uv run python scripts/profile_catap.py
```

The profiler reports per-call distributions (p50/p95/p99/max) alongside
`tracemalloc` peak/retained bytes and `getrusage` CPU time, so any future
regression in latency, memory, or CPU utilization shows up in the same report.

If you want the repeatable synthetic measurements without touching live macOS
audio capture, run:

```bash
uv run python scripts/profile_catap.py --skip-live
```

## Current Results

### 1. AudioConverter is now the active float conversion path

Synthetic conversion of the current 48 kHz stereo float32 buffers measured:

| Workload | `AudioConverter` throughput | Historical vDSP throughput | `AudioConverter` p99 |
| --- | ---: | ---: | ---: |
| 512-frame buffers x 20,000 | ~3,870 MB/s | ~2,570 MB/s | ~1.2 us |
| 4096-frame buffers x 3,000 | ~24,000 MB/s | ~12,200 MB/s | ~1.5 us |

The standalone `AudioConverter` wrapper retains under 0.2 B per call on
average and roughly doubles throughput versus the old vDSP helper on large
buffers. One compatibility caveat remains: the converter is not byte-identical
to `_float32_to_int16`; around 88-90% of samples differ by 1-2 LSBs because
Core Audio rounds to the full int16 range (`0.5 -> 16384`, `-1.0 -> -32768`)
instead of the legacy truncate-toward-zero mapping.

### 2. The real-time callback is well under budget

On this machine, a live `record_system_audio(..., on_data=...)` sample produced
exactly 512-frame callbacks at a 10.66 ms cadence (48 kHz stereo).

The synthetic `_io_proc` benchmark measured:

| Path | Mean per callback | p99 | Peak tracked bytes over 2,000 calls |
| --- | ---: | ---: | ---: |
| Pool acquire + memmove, no queue | ~0.8 us | ~0.9 us | ~1,000 B |
| Pool acquire + memmove + queue put | ~1.1 us | ~2.0 us | ~265 KB |

Both paths are well under 1% of the 10.66 ms callback budget. The
`with_queue` peak is dominated by the 5,000 `(buf, num_frames, byte_count)`
tuples the queue retains during the benchmark; the per-callback retained cost
is about 132 B, versus about 4.2 KB before the buffer pool was introduced.

### 3. Worker pipeline is much faster, and conversion is no longer the main Python hotspot

Feeding the worker 12,000 synthetic 512-frame buffers (128 seconds of audio)
now completes in about 0.054 seconds, or roughly 2,370x realtime. The worker
ran at about 128% CPU utilization, but the end-to-end path is now about 75%
faster than the previous vDSP-backed worker measurement (~0.10 s).

Per-buffer retained memory is now 0.12 B. In `cProfile`, the visible Python
time is dominated by:

- `wave.writeframesraw` + buffered `file.write`: most of the measured time
- queue bookkeeping / `queue.get`: the next largest Python cost
- worker-loop overhead: small

The converter itself no longer shows up as a Python hotspot because it runs
inside one C call; the resource-usage delta is the more trustworthy signal for
this path.

### 4. Process listing is fast once the interpreter is warm

Hot `list_audio_processes()` calls measured:

| Metric | Time |
| --- | ---: |
| Mean | ~8.5 ms |
| Median | ~6.2 ms |
| P95 | ~8.5 ms |

The hot-path profile is dominated by Core Audio property lookups in
[`get_property_bytes`](../src/catap/bindings/_coreaudio.py). Python-side sorting
and unpacking are negligible compared with the bridge calls.

### 5. Cold-start latency is mostly import cost

Subprocess timings:

| Command | Mean wall time |
| --- | ---: |
| `python -c 'import catap'` | ~63 ms |
| `python -m catap list-apps` | ~113 ms |

`python -X importtime -c 'import catap'` shows the heaviest imports are
`AppKit`, `Foundation`, and `objc`. For short-lived CLI commands, import cost
matters more than the steady-state library hot path.

### 6. Live recording startup is much more expensive than shutdown

Five short live captures (`session.start()`, 300 ms hold, `session.stop()`)
measured:

| Phase | Mean |
| --- | ---: |
| Start | ~31 ms |
| Stop | ~6.7 ms |

This is expected for the current architecture: startup creates the tap wrapper
aggregate device, queries format, registers the IO proc, pre-allocates the
buffer pool, and starts the device.

### 7. `ExtAudioFileWrite` still does not beat the new current path

The worker now uses `AudioConverter` behind the existing `wave` writer. In a
matched direct synthetic write loop over 12,000 x 512-frame buffers (128
seconds of audio), the measured write paths were:

| Path | Elapsed | Realtime factor |
| --- | ---: | ---: |
| Current `AudioConverter` + `wave.writeframesraw` | ~33 ms | ~3,930x |
| Legacy vDSP + `wave.writeframesraw` | ~37 ms | ~3,470x |
| `ExtAudioFileWrite` | ~40 ms | ~3,210x |

So integrating `AudioConverter` paid off, but synchronous `ExtAudioFileWrite`
still does not beat the simpler direct WAV writer on this machine.

Historical note: the vDSP path is still kept in-tree in this commit only so
these before/after comparisons remain directly reproducible.

## Optimization Candidates

1. Reduce cold import overhead for CLI use cases.
   `catap.__init__` eagerly imports process, tap, recorder, and session
   modules, pulling AppKit, Foundation, and objc before every CLI subcommand.
   Lazy imports would improve short-lived command latency. Likely gain is only
   a few tens of milliseconds, so this is a nice-to-have.

2. Leave `_io_proc` alone unless regression data says otherwise.
   After the buffer pool and mutex removal, the callback runs at about 1.1 us
   with the worker queue attached - roughly 1/10,000 of the 10.66 ms budget.
   Further changes there are higher risk than reward.

3. Treat process-list optimization as optional.
   Once warm, the library already lists audio processes in single-digit
   milliseconds. Any improvement here is secondary to conversion and import
   costs.

4. If we revisit the write path, prefer batching or `ExtAudioFileWriteAsync`
   experiments over synchronous `ExtAudioFileWrite`.
   The synchronous API was slower than the new current path in matched
   synthetic runs, so any further AudioToolbox work should target a different
   shape of optimization.

## Memory Notes

The live callback shape on this machine was 512 frames, 2 channels, float32:

- bytes per callback buffer: `512 * 2 * 4 = 4096`
- default `max_pending_buffers=256`: ~1.0 MiB raw audio queue capacity, plus a
  matching pool of 256 pre-allocated 8 KiB ctypes buffers (~2.0 MiB) reused
  between the Core Audio callback and the worker thread
- integration test `max_pending_buffers=64`: ~256 KiB queue plus ~512 KiB pool

The pool trades a fixed up-front allocation for zero per-callback buffer
allocations in steady state, which keeps the real-time thread off the Python
allocator and caps the memory the worker queue can retain.
