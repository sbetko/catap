"""Bindings for Core Audio hardware tap functions."""

from __future__ import annotations

import contextlib
import ctypes
from typing import TYPE_CHECKING

from catap.bindings._coreaudio import _CoreAudio

if TYPE_CHECKING:
    from catap.bindings.tap_description import TapDescription

_AudioHardwareCreateProcessTap = _CoreAudio.AudioHardwareCreateProcessTap
_AudioHardwareCreateProcessTap.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
]
_AudioHardwareCreateProcessTap.restype = ctypes.c_int32

_AudioHardwareDestroyProcessTap = _CoreAudio.AudioHardwareDestroyProcessTap
_AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
_AudioHardwareDestroyProcessTap.restype = ctypes.c_int32


def create_process_tap(
    description: TapDescription,
    *,
    out: ctypes.c_uint32 | None = None,
) -> int:
    """Create a new audio tap and return its AudioObjectID.

    Core Audio writes the new tap's ID into ``out`` (or an internal buffer when
    ``out`` is omitted) before this function returns. Passing a caller-owned
    ``out`` closes the interruption window between tap creation and the caller
    storing the returned ID: if an exception such as ``KeyboardInterrupt``
    unwinds this call after the tap exists, the caller can recover the ID from
    ``out`` and destroy the tap. Without ``out``, an unwind at that point
    destroys the tap here on a best-effort basis instead.
    """
    tap_id = ctypes.c_uint32(0) if out is None else out
    tap_id.value = 0
    try:
        status = _AudioHardwareCreateProcessTap(
            description.objc_object.__c_void_p__(),
            ctypes.byref(tap_id),
        )
        if status != 0:
            tap_id.value = 0
            raise OSError(f"AudioHardwareCreateProcessTap failed with status {status}")
        return tap_id.value
    except OSError:
        raise
    except BaseException:
        if out is None and tap_id.value:
            with contextlib.suppress(OSError):
                destroy_process_tap(tap_id.value)
        raise


def destroy_process_tap(tap_id: int) -> None:
    """Destroy an existing audio tap."""
    status = _AudioHardwareDestroyProcessTap(tap_id)
    if status != 0:
        raise OSError(f"AudioHardwareDestroyProcessTap failed with status {status}")
