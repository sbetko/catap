"""Command-line interface for catap."""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
import threading
from collections.abc import Sequence
from typing import Protocol

from catap import (
    AmbiguousAudioProcessError,
    AudioProcess,
    RecordingSession,
    TapDescription,
    __version__,
    find_process_by_name,
    list_audio_processes,
)
from catap.session import build_process_tap_description, build_system_tap_description

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


class _DisplayProcess(Protocol):
    name: str
    pid: int
    bundle_id: str | None
    is_outputting: bool


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid number") from exc

    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")

    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catap",
        description="catap - Python Core Audio Tap for capturing application audio.",
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
        help="Show all audio processes, not just those outputting audio",
    )

    record_parser = subparsers.add_parser(
        "record",
        help="Record app audio or all system audio",
    )
    record_parser.add_argument(
        "app_name",
        nargs="?",
        help=(
            "Application name to record (partial match, case-insensitive). "
            "Required unless --system is set."
        ),
    )
    record_parser.add_argument(
        "--system",
        action="store_true",
        help="Record all system audio",
    )
    record_parser.add_argument(
        "--output",
        "-o",
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
        help="Mute the app while recording (app recording only)",
    )
    record_parser.add_argument(
        "--exclude",
        "-e",
        action="append",
        default=[],
        help="App names to exclude from system recording (repeatable)",
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


def _describe_process(process: _DisplayProcess) -> str:
    bundle = process.bundle_id or "N/A"
    status = "outputting" if process.is_outputting else "idle"
    return f"{process.name} (PID: {process.pid}, Bundle ID: {bundle}, {status})"


def _print_ambiguous_process_error(
    query: str, exc: AmbiguousAudioProcessError
) -> None:
    message_lines = [f"Multiple audio processes match '{query}':"]
    message_lines.extend(
        f"  - {_describe_process(process)}" for process in exc.matches[:10]
    )
    if len(exc.matches) > 10:
        message_lines.append(f"  ... and {len(exc.matches) - 10} more")
    print("\n".join(message_lines), file=sys.stderr)


def _build_app_tap(app_name: str, mute: bool) -> TapDescription | None:
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

        message_lines = [f"No audio process found matching '{app_name}'"]
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
        return None

    print(f"Recording from: {process.name} (PID: {process.pid})", flush=True)
    if mute:
        print("Muting app audio during recording", flush=True)

    return build_process_tap_description(process, mute=mute)


def _build_system_tap(exclude: list[str]) -> TapDescription | None:
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
            excluded_processes.append(process)
            print(f"Excluding: {process.name} (PID: {process.pid})", flush=True)
        else:
            print(
                f"Warning: No audio process found matching '{excluded_app_name}'",
                file=sys.stderr,
            )

    print("Recording all system audio", flush=True)
    return build_system_tap_description(excluded_processes)


def _print_recording_start_error(exc: OSError) -> None:
    print(f"Error starting recording: {exc}", file=sys.stderr)
    print("", file=sys.stderr)

    hint_lines = _OUTPUT_HINT if exc.errno is not None else _PERMISSION_HINT
    for line in hint_lines:
        print(line, file=sys.stderr)


def _run_recording_session(
    tap_desc: TapDescription,
    output: str,
    duration: float | None,
) -> int:
    print(f"Output: {output}", flush=True)
    session = RecordingSession(tap_desc, output)

    stop_event = threading.Event()

    def signal_handler(_sig: int, _frame: object) -> None:
        stop_event.set()
        print("\nStopping recording...", flush=True)

    original_handler = signal.signal(signal.SIGINT, signal_handler)

    try:
        try:
            session.start()
        except OSError as exc:
            with contextlib.suppress(OSError):
                session.close()
            _print_recording_start_error(exc)
            return 1

        if session.tap_id is not None:
            print(f"Created tap (ID: {session.tap_id})", flush=True)

        try:
            if duration is not None:
                print(
                    f"Recording for {duration} seconds... (Ctrl+C to stop early)",
                    flush=True,
                )
                stop_event.wait(duration)
            else:
                print("Recording... (Ctrl+C to stop)", flush=True)
                stop_event.wait()

            session.stop()
            print(f"Recorded {session.duration_seconds:.2f} seconds", flush=True)
            print(f"Saved to: {output}", flush=True)
            return 0
        except OSError as exc:
            print(f"Recording error: {exc}", file=sys.stderr)
            return 1
        finally:
            with contextlib.suppress(OSError):
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

        if args.command == "record":
            if args.system:
                if args.app_name:
                    parser.error("record: APP_NAME cannot be used with --system")
                if args.mute:
                    parser.error(
                        "record: --mute can only be used when recording a single app"
                    )
                tap_desc = _build_system_tap(args.exclude)
                if tap_desc is None:
                    return 1
            else:
                if not args.app_name:
                    parser.error("record: APP_NAME is required unless --system is set")
                if args.exclude:
                    parser.error("record: --exclude requires --system")
                tap_desc = _build_app_tap(args.app_name, args.mute)
                if tap_desc is None:
                    return 1

            return _run_recording_session(
                tap_desc,
                output=args.output,
                duration=args.duration,
            )

        parser.error(f"Unknown command: {args.command}")
    except SystemExit as exc:
        return _exit_code_from_system_exit(exc)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
