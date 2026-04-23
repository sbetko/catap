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
    pid = struct.pack("<I", 9001)
    is_outputting = struct.pack("<I", 1)

    def get_object_ids(object_id: int, selector: int) -> list[int]:
        assert object_id == process_module.kAudioObjectSystemObject
        assert selector == process_module.kAudioHardwarePropertyProcessObjectList
        return [41]

    def get_property(
        object_id: int,
        selector: int,
        scope: int = 0,
        element: int = 0,
    ) -> bytes:
        del scope, element
        if object_id == 41 and selector == process_module.kAudioProcessPropertyPID:
            return pid
        if (
            object_id == 41
            and selector == process_module.kAudioProcessPropertyIsRunningOutput
        ):
            return is_outputting
        raise AssertionError(f"unexpected property lookup: {(object_id, selector)}")

    monkeypatch.setattr(process_module, "_get_audio_object_ids", get_object_ids)
    monkeypatch.setattr(process_module, "_get_audio_object_property", get_property)
    monkeypatch.setattr(
        process_module,
        "_get_optional_audio_object_cfstring_property",
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


def test_list_audio_processes_propagates_core_audio_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_module,
        "_get_audio_object_ids",
        lambda object_id, selector: (_ for _ in ()).throw(
            OSError("core audio unavailable")
        ),
    )

    with pytest.raises(OSError, match="core audio unavailable"):
        process_module.list_audio_processes()


def test_find_process_by_name_prefers_exact_name_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_module,
        "list_audio_processes",
        lambda: [
            process_module.AudioProcess(
                2,
                200,
                "com.apple.MusicHelper",
                "MusicBox",
                False,
            ),
            process_module.AudioProcess(1, 100, "com.apple.Music", "Music", True),
        ],
    )

    process = process_module.find_process_by_name("music")

    assert process == process_module.AudioProcess(
        audio_object_id=1,
        pid=100,
        bundle_id="com.apple.Music",
        name="Music",
        is_outputting=True,
    )


def test_find_process_by_name_raises_on_ambiguous_partial_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_module,
        "list_audio_processes",
        lambda: [
            process_module.AudioProcess(1, 100, "com.apple.Music", "Music", True),
            process_module.AudioProcess(
                2,
                200,
                "com.apple.MusicHelper",
                "Music Helper",
                False,
            ),
        ],
    )

    with pytest.raises(
        process_module.AmbiguousAudioProcessError,
        match="Multiple audio processes match 'mus'",
    ) as exc_info:
        process_module.find_process_by_name("mus")

    assert len(exc_info.value.matches) == 2


def test_find_process_by_name_prefers_exact_bundle_id_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = process_module.AudioProcess(
        2,
        200,
        "com.apple.MusicHelper",
        "Music Helper",
        False,
    )
    target = process_module.AudioProcess(
        1,
        100,
        "com.apple.Music",
        "Music Player",
        True,
    )
    monkeypatch.setattr(
        process_module,
        "list_audio_processes",
        lambda: [helper, target],
    )

    assert process_module.find_process_by_name("com.apple.music") is target
