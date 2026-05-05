"""Public audio-buffer metadata objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SampleType = Literal["float", "signed_integer"]


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioStreamFormat:
    """Validated native PCM format for buffers delivered by ``catap``."""

    sample_rate: float
    num_channels: int
    bits_per_sample: int
    sample_type: SampleType
    format_id: str

    @property
    def bytes_per_sample(self) -> int:
        """Bytes per channel sample."""
        return self.bits_per_sample // 8

    @property
    def bytes_per_frame(self) -> int:
        """Total bytes per interleaved frame across all channels."""
        return self.num_channels * self.bytes_per_sample

    @property
    def is_float(self) -> bool:
        """True for floating-point PCM samples."""
        return self.sample_type == "float"

    @property
    def is_signed_integer(self) -> bool:
        """True for signed integer PCM samples."""
        return self.sample_type == "signed_integer"


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioBuffer:
    """One native PCM callback buffer.

    ``data`` is immutable bytes and is safe to retain after the callback returns.
    """

    data: bytes
    frame_count: int
    format: AudioStreamFormat
    input_sample_time: float | None = None

    @property
    def byte_count(self) -> int:
        """Number of bytes in ``data``."""
        return len(self.data)

    @property
    def duration_seconds(self) -> float:
        """Duration represented by this buffer."""
        return self.frame_count / self.format.sample_rate


def _format_id_to_fourcc(format_id: int) -> str:
    """Return a FourCC string for a Core Audio format id."""
    # Core Audio FourCCs are big-endian integers. Callback-visible formats
    # currently rely on recorder validation restricting this to printable LPCM.
    return format_id.to_bytes(4, "big", signed=False).decode("latin-1")
