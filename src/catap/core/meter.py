"""VU meter visualization for audio levels."""

from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

# Rich is an optional dependency
try:
    from rich.console import Console
    from rich.live import Live
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


@dataclass
class ChannelLevels:
    """Audio levels for a single channel."""

    rms_db: float
    peak_db: float


def calculate_levels(
    data: bytes,
    num_channels: int,
    is_float: bool = True,
) -> list[ChannelLevels]:
    """
    Calculate RMS and peak levels for each channel.

    Args:
        data: Raw audio data (float32 interleaved)
        num_channels: Number of audio channels
        is_float: True if data is float32 format

    Returns:
        List of ChannelLevels, one per channel
    """
    if not is_float:
        raise NotImplementedError("Only float32 format supported")

    if len(data) == 0:
        return [ChannelLevels(rms_db=-96.0, peak_db=-96.0) for _ in range(num_channels)]

    num_samples = len(data) // 4
    if num_samples == 0:
        return [ChannelLevels(rms_db=-96.0, peak_db=-96.0) for _ in range(num_channels)]

    samples = struct.unpack(f"<{num_samples}f", data)

    # Deinterleave channels
    channels: list[list[float]] = [[] for _ in range(num_channels)]
    for i, sample in enumerate(samples):
        channels[i % num_channels].append(sample)

    levels = []
    for channel_samples in channels:
        if not channel_samples:
            levels.append(ChannelLevels(rms_db=-96.0, peak_db=-96.0))
            continue

        # Calculate RMS
        sum_squares = sum(s * s for s in channel_samples)
        rms = math.sqrt(sum_squares / len(channel_samples))
        rms_db = 20 * math.log10(max(rms, 1e-10))

        # Calculate peak
        peak = max(abs(s) for s in channel_samples)
        peak_db = 20 * math.log10(max(peak, 1e-10))

        levels.append(ChannelLevels(rms_db=rms_db, peak_db=peak_db))

    return levels


class VUMeter:
    """
    Real-time VU meter display using rich library.

    Displays live audio level bars in the terminal with color coding:
    - Green: Normal levels (< -12 dB)
    - Yellow: Moderate levels (-12 to -3 dB)
    - Red: High levels (> -3 dB)

    Usage:
        meter = VUMeter(num_channels=2, duration_callback=lambda: recorder.duration_seconds)

        # Use as context manager for display
        with meter:
            recorder = AudioRecorder(tap_id, "out.wav", on_data=meter.update)
            recorder.start()
            time.sleep(10)
            recorder.stop()
    """

    # Display constants
    BAR_WIDTH = 40
    MIN_DB = -60.0
    MAX_DB = 0.0

    def __init__(
        self,
        num_channels: int = 2,
        update_fps: float = 15.0,
        duration_callback: Callable[[], float] | None = None,
    ) -> None:
        """
        Initialize the VU meter.

        Args:
            num_channels: Number of audio channels (1=mono, 2=stereo)
            update_fps: Target refresh rate for display
            duration_callback: Optional callback that returns recording duration

        Raises:
            ImportError: If rich library is not installed
        """
        if not RICH_AVAILABLE:
            raise ImportError(
                "VU meter requires 'rich' library. Install with: pip install rich"
            )

        self.num_channels = num_channels
        self._update_interval = 1.0 / update_fps
        self._duration_callback = duration_callback

        # Level state (updated atomically)
        self._levels: list[ChannelLevels] = [
            ChannelLevels(rms_db=-96.0, peak_db=-96.0) for _ in range(num_channels)
        ]
        self._lock = threading.Lock()

        # Rich display
        self._console = Console(stderr=True)  # Output to stderr, not stdout
        self._live: Live | None = None
        self._running = False
        self._render_thread: threading.Thread | None = None

    def update(self, data: bytes, num_frames: int) -> None:
        """
        Update meter with new audio data.

        Called as the on_data callback from AudioRecorder.

        Args:
            data: Raw audio data (float32)
            num_frames: Number of audio frames
        """
        try:
            levels = calculate_levels(data, self.num_channels)
            with self._lock:
                self._levels = levels
        except Exception:
            # Don't crash the audio thread on meter errors
            pass

    def _render(self) -> Text:
        """Render the meter display."""
        text = Text()

        with self._lock:
            levels = list(self._levels)

        # Channel names
        if self.num_channels == 1:
            channel_names = ["M"]  # Mono
        elif self.num_channels == 2:
            channel_names = ["L", "R"]  # Stereo
        else:
            channel_names = [str(i) for i in range(self.num_channels)]

        for name, level in zip(channel_names, levels):
            # Calculate bar fill
            db = max(self.MIN_DB, min(self.MAX_DB, level.rms_db))
            fill_ratio = (db - self.MIN_DB) / (self.MAX_DB - self.MIN_DB)
            filled = int(fill_ratio * self.BAR_WIDTH)

            # Color based on level
            if db > -3:
                color = "red"
            elif db > -12:
                color = "yellow"
            else:
                color = "green"

            # Build bar with Unicode blocks
            bar = "\u2588" * filled + "\u2591" * (self.BAR_WIDTH - filled)
            text.append(
                f"\u2595{bar}\u258f {name} {level.rms_db:6.1f} dB\n", style=color
            )

        # Add duration if callback provided
        if self._duration_callback:
            try:
                duration = self._duration_callback()
                minutes = int(duration // 60)
                seconds = int(duration % 60)
                text.append(f"Duration: {minutes:02d}:{seconds:02d}")
            except Exception:
                pass

        return text

    def _render_loop(self) -> None:
        """Background thread for rendering updates."""
        while self._running:
            if self._live:
                try:
                    self._live.update(self._render())
                except Exception:
                    pass
            time.sleep(self._update_interval)

    def __enter__(self) -> "VUMeter":
        """Start the live display."""
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=15,
            transient=True,
        )
        self._live.__enter__()
        self._running = True
        self._render_thread = threading.Thread(target=self._render_loop, daemon=True)
        self._render_thread.start()
        return self

    def __exit__(self, *args) -> None:
        """Stop the live display."""
        self._running = False
        if self._render_thread:
            self._render_thread.join(timeout=0.5)
        if self._live:
            self._live.__exit__(*args)
