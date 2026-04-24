"""CLI behavior tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import catap.cli as cli
from catap.bindings.process import AmbiguousAudioProcessError, AudioProcess
from catap.cli import main


@dataclass
class _FakeProcess:
    audio_object_id: int
    pid: int
    bundle_id: str | None
    name: str
    is_outputting: bool


def _set_cli_symbols(monkeypatch: pytest.MonkeyPatch, **attrs: object) -> None:
    for name, value in attrs.items():
        monkeypatch.setattr(cli, name, value)


class _SuccessfulSession:
    def __init__(self, tap_desc: object, output: str) -> None:
        self.tap_desc = tap_desc
        self.output = output
        self.tap_id = 42
        self.duration_seconds = 0.01

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_list_apps_filters_idle_processes_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_cli_symbols(
        monkeypatch,
        list_audio_processes=lambda: [
            _FakeProcess(1, 111, "com.apple.Music", "Music", True),
            _FakeProcess(2, 222, "com.tinyspeck.slackmacgap", "Slack", False),
        ],
    )

    exit_code = main(["list-apps"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Music" in captured.out
    assert "Slack" not in captured.out


def test_list_apps_all_includes_idle_processes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_cli_symbols(
        monkeypatch,
        list_audio_processes=lambda: [
            _FakeProcess(1, 111, "com.apple.Music", "Music", True),
            _FakeProcess(2, 222, "com.tinyspeck.slackmacgap", "Slack", False),
        ],
    )

    exit_code = main(["list-apps", "--all"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Music" in captured.out
    assert "Slack" in captured.out


def test_record_returns_error_when_process_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_cli_symbols(
        monkeypatch,
        find_process_by_name=lambda _: None,
        list_audio_processes=lambda: [
            _FakeProcess(2, 222, "com.tinyspeck.slackmacgap", "Slack", False)
        ],
    )

    exit_code = main(["record", "Music", "-d", "1"])
    captured = capsys.readouterr()

    assert exit_code != 0
    assert "No audio process found matching 'Music'" in captured.err
    assert "Available audio processes:" in captured.err
    assert "Slack" in captured.err
    assert "PID: 222" in captured.err
    assert "Audio ID: 2" in captured.err
    assert "Bundle ID: com.tinyspeck.slackmacgap" in captured.err


def test_record_duration_must_be_positive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "Music", "-d", "0"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "must be greater than 0" in captured.err


def test_record_requires_app_name_when_not_recording_system_audio(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert (
        "APP_NAME, --pid, or --audio-object-id is required unless --system is set"
        in captured.err
    )


def test_record_does_not_report_output_error_as_permissions_issue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()

    class _FakeSession:
        def __init__(self, tap_desc: object, output: str) -> None:
            self.tap_desc = tap_desc
            self.output = output
            self.tap_id = None

        def start(self) -> None:
            raise FileNotFoundError(2, "No such file or directory", self.output)

        def close(self) -> None:
            return None

    _set_cli_symbols(
        monkeypatch,
        _build_app_tap=lambda app_name, mute: fake_tap,
        RecordingSession=_FakeSession,
    )

    exit_code = main(["record", "Music", "-o", "/tmp/missing/output.wav"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "This looks like an output file problem" in captured.err
    assert "Screen & System Audio Recording" not in captured.err


def test_record_reports_ambiguous_process_matches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    matches = (
        AudioProcess(1, 111, "com.apple.Music", "Music", True),
        AudioProcess(2, 222, "com.apple.MusicHelper", "Music", False),
    )

    def _raise_ambiguous(_: str) -> AudioProcess | None:
        raise AmbiguousAudioProcessError("Music", matches)

    _set_cli_symbols(monkeypatch, find_process_by_name=_raise_ambiguous)

    exit_code = main(["record", "Music", "-d", "1"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Multiple audio processes match 'Music':" in captured.err
    assert "PID: 111" in captured.err
    assert "PID: 222" in captured.err


def test_record_can_target_process_by_pid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()
    process = AudioProcess(11, 111, "com.example.tone", "Tone", True)
    seen: dict[str, object] = {}

    def _build_tap(process_arg: AudioProcess, *, mute: bool = False) -> object:
        seen["process"] = process_arg
        seen["mute"] = mute
        return fake_tap

    _set_cli_symbols(
        monkeypatch,
        list_audio_processes=lambda: [process],
        build_process_tap_description=_build_tap,
        RecordingSession=_SuccessfulSession,
    )

    exit_code = main(
        ["record", "--pid", "111", "--mute", "-d", "0.001", "-o", "tone.wav"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen == {"process": process, "mute": True}
    assert "Recording from: Tone (PID: 111, Audio ID: 11)" in captured.out


def test_record_can_target_process_by_audio_object_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()
    process = AudioProcess(11, 111, "com.example.tone", "Tone", True)
    seen: dict[str, object] = {}

    def _build_tap(process_arg: AudioProcess, *, mute: bool = False) -> object:
        seen["process"] = process_arg
        seen["mute"] = mute
        return fake_tap

    _set_cli_symbols(
        monkeypatch,
        list_audio_processes=lambda: [process],
        build_process_tap_description=_build_tap,
        RecordingSession=_SuccessfulSession,
    )

    exit_code = main(
        [
            "record",
            "--audio-id",
            "11",
            "-d",
            "0.001",
            "-o",
            "tone.wav",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen == {"process": process, "mute": False}
    assert "Recording from: Tone (PID: 111, Audio ID: 11)" in captured.out


def test_system_record_can_exclude_by_pid_and_audio_object_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    tone = AudioProcess(12, 222, None, "Unknown", True)
    seen: dict[str, object] = {}

    def _build_system_tap(excluded: list[AudioProcess]) -> object:
        seen["excluded"] = tuple(excluded)
        return fake_tap

    _set_cli_symbols(
        monkeypatch,
        list_audio_processes=lambda: [music, tone],
        build_system_tap_description=_build_system_tap,
        RecordingSession=_SuccessfulSession,
    )

    exit_code = main(
        [
            "record",
            "--system",
            "--exclude-pid",
            "111",
            "--exclude-audio-id",
            "12",
            "-d",
            "0.001",
            "-o",
            "mix.wav",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen == {"excluded": (music, tone)}
    assert "Excluding: Music (PID: 111, Audio ID: 11)" in captured.out
    assert "Excluding: Unknown (PID: 222, Audio ID: 12)" in captured.out


def test_record_rejects_multiple_process_selectors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "Music", "--pid", "111"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "choose only one of APP_NAME, --pid, or --audio-object-id" in captured.err


def test_record_rejects_process_selectors_with_system(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "--system", "--pid", "111"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--pid and --audio-object-id cannot be used with --system" in captured.err
