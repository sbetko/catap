# ruff: noqa: E402
"""Public API for catap."""

from __future__ import annotations

import platform
from importlib.metadata import PackageNotFoundError, version

if platform.system() != "Darwin":
    raise ImportError("catap only supports macOS 14.2 or later.")

_macos_version = platform.mac_ver()[0]
if not _macos_version:
    raise ImportError("catap only supports macOS 14.2 or later.")

try:
    _macos_version_tuple = tuple(int(part) for part in _macos_version.split("."))
except ValueError as exc:
    raise ImportError("catap only supports macOS 14.2 or later.") from exc

if _macos_version_tuple < (14, 2):
    raise ImportError(
        f"catap requires macOS 14.2 or later. Detected macOS {_macos_version}."
    )

from catap.bindings.hardware import create_process_tap, destroy_process_tap
from catap.bindings.process import (
    AmbiguousAudioProcessError,
    AudioProcess,
    find_process_by_name,
    list_audio_processes,
)
from catap.bindings.tap import AudioTap, find_tap_by_uid, list_audio_taps
from catap.bindings.tap_description import TapDescription, TapMuteBehavior
from catap.recorder import AudioRecorder
from catap.session import (
    AudioProcessNotFoundError,
    RecordingSession,
    record_process,
    record_system_audio,
    record_tap,
)

try:
    __version__ = version("catap")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = [
    "AmbiguousAudioProcessError",
    "AudioProcess",
    "AudioProcessNotFoundError",
    "AudioRecorder",
    "AudioTap",
    "RecordingSession",
    "TapDescription",
    "TapMuteBehavior",
    "create_process_tap",
    "destroy_process_tap",
    "find_process_by_name",
    "find_tap_by_uid",
    "list_audio_processes",
    "list_audio_taps",
    "record_process",
    "record_system_audio",
    "record_tap",
]
