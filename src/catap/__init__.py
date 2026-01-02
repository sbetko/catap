"""catap - Python wrapper for Apple's Core Audio Tap API."""

from catap.bindings.tap_description import TapDescription, TapMuteBehavior
from catap.bindings.hardware import create_process_tap, destroy_process_tap
from catap.bindings.process import AudioProcess, list_audio_processes, find_process_by_name
from catap.core.recorder import AudioRecorder
from catap.core.streamer import AudioStreamer, StreamFormat, write_wav_header, float32_to_int16

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
    # Streaming
    "AudioStreamer",
    "StreamFormat",
    "write_wav_header",
    "float32_to_int16",
]


def get_vu_meter():
    """
    Get the VUMeter class (requires 'rich' library).

    Returns:
        VUMeter class

    Raises:
        ImportError: If rich library is not installed
    """
    from catap.core.meter import VUMeter
    return VUMeter
