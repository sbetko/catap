"""Process enumeration tests."""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

import catap.bindings.process as process_module


class _FakeApp:
    def __init__(self, bundle_id: str, name: str) -> None:
        self._bundle_id = bundle_id
        self._name = name

    def bundleIdentifier(self) -> str:
        return self._bundle_id

    def localizedName(self) -> str:
        return self._name


class _FakeWorkspace:
    def __init__(self, apps: list[_FakeApp]) -> None:
        self._apps = apps

    def runningApplications(self) -> list[_FakeApp]:
        return self._apps


def test_list_audio_processes_decodes_bundle_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_list = struct.pack("<I", 41)
    pid = struct.pack("<I", 9001)
    is_outputting = struct.pack("<I", 1)

    def get_property(
        object_id: int,
        selector: int,
        scope: int = 0,
        element: int = 0,
    ) -> bytes:
        del scope, element
        if object_id == process_module.kAudioObjectSystemObject:
            assert selector == process_module.kAudioHardwarePropertyProcessObjectList
            return process_list
        if object_id == 41 and selector == process_module.kAudioProcessPropertyPID:
            return pid
        if (
            object_id == 41
            and selector == process_module.kAudioProcessPropertyIsRunningOutput
        ):
            return is_outputting
        raise AssertionError(f"unexpected property lookup: {(object_id, selector)}")

    monkeypatch.setattr(process_module, "_get_audio_object_property", get_property)
    monkeypatch.setattr(
        process_module,
        "_get_audio_object_cfstring_property",
        lambda object_id, selector, scope=0, element=0: (
            "com.apple.Music"
            if (object_id, selector)
            == (41, process_module.kAudioProcessPropertyBundleID)
            else None
        ),
    )
    monkeypatch.setattr(
        process_module,
        "NSWorkspace",
        SimpleNamespace(
            sharedWorkspace=lambda: _FakeWorkspace(
                [_FakeApp("com.apple.Music", "Music")]
            )
        ),
    )
    monkeypatch.setattr(
        process_module,
        "NSRunningApplication",
        SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid: None),
    )

    processes = process_module.list_audio_processes()

    assert processes == [
        process_module.AudioProcess(
            audio_object_id=41,
            pid=9001,
            bundle_id="com.apple.Music",
            name="Music",
            is_outputting=True,
        )
    ]
