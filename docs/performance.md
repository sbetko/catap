# Performance Notes

`catap` records through a native Core Audio dylib. The Core Audio IOProc is a C
function, not a Python callback.

On the real-time thread, the native IOProc validates buffer count, channel
count, byte size, and group frame alignment. Single-track callbacks enter a
preallocated single-producer/single-consumer ring as one chunk. Multitrack
callback groups enter atomically. The IOProc records counters and returns. It
does not run Python, allocate per callback in the steady state, write files,
call user callbacks, or wait on a worker.

A Python drain thread reads the native ring and routes chunks to one worker per
track. WAV writing and callbacks run outside the Core Audio real-time path.

If the native ring fills, `catap` drops the incoming buffer or callback group
and fails the capture. `max_pending_buffers` controls native and worker queue
depth.

The native dylib is required for recording. If it is missing or has an
unsupported ABI version, recording fails at startup instead of falling back to
the old Python IOProc path.
