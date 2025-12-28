"""Bindings for Core Audio hardware tap functions."""
from __future__ import annotations

import ctypes
from typing import TYPE_CHECKING

import objc

if TYPE_CHECKING:
    from catap.bindings.tap_description import TapDescription

# Load CoreAudio framework
_CoreAudio = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
)

# Define C function signatures
# OSStatus AudioHardwareCreateProcessTap(
#     CATapDescription* inDescription,
#     AudioObjectID* outTapID
# )
_AudioHardwareCreateProcessTap = _CoreAudio.AudioHardwareCreateProcessTap
_AudioHardwareCreateProcessTap.argtypes = [
    ctypes.c_void_p,  # CATapDescription* (PyObjC object pointer)
    ctypes.POINTER(ctypes.c_uint32),  # AudioObjectID*
]
_AudioHardwareCreateProcessTap.restype = ctypes.c_int32  # OSStatus

# OSStatus AudioHardwareDestroyProcessTap(AudioObjectID inTapID)
_AudioHardwareDestroyProcessTap = _CoreAudio.AudioHardwareDestroyProcessTap
_AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]  # AudioObjectID
_AudioHardwareDestroyProcessTap.restype = ctypes.c_int32  # OSStatus


def create_process_tap(description: TapDescription) -> int:
    """
    Create a new audio tap from a description.

    Args:
        description: TapDescription specifying the tap configuration

    Returns:
        AudioObjectID of the created tap

    Raises:
        OSError: If tap creation fails
    """
    tap_id = ctypes.c_uint32(0)

    # Get the ObjC object pointer from the TapDescription
    objc_obj = description.objc_object
    # Get the pointer using __c_void_p__ property
    # PyObjC objects have a __c_void_p__ property that returns a ctypes.c_void_p
    objc_ptr = objc_obj.__c_void_p__()

    # Create the tap
    status = _AudioHardwareCreateProcessTap(objc_ptr, ctypes.byref(tap_id))

    if status != 0:
        raise OSError(f"AudioHardwareCreateProcessTap failed with status {status}")

    return tap_id.value


def destroy_process_tap(tap_id: int) -> None:
    """
    Destroy an existing audio tap.

    Args:
        tap_id: AudioObjectID of the tap to destroy

    Raises:
        OSError: If tap destruction fails
    """
    status = _AudioHardwareDestroyProcessTap(tap_id)

    if status != 0:
        raise OSError(f"AudioHardwareDestroyProcessTap failed with status {status}")
