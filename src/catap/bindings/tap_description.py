"""Wrapper for CATapDescription Objective-C class."""

from __future__ import annotations

from collections.abc import Sequence
from enum import IntEnum
from typing import TYPE_CHECKING, Any

import objc
from Foundation import NSArray, NSNumber  # ty: ignore[unresolved-import]

if TYPE_CHECKING:
    from catap.bindings.device import AudioDeviceStream

try:
    CATapDescription = objc.lookUpClass("CATapDescription")  # ty: ignore[unresolved-attribute]
except objc.nosuchclass_error as e:  # ty: ignore[unresolved-attribute]
    raise ImportError(
        "CATapDescription class not found. "
        "Ensure you're running on macOS 14.2 or later with "
        "pyobjc-framework-CoreAudio installed."
    ) from e


def _process_id_array(processes: Sequence[int]) -> Any:
    """Convert process AudioObjectIDs into the NSArray expected by PyObjC."""
    return NSArray.arrayWithArray_(
        [NSNumber.numberWithUnsignedInt_(pid) for pid in processes]
    )


class TapMuteBehavior(IntEnum):
    """Mute behavior for tapped processes."""

    UNMUTED = 0  # Audio sent to hardware AND captured
    MUTED = 1  # Audio captured only, not sent to hardware
    # Muted only while an audio client is actively reading the tap.
    MUTED_WHEN_TAPPED = 2


class TapDescription:
    """
    Python wrapper for CATapDescription.

    Describes a tap that captures audio from processes.
    """

    @staticmethod
    def _alloc() -> Any:
        """Allocate a CATapDescription Objective-C instance."""
        return CATapDescription.alloc()

    @classmethod
    def _from_objc_description(cls, description: Any) -> TapDescription:
        """Wrap an initialized Objective-C CATapDescription."""
        instance = cls.__new__(cls)
        instance._desc = description
        return instance

    def __init__(self) -> None:
        """Create an empty tap description."""
        self._desc = self._alloc().init()

    @classmethod
    def _from_processes(
        cls, processes: Sequence[int], initializer_name: str
    ) -> TapDescription:
        """Create an instance using a CATapDescription process initializer."""
        initializer = getattr(cls._alloc(), initializer_name)
        return cls._from_objc_description(initializer(_process_id_array(processes)))

    @staticmethod
    def _resolve_device_stream(
        device: str | AudioDeviceStream,
        stream: int | None,
    ) -> tuple[str, int]:
        """Normalize a target device UID and stream index."""
        if isinstance(device, str):
            if stream is None:
                raise ValueError("stream must be provided when targeting a device UID")
            return device, stream

        if device.direction != "output":
            raise ValueError(
                "device-targeted process taps require an output stream; "
                f"got {device.direction!r} stream {device.stream_index} on "
                f"{device.device_name or device.device_uid!r}"
            )

        return device.device_uid, device.stream_index

    @classmethod
    def stereo_mixdown_of_processes(cls, processes: Sequence[int]) -> TapDescription:
        """Create a tap that mixes the given processes to stereo."""
        return cls._from_processes(processes, "initStereoMixdownOfProcesses_")

    @classmethod
    def stereo_global_tap_excluding(cls, processes: Sequence[int]) -> TapDescription:
        """Create a global stereo tap that excludes the given processes."""
        return cls._from_processes(processes, "initStereoGlobalTapButExcludeProcesses_")

    @classmethod
    def mono_mixdown_of_processes(cls, processes: Sequence[int]) -> TapDescription:
        """Create a tap that mixes the given processes to mono."""
        return cls._from_processes(processes, "initMonoMixdownOfProcesses_")

    @classmethod
    def mono_global_tap_excluding(cls, processes: Sequence[int]) -> TapDescription:
        """Create a global mono tap that excludes the given processes."""
        return cls._from_processes(processes, "initMonoGlobalTapButExcludeProcesses_")

    @classmethod
    def of_processes_for_device_stream(
        cls,
        processes: Sequence[int],
        device: str | AudioDeviceStream,
        stream: int | None = None,
    ) -> TapDescription:
        """
        Create a tap for processes routed to a specific output device stream.

        Pass either an ``AudioDeviceStream`` from ``list_audio_devices()`` or a
        raw ``device_uid`` plus ``stream`` index.
        """
        device_uid, stream_index = cls._resolve_device_stream(device, stream)
        initializer = cls._alloc().initWithProcesses_andDeviceUID_withStream_
        return cls._from_objc_description(
            initializer(_process_id_array(processes), device_uid, stream_index)
        )

    @classmethod
    def excluding_processes_for_device_stream(
        cls,
        processes: Sequence[int],
        device: str | AudioDeviceStream,
        stream: int | None = None,
    ) -> TapDescription:
        """
        Create a tap for one output device stream while excluding selected processes.

        Pass either an ``AudioDeviceStream`` from ``list_audio_devices()`` or a
        raw ``device_uid`` plus ``stream`` index.
        """
        device_uid, stream_index = cls._resolve_device_stream(device, stream)
        initializer = cls._alloc().initExcludingProcesses_andDeviceUID_withStream_
        return cls._from_objc_description(
            initializer(_process_id_array(processes), device_uid, stream_index)
        )

    @property
    def name(self) -> str:
        """Human-readable name of the tap."""
        return str(self._desc.name())

    @name.setter
    def name(self, value: str) -> None:
        self._desc.setName_(value)

    @property
    def uuid(self) -> str:
        """UUID of the tap as a string."""
        return str(self._desc.UUID().UUIDString())

    @property
    def processes(self) -> list[int]:
        """List of process AudioObjectIDs to tap or exclude."""
        ns_array = self._desc.processes()
        return [int(n.unsignedIntValue()) for n in ns_array] if ns_array else []

    @processes.setter
    def processes(self, value: Sequence[int]) -> None:
        self._desc.setProcesses_(_process_id_array(value))

    @property
    def is_exclusive(self) -> bool:
        """True if tapping all EXCEPT listed processes."""
        return bool(self._desc.isExclusive())

    @is_exclusive.setter
    def is_exclusive(self, value: bool) -> None:
        self._desc.setExclusive_(value)

    @property
    def is_mono(self) -> bool:
        """True if mixing to mono."""
        return bool(self._desc.isMono())

    @is_mono.setter
    def is_mono(self, value: bool) -> None:
        self._desc.setMono_(value)

    @property
    def is_mixdown(self) -> bool:
        """True if mixing to mono or stereo."""
        return bool(self._desc.isMixdown())

    @is_mixdown.setter
    def is_mixdown(self, value: bool) -> None:
        self._desc.setMixdown_(value)

    @property
    def is_private(self) -> bool:
        """True if tap is only visible to creator."""
        return bool(self._desc.isPrivate())

    @is_private.setter
    def is_private(self, value: bool) -> None:
        self._desc.setPrivate_(value)

    @property
    def mute_behavior(self) -> TapMuteBehavior:
        """Mute behavior for tapped processes."""
        return TapMuteBehavior(self._desc.isMuted())

    @mute_behavior.setter
    def mute_behavior(self, value: TapMuteBehavior) -> None:
        self._desc.setMuteBehavior_(int(value))

    @property
    def device_uid(self) -> str | None:
        """Optional device UID for device-specific taps."""
        uid = self._desc.deviceUID()
        return str(uid) if uid else None

    @device_uid.setter
    def device_uid(self, value: str | None) -> None:
        self._desc.setDeviceUID_(value)

    @property
    def stream(self) -> int | None:
        """Optional stream index for device-specific taps."""
        stream = self._desc.stream()
        if stream is None:
            return None
        return int(stream.integerValue())

    @stream.setter
    def stream(self, value: int | None) -> None:
        if value is not None:
            self._desc.setStream_(NSNumber.numberWithInteger_(value))
        else:
            self._desc.setStream_(None)

    @property
    def objc_object(self) -> Any:
        """Get the underlying Objective-C object."""
        return self._desc

    def __repr__(self) -> str:
        """String representation of the tap description."""
        return (
            f"TapDescription(name={self.name!r}, "
            f"processes={self.processes}, "
            f"is_exclusive={self.is_exclusive}, "
            f"is_mono={self.is_mono}, "
            f"device_uid={self.device_uid!r}, "
            f"stream={self.stream!r}, "
            f"mute_behavior={self.mute_behavior.name})"
        )
