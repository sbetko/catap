"""Command-line interface for catap."""
from __future__ import annotations

import os
import click


@click.group()
@click.version_option()
def main() -> None:
    """catap - Python Core Audio Tap for capturing application audio."""
    pass


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
@click.option(
    "--meter", "-m",
    is_flag=True,
    help="Show live VU meter during recording"
)
def record(
    app_name: str,
    output: str,
    duration: float | None,
    mute: bool,
    meter: bool,
) -> None:
    """
    Record audio from an application.

    APP_NAME can be a partial match (case-insensitive) of the application name.
    Use 'catap list-apps' to see available applications.
    """
    import signal
    import time
    from catap.bindings.process import find_process_by_name, list_audio_processes
    from catap.bindings.tap_description import TapDescription, TapMuteBehavior
    from catap.bindings.hardware import create_process_tap, destroy_process_tap
    from catap.core.recorder import AudioRecorder

    # Find the process
    process = find_process_by_name(app_name)
    if not process:
        # Show available processes to help user
        all_procs = list_audio_processes()
        click.echo(f"Error: No audio process found matching '{app_name}'", err=True)
        if all_procs:
            click.echo("\nAvailable audio processes:", err=True)
            for p in all_procs[:10]:
                status = "outputting" if p.is_outputting else "idle"
                click.echo(f"  - {p.name} ({status})", err=True)
            if len(all_procs) > 10:
                click.echo(f"  ... and {len(all_procs) - 10} more", err=True)
        return

    click.echo(f"Recording from: {process.name} (PID: {process.pid})")
    click.echo(f"Output: {output}")

    # Create tap description
    tap_desc = TapDescription.stereo_mixdown_of_processes([process.audio_object_id])
    tap_desc.name = f"catap recording {process.name}"
    tap_desc.is_private = True

    if mute:
        tap_desc.mute_behavior = TapMuteBehavior.MUTED
        click.echo("Muting app audio during recording")
    else:
        tap_desc.mute_behavior = TapMuteBehavior.UNMUTED

    # Create tap
    try:
        tap_id = create_process_tap(tap_desc)
    except OSError as e:
        click.echo(f"Error creating audio tap: {e}", err=True)
        click.echo("\nThis may be a permissions issue. Try:", err=True)
        click.echo("  1. Check System Settings > Privacy & Security > Microphone", err=True)
        click.echo("  2. Ensure your terminal app (Terminal, iTerm, etc.) has permission", err=True)
        return

    click.echo(f"Created tap (ID: {tap_id})")

    # Set up VU meter if requested
    vu_meter = None
    if meter:
        try:
            from catap.core.meter import VUMeter
            vu_meter = VUMeter(num_channels=2)
        except ImportError:
            click.echo(
                "Warning: VU meter requires 'rich' library. "
                "Install with: pip install rich",
                err=True
            )

    # Create recorder with optional meter callback
    def on_data_callback(data: bytes, num_frames: int) -> None:
        if vu_meter:
            vu_meter.update(data, num_frames)

    recorder = AudioRecorder(
        tap_id, output, on_data=on_data_callback if vu_meter else None
    )

    # Handle Ctrl+C gracefully
    stop_flag = False

    def signal_handler(sig, frame):
        nonlocal stop_flag
        stop_flag = True
        if not vu_meter:
            click.echo("\nStopping recording...")

    original_handler = signal.signal(signal.SIGINT, signal_handler)

    try:
        # Start recording
        recorder.start()

        # Update meter with duration callback after recorder starts
        if vu_meter:
            vu_meter._duration_callback = lambda: recorder.duration_seconds

        # Recording loop with optional VU meter display
        def recording_loop():
            nonlocal stop_flag
            if duration:
                if not vu_meter:
                    click.echo(f"Recording for {duration} seconds... (Ctrl+C to stop early)")
                start_time = time.time()
                while time.time() - start_time < duration and not stop_flag:
                    time.sleep(0.1)
            else:
                if not vu_meter:
                    click.echo("Recording... (Ctrl+C to stop)")
                while not stop_flag:
                    time.sleep(0.1)

        if vu_meter:
            with vu_meter:
                recording_loop()
        else:
            recording_loop()

        # Stop recording
        recorder.stop()

        click.echo(f"Recorded {recorder.duration_seconds:.2f} seconds")
        click.echo(f"Saved to: {output}")

    except OSError as e:
        click.echo(f"Recording error: {e}", err=True)
    finally:
        # Restore signal handler
        signal.signal(signal.SIGINT, original_handler)

        # Clean up tap
        try:
            destroy_process_tap(tap_id)
        except OSError:
            pass  # Ignore cleanup errors


@main.command("record-system")
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
    "--exclude", "-e",
    multiple=True,
    help="App names to exclude from recording (can be specified multiple times)"
)
@click.option(
    "--meter", "-m",
    is_flag=True,
    help="Show live VU meter during recording"
)
def record_system(
    output: str,
    duration: float | None,
    exclude: tuple[str, ...],
    meter: bool,
) -> None:
    """
    Record all system audio.

    This captures audio from all applications. Use --exclude to omit specific apps.
    """
    import signal
    import time
    from catap.bindings.process import find_process_by_name
    from catap.bindings.tap_description import TapDescription, TapMuteBehavior
    from catap.bindings.hardware import create_process_tap, destroy_process_tap
    from catap.core.recorder import AudioRecorder

    # Find processes to exclude
    exclude_ids = []
    if exclude:
        for app_name in exclude:
            process = find_process_by_name(app_name)
            if process:
                exclude_ids.append(process.audio_object_id)
                click.echo(f"Excluding: {process.name} (PID: {process.pid})")
            else:
                click.echo(f"Warning: No audio process found matching '{app_name}'", err=True)

    click.echo("Recording all system audio")
    click.echo(f"Output: {output}")

    # Create global tap description
    tap_desc = TapDescription.stereo_global_tap_excluding(exclude_ids)
    tap_desc.name = "catap system recording"
    tap_desc.is_private = True
    tap_desc.mute_behavior = TapMuteBehavior.UNMUTED

    # Create tap
    try:
        tap_id = create_process_tap(tap_desc)
    except OSError as e:
        click.echo(f"Error creating audio tap: {e}", err=True)
        click.echo("\nThis may be a permissions issue. Try:", err=True)
        click.echo("  1. Check System Settings > Privacy & Security > Microphone", err=True)
        click.echo("  2. Ensure your terminal app (Terminal, iTerm, etc.) has permission", err=True)
        return

    click.echo(f"Created tap (ID: {tap_id})")

    # Set up VU meter if requested
    vu_meter = None
    if meter:
        try:
            from catap.core.meter import VUMeter
            vu_meter = VUMeter(num_channels=2)
        except ImportError:
            click.echo(
                "Warning: VU meter requires 'rich' library. "
                "Install with: pip install rich",
                err=True
            )

    # Create recorder with optional meter callback
    def on_data_callback(data: bytes, num_frames: int) -> None:
        if vu_meter:
            vu_meter.update(data, num_frames)

    recorder = AudioRecorder(
        tap_id, output, on_data=on_data_callback if vu_meter else None
    )

    # Handle Ctrl+C gracefully
    stop_flag = False

    def signal_handler(sig, frame):
        nonlocal stop_flag
        stop_flag = True
        if not vu_meter:
            click.echo("\nStopping recording...")

    original_handler = signal.signal(signal.SIGINT, signal_handler)

    try:
        # Start recording
        recorder.start()

        # Update meter with duration callback after recorder starts
        if vu_meter:
            vu_meter._duration_callback = lambda: recorder.duration_seconds

        # Recording loop with optional VU meter display
        def recording_loop():
            nonlocal stop_flag
            if duration:
                if not vu_meter:
                    click.echo(f"Recording for {duration} seconds... (Ctrl+C to stop early)")
                start_time = time.time()
                while time.time() - start_time < duration and not stop_flag:
                    time.sleep(0.1)
            else:
                if not vu_meter:
                    click.echo("Recording... (Ctrl+C to stop)")
                while not stop_flag:
                    time.sleep(0.1)

        if vu_meter:
            with vu_meter:
                recording_loop()
        else:
            recording_loop()

        # Stop recording
        recorder.stop()

        click.echo(f"Recorded {recorder.duration_seconds:.2f} seconds")
        click.echo(f"Saved to: {output}")

    except OSError as e:
        click.echo(f"Recording error: {e}", err=True)
    finally:
        # Restore signal handler
        signal.signal(signal.SIGINT, original_handler)

        # Clean up tap
        try:
            destroy_process_tap(tap_id)
        except OSError:
            pass  # Ignore cleanup errors


@main.command("stream")
@click.argument("app_name")
@click.option(
    "--format", "-f",
    type=click.Choice(["f32le", "s16le", "wav"]),
    default="f32le",
    help="Output format: f32le (native float), s16le (16-bit int), wav (with header)"
)
@click.option(
    "--duration", "-d",
    type=float,
    default=None,
    help="Duration limit in seconds (default: until Ctrl+C)"
)
@click.option(
    "--mute/--no-mute",
    default=False,
    help="Mute the app while streaming"
)
def stream(
    app_name: str,
    format: str,
    duration: float | None,
    mute: bool,
) -> None:
    """
    Stream audio from an application to stdout.

    Output is raw PCM data suitable for piping to ffmpeg, sox, etc.
    Status messages are written to stderr.

    \b
    Examples:
        catap stream Spotify | ffmpeg -f f32le -ar 44100 -ac 2 -i - output.mp3
        catap stream Safari --format s16le | sox -t raw -r 44100 -c 2 -e signed -b 16 - out.wav
    """
    import signal
    import sys
    import time
    from catap.bindings.process import find_process_by_name, list_audio_processes
    from catap.bindings.tap_description import TapDescription, TapMuteBehavior
    from catap.bindings.hardware import create_process_tap, destroy_process_tap
    from catap.core.recorder import AudioRecorder
    from catap.core.streamer import AudioStreamer

    # Find the process
    process = find_process_by_name(app_name)
    if not process:
        all_procs = list_audio_processes()
        click.echo(f"Error: No audio process found matching '{app_name}'", err=True)
        if all_procs:
            click.echo("\nAvailable audio processes:", err=True)
            for p in all_procs[:10]:
                status = "outputting" if p.is_outputting else "idle"
                click.echo(f"  - {p.name} ({status})", err=True)
        return

    click.echo(f"Streaming from: {process.name} (PID: {process.pid})", err=True)
    click.echo(f"Format: {format}", err=True)

    # Create tap description
    tap_desc = TapDescription.stereo_mixdown_of_processes([process.audio_object_id])
    tap_desc.name = f"catap streaming {process.name}"
    tap_desc.is_private = True
    tap_desc.mute_behavior = TapMuteBehavior.MUTED if mute else TapMuteBehavior.UNMUTED

    # Create tap
    try:
        tap_id = create_process_tap(tap_desc)
    except OSError as e:
        click.echo(f"Error creating audio tap: {e}", err=True)
        return

    # Create streamer (sample rate will be updated after recorder starts)
    streamer = AudioStreamer(
        format=format,
        sample_rate=44100,
        num_channels=2,
        output=sys.stdout.buffer,
    )

    # Create recorder in streaming mode (no output file)
    recorder = AudioRecorder(tap_id, output_path=None, on_data=streamer.write)

    # Handle Ctrl+C gracefully
    stop_flag = False

    def signal_handler(sig, frame):
        nonlocal stop_flag
        stop_flag = True
        click.echo("\nStopping stream...", err=True)

    original_handler = signal.signal(signal.SIGINT, signal_handler)

    try:
        recorder.start()

        # Update streamer with actual format info
        streamer.sample_rate = int(recorder.sample_rate)
        streamer.num_channels = recorder.num_channels

        if duration:
            click.echo(f"Streaming for {duration} seconds...", err=True)
            start_time = time.time()
            while time.time() - start_time < duration and not stop_flag:
                time.sleep(0.1)
        else:
            click.echo("Streaming... (Ctrl+C to stop)", err=True)
            while not stop_flag:
                time.sleep(0.1)

        recorder.stop()
        click.echo(f"Streamed {recorder.duration_seconds:.2f} seconds", err=True)

    except OSError as e:
        click.echo(f"Streaming error: {e}", err=True)
    finally:
        signal.signal(signal.SIGINT, original_handler)
        try:
            destroy_process_tap(tap_id)
        except OSError:
            pass


@main.command("stream-system")
@click.option(
    "--format", "-f",
    type=click.Choice(["f32le", "s16le", "wav"]),
    default="f32le",
    help="Output format: f32le (native float), s16le (16-bit int), wav (with header)"
)
@click.option(
    "--duration", "-d",
    type=float,
    default=None,
    help="Duration limit in seconds (default: until Ctrl+C)"
)
@click.option(
    "--exclude", "-e",
    multiple=True,
    help="App names to exclude from streaming (can be specified multiple times)"
)
def stream_system(
    format: str,
    duration: float | None,
    exclude: tuple[str, ...],
) -> None:
    """
    Stream all system audio to stdout.

    Output is raw PCM data suitable for piping to ffmpeg, sox, etc.
    Use --exclude to omit specific apps.

    \b
    Examples:
        catap stream-system | ffmpeg -f f32le -ar 44100 -ac 2 -i - output.flac
        catap stream-system --exclude Zoom | ...
    """
    import signal
    import sys
    import time
    from catap.bindings.process import find_process_by_name
    from catap.bindings.tap_description import TapDescription, TapMuteBehavior
    from catap.bindings.hardware import create_process_tap, destroy_process_tap
    from catap.core.recorder import AudioRecorder
    from catap.core.streamer import AudioStreamer

    # Find processes to exclude
    exclude_ids = []
    if exclude:
        for app_name in exclude:
            process = find_process_by_name(app_name)
            if process:
                exclude_ids.append(process.audio_object_id)
                click.echo(f"Excluding: {process.name} (PID: {process.pid})", err=True)
            else:
                click.echo(f"Warning: No audio process found matching '{app_name}'", err=True)

    click.echo("Streaming all system audio", err=True)
    click.echo(f"Format: {format}", err=True)

    # Create global tap description
    tap_desc = TapDescription.stereo_global_tap_excluding(exclude_ids)
    tap_desc.name = "catap system streaming"
    tap_desc.is_private = True
    tap_desc.mute_behavior = TapMuteBehavior.UNMUTED

    # Create tap
    try:
        tap_id = create_process_tap(tap_desc)
    except OSError as e:
        click.echo(f"Error creating audio tap: {e}", err=True)
        return

    # Create streamer
    streamer = AudioStreamer(
        format=format,
        sample_rate=44100,
        num_channels=2,
        output=sys.stdout.buffer,
    )

    # Create recorder in streaming mode
    recorder = AudioRecorder(tap_id, output_path=None, on_data=streamer.write)

    # Handle Ctrl+C gracefully
    stop_flag = False

    def signal_handler(sig, frame):
        nonlocal stop_flag
        stop_flag = True
        click.echo("\nStopping stream...", err=True)

    original_handler = signal.signal(signal.SIGINT, signal_handler)

    try:
        recorder.start()

        # Update streamer with actual format info
        streamer.sample_rate = int(recorder.sample_rate)
        streamer.num_channels = recorder.num_channels

        if duration:
            click.echo(f"Streaming for {duration} seconds...", err=True)
            start_time = time.time()
            while time.time() - start_time < duration and not stop_flag:
                time.sleep(0.1)
        else:
            click.echo("Streaming... (Ctrl+C to stop)", err=True)
            while not stop_flag:
                time.sleep(0.1)

        recorder.stop()
        click.echo(f"Streamed {recorder.duration_seconds:.2f} seconds", err=True)

    except OSError as e:
        click.echo(f"Streaming error: {e}", err=True)
    finally:
        signal.signal(signal.SIGINT, original_handler)
        try:
            destroy_process_tap(tap_id)
        except OSError:
            pass


if __name__ == "__main__":
    main()
