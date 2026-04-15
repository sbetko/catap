"""Runtime compatibility helpers for catap."""

from __future__ import annotations

import platform

_MINIMUM_MACOS_VERSION = (14, 2)


def _parse_version(version: str) -> tuple[int, ...]:
    """Parse a dotted version string into an integer tuple."""
    parts: list[int] = []

    for part in version.split("."):
        digits = ""
        for char in part:
            if char.isdigit():
                digits += char
            else:
                break

        if not digits:
            break

        parts.append(int(digits))

    return tuple(parts)


def _version_at_least(version: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    """Return True when version is greater than or equal to minimum."""
    length = max(len(version), len(minimum))
    normalized_version = version + (0,) * (length - len(version))
    normalized_minimum = minimum + (0,) * (length - len(minimum))
    return normalized_version >= normalized_minimum


def get_runtime_support_error() -> str | None:
    """Return a human-friendly runtime support error, if any."""
    if platform.system() != "Darwin":
        return "catap only supports macOS 14.2 or later."

    macos_version = platform.mac_ver()[0]
    parsed_version = _parse_version(macos_version)

    if not parsed_version:
        return "catap only supports macOS 14.2 or later."

    if not _version_at_least(parsed_version, _MINIMUM_MACOS_VERSION):
        return (
            "catap requires macOS 14.2 or later. "
            f"Detected macOS {macos_version}."
        )

    return None


def ensure_supported_runtime() -> None:
    """Raise a RuntimeError when catap is used on an unsupported platform."""
    error = get_runtime_support_error()
    if error is not None:
        raise RuntimeError(error)
