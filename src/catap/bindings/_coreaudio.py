"""Shared Core Audio framework loader for macOS-only bindings."""

from __future__ import annotations

import ctypes

from catap._runtime import get_runtime_support_error

if _runtime_error := get_runtime_support_error():
    raise ImportError(_runtime_error)

_CoreAudio = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
)
