"""catap - Python wrapper for Apple's Core Audio Tap API."""

from catap.bindings.tap_description import TapDescription, TapMuteBehavior
from catap.bindings.hardware import create_process_tap, destroy_process_tap
from catap.bindings.process import (
    AudioProcess,
    list_audio_processes,
    find_process_by_name,
)
from catap.core.recorder import AudioRecorder

__version__ = "0.1.0"

__all__ = [
    # Tap description and lifecycle
    "TapDescription",
    "TapMuteBehavior",
    "create_process_tap",
    "destroy_process_tap",
    # Process utilities
    "AudioProcess",
    "list_audio_processes",
    "find_process_by_name",
    # Recording
    "AudioRecorder",
]
