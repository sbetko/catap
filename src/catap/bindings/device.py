"""Discover Core Audio devices and their streams."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from catap.bindings._audiotoolbox import (
    AudioStreamBasicDescription,
    kAudioFormatFlagIsFloat,
)
from catap.bindings._coreaudio import (
    get_optional_property_cfstring as _get_optional_audio_object_cfstring_property,
    get_property_bytes as _get_audio_object_property,
    get_property_cfstring as _get_audio_object_cfstring_property,
    get_property_object_ids as _get_audio_object_ids,
    get_property_struct as _get_audio_object_struct_property,
    kAudioObjectPropertyScopeInput,
    kAudioObjectPropertyScopeOutput,
    kAudioObjectSystemObject,
)

kAudioHardwarePropertyDevices = int.from_bytes(b"dev#", "big")
kAudioHardwarePropertyDefaultInputDevice = int.from_bytes(b"dIn ", "big")
kAudioHardwarePropertyDefaultOutputDevice = int.from_bytes(b"dOut", "big")
kAudioHardwarePropertyDefaultSystemOutputDevice = int.from_bytes(b"sOut", "big")
kAudioObjectPropertyName = int.from_bytes(b"lnam", "big")
kAudioObjectPropertyManufacturer = int.from_bytes(b"lmak", "big")
kAudioDevicePropertyDeviceUID = int.from_bytes(b"uid ", "big")
kAudioDevicePropertyStreams = int.from_bytes(b"stm#", "big")
kAudioStreamPropertyDirection = int.from_bytes(b"sdir", "big")
kAudioStreamPropertyPhysicalFormat = int.from_bytes(b"pft ", "big")

_INPUT_SCOPE = (kAudioObjectPropertyScopeInput, "input")
_OUTPUT_SCOPE = (kAudioObjectPropertyScopeOutput, "output")


@dataclass(frozen=True, slots=True)
class AudioDeviceStream:
    """Describes one hardware stream on a Core Audio device."""

    audio_object_id: int
    device_uid: str
    device_name: str
    stream_index: int
    direction: Literal["input", "output"]
    name: str
    num_channels: int
    sample_rate: float
    bits_per_channel: int
    is_float: bool
    format_id: int


@dataclass(frozen=True, slots=True)
class AudioDevice:
    """Represents a Core Audio hardware device."""

    audio_object_id: int
    uid: str
    name: str
    manufacturer: str | None
    streams: tuple[AudioDeviceStream, ...]
    is_default_input: bool
    is_default_output: bool
    is_default_system_output: bool

    @property
    def input_streams(self) -> tuple[AudioDeviceStream, ...]:
        """Streams that capture input into the device."""
        return tuple(stream for stream in self.streams if stream.direction == "input")

    @property
    def output_streams(self) -> tuple[AudioDeviceStream, ...]:
        """Streams that play output through the device."""
        return tuple(stream for stream in self.streams if stream.direction == "output")


class AmbiguousAudioDeviceError(LookupError):
    """Raised when a device query matches more than one device."""

    def __init__(self, query: str, matches: Iterable[AudioDevice]) -> None:
        self.query = query
        self.matches = tuple(matches)

        formatted_matches = ", ".join(
            f"{device.name} (UID: {device.uid})" for device in self.matches[:5]
        )
        if len(self.matches) > 5:
            formatted_matches = f"{formatted_matches}, and {len(self.matches) - 5} more"

        super().__init__(f"Multiple audio devices match '{query}': {formatted_matches}")


def _get_object_id_property(selector: int) -> int | None:
    data = _get_audio_object_property(kAudioObjectSystemObject, selector)
    if len(data) < 4:
        return None
    return int.from_bytes(data[:4], "little")


def _stream_direction(
    stream_id: int,
    fallback: Literal["input", "output"],
) -> Literal["input", "output"]:
    try:
        data = _get_audio_object_property(stream_id, kAudioStreamPropertyDirection)
    except OSError:
        return fallback

    if len(data) < 4:
        return fallback

    return "input" if int.from_bytes(data[:4], "little") else "output"


def _stream_name(
    stream_id: int,
    direction: Literal["input", "output"],
    index: int,
) -> str:
    name = _get_optional_audio_object_cfstring_property(
        stream_id, kAudioObjectPropertyName
    )
    if name:
        return name
    return f"{direction.title()} Stream {index}"


def _device_streams(
    device_id: int,
    device_uid: str,
    device_name: str,
) -> tuple[AudioDeviceStream, ...]:
    streams: list[AudioDeviceStream] = []

    for scope, fallback_direction in (_OUTPUT_SCOPE, _INPUT_SCOPE):
        try:
            stream_ids = _get_audio_object_ids(
                device_id, kAudioDevicePropertyStreams, scope=scope
            )
        except OSError:
            continue

        for stream_index, stream_id in enumerate(stream_ids):
            try:
                stream_format = _get_audio_object_struct_property(
                    stream_id,
                    kAudioStreamPropertyPhysicalFormat,
                    AudioStreamBasicDescription,
                )
                direction = _stream_direction(stream_id, fallback_direction)
                streams.append(
                    AudioDeviceStream(
                        audio_object_id=stream_id,
                        device_uid=device_uid,
                        device_name=device_name,
                        stream_index=stream_index,
                        direction=direction,
                        name=_stream_name(stream_id, direction, stream_index),
                        num_channels=stream_format.mChannelsPerFrame,
                        sample_rate=stream_format.mSampleRate,
                        bits_per_channel=stream_format.mBitsPerChannel,
                        is_float=bool(
                            stream_format.mFormatFlags & kAudioFormatFlagIsFloat
                        ),
                        format_id=stream_format.mFormatID,
                    )
                )
            except OSError:
                continue

    return tuple(streams)


def list_audio_devices() -> list[AudioDevice]:
    """List the Core Audio devices currently visible to the system."""
    device_ids = _get_audio_object_ids(
        kAudioObjectSystemObject,
        kAudioHardwarePropertyDevices,
    )
    if not device_ids:
        return []

    default_input_id = _get_object_id_property(kAudioHardwarePropertyDefaultInputDevice)
    default_output_id = _get_object_id_property(
        kAudioHardwarePropertyDefaultOutputDevice
    )
    default_system_output_id = _get_object_id_property(
        kAudioHardwarePropertyDefaultSystemOutputDevice
    )

    devices: list[AudioDevice] = []
    for device_id in device_ids:
        try:
            uid = _get_audio_object_cfstring_property(
                device_id,
                kAudioDevicePropertyDeviceUID,
            )
            if not uid:
                continue

            name = (
                _get_optional_audio_object_cfstring_property(
                    device_id, kAudioObjectPropertyName
                )
                or uid
            )
            manufacturer = _get_optional_audio_object_cfstring_property(
                device_id, kAudioObjectPropertyManufacturer
            )

            devices.append(
                AudioDevice(
                    audio_object_id=device_id,
                    uid=uid,
                    name=name,
                    manufacturer=manufacturer,
                    streams=_device_streams(device_id, uid, name),
                    is_default_input=device_id == default_input_id,
                    is_default_output=device_id == default_output_id,
                    is_default_system_output=device_id == default_system_output_id,
                )
            )
        except OSError:
            continue

    return sorted(
        devices,
        key=lambda device: (
            not device.is_default_output,
            device.name.casefold(),
            device.uid,
        ),
    )


def find_audio_device_by_uid(uid: str) -> AudioDevice | None:
    """Find a device by its Core Audio UID."""
    if not uid:
        return None

    for device in list_audio_devices():
        if device.uid == uid:
            return device
    return None


def find_audio_device_by_name(name: str) -> AudioDevice | None:
    """Find a device by exact or uniquely partial name match."""
    if not name:
        return None

    query = name.casefold()
    devices = list_audio_devices()

    exact_name_matches = [
        device for device in devices if device.name.casefold() == query
    ]
    if exact_name_matches:
        if len(exact_name_matches) > 1:
            raise AmbiguousAudioDeviceError(name, exact_name_matches)
        return exact_name_matches[0]

    exact_uid_matches = [device for device in devices if device.uid.casefold() == query]
    if exact_uid_matches:
        if len(exact_uid_matches) > 1:
            raise AmbiguousAudioDeviceError(name, exact_uid_matches)
        return exact_uid_matches[0]

    partial_name_matches = [
        device for device in devices if query in device.name.casefold()
    ]
    if len(partial_name_matches) > 1:
        raise AmbiguousAudioDeviceError(name, partial_name_matches)
    if partial_name_matches:
        return partial_name_matches[0]
    return None


__all__ = [
    "AmbiguousAudioDeviceError",
    "AudioDevice",
    "AudioDeviceStream",
    "find_audio_device_by_name",
    "find_audio_device_by_uid",
    "list_audio_devices",
]
