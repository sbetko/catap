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

from catap._multitrack import MultitrackAudioRecorder
from catap.audio_buffer import (
    AudioBuffer,
    AudioStreamFormat,
)
from catap.bindings.device import (
    AmbiguousAudioDeviceError,
    AudioDevice,
    AudioDeviceStream,
    find_audio_device_by_name,
    find_audio_device_by_uid,
    list_audio_devices,
)
from catap.bindings.hardware import create_process_tap, destroy_process_tap
from catap.bindings.process import (
    AmbiguousAudioProcessError,
    AudioProcess,
    find_process_by_name,
    find_process_by_pid,
    list_audio_processes,
)
from catap.bindings.tap import (
    AudioTap,
    AudioTapNotFoundError,
    TapStreamFormat,
    find_tap_by_uid,
    get_tap_description,
    get_tap_format,
    list_audio_taps,
    set_tap_description,
)
from catap.bindings.tap_description import (
    TapDescription,
    TapMuteBehavior,
    bundle_id_taps_supported,
)
from catap.drift import DriftCompensationQuality
from catap.recorder import AudioRecorder, UnsupportedTapFormatError
from catap.session import (
    AudioDeviceNotFoundError,
    AudioProcessNotFoundError,
    MultitrackRecordingSession,
    RecordingSession,
    record_bundle_ids,
    record_device,
    record_multitrack,
    record_process,
    record_processes,
    record_system_audio,
    record_tap,
)

try:
    __version__ = version("catap")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = [
    "AmbiguousAudioDeviceError",
    "AmbiguousAudioProcessError",
    "AudioBuffer",
    "AudioDevice",
    "AudioDeviceNotFoundError",
    "AudioDeviceStream",
    "AudioProcess",
    "AudioProcessNotFoundError",
    "AudioRecorder",
    "AudioStreamFormat",
    "AudioTap",
    "AudioTapNotFoundError",
    "DriftCompensationQuality",
    "MultitrackAudioRecorder",
    "MultitrackRecordingSession",
    "RecordingSession",
    "TapDescription",
    "TapMuteBehavior",
    "TapStreamFormat",
    "UnsupportedTapFormatError",
    "bundle_id_taps_supported",
    "create_process_tap",
    "destroy_process_tap",
    "find_audio_device_by_name",
    "find_audio_device_by_uid",
    "find_process_by_name",
    "find_process_by_pid",
    "find_tap_by_uid",
    "get_tap_description",
    "get_tap_format",
    "list_audio_devices",
    "list_audio_processes",
    "list_audio_taps",
    "record_bundle_ids",
    "record_device",
    "record_multitrack",
    "record_process",
    "record_processes",
    "record_system_audio",
    "record_tap",
    "set_tap_description",
]
