"""Discover and inspect Core Audio tap objects."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from catap.bindings._coreaudio import (
    get_property_bytes as _get_audio_object_property,
    get_property_cfstring as _get_audio_object_cfstring_property,
    get_property_objc_object as _get_audio_object_objc_property,
    kAudioHardwareBadObjectError,
    kAudioObjectSystemObject,
)
from catap.bindings.tap_description import TapDescription

kAudioHardwarePropertyTapList = int.from_bytes(b"tps#", "big")
kAudioTapPropertyUID = int.from_bytes(b"tuid", "big")
kAudioTapPropertyDescription = int.from_bytes(b"tdsc", "big")


class AudioTapNotFoundError(OSError):
    """Raised when a tap ID no longer refers to a live Core Audio tap."""


def _coerce_missing_tap_error(tap_id: int, exc: OSError) -> OSError:
    """Return a friendlier exception for stale or destroyed taps."""
    if getattr(exc, "status", None) == kAudioHardwareBadObjectError:
        return AudioTapNotFoundError(
            f"Audio tap {tap_id} is no longer available. "
            "It may have been destroyed by another process."
        )
    return exc


def _raise_if_missing_tap(tap_id: int, exc: OSError) -> None:
    """Raise a tap-specific error for stale tap IDs."""
    if getattr(exc, "status", None) == kAudioHardwareBadObjectError:
        raise _coerce_missing_tap_error(tap_id, exc) from exc


def get_tap_description(tap_id: int) -> TapDescription:
    """Return the current description for an existing tap."""
    try:
        description = _get_audio_object_objc_property(
            tap_id, kAudioTapPropertyDescription
        )
    except OSError as exc:
        _raise_if_missing_tap(tap_id, exc)
        raise
    return TapDescription._from_objc_description(description)


@dataclass(frozen=True, slots=True)
class AudioTap:
    """Represents a visible Core Audio tap."""

    audio_object_id: int
    uid: str
    description: TapDescription

    @property
    def name(self) -> str:
        """Human-readable tap name."""
        return self.description.name

    @property
    def is_private(self) -> bool:
        """True when the tap is only visible to its creator."""
        return self.description.is_private

    @property
    def device_uid(self) -> str | None:
        """Optional hardware device UID targeted by the tap."""
        return self.description.device_uid

    @property
    def stream(self) -> int | None:
        """Optional hardware stream index targeted by the tap."""
        return self.description.stream


def list_audio_taps() -> list[AudioTap]:
    """List every tap currently visible to the calling process."""
    data = _get_audio_object_property(
        kAudioObjectSystemObject, kAudioHardwarePropertyTapList
    )
    if not data:
        return []

    count = len(data) // 4
    tap_ids = [struct.unpack("<I", data[i * 4 : (i + 1) * 4])[0] for i in range(count)]

    taps: list[AudioTap] = []
    for tap_id in tap_ids:
        try:
            uid = _get_audio_object_cfstring_property(tap_id, kAudioTapPropertyUID)
            if not uid:
                continue
            taps.append(
                AudioTap(
                    audio_object_id=tap_id,
                    uid=uid,
                    description=get_tap_description(tap_id),
                )
            )
        except (OSError, struct.error):
            continue

    return sorted(taps, key=lambda tap: (tap.name.casefold(), tap.uid))


def find_tap_by_uid(uid: str) -> AudioTap | None:
    """Find a visible tap by its persistent UID string."""
    if not uid:
        return None

    for tap in list_audio_taps():
        if tap.uid == uid:
            return tap
    return None


__all__ = [
    "AudioTap",
    "AudioTapNotFoundError",
    "find_tap_by_uid",
    "get_tap_description",
    "list_audio_taps",
]
