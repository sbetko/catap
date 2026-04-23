"""Visible tap discovery tests."""

from __future__ import annotations

from typing import Any

import pytest

import catap.bindings.tap as tap_module
from catap.bindings.tap_description import TapMuteBehavior


class _FakeUUID:
    def UUIDString(self) -> str:
        return "fake-uuid"


class _FakeNSNumber:
    def __init__(self, value: int) -> None:
        self._value = value

    def unsignedIntValue(self) -> int:
        return self._value

    def integerValue(self) -> int:
        return self._value


class _FakeTapDescriptionObjC:
    def __init__(
        self,
        name: str,
        *,
        private: bool = False,
        device_uid: str | None = None,
        stream: int | None = None,
    ) -> None:
        self._name = name
        self._uuid = _FakeUUID()
        self._private = private
        self._device_uid = device_uid
        self._stream = stream

    def name(self) -> str:
        return self._name

    def UUID(self) -> _FakeUUID:
        return self._uuid

    def processes(self) -> list[_FakeNSNumber]:
        return []

    def isExclusive(self) -> bool:
        return False

    def isMono(self) -> bool:
        return False

    def isMixdown(self) -> bool:
        return False

    def isPrivate(self) -> bool:
        return self._private

    def isMuted(self) -> int:
        return int(TapMuteBehavior.UNMUTED)

    def deviceUID(self) -> str | None:
        return self._device_uid

    def stream(self) -> _FakeNSNumber | None:
        if self._stream is None:
            return None
        return _FakeNSNumber(self._stream)


def test_list_audio_taps_returns_visible_taps_sorted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptions = {
        200: _FakeTapDescriptionObjC("zeta tap", private=False),
        100: _FakeTapDescriptionObjC(
            "Alpha tap", private=True, device_uid="BuiltInSpeakerDevice", stream=1
        ),
    }

    def _get_audio_object_ids(object_id: int, selector: int) -> list[int]:
        assert object_id == tap_module.kAudioObjectSystemObject
        assert selector == tap_module.kAudioHardwarePropertyTapList
        return [200, 100]

    def _get_audio_object_cfstring_property(object_id: int, selector: int) -> str:
        assert selector == tap_module.kAudioTapPropertyUID
        return {
            100: "tap-alpha",
            200: "tap-zeta",
        }[object_id]

    def _get_audio_object_objc_property(object_id: int, selector: int) -> Any:
        assert selector == tap_module.kAudioTapPropertyDescription
        return descriptions[object_id]

    monkeypatch.setattr(
        tap_module, "_get_audio_object_ids", _get_audio_object_ids
    )
    monkeypatch.setattr(
        tap_module,
        "_get_audio_object_cfstring_property",
        _get_audio_object_cfstring_property,
    )
    monkeypatch.setattr(
        tap_module, "_get_audio_object_objc_property", _get_audio_object_objc_property
    )

    taps = tap_module.list_audio_taps()

    assert [tap.audio_object_id for tap in taps] == [100, 200]
    assert taps[0].uid == "tap-alpha"
    assert taps[0].name == "Alpha tap"
    assert taps[0].is_private is True
    assert taps[0].device_uid == "BuiltInSpeakerDevice"
    assert taps[0].stream == 1
    assert taps[1].uid == "tap-zeta"
    assert taps[1].name == "zeta tap"
    assert taps[1].is_private is False


def test_list_audio_taps_preserves_stream_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptions = {
        100: _FakeTapDescriptionObjC(
            "Alpha tap", private=True, device_uid="BuiltInSpeakerDevice", stream=0
        ),
    }

    def _get_audio_object_ids(object_id: int, selector: int) -> list[int]:
        assert object_id == tap_module.kAudioObjectSystemObject
        assert selector == tap_module.kAudioHardwarePropertyTapList
        return [100]

    def _get_audio_object_cfstring_property(object_id: int, selector: int) -> str:
        assert selector == tap_module.kAudioTapPropertyUID
        assert object_id == 100
        return "tap-alpha"

    def _get_audio_object_objc_property(object_id: int, selector: int) -> Any:
        assert selector == tap_module.kAudioTapPropertyDescription
        assert object_id == 100
        return descriptions[object_id]

    monkeypatch.setattr(
        tap_module, "_get_audio_object_ids", _get_audio_object_ids
    )
    monkeypatch.setattr(
        tap_module,
        "_get_audio_object_cfstring_property",
        _get_audio_object_cfstring_property,
    )
    monkeypatch.setattr(
        tap_module, "_get_audio_object_objc_property", _get_audio_object_objc_property
    )

    taps = tap_module.list_audio_taps()

    assert len(taps) == 1
    assert taps[0].device_uid == "BuiltInSpeakerDevice"
    assert taps[0].stream == 0


def test_find_tap_by_uid_matches_exact_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    alpha = tap_module.AudioTap(
        100,
        "tap-alpha",
        tap_module.TapDescription._from_objc_description(_FakeTapDescriptionObjC("A")),
    )
    beta = tap_module.AudioTap(
        200,
        "tap-beta",
        tap_module.TapDescription._from_objc_description(_FakeTapDescriptionObjC("B")),
    )
    monkeypatch.setattr(tap_module, "list_audio_taps", lambda: [alpha, beta])

    assert tap_module.find_tap_by_uid("tap-beta") is beta
    assert tap_module.find_tap_by_uid("missing") is None
    assert tap_module.find_tap_by_uid("") is None


def test_get_tap_description_raises_audio_tap_not_found_for_bad_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_error = OSError("bad object")
    stale_error.status = tap_module.kAudioHardwareBadObjectError  # type: ignore[attr-defined]
    monkeypatch.setattr(
        tap_module,
        "_get_audio_object_objc_property",
        lambda object_id, selector: (_ for _ in ()).throw(stale_error),
    )

    with pytest.raises(
        tap_module.AudioTapNotFoundError,
        match="Audio tap 77 is no longer available",
    ):
        tap_module.get_tap_description(77)
