"""Command-line interface for catap."""

from __future__ import annotations

import argparse
import contextlib
import math
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from catap import (
    AmbiguousAudioDeviceError,
    AmbiguousAudioProcessError,
    AudioDeviceStream,
    AudioProcess,
    RecordingSession,
    TapDescription,
    TapMuteBehavior,
    UnsupportedTapFormatError,
    __version__,
    events as catap_events,
    find_audio_device_by_name,
    find_audio_device_by_uid,
    find_process_by_name,
    find_tap_by_uid,
    list_audio_devices,
    list_audio_processes,
    list_audio_taps,
    record_tap,
)
from catap.session import (
    build_bundle_tap_description,
    build_device_tap_description,
    build_processes_tap_description,
    build_system_tap_description,
    record_multitrack,
)

_PERMISSION_HINT = [
    "This may be a permissions issue. Try:",
    "  1. Check System Settings > Privacy & Security > Screen & System Audio Recording",
    "  2. Ensure your terminal app has permission",
]
_OUTPUT_HINT = [
    "This looks like an output file problem. Try:",
    "  1. Ensure the destination directory exists",
    "  2. Ensure you can write to the output path",
]
_SILENCE_HINT = [
    "Warning: the capture contained only silence.",
    "If audio was playing, macOS may have zeroed the capture because this",
    "terminal lacks permission. Check System Settings > Privacy & Security >",
    "Screen & System Audio Recording, then restart the terminal app.",
]
_CAPTURE_WAIT_POLL_INTERVAL_SECONDS = 0.1


class _DisplayProcess(Protocol):
    audio_object_id: int
    name: str
    pid: int
    bundle_id: str | None
    is_outputting: bool


class _CaptureFailureWaiter(Protocol):
    def wait_for_capture_failure(self, timeout: float | None = None) -> bool: ...


def _wait_for_stop_or_capture_failure(
    capture: _CaptureFailureWaiter,
    stop_event: threading.Event,
    duration: float | None,
) -> None:
    """Wake for Ctrl+C, the duration deadline, or a recorder failure."""
    deadline = None if duration is None else time.monotonic() + duration
    while not stop_event.is_set():
        timeout = _CAPTURE_WAIT_POLL_INTERVAL_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            timeout = min(timeout, remaining)
        if capture.wait_for_capture_failure(timeout):
            return


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid number") from exc

    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")

    return parsed


def _output_path(value: str) -> str:
    if not value:
        raise argparse.ArgumentTypeError("output path must not be empty")
    return value


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer") from exc

    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")

    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer") from exc

    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")

    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catap",
        description="catap - Core Audio process-tap recording for macOS.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_apps_parser = subparsers.add_parser(
        "list-apps",
        help="List applications producing audio",
    )
    list_apps_parser.add_argument(
        "--all",
        "-a",
        dest="show_all",
        action="store_true",
        help="Show all audio processes, including idle processes",
    )

    subparsers.add_parser(
        "list-taps",
        help="List Core Audio taps visible to this process",
    )

    subparsers.add_parser(
        "list-devices",
        help="List Core Audio devices and their streams",
    )

    watch_parser = subparsers.add_parser(
        "watch",
        help="Stream process, tap, and device change events until Ctrl+C",
    )
    watch_parser.add_argument(
        "--processes",
        action="store_true",
        help="Watch the audio process list",
    )
    watch_parser.add_argument(
        "--taps",
        action="store_true",
        help="Watch the visible tap list",
    )
    watch_parser.add_argument(
        "--devices",
        action="store_true",
        help="Watch the device list",
    )
    watch_parser.add_argument(
        "--default-output",
        dest="default_output",
        action="store_true",
        help="Watch the default output device",
    )

    multitrack_parser = subparsers.add_parser(
        "record-multitrack",
        help="Record each app as its own synchronized track",
    )
    multitrack_parser.add_argument(
        "app_names",
        nargs="*",
        metavar="APP_NAME",
        help=(
            "Application names to record; each becomes its own track. "
            "Required unless --pid or --audio-object-id selects targets."
        ),
    )
    multitrack_parser.add_argument(
        "--pid",
        action="append",
        type=_positive_int,
        default=[],
        help="Record the audio process with this OS process ID (repeatable)",
    )
    multitrack_parser.add_argument(
        "--audio-object-id",
        "--audio-id",
        dest="audio_object_id",
        action="append",
        type=_positive_int,
        default=[],
        help=("Record the process with this Core Audio process object ID (repeatable)"),
    )
    multitrack_parser.add_argument(
        "--output-dir",
        "-o",
        dest="output_dir",
        type=_output_path,
        default="multitrack",
        help=(
            "Directory for one WAV per track, named after each source "
            "(default: multitrack)"
        ),
    )
    multitrack_parser.add_argument(
        "--duration",
        "-d",
        type=_positive_float,
        default=None,
        help="Recording duration in seconds (default: until Ctrl+C)",
    )
    multitrack_parser.add_argument(
        "--with-mic",
        dest="with_mic",
        nargs="?",
        const=True,
        default=None,
        metavar="DEVICE",
        help=(
            "Also record an input device as its own track: the default "
            "input device, or a named device (experimental; requires "
            "microphone permission)"
        ),
    )
    multitrack_parser.add_argument(
        "--mute",
        action="store_true",
        help="Mute the tapped apps while recording",
    )
    multitrack_parser.add_argument(
        "--mute-when-tapped",
        dest="mute_when_tapped",
        action="store_true",
        help="Mute the tapped apps only while the tap is being read",
    )
    multitrack_parser.add_argument(
        "--mono",
        action="store_true",
        help="Mix each app track down to mono instead of stereo",
    )

    record_parser = subparsers.add_parser(
        "record",
        help="Record processes, a device, a tap, or a global mix",
    )
    record_parser.add_argument(
        "app_names",
        nargs="*",
        metavar="APP_NAME",
        help=(
            "Application names to record (partial match, case-insensitive). "
            "Several names are mixed through one tap. Required unless "
            "--system, --pid, --audio-object-id, --tap, --device, or "
            "--bundle-id selects the target."
        ),
    )
    record_parser.add_argument(
        "--system",
        action="store_true",
        help="Record a global process-output mix",
    )
    record_parser.add_argument(
        "--tap",
        metavar="UID",
        help="Record an existing visible tap by its UID",
    )
    record_parser.add_argument(
        "--device",
        metavar="NAME_OR_UID",
        help="Record audio routed to this output device",
    )
    record_parser.add_argument(
        "--stream",
        type=_non_negative_int,
        default=None,
        help="Output stream index on --device (default: first output stream)",
    )
    record_parser.add_argument(
        "--bundle-id",
        dest="bundle_ids",
        action="append",
        default=[],
        metavar="BUNDLE_ID",
        help=(
            "Record applications by bundle ID (repeatable; macOS 26 or "
            "later). Tapped apps rejoin the capture when they restart "
            "unless --no-restore is set."
        ),
    )
    record_parser.add_argument(
        "--no-restore",
        dest="no_restore",
        action="store_true",
        help="Do not re-attach bundle-ID tapped apps when they restart",
    )
    record_parser.add_argument(
        "--output",
        "-o",
        type=_output_path,
        default="output.wav",
        help="Output file path (default: output.wav)",
    )
    record_parser.add_argument(
        "--duration",
        "-d",
        type=_positive_float,
        default=None,
        help="Recording duration in seconds (default: until Ctrl+C)",
    )
    record_parser.add_argument(
        "--mute",
        action="store_true",
        help="Mute the tapped apps while recording",
    )
    record_parser.add_argument(
        "--mute-when-tapped",
        dest="mute_when_tapped",
        action="store_true",
        help="Mute the tapped apps only while the tap is being read",
    )
    record_parser.add_argument(
        "--mono",
        action="store_true",
        help="Mix the capture down to mono instead of stereo",
    )
    record_parser.add_argument(
        "--visible",
        action="store_true",
        help="Create the tap visible to other audio clients, not private",
    )
    record_parser.add_argument(
        "--pid",
        action="append",
        type=_positive_int,
        default=[],
        help="Record the audio process with this OS process ID (repeatable)",
    )
    record_parser.add_argument(
        "--audio-object-id",
        "--audio-id",
        dest="audio_object_id",
        action="append",
        type=_positive_int,
        default=[],
        help=("Record the process with this Core Audio process object ID (repeatable)"),
    )
    record_parser.add_argument(
        "--exclude",
        "-e",
        action="append",
        default=[],
        help=("App names to exclude from global or device recording (repeatable)"),
    )
    record_parser.add_argument(
        "--exclude-pid",
        action="append",
        type=_positive_int,
        default=[],
        help="OS process IDs to exclude from global recording (repeatable)",
    )
    record_parser.add_argument(
        "--exclude-audio-object-id",
        "--exclude-audio-id",
        dest="exclude_audio_object_id",
        action="append",
        type=_positive_int,
        default=[],
        help=(
            "Core Audio process object IDs to exclude from global recording "
            "(repeatable)"
        ),
    )

    return parser


def _list_apps(show_all: bool) -> int:
    try:
        processes = list_audio_processes()
    except Exception as exc:
        print(f"Error listing audio processes: {exc}", file=sys.stderr)
        return 1

    if not show_all:
        processes = [process for process in processes if process.is_outputting]

    if not processes:
        if show_all:
            print("No audio processes found.", flush=True)
        else:
            print("No applications currently outputting audio.", flush=True)
            print("Use --all to see all registered audio processes.", flush=True)
        return 0

    print(
        f"{'Status':<2} {'Name':<30} {'Bundle ID':<40} {'Audio ID':<10} {'PID':<8}",
        flush=True,
    )
    print("-" * 92, flush=True)

    for process in processes:
        bundle = process.bundle_id or "N/A"
        status = "♪" if process.is_outputting else " "
        print(
            f"{status:<2} {process.name:<30} {bundle:<40} "
            f"{process.audio_object_id:<10} {process.pid:<8}",
            flush=True,
        )

    return 0


def _list_taps() -> int:
    try:
        taps = list_audio_taps()
    except Exception as exc:
        print(f"Error listing audio taps: {exc}", file=sys.stderr)
        return 1

    if not taps:
        print("No visible audio taps.", flush=True)
        return 0

    print(
        f"{'Name':<30} {'UID':<38} {'Private':<8} {'Mute':<14} {'Target':<24}",
        flush=True,
    )
    print("-" * 116, flush=True)

    for tap in taps:
        description = tap.description
        bundle_ids = description.bundle_ids
        if tap.device_uid is not None:
            stream = tap.stream if tap.stream is not None else 0
            target = f"{tap.device_uid}[{stream}]"
        elif bundle_ids:
            count = len(bundle_ids)
            label = "bundle ID" if count == 1 else "bundle IDs"
            target = (
                f"global -{count} {label}"
                if description.is_exclusive
                else f"{count} {label}"
            )
        elif description.is_exclusive:
            excluded = len(description.processes)
            target = "global" if not excluded else f"global -{excluded}"
        else:
            target = f"{len(description.processes)} process(es)"
        print(
            f"{tap.name:<30} {tap.uid:<38} "
            f"{'yes' if tap.is_private else 'no':<8} "
            f"{description.mute_behavior.name.lower():<14} {target:<24}",
            flush=True,
        )

    return 0


def _list_devices() -> int:
    try:
        devices = list_audio_devices()
    except Exception as exc:
        print(f"Error listing audio devices: {exc}", file=sys.stderr)
        return 1

    if not devices:
        print("No audio devices found.", flush=True)
        return 0

    for device in devices:
        markers = []
        if device.is_default_output:
            markers.append("default output")
        if device.is_default_input:
            markers.append("default input")
        if device.is_default_system_output:
            markers.append("system output")
        suffix = f" ({', '.join(markers)})" if markers else ""
        print(f"{device.name}{suffix}", flush=True)
        print(f"  UID: {device.uid}", flush=True)
        for stream in device.streams:
            sample_type = "float" if stream.is_float else "int"
            print(
                f"  [{stream.direction} {stream.stream_index}] "
                f"{stream.num_channels}ch {stream.sample_rate:.0f} Hz "
                f"{sample_type}{stream.bits_per_channel}",
                flush=True,
            )

    return 0


def _watch(
    *,
    watch_processes: bool,
    watch_taps: bool,
    watch_devices: bool,
    watch_default_output: bool,
) -> int:
    if not any((watch_processes, watch_taps, watch_devices, watch_default_output)):
        watch_processes = watch_taps = watch_devices = watch_default_output = True

    stop_event = threading.Event()

    def signal_handler(_sig: int, _frame: object) -> None:
        stop_event.set()

    def _emit(label: str, detail_provider: Callable[[], str]) -> Callable:
        def callback(event: catap_events.AudioPropertyEvent) -> None:
            del event
            try:
                detail = detail_provider()
            except Exception:
                detail = ""
            timestamp = time.strftime("%H:%M:%S")
            suffix = f" — {detail}" if detail else ""
            print(f"[{timestamp}] {label} changed{suffix}", flush=True)

        return callback

    def _outputting_count() -> str:
        outputting = sum(
            1 for process in list_audio_processes() if process.is_outputting
        )
        return f"{outputting} outputting"

    def _tap_count() -> str:
        return f"{len(list_audio_taps())} visible tap(s)"

    def _device_count() -> str:
        return f"{len(list_audio_devices())} device(s)"

    def _default_output_name() -> str:
        for device in list_audio_devices():
            if device.is_default_output:
                return device.name
        return "unknown"

    watches: list[catap_events.AudioPropertyWatch] = []
    original_handler = signal.signal(signal.SIGINT, signal_handler)
    try:
        try:
            if watch_processes:
                watches.append(
                    catap_events.watch_audio_processes(
                        _emit("processes", _outputting_count)
                    )
                )
            if watch_taps:
                watches.append(catap_events.watch_audio_taps(_emit("taps", _tap_count)))
            if watch_devices:
                watches.append(
                    catap_events.watch_audio_devices(_emit("devices", _device_count))
                )
            if watch_default_output:
                watches.append(
                    catap_events.watch_default_output_device(
                        _emit("default output", _default_output_name)
                    )
                )
        except Exception as exc:
            print(f"Error starting watch: {exc}", file=sys.stderr)
            return 1

        print("Watching for Core Audio changes... (Ctrl+C to stop)", flush=True)
        stop_event.wait()
        print("\nStopping watch...", flush=True)
        return 0
    finally:
        signal.signal(signal.SIGINT, original_handler)
        for watch in watches:
            with contextlib.suppress(Exception):
                watch.close()


def _describe_process(process: _DisplayProcess) -> str:
    bundle = process.bundle_id or "N/A"
    status = "outputting" if process.is_outputting else "idle"
    return (
        f"{process.name} (PID: {process.pid}, "
        f"Audio ID: {process.audio_object_id}, Bundle ID: {bundle}, {status})"
    )


def _print_ambiguous_process_error(query: str, exc: AmbiguousAudioProcessError) -> None:
    message_lines = [f"Multiple audio processes match '{query}':"]
    message_lines.extend(
        f"  - {_describe_process(process)}" for process in exc.matches[:10]
    )
    if len(exc.matches) > 10:
        message_lines.append(f"  ... and {len(exc.matches) - 10} more")
    print("\n".join(message_lines), file=sys.stderr)


def _print_ambiguous_selector_error(
    selector: str, matches: Sequence[_DisplayProcess]
) -> None:
    message_lines = [f"Multiple audio processes match {selector}:"]
    message_lines.extend(
        f"  - {_describe_process(process)}" for process in matches[:10]
    )
    if len(matches) > 10:
        message_lines.append(f"  ... and {len(matches) - 10} more")
    print("\n".join(message_lines), file=sys.stderr)


def _print_missing_process_error(
    message: str,
    all_processes: Sequence[_DisplayProcess],
) -> None:
    message_lines = [message]
    if all_processes:
        message_lines.append("")
        message_lines.append("Available audio processes:")
        message_lines.extend(
            f"  - {_describe_process(listed_process)}"
            for listed_process in all_processes[:10]
        )
        if len(all_processes) > 10:
            message_lines.append(f"  ... and {len(all_processes) - 10} more")
    print("\n".join(message_lines), file=sys.stderr)


def _lookup_process_by_selector(
    selector: str,
    predicate: Callable[[AudioProcess], bool],
) -> tuple[AudioProcess | None, Sequence[AudioProcess] | None]:
    try:
        all_processes = list_audio_processes()
    except OSError as exc:
        print(f"Error looking up audio processes: {exc}", file=sys.stderr)
        return None, None

    matches = [process for process in all_processes if predicate(process)]
    if len(matches) > 1:
        _print_ambiguous_selector_error(selector, matches)
        return None, None
    if matches:
        return matches[0], all_processes
    return None, all_processes


def _resolve_process_by_name(app_name: str) -> AudioProcess | None:
    """Resolve one app name, printing lookup failures like the record flow."""
    try:
        process = find_process_by_name(app_name)
    except AmbiguousAudioProcessError as exc:
        _print_ambiguous_process_error(app_name, exc)
        return None
    except OSError as exc:
        print(f"Error looking up audio processes: {exc}", file=sys.stderr)
        return None

    if not process:
        try:
            all_processes = list_audio_processes()
        except OSError as exc:
            print(f"No audio process found matching '{app_name}'", file=sys.stderr)
            print("", file=sys.stderr)
            print(f"Error listing audio processes: {exc}", file=sys.stderr)
            return None

        _print_missing_process_error(
            f"No audio process found matching '{app_name}'",
            all_processes,
        )
        return None

    return process


def _append_unique_process(
    processes: list[AudioProcess],
    process: AudioProcess,
) -> None:
    if any(
        existing.audio_object_id == process.audio_object_id for existing in processes
    ):
        return
    processes.append(process)


def _resolve_record_processes(
    app_names: Sequence[str],
    pids: Sequence[int],
    audio_object_ids: Sequence[int],
) -> list[AudioProcess] | None:
    """Resolve every process selector, or report the failure and return None."""
    processes: list[AudioProcess] = []

    for app_name in app_names:
        process = _resolve_process_by_name(app_name)
        if process is None:
            return None
        _append_unique_process(processes, process)

    for pid in pids:
        process, all_processes = _lookup_process_by_selector(
            f"PID {pid}",
            lambda listed_process, pid=pid: listed_process.pid == pid,
        )
        if all_processes is None:
            return None
        if process is None:
            _print_missing_process_error(
                f"No audio process found with PID {pid}",
                all_processes,
            )
            return None
        _append_unique_process(processes, process)

    for audio_object_id in audio_object_ids:
        process, all_processes = _lookup_process_by_selector(
            f"Audio ID {audio_object_id}",
            lambda listed_process, audio_id=audio_object_id: (
                listed_process.audio_object_id == audio_id
            ),
        )
        if all_processes is None:
            return None
        if process is None:
            _print_missing_process_error(
                f"No audio process found with Audio ID {audio_object_id}",
                all_processes,
            )
            return None
        _append_unique_process(processes, process)

    return processes


def _mute_behavior_from_args(args: argparse.Namespace) -> TapMuteBehavior:
    if args.mute:
        return TapMuteBehavior.MUTED
    if args.mute_when_tapped:
        return TapMuteBehavior.MUTED_WHEN_TAPPED
    return TapMuteBehavior.UNMUTED


def _print_mute_notice(mute_behavior: TapMuteBehavior) -> None:
    if mute_behavior is TapMuteBehavior.MUTED:
        print("Muting app audio during recording", flush=True)
    elif mute_behavior is TapMuteBehavior.MUTED_WHEN_TAPPED:
        print("Muting app audio while the tap is being read", flush=True)


def _build_processes_tap(
    processes: Sequence[AudioProcess],
    *,
    mute_behavior: TapMuteBehavior,
    mono: bool,
    visible: bool,
) -> TapDescription:
    for process in processes:
        print(
            f"Recording from: {process.name} "
            f"(PID: {process.pid}, Audio ID: {process.audio_object_id})",
            flush=True,
        )
    _print_mute_notice(mute_behavior)

    return build_processes_tap_description(
        processes,
        mute=mute_behavior,
        mono=mono,
        visible=visible,
    )


def _resolve_excluded_processes(
    exclude: Sequence[str],
    exclude_pids: Sequence[int],
    exclude_audio_object_ids: Sequence[int],
) -> list[AudioProcess] | None:
    """Resolve exclusions, warning on misses and failing on ambiguity."""
    excluded_processes: list[AudioProcess] = []
    for excluded_app_name in exclude:
        try:
            process = find_process_by_name(excluded_app_name)
        except AmbiguousAudioProcessError as exc:
            _print_ambiguous_process_error(excluded_app_name, exc)
            return None
        except OSError as exc:
            print(f"Error looking up audio processes: {exc}", file=sys.stderr)
            return None

        if process:
            _append_excluded_process(excluded_processes, process)
        else:
            print(
                f"Warning: No audio process found matching '{excluded_app_name}'",
                file=sys.stderr,
            )

    for excluded_pid in exclude_pids:
        process, all_processes = _lookup_process_by_selector(
            f"PID {excluded_pid}",
            lambda listed_process, pid=excluded_pid: listed_process.pid == pid,
        )
        if all_processes is None:
            return None
        if process is None:
            print(
                f"Warning: No audio process found with PID {excluded_pid}",
                file=sys.stderr,
            )
            continue
        _append_excluded_process(excluded_processes, process)

    for excluded_audio_object_id in exclude_audio_object_ids:
        process, all_processes = _lookup_process_by_selector(
            f"Audio ID {excluded_audio_object_id}",
            lambda listed_process, audio_id=excluded_audio_object_id: (
                listed_process.audio_object_id == audio_id
            ),
        )
        if all_processes is None:
            return None
        if process is None:
            print(
                "Warning: No audio process found with Audio ID "
                f"{excluded_audio_object_id}",
                file=sys.stderr,
            )
            continue
        _append_excluded_process(excluded_processes, process)

    return excluded_processes


def _append_excluded_process(
    excluded_processes: list[AudioProcess],
    process: AudioProcess,
) -> None:
    if any(
        excluded.audio_object_id == process.audio_object_id
        for excluded in excluded_processes
    ):
        return
    excluded_processes.append(process)
    print(
        f"Excluding: {process.name} "
        f"(PID: {process.pid}, Audio ID: {process.audio_object_id})",
        flush=True,
    )


def _build_system_tap(
    exclude: list[str],
    *,
    exclude_pids: Sequence[int] = (),
    exclude_audio_object_ids: Sequence[int] = (),
    mono: bool = False,
    visible: bool = False,
) -> TapDescription | None:
    excluded_processes = _resolve_excluded_processes(
        exclude, exclude_pids, exclude_audio_object_ids
    )
    if excluded_processes is None:
        return None

    print("Recording global process-output mix", flush=True)
    return build_system_tap_description(
        excluded_processes,
        mono=mono,
        visible=visible,
    )


def _resolve_device_output_stream(
    query: str,
    stream_index: int | None,
) -> AudioDeviceStream | None:
    """Resolve a device name or UID into one output stream, or report why not."""
    try:
        device = find_audio_device_by_uid(query)
        if device is None:
            device = find_audio_device_by_name(query)
    except AmbiguousAudioDeviceError as exc:
        print(str(exc), file=sys.stderr)
        return None
    except OSError as exc:
        print(f"Error looking up audio devices: {exc}", file=sys.stderr)
        return None

    if device is None:
        message_lines = [f"No audio device found matching '{query}'"]
        try:
            devices = list_audio_devices()
        except OSError:
            devices = []
        if devices:
            message_lines.append("")
            message_lines.append("Available audio devices:")
            message_lines.extend(
                f"  - {listed.name} (UID: {listed.uid})" for listed in devices[:10]
            )
        print("\n".join(message_lines), file=sys.stderr)
        return None

    output_streams = device.output_streams
    if not output_streams:
        print(
            f"Audio device '{device.name}' has no output streams to tap",
            file=sys.stderr,
        )
        return None

    wanted_index = 0 if stream_index is None else stream_index
    for stream in output_streams:
        if stream.stream_index == wanted_index:
            return stream

    available = ", ".join(str(s.stream_index) for s in output_streams)
    print(
        f"Audio device '{device.name}' has no output stream {wanted_index}; "
        f"available: {available}",
        file=sys.stderr,
    )
    return None


def _build_device_tap(
    device_query: str,
    stream_index: int | None,
    *,
    included: Sequence[AudioProcess] = (),
    exclude: Sequence[str] = (),
    mute_behavior: TapMuteBehavior = TapMuteBehavior.UNMUTED,
    visible: bool = False,
) -> TapDescription | None:
    device_stream = _resolve_device_output_stream(device_query, stream_index)
    if device_stream is None:
        return None

    excluded_processes = _resolve_excluded_processes(exclude, (), ())
    if excluded_processes is None:
        return None

    for process in included:
        print(
            f"Recording from: {process.name} "
            f"(PID: {process.pid}, Audio ID: {process.audio_object_id})",
            flush=True,
        )
    print(
        "Recording device: "
        f"{device_stream.device_name or device_stream.device_uid} "
        f"[output stream {device_stream.stream_index}]",
        flush=True,
    )
    _print_mute_notice(mute_behavior)

    return build_device_tap_description(
        device_stream,
        included=included,
        excluded=excluded_processes,
        mute=mute_behavior,
        visible=visible,
    )


def _build_bundle_tap(
    bundle_ids: Sequence[str],
    *,
    restore: bool,
    mute_behavior: TapMuteBehavior,
    mono: bool,
    visible: bool,
) -> TapDescription | None:
    for bundle_id in bundle_ids:
        print(f"Recording from bundle ID: {bundle_id}", flush=True)
    if restore:
        print("Tapped apps rejoin the capture if they restart", flush=True)
    _print_mute_notice(mute_behavior)

    try:
        return build_bundle_tap_description(
            bundle_ids,
            restore=restore,
            mute=mute_behavior,
            mono=mono,
            visible=visible,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None


def _build_tap_session(
    tap_uid: str,
    output: str,
) -> RecordingSession | None:
    """Create a session for an existing visible tap by UID."""
    try:
        tap = find_tap_by_uid(tap_uid)
    except OSError as exc:
        print(f"Error looking up audio taps: {exc}", file=sys.stderr)
        return None

    if tap is None:
        message_lines = [f"No visible audio tap found with UID '{tap_uid}'"]
        try:
            taps = list_audio_taps()
        except OSError:
            taps = []
        if taps:
            message_lines.append("")
            message_lines.append("Visible audio taps:")
            message_lines.extend(
                f"  - {listed.name} (UID: {listed.uid})" for listed in taps[:10]
            )
        print("\n".join(message_lines), file=sys.stderr)
        return None

    print(f"Recording from existing tap: {tap.name} (UID: {tap.uid})", flush=True)
    return record_tap(tap, output)


def _print_recording_start_error(
    exc: OSError | RuntimeError | UnsupportedTapFormatError,
) -> None:
    print(f"Error starting recording: {exc}", file=sys.stderr)

    if isinstance(exc, OSError):
        print("", file=sys.stderr)
        hint_lines = _OUTPUT_HINT if exc.errno is not None else _PERMISSION_HINT
        for line in hint_lines:
            print(line, file=sys.stderr)


def _run_recording_session(
    session: RecordingSession,
    output: str,
    duration: float | None,
    *,
    announce_tap: bool = True,
) -> int:
    print(f"Output: {output}", flush=True)

    stop_event = threading.Event()

    def signal_handler(_sig: int, _frame: object) -> None:
        stop_event.set()
        print("\nStopping recording...", flush=True)

    original_handler = signal.signal(signal.SIGINT, signal_handler)

    try:
        try:
            session.start()
        except (OSError, RuntimeError, UnsupportedTapFormatError) as exc:
            with contextlib.suppress(OSError, RuntimeError):
                session.close()
            _print_recording_start_error(exc)
            return 1

        if announce_tap and session.tap_id is not None:
            print(f"Created tap (ID: {session.tap_id})", flush=True)

        try:
            if duration is not None:
                print(
                    f"Recording for {duration} seconds... (Ctrl+C to stop early)",
                    flush=True,
                )
            else:
                print("Recording... (Ctrl+C to stop)", flush=True)

            _wait_for_stop_or_capture_failure(session, stop_event, duration)

            session.stop()
            print(f"Recorded {session.duration_seconds:.2f} seconds", flush=True)
            if session.captured_only_silence:
                for line in _SILENCE_HINT:
                    print(line, file=sys.stderr)
            print(f"Saved to: {output}", flush=True)
            return 0
        except (OSError, RuntimeError) as exc:
            print(f"Recording error: {exc}", file=sys.stderr)
            return 1
        finally:
            with contextlib.suppress(OSError, RuntimeError):
                session.close()
    finally:
        signal.signal(signal.SIGINT, original_handler)


def _validate_record_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Reject conflicting record target and option combinations."""
    has_process_selectors = bool(args.app_names or args.pid or args.audio_object_id)
    has_excludes = bool(
        args.exclude or args.exclude_pid or args.exclude_audio_object_id
    )
    target_kinds = sum(
        (
            has_process_selectors,
            args.system,
            args.tap is not None,
            args.bundle_ids != [],
        )
    )
    # --device combines with process selectors (an include list), so it only
    # counts as a target on its own.
    if target_kinds == 0 and args.device is None:
        parser.error(
            "record: a target is required: APP_NAME, --pid, "
            "--audio-object-id, --system, --tap, --device, or --bundle-id"
        )
    if target_kinds > 1:
        parser.error(
            "record: choose one target kind: process selectors, --system, "
            "--tap, or --bundle-id"
        )

    if args.mute and args.mute_when_tapped:
        parser.error("record: --mute and --mute-when-tapped are mutually exclusive")

    if args.system:
        if args.device is not None:
            parser.error("record: --device cannot be used with --system")
        if args.mute or args.mute_when_tapped:
            parser.error(
                "record: --mute options can only be used when recording "
                "specific apps or a device"
            )
    elif args.tap is not None:
        if args.device is not None:
            parser.error("record: --device cannot be used with --tap")
        incompatible = (
            args.mute
            or args.mute_when_tapped
            or args.mono
            or args.visible
            or has_excludes
        )
        if incompatible:
            parser.error(
                "record: --tap records an existing tap; tap-creation "
                "options cannot be used with it"
            )
    elif args.device is not None:
        if args.bundle_ids:
            parser.error("record: --bundle-id cannot be used with --device")
        if args.mono:
            parser.error(
                "record: --mono cannot be used with --device; the capture "
                "format follows the device stream"
            )
        if has_process_selectors and has_excludes:
            parser.error(
                "record: --device accepts app selectors or --exclude options, not both"
            )
        if args.exclude_pid or args.exclude_audio_object_id:
            parser.error("record: --device exclusions support --exclude app names only")
    elif args.bundle_ids:
        if has_excludes:
            parser.error("record: --exclude options cannot be used with --bundle-id")
    else:
        if has_excludes:
            parser.error("record: --exclude options require --system or --device")

    if args.no_restore and not args.bundle_ids:
        parser.error("record: --no-restore requires --bundle-id")
    if args.stream is not None and args.device is None:
        parser.error("record: --stream requires --device")


def _run_record_command(args: argparse.Namespace) -> int:
    mute_behavior = _mute_behavior_from_args(args)

    if args.tap is not None:
        session = _build_tap_session(args.tap, args.output)
        if session is None:
            return 1
        return _run_recording_session(
            session, args.output, args.duration, announce_tap=False
        )

    if args.system:
        tap_desc = _build_system_tap(
            args.exclude,
            exclude_pids=args.exclude_pid,
            exclude_audio_object_ids=args.exclude_audio_object_id,
            mono=args.mono,
            visible=args.visible,
        )
    elif args.bundle_ids:
        tap_desc = _build_bundle_tap(
            args.bundle_ids,
            restore=not args.no_restore,
            mute_behavior=mute_behavior,
            mono=args.mono,
            visible=args.visible,
        )
    elif args.device is not None:
        included = _resolve_record_processes(
            args.app_names, args.pid, args.audio_object_id
        )
        if included is None:
            return 1
        tap_desc = _build_device_tap(
            args.device,
            args.stream,
            included=included,
            exclude=args.exclude,
            mute_behavior=mute_behavior,
            visible=args.visible,
        )
    else:
        processes = _resolve_record_processes(
            args.app_names, args.pid, args.audio_object_id
        )
        if processes is None:
            return 1
        tap_desc = _build_processes_tap(
            processes,
            mute_behavior=mute_behavior,
            mono=args.mono,
            visible=args.visible,
        )

    if tap_desc is None:
        return 1

    return _run_recording_session(
        RecordingSession(tap_desc, args.output),
        args.output,
        args.duration,
    )


def _run_multitrack_command(args: argparse.Namespace) -> int:
    if not (args.app_names or args.pid or args.audio_object_id):
        print(
            "record-multitrack: APP_NAME, --pid, or --audio-object-id is required",
            file=sys.stderr,
        )
        return 2
    if args.mute and args.mute_when_tapped:
        print(
            "record-multitrack: --mute and --mute-when-tapped are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    processes = _resolve_record_processes(
        args.app_names, args.pid, args.audio_object_id
    )
    if processes is None:
        return 1
    if len(processes) < 2 and not args.with_mic:
        print(
            "record-multitrack: needs at least two tracks; use "
            "'catap record' for a single app",
            file=sys.stderr,
        )
        return 2

    mute_behavior = _mute_behavior_from_args(args)

    for process in processes:
        print(
            f"Recording from: {process.name} "
            f"(PID: {process.pid}, Audio ID: {process.audio_object_id})",
            flush=True,
        )
    if args.with_mic:
        mic_label = (
            "default input device"
            if args.with_mic is True
            else f"input device '{args.with_mic}'"
        )
        print(f"Recording microphone track from the {mic_label}", flush=True)
    _print_mute_notice(mute_behavior)

    try:
        session = record_multitrack(
            processes,
            args.output_dir,
            microphone=args.with_mic if args.with_mic is not None else False,
            mute=mute_behavior,
            mono=args.mono,
        )
    except (LookupError, OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for label, path in zip(session.track_labels, session.output_paths, strict=True):
        print(f"Track: {label} -> {path}", flush=True)

    stop_event = threading.Event()

    def signal_handler(_sig: int, _frame: object) -> None:
        stop_event.set()
        print("\nStopping recording...", flush=True)

    original_handler = signal.signal(signal.SIGINT, signal_handler)

    try:
        try:
            session.start()
        except (OSError, RuntimeError, UnsupportedTapFormatError) as exc:
            with contextlib.suppress(OSError, RuntimeError):
                session.close()
            _print_recording_start_error(exc)
            return 1

        try:
            if args.duration is not None:
                print(
                    f"Recording for {args.duration} seconds... (Ctrl+C to stop early)",
                    flush=True,
                )
            else:
                print("Recording... (Ctrl+C to stop)", flush=True)

            _wait_for_stop_or_capture_failure(
                session,
                stop_event,
                args.duration,
            )

            session.stop()
            print(
                f"Recorded {session.duration_seconds:.2f} seconds",
                flush=True,
            )
            silent_tracks = [
                label
                for label, silent in zip(
                    session.track_labels,
                    session.track_captured_only_silence,
                    strict=True,
                )
                if silent
            ]
            if silent_tracks:
                print(
                    "Warning: these tracks contained only silence: "
                    f"{', '.join(silent_tracks)}",
                    file=sys.stderr,
                )
                if session.captured_only_silence:
                    for line in _SILENCE_HINT[1:]:
                        print(line, file=sys.stderr)
            print(f"Saved tracks to: {args.output_dir}", flush=True)
            return 0
        except (OSError, RuntimeError) as exc:
            print(f"Recording error: {exc}", file=sys.stderr)
            return 1
        finally:
            with contextlib.suppress(OSError, RuntimeError):
                session.close()
    finally:
        signal.signal(signal.SIGINT, original_handler)


def _exit_code_from_system_exit(exc: SystemExit) -> int:
    code = exc.code
    return code if isinstance(code, int) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()

    try:
        args = parser.parse_args(argv)

        if args.command == "list-apps":
            return _list_apps(show_all=args.show_all)

        if args.command == "list-taps":
            return _list_taps()

        if args.command == "list-devices":
            return _list_devices()

        if args.command == "watch":
            return _watch(
                watch_processes=args.processes,
                watch_taps=args.taps,
                watch_devices=args.devices,
                watch_default_output=args.default_output,
            )

        if args.command == "record-multitrack":
            return _run_multitrack_command(args)

        if args.command == "record":
            _validate_record_args(parser, args)
            return _run_record_command(args)

        parser.error(f"Unknown command: {args.command}")
    except SystemExit as exc:
        return _exit_code_from_system_exit(exc)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
