# Performance and Real-Time Notes

`catap` has one hard boundary: the Core Audio callback must stay boring.

The callback copies each incoming Core Audio buffer into a pool-owned ctypes
buffer, then tries to enqueue that buffer for the background worker. It does
not call user code, write files, allocate in the steady state, or wait for the
worker. User callbacks and WAV writes happen on the `catap-audio-worker`
thread.

## Queueing Model

The recorder uses a bounded queue. If the worker falls behind, new buffers are
dropped instead of allowing memory use to grow without limit. Dropped buffers
are reported when recording stops.

The buffer pool is a `queue.SimpleQueue`: the Core Audio callback takes an
available buffer with `get_nowait()`, and the worker returns buffers with
`put()`. This avoids a separate Python-level lock around pool mutations while
still using a thread-safe primitive in free-threaded builds. The work queue is
still bounded with `queue.Queue` so overload becomes dropped audio instead of
unbounded memory growth. Multi-step recorder state, such as frame counters and
callback failures, uses explicit locks.

## Known Tradeoffs

- If Core Audio delivers a buffer larger than the pool's current buffer size,
  the callback resizes that one pool buffer. This is expected to be rare after
  startup, but it is still an allocation on the callback thread.
- `on_data` receives a private `bytes` copy so user code can keep it after the
  pool buffer is reused.
- The default queue depth is 256 buffers. Bigger values tolerate slower sinks
  at the cost of more memory and more delayed failure reporting.
- Callback exceptions are never raised into Core Audio. The first failure is
  captured and reported on `AudioRecorder.stop()`.

## Profiling Status

The old profiling scripts were removed during the recorder refactor because
they depended on private implementation details that no longer exist. A future
profiling harness should be written against the current architecture instead
of preserving those old seams.

Useful measurements for the next harness:

- Worker throughput for WAV output and `on_data` callbacks.
- Time spent in float32-to-int16 conversion.
- Queue depth over time during long recordings.
- Buffer drops under intentionally slow sinks.
- Callback hot-path cost with and without pool-buffer resize.

For a synthetic profile that does not require audio-capture permission, run:

```bash
uv run python scripts/catap_profile_pipeline.py
```

To simulate a slow callback with Core Audio-like pacing:

```bash
uv run python scripts/catap_profile_pipeline.py --slow-callback-ms 2
```

The default converter and worker profiles are unpaced throughput tests. The
slow-callback profile is paced at the synthetic buffer interval. To also run
the old-style burst pressure test, add `--slow-burst`.

For a live probe that creates a real tap and measures callback timing through
the public recording API:

```bash
uv run python scripts/catap_live_probe.py --seconds 2
```

The live probe reports callback intervals, observed frames/bytes, stop-time
drop errors, and best-effort queue depth when the current private recorder
state exposes it. It requires the same macOS audio-capture permission as a
normal recording.
