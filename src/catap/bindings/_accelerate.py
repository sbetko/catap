"""Accelerate (vDSP) bindings used for fast float32 → int16 conversion.

Accelerate is a macOS system framework that ships with every OS version we
support, so loading it unconditionally is safe.
"""

from __future__ import annotations

import ctypes
from typing import Any

_Accelerate = ctypes.CDLL(
    "/System/Library/Frameworks/Accelerate.framework/Accelerate"
)

_c_float_p = ctypes.POINTER(ctypes.c_float)
_c_int16_p = ctypes.POINTER(ctypes.c_int16)

# void vDSP_vsmul(const float *A, vDSP_Stride I, const float *B,
#                 float *C, vDSP_Stride K, vDSP_Length N);
_vDSP_vsmul = _Accelerate.vDSP_vsmul
_vDSP_vsmul.argtypes = [
    _c_float_p,
    ctypes.c_long,
    _c_float_p,
    _c_float_p,
    ctypes.c_long,
    ctypes.c_ulong,
]
_vDSP_vsmul.restype = None

# void vDSP_vclip(const float *A, vDSP_Stride I, const float *B, const float *C,
#                 float *D, vDSP_Stride K, vDSP_Length N);
_vDSP_vclip = _Accelerate.vDSP_vclip
_vDSP_vclip.argtypes = [
    _c_float_p,
    ctypes.c_long,
    _c_float_p,
    _c_float_p,
    _c_float_p,
    ctypes.c_long,
    ctypes.c_ulong,
]
_vDSP_vclip.restype = None

# void vDSP_vfix16(const float *A, vDSP_Stride I, short *B,
#                  vDSP_Stride K, vDSP_Length N);
# Truncates toward zero, matching Python's int() semantics.
_vDSP_vfix16 = _Accelerate.vDSP_vfix16
_vDSP_vfix16.argtypes = [
    _c_float_p,
    ctypes.c_long,
    _c_int16_p,
    ctypes.c_long,
    ctypes.c_ulong,
]
_vDSP_vfix16.restype = None

_SCALE = ctypes.c_float(32767.0)
_LOW = ctypes.c_float(-32767.0)
_HIGH = ctypes.c_float(32767.0)
_SCALE_REF = ctypes.byref(_SCALE)
_LOW_REF = ctypes.byref(_LOW)
_HIGH_REF = ctypes.byref(_HIGH)

# Grow-only float32 scratch reused across calls. Audio callback buffers are
# small and bounded in practice, so peak scratch stays near the largest buffer
# Core Audio has ever delivered. Not thread-safe: the recorder funnels all
# conversions through a single worker thread.
_scratch_floats: ctypes.Array[ctypes.c_float] | None = None
_scratch_capacity: int = 0

# int16 output buffers cached per sample count. Core Audio buffer sizes are
# stable within a session, so in practice this dict holds one or two entries.
# Exact-size buffers let the return use ``bytes(arr)`` (~70 ns) rather than
# ``ctypes.string_at(arr, n)`` (~188 ns).
_int16_by_size: dict[int, ctypes.Array[ctypes.c_int16]] = {}


def float32_to_int16(data: Any, size: int | None = None) -> bytes:
    """Convert 32-bit float audio samples to 16-bit signed integer samples.

    Dispatches the per-sample arithmetic to Apple's Accelerate (vDSP) SIMD
    routines: scale by 32767, clip symmetrically to [-32767, 32767], truncate
    toward zero. Samples outside [-1.0, 1.0] are clipped; intermediate math
    is float32 so results may differ from float64 truncation by at most one
    LSB on values that are not exactly representable.

    ``data`` must be something ``ctypes.memmove`` accepts as its source: a
    ``bytes`` object or a ctypes array. (``bytearray`` and ``memoryview`` are
    rejected by ctypes.) ``size`` lets callers copy fewer than ``len(data)``
    bytes when the source is a larger pool buffer.
    """
    global _scratch_floats, _scratch_capacity

    byte_count = len(data) if size is None else size
    num_samples = byte_count // 4
    if num_samples == 0:
        return b""

    if num_samples > _scratch_capacity:
        _scratch_floats = (ctypes.c_float * num_samples)()
        _scratch_capacity = num_samples

    ints = _int16_by_size.get(num_samples)
    if ints is None:
        ints = (ctypes.c_int16 * num_samples)()
        _int16_by_size[num_samples] = ints

    scratch = _scratch_floats
    assert scratch is not None  # guaranteed by the grow block above
    ctypes.memmove(scratch, data, num_samples * 4)
    _vDSP_vsmul(scratch, 1, _SCALE_REF, scratch, 1, num_samples)
    _vDSP_vclip(scratch, 1, _LOW_REF, _HIGH_REF, scratch, 1, num_samples)
    _vDSP_vfix16(scratch, 1, ints, 1, num_samples)
    return bytes(ints)
