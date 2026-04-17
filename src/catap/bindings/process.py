"""Enumerate audio-producing processes."""

from __future__ import annotations

import contextlib
import struct
from collections.abc import Iterable
from dataclasses import dataclass

from AppKit import NSRunningApplication, NSWorkspace  # ty: ignore[unresolved-import]

from catap.bindings._coreaudio import (
    get_property_bytes as _get_audio_object_property,
    get_property_cfstring as _get_audio_object_cfstring_property,
    kAudioObjectSystemObject,
)

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


def list_audio_processes() -> list[AudioProcess]:
    """List all processes currently registered with Core Audio."""
    data = _get_audio_object_property(
        kAudioObjectSystemObject, kAudioHardwarePropertyProcessObjectList
    )

    if not data:
        return []

    # Parse array of AudioObjectID (UInt32)
    count = len(data) // 4
    process_ids = [
        struct.unpack("<I", data[i * 4 : (i + 1) * 4])[0] for i in range(count)
    ]

    workspace = NSWorkspace.sharedWorkspace()
    running_apps = {
        str(app.bundleIdentifier()): app
        for app in workspace.runningApplications()
        if app.bundleIdentifier()
    }

    processes = []
    for audio_id in process_ids:
        try:
            pid_data = _get_audio_object_property(audio_id, kAudioProcessPropertyPID)
            pid = struct.unpack("<I", pid_data[:4])[0]

            bundle_id: str | None = None
            with contextlib.suppress(OSError):
                bundle_id = _get_audio_object_cfstring_property(
                    audio_id, kAudioProcessPropertyBundleID
                )

            is_outputting = False
            with contextlib.suppress(OSError):
                output_data = _get_audio_object_property(
                    audio_id, kAudioProcessPropertyIsRunningOutput
                )
                if output_data:
                    is_outputting = struct.unpack("<I", output_data[:4])[0] != 0

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
            continue

    return sorted(processes, key=lambda process: (process.name.casefold(), process.pid))


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
