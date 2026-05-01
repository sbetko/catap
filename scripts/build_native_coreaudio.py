#!/usr/bin/env python3
"""Build catap's native CoreAudio support dylib."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = _ROOT / "native" / "catap_coreaudio" / "src" / "catap_coreaudio.c"
_INCLUDE = _ROOT / "native" / "catap_coreaudio" / "include"
_DEFAULT_OUTPUT = _ROOT / "src" / "catap" / "native" / "libcatap_coreaudio.dylib"


def _build_command(output: Path, *, debug: bool) -> list[str]:
    optimization_flags = ["-O0", "-g"] if debug else ["-O3", "-DNDEBUG"]
    return [
        "cc",
        "-std=c11",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-fvisibility=hidden",
        "-dynamiclib",
        "-install_name",
        "@rpath/libcatap_coreaudio.dylib",
        "-mmacosx-version-min=14.2",
        *optimization_flags,
        "-I",
        str(_INCLUDE),
        str(_SOURCE),
        "-framework",
        "CoreAudio",
        "-o",
        str(output),
    ]


def build(output: Path, *, debug: bool = False) -> None:
    if platform.system() != "Darwin":
        raise SystemExit("catap native CoreAudio builds require macOS")

    output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("MACOSX_DEPLOYMENT_TARGET", "14.2")
    subprocess.run(_build_command(output, debug=debug), env=env, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"Path for the built dylib (default: {_DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Build with debug symbols and no optimization",
    )
    args = parser.parse_args()

    build(args.output, debug=args.debug)


if __name__ == "__main__":
    main()
