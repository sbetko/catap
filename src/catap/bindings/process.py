"""Enumerate audio-producing processes."""

from __future__ import annotations

import ctypes
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from AppKit import NSRunningApplication, NSWorkspace  # ty: ignore[unresolved-import]

from catap.bindings._coreaudio import (
    get_optional_property_cfstring as _get_optional_audio_object_cfstring_property,
    get_property_bytes as _get_audio_object_property,
    get_property_object_id_with_qualifier as _translate_qualifier,
    get_property_object_ids as _get_audio_object_ids,
    kAudioObjectSystemObject,
    kAudioObjectUnknown,
)

# Property selectors
kAudioHardwarePropertyProcessObjectList = int.from_bytes(b"prs#", "big")
kAudioHardwarePropertyTranslatePIDToProcessObject = int.from_bytes(b"id2p", "big")
kAudioProcessPropertyPID = int.from_bytes(b"ppid", "big")
kAudioProcessPropertyBundleID = int.from_bytes(b"pbid", "big")
kAudioProcessPropertyIsRunning = int.from_bytes(b"pir?", "big")
kAudioProcessPropertyIsRunningInput = int.from_bytes(b"piri", "big")
kAudioProcessPropertyIsRunningOutput = int.from_bytes(b"piro", "big")

_PID_T_MAX = (1 << 31) - 1


@dataclass
class AudioProcess:
    """Represents a process that is using audio."""

    audio_object_id: int
    pid: int
    bundle_id: str | None
    name: str
    is_outputting: bool
    is_running: bool = False
    is_inputting: bool = False


class AmbiguousAudioProcessError(LookupError):
    """Raised when a process query matches more than one audio process."""

    def __init__(self, query: str, matches: Iterable[AudioProcess]) -> None:
        self.query = query
        self.matches = tuple(matches)

        formatted_matches = ", ".join(
            (
                f"{process.name} "
                f"(PID: {process.pid}, Bundle ID: {process.bundle_id or 'N/A'})"
            )
            for process in self.matches[:5]
        )
        if len(self.matches) > 5:
            formatted_matches = (
                f"{formatted_matches}, and {len(self.matches) - 5} more"
            )

        super().__init__(
            f"Multiple audio processes match '{query}': {formatted_matches}"
        )


def _get_process_bool_property(audio_id: int, selector: int) -> bool:
    """Read an optional UInt32 boolean process property, defaulting to False."""
    try:
        data = _get_audio_object_property(audio_id, selector)
    except OSError:
        return False
    if len(data) < 4:
        return False
    return struct.unpack("<I", data[:4])[0] != 0


def _running_applications_by_bundle_id() -> dict[str, Any]:
    """Map bundle IDs to the workspace's running applications."""
    workspace = NSWorkspace.sharedWorkspace()
    return {
        str(app.bundleIdentifier()): app
        for app in workspace.runningApplications()
        if app.bundleIdentifier()
    }


def _read_audio_process(
    audio_id: int,
    running_apps: dict[str, Any],
) -> AudioProcess:
    """Build an ``AudioProcess`` from one Core Audio process object."""
    pid_data = _get_audio_object_property(audio_id, kAudioProcessPropertyPID)
    pid = struct.unpack("<I", pid_data[:4])[0]

    bundle_id = _get_optional_audio_object_cfstring_property(
        audio_id, kAudioProcessPropertyBundleID
    )

    is_outputting = _get_process_bool_property(
        audio_id, kAudioProcessPropertyIsRunningOutput
    )
    is_running = _get_process_bool_property(audio_id, kAudioProcessPropertyIsRunning)
    is_inputting = _get_process_bool_property(
        audio_id, kAudioProcessPropertyIsRunningInput
    )

    name = "Unknown"
    if bundle_id and bundle_id in running_apps:
        app = running_apps[bundle_id]
        name = str(app.localizedName()) or name
    else:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app:
            name = str(app.localizedName()) or name
            if not bundle_id and app.bundleIdentifier():
                bundle_id = str(app.bundleIdentifier())

    return AudioProcess(
        audio_object_id=audio_id,
        pid=pid,
        bundle_id=bundle_id,
        name=name,
        is_outputting=is_outputting,
        is_running=is_running,
        is_inputting=is_inputting,
    )


def list_audio_processes() -> list[AudioProcess]:
    """List all processes currently registered with Core Audio."""
    process_ids = _get_audio_object_ids(
        kAudioObjectSystemObject, kAudioHardwarePropertyProcessObjectList
    )
    if not process_ids:
        return []

    running_apps = _running_applications_by_bundle_id()

    processes = []
    for audio_id in process_ids:
        try:
            processes.append(_read_audio_process(audio_id, running_apps))
        except (OSError, struct.error):
            continue

    return sorted(processes, key=lambda process: (process.name.casefold(), process.pid))


def find_process_by_pid(pid: int) -> AudioProcess | None:
    """Find the audio process object for an OS process ID.

    Uses Core Audio's PID translation property, so it does not need to
    enumerate every process object. Returns None when the PID has no audio
    process object.
    """
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise TypeError("pid must be an int")
    if not 1 <= pid <= _PID_T_MAX:
        raise ValueError(f"pid must be between 1 and {_PID_T_MAX}, got {pid}")

    audio_id = _translate_qualifier(
        kAudioObjectSystemObject,
        kAudioHardwarePropertyTranslatePIDToProcessObject,
        ctypes.c_int32(pid),
    )
    if audio_id == kAudioObjectUnknown:
        return None

    try:
        return _read_audio_process(audio_id, _running_applications_by_bundle_id())
    except (OSError, struct.error):
        return None


def find_process_by_name(name: str) -> AudioProcess | None:
    """Find an audio process by exact or uniquely partial name match.

    Exact application-name matches win over bundle ID matches, which win over
    partial name matches. Raises AmbiguousAudioProcessError when the query
    matches more than one process at the same precedence level. Returns None for
    an empty query; otherwise every process would match.
    """
    if not name:
        return None

    query = name.casefold()
    processes = list_audio_processes()

    exact_name_matches = [
        process for process in processes if process.name.casefold() == query
    ]
    if exact_name_matches:
        if len(exact_name_matches) > 1:
            raise AmbiguousAudioProcessError(name, exact_name_matches)
        return exact_name_matches[0]

    exact_bundle_matches = [
        process
        for process in processes
        if process.bundle_id and process.bundle_id.casefold() == query
    ]
    if exact_bundle_matches:
        if len(exact_bundle_matches) > 1:
            raise AmbiguousAudioProcessError(name, exact_bundle_matches)
        return exact_bundle_matches[0]

    partial_name_matches = [
        process for process in processes if query in process.name.casefold()
    ]
    if len(partial_name_matches) > 1:
        raise AmbiguousAudioProcessError(name, partial_name_matches)
    if partial_name_matches:
        return partial_name_matches[0]
    return None
