"""Enumerate audio-producing processes."""

from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass

from AppKit import NSRunningApplication, NSWorkspace  # ty: ignore[unresolved-import]

from catap.bindings._coreaudio import _CoreAudio

_CoreFoundation = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_CFRelease = _CoreFoundation.CFRelease
_CFRelease.argtypes = [ctypes.c_void_p]
_CFRelease.restype = None

# Define C function signatures
# OSStatus AudioObjectGetPropertyDataSize(
#     AudioObjectID inObjectID,
#     const AudioObjectPropertyAddress* inAddress,
#     UInt32 inQualifierDataSize,
#     const void* inQualifierData,
#     UInt32* outDataSize
# )
_AudioObjectGetPropertyDataSize = _CoreAudio.AudioObjectGetPropertyDataSize
_AudioObjectGetPropertyDataSize.argtypes = [
    ctypes.c_uint32,  # inObjectID
    ctypes.POINTER(ctypes.c_uint32 * 3),  # inAddress (selector, scope, element)
    ctypes.c_uint32,  # inQualifierDataSize
    ctypes.c_void_p,  # inQualifierData
    ctypes.POINTER(ctypes.c_uint32),  # outDataSize
]
_AudioObjectGetPropertyDataSize.restype = ctypes.c_int32

# OSStatus AudioObjectGetPropertyData(
#     AudioObjectID inObjectID,
#     const AudioObjectPropertyAddress* inAddress,
#     UInt32 inQualifierDataSize,
#     const void* inQualifierData,
#     UInt32* ioDataSize,
#     void* outData
# )
_AudioObjectGetPropertyData = _CoreAudio.AudioObjectGetPropertyData
_AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32,  # inObjectID
    ctypes.POINTER(ctypes.c_uint32 * 3),  # inAddress
    ctypes.c_uint32,  # inQualifierDataSize
    ctypes.c_void_p,  # inQualifierData
    ctypes.POINTER(ctypes.c_uint32),  # ioDataSize
    ctypes.c_void_p,  # outData
]
_AudioObjectGetPropertyData.restype = ctypes.c_int32

# Constants from AudioHardware.h
kAudioObjectSystemObject = 1
kAudioObjectPropertyScopeGlobal = int.from_bytes(b"glob", "big")
kAudioObjectPropertyElementMain = 0

# Property selectors
kAudioHardwarePropertyProcessObjectList = int.from_bytes(b"prs#", "big")
kAudioProcessPropertyPID = int.from_bytes(b"ppid", "big")
kAudioProcessPropertyBundleID = int.from_bytes(b"pbid", "big")
kAudioProcessPropertyIsRunningOutput = int.from_bytes(b"piro", "big")


@dataclass
class AudioProcess:
    """Represents a process that is using audio."""

    audio_object_id: int
    pid: int
    bundle_id: str | None
    name: str
    is_outputting: bool


def _get_audio_object_property(
    object_id: int,
    selector: int,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> bytes:
    """
    Get a property from an audio object.

    Args:
        object_id: The AudioObjectID to query
        selector: Property selector (four-char code as int)
        scope: Property scope (default: global)
        element: Property element (default: main)

    Returns:
        Property data as bytes

    Raises:
        OSError: If property cannot be retrieved
    """
    # Create property address (selector, scope, element)
    address = (ctypes.c_uint32 * 3)(selector, scope, element)

    # Get property size
    data_size = ctypes.c_uint32(0)
    status = _AudioObjectGetPropertyDataSize(
        object_id,
        ctypes.byref(address),
        0,  # inQualifierDataSize
        None,  # inQualifierData
        ctypes.byref(data_size),
    )

    if status != 0:
        raise OSError(
            f"Failed to get property size for selector {selector:08x}: status {status}"
        )

    if data_size.value == 0:
        return b""

    # Allocate buffer for property data
    buffer = ctypes.create_string_buffer(data_size.value)
    actual_size = ctypes.c_uint32(data_size.value)

    # Get property data
    status = _AudioObjectGetPropertyData(
        object_id,
        ctypes.byref(address),
        0,  # inQualifierDataSize
        None,  # inQualifierData
        ctypes.byref(actual_size),
        buffer,
    )

    if status != 0:
        raise OSError(
            f"Failed to get property data for selector {selector:08x}: status {status}"
        )

    return buffer.raw[: actual_size.value]


def _get_audio_object_cfstring_property(
    object_id: int,
    selector: int,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> str | None:
    """
    Get a retained CFString property from an audio object.

    Returns None when the property is empty.
    """
    address = (ctypes.c_uint32 * 3)(selector, scope, element)
    data_size = ctypes.c_uint32(0)
    status = _AudioObjectGetPropertyDataSize(
        object_id,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(data_size),
    )

    if status != 0:
        raise OSError(
            f"Failed to get property size for selector {selector:08x}: status {status}"
        )

    if data_size.value == 0:
        return None

    cf_string_ref = ctypes.c_void_p()
    actual_size = ctypes.c_uint32(ctypes.sizeof(cf_string_ref))
    status = _AudioObjectGetPropertyData(
        object_id,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(actual_size),
        ctypes.byref(cf_string_ref),
    )

    if status != 0:
        raise OSError(
            f"Failed to get property data for selector {selector:08x}: status {status}"
        )

    if not cf_string_ref.value:
        return None

    import objc

    try:
        return str(objc.objc_object(c_void_p=cf_string_ref.value))  # ty: ignore[unresolved-attribute]
    finally:
        _CFRelease(cf_string_ref)


def list_audio_processes() -> list[AudioProcess]:
    """
    List all processes currently registered with Core Audio.

    Returns:
        List of AudioProcess objects for all audio processes
    """
    processes = []

    # Get list of process object IDs
    try:
        data = _get_audio_object_property(
            kAudioObjectSystemObject, kAudioHardwarePropertyProcessObjectList
        )
    except OSError:
        # Property might not exist if no processes are registered
        return []

    if not data:
        return []

    # Parse as array of UInt32 (AudioObjectID)
    # Each AudioObjectID is 4 bytes
    count = len(data) // 4
    process_ids = [
        struct.unpack("<I", data[i * 4 : (i + 1) * 4])[0] for i in range(count)
    ]

    # Get running apps for name lookup
    workspace = NSWorkspace.sharedWorkspace()
    running_apps = {
        str(app.bundleIdentifier()): app
        for app in workspace.runningApplications()
        if app.bundleIdentifier()
    }

    for audio_id in process_ids:
        try:
            # Get PID
            pid_data = _get_audio_object_property(audio_id, kAudioProcessPropertyPID)
            pid = struct.unpack("<I", pid_data[:4])[0]

            # Get bundle ID (CFString)
            bundle_id = None
            try:
                bundle_id = _get_audio_object_cfstring_property(
                    audio_id, kAudioProcessPropertyBundleID
                )
            except OSError:
                pass

            # Check if outputting audio
            is_outputting = False
            try:
                output_data = _get_audio_object_property(
                    audio_id, kAudioProcessPropertyIsRunningOutput
                )
                if output_data:
                    is_outputting = struct.unpack("<I", output_data[:4])[0] != 0
            except OSError:
                pass

            # Get app name from NSRunningApplication
            name = "Unknown"
            if bundle_id and bundle_id in running_apps:
                app = running_apps[bundle_id]
                name = str(app.localizedName()) or name
            else:
                # Try to get by PID
                app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if app:
                    name = str(app.localizedName()) or name
                    if not bundle_id and app.bundleIdentifier():
                        bundle_id = str(app.bundleIdentifier())

            processes.append(
                AudioProcess(
                    audio_object_id=audio_id,
                    pid=pid,
                    bundle_id=bundle_id,
                    name=name,
                    is_outputting=is_outputting,
                )
            )

        except (OSError, struct.error):
            # Skip processes we can't read
            continue

    return processes


def find_process_by_name(name: str) -> AudioProcess | None:
    """
    Find an audio process by app name (case-insensitive partial match).

    Args:
        name: Application name to search for

    Returns:
        AudioProcess if found, None otherwise
    """
    name_lower = name.lower()
    for process in list_audio_processes():
        if name_lower in process.name.lower():
            return process
    return None
