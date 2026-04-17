"""Command-line interface for catap."""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
import threading
from collections.abc import Sequence

from catap import (
    RecordingSession,
    TapDescription,
    TapMuteBehavior,
    __version__,
    find_process_by_name,
    list_audio_processes,
)

_PERMISSION_HINT = [
    "This may be a permissions issue. Try:",
    "  1. Check System Settings > Privacy & Security > Screen & System Audio Recording",
    "  2. Ensure your terminal app has permission",
]


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
            print("No audio processes found.")
        else:
            print("No applications currently outputting audio.")
            print("Use --all to see all registered audio processes.")
        return 0

    print(f"{'Status':<2} {'Name':<30} {'Bundle ID':<40} {'Audio ID':<10} {'PID':<8}")
    print("-" * 92)

    for process in processes:
        bundle = process.bundle_id or "N/A"
        status = "♪" if process.is_outputting else " "
        print(
            f"{status:<2} {process.name:<30} {bundle:<40} "
            f"{process.audio_object_id:<10} {process.pid:<8}"
        )

    return 0


def _build_app_tap(app_name: str, mute: bool) -> TapDescription | None:
    process = find_process_by_name(app_name)
    if not process:
        all_processes = list_audio_processes()
        message_lines = [f"No audio process found matching '{app_name}'"]
        if all_processes:
            message_lines.append("")
            message_lines.append("Available audio processes:")
            for listed_process in all_processes[:10]:
                status = "outputting" if listed_process.is_outputting else "idle"
                message_lines.append(f"  - {listed_process.name} ({status})")
            if len(all_processes) > 10:
                message_lines.append(f"  ... and {len(all_processes) - 10} more")
        print("\n".join(message_lines), file=sys.stderr)
        return None

    print(f"Recording from: {process.name} (PID: {process.pid})")

    tap_desc = TapDescription.stereo_mixdown_of_processes([process.audio_object_id])
    tap_desc.name = f"catap recording {process.name}"
    tap_desc.is_private = True

    if mute:
        tap_desc.mute_behavior = TapMuteBehavior.MUTED
        print("Muting app audio during recording")
    else:
        tap_desc.mute_behavior = TapMuteBehavior.UNMUTED

    return tap_desc


def _build_system_tap(exclude: list[str]) -> TapDescription:
    exclude_ids: list[int] = []
    for excluded_app_name in exclude:
        process = find_process_by_name(excluded_app_name)
        if process:
            exclude_ids.append(process.audio_object_id)
            print(f"Excluding: {process.name} (PID: {process.pid})")
        else:
            print(
                f"Warning: No audio process found matching '{excluded_app_name}'",
                file=sys.stderr,
            )

    print("Recording all system audio")
    tap_desc = TapDescription.stereo_global_tap_excluding(exclude_ids)
    tap_desc.name = "catap system recording"
    tap_desc.is_private = True
    tap_desc.mute_behavior = TapMuteBehavior.UNMUTED
    return tap_desc


def _run_recording_session(
    tap_desc: TapDescription,
    output: str,
    duration: float | None,
) -> int:
    print(f"Output: {output}")
    session = RecordingSession(tap_desc, output)

    stop_event = threading.Event()

    def signal_handler(_sig: int, _frame: object) -> None:
        stop_event.set()
        print("\nStopping recording...")

    original_handler = signal.signal(signal.SIGINT, signal_handler)

    try:
        try:
            session.start()
        except OSError as exc:
            with contextlib.suppress(OSError):
                session.close()
            print(f"Error starting recording: {exc}", file=sys.stderr)
            print("", file=sys.stderr)
            for line in _PERMISSION_HINT:
                print(line, file=sys.stderr)
            return 1

        if session.tap_id is not None:
            print(f"Created tap (ID: {session.tap_id})")

        try:
            if duration is not None:
                print(f"Recording for {duration} seconds... (Ctrl+C to stop early)")
                stop_event.wait(duration)
            else:
                print("Recording... (Ctrl+C to stop)")
                stop_event.wait()

            session.stop()
            print(f"Recorded {session.duration_seconds:.2f} seconds")
            print(f"Saved to: {output}")
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
