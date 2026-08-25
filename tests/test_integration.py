"""Opt-in macOS integration smoke tests."""

from __future__ import annotations

import array
import math
import os
import platform
import subprocess
import time
import wave
from pathlib import Path

import pytest

RUN_INTEGRATION = os.getenv("CATAP_RUN_INTEGRATION") == "1"
RUN_TONE_INTEGRATION = os.getenv("CATAP_RUN_TONE_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only"),
]

_TONE_FREQUENCY_HZ = 1_000.0
_QUICKTIME_APP_NAME = "QuickTime Player"
_QUICKTIME_BUNDLE_ID = "com.apple.QuickTimePlayerX"


def _write_tone_wav(
    path: Path,
    *,
    frequency: float,
    duration: float,
    sample_rate: int = 44_100,
    amplitude: float = 0.4,
) -> None:
    """Write a mono 16-bit sine tone for playback through ``afplay``."""
    frame_count = int(duration * sample_rate)
    scale = amplitude * 32_767.0
    samples = array.array(
        "h",
        (
            int(scale * math.sin(2.0 * math.pi * frequency * index / sample_rate))
            for index in range(frame_count)
        ),
    )
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())


def _read_mono_samples(path: Path) -> tuple[list[float], float]:
    """Read a captured 16-bit WAV as a mono float mixdown."""
    with wave.open(str(path), "rb") as wav_file:
        num_channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = float(wav_file.getframerate())
        frames = wav_file.readframes(wav_file.getnframes())

    if sample_width != 2:
        pytest.fail(f"expected 16-bit capture output, got {8 * sample_width}-bit")

    interleaved = array.array("h", frames)
    mono = [
        sum(interleaved[base : base + num_channels]) / (num_channels * 32_768.0)
        for base in range(0, len(interleaved), num_channels)
    ]
    return mono, sample_rate


def _tone_power_ratio(
    samples: list[float],
    sample_rate: float,
    frequency: float,
) -> float:
    """Return the fraction of signal energy at ``frequency`` (Goertzel).

    A pure sine yields ~0.5 (its mirror bin holds the other half); silence
    and broadband noise yield ~0.
    """
    total = len(samples)
    windowed = [
        sample * (0.5 - 0.5 * math.cos(2.0 * math.pi * index / (total - 1)))
        for index, sample in enumerate(samples)
    ]

    omega = 2.0 * math.pi * frequency / sample_rate
    coeff = 2.0 * math.cos(omega)
    s_prev = s_prev2 = 0.0
    for sample in windowed:
        s_prev2, s_prev = s_prev, sample + coeff * s_prev - s_prev2
    bin_power = s_prev * s_prev + s_prev2 * s_prev2 - coeff * s_prev * s_prev2

    total_energy = sum(sample * sample for sample in windowed)
    if total_energy <= 0.0:
        return 0.0
    return bin_power / (total * total_energy)


def _quicktime_pid() -> int | None:
    """Return QuickTime Player's PID without launching the application."""
    result = subprocess.run(
        ["pgrep", "-x", _QUICKTIME_APP_NAME],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise RuntimeError(f"pgrep failed: {result.stderr.strip()}")
    return int(result.stdout.splitlines()[0])


def _wait_for_pid_exit(pid: int, *, timeout: float = 10.0) -> bool:
    """Return whether ``pid`` exited before ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def _play_tone_in_quicktime(path: Path) -> int:
    """Open and loop ``path`` in a newly launched QuickTime Player."""
    script = """
on run argv
    set toneFile to POSIX file (item 1 of argv)
    tell application "QuickTime Player"
        open toneFile
        set toneDocument to document 1
        set looping of toneDocument to true
        set muted of toneDocument to false
        set audio volume of toneDocument to 1.0
        set current time of toneDocument to 0.0
        play toneDocument
        repeat 100 times
            if playing of toneDocument then return
            delay 0.05
        end repeat
        error "QuickTime Player did not start playback"
    end tell
end run
"""
    result = subprocess.run(
        ["osascript", "-e", script, str(path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=15.0,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to start QuickTime Player playback: {result.stderr.strip()}"
        )

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        pid = _quicktime_pid()
        if pid is not None:
            return pid
        time.sleep(0.1)
    raise RuntimeError("QuickTime Player played the tone but no process was found")


def _quit_quicktime() -> bool:
    """Close test documents and quit QuickTime, forcing cleanup if needed.

    Returns true for a graceful application quit. The fallback only targets
    the PID that this test found, and exists to avoid leaking an application
    when an Apple event fails during test cleanup.
    """
    pid = _quicktime_pid()
    if pid is None:
        return True

    script = """
tell application "QuickTime Player"
    close every document saving no
    quit
end tell
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=15.0,
        )
    except subprocess.TimeoutExpired:
        result = None

    if _wait_for_pid_exit(pid):
        return result is not None and result.returncode == 0

    subprocess.run(["kill", "-TERM", str(pid)], check=False)
    if not _wait_for_pid_exit(pid, timeout=5.0):
        subprocess.run(["kill", "-KILL", str(pid)], check=False)
        if not _wait_for_pid_exit(pid, timeout=5.0):
            raise RuntimeError(f"could not clean up QuickTime Player PID {pid}")
    return False


def test_list_audio_processes_smoke() -> None:
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    from catap import list_audio_processes

    processes = list_audio_processes()
    assert isinstance(processes, list)

    for process in processes[:5]:
        assert isinstance(process.audio_object_id, int)
        assert process.audio_object_id > 0
        assert isinstance(process.pid, int)
        assert process.pid >= 0
        assert isinstance(process.name, str)
        assert process.name
        assert isinstance(process.is_outputting, bool)


def test_list_audio_devices_smoke() -> None:
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    from catap import list_audio_devices

    devices = list_audio_devices()
    assert isinstance(devices, list)

    for device in devices[:5]:
        assert isinstance(device.audio_object_id, int)
        assert device.audio_object_id > 0
        assert isinstance(device.uid, str)
        assert device.uid
        assert isinstance(device.name, str)
        assert device.name
        assert isinstance(device.streams, tuple)


def test_record_system_audio_smoke(tmp_path) -> None:
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    from catap import record_system_audio

    output_path = tmp_path / "integration-recording.wav"
    session = record_system_audio(
        output_path=output_path,
        max_pending_buffers=64,
    )
    session.record_for(0.2)

    assert output_path.exists()
    assert session.stream_format is not None
    assert session.duration_seconds >= 0.0

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnchannels() > 0
        assert wav_file.getframerate() > 0
        assert wav_file.getsampwidth() > 0
        assert wav_file.getnframes() == session.frames_recorded


def test_record_multitrack_smoke(tmp_path) -> None:
    """Two players recorded as separate sample-synchronized tracks."""
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    from catap import list_audio_processes, record_multitrack

    tone_a = tmp_path / "tone-a.wav"
    tone_b = tmp_path / "tone-b.wav"
    _write_tone_wav(tone_a, frequency=440.0, duration=6.0)
    _write_tone_wav(tone_b, frequency=880.0, duration=6.0)

    players = [
        subprocess.Popen(["afplay", str(tone_a)]),
        subprocess.Popen(["afplay", str(tone_b)]),
    ]
    try:
        player_pids = {player.pid for player in players}
        processes = []
        for _ in range(40):
            time.sleep(0.25)
            processes = [
                process
                for process in list_audio_processes()
                if process.pid in player_pids
            ]
            if len(processes) == 2:
                break
        if len(processes) != 2:
            pytest.fail("afplay processes never registered with Core Audio")

        output_dir = tmp_path / "multitrack"
        output_dir.mkdir()
        session = record_multitrack(processes, output_dir)
        session.record_for(0.5)
    finally:
        for player in players:
            player.terminate()
        for player in players:
            player.wait()

    assert session.track_count == 2
    assert len(session.stream_formats) == 2
    assert session.frames_recorded > 0

    wav_paths = sorted(output_dir.glob("*.wav"))
    assert len(wav_paths) == 2
    frame_counts = set()
    for wav_path in wav_paths:
        with wave.open(str(wav_path), "rb") as wav_file:
            assert wav_file.getnchannels() > 0
            assert wav_file.getframerate() > 0
            frame_counts.add(wav_file.getnframes())
    assert len(frame_counts) == 1, (
        f"multitrack outputs are not sample-locked: {frame_counts}"
    )


def test_record_multitrack_with_microphone_delivers_aligned_tracks(
    tmp_path,
) -> None:
    """A real microphone track stays aligned with a non-silent tap track."""
    if not RUN_TONE_INTEGRATION:
        pytest.skip(
            "set CATAP_RUN_TONE_INTEGRATION=1 to run the microphone multitrack test"
        )

    from catap import list_audio_processes, record_multitrack

    tone_path = tmp_path / "mic-multitrack-tone.wav"
    _write_tone_wav(tone_path, frequency=_TONE_FREQUENCY_HZ, duration=15.0)
    player = subprocess.Popen(["afplay", str(tone_path)])
    try:
        process = None
        for _ in range(40):
            time.sleep(0.1)
            process = next(
                (
                    candidate
                    for candidate in list_audio_processes()
                    if candidate.pid == player.pid
                ),
                None,
            )
            if process is not None:
                break
        if process is None:
            pytest.fail("afplay never registered with Core Audio")

        output_dir = tmp_path / "mic-multitrack"
        session = record_multitrack(
            [process],
            output_dir,
            microphone=True,
            max_pending_buffers=256,
        )
        session.record_for(1.5)
    finally:
        player.terminate()
        player.wait()

    assert session.track_count >= 2
    assert len(session.stream_formats) == session.track_count
    assert len(session.track_captured_only_silence) == session.track_count
    assert any(not silent for silent in session.track_captured_only_silence[:-1])
    assert session.track_captured_only_silence[-1] is False

    frame_counts: list[int] = []
    for output_path in session.output_paths:
        assert output_path is not None
        with wave.open(str(output_path), "rb") as wav_file:
            frame_counts.append(wav_file.getnframes())
    assert frame_counts == [session.frames_recorded] * session.track_count

    microphone_paths = session.output_paths[:-1]
    tap_path = session.output_paths[-1]
    assert all(path is not None for path in microphone_paths)
    assert tap_path is not None
    tap_samples, tap_rate = _read_mono_samples(tap_path)
    microphone_peaks: list[float] = []
    for microphone_path in microphone_paths:
        assert microphone_path is not None
        microphone_samples, microphone_rate = _read_mono_samples(microphone_path)
        assert microphone_rate == tap_rate
        microphone_peaks.append(
            max((abs(sample) for sample in microphone_samples), default=0.0)
        )
    assert max(microphone_peaks, default=0.0) > 0.0
    assert _tone_power_ratio(tap_samples, tap_rate, _TONE_FREQUENCY_HZ) > 0.1


def test_record_processes_combined_tap_smoke(tmp_path) -> None:
    """Two players mixed through one multi-process tap."""
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    from catap import list_audio_processes, record_processes

    tone = tmp_path / "tone.wav"
    _write_tone_wav(tone, frequency=440.0, duration=6.0)

    players = [
        subprocess.Popen(["afplay", str(tone)]),
        subprocess.Popen(["afplay", str(tone)]),
    ]
    try:
        player_pids = {player.pid for player in players}
        processes = []
        for _ in range(40):
            time.sleep(0.25)
            processes = [
                process
                for process in list_audio_processes()
                if process.pid in player_pids
            ]
            if len(processes) == 2:
                break
        if len(processes) != 2:
            pytest.fail("afplay processes never registered with Core Audio")

        output_path = tmp_path / "combined.wav"
        session = record_processes(processes, output_path)
        session.record_for(0.3)
    finally:
        for player in players:
            player.terminate()
        for player in players:
            player.wait()

    assert output_path.exists()
    assert len(session.tap_description.processes) == 2


def test_tap_watch_fires_on_tap_lifecycle() -> None:
    """A visible tap's creation shows up through the event layer."""
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    import threading

    from catap import TapDescription, create_process_tap, destroy_process_tap
    from catap.events import watch_audio_taps

    event_seen = threading.Event()
    watch = watch_audio_taps(lambda event: event_seen.set())
    try:
        description = TapDescription.stereo_global_tap_excluding([])
        description.name = "catap integration watch tap"
        description.is_private = False
        tap_id = create_process_tap(description)
        try:
            assert event_seen.wait(timeout=5.0), (
                "tap-list watch never fired after tap creation"
            )
        finally:
            destroy_process_tap(tap_id)
    finally:
        watch.close()
    assert watch.failures == []


def test_record_for_wakes_when_existing_tap_disappears(tmp_path) -> None:
    """A live external-tap failure wakes a fixed-duration owner promptly."""
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    import threading

    from catap import (
        RecordingSession,
        TapDescription,
        create_process_tap,
        destroy_process_tap,
    )

    description = TapDescription.stereo_global_tap_excluding([])
    description.name = "catap integration disappearing tap"
    tap_id = create_process_tap(description)
    tap_is_live = True
    output_path = tmp_path / "disappearing-tap.wav"
    start_returned = threading.Event()

    class _SignalingRecordingSession(RecordingSession):
        def start(self) -> None:
            super().start()
            start_returned.set()

    session = _SignalingRecordingSession.from_tap(tap_id, output_path)
    failures: list[BaseException] = []

    def record() -> None:
        try:
            session.record_for(6.0)
        except BaseException as exc:
            failures.append(exc)

    recording_thread = threading.Thread(target=record)
    try:
        recording_thread.start()
        assert start_returned.wait(timeout=3.0), (
            failures[0] if failures else "capture did not start"
        )
        assert session.is_recording

        destroyed_at = time.monotonic()
        destroy_process_tap(tap_id)
        tap_is_live = False
        recording_thread.join(timeout=7.0)
        wake_latency = time.monotonic() - destroyed_at

        assert not recording_thread.is_alive(), "record_for did not return"
        assert wake_latency < 3.0, (
            f"record_for took {wake_latency:.2f}s to react to tap destruction"
        )
        assert len(failures) == 1
        assert isinstance(failures[0], (OSError, RuntimeError))
        assert "disappeared" in str(failures[0]).lower()
        assert session.capture_failed
        assert not output_path.exists()
    finally:
        if tap_is_live:
            destroy_process_tap(tap_id)
        recording_thread.join(timeout=8.0)
        assert not recording_thread.is_alive(), "recording thread leaked"
        session.close()
        assert not session.needs_cleanup


def test_set_tap_description_retargets_live_tap() -> None:
    """Live tap retargeting round-trips through Core Audio.

    Requires System Audio Recording permission (like the tone gate); without
    it Core Audio refuses the modification.
    """
    if not RUN_TONE_INTEGRATION:
        pytest.skip(
            "set CATAP_RUN_TONE_INTEGRATION=1 to run permissioned integration tests"
        )

    from catap import (
        TapDescription,
        TapMuteBehavior,
        create_process_tap,
        destroy_process_tap,
        get_tap_description,
        set_tap_description,
    )

    description = TapDescription.stereo_global_tap_excluding([])
    description.name = "catap integration retarget tap"
    tap_id = create_process_tap(description)
    try:
        updated = get_tap_description(tap_id)
        updated.mute_behavior = TapMuteBehavior.MUTED_WHEN_TAPPED
        set_tap_description(tap_id, updated)
        assert (
            get_tap_description(tap_id).mute_behavior
            is TapMuteBehavior.MUTED_WHEN_TAPPED
        )
    finally:
        destroy_process_tap(tap_id)


def test_record_known_tone_delivers_audio(tmp_path) -> None:
    """Play a known tone aloud and require the capture to contain it."""
    if not RUN_TONE_INTEGRATION:
        pytest.skip(
            "set CATAP_RUN_TONE_INTEGRATION=1 to run the audible tone capture test"
        )

    from catap import record_system_audio

    tone_path = tmp_path / "tone.wav"
    _write_tone_wav(tone_path, frequency=_TONE_FREQUENCY_HZ, duration=4.0)
    output_path = tmp_path / "tone-capture.wav"

    player = subprocess.Popen(["afplay", str(tone_path)])
    try:
        # Give afplay time to open the output device and start rendering.
        time.sleep(1.0)
        session = record_system_audio(output_path=output_path)
        session.record_for(1.5)
    finally:
        player.terminate()
        player.wait()

    assert session.stream_format is not None
    sample_rate = session.stream_format.sample_rate
    assert session.frames_recorded >= sample_rate, (
        f"captured only {session.frames_recorded} frames at {sample_rate:.0f} Hz; "
        "expected at least one second of audio"
    )

    assert session.captured_only_silence is False, (
        "session.captured_only_silence should be false after a real capture"
    )

    samples, capture_rate = _read_mono_samples(output_path)
    peak = max((abs(sample) for sample in samples), default=0.0)
    assert peak > 0.0, (
        "capture delivered frames but every sample is zero. macOS zeroes tap "
        "audio when the recording process lacks system-audio permission; "
        "grant System Audio Recording to the app hosting this test run."
    )

    ratio = _tone_power_ratio(samples, capture_rate, _TONE_FREQUENCY_HZ)
    assert ratio > 0.1, (
        f"{_TONE_FREQUENCY_HZ:.0f} Hz tone holds only {ratio:.3f} of captured "
        "energy; expected a dominant tone. Is the test tone audible on the "
        "default output device?"
    )


def test_record_bundle_ids_restores_after_quicktime_relaunch(tmp_path) -> None:
    """A bundle-ID tap follows QuickTime Player across a real relaunch."""
    if not RUN_TONE_INTEGRATION:
        pytest.skip("set CATAP_RUN_TONE_INTEGRATION=1 to run the bundle restore test")

    from catap import bundle_id_taps_supported, record_bundle_ids

    if not bundle_id_taps_supported():
        pytest.skip("bundle-ID taps require macOS 26 or later")
    if _quicktime_pid() is not None:
        pytest.skip(
            "QuickTime Player is already running; close it so this test does "
            "not disturb existing documents"
        )

    tone_path = tmp_path / "quicktime-restore-tone.wav"
    _write_tone_wav(tone_path, frequency=_TONE_FREQUENCY_HZ, duration=4.0)
    output_path = tmp_path / "quicktime-restore-capture.wav"
    session = record_bundle_ids(
        [_QUICKTIME_BUNDLE_ID],
        output_path=output_path,
        restore=True,
    )

    windows: list[tuple[str, int, int]] = []
    try:
        session.start()
        tap_id = session.tap_id
        first_pid = _play_tone_in_quicktime(tone_path)
        time.sleep(0.75)
        first_start = session.frames_recorded
        time.sleep(1.5)
        windows.append(("before relaunch", first_start, session.frames_recorded))

        assert _quit_quicktime(), "QuickTime Player did not quit gracefully"
        assert session.is_recording
        assert session.tap_id == tap_id

        # Let Core Audio publish the target's departure before relaunching it.
        time.sleep(0.75)
        second_pid = _play_tone_in_quicktime(tone_path)
        assert second_pid != first_pid, "QuickTime Player did not actually relaunch"
        time.sleep(0.75)
        second_start = session.frames_recorded
        time.sleep(1.5)
        windows.append(("after relaunch", second_start, session.frames_recorded))

        assert session.is_recording
        assert session.tap_id == tap_id
        assert _quit_quicktime(), "relaunched QuickTime Player did not quit cleanly"
    finally:
        try:
            session.close()
        finally:
            # If an assertion or Apple event failed, never leave the app or its
            # test document behind. A forced fallback is acceptable here
            # because the test skipped unless QuickTime was initially absent.
            _quit_quicktime()

    assert len(windows) == 2
    assert session.captured_only_silence is False
    samples, capture_rate = _read_mono_samples(output_path)

    for phase, start, end in windows:
        assert end <= len(samples), (
            f"{phase} frame window {start}:{end} exceeds the "
            f"{len(samples)}-frame output"
        )
        phase_samples = samples[start:end]
        assert len(phase_samples) >= capture_rate, (
            f"captured only {len(phase_samples) / capture_rate:.2f}s {phase}"
        )

        peak = max((abs(sample) for sample in phase_samples), default=0.0)
        assert peak > 0.0, (
            f"capture {phase} was silent; grant System Audio Recording "
            "permission to the app hosting this test run"
        )

        ratio = _tone_power_ratio(
            phase_samples,
            capture_rate,
            _TONE_FREQUENCY_HZ,
        )
        assert ratio > 0.1, (
            f"{_TONE_FREQUENCY_HZ:.0f} Hz tone holds only {ratio:.3f} of "
            f"captured energy {phase}; bundle-ID restoration may have failed"
        )
