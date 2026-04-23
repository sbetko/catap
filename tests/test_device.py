"""Audio device discovery tests."""

from __future__ import annotations

import struct
from typing import Any

import pytest

import catap.bindings.device as device_module
from catap.bindings._audiotoolbox import (
    AudioStreamBasicDescription,
    kAudioFormatFlagIsFloat,
    kAudioFormatLinearPCM,
)


def _pack_ids(*ids: int) -> bytes:
    return b"".join(struct.pack("<I", object_id) for object_id in ids)


def _fake_stream_format(
    *,
    sample_rate: float,
    channels: int,
    bits_per_channel: int,
    is_float: bool,
) -> AudioStreamBasicDescription:
    return AudioStreamBasicDescription(
        sample_rate,
        kAudioFormatLinearPCM,
        kAudioFormatFlagIsFloat if is_float else 0,
        channels * max(1, bits_per_channel // 8),
        1,
        channels * max(1, bits_per_channel // 8),
        channels,
        bits_per_channel,
        0,
    )


def test_list_audio_devices_returns_stream_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_formats = {
        1001: _fake_stream_format(
            sample_rate=48_000.0,
            channels=2,
            bits_per_channel=32,
            is_float=True,
        ),
        1002: _fake_stream_format(
            sample_rate=44_100.0,
            channels=1,
            bits_per_channel=24,
            is_float=False,
        ),
        2001: _fake_stream_format(
            sample_rate=48_000.0,
            channels=2,
            bits_per_channel=32,
            is_float=True,
        ),
    }

    def _get_audio_object_property(
        object_id: int,
        selector: int,
        scope: int = device_module.kAudioObjectPropertyScopeOutput,
    ) -> bytes:
        if object_id == device_module.kAudioObjectSystemObject:
            default_system_output_selector = (
                device_module.kAudioHardwarePropertyDefaultSystemOutputDevice
            )
            return {
                device_module.kAudioHardwarePropertyDevices: _pack_ids(20, 10),
                device_module.kAudioHardwarePropertyDefaultInputDevice: _pack_ids(20),
                device_module.kAudioHardwarePropertyDefaultOutputDevice: _pack_ids(10),
                default_system_output_selector: _pack_ids(10),
            }[selector]

        if selector == device_module.kAudioDevicePropertyStreams:
            return {
                (10, device_module.kAudioObjectPropertyScopeOutput): _pack_ids(
                    1001, 1002
                ),
                (10, device_module.kAudioObjectPropertyScopeInput): b"",
                (20, device_module.kAudioObjectPropertyScopeOutput): b"",
                (20, device_module.kAudioObjectPropertyScopeInput): _pack_ids(2001),
            }[(object_id, scope)]

        if selector == device_module.kAudioStreamPropertyDirection:
            return {
                1001: _pack_ids(0),
                1002: _pack_ids(0),
                2001: _pack_ids(1),
            }[object_id]

        raise AssertionError(
            f"Unexpected property request: {(object_id, selector, scope)}"
        )

    def _get_audio_object_ids(
        object_id: int,
        selector: int,
        scope: int = device_module.kAudioObjectPropertyScopeOutput,
        element: int = 0,
    ) -> list[int]:
        del element
        if object_id == device_module.kAudioObjectSystemObject:
            assert selector == device_module.kAudioHardwarePropertyDevices
            return [20, 10]

        if selector == device_module.kAudioDevicePropertyStreams:
            return {
                (10, device_module.kAudioObjectPropertyScopeOutput): [1001, 1002],
                (10, device_module.kAudioObjectPropertyScopeInput): [],
                (20, device_module.kAudioObjectPropertyScopeOutput): [],
                (20, device_module.kAudioObjectPropertyScopeInput): [2001],
            }[(object_id, scope)]

        raise AssertionError(
            f"Unexpected object-id request: {(object_id, selector, scope)}"
        )

    def _get_audio_object_cfstring_property(object_id: int, selector: int) -> str:
        return {
            (10, device_module.kAudioDevicePropertyDeviceUID): "BuiltInSpeakerDevice",
            (10, device_module.kAudioObjectPropertyName): "Built-in Speakers",
            (10, device_module.kAudioObjectPropertyManufacturer): "Apple",
            (20, device_module.kAudioDevicePropertyDeviceUID): "USBMicDevice",
            (20, device_module.kAudioObjectPropertyName): "USB Microphone",
            (20, device_module.kAudioObjectPropertyManufacturer): "Focusrite",
            (1001, device_module.kAudioObjectPropertyName): "Main Out",
            (1002, device_module.kAudioObjectPropertyName): "Alt Out",
            (2001, device_module.kAudioObjectPropertyName): "Mic In",
        }[(object_id, selector)]

    def _get_audio_object_struct_property(
        object_id: int,
        selector: int,
        struct_type: type[Any],
    ) -> AudioStreamBasicDescription:
        assert selector == device_module.kAudioStreamPropertyPhysicalFormat
        assert struct_type is AudioStreamBasicDescription
        return stream_formats[object_id]

    monkeypatch.setattr(
        device_module, "_get_audio_object_ids", _get_audio_object_ids
    )
    monkeypatch.setattr(
        device_module, "_get_audio_object_property", _get_audio_object_property
    )
    monkeypatch.setattr(
        device_module,
        "_get_audio_object_cfstring_property",
        _get_audio_object_cfstring_property,
    )
    monkeypatch.setattr(
        device_module,
        "_get_optional_audio_object_cfstring_property",
        lambda object_id, selector, scope=0, element=0: (
            _get_audio_object_cfstring_property(object_id, selector)
        ),
    )
    monkeypatch.setattr(
        device_module,
        "_get_audio_object_struct_property",
        _get_audio_object_struct_property,
    )

    devices = device_module.list_audio_devices()

    assert [device.uid for device in devices] == [
        "BuiltInSpeakerDevice",
        "USBMicDevice",
    ]
    assert devices[0].is_default_output is True
    assert devices[0].is_default_system_output is True
    assert devices[0].is_default_input is False
    assert len(devices[0].output_streams) == 2
    assert devices[0].output_streams[0].stream_index == 0
    assert devices[0].output_streams[0].direction == "output"
    assert devices[0].output_streams[0].device_uid == "BuiltInSpeakerDevice"
    assert devices[0].output_streams[0].sample_rate == 48_000.0
    assert devices[0].output_streams[0].num_channels == 2
    assert devices[0].output_streams[0].is_float is True
    assert devices[1].is_default_input is True
    assert len(devices[1].input_streams) == 1
    assert devices[1].input_streams[0].direction == "input"


def test_find_audio_device_by_name_prefers_exact_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    speaker = device_module.AudioDevice(
        audio_object_id=1,
        uid="speaker",
        name="Built-in Speakers",
        manufacturer="Apple",
        streams=(),
        is_default_input=False,
        is_default_output=True,
        is_default_system_output=True,
    )
    headphones = device_module.AudioDevice(
        audio_object_id=2,
        uid="headphones",
        name="Headphones",
        manufacturer="Apple",
        streams=(),
        is_default_input=False,
        is_default_output=False,
        is_default_system_output=False,
    )
    monkeypatch.setattr(
        device_module, "list_audio_devices", lambda: [speaker, headphones]
    )

    assert device_module.find_audio_device_by_name("Built-in Speakers") is speaker
    assert device_module.find_audio_device_by_name("headphones") is headphones
    assert device_module.find_audio_device_by_uid("speaker") is speaker
    assert device_module.find_audio_device_by_uid("") is None


def test_find_audio_device_by_name_raises_when_partial_match_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left = device_module.AudioDevice(
        audio_object_id=1,
        uid="speaker-left",
        name="Studio Speaker Left",
        manufacturer=None,
        streams=(),
        is_default_input=False,
        is_default_output=False,
        is_default_system_output=False,
    )
    right = device_module.AudioDevice(
        audio_object_id=2,
        uid="speaker-right",
        name="Studio Speaker Right",
        manufacturer=None,
        streams=(),
        is_default_input=False,
        is_default_output=False,
        is_default_system_output=False,
    )
    monkeypatch.setattr(device_module, "list_audio_devices", lambda: [left, right])

    with pytest.raises(
        device_module.AmbiguousAudioDeviceError,
        match="Multiple audio devices match 'Speaker'",
    ):
        device_module.find_audio_device_by_name("Speaker")


def test_find_audio_device_by_name_matches_exact_uid_after_name_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    speaker = device_module.AudioDevice(
        audio_object_id=1,
        uid="speaker-main",
        name="Built-in Speakers",
        manufacturer="Apple",
        streams=(),
        is_default_input=False,
        is_default_output=True,
        is_default_system_output=True,
    )
    monitor = device_module.AudioDevice(
        audio_object_id=2,
        uid="studio-monitor",
        name="Speaker Main",
        manufacturer="Apple",
        streams=(),
        is_default_input=False,
        is_default_output=False,
        is_default_system_output=False,
    )
    monkeypatch.setattr(
        device_module, "list_audio_devices", lambda: [speaker, monitor]
    )

    assert device_module.find_audio_device_by_name("speaker-main") is speaker


@pytest.mark.parametrize("payload", [b"", b"\x01\x02\x03"])
def test_get_object_id_property_returns_none_for_short_payloads(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    monkeypatch.setattr(
        device_module,
        "_get_audio_object_property",
        lambda object_id, selector, scope=0: payload,
    )

    assert device_module._get_object_id_property(
        device_module.kAudioHardwarePropertyDefaultOutputDevice
    ) is None
