"""CLI behavior tests."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import catap.cli as cli
from catap.bindings.device import AudioDevice, AudioDeviceStream
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
        self.captured_only_silence = False
        self._capture_failure_event = threading.Event()

    def start(self) -> None:
        self._capture_failure_event.clear()
        return None

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None

    def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
        return self._capture_failure_event.wait(timeout)


def test_capture_wait_skips_waiter_when_stop_was_already_requested() -> None:
    class _UnexpectedWaiter:
        def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
            del timeout
            raise AssertionError("failure waiter should not be called")

    stop_event = threading.Event()
    stop_event.set()

    cli._wait_for_stop_or_capture_failure(_UnexpectedWaiter(), stop_event, None)


def test_capture_wait_preserves_monotonic_duration_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    timeouts: list[float] = []

    class _AdvancingWaiter:
        def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
            nonlocal clock
            assert timeout is not None
            timeouts.append(timeout)
            clock += timeout
            return False

    monkeypatch.setattr(cli.time, "monotonic", lambda: clock)

    cli._wait_for_stop_or_capture_failure(
        _AdvancingWaiter(),
        threading.Event(),
        0.25,
    )

    assert timeouts == pytest.approx([0.1, 0.1, 0.05])


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


@pytest.mark.parametrize("duration", ["nan", "inf", "-inf"])
def test_record_duration_must_be_finite(
    duration: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "Music", f"--duration={duration}"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "must be finite" in captured.err


def test_record_output_path_must_not_be_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "Music", "-o", ""])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "output path must not be empty" in captured.err
    assert "Traceback" not in captured.err


def test_record_requires_a_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "a target is required" in captured.err
    assert "--bundle-id" in captured.err


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
        _resolve_record_processes=lambda names, pids, audio_ids: [object()],
        _build_processes_tap=lambda processes, **kwargs: fake_tap,
        RecordingSession=_FakeSession,
    )

    exit_code = main(["record", "Music", "-o", "/tmp/missing/output.wav"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "This looks like an output file problem" in captured.err
    assert "Screen & System Audio Recording" not in captured.err


def test_record_reports_core_audio_oserror_with_permission_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()

    class _FakeSession:
        def __init__(self, tap_desc: object, output: str) -> None:
            self.tap_desc = tap_desc
            self.output = output

        def start(self) -> None:
            raise OSError("Core Audio rejected capture")

        def close(self) -> None:
            return None

    _set_cli_symbols(
        monkeypatch,
        _resolve_record_processes=lambda names, pids, audio_ids: [object()],
        _build_processes_tap=lambda processes, **kwargs: fake_tap,
        RecordingSession=_FakeSession,
    )

    exit_code = main(["record", "Music"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error starting recording: Core Audio rejected capture" in captured.err
    assert "Screen & System Audio Recording" in captured.err
    assert "output file problem" not in captured.err


def test_record_reports_runtime_error_during_start_without_hint_or_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()

    class _FakeSession:
        def __init__(self, tap_desc: object, output: str) -> None:
            self.tap_desc = tap_desc
            self.output = output

        def start(self) -> None:
            raise RuntimeError("native recorder unavailable")

        def close(self) -> None:
            raise RuntimeError("cleanup also failed")

    _set_cli_symbols(
        monkeypatch,
        _resolve_record_processes=lambda names, pids, audio_ids: [object()],
        _build_processes_tap=lambda processes, **kwargs: fake_tap,
        RecordingSession=_FakeSession,
    )

    exit_code = main(["record", "Music"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error starting recording: native recorder unavailable" in captured.err
    assert "cleanup also failed" not in captured.err
    assert "Screen & System Audio Recording" not in captured.err
    assert "output file problem" not in captured.err
    assert "Traceback" not in captured.err


def test_record_reports_unsupported_format_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _UnsupportedFormatSession(_SuccessfulSession):
        def start(self) -> None:
            raise cli.UnsupportedTapFormatError(
                "Unsupported tap layout: non-interleaved audio"
            )

    _set_cli_symbols(
        monkeypatch,
        _resolve_record_processes=lambda names, pids, audio_ids: [object()],
        _build_processes_tap=lambda processes, **kwargs: object(),
        RecordingSession=_UnsupportedFormatSession,
    )

    exit_code = main(["record", "Music"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error starting recording: Unsupported tap layout" in captured.err
    assert "Traceback" not in captured.err


def test_record_reports_runtime_error_during_stop_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()

    class _FakeSession(_SuccessfulSession):
        def stop(self) -> None:
            raise RuntimeError("dropped native audio buffers")

        def close(self) -> None:
            raise RuntimeError("cleanup also failed")

    _set_cli_symbols(
        monkeypatch,
        _resolve_record_processes=lambda names, pids, audio_ids: [object()],
        _build_processes_tap=lambda processes, **kwargs: fake_tap,
        RecordingSession=_FakeSession,
    )

    exit_code = main(["record", "Music", "-d", "0.001"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Recording error: dropped native audio buffers" in captured.err
    assert "cleanup also failed" not in captured.err
    assert "Traceback" not in captured.err


def test_record_live_failure_wakes_indefinite_wait_and_uses_stop_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FailedSession(_SuccessfulSession):
        def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
            del timeout
            return True

        def stop(self) -> None:
            raise RuntimeError("tap disappeared")

    _set_cli_symbols(
        monkeypatch,
        _resolve_record_processes=lambda names, pids, audio_ids: [object()],
        _build_processes_tap=lambda processes, **kwargs: object(),
        RecordingSession=_FailedSession,
    )

    exit_code = main(["record", "Music"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Recording error: tap disappeared" in captured.err
    assert "Traceback" not in captured.err


def test_record_suppresses_runtime_error_from_final_close(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()

    class _FakeSession(_SuccessfulSession):
        def close(self) -> None:
            raise RuntimeError("already closed")

    _set_cli_symbols(
        monkeypatch,
        _resolve_record_processes=lambda names, pids, audio_ids: [object()],
        _build_processes_tap=lambda processes, **kwargs: fake_tap,
        RecordingSession=_FakeSession,
    )

    exit_code = main(["record", "Music", "-d", "0.001"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Saved to: output.wav" in captured.out
    assert "already closed" not in captured.err
    assert "Traceback" not in captured.err


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

    def _build_tap(
        processes_arg: list[AudioProcess],
        *,
        mute: object = False,
        mono: bool = False,
        visible: bool = False,
    ) -> object:
        seen["processes"] = tuple(processes_arg)
        seen["mute"] = mute
        return fake_tap

    _set_cli_symbols(
        monkeypatch,
        list_audio_processes=lambda: [process],
        build_processes_tap_description=_build_tap,
        RecordingSession=_SuccessfulSession,
    )

    exit_code = main(
        ["record", "--pid", "111", "--mute", "-d", "0.001", "-o", "tone.wav"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen == {"processes": (process,), "mute": cli.TapMuteBehavior.MUTED}
    assert "Recording from: Tone (PID: 111, Audio ID: 11)" in captured.out


def test_record_can_target_process_by_audio_object_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()
    process = AudioProcess(11, 111, "com.example.tone", "Tone", True)
    seen: dict[str, object] = {}

    def _build_tap(
        processes_arg: list[AudioProcess],
        *,
        mute: object = False,
        mono: bool = False,
        visible: bool = False,
    ) -> object:
        seen["processes"] = tuple(processes_arg)
        seen["mute"] = mute
        return fake_tap

    _set_cli_symbols(
        monkeypatch,
        list_audio_processes=lambda: [process],
        build_processes_tap_description=_build_tap,
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
    assert seen == {
        "processes": (process,),
        "mute": cli.TapMuteBehavior.UNMUTED,
    }
    assert "Recording from: Tone (PID: 111, Audio ID: 11)" in captured.out


def test_record_warns_when_capture_contains_only_silence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = AudioProcess(11, 111, "com.example.tone", "Tone", True)

    class _SilentSession(_SuccessfulSession):
        def __init__(self, tap_desc: object, output: str) -> None:
            super().__init__(tap_desc, output)
            self.captured_only_silence = True

    _set_cli_symbols(
        monkeypatch,
        list_audio_processes=lambda: [process],
        build_processes_tap_description=lambda processes_arg, **kwargs: object(),
        RecordingSession=_SilentSession,
    )

    exit_code = main(["record", "--pid", "111", "-d", "0.001", "-o", "tone.wav"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "contained only silence" in captured.err
    assert "Screen & System Audio Recording" in captured.err


def test_record_does_not_warn_when_capture_has_audio(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = AudioProcess(11, 111, "com.example.tone", "Tone", True)

    _set_cli_symbols(
        monkeypatch,
        list_audio_processes=lambda: [process],
        build_processes_tap_description=lambda processes_arg, **kwargs: object(),
        RecordingSession=_SuccessfulSession,
    )

    exit_code = main(["record", "--pid", "111", "-d", "0.001", "-o", "tone.wav"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "contained only silence" not in captured.err


def test_system_record_can_exclude_by_pid_and_audio_object_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    tone = AudioProcess(12, 222, None, "Unknown", True)
    seen: dict[str, object] = {}

    def _build_system_tap(
        excluded: list[AudioProcess],
        *,
        mono: bool = False,
        visible: bool = False,
    ) -> object:
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


def test_record_combines_process_selectors_into_one_tap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tap = object()
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    tone = AudioProcess(12, 222, "com.example.tone", "Tone", True)
    seen: dict[str, object] = {}

    def _build_tap(
        processes_arg: list[AudioProcess],
        *,
        mute: object = False,
        mono: bool = False,
        visible: bool = False,
    ) -> object:
        seen["processes"] = tuple(processes_arg)
        return fake_tap

    _set_cli_symbols(
        monkeypatch,
        find_process_by_name=lambda name: music if name == "Music" else None,
        list_audio_processes=lambda: [music, tone],
        build_processes_tap_description=_build_tap,
        RecordingSession=_SuccessfulSession,
    )

    exit_code = main(
        ["record", "Music", "--pid", "222", "-d", "0.001", "-o", "mix.wav"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen == {"processes": (music, tone)}
    assert "Recording from: Music (PID: 111, Audio ID: 11)" in captured.out
    assert "Recording from: Tone (PID: 222, Audio ID: 12)" in captured.out


def test_record_rejects_process_selectors_with_system(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "--system", "--pid", "111"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "choose one target kind" in captured.err


def test_record_rejects_both_mute_modes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "Music", "--mute", "--mute-when-tapped"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--mute and --mute-when-tapped are mutually exclusive" in captured.err


def test_record_rejects_tap_creation_options_with_tap(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "--tap", "some-uid", "--mute"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "records an existing tap" in captured.err


def test_record_rejects_mono_with_device(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "--device", "Speakers", "--mono"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--mono cannot be used with --device" in captured.err


def test_record_rejects_no_restore_without_bundle_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "Music", "--no-restore"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--no-restore requires --bundle-id" in captured.err


def test_record_rejects_stream_without_device(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record", "Music", "--stream", "0"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--stream requires --device" in captured.err


def test_record_multitrack_requires_a_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record-multitrack"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "APP_NAME, --pid, or --audio-object-id is required" in captured.err


def test_record_multitrack_rejects_both_mute_modes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["record-multitrack", "Music", "Zoom", "--mute", "--mute-when-tapped"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--mute and --mute-when-tapped are mutually exclusive" in captured.err


def test_record_multitrack_rejects_single_app_without_mic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    _set_cli_symbols(
        monkeypatch,
        _resolve_record_processes=lambda names, pids, audio_ids: [process],
    )

    exit_code = main(["record-multitrack", "Music"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "needs at least two tracks" in captured.err
    assert "'catap record'" in captured.err


def test_record_multitrack_output_directory_must_not_be_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["record-multitrack", "Music", "Zoom", "--output-dir", ""])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "output path must not be empty" in captured.err
    assert "Traceback" not in captured.err


def test_record_multitrack_reports_unsupported_format_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)

    class _UnsupportedFormatSession:
        track_labels = ("Music", "Zoom")
        output_paths = ("Music.wav", "Zoom.wav")

        def start(self) -> None:
            raise cli.UnsupportedTapFormatError("Unsupported tap channel count: 0")

        def close(self) -> None:
            return None

    _set_cli_symbols(
        monkeypatch,
        _resolve_record_processes=lambda names, pids, audio_ids: [music, zoom],
        record_multitrack=lambda *args, **kwargs: _UnsupportedFormatSession(),
    )

    exit_code = main(["record-multitrack", "Music", "Zoom"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error starting recording: Unsupported tap channel count" in captured.err
    assert "Traceback" not in captured.err


def test_record_multitrack_live_failure_wakes_indefinite_wait(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    music = AudioProcess(11, 111, "com.apple.Music", "Music", True)
    zoom = AudioProcess(12, 222, "us.zoom.xos", "Zoom", True)

    class _FailedSession:
        track_labels = ("Music", "Zoom")
        output_paths = ("Music.wav", "Zoom.wav")

        def start(self) -> None:
            return None

        def wait_for_capture_failure(self, timeout: float | None = None) -> bool:
            del timeout
            return True

        def stop(self) -> None:
            raise RuntimeError("track worker failed")

        def close(self) -> None:
            return None

    _set_cli_symbols(
        monkeypatch,
        _resolve_record_processes=lambda names, pids, audio_ids: [music, zoom],
        record_multitrack=lambda *args, **kwargs: _FailedSession(),
    )

    exit_code = main(["record-multitrack", "Music", "Zoom"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Recording error: track worker failed" in captured.err
    assert "Traceback" not in captured.err


def test_list_taps_reports_when_no_taps_are_visible(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_cli_symbols(monkeypatch, list_audio_taps=lambda: [])

    exit_code = main(["list-taps"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No visible audio taps." in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("exclusive", "expected"),
    [(False, "1 bundle ID"), (True, "global -1 bundle ID")],
    ids=["inclusive", "exclusive"],
)
def test_list_taps_reports_bundle_id_targets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exclusive: bool,
    expected: str,
) -> None:
    description = SimpleNamespace(
        bundle_ids=["com.apple.Music"],
        is_exclusive=exclusive,
        processes=[],
        mute_behavior=cli.TapMuteBehavior.UNMUTED,
    )
    tap = SimpleNamespace(
        description=description,
        device_uid=None,
        stream=None,
        name="Music bundle tap",
        uid="tap-uid",
        is_private=False,
    )
    _set_cli_symbols(monkeypatch, list_audio_taps=lambda: [tap])

    exit_code = main(["list-taps"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert expected in captured.out
    assert "0 process(es)" not in captured.out


def test_list_devices_prints_device_and_stream_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = AudioDeviceStream(
        audio_object_id=71,
        device_uid="BuiltInSpeakerDevice",
        device_name="MacBook Pro Speakers",
        stream_index=0,
        direction="output",
        name="Speaker Stream",
        num_channels=2,
        sample_rate=48_000.0,
        bits_per_channel=32,
        is_float=True,
        format_id=0,
    )
    device = AudioDevice(
        audio_object_id=70,
        uid="BuiltInSpeakerDevice",
        name="MacBook Pro Speakers",
        manufacturer="Apple",
        streams=(stream,),
        is_default_input=False,
        is_default_output=True,
        is_default_system_output=False,
    )
    _set_cli_symbols(monkeypatch, list_audio_devices=lambda: [device])

    exit_code = main(["list-devices"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "MacBook Pro Speakers (default output)" in captured.out
    assert "UID: BuiltInSpeakerDevice" in captured.out
    assert "[output 0] 2ch 48000 Hz float32" in captured.out


def test_record_rejects_bundle_id_with_device(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["record", "--device", "Speakers", "--bundle-id", "com.apple.Music"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--bundle-id cannot be used with --device" in captured.err
