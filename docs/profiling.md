# Profiling Notes

This project was profiled on April 17, 2026 on `Darwin 25.2.0 arm64` with
`Python 3.12.12`. These numbers reflect the current implementation after the
Accelerate/vDSP conversion, the scratch-buffer reuse inside `_float32_to_int16`,
the ctypes buffer pool shared between the Core Audio callback and the worker
thread, and the removal of the hot-path mutex.

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

### 1. Float conversion throughput on the worker thread

Synthetic conversion of the current 48 kHz stereo float32 buffers measured:

| Workload | Input throughput | Realtime factor | p99 per call |
| --- | ---: | ---: | ---: |
| 512-frame buffers x 20,000 | ~2,540 MB/s | ~6,900x | ~1.7 us |
| 4096-frame buffers x 3,000 | ~9,000 MB/s | ~24,600x | ~5.4 us |

Allocations per call are under 1 byte on average because the Accelerate
scratch float buffer and the per-size int16 output buffer are both reused
across calls. `cProfile` attributes essentially all of the remaining time to
`_float32_to_int16` itself (`ctypes.memmove` + three vDSP routines + a
`bytes(ints)` copy of the int16 output).

### 2. The real-time callback is well under budget

On this machine, a live `record_system_audio(..., on_data=...)` sample produced
exactly 512-frame callbacks at a 10.66 ms cadence (48 kHz stereo).

The synthetic `_io_proc` benchmark measured:

| Path | Mean per callback | p99 | Peak tracked bytes over 2,000 calls |
| --- | ---: | ---: | ---: |
| Pool acquire + memmove, no queue | ~0.8 us | ~0.9 us | ~1,000 B |
| Pool acquire + memmove + queue put | ~1.1 us | ~1.9 us | ~265 KB |

Both paths are well under 1% of the 10.66 ms callback budget. The
`with_queue` peak is dominated by the 5,000 `(buf, num_frames, byte_count)`
tuples the queue retains during the benchmark; the per-callback retained cost
is about 132 B, versus about 4.2 KB before the buffer pool was introduced.

### 3. Worker pipeline is still CPU-bound in conversion

Feeding the worker 12,000 synthetic 512-frame buffers (128 seconds of audio)
completed in about 0.10 seconds, or roughly 1,330x realtime. The worker ran at
about 124% CPU utilization, indicating it is bottlenecked on the Python-side
conversion rather than on disk I/O.

Per-buffer retained memory is now 0.25 B (down from ~3.5 B), and `cProfile`
attributes cumulative time to:

- `wave.writeframesraw` (WAV writer + buffered file write): ~35-40%
- `_float32_to_int16`: ~30-35%
- queue bookkeeping + worker loop: single-digit percentages

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

## Optimization Candidates

1. Explore Core Audio `AudioConverter` / `ExtAudioFile` for the
   float32 -> int16 + WAV path.
   The Python-side conversion is already fast (tens of GB/s on large buffers),
   but handing the whole path off to Core Audio could drop CPU further and
   would move the WAV writer work out of the GIL-serialized path.

2. Reduce cold import overhead for CLI use cases.
   `catap.__init__` eagerly imports process, tap, recorder, and session
   modules, pulling AppKit, Foundation, and objc before every CLI subcommand.
   Lazy imports would improve short-lived command latency. Likely gain is only
   a few tens of milliseconds, so this is a nice-to-have.

3. Leave `_io_proc` alone unless regression data says otherwise.
   After the buffer pool and mutex removal, the callback runs at about 1.1 us
   with the worker queue attached - roughly 1/10,000 of the 10.66 ms budget.
   Further changes there are higher risk than reward.

4. Treat process-list optimization as optional.
   Once warm, the library already lists audio processes in single-digit
   milliseconds. Any improvement here is secondary to conversion and import
   costs.

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
