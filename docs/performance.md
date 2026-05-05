# Performance Notes

`catap` records through a native Core Audio dylib. The Core Audio IOProc is a C
function, not a Python callback.

On the real-time thread, the native IOProc validates the one-buffer interleaved
layout, copies the incoming audio bytes into a preallocated single-producer /
single-consumer ring, records simple counters, and returns. It does not run
Python code, allocate per callback in the steady state, write files, call user
callbacks, or wait on the background worker.

A Python drain thread reads the native ring and hands audio bytes to the
normal worker. WAV writing and `on_buffer` callbacks still run on
`catap-audio-worker`, outside the Core Audio real-time path.

If the native ring fills, `catap` drops incoming buffers and reports that on
stop instead of growing memory without bound. `max_pending_buffers` controls
the ring depth and worker queue depth.

The native dylib is required for recording. If it is missing or has an
unsupported ABI version, recording fails at startup instead of falling back to
the old Python IOProc path.
