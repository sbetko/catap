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
    assert (
        "Slack (PID: 222, Bundle ID: com.tinyspeck.slackmacgap, idle)"
        in captured.err
    )


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
    assert "APP_NAME is required unless --system is set" in captured.err


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
