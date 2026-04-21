"""Bindings for Core Audio hardware tap functions."""

from __future__ import annotations

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


def create_process_tap(description: TapDescription) -> int:
    """Create a new audio tap and return its AudioObjectID."""
    tap_id = ctypes.c_uint32(0)
    status = _AudioHardwareCreateProcessTap(
        description.objc_object.__c_void_p__(),
        ctypes.byref(tap_id),
    )
    if status != 0:
        raise OSError(f"AudioHardwareCreateProcessTap failed with status {status}")
    return tap_id.value


def destroy_process_tap(tap_id: int) -> None:
    """Destroy an existing audio tap."""
    status = _AudioHardwareDestroyProcessTap(tap_id)
    if status != 0:
        raise OSError(f"AudioHardwareDestroyProcessTap failed with status {status}")
