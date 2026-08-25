"""Drift-compensation configuration for aggregate-device capture."""

from __future__ import annotations

from enum import IntEnum


class DriftCompensationQuality(IntEnum):
    """Core Audio quality level for aggregate-device tap drift compensation.

    Higher levels can improve resampling quality at additional CPU cost. Pass
    one of these values to a recorder or recording-session constructor; omit
    the option to leave Core Audio's default quality unchanged.
    """

    MINIMUM = 0
    LOW = 0x20
    MEDIUM = 0x40
    HIGH = 0x60
    MAXIMUM = 0x7F


def _validate_drift_compensation_quality(
    quality: DriftCompensationQuality | None,
) -> DriftCompensationQuality | None:
    """Require the public enum rather than accepting arbitrary integers."""
    if quality is not None and type(quality) is not DriftCompensationQuality:
        raise TypeError(
            "drift_compensation_quality must be a "
            "DriftCompensationQuality value or None"
        )
    return quality


__all__ = ["DriftCompensationQuality"]
