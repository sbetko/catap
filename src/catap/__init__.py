"""Public API for catap."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from catap.bindings.hardware import create_process_tap, destroy_process_tap
from catap.bindings.process import (
    AudioProcess,
    find_process_by_name,
    list_audio_processes,
)
from catap.bindings.tap_description import TapDescription, TapMuteBehavior
from catap.core.recorder import AudioRecorder

try:
    __version__ = version("catap")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = [
    "TapDescription",
    "TapMuteBehavior",
    "create_process_tap",
    "destroy_process_tap",
    "AudioProcess",
    "list_audio_processes",
    "find_process_by_name",
    "AudioRecorder",
]
