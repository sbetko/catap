#!/usr/bin/env python3
"""
Probe exactly which lifecycle step changes audible playback for a tapped process.

The script launches a helper subprocess that plays a continuous tone via
AVAudioEngine, then applies the low-level tap/recorder lifecycle in phases:

1. create_process_tap()
2. create aggregate device
3. create IO proc
4. AudioDeviceStart()
5. AudioDeviceStop()
6. destroy IO proc
7. destroy aggregate device
8. destroy tap

The helper also reports a couple of Core Audio process-level flags, but those
have not matched the audible behavior observed during probing. Use
`--interactive` and the user-heard results as the authoritative signal when you
need to pin down exactly when sound disappears or returns.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from catap import (
    TapDescription,
    TapMuteBehavior,
    create_process_tap,
    destroy_process_tap,
    list_audio_processes,
)
from catap._devtools.tone import TonePlayer
from catap.bindings._coreaudio import (
    get_property_bytes,
    kAudioObjectSystemObject,
)
from catap.recorder import (
    AudioBufferList,
    AudioDeviceIOProcType,
    AudioTimeStamp,
    _AudioDeviceCreateIOProcID,
    _AudioDeviceDestroyIOProcID,
    _AudioDeviceStart,
    _AudioDeviceStop,
    _create_aggregate_device_for_tap,
    _destroy_aggregate_device,
    _get_tap_uid,
)

# Experimental Core Audio property selectors used only by this probe.
_kAudioHardwarePropertyDefaultOutputDevice = int.from_bytes(b"dOut", "big")
_kAudioHardwarePropertyProcessIsAudible = int.from_bytes(b"pmut", "big")
_kAudioDevicePropertyProcessMute = int.from_bytes(b"appm", "big")
_kAudioObjectPropertyScopeOutput = int.from_bytes(b"outp", "big")


def _read_uint32_property(
    object_id: int,
    selector: int,
    scope: int = 0,
    element: int = 0,
) -> int:
    """Read a UInt32 Core Audio property for probe-only diagnostics."""
    data = get_property_bytes(object_id, selector, scope, element)
    if len(data) < 4:
        raise OSError(
            f"Property selector {selector:08x} on object {object_id} returned "
            f"{len(data)} bytes"
        )
    return int.from_bytes(data[:4], "little")


def _probe_current_process_is_audible() -> bool:
    """Return the probe helper's current-process audibility flag."""
    return (
        _read_uint32_property(
            kAudioObjectSystemObject,
            _kAudioHardwarePropertyProcessIsAudible,
        )
        != 0
    )


def _probe_current_process_output_is_muted() -> bool:
    """Return the probe helper's default-output process-mute flag."""
    default_output_device = _read_uint32_property(
        kAudioObjectSystemObject,
        _kAudioHardwarePropertyDefaultOutputDevice,
    )
    return (
        _read_uint32_property(
            default_output_device,
            _kAudioDevicePropertyProcessMute,
            _kAudioObjectPropertyScopeOutput,
        )
        != 0
    )


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload), flush=True)


@dataclass(frozen=True)
class HelperEvent:
    """Structured event emitted by the helper subprocess."""

    event: str
    audible: bool | None
    muted: bool | None
    monotonic_ns: int | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class PhaseReport:
    """Observed helper state after one recorder lifecycle phase."""

    name: str
    audible: bool | None
    muted: bool | None
    user_audible: bool | None
    changed: bool
    change_count: int


class HelperMonitor:
    """Collect JSON events from the helper subprocess."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process
        self._events: list[HelperEvent] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            payload = json.loads(line)
            event = HelperEvent(
                event=str(payload["event"]),
                audible=(
                    None
                    if payload.get("audible") is None
                    else bool(payload["audible"])
                ),
                muted=(
                    None if payload.get("muted") is None else bool(payload["muted"])
                ),
                monotonic_ns=(
                    None
                    if payload.get("monotonic_ns") is None
                    else int(payload["monotonic_ns"])
                ),
                payload=payload,
            )
            with self._lock:
                self._events.append(event)

    def events(self) -> list[HelperEvent]:
        with self._lock:
            return list(self._events)

    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def wait_for_ready(self, timeout_seconds: float) -> int:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for event in self.events():
                if event.event == "helper_ready":
                    pid = event.payload.get("pid")
                    if isinstance(pid, int):
                        return pid
            if self._process.poll() is not None:
                break
            time.sleep(0.01)

        stderr = self._read_stderr()
        raise TimeoutError(
            "Timed out waiting for helper_ready event"
            + (f"; helper stderr:\n{stderr}" if stderr else "")
        )

    def latest_audible(self) -> bool | None:
        latest: bool | None = None
        for event in self.events():
            if event.audible is not None:
                latest = event.audible
        return latest

    def latest_muted(self) -> bool | None:
        latest: bool | None = None
        for event in self.events():
            if event.muted is not None:
                latest = event.muted
        return latest

    def _read_stderr(self) -> str:
        if self._process.stderr is None or self._process.poll() is None:
            return ""
        try:
            return self._process.stderr.read().strip()
        except Exception:
            return ""


def _helper_main(args: argparse.Namespace) -> int:
    stop_requested = False

    def _request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    player = TonePlayer(duration_seconds=args.tone_seconds)
    player.start()

    last_audible = _probe_current_process_is_audible()
    last_muted = _probe_current_process_output_is_muted()
    _print_json(
        {
            "event": "helper_ready",
            "pid": os.getpid(),
            "audible": last_audible,
            "muted": last_muted,
            "monotonic_ns": time.monotonic_ns(),
        }
    )

    try:
        while not stop_requested:
            audible = _probe_current_process_is_audible()
            muted = _probe_current_process_output_is_muted()
            if audible != last_audible or muted != last_muted:
                last_audible = audible
                last_muted = muted
                _print_json(
                    {
                        "event": "state_changed",
                        "audible": audible,
                        "muted": muted,
                        "monotonic_ns": time.monotonic_ns(),
                    }
                )
            time.sleep(0.01)
    finally:
        player.stop()

    return 0


def _wait_for_audio_process(pid: int, timeout_seconds: float) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for process in list_audio_processes():
            if process.pid == pid and process.is_outputting:
                return process
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for helper pid {pid} to output audio")


def _observe_phase(
    monitor: HelperMonitor,
    name: str,
    *,
    settle_seconds: float,
    start_index: int,
    interactive: bool = False,
) -> PhaseReport:
    deadline = time.monotonic() + settle_seconds
    while time.monotonic() < deadline:
        if monitor._process.poll() is not None:
            break
        time.sleep(0.01)

    events = monitor.events()
    changes = [
        event
        for event in events[start_index:]
        if event.event == "state_changed"
    ]
    user_audible = _prompt_user_audible(name) if interactive else None
    return PhaseReport(
        name=name,
        audible=monitor.latest_audible(),
        muted=monitor.latest_muted(),
        user_audible=user_audible,
        changed=bool(changes),
        change_count=len(changes),
    )


def _format_bool(value: bool | None) -> str:
    """Format tri-state booleans for human-readable reports."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "n/a"


def _print_interactive_intro(
    out: TextIO,
    mute_behavior: TapMuteBehavior,
    settle_seconds: float,
) -> None:
    """Print instructions for the manual listening flow."""
    print(f"Mute behavior: {mute_behavior.name}", file=out)
    print("", file=out)
    print("Interactive listening mode", file=out)
    print(
        "The helper tone should start playing now. At each phase, the script "
        "will pause, apply one lifecycle step, wait briefly, and then ask "
        "whether the tone is audible on your speakers.",
        file=out,
    )
    print(
        f"Settle time after each phase: {settle_seconds:.2f}s",
        file=out,
    )
    print("Answer with `y`, `n`, or `q` to abort.", file=out)


def _prompt_before_phase(name: str, description: str) -> None:
    """Pause before a lifecycle step in interactive mode."""
    print("", file=sys.stdout)
    print(f"Next phase: {name}", file=sys.stdout)
    print(description, file=sys.stdout)
    input("Press Enter to apply this phase...")


def _prompt_user_audible(name: str) -> bool:
    """Ask the user whether the helper tone is audible after a phase."""
    while True:
        answer = input(
            f"After {name}, is the tone audible on your speakers? [y/n/q] "
        ).strip().casefold()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        if answer in {"q", "quit"}:
            raise KeyboardInterrupt("Probe aborted by user")
        print("Please answer `y`, `n`, or `q`.", file=sys.stdout)


def _report_table(
    out: TextIO,
    mute_behavior: TapMuteBehavior,
    phases: list[PhaseReport],
) -> None:
    print(f"Mute behavior: {mute_behavior.name}", file=out)
    print("", file=out)
    print("Phase                          Muted  Audible  User  Changed", file=out)
    print("-----------------------------  -----  -------  ----  -------", file=out)
    for phase in phases:
        muted = _format_bool(phase.muted)
        audible = _format_bool(phase.audible)
        user_audible = _format_bool(phase.user_audible)
        changed = _format_bool(phase.changed)
        print(
            f"{phase.name:29}  {muted:5}  {audible:7}  {user_audible:4}  {changed}",
            file=out,
        )

    first_muted = next((phase.name for phase in phases if phase.muted is True), None)
    first_unmuted = next(
        (
            phase.name
            for phase in phases
            if (
                first_muted is not None
                and phase.name != first_muted
                and phase.muted is False
            )
        ),
        None,
    )
    first_user_muted_index = next(
        (
            index
            for index, phase in enumerate(phases)
            if phase.user_audible is False
        ),
        None,
    )
    first_user_muted = (
        phases[first_user_muted_index].name
        if first_user_muted_index is not None
        else None
    )
    first_user_unmuted = (
        next(
            (
                phase.name
                for phase in phases[first_user_muted_index + 1 :]
                if phase.user_audible is True
            ),
            None,
        )
        if first_user_muted_index is not None
        else None
    )

    print("", file=out)
    print(f"First muted phase: {first_muted or 'not observed'}", file=out)
    print(f"First unmuted-again phase: {first_unmuted or 'not observed'}", file=out)
    if any(phase.user_audible is not None for phase in phases):
        print(
            f"First user-heard mute phase: {first_user_muted or 'not observed'}",
            file=out,
        )
        print(
            f"First user-heard unmute phase: "
            f"{first_user_unmuted or 'not observed'}",
            file=out,
        )


def _no_op_io_proc(
    _device: int,
    _now: ctypes.POINTER(AudioTimeStamp),
    _input_data: ctypes.POINTER(AudioBufferList),
    _input_time: ctypes.POINTER(AudioTimeStamp),
    _output_data: ctypes.POINTER(AudioBufferList),
    _output_time: ctypes.POINTER(AudioTimeStamp),
    _client_data: ctypes.c_void_p,
) -> int:
    return 0


def _controller_main(args: argparse.Namespace) -> int:
    mute_behavior = TapMuteBehavior[args.mute_behavior]
    settle_seconds = args.settle_ms / 1000.0
    interactive = bool(args.interactive)
    script_path = str(Path(__file__).resolve())

    if interactive and not sys.stdin.isatty():
        raise RuntimeError("--interactive requires a terminal (TTY) on stdin")

    helper = subprocess.Popen(
        [
            sys.executable,
            "-u",
            script_path,
            "--helper",
            "--tone-seconds",
            str(args.tone_seconds),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    monitor = HelperMonitor(helper)

    tap_id: int | None = None
    aggregate_device_id: int | None = None
    io_proc_id: ctypes.c_void_p | None = None
    callback = AudioDeviceIOProcType(_no_op_io_proc)
    started = False

    try:
        helper_pid = monitor.wait_for_ready(timeout_seconds=5.0)
        process = _wait_for_audio_process(helper_pid, timeout_seconds=5.0)

        if interactive:
            _print_interactive_intro(sys.stdout, mute_behavior, settle_seconds)

        baseline_index = monitor.event_count()
        baseline = _observe_phase(
            monitor,
            "baseline",
            settle_seconds=settle_seconds,
            start_index=baseline_index,
            interactive=interactive,
        )

        description = TapDescription.stereo_mixdown_of_processes(
            [process.audio_object_id]
        )
        description.name = f"catap mute timing probe ({mute_behavior.name})"
        description.is_private = True
        description.mute_behavior = mute_behavior

        phases = [baseline]

        if interactive:
            _prompt_before_phase(
                "create_process_tap",
                "Creates the tap object, but does not yet attach an aggregate "
                "device or start reading from it.",
            )
        phase_index = monitor.event_count()
        tap_id = create_process_tap(description)
        phases.append(
            _observe_phase(
                monitor,
                "create_process_tap",
                settle_seconds=settle_seconds,
                start_index=phase_index,
                interactive=interactive,
            )
        )

        if interactive:
            _prompt_before_phase(
                "create_aggregate_device",
                "Creates an aggregate device that contains the tap, but does "
                "not register or start an IO proc yet.",
            )
        phase_index = monitor.event_count()
        aggregate_device_id = _create_aggregate_device_for_tap(
            _get_tap_uid(tap_id),
            "catap mute timing probe aggregate",
        )
        phases.append(
            _observe_phase(
                monitor,
                "create_aggregate_device",
                settle_seconds=settle_seconds,
                start_index=phase_index,
                interactive=interactive,
            )
        )

        if interactive:
            _prompt_before_phase(
                "create_io_proc",
                "Registers an IO proc on the aggregate device, but does not "
                "start the device yet.",
            )
        phase_index = monitor.event_count()
        created_io_proc = ctypes.c_void_p()
        status = _AudioDeviceCreateIOProcID(
            aggregate_device_id,
            callback,
            None,
            ctypes.byref(created_io_proc),
        )
        if status != 0:
            raise OSError(f"Failed to create IO proc: status {status}")
        io_proc_id = created_io_proc
        phases.append(
            _observe_phase(
                monitor,
                "create_io_proc",
                settle_seconds=settle_seconds,
                start_index=phase_index,
                interactive=interactive,
            )
        )

        if interactive:
            _prompt_before_phase(
                "audio_device_start",
                "Starts the aggregate device so the IO proc begins actively "
                "reading from the tap.",
            )
        phase_index = monitor.event_count()
        status = _AudioDeviceStart(aggregate_device_id, io_proc_id)
        if status != 0:
            raise OSError(f"Failed to start audio device: status {status}")
        started = True
        phases.append(
            _observe_phase(
                monitor,
                "audio_device_start",
                settle_seconds=settle_seconds,
                start_index=phase_index,
                interactive=interactive,
            )
        )

        if interactive:
            _prompt_before_phase(
                "audio_device_stop",
                "Stops the aggregate device so the IO proc should stop reading "
                "from the tap.",
            )
        phase_index = monitor.event_count()
        status = _AudioDeviceStop(aggregate_device_id, io_proc_id)
        if status != 0:
            raise OSError(f"Failed to stop audio device: status {status}")
        started = False
        phases.append(
            _observe_phase(
                monitor,
                "audio_device_stop",
                settle_seconds=settle_seconds,
                start_index=phase_index,
                interactive=interactive,
            )
        )

        if interactive:
            _prompt_before_phase(
                "destroy_io_proc",
                "Destroys the registered IO proc after the device has already "
                "been stopped.",
            )
        phase_index = monitor.event_count()
        status = _AudioDeviceDestroyIOProcID(aggregate_device_id, io_proc_id)
        if status != 0:
            raise OSError(f"Failed to destroy IO proc: status {status}")
        io_proc_id = None
        phases.append(
            _observe_phase(
                monitor,
                "destroy_io_proc",
                settle_seconds=settle_seconds,
                start_index=phase_index,
                interactive=interactive,
            )
        )

        if interactive:
            _prompt_before_phase(
                "destroy_aggregate_device",
                "Destroys the aggregate device while leaving the tap itself in "
                "place.",
            )
        phase_index = monitor.event_count()
        _destroy_aggregate_device(aggregate_device_id)
        aggregate_device_id = None
        phases.append(
            _observe_phase(
                monitor,
                "destroy_aggregate_device",
                settle_seconds=settle_seconds,
                start_index=phase_index,
                interactive=interactive,
            )
        )

        if interactive:
            _prompt_before_phase(
                "destroy_process_tap",
                "Destroys the tap object itself. The helper tone will still "
                "continue until the script exits.",
            )
        phase_index = monitor.event_count()
        destroy_process_tap(tap_id)
        tap_id = None
        phases.append(
            _observe_phase(
                monitor,
                "destroy_process_tap",
                settle_seconds=settle_seconds,
                start_index=phase_index,
                interactive=interactive,
            )
        )

        if args.json:
            json.dump(
                {
                    "mute_behavior": mute_behavior.name,
                    "phases": [
                        {
                            "name": phase.name,
                            "audible": phase.audible,
                            "muted": phase.muted,
                            "user_audible": phase.user_audible,
                            "changed": phase.changed,
                            "change_count": phase.change_count,
                        }
                        for phase in phases
                    ],
                },
                sys.stdout,
                indent=2,
            )
            print()
        else:
            _report_table(sys.stdout, mute_behavior, phases)
        return 0
    finally:
        cleanup_errors: list[str] = []

        if started and aggregate_device_id is not None and io_proc_id is not None:
            stop_status = _AudioDeviceStop(aggregate_device_id, io_proc_id)
            if stop_status != 0:
                cleanup_errors.append(
                    f"AudioDeviceStop cleanup failed with status {stop_status}"
                )
        if aggregate_device_id is not None and io_proc_id is not None:
            destroy_status = _AudioDeviceDestroyIOProcID(
                aggregate_device_id,
                io_proc_id,
            )
            if destroy_status != 0:
                cleanup_errors.append(
                    f"AudioDeviceDestroyIOProcID cleanup failed with status "
                    f"{destroy_status}"
                )
        if aggregate_device_id is not None:
            try:
                _destroy_aggregate_device(aggregate_device_id)
            except OSError as exc:
                cleanup_errors.append(str(exc))
        if tap_id is not None:
            try:
                destroy_process_tap(tap_id)
            except OSError as exc:
                cleanup_errors.append(str(exc))

        if helper.poll() is None:
            helper.terminate()
            try:
                helper.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                helper.kill()
                helper.wait(timeout=5.0)

        if cleanup_errors:
            print(
                "cleanup warnings:\n" + "\n".join(cleanup_errors),
                file=sys.stderr,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--helper",
        action="store_true",
        help="Run the audio-producing helper subprocess.",
    )
    parser.add_argument(
        "--mute-behavior",
        choices=[behavior.name for behavior in TapMuteBehavior],
        default=TapMuteBehavior.MUTED_WHEN_TAPPED.name,
        help="Tap mute behavior to probe.",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=400,
        help="Milliseconds to wait after each phase before sampling.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable phase results.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Pause at each phase and ask whether the tone is audible.",
    )
    parser.add_argument(
        "--tone-seconds",
        type=float,
        default=120.0,
        help="Length of the helper tone buffer before it naturally ends.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.helper:
        return _helper_main(args)
    return _controller_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
