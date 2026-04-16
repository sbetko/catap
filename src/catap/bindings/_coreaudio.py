"""Shared Core Audio framework loader for macOS-only bindings."""

from __future__ import annotations

import ctypes

_CoreAudio = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
)
