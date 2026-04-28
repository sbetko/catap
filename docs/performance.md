# Performance and Real-Time Notes

The Core Audio callback runs on a real-time thread and has to return quickly.
It copies each incoming buffer into a pool-owned ctypes buffer and enqueues it
for the background worker. It does not call user code, write files, allocate
in the steady state, or wait for the worker. User callbacks and WAV writes
run on the `catap-audio-worker` thread.

## Queueing

The recorder uses a bounded queue. If the worker falls behind, new buffers
are dropped rather than letting memory grow without bound. The dropped count
is reported when recording stops.

The buffer pool is a `queue.SimpleQueue`: the callback takes a buffer with
`get_nowait()` and the worker returns it with `put()`. This avoids a separate
Python-level lock around pool mutation while still using a thread-safe
primitive on free-threaded builds. The work queue is a bounded `queue.Queue`,
so overload turns into dropped audio rather than runaway memory. Multi-step
recorder state — frame counters, callback failures — uses explicit locks.

## Tradeoffs

- If Core Audio delivers a buffer larger than the pool's current buffer size,
  the callback resizes that pool buffer in place. This should be rare after
  startup, but it is an allocation on the callback thread.
- `on_data` receives a private `bytes` copy, so user code can hold onto it
  after the pool buffer is reused.
- Default queue depth is 256 buffers. Larger values tolerate slower sinks at
  the cost of more memory and slower failure reporting.
- Callback exceptions are never raised into Core Audio. The first failure is
  captured and reported on `AudioRecorder.stop()`.

## Profiling

For real Core Audio runs, use Apple's `Audio System Trace` Instruments
template. The `Audio Client`, `Audio Statistics`, and `Audio Server` tracks
show IOProc timing, engine jitter, I/O cycle load, and overloads:

```bash
xcrun xctrace record \
  --template "Audio System Trace" \
  --all-processes \
  --time-limit 20s \
  --output /tmp/catap-audio.trace
```

Open the trace in Instruments. Check that callback work stays short, that
file and user-callback work runs on the `catap-audio-worker` thread, and
that the Audio Client/Server tracks show no overloads during normal
recording.

For a synthetic profile that does not require audio-capture permission:

```bash
uv run python scripts/catap_profile_pipeline.py
```

To simulate a slow callback with Core Audio-like pacing:

```bash
uv run python scripts/catap_profile_pipeline.py --slow-callback-ms 2
```

The default converter and worker profiles are unpaced throughput tests. The
slow-callback profile is paced at the synthetic buffer interval. Add
`--slow-burst` to also run the older burst pressure test.

For a live probe that creates a real tap and measures callback timing through
the public recording API:

```bash
uv run python scripts/catap_live_probe.py --seconds 2
```

The probe reports callback intervals, observed frames and bytes, drop errors
on stop, and (best-effort) queue depth when the private recorder state
exposes it. It needs the same macOS audio-capture permission as a normal
recording.
