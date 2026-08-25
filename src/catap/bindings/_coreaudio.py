"""Shared Core Audio / Core Foundation bindings for macOS-only code."""

from __future__ import annotations

import ctypes
from typing import Any

import objc

_CoreAudio = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
)
_CoreFoundation = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)

_CFRelease = _CoreFoundation.CFRelease
_CFRelease.argtypes = [ctypes.c_void_p]
_CFRelease.restype = None

kAudioObjectSystemObject = 1
kAudioObjectUnknown = 0
kAudioObjectPropertyScopeGlobal = int.from_bytes(b"glob", "big")
kAudioObjectPropertyScopeInput = int.from_bytes(b"inpt", "big")
kAudioObjectPropertyScopeOutput = int.from_bytes(b"outp", "big")
kAudioObjectPropertyElementMain = 0
kAudioHardwareUnknownPropertyError = int.from_bytes(b"who?", "big")
kAudioHardwareBadObjectError = int.from_bytes(b"!obj", "big")
kAudioDevicePermissionsError = int.from_bytes(b"!hog", "big")

_PropertyAddress = ctypes.c_uint32 * 3  # (selector, scope, element)

# OSStatus AudioObjectGetPropertyDataSize(
#     AudioObjectID, const AudioObjectPropertyAddress*,
#     UInt32, const void*, UInt32*)
_AudioObjectGetPropertyDataSize = _CoreAudio.AudioObjectGetPropertyDataSize
_AudioObjectGetPropertyDataSize.argtypes = [
    ctypes.c_uint32,
    ctypes.POINTER(_PropertyAddress),
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
]
_AudioObjectGetPropertyDataSize.restype = ctypes.c_int32

# OSStatus AudioObjectGetPropertyData(
#     AudioObjectID, const AudioObjectPropertyAddress*,
#     UInt32, const void*, UInt32*, void*)
_AudioObjectGetPropertyData = _CoreAudio.AudioObjectGetPropertyData
_AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32,
    ctypes.POINTER(_PropertyAddress),
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_void_p,
]
_AudioObjectGetPropertyData.restype = ctypes.c_int32

# OSStatus AudioObjectSetPropertyData(
#     AudioObjectID, const AudioObjectPropertyAddress*,
#     UInt32, const void*, UInt32, const void*)
_AudioObjectSetPropertyData = _CoreAudio.AudioObjectSetPropertyData
_AudioObjectSetPropertyData.argtypes = [
    ctypes.c_uint32,
    ctypes.POINTER(_PropertyAddress),
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_void_p,
]
_AudioObjectSetPropertyData.restype = ctypes.c_int32

# OSStatus (*AudioObjectPropertyListenerProc)(
#     AudioObjectID, UInt32, const AudioObjectPropertyAddress*, void*)
AudioObjectPropertyListenerProc = ctypes.CFUNCTYPE(
    ctypes.c_int32,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.POINTER(_PropertyAddress),
    ctypes.c_void_p,
)

_AudioObjectAddPropertyListener = _CoreAudio.AudioObjectAddPropertyListener
_AudioObjectAddPropertyListener.argtypes = [
    ctypes.c_uint32,
    ctypes.POINTER(_PropertyAddress),
    AudioObjectPropertyListenerProc,
    ctypes.c_void_p,
]
_AudioObjectAddPropertyListener.restype = ctypes.c_int32

_AudioObjectRemovePropertyListener = _CoreAudio.AudioObjectRemovePropertyListener
_AudioObjectRemovePropertyListener.argtypes = [
    ctypes.c_uint32,
    ctypes.POINTER(_PropertyAddress),
    AudioObjectPropertyListenerProc,
    ctypes.c_void_p,
]
_AudioObjectRemovePropertyListener.restype = ctypes.c_int32


def _property_address(
    selector: int,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> Any:
    return _PropertyAddress(selector, scope, element)


def _status_error(message: str, status: int) -> OSError:
    """Return an ``OSError`` annotated with the Core Audio status code."""
    error = OSError(message)
    error.status = status  # type: ignore[attr-defined]
    return error


def get_property_data_size(object_id: int, address: Any) -> int:
    """Return the byte size of a property, raising OSError on failure."""
    size = ctypes.c_uint32(0)
    status = _AudioObjectGetPropertyDataSize(
        object_id, ctypes.byref(address), 0, None, ctypes.byref(size)
    )
    if status != 0:
        raise _status_error(
            f"Failed to get property size for selector {address[0]:08x}: "
            f"status {status}",
            status,
        )
    return size.value


def get_property_bytes(
    object_id: int,
    selector: int,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> bytes:
    """Fetch a property as raw bytes."""
    address = _property_address(selector, scope, element)
    size = get_property_data_size(object_id, address)
    if size == 0:
        return b""

    buffer = ctypes.create_string_buffer(size)
    actual_size = ctypes.c_uint32(size)
    status = _AudioObjectGetPropertyData(
        object_id,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(actual_size),
        buffer,
    )
    if status != 0:
        raise _status_error(
            f"Failed to get property data for selector {selector:08x}: status {status}",
            status,
        )
    return buffer.raw[: actual_size.value]


def get_property_struct(
    object_id: int,
    selector: int,
    struct_type: type[ctypes.Structure],
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> ctypes.Structure:
    """Fetch a property directly into a ctypes Structure."""
    address = _property_address(selector, scope, element)
    value = struct_type()
    size = ctypes.c_uint32(ctypes.sizeof(struct_type))
    status = _AudioObjectGetPropertyData(
        object_id,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(value),
    )
    if status != 0:
        raise _status_error(
            f"Failed to get property struct for selector {selector:08x}: "
            f"status {status}",
            status,
        )
    return value


def get_property_cfstring(
    object_id: int,
    selector: int,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> str | None:
    """Fetch a CFStringRef property and convert it to a Python str.

    Returns None if the property is empty.
    """
    address = _property_address(selector, scope, element)
    if get_property_data_size(object_id, address) == 0:
        return None

    cf_string_ref = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(cf_string_ref))
    status = _AudioObjectGetPropertyData(
        object_id,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(cf_string_ref),
    )
    if status != 0:
        raise _status_error(
            f"Failed to get property data for selector {selector:08x}: status {status}",
            status,
        )

    if not cf_string_ref.value:
        return None

    try:
        return str(objc.objc_object(c_void_p=cf_string_ref.value))  # ty: ignore[unresolved-attribute]
    finally:
        _CFRelease(cf_string_ref)


def get_optional_property_cfstring(
    object_id: int,
    selector: int,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> str | None:
    """Fetch an optional CFString property, suppressing only lookup failures."""
    try:
        return get_property_cfstring(object_id, selector, scope, element)
    except OSError:
        return None


def get_property_object_ids(
    object_id: int,
    selector: int,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> list[int]:
    """Fetch a property containing packed AudioObjectIDs.

    The payload is decoded in complete 4-byte chunks only; any trailing partial
    bytes are ignored.
    """
    data = get_property_bytes(object_id, selector, scope, element)
    full_length = len(data) - (len(data) % 4)
    return [
        int.from_bytes(data[offset : offset + 4], "little")
        for offset in range(0, full_length, 4)
    ]


def get_property_object_id_with_qualifier(
    object_id: int,
    selector: int,
    qualifier: Any,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> int:
    """Translate a qualifier value into an AudioObjectID.

    ``qualifier`` is a ctypes value passed by reference, matching Core Audio's
    translation properties (for example a ``pid_t`` for
    ``kAudioHardwarePropertyTranslatePIDToProcessObject`` or a boxed
    ``CFStringRef`` for ``kAudioHardwarePropertyTranslateUIDToTap``). Returns
    ``kAudioObjectUnknown`` (0) when the qualifier matches nothing.
    """
    address = _property_address(selector, scope, element)
    value = ctypes.c_uint32(kAudioObjectUnknown)
    size = ctypes.c_uint32(ctypes.sizeof(value))
    status = _AudioObjectGetPropertyData(
        object_id,
        ctypes.byref(address),
        ctypes.sizeof(qualifier),
        ctypes.byref(qualifier),
        ctypes.byref(size),
        ctypes.byref(value),
    )
    if status != 0:
        raise _status_error(
            f"Failed to translate qualifier for selector {selector:08x}: "
            f"status {status}",
            status,
        )
    return value.value


def set_property_objc_object(
    object_id: int,
    selector: int,
    value: Any,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> None:
    """Set an Objective-C object property on a Core Audio object."""
    address = _property_address(selector, scope, element)
    objc_ref = ctypes.c_void_p(value.__c_void_p__().value)
    status = _AudioObjectSetPropertyData(
        object_id,
        ctypes.byref(address),
        0,
        None,
        ctypes.sizeof(objc_ref),
        ctypes.byref(objc_ref),
    )
    if status != 0:
        raise _status_error(
            f"Failed to set Objective-C property for selector {selector:08x}: "
            f"status {status}",
            status,
        )


def add_property_listener(
    object_id: int,
    selector: int,
    listener_proc: Any,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> None:
    """Register an ``AudioObjectPropertyListenerProc`` for one property.

    The caller owns keeping ``listener_proc`` (a ctypes
    ``AudioObjectPropertyListenerProc``) alive until the matching
    ``remove_property_listener`` call succeeds; Core Audio holds the raw
    function pointer.
    """
    address = _property_address(selector, scope, element)
    status = _AudioObjectAddPropertyListener(
        object_id, ctypes.byref(address), listener_proc, None
    )
    if status != 0:
        raise _status_error(
            f"Failed to add property listener for selector {selector:08x}: "
            f"status {status}",
            status,
        )


def remove_property_listener(
    object_id: int,
    selector: int,
    listener_proc: Any,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> None:
    """Deregister a listener previously added with ``add_property_listener``."""
    address = _property_address(selector, scope, element)
    status = _AudioObjectRemovePropertyListener(
        object_id, ctypes.byref(address), listener_proc, None
    )
    if status != 0:
        raise _status_error(
            f"Failed to remove property listener for selector {selector:08x}: "
            f"status {status}",
            status,
        )


def get_property_objc_object(
    object_id: int,
    selector: int,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> Any:
    """Fetch an Objective-C object property and wrap it for PyObjC."""
    address = _property_address(selector, scope, element)
    if get_property_data_size(object_id, address) == 0:
        raise OSError(f"Property {selector:08x} returned no object")

    objc_ref = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(objc_ref))
    status = _AudioObjectGetPropertyData(
        object_id,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(objc_ref),
    )
    if status != 0:
        raise _status_error(
            f"Failed to get Objective-C property for selector {selector:08x}: "
            f"status {status}",
            status,
        )
    if not objc_ref.value:
        raise OSError(f"Property {selector:08x} returned an empty object")

    try:
        # ``AudioObjectGetPropertyData`` hands the caller an owned reference.
        # PyObjC retains the object again when it builds the Python proxy, so
        # balance Core Audio's reference after the proxy has taken ownership.
        return objc.objc_object(c_void_p=objc_ref.value)  # ty: ignore[unresolved-attribute]
    finally:
        _CFRelease(objc_ref)
