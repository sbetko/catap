#!/usr/bin/env python3
"""
Low-level Core Audio pipeline lab for catap.

This lab stays close to catap's raw primitives while exposing the newer
capabilities around shared taps and device-stream-targeted taps.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from catap import (  # noqa: E402
    AudioDevice,
    AudioDeviceStream,
    AudioProcess,
    AudioRecorder,
    AudioTap,
    AudioTapNotFoundError,
    TapDescription,
    TapMuteBehavior,
    create_process_tap,
    destroy_process_tap,
    list_audio_devices,
    list_audio_processes,
    list_audio_taps,
)

REFRESH_MS = 250
DEFAULT_DIR = Path.home() / "Desktop"
if not DEFAULT_DIR.exists():
    DEFAULT_DIR = Path.home()

BG = "#1e1e24"
CARD = "#2b2b36"
CARD_ALT = "#353542"
ACCENT = "#5c9eff"
ACCENT_SOFT = "#2c3e50"
BORDER = "#404050"
INK = "#e2e2e6"
MUTED = "#8b8b9e"
LOG_BG = "#18181d"
WARN = "#ff6b6b"
SUCCESS = "#2ed573"

BODY_FONT = ("Avenir Next", 12)
TITLE_FONT = ("Avenir Next", 22, "bold")
SECTION_FONT = ("Avenir Next", 14, "bold")
MONO_FONT = ("Menlo", 11)


def configure_styles(root: tk.Tk) -> None:
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    root.configure(background=BG)

    style.configure(
        ".",
        background=BG,
        foreground=INK,
        fieldbackground=LOG_BG,
        font=BODY_FONT,
    )
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Inner.TFrame", background=CARD_ALT)
    style.configure("Title.TLabel", background=BG, foreground=INK, font=TITLE_FONT)
    style.configure(
        "Section.TLabel",
        background=CARD,
        foreground=INK,
        font=SECTION_FONT,
    )
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=BODY_FONT)
    style.configure(
        "MutedCard.TLabel",
        background=CARD,
        foreground=MUTED,
        font=BODY_FONT,
    )
    style.configure(
        "Card.TLabelframe",
        background=CARD,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
    )
    style.configure(
        "Card.TLabelframe.Label",
        background=CARD,
        foreground=INK,
        font=SECTION_FONT,
    )
    style.configure(
        "Inner.TLabelframe",
        background=CARD_ALT,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
    )
    style.configure(
        "Inner.TLabelframe.Label",
        background=CARD_ALT,
        foreground=INK,
        font=BODY_FONT,
    )
    style.configure(
        "TButton",
        background=CARD_ALT,
        foreground=INK,
        bordercolor=BORDER,
        padding=(10, 6),
    )
    style.configure(
        "Primary.TButton",
        background=ACCENT,
        foreground="#ffffff",
        bordercolor=ACCENT,
        padding=(10, 6),
    )
    style.configure(
        "Danger.TButton",
        background=WARN,
        foreground="#ffffff",
        bordercolor=WARN,
        padding=(10, 6),
    )
    style.configure(
        "Success.TButton",
        background=SUCCESS,
        foreground="#ffffff",
        bordercolor=SUCCESS,
        padding=(10, 6),
    )
    style.configure("TEntry", fieldbackground=LOG_BG, foreground=INK)
    style.configure("TCombobox", fieldbackground=LOG_BG)
    style.configure("TCheckbutton", background=CARD, foreground=INK)
    style.configure("TRadiobutton", background=CARD, foreground=INK)
    style.configure(
        "Treeview",
        background=LOG_BG,
        fieldbackground=LOG_BG,
        foreground=INK,
        bordercolor=BORDER,
        rowheight=26,
    )
    style.configure(
        "Treeview.Heading",
        background=CARD_ALT,
        foreground=INK,
        relief="flat",
        font=("Avenir Next", 11, "bold"),
    )
    style.configure(
        "Lab.TNotebook",
        background=CARD,
        borderwidth=0,
        tabmargins=(0, 0, 0, 0),
    )
    style.configure(
        "Lab.TNotebook.Tab",
        background=CARD_ALT,
        foreground=MUTED,
        borderwidth=0,
        padding=(14, 8),
        font=("Avenir Next", 11, "bold"),
    )
    style.map(
        "Lab.TNotebook.Tab",
        background=[("selected", CARD_ALT)],
        foreground=[("selected", INK)],
    )
    style.map(
        "Treeview",
        background=[("selected", ACCENT_SOFT)],
        foreground=[("selected", "#ffffff")],
    )


def _fmt_sample_rate(value: float) -> str:
    return f"{value / 1000:.1f} kHz"


def _fmt_stream_label(stream: AudioDeviceStream) -> str:
    return (
        f"#{stream.stream_index} · {stream.name} · {stream.num_channels} ch · "
        f"{_fmt_sample_rate(stream.sample_rate)}"
    )


def _fmt_device_label(device: AudioDevice) -> str:
    prefix = "Default Output · " if device.is_default_output else ""
    return f"{prefix}{device.name} [{device.uid}]"


def _fmt_tap_label(tap: AudioTap) -> str:
    route = (
        f"{tap.device_uid} stream {tap.stream}"
        if tap.device_uid is not None and tap.stream is not None
        else "all routes"
    )
    return f"{tap.name} · id {tap.audio_object_id} · {route}"


class Telemetry:
    """Thread-safe counters for the streaming ``on_data`` callback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.buffers = 0
            self.bytes = 0
            self.frames = 0

    def callback(self, data: bytes, num_frames: int) -> None:
        with self._lock:
            self.buffers += 1
            self.bytes += len(data)
            self.frames += num_frames

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "buffers": self.buffers,
                "bytes": self.bytes,
                "frames": self.frames,
            }


class ToneHelper:
    """Launch a deterministic helper tone via ``afplay``."""

    def __init__(self, status: tk.StringVar) -> None:
        self.status = status
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, seconds: float) -> None:
        self.stop(silent=True)
        script = REPO_ROOT / "scripts" / "catap_test_tone.py"
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(script), "--seconds", f"{seconds:.1f}"]
            )
        except OSError as exc:
            self.process = None
            raise OSError(f"Failed to launch helper tone: {exc}") from exc

        self.status.set(
            "Helper tone active via afplay. Refresh processes and target `afplay`."
        )

    def stop(self, *, silent: bool = False) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
            if process.poll() is None:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)

        if not silent:
            self.status.set("Helper tone idle.")

    def poll(self) -> None:
        process = self.process
        if process is not None and process.poll() is not None:
            self.process = None
            self.status.set("Helper tone finished.")


class CoreLabApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("catap Core API Lab")
        self.root.geometry("1500x940")
        self.root.minsize(1260, 760)

        configure_styles(self.root)

        self.processes: list[AudioProcess] = []
        self.target_processes: dict[int, AudioProcess] = {}
        self.visible_taps: list[AudioTap] = []
        self.devices: list[AudioDevice] = []
        self.current_streams: list[AudioDeviceStream] = []

        self.active_tap_id: int | None = None
        self.active_tap_description: TapDescription | None = None
        self.active_tap_owned = False
        self.active_tap_source = "none"

        self.target_mode = tk.StringVar(value="mixdown")
        self.show_idle = tk.BooleanVar(value=False)

        self.tap_name = tk.StringVar(value="Core Lab Tap")
        self.tap_is_mono = tk.BooleanVar(value=False)
        self.tap_is_private = tk.BooleanVar(value=True)
        self.tap_mute_behavior = tk.StringVar(value="UNMUTED")
        self.route_mode = tk.StringVar(value="classic")
        self.selected_device = tk.StringVar(value="")
        self.selected_stream = tk.StringVar(value="")

        self.output_dir = tk.StringVar(value=str(DEFAULT_DIR))
        self.write_wav = tk.BooleanVar(value=True)
        self.enable_callback = tk.BooleanVar(value=True)
        self.max_pending = tk.StringVar(value="256")
        self.helper_tone_seconds = tk.StringVar(value="60")
        self.helper_tone_status = tk.StringVar(value="Helper tone idle.")

        self.tap_status = tk.StringVar(value="Tap idle.")
        self.tap_meta = tk.StringVar(value="No active tap.")
        self.recorder_status = tk.StringVar(value="Recorder idle.")
        self.telemetry_status = tk.StringVar(
            value="[ Telemetry output will appear here ]"
        )

        self.recorder: AudioRecorder | None = None
        self.telemetry = Telemetry()
        self.tone_helper = ToneHelper(self.helper_tone_status)

        self.process_tree: ttk.Treeview
        self.target_listbox: tk.Listbox
        self.tap_listbox: tk.Listbox
        self.device_combo: ttk.Combobox
        self.stream_combo: ttk.Combobox
        self.mono_check: ttk.Checkbutton
        self.btn_delete_shared_tap: ttk.Button
        self.btn_create_tap: ttk.Button
        self.btn_destroy_tap: ttk.Button
        self.btn_start_rec: ttk.Button
        self.btn_stop_rec: ttk.Button

        self._build_ui()
        self._refresh_processes()
        self._refresh_taps()
        self._refresh_devices()
        self._sync_route_controls()
        self.root.after(REFRESH_MS, self._fast_poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 12, 16, 8))
        header.pack(fill=tk.X)
        ttk.Label(header, text="catap Core Audio API Lab", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            header,
            text=(
                "Exercise low-level process taps, shared non-private taps, "
                "device-stream-targeted taps, and raw AudioRecorder startup."
            ),
            style="Muted.TLabel",
        ).pack(anchor="w")

        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)
        for column in range(3):
            main.columnconfigure(column, weight=1, uniform="col")
        main.rowconfigure(0, weight=1)

        self._build_source_panel(main, 0)
        self._build_tap_panel(main, 1)
        self._build_recorder_panel(main, 2)

    def _build_source_panel(self, parent: ttk.Frame, col: int) -> None:
        frame = ttk.LabelFrame(
            parent,
            text="1. Sources",
            style="Card.TLabelframe",
            padding=14,
        )
        frame.grid(row=0, column=col, sticky="nsew", padx=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        process_box = ttk.LabelFrame(
            frame,
            text="Process Targeting",
            style="Inner.TLabelframe",
            padding=10,
        )
        process_box.grid(row=0, column=0, sticky="nsew")
        process_box.columnconfigure(0, weight=1)
        process_box.rowconfigure(3, weight=1)

        ttk.Label(
            process_box,
            text="Choose processes to include or exclude when creating a new tap.",
            style="Inner.TLabelframe.Label",
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        mode_frame = ttk.Frame(process_box, style="Inner.TFrame")
        mode_frame.grid(row=1, column=0, sticky="ew")
        ttk.Radiobutton(
            mode_frame,
            text="Mixdown Selected",
            variable=self.target_mode,
            value="mixdown",
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(
            mode_frame,
            text="Global (Exclude Selected)",
            variable=self.target_mode,
            value="exclude",
        ).pack(side=tk.LEFT)

        filter_frame = ttk.Frame(process_box, style="Inner.TFrame")
        filter_frame.grid(row=2, column=0, sticky="ew", pady=(10, 8))
        ttk.Button(
            filter_frame, text="Refresh", command=self._refresh_processes
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            filter_frame,
            text="Show Idle",
            variable=self.show_idle,
            command=self._refresh_processes,
        ).pack(side=tk.LEFT, padx=10)

        tree_frame = ttk.Frame(process_box, style="Inner.TFrame")
        tree_frame.grid(row=3, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.process_tree = ttk.Treeview(
            tree_frame,
            columns=("state", "name", "pid"),
            show="headings",
            selectmode="extended",
        )
        self.process_tree.heading("state", text="State")
        self.process_tree.heading("name", text="Name")
        self.process_tree.heading("pid", text="PID")
        self.process_tree.column("state", width=60, anchor="w")
        self.process_tree.column("name", width=180, anchor="w")
        self.process_tree.column("pid", width=60, anchor="w")
        self.process_tree.grid(row=0, column=0, sticky="nsew")

        process_scroll = ttk.Scrollbar(
            tree_frame,
            orient=tk.VERTICAL,
            command=self.process_tree.yview,
        )
        process_scroll.grid(row=0, column=1, sticky="ns")
        self.process_tree.configure(yscrollcommand=process_scroll.set)

        ttk.Label(
            process_box,
            text="Selected Processes",
            style="Inner.TLabelframe.Label",
        ).grid(row=4, column=0, sticky="w", pady=(10, 4))

        self.target_listbox = tk.Listbox(
            process_box,
            height=4,
            bg=LOG_BG,
            fg=INK,
            relief=tk.SOLID,
            borderwidth=1,
            highlightthickness=0,
            font=MONO_FONT,
        )
        self.target_listbox.grid(row=5, column=0, sticky="ew")

        target_actions = ttk.Frame(process_box, style="Inner.TFrame")
        target_actions.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(
            target_actions,
            text="Add Selected",
            command=self._add_targets,
        ).pack(side=tk.LEFT)
        ttk.Button(
            target_actions,
            text="Remove",
            command=self._remove_target,
        ).pack(side=tk.LEFT, padx=8)
        ttk.Button(
            target_actions,
            text="Clear",
            command=self._clear_targets,
        ).pack(side=tk.LEFT)

        lower = ttk.Frame(frame, style="Card.TFrame")
        lower.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(0, weight=1)

        tool_tabs = ttk.Notebook(lower, style="Lab.TNotebook")
        tool_tabs.grid(row=0, column=0, sticky="ew")

        tap_box = ttk.Frame(tool_tabs, style="Inner.TFrame", padding=10)
        tap_box.columnconfigure(0, weight=1)
        tap_box.rowconfigure(1, weight=1)
        tap_actions = ttk.Frame(tap_box, style="Inner.TFrame")
        tap_actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tap_actions.columnconfigure(0, weight=1)
        tap_actions.columnconfigure(1, weight=1)
        tap_actions.columnconfigure(2, weight=1)
        ttk.Button(
            tap_actions,
            text="Refresh",
            command=self._refresh_taps,
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            tap_actions,
            text="Attach Selected Tap",
            command=self._attach_selected_tap,
        ).grid(row=0, column=1, sticky="ew", padx=8)
        self.btn_delete_shared_tap = ttk.Button(
            tap_actions,
            text="Delete Tap",
            style="Danger.TButton",
            command=self._delete_selected_tap,
        )
        self.btn_delete_shared_tap.grid(row=0, column=2, sticky="ew")

        tap_list_frame = ttk.Frame(tap_box, style="Inner.TFrame")
        tap_list_frame.grid(row=1, column=0, sticky="nsew")
        tap_list_frame.columnconfigure(0, weight=1)
        tap_list_frame.rowconfigure(0, weight=1)

        self.tap_listbox = tk.Listbox(
            tap_list_frame,
            height=5,
            bg=LOG_BG,
            fg=INK,
            relief=tk.SOLID,
            borderwidth=1,
            highlightthickness=0,
            font=MONO_FONT,
        )
        self.tap_listbox.grid(row=0, column=0, sticky="nsew")

        tap_scroll = ttk.Scrollbar(
            tap_list_frame,
            orient=tk.VERTICAL,
            command=self.tap_listbox.yview,
        )
        tap_scroll.grid(row=0, column=1, sticky="ns")
        self.tap_listbox.configure(yscrollcommand=tap_scroll.set)

        ttk.Label(
            tap_box,
            text="Visible non-private taps from this process or another one.",
            style="Inner.TLabelframe.Label",
            wraplength=360,
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        tone_box = ttk.Frame(tool_tabs, style="Inner.TFrame", padding=10)
        tone_box.columnconfigure(1, weight=1)

        ttk.Label(
            tone_box,
            text="Seconds",
            style="Inner.TLabelframe.Label",
        ).grid(row=0, column=0, sticky="w")
        ttk.Entry(tone_box, textvariable=self.helper_tone_seconds, width=10).grid(
            row=0, column=1, sticky="w", padx=8
        )

        tone_actions = ttk.Frame(tone_box, style="Inner.TFrame")
        tone_actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 6))
        ttk.Button(
            tone_actions,
            text="Start Tone",
            style="Primary.TButton",
            command=self._start_helper_tone,
        ).pack(side=tk.LEFT)
        ttk.Button(
            tone_actions,
            text="Stop Tone",
            style="Danger.TButton",
            command=self._stop_helper_tone,
        ).pack(side=tk.LEFT, padx=8)

        ttk.Label(
            tone_box,
            textvariable=self.helper_tone_status,
            style="Inner.TLabelframe.Label",
            wraplength=360,
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        tool_tabs.add(tap_box, text="Shared Taps")
        tool_tabs.add(tone_box, text="Helper Tone")

    def _build_tap_panel(self, parent: ttk.Frame, col: int) -> None:
        frame = ttk.LabelFrame(
            parent,
            text="2. Tap Builder",
            style="Card.TLabelframe",
            padding=14,
        )
        frame.grid(row=0, column=col, sticky="nsew", padx=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)

        ttk.Label(
            frame,
            text=(
                "Create a new CATapDescription or attach to one of the shared "
                "taps from the Sources column."
            ),
            style="MutedCard.TLabel",
            wraplength=360,
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        desc_box = ttk.LabelFrame(
            frame,
            text="TapDescription",
            style="Inner.TLabelframe",
            padding=10,
        )
        desc_box.grid(row=1, column=0, sticky="ew")
        desc_box.columnconfigure(1, weight=1)

        ttk.Label(desc_box, text="Name", style="Inner.TLabelframe.Label").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(desc_box, textvariable=self.tap_name).grid(
            row=0, column=1, sticky="ew", padx=8, pady=4
        )

        self.mono_check = ttk.Checkbutton(
            desc_box,
            text="Mono Mixdown",
            variable=self.tap_is_mono,
        )
        self.mono_check.grid(row=1, column=1, sticky="w", padx=8, pady=4)

        ttk.Checkbutton(
            desc_box,
            text="Private Tap",
            variable=self.tap_is_private,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(desc_box, text="Mute", style="Inner.TLabelframe.Label").grid(
            row=3, column=0, sticky="w"
        )
        ttk.Combobox(
            desc_box,
            values=[behavior.name for behavior in TapMuteBehavior],
            state="readonly",
            textvariable=self.tap_mute_behavior,
        ).grid(row=3, column=1, sticky="ew", padx=8, pady=4)

        route_box = ttk.LabelFrame(
            frame,
            text="Routing",
            style="Inner.TLabelframe",
            padding=10,
        )
        route_box.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        route_box.columnconfigure(1, weight=1)

        ttk.Radiobutton(
            route_box,
            text="Classic process mixdown/global tap",
            variable=self.route_mode,
            value="classic",
            command=self._sync_route_controls,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(
            route_box,
            text="Target a hardware output stream",
            variable=self.route_mode,
            value="device-stream",
            command=self._sync_route_controls,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 8))

        ttk.Label(
            route_box,
            text="Device",
            style="Inner.TLabelframe.Label",
        ).grid(row=2, column=0, sticky="w")
        device_row = ttk.Frame(route_box, style="Inner.TFrame")
        device_row.grid(row=2, column=1, sticky="ew", padx=8, pady=4)
        device_row.columnconfigure(0, weight=1)
        self.device_combo = ttk.Combobox(
            device_row,
            textvariable=self.selected_device,
            state="readonly",
        )
        self.device_combo.grid(row=0, column=0, sticky="ew")
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        ttk.Button(
            device_row,
            text="Refresh",
            command=self._refresh_devices,
        ).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(
            route_box,
            text="Stream",
            style="Inner.TLabelframe.Label",
        ).grid(row=3, column=0, sticky="w")
        self.stream_combo = ttk.Combobox(
            route_box,
            textvariable=self.selected_stream,
            state="readonly",
        )
        self.stream_combo.grid(row=3, column=1, sticky="ew", padx=8, pady=4)

        ttk.Label(
            route_box,
            text=(
                "Stream-targeted taps use the selected stream's native format. "
                "Mono mixdown is disabled in that mode."
            ),
            style="Inner.TLabelframe.Label",
            wraplength=360,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        actions = ttk.Frame(frame, style="Card.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(16, 12))
        self.btn_create_tap = ttk.Button(
            actions,
            text="Create Tap",
            style="Primary.TButton",
            command=self._create_tap,
        )
        self.btn_create_tap.pack(side=tk.LEFT)
        self.btn_destroy_tap = ttk.Button(
            actions,
            text="Destroy Tap",
            style="Danger.TButton",
            command=self._destroy_or_detach_tap,
            state=tk.DISABLED,
        )
        self.btn_destroy_tap.pack(side=tk.LEFT, padx=10)

        active_box = ttk.LabelFrame(
            frame,
            text="Active Tap",
            style="Inner.TLabelframe",
            padding=10,
        )
        active_box.grid(row=4, column=0, sticky="nsew")
        active_box.columnconfigure(0, weight=1)
        active_box.rowconfigure(1, weight=1)

        ttk.Label(
            active_box,
            textvariable=self.tap_status,
            style="Inner.TLabelframe.Label",
            wraplength=360,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            active_box,
            textvariable=self.tap_meta,
            font=MONO_FONT,
            background=CARD_ALT,
            foreground=MUTED,
            justify=tk.LEFT,
            anchor="nw",
            wraplength=360,
        ).grid(row=1, column=0, sticky="nsew", pady=(10, 0))

    def _build_recorder_panel(self, parent: ttk.Frame, col: int) -> None:
        frame = ttk.LabelFrame(
            parent,
            text="3. Audio Recorder",
            style="Card.TLabelframe",
            padding=14,
        )
        frame.grid(row=0, column=col, sticky="nsew", padx=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        ttk.Label(
            frame,
            text=(
                "Create an aggregate device for the active tap and start the "
                "raw IO proc-backed recorder."
            ),
            style="MutedCard.TLabel",
            wraplength=360,
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        opts = ttk.LabelFrame(
            frame,
            text="Recorder Options",
            style="Inner.TLabelframe",
            padding=10,
        )
        opts.grid(row=1, column=0, sticky="ew")
        opts.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            opts,
            text="Write WAV File",
            variable=self.write_wav,
        ).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Checkbutton(
            opts,
            text="Enable Streaming Callback",
            variable=self.enable_callback,
        ).grid(row=1, column=0, sticky="w", pady=4)

        ttk.Label(
            opts,
            text="Max Pending",
            style="Inner.TLabelframe.Label",
        ).grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(opts, textvariable=self.max_pending, width=10).grid(
            row=2, column=1, sticky="w", padx=8, pady=4
        )

        ttk.Label(
            opts,
            text="Output Folder",
            style="Inner.TLabelframe.Label",
        ).grid(row=3, column=0, sticky="w", pady=4)
        out_row = ttk.Frame(opts, style="Inner.TFrame")
        out_row.grid(row=3, column=1, sticky="ew", padx=8, pady=4)
        out_row.columnconfigure(0, weight=1)
        ttk.Entry(out_row, textvariable=self.output_dir).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(out_row, text="...", width=3, command=self._choose_dir).grid(
            row=0, column=1, padx=(4, 0)
        )

        actions = ttk.Frame(frame, style="Card.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(16, 12))
        self.btn_start_rec = ttk.Button(
            actions,
            text="Start Recording",
            style="Primary.TButton",
            command=self._start_recorder,
            state=tk.DISABLED,
        )
        self.btn_start_rec.pack(side=tk.LEFT)
        self.btn_stop_rec = ttk.Button(
            actions,
            text="Stop Recording",
            style="Danger.TButton",
            command=self._stop_recorder,
            state=tk.DISABLED,
        )
        self.btn_stop_rec.pack(side=tk.LEFT, padx=10)

        live_box = ttk.LabelFrame(
            frame,
            text="Live Metrics",
            style="Inner.TLabelframe",
            padding=10,
        )
        live_box.grid(row=3, column=0, sticky="nsew")
        live_box.columnconfigure(0, weight=1)
        live_box.rowconfigure(1, weight=1)

        ttk.Label(
            live_box,
            textvariable=self.recorder_status,
            style="Inner.TLabelframe.Label",
            wraplength=360,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            live_box,
            textvariable=self.telemetry_status,
            font=MONO_FONT,
            background=CARD_ALT,
            foreground=SUCCESS,
            justify=tk.LEFT,
            anchor="nw",
            wraplength=360,
        ).grid(row=1, column=0, sticky="nsew", pady=(10, 0))

    def _refresh_processes(self) -> None:
        try:
            all_processes = list_audio_processes()
        except Exception as exc:
            messagebox.showerror("Refresh Error", f"Failed to list processes:\n{exc}")
            return

        self.process_tree.delete(*self.process_tree.get_children())
        self.processes.clear()

        for process in all_processes:
            if not self.show_idle.get() and not process.is_outputting:
                continue
            self.processes.append(process)
            state_text = "Live" if process.is_outputting else "Idle"
            self.process_tree.insert(
                "",
                tk.END,
                text=str(process.pid),
                values=(state_text, process.name, process.pid),
            )

    def _add_targets(self) -> None:
        for item in self.process_tree.selection():
            pid = int(self.process_tree.item(item, "text"))
            process = next(
                (value for value in self.processes if value.pid == pid),
                None,
            )
            if process is not None:
                self.target_processes[process.pid] = process
        self._sync_targets_ui()

    def _remove_target(self) -> None:
        selection = self.target_listbox.curselection()
        if not selection:
            return
        keys = list(self.target_processes.keys())
        index = selection[0]
        if index < len(keys):
            self.target_processes.pop(keys[index], None)
        self._sync_targets_ui()

    def _clear_targets(self) -> None:
        self.target_processes.clear()
        self._sync_targets_ui()

    def _sync_targets_ui(self) -> None:
        self.target_listbox.delete(0, tk.END)
        for process in self.target_processes.values():
            self.target_listbox.insert(
                tk.END,
                f"{process.name} (pid {process.pid}, obj {process.audio_object_id})",
            )

    def _refresh_taps(self) -> None:
        try:
            self.visible_taps = list_audio_taps()
        except Exception as exc:
            messagebox.showerror("Tap Refresh Failed", str(exc))
            return

        self.tap_listbox.delete(0, tk.END)
        for tap in self.visible_taps:
            self.tap_listbox.insert(tk.END, _fmt_tap_label(tap))

    def _selected_visible_tap(self) -> AudioTap | None:
        selection = self.tap_listbox.curselection()
        if not selection:
            return None
        index = selection[0]
        if index >= len(self.visible_taps):
            return None
        return self.visible_taps[index]

    def _refresh_devices(self) -> None:
        try:
            self.devices = list_audio_devices()
        except Exception as exc:
            messagebox.showerror("Device Refresh Failed", str(exc))
            return

        labels = [
            _fmt_device_label(device)
            for device in self.devices
            if device.output_streams
        ]
        self.device_combo["values"] = labels

        if not labels:
            self.selected_device.set("")
            self.current_streams = []
            self.stream_combo["values"] = []
            self.selected_stream.set("")
            return

        if self.selected_device.get() not in labels:
            default_device = next(
                (
                    _fmt_device_label(device)
                    for device in self.devices
                    if device.output_streams and device.is_default_output
                ),
                labels[0],
            )
            self.selected_device.set(default_device)

        self._update_stream_choices()

    def _on_device_selected(self, _event: object | None = None) -> None:
        self._update_stream_choices()

    def _update_stream_choices(self) -> None:
        device = self._selected_device()
        self.current_streams = list(device.output_streams) if device is not None else []
        labels = [_fmt_stream_label(stream) for stream in self.current_streams]
        self.stream_combo["values"] = labels
        if labels:
            if self.selected_stream.get() not in labels:
                self.selected_stream.set(labels[0])
        else:
            self.selected_stream.set("")

    def _selected_device(self) -> AudioDevice | None:
        target = self.selected_device.get()
        for device in self.devices:
            if _fmt_device_label(device) == target:
                return device
        return None

    def _selected_stream(self) -> AudioDeviceStream | None:
        target = self.selected_stream.get()
        for stream in self.current_streams:
            if _fmt_stream_label(stream) == target:
                return stream
        return None

    def _sync_route_controls(self) -> None:
        device_stream_mode = self.route_mode.get() == "device-stream"
        state = "readonly" if device_stream_mode else tk.DISABLED
        self.device_combo.config(state=state)
        self.stream_combo.config(state=state)
        self.mono_check.config(
            state=tk.DISABLED if device_stream_mode else tk.NORMAL
        )
        if device_stream_mode:
            self.tap_is_mono.set(False)

    def _build_description(self) -> TapDescription:
        audio_object_ids = [
            process.audio_object_id for process in self.target_processes.values()
        ]

        if self.route_mode.get() == "device-stream":
            stream = self._selected_stream()
            if stream is None:
                raise ValueError("Pick a device stream before creating the tap.")
            if self.target_mode.get() == "mixdown":
                description = TapDescription.of_processes_for_device_stream(
                    audio_object_ids,
                    stream,
                )
            else:
                description = TapDescription.excluding_processes_for_device_stream(
                    audio_object_ids,
                    stream,
                )
        elif self.target_mode.get() == "mixdown":
            description = (
                TapDescription.mono_mixdown_of_processes(audio_object_ids)
                if self.tap_is_mono.get()
                else TapDescription.stereo_mixdown_of_processes(audio_object_ids)
            )
        else:
            description = (
                TapDescription.mono_global_tap_excluding(audio_object_ids)
                if self.tap_is_mono.get()
                else TapDescription.stereo_global_tap_excluding(audio_object_ids)
            )

        description.name = self.tap_name.get().strip() or "Core Lab Tap"
        description.is_private = self.tap_is_private.get()
        description.mute_behavior = TapMuteBehavior[self.tap_mute_behavior.get()]
        return description

    def _create_tap(self) -> None:
        if self.active_tap_id is not None:
            messagebox.showwarning(
                "Tap Exists",
                "A tap is already active. Destroy or detach it first.",
            )
            return

        try:
            description = self._build_description()
            tap_id = create_process_tap(description)
        except Exception as exc:
            messagebox.showerror("Tap Creation Failed", str(exc))
            return

        self._activate_tap(
            tap_id=tap_id,
            description=description,
            owned=True,
            source=f"Created tap `{description.name}`",
        )
        self._refresh_taps()

    def _attach_selected_tap(self) -> None:
        if self.active_tap_id is not None:
            messagebox.showwarning(
                "Tap Exists",
                "A tap is already active. Destroy or detach it first.",
            )
            return

        selection = self.tap_listbox.curselection()
        if not selection:
            messagebox.showinfo(
                "No Tap Selected",
                "Choose a visible shared tap from the list first.",
            )
            return

        tap = self.visible_taps[selection[0]]
        self._activate_tap(
            tap_id=tap.audio_object_id,
            description=tap.description,
            owned=False,
            source=f"Attached shared tap `{tap.name}`",
        )

    def _delete_selected_tap(self) -> None:
        tap = self._selected_visible_tap()
        if tap is None:
            messagebox.showinfo(
                "No Tap Selected",
                "Choose a visible tap from the list first.",
            )
            return

        if (
            self.recorder is not None
            and self.recorder.is_recording
            and self.active_tap_id == tap.audio_object_id
        ):
            messagebox.showwarning(
                "Recorder Active",
                "Stop the recorder before deleting the active tap.",
            )
            return

        confirmed = messagebox.askyesno(
            "Delete Tap",
            (
                f"Delete visible tap '{tap.name}' (id {tap.audio_object_id})?\n\n"
                "This destroys the tap for every process that can see it."
            ),
        )
        if not confirmed:
            return

        try:
            destroy_process_tap(tap.audio_object_id)
        except Exception as exc:
            messagebox.showerror("Tap Deletion Failed", str(exc))
            return

        if self.active_tap_id == tap.audio_object_id:
            self._clear_active_tap(f"Deleted visible tap {tap.audio_object_id}.")

        self._refresh_taps()

    def _activate_tap(
        self,
        *,
        tap_id: int,
        description: TapDescription,
        owned: bool,
        source: str,
    ) -> None:
        self.active_tap_id = tap_id
        self.active_tap_description = description
        self.active_tap_owned = owned
        self.active_tap_source = source

        ownership = "owned" if owned else "shared"
        route = (
            f"{description.device_uid} stream {description.stream}"
            if description.device_uid is not None and description.stream is not None
            else ("global exclude" if description.is_exclusive else "process mixdown")
        )
        self.tap_status.set(f"{source} ({ownership}).")
        self.tap_meta.set(
            f"id: {tap_id}\n"
            f"name: {description.name}\n"
            f"route: {route}\n"
            f"private: {description.is_private}\n"
            f"mute: {description.mute_behavior.name}"
        )
        self.btn_create_tap.config(state=tk.DISABLED)
        self.btn_destroy_tap.config(
            state=tk.NORMAL,
            text="Destroy Tap" if owned else "Detach Tap",
        )
        self.btn_start_rec.config(state=tk.NORMAL)

    def _clear_active_tap(self, status: str = "Tap idle.") -> None:
        self.active_tap_id = None
        self.active_tap_description = None
        self.active_tap_owned = False
        self.active_tap_source = "none"
        self.tap_status.set(status)
        self.tap_meta.set("No active tap.")
        self.btn_create_tap.config(state=tk.NORMAL)
        self.btn_destroy_tap.config(state=tk.DISABLED, text="Destroy Tap")
        self.btn_start_rec.config(state=tk.DISABLED)

    def _destroy_or_detach_tap(self) -> None:
        if self.recorder is not None and self.recorder.is_recording:
            messagebox.showwarning(
                "Recorder Active",
                "Stop the recorder before destroying or detaching the tap.",
            )
            return

        tap_id = self.active_tap_id
        if tap_id is None:
            return

        if self.active_tap_owned:
            try:
                destroy_process_tap(tap_id)
            except Exception as exc:
                messagebox.showerror("Tap Destruction Failed", str(exc))
                return
            self._clear_active_tap(f"Destroyed tap {tap_id}.")
            self._refresh_taps()
            return

        self._clear_active_tap(f"Detached shared tap {tap_id}.")

    def _choose_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir.get())
        if selected:
            self.output_dir.set(selected)

    def _start_recorder(self) -> None:
        if self.active_tap_id is None:
            return
        if self.recorder is not None and self.recorder.is_recording:
            return
        if not self.write_wav.get() and not self.enable_callback.get():
            messagebox.showerror(
                "Config Error",
                "Enable WAV writing or the streaming callback before recording.",
            )
            return

        output_path = None
        if self.write_wav.get():
            output_path = (
                Path(self.output_dir.get()) / f"core_lab_out_{int(time.time())}.wav"
            )

        try:
            max_pending = int(self.max_pending.get())
        except ValueError:
            max_pending = 256

        self.telemetry.reset()
        on_data = self.telemetry.callback if self.enable_callback.get() else None

        try:
            recorder = AudioRecorder(
                tap_id=self.active_tap_id,
                output_path=output_path,
                on_data=on_data,
                max_pending_buffers=max_pending,
            )
            recorder.start()
        except AudioTapNotFoundError as exc:
            if not self.active_tap_owned:
                self._clear_active_tap(str(exc))
                self._refresh_taps()
            messagebox.showerror("Recorder Failed", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Recorder Failed", str(exc))
            return

        self.recorder = recorder
        self.recorder_status.set(
            f"Recorder active on tap {self.active_tap_id}."
        )
        self.btn_start_rec.config(state=tk.DISABLED)
        self.btn_stop_rec.config(state=tk.NORMAL)
        self.btn_destroy_tap.config(state=tk.DISABLED)

    def _stop_recorder(self) -> None:
        recorder = self.recorder
        if recorder is None or not recorder.is_recording:
            return

        try:
            recorder.stop()
        except Exception as exc:
            messagebox.showerror("Recorder Stop Failed", str(exc))
            return

        self.recorder = None
        self.recorder_status.set("Recorder idle.")
        self.btn_stop_rec.config(state=tk.DISABLED)
        self.btn_destroy_tap.config(
            state=tk.NORMAL if self.active_tap_id is not None else tk.DISABLED,
            text="Destroy Tap" if self.active_tap_owned else "Detach Tap",
        )
        if self.active_tap_id is not None:
            self.btn_start_rec.config(state=tk.NORMAL)

    def _start_helper_tone(self) -> None:
        try:
            seconds = float(self.helper_tone_seconds.get())
            if seconds <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Helper Tone Error",
                "Seconds must be a positive number.",
            )
            return

        try:
            self.tone_helper.start(seconds)
        except Exception as exc:
            messagebox.showerror("Helper Tone Error", str(exc))
            return

        self.root.after(400, self._refresh_processes)

    def _stop_helper_tone(self) -> None:
        self.tone_helper.stop()
        self.root.after(200, self._refresh_processes)

    def _fast_poll(self) -> None:
        self.tone_helper.poll()
        recorder = self.recorder
        if recorder is not None and recorder.is_recording:
            snap = self.telemetry.snapshot()
            self.telemetry_status.set(
                f"buffers: {snap['buffers']}  |  "
                f"frames: {snap['frames']}  |  "
                f"bytes: {snap['bytes'] / 1024:.1f} KB"
            )
        self.root.after(REFRESH_MS, self._fast_poll)

    def _on_close(self) -> None:
        self.tone_helper.stop(silent=True)
        if self.recorder is not None and self.recorder.is_recording:
            with contextlib.suppress(Exception):
                self.recorder.stop()
        if self.active_tap_owned and self.active_tap_id is not None:
            with contextlib.suppress(Exception):
                destroy_process_tap(self.active_tap_id)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = CoreLabApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app._on_close()


if __name__ == "__main__":
    main()
