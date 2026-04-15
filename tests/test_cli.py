"""CLI behavior tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import catap.cli as cli
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
    assert "Slack (idle)" in captured.err


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
