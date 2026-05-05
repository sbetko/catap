"""Build hooks for catap."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

if sys.platform == "darwin":
    os.environ.setdefault("MACOSX_DEPLOYMENT_TARGET", "14.2")

from setuptools import setup
from setuptools.command.bdist_wheel import bdist_wheel as _bdist_wheel
from setuptools.command.build_py import build_py as _build_py


class build_py(_build_py):
    """Build Python modules and place the native dylib beside them."""

    def run(self) -> None:
        super().run()
        self._build_native_coreaudio()

    def _build_native_coreaudio(self) -> None:
        output = (
            Path(self.build_lib)
            / "catap"
            / "native"
            / "libcatap_coreaudio.dylib"
        )
        command = [
            sys.executable,
            "scripts/build_native_coreaudio.py",
            "--output",
            str(output),
        ]
        self.announce(f"building native CoreAudio dylib: {output}", level=2)
        subprocess.run(command, check=True)


class bdist_wheel(_bdist_wheel):
    """Mark wheels as platform wheels while keeping the Python tag generic."""

    def finalize_options(self) -> None:
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self) -> tuple[str, str, str]:
        _python, _abi, platform_tag = super().get_tag()
        if sys.platform == "darwin":
            platform_tag = "macosx_14_0_universal2"
        return "py3", "none", platform_tag


setup(
    cmdclass={
        "build_py": build_py,
        "bdist_wheel": bdist_wheel,
    },
)
