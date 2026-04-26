"""Shared helpers for recorder, worker, and session internals."""

from __future__ import annotations

import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeAlias

_RecordingFailure: TypeAlias = OSError | RuntimeError

_DEFAULT_MAX_PENDING_BUFFERS = 256


def _validate_recording_target(
    output_path: str | Path | None,
    on_data: Callable[[bytes, int], None] | None,
) -> Path | None:
    """Normalize the recording target and reject target-less captures."""
    normalized_output_path = Path(output_path) if output_path else None
    if normalized_output_path is None and on_data is None:
        raise ValueError(
            "output_path must be provided unless on_data is set for streaming mode"
        )
    return normalized_output_path


def _validate_max_pending_buffers(value: int) -> int:
    """Validate and normalize the recorder queue bound."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("max_pending_buffers must be an integer")
    if value <= 0:
        raise ValueError("max_pending_buffers must be greater than 0")
    return value


def _add_secondary_failure(
    primary: BaseException, summary: str, secondary: BaseException
) -> None:
    """Attach a secondary failure's traceback to ``primary`` as a note."""
    primary.add_note(
        f"{summary}:\n{''.join(traceback.format_exception(secondary)).rstrip()}"
    )


def _combine_errors(
    summary: str,
    errors: Sequence[_RecordingFailure],
) -> _RecordingFailure:
    """Annotate the primary error with summary and secondary tracebacks."""
    primary = errors[0]
    primary.add_note(summary)

    for error in errors[1:]:
        primary.add_note(
            "Additional cleanup failure:\n"
            f"{''.join(traceback.format_exception(error)).rstrip()}"
        )

    return primary


def _translate_exception(
    error_type: type[OSError] | type[RuntimeError],
    message: str,
    cause: Exception,
) -> _RecordingFailure:
    """Create an exception with an explicit cause chain."""
    try:
        raise error_type(message) from cause
    except error_type as wrapped:
        return wrapped
