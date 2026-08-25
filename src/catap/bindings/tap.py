"""Discover, inspect, and modify Core Audio tap objects."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

from Foundation import NSString  # ty: ignore[unresolved-import]

from catap.audio_buffer import _format_id_to_fourcc
from catap.bindings._audiotoolbox import (
    AudioStreamBasicDescription,
    kAudioFormatFlagIsBigEndian,
    kAudioFormatFlagIsFloat,
    kAudioFormatFlagIsNonInterleaved,
    kAudioFormatFlagIsPacked,
    kAudioFormatFlagIsSignedInteger,
)
from catap.bindings._coreaudio import (
    get_property_cfstring as _get_audio_object_cfstring_property,
    get_property_objc_object as _get_audio_object_objc_property,
    get_property_object_id_with_qualifier as _translate_qualifier,
    get_property_object_ids as _get_audio_object_ids,
    get_property_struct as _get_audio_object_struct_property,
    kAudioDevicePermissionsError,
    kAudioHardwareBadObjectError,
    kAudioHardwareUnknownPropertyError,
    kAudioObjectSystemObject,
    kAudioObjectUnknown,
    set_property_objc_object as _set_audio_object_objc_property,
)
from catap.bindings.tap_description import TapDescription

kAudioHardwarePropertyTapList = int.from_bytes(b"tps#", "big")
kAudioHardwarePropertyTranslateUIDToTap = int.from_bytes(b"uidt", "big")
kAudioTapPropertyUID = int.from_bytes(b"tuid", "big")
kAudioTapPropertyDescription = int.from_bytes(b"tdsc", "big")
kAudioTapPropertyFormat = int.from_bytes(b"tfmt", "big")


class AudioTapNotFoundError(OSError):
    """Raised when a tap ID no longer refers to a live Core Audio tap."""


def _raise_if_missing_tap(tap_id: int, exc: OSError) -> None:
    """Translate a stale-tap OSStatus into ``AudioTapNotFoundError``."""
    if getattr(exc, "status", None) in {
        kAudioHardwareBadObjectError,
        kAudioHardwareUnknownPropertyError,
    }:
        raise AudioTapNotFoundError(
            f"Audio tap {tap_id} is no longer available. "
            "It may have been destroyed by another process."
        ) from exc


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


def set_tap_description(tap_id: int, description: TapDescription) -> None:
    """Replace the description of an existing tap.

    Core Audio applies the new description to the live tap, so a running
    capture keeps flowing while the tap is retargeted. Changing fields that
    affect the tap's stream format (``is_mono``, ``is_mixdown``,
    ``device_uid``, ``stream``) changes the audio delivered to readers
    mid-stream; catap's recorder treats that as capture drift and fails the
    capture rather than publishing mixed-format output.

    Requires the same System Audio Recording permission as reading tap
    audio; without it Core Audio refuses with a permissions error.
    """
    try:
        _set_audio_object_objc_property(
            tap_id, kAudioTapPropertyDescription, description.objc_object
        )
    except OSError as exc:
        _raise_if_missing_tap(tap_id, exc)
        if getattr(exc, "status", None) == kAudioDevicePermissionsError:
            raise PermissionError(
                f"Core Audio refused to modify tap {tap_id} "
                "(kAudioDevicePermissionsError). Modifying a live tap "
                "requires System Audio Recording permission for the app "
                "hosting this process."
            ) from exc
        raise


@dataclass(frozen=True, slots=True, kw_only=True)
class TapStreamFormat:
    """Stream format Core Audio reports for a tap."""

    sample_rate: float
    num_channels: int
    bits_per_sample: int
    bytes_per_frame: int
    is_float: bool
    is_signed_integer: bool
    is_interleaved: bool
    is_packed: bool
    is_big_endian: bool
    format_id: str


def get_tap_format(tap_id: int) -> TapStreamFormat:
    """Return the current stream format for an existing tap.

    This is the format of the data the tap delivers to any aggregate device
    that contains it. The format can change over the tap's lifetime, for
    example when a device-stream tap follows a hardware format change.
    """
    try:
        asbd = _get_audio_object_struct_property(
            tap_id, kAudioTapPropertyFormat, AudioStreamBasicDescription
        )
    except OSError as exc:
        _raise_if_missing_tap(tap_id, exc)
        raise
    assert isinstance(asbd, AudioStreamBasicDescription)
    return TapStreamFormat(
        sample_rate=asbd.mSampleRate,
        num_channels=asbd.mChannelsPerFrame,
        bits_per_sample=asbd.mBitsPerChannel,
        bytes_per_frame=asbd.mBytesPerFrame,
        is_float=bool(asbd.mFormatFlags & kAudioFormatFlagIsFloat),
        is_signed_integer=bool(asbd.mFormatFlags & kAudioFormatFlagIsSignedInteger),
        is_interleaved=not bool(asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved),
        is_packed=bool(asbd.mFormatFlags & kAudioFormatFlagIsPacked),
        is_big_endian=bool(asbd.mFormatFlags & kAudioFormatFlagIsBigEndian),
        format_id=_format_id_to_fourcc(asbd.mFormatID),
    )


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
    tap_ids = _get_audio_object_ids(
        kAudioObjectSystemObject, kAudioHardwarePropertyTapList
    )
    if not tap_ids:
        return []

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
        except OSError:
            continue

    return sorted(taps, key=lambda tap: (tap.name.casefold(), tap.uid))


def find_tap_by_uid(uid: str) -> AudioTap | None:
    """Find a visible tap by its persistent UID string."""
    if not uid:
        return None

    try:
        uid_string = NSString.stringWithString_(uid)
        tap_id = _translate_qualifier(
            kAudioObjectSystemObject,
            kAudioHardwarePropertyTranslateUIDToTap,
            ctypes.c_void_p(uid_string.__c_void_p__().value),
        )
    except OSError:
        tap_id = kAudioObjectUnknown

    if tap_id != kAudioObjectUnknown:
        try:
            resolved_uid = _get_audio_object_cfstring_property(
                tap_id, kAudioTapPropertyUID
            )
            if resolved_uid:
                return AudioTap(
                    audio_object_id=tap_id,
                    uid=resolved_uid,
                    description=get_tap_description(tap_id),
                )
        except OSError:
            pass

    # The translation property missed or the tap vanished mid-read; fall back
    # to the visible-tap listing before reporting the UID as gone.
    for tap in list_audio_taps():
        if tap.uid == uid:
            return tap
    return None


__all__ = [
    "AudioTap",
    "AudioTapNotFoundError",
    "TapStreamFormat",
    "find_tap_by_uid",
    "get_tap_description",
    "get_tap_format",
    "list_audio_taps",
    "set_tap_description",
]
