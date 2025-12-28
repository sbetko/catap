"""Command-line interface for catap."""
from __future__ import annotations

import os
import click
from catap.bundle.launcher import (
    is_running_from_bundle,
    check_bundle_exists,
    get_bundle_path,
    ensure_running_from_bundle,
)


@click.group()
@click.version_option()
def main() -> None:
    """catap - Python Core Audio Tap for capturing application audio."""
    pass


@main.command("test-bundle")
@click.option(
    "--write-log", "-w",
    is_flag=True,
    help="Write test results to a log file for async testing"
)
def test_bundle(write_log: bool) -> None:
    """Test the app bundle configuration and permissions setup."""
    import sys
    from datetime import datetime

    output_lines = []
    output_lines.append("=== catap Bundle Test ===\n")

    # Check if bundle exists
    bundle_exists = check_bundle_exists()
    bundle_path = get_bundle_path()
    output_lines.append(f"Bundle path: {bundle_path}")
    output_lines.append(f"Bundle exists: {'✓' if bundle_exists else '✗'}")

    if bundle_exists:
        info_plist = bundle_path / "Contents" / "Info.plist"
        output_lines.append(f"Info.plist: {info_plist}")
        output_lines.append(f"Info.plist exists: {'✓' if info_plist.exists() else '✗'}")

        launcher = bundle_path / "Contents" / "MacOS" / "catap"
        output_lines.append(f"Launcher script: {launcher}")
        output_lines.append(f"Launcher exists: {'✓' if launcher.exists() else '✗'}")
        output_lines.append(f"Launcher executable: {'✓' if os.access(launcher, os.X_OK) else '✗'}")

    # Check if running from bundle
    from_bundle = is_running_from_bundle()
    output_lines.append(f"\nRunning from bundle: {'✓ YES' if from_bundle else '✗ NO'}")

    if from_bundle:
        output_lines.append("Environment variable CATAP_RUNNING_FROM_BUNDLE is set")
        output_lines.append("\nThis means audio capture permissions should work!")
    else:
        output_lines.append("\nNOTE: Not running from bundle. Audio capture may not have proper permissions.")
        output_lines.append(f"To test bundle launch, run:")
        output_lines.append(f"  open -a {bundle_path} --args test-bundle --write-log")

    output_lines.append(f"\nPython executable: {sys.executable}")
    output_lines.append(f"Timestamp: {datetime.now().isoformat()}")

    # Print to console
    for line in output_lines:
        click.echo(line)

    # Optionally write to log file
    if write_log:
        log_file = os.path.expanduser("~/catap_bundle_test.log")
        with open(log_file, "a") as f:
            f.write("\n" + "="*60 + "\n")
            f.write("\n".join(output_lines))
            f.write("\n" + "="*60 + "\n")
        click.echo(f"\nTest results written to: {log_file}")


@main.command("list-apps")
@click.option(
    "--all", "-a",
    is_flag=True,
    help="Show all audio processes, not just those outputting audio"
)
def list_apps(all: bool) -> None:
    """List applications that are producing audio."""
    from catap.bindings.process import list_audio_processes

    try:
        processes = list_audio_processes()
    except Exception as e:
        click.echo(f"Error listing audio processes: {e}", err=True)
        return

    if not all:
        processes = [p for p in processes if p.is_outputting]

    if not processes:
        if all:
            click.echo("No audio processes found.")
        else:
            click.echo("No applications currently outputting audio.")
            click.echo("Use --all to see all registered audio processes.")
        return

    # Display table header
    click.echo(f"{'Status':<2} {'Name':<30} {'Bundle ID':<40} {'Audio ID':<10} {'PID':<8}")
    click.echo("-" * 92)

    for proc in processes:
        bundle = proc.bundle_id or "N/A"
        status = "♪" if proc.is_outputting else " "
        click.echo(
            f"{status:<2} {proc.name:<30} {bundle:<40} {proc.audio_object_id:<10} {proc.pid:<8}"
        )


@main.command("test-tap")
@click.option(
    "--log", "-l",
    default="~/catap_tap_test.log",
    help="Log file path (default: ~/catap_tap_test.log)"
)
def test_tap(log: str) -> None:
    """Test creating and destroying a tap (requires permissions)."""
    import sys
    from datetime import datetime
    from catap.bindings.process import list_audio_processes
    from catap.bindings.tap_description import TapDescription
    from catap.bindings.hardware import create_process_tap, destroy_process_tap

    output_lines = []
    output_lines.append("=== catap Tap Creation Test ===")
    output_lines.append(f"Timestamp: {datetime.now().isoformat()}")
    output_lines.append(f"Running from bundle: {is_running_from_bundle()}")
    output_lines.append(f"Python: {sys.executable}\n")

    try:
        # Get audio processes
        processes = list_audio_processes()
        output_lines.append(f"Found {len(processes)} audio processes")

        if not processes:
            output_lines.append("ERROR: No audio processes found to test with")
            output_lines.append("Try playing some audio and run again")
        else:
            # Use first process for testing
            proc = processes[0]
            output_lines.append(f"Testing with: {proc.name} (ID: {proc.audio_object_id}, PID: {proc.pid})")

            # Create tap description
            tap_desc = TapDescription.stereo_mixdown_of_processes([proc.audio_object_id])
            tap_desc.name = f"Test tap for {proc.name}"
            tap_desc.is_private = True

            output_lines.append(f"Created tap description: {tap_desc}")

            # Try to create tap
            try:
                tap_id = create_process_tap(tap_desc)
                output_lines.append(f"✓ SUCCESS: Created tap with ID {tap_id}")

                # Try to destroy tap
                try:
                    destroy_process_tap(tap_id)
                    output_lines.append(f"✓ SUCCESS: Destroyed tap {tap_id}")
                    output_lines.append("\n🎉 TAP TEST PASSED: Creation and destruction working!")
                except OSError as e:
                    output_lines.append(f"✗ ERROR destroying tap: {e}")

            except OSError as e:
                output_lines.append(f"✗ ERROR creating tap: {e}")
                error_str = str(e)

                # Parse error code
                if 'status' in error_str:
                    error_code = error_str.split('status ')[-1].strip(')')
                    output_lines.append(f"Error code: {error_code}")

                    # Common error codes
                    if error_code in ['561211770', '2003329802', '-1']:
                        output_lines.append("\nLIKELY CAUSE: Audio capture permission denied")
                        output_lines.append("Expected permission prompt should have appeared")
                        output_lines.append("Check System Settings > Privacy & Security > Microphone")
                    elif error_code == '560947818':  # kAudioHardwareIllegalOperationError
                        output_lines.append("\nLIKELY CAUSE: Invalid operation or process")
                    else:
                        output_lines.append(f"\nUnknown error code: {error_code}")

    except Exception as e:
        output_lines.append(f"\n✗ UNEXPECTED ERROR: {e}")
        import traceback
        output_lines.append(traceback.format_exc())

    # Print to console
    for line in output_lines:
        click.echo(line)

    # Write to log file
    log_path = os.path.expanduser(log)
    try:
        with open(log_path, "a") as f:
            f.write("\n" + "="*60 + "\n")
            f.write("\n".join(output_lines))
            f.write("\n" + "="*60 + "\n")
        click.echo(f"\n📝 Log written to: {log_path}")
    except Exception as e:
        click.echo(f"\n⚠️  Could not write log: {e}", err=True)


@main.command("record")
@click.argument("app_name")
@click.option(
    "--output", "-o",
    default="output.wav",
    help="Output file path (default: output.wav)"
)
@click.option(
    "--duration", "-d",
    type=float,
    default=None,
    help="Recording duration in seconds (default: until Ctrl+C)"
)
@click.option(
    "--mute/--no-mute",
    default=False,
    help="Mute the app while recording"
)
def record(
    app_name: str,
    output: str,
    duration: float | None,
    mute: bool,
) -> None:
    """
    Record audio from an application.

    APP_NAME can be a partial match (case-insensitive) of the application name.
    Use 'catap list-apps' to see available applications.
    """
    click.echo(f"record command - not yet implemented")
    click.echo(f"App: {app_name}")
    click.echo(f"Output: {output}")
    if duration:
        click.echo(f"Duration: {duration}s")
    click.echo(f"Mute: {mute}")


if __name__ == "__main__":
    main()
