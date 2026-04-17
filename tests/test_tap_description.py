"""TapDescription wrapper behavior tests."""

from __future__ import annotations

from typing import Any, ClassVar

import catap.bindings.tap_description as tap_description_module


class _FakeUUID:
    def UUIDString(self) -> str:
        return "fake-uuid"


class _FakeNSNumber:
    def __init__(self, value: int) -> None:
        self._value = value

    def unsignedIntValue(self) -> int:
        return self._value


class _FakeTapDescriptionObjC:
    def __init__(
        self,
        processes: list[int] | None = None,
        *,
        exclusive: bool = False,
        mono: bool = False,
        mixdown: bool = False,
    ) -> None:
        self._name = ""
        self._uuid = _FakeUUID()
        self._processes = list(processes or [])
        self._exclusive = exclusive
        self._mono = mono
        self._mixdown = mixdown
        self._private = False
        self._mute_behavior = tap_description_module.TapMuteBehavior.UNMUTED

    def name(self) -> str:
        return self._name

    def setName_(self, value: str) -> None:
        self._name = value

    def UUID(self) -> _FakeUUID:
        return self._uuid

    def processes(self) -> list[_FakeNSNumber]:
        return [_FakeNSNumber(process) for process in self._processes]

    def setProcesses_(self, value: list[int]) -> None:
        self._processes = list(value)

    def isExclusive(self) -> bool:
        return self._exclusive

    def setExclusive_(self, value: bool) -> None:
        self._exclusive = value

    def isMono(self) -> bool:
        return self._mono

    def setMono_(self, value: bool) -> None:
        self._mono = value

    def isMixdown(self) -> bool:
        return self._mixdown

    def setMixdown_(self, value: bool) -> None:
        self._mixdown = value

    def isPrivate(self) -> bool:
        return self._private

    def setPrivate_(self, value: bool) -> None:
        self._private = value

    def isMuted(self) -> int:
        return int(self._mute_behavior)

    def setMuteBehavior_(self, value: int) -> None:
        self._mute_behavior = tap_description_module.TapMuteBehavior(value)


class _FakeTapDescriptionAllocator:
    _initializer_flags: ClassVar[dict[str, dict[str, bool]]] = {
        "initMonoMixdownOfProcesses_": {"mono": True, "mixdown": True},
        "initMonoGlobalTapButExcludeProcesses_": {
            "exclusive": True,
            "mono": True,
            "mixdown": True,
        },
    }

    def init(self) -> _FakeTapDescriptionObjC:
        return _FakeTapDescriptionObjC()

    def __getattr__(self, name: str) -> Any:
        if name not in self._initializer_flags:
            raise AttributeError(name)

        def _initializer(processes: list[int]) -> _FakeTapDescriptionObjC:
            return _FakeTapDescriptionObjC(
                processes, **self._initializer_flags[name]
            )

        return _initializer


class _FakeCATapDescription:
    @staticmethod
    def alloc() -> _FakeTapDescriptionAllocator:
        return _FakeTapDescriptionAllocator()


def _install_fake_tap_description(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        tap_description_module, "CATapDescription", _FakeCATapDescription
    )
    monkeypatch.setattr(
        tap_description_module, "_process_id_array", lambda processes: list(processes)
    )


def test_mono_factory_methods_configure_expected_flags(
    monkeypatch: Any,
) -> None:
    _install_fake_tap_description(monkeypatch)

    mono_tap = tap_description_module.TapDescription.mono_mixdown_of_processes([11, 12])
    global_mono_tap = tap_description_module.TapDescription.mono_global_tap_excluding(
        [21]
    )

    assert mono_tap.processes == [11, 12]
    assert mono_tap.is_mono is True
    assert mono_tap.is_mixdown is True
    assert mono_tap.is_exclusive is False

    assert global_mono_tap.processes == [21]
    assert global_mono_tap.is_mono is True
    assert global_mono_tap.is_mixdown is True
    assert global_mono_tap.is_exclusive is True


def test_mute_behavior_round_trips_muted_when_tapped(monkeypatch: Any) -> None:
    _install_fake_tap_description(monkeypatch)

    tap_description = tap_description_module.TapDescription()
    tap_description.mute_behavior = (
        tap_description_module.TapMuteBehavior.MUTED_WHEN_TAPPED
    )

    assert (
        tap_description.mute_behavior
        is tap_description_module.TapMuteBehavior.MUTED_WHEN_TAPPED
    )
