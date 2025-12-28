"""Handle app bundle detection and re-launching."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def get_bundle_path() -> Path:
    """Get the path to the catap.app bundle inside the package."""
    package_dir = Path(__file__).parent.parent
    return package_dir / "catap.app"


def is_running_from_bundle() -> bool:
    """Check if we're running from inside the app bundle."""
    return os.environ.get("CATAP_RUNNING_FROM_BUNDLE") == "1"


def check_bundle_exists() -> bool:
    """Check if the app bundle exists and is valid."""
    bundle_path = get_bundle_path()
    info_plist = bundle_path / "Contents" / "Info.plist"
    return bundle_path.exists() and info_plist.exists()


def relaunch_via_bundle(args: list[str] | None = None) -> int:
    """
    Re-launch the current command through the app bundle.

    This is needed to get audio capture permissions on macOS.
    The app bundle contains NSAudioCaptureUsageDescription.

    Args:
        args: Arguments to pass to the relaunched command

    Returns:
        Exit code from the bundle process
    """
    bundle_path = get_bundle_path()

    if not check_bundle_exists():
        print(
            "Error: catap.app bundle not found. "
            "Audio capture may not work without it.",
            file=sys.stderr
        )
        return 1

    # Build the command to launch via 'open'
    cmd = ["open", "-a", str(bundle_path), "--args"]

    if args:
        cmd.extend(args)
    else:
        # Pass the original command-line arguments
        cmd.extend(sys.argv[1:])

    # Execute and wait
    result = subprocess.run(cmd)
    return result.returncode


def ensure_running_from_bundle() -> bool:
    """
    Ensure we're running from the app bundle for permission purposes.

    If not running from bundle, relaunches via the bundle and exits.

    Returns:
        True if already running from bundle, never returns if relaunching
    """
    if is_running_from_bundle():
        return True

    # Need to relaunch
    sys.exit(relaunch_via_bundle())
