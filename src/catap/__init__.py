"""Public API for catap."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from catap.bindings.hardware import create_process_tap, destroy_process_tap
    from catap.bindings.process import (
        AudioProcess,
        find_process_by_name,
        list_audio_processes,
    )
    from catap.bindings.tap_description import TapDescription, TapMuteBehavior
    from catap.core.recorder import AudioRecorder

_EXPORTS: dict[str, tuple[str, str]] = {
    "TapDescription": ("catap.bindings.tap_description", "TapDescription"),
    "TapMuteBehavior": ("catap.bindings.tap_description", "TapMuteBehavior"),
    "create_process_tap": ("catap.bindings.hardware", "create_process_tap"),
    "destroy_process_tap": ("catap.bindings.hardware", "destroy_process_tap"),
    "AudioProcess": ("catap.bindings.process", "AudioProcess"),
    "list_audio_processes": ("catap.bindings.process", "list_audio_processes"),
    "find_process_by_name": ("catap.bindings.process", "find_process_by_name"),
    "AudioRecorder": ("catap.core.recorder", "AudioRecorder"),
}


def __getattr__(name: str) -> Any:
    """Lazily import exported API symbols."""
    try:
        module_name, symbol_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), symbol_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | {"__version__"})
