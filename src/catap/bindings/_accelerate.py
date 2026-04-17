"""Accelerate (vDSP) bindings used for fast float32 → int16 conversion.

Accelerate is a macOS system framework that ships with every OS version we
support, so loading it unconditionally is safe.
"""

from __future__ import annotations

import ctypes

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


def float32_to_int16(data: bytes) -> bytes:
    """Convert 32-bit float audio samples to 16-bit signed integer samples.

    Dispatches the per-sample arithmetic to Apple's Accelerate (vDSP) SIMD
    routines: scale by 32767, clip symmetrically to [-32767, 32767], truncate
    toward zero. Samples outside [-1.0, 1.0] are clipped; intermediate math
    is float32 so results may differ from float64 truncation by at most one
    LSB on values that are not exactly representable.
    """
    num_samples = len(data) // 4
    if num_samples == 0:
        return b""

    scratch = (ctypes.c_float * num_samples).from_buffer_copy(data)
    _vDSP_vsmul(scratch, 1, _SCALE_REF, scratch, 1, num_samples)
    _vDSP_vclip(scratch, 1, _LOW_REF, _HIGH_REF, scratch, 1, num_samples)
    ints = (ctypes.c_int16 * num_samples)()
    _vDSP_vfix16(scratch, 1, ints, 1, num_samples)
    return bytes(ints)
