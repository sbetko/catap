"""Centralized import surface for macOS-only backend functionality."""

from __future__ import annotations

# Import the shared loader first so unsupported runtimes fail with one
# package-level error before we touch any PyObjC modules.
from catap.bindings._coreaudio import _CoreAudio  # noqa: F401
from catap.bindings.hardware import create_process_tap, destroy_process_tap
from catap.bindings.process import (
    AudioProcess,
    find_process_by_name,
    list_audio_processes,
)
from catap.bindings.tap_description import TapDescription, TapMuteBehavior
from catap.core.recorder import AudioRecorder

__all__ = [
    "AudioProcess",
    "AudioRecorder",
    "TapDescription",
    "TapMuteBehavior",
    "create_process_tap",
    "destroy_process_tap",
    "find_process_by_name",
    "list_audio_processes",
]
