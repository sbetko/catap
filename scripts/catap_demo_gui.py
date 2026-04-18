#!/usr/bin/env python3
"""Interactive GUI demo for exercising catap's public API."""

from __future__ import annotations

import contextlib
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from catap import (  # noqa: E402
    AmbiguousAudioProcessError,
    AudioProcess,
    AudioRecorder,
    RecordingSession,
    TapDescription,
    TapMuteBehavior,
    create_process_tap,
    destroy_process_tap,
    find_process_by_name,
    list_audio_processes,
    record_process,
    record_system_audio,
)


class CallbackTelemetry:
    """Thread-safe counters for `on_data` callback activity."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._buffer_count = 0
            self._byte_count = 0
            self._frame_count = 0
            self._last_buffer_size = 0
            self._last_update_time = 0.0
            self._frozen_reference_time: float | None = None

    def callback(self, data: bytes, num_frames: int) -> None:
        with self._lock:
            self._buffer_count += 1
            self._byte_count += len(data)
            self._frame_count += num_frames
            self._last_buffer_size = len(data)
            self._last_update_time = time.time()
            self._frozen_reference_time = None

    def freeze(self) -> None:
        with self._lock:
            if self._frozen_reference_time is None:
                self._frozen_reference_time = time.time()

    def snapshot(self) -> tuple[int, int, int, int, float, float]:
        with self._lock:
            reference_time = self._frozen_reference_time
            if reference_time is None:
                reference_time = time.time()
            return (
                self._buffer_count,
                self._byte_count,
                self._frame_count,
                self._last_buffer_size,
                self._last_update_time,
                reference_time,
            )


class CatapDemoApp:
    """Tkinter desktop harness for trying catap end to end."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("catap Demo")
        self.root.geometry("1380x920")
        self.root.minsize(1180, 760)

        style = ttk.Style(self.root)
        self._listbox_background = (
            style.lookup("TEntry", "fieldbackground") or "#fbfaf6"
        )
        self._listbox_foreground = style.lookup("TLabel", "foreground") or "#1f2933"
        self._listbox_placeholder_foreground = "#7a7468"
        self._live_value_wraplength = 320

        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._processes: list[AudioProcess] = []
        self._process_index: dict[str, AudioProcess] = {}

        default_dir = Path.home() / "Desktop"
        if not default_dir.exists():
            default_dir = Path.home()

        self.browser_query_var = tk.StringVar()
        self.browser_show_all_var = tk.BooleanVar(value=False)
        self.browser_status_var = tk.StringVar(value="Ready.")

        self.high_mode_var = tk.StringVar(value="app")
        self.high_app_query_var = tk.StringVar()
        self.high_write_file_var = tk.BooleanVar(value=True)
        self.high_output_path_var = tk.StringVar(
            value=str(default_dir / "catap-demo-high.wav")
        )
        self.high_duration_var = tk.StringVar(value="10")
        self.high_enable_callback_var = tk.BooleanVar(value=True)
        self.high_mute_var = tk.BooleanVar(value=False)
        self.high_max_pending_var = tk.StringVar(value="256")
        self.high_status_var = tk.StringVar(value="Idle.")
        self.high_session_state_vars = {
            "source": tk.StringVar(value="n/a"),
            "tap_id": tk.StringVar(value="n/a"),
            "active": tk.StringVar(value="False"),
            "output": tk.StringVar(value="n/a"),
            "excluded": tk.StringVar(value="None"),
        }
        self.high_recorder_state_vars = {
            "frames": tk.StringVar(value="0"),
            "duration": tk.StringVar(value="0.00s"),
            "format": tk.StringVar(value="n/a"),
        }
        self.high_callback_state_vars = {
            "buffers": tk.StringVar(value="0"),
            "frames": tk.StringVar(value="0"),
            "bytes": tk.StringVar(value="0"),
            "last_size": tk.StringVar(value="0"),
            "last_update": tk.StringVar(value="n/a"),
        }
        self.high_excluded_processes: list[AudioProcess] = []
        self.high_session: RecordingSession | None = None
        self.high_telemetry = CallbackTelemetry()
        self.high_stop_after_id: str | None = None
        self.high_last_output_path: Path | None = None

        self.low_mode_var = tk.StringVar(value="app")
        self.low_app_query_var = tk.StringVar()
        self.low_write_file_var = tk.BooleanVar(value=True)
        self.low_output_path_var = tk.StringVar(
            value=str(default_dir / "catap-demo-low.wav")
        )
        self.low_duration_var = tk.StringVar(value="10")
        self.low_enable_callback_var = tk.BooleanVar(value=True)
        self.low_max_pending_var = tk.StringVar(value="256")
        self.low_name_var = tk.StringVar(value="catap demo low-level tap")
        self.low_private_var = tk.BooleanVar(value=True)
        self.low_mono_var = tk.BooleanVar(value=False)
        self.low_mute_behavior_var = tk.StringVar(value="UNMUTED")
        self.low_status_var = tk.StringVar(value="Idle.")
        self.low_tap_state_vars = {
            "tap_id": tk.StringVar(value="n/a"),
            "name": tk.StringVar(value="n/a"),
            "scope": tk.StringVar(value="n/a"),
            "private": tk.StringVar(value="n/a"),
            "mute_behavior": tk.StringVar(value="n/a"),
        }
        self.low_recorder_state_vars = {
            "active": tk.StringVar(value="False"),
            "output": tk.StringVar(value="n/a"),
            "frames": tk.StringVar(value="0"),
            "duration": tk.StringVar(value="0.00s"),
            "format": tk.StringVar(value="n/a"),
        }
        self.low_callback_state_vars = {
            "buffers": tk.StringVar(value="0"),
            "frames": tk.StringVar(value="0"),
            "bytes": tk.StringVar(value="0"),
            "last_size": tk.StringVar(value="0"),
            "last_update": tk.StringVar(value="n/a"),
        }
        self.low_excluded_processes: list[AudioProcess] = []
        self.low_tap_description: TapDescription | None = None
        self.low_tap_id: int | None = None
        self.low_recorder: AudioRecorder | None = None
        self.low_telemetry = CallbackTelemetry()
        self.low_stop_after_id: str | None = None
        self.low_last_output_path: Path | None = None

        self.playback_status_var = tk.StringVar(value="Playback idle.")
        self.playback_process: subprocess.Popen[bytes] | None = None
        self.playback_path: Path | None = None
        self.playback_owner: str | None = None
        self.playback_poll_after_id: str | None = None

        self._build_ui()
        self._update_high_mode_controls()
        self._update_low_mode_controls()
        self._update_high_output_controls()
        self._update_low_output_controls()
        self._refresh_exclusions_listbox(
            self.high_exclusions_listbox,
            self.high_excluded_processes,
        )
        self._refresh_exclusions_listbox(
            self.low_exclusions_listbox,
            self.low_excluded_processes,
        )

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_ui_queue)
        self.root.after(250, self._refresh_stats)
        self.refresh_processes()

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        main_panes = ttk.Panedwindow(main, orient=tk.VERTICAL)
        main_panes.grid(row=0, column=0, sticky="nsew")

        top_frame = ttk.Frame(main_panes)
        top_frame.columnconfigure(0, weight=1)
        top_frame.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(top_frame)
        notebook.grid(row=0, column=0, sticky="nsew")

        browser_tab = ttk.Frame(notebook, padding=12)
        high_tab = ttk.Frame(notebook, padding=12)
        low_tab = ttk.Frame(notebook, padding=12)
        notebook.add(browser_tab, text="Process Browser")
        notebook.add(high_tab, text="High-Level Session")
        notebook.add(low_tab, text="Low-Level Lab")

        self._build_browser_tab(browser_tab)
        self._build_high_tab(high_tab)
        self._build_low_tab(low_tab)

        log_frame = ttk.LabelFrame(main_panes, text="Event Log", padding=10)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=8,
            wrap=tk.WORD,
            state=tk.DISABLED,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        main_panes.add(top_frame, weight=5)
        main_panes.add(log_frame, weight=1)

    def _build_browser_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)

        ttk.Label(
            controls,
            text=(
                "Browse Core Audio processes, test list lookups, and feed exact "
                "bundle IDs or names into the recorder tabs."
            ),
            wraplength=980,
        ).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 10))

        ttk.Label(controls, text="Find App").grid(row=1, column=0, sticky="w")
        ttk.Entry(
            controls,
            textvariable=self.browser_query_var,
            width=32,
        ).grid(row=1, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(
            controls,
            text="Find Match",
            command=self.find_browser_match,
        ).grid(row=1, column=2, sticky="ew")
        ttk.Button(
            controls,
            text="Refresh",
            command=self.refresh_processes,
        ).grid(row=1, column=3, sticky="ew", padx=(8, 0))
        ttk.Checkbutton(
            controls,
            text="Show Idle Processes",
            variable=self.browser_show_all_var,
            command=self.refresh_processes,
        ).grid(row=1, column=4, sticky="w", padx=(12, 0))
        ttk.Label(
            controls,
            textvariable=self.browser_status_var,
        ).grid(row=1, column=5, sticky="e", padx=(12, 0))

        actions = ttk.Frame(parent)
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 10))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)

        source_actions = ttk.LabelFrame(actions, text="Set Source", padding=8)
        source_actions.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        source_actions.columnconfigure(0, weight=1)
        source_actions.columnconfigure(1, weight=1)
        ttk.Button(
            source_actions,
            text="Set High App",
            command=self.use_selection_for_high_app,
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            source_actions,
            text="Set Low App",
            command=self.use_selection_for_low_app,
        ).grid(row=0, column=1, sticky="ew", padx=(8, 0))

        exclusion_actions = ttk.LabelFrame(actions, text="Add To Exclusions", padding=8)
        exclusion_actions.grid(row=0, column=1, sticky="ew")
        exclusion_actions.columnconfigure(0, weight=1)
        exclusion_actions.columnconfigure(1, weight=1)
        ttk.Button(
            exclusion_actions,
            text="Exclude In High",
            command=self.add_selection_to_high_exclusions,
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            exclusion_actions,
            text="Exclude In Low",
            command=self.add_selection_to_low_exclusions,
        ).grid(row=0, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(
            actions,
            text="Tip: double-click a process to copy it into both app-query fields.",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        columns = ("status", "name", "bundle", "audio_id", "pid")
        self.process_tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "status": "Status",
            "name": "Name",
            "bundle": "Bundle ID",
            "audio_id": "Audio ID",
            "pid": "PID",
        }
        widths = {
            "status": 90,
            "name": 220,
            "bundle": 360,
            "audio_id": 110,
            "pid": 90,
        }
        for column in columns:
            self.process_tree.heading(column, text=headings[column])
            self.process_tree.column(column, width=widths[column], anchor="w")
        self.process_tree.grid(row=0, column=0, sticky="nsew")
        self.process_tree.bind("<Double-1>", self._on_process_double_click)

        scroll = ttk.Scrollbar(
            tree_frame,
            orient=tk.VERTICAL,
            command=self.process_tree.yview,
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.process_tree.configure(yscrollcommand=scroll.set)

    def _build_live_metric_rows(
        self,
        parent: ttk.Frame,
        rows: list[tuple[str, tk.StringVar]],
        *,
        wraplength: int | None = None,
    ) -> None:
        parent.columnconfigure(1, weight=1)
        for row_index, (label, variable) in enumerate(rows):
            ttk.Label(parent, text=label).grid(
                row=row_index,
                column=0,
                sticky="nw",
                padx=(0, 6),
                pady=(0, 2),
            )
            ttk.Label(
                parent,
                textvariable=variable,
                justify=tk.LEFT,
                anchor="w",
                wraplength=wraplength or self._live_value_wraplength,
            ).grid(
                row=row_index,
                column=1,
                sticky="nw",
                pady=(0, 2),
            )

    def _create_exclusions_listbox(self, parent: ttk.Frame) -> tk.Listbox:
        return tk.Listbox(
            parent,
            height=4,
            activestyle=tk.NONE,
            exportselection=False,
            relief=tk.SOLID,
            borderwidth=1,
            highlightthickness=0,
            bg=self._listbox_background,
            fg=self._listbox_foreground,
            selectmode=tk.SINGLE,
        )

    def _build_high_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)

        settings = ttk.LabelFrame(parent, text="Session Controls", padding=12)
        settings.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        settings.columnconfigure(1, weight=1)

        ttk.Label(
            settings,
            text=(
                "This tab exercises record_process, record_system_audio, "
                "RecordingSession.start/stop, and RecordingSession.record_for."
            ),
            wraplength=760,
        ).grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        ttk.Label(settings, text="Source").grid(row=1, column=0, sticky="w")
        source_frame = ttk.Frame(settings)
        source_frame.grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(
            source_frame,
            text="Single App",
            variable=self.high_mode_var,
            value="app",
            command=self._update_high_mode_controls,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            source_frame,
            text="System Audio",
            variable=self.high_mode_var,
            value="system",
            command=self._update_high_mode_controls,
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))

        ttk.Label(settings, text="App Query").grid(row=2, column=0, sticky="w")
        self.high_app_entry = ttk.Entry(
            settings,
            textvariable=self.high_app_query_var,
            width=36,
        )
        self.high_app_entry.grid(row=2, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(
            settings,
            text="Use Browser Selection",
            command=self.use_selection_for_high_app,
        ).grid(row=2, column=2, sticky="ew")

        self.high_mute_check = ttk.Checkbutton(
            settings,
            text="Mute app while recording",
            variable=self.high_mute_var,
        )
        self.high_mute_check.grid(row=3, column=1, sticky="w", pady=(6, 0))

        ttk.Separator(settings, orient=tk.HORIZONTAL).grid(
            row=4,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=12,
        )

        self.high_write_file_check = ttk.Checkbutton(
            settings,
            text="Write WAV file",
            variable=self.high_write_file_var,
            command=self._update_high_output_controls,
        )
        self.high_write_file_check.grid(row=5, column=0, sticky="w")
        self.high_callback_check = ttk.Checkbutton(
            settings,
            text="Enable callback telemetry",
            variable=self.high_enable_callback_var,
        )
        self.high_callback_check.grid(row=5, column=1, sticky="w")

        ttk.Label(settings, text="Output Path").grid(row=6, column=0, sticky="w")
        self.high_output_entry = ttk.Entry(
            settings,
            textvariable=self.high_output_path_var,
        )
        self.high_output_entry.grid(row=6, column=1, sticky="ew", padx=(8, 8))
        self.high_output_button = ttk.Button(
            settings,
            text="Browse",
            command=lambda: self._choose_output_path(self.high_output_path_var),
        )
        self.high_output_button.grid(row=6, column=2, sticky="ew")

        ttk.Label(settings, text="Duration (sec)").grid(row=7, column=0, sticky="w")
        ttk.Entry(
            settings,
            textvariable=self.high_duration_var,
            width=14,
        ).grid(row=7, column=1, sticky="w", padx=(8, 8))

        ttk.Label(settings, text="Max Pending Buffers").grid(
            row=8,
            column=0,
            sticky="w",
        )
        ttk.Entry(
            settings,
            textvariable=self.high_max_pending_var,
            width=14,
        ).grid(row=8, column=1, sticky="w", padx=(8, 8))

        exclusions = ttk.LabelFrame(settings, text="System Exclusions", padding=8)
        exclusions.grid(row=9, column=0, columnspan=3, sticky="nsew", pady=(12, 0))
        exclusions.columnconfigure(0, weight=1)
        exclusions.rowconfigure(0, weight=1)
        self.high_exclusions_listbox = self._create_exclusions_listbox(exclusions)
        self.high_exclusions_listbox.grid(row=0, column=0, sticky="nsew")
        exclusions_buttons = ttk.Frame(exclusions)
        exclusions_buttons.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        ttk.Button(
            exclusions_buttons,
            text="Add Browser Selection",
            command=self.add_selection_to_high_exclusions,
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            exclusions_buttons,
            text="Remove",
            command=self.remove_selected_high_exclusion,
        ).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(
            exclusions_buttons,
            text="Clear",
            command=self.clear_high_exclusions,
        ).grid(row=2, column=0, sticky="ew", pady=(6, 0))

        actions = ttk.Frame(settings)
        actions.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(
            actions,
            text="Start Session",
            command=self.start_high_session,
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            actions,
            text="Record For Duration",
            command=self.record_high_for_duration,
        ).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(
            actions,
            text="Stop Session",
            command=self.stop_high_session,
        ).grid(row=0, column=2, sticky="ew", padx=(8, 0))

        live = ttk.LabelFrame(parent, text="Live Session State", padding=12)
        live.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        live.columnconfigure(0, weight=1)
        live.rowconfigure(1, weight=1)

        ttk.Label(
            live,
            textvariable=self.high_status_var,
            font=("", 12, "bold"),
        ).grid(row=0, column=0, sticky="w")

        summary = ttk.Frame(live)
        summary.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        summary.columnconfigure(0, weight=1)

        session_state = ttk.LabelFrame(summary, text="Session", padding=6)
        session_state.grid(row=0, column=0, sticky="ew")
        self._build_live_metric_rows(
            session_state,
            [
                ("Source", self.high_session_state_vars["source"]),
                ("Tap ID", self.high_session_state_vars["tap_id"]),
                ("Active", self.high_session_state_vars["active"]),
                ("Output", self.high_session_state_vars["output"]),
                ("Excluded", self.high_session_state_vars["excluded"]),
            ],
        )

        recorder_state = ttk.LabelFrame(summary, text="Recorder", padding=6)
        recorder_state.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self._build_live_metric_rows(
            recorder_state,
            [
                ("Frames", self.high_recorder_state_vars["frames"]),
                ("Duration", self.high_recorder_state_vars["duration"]),
                ("Format", self.high_recorder_state_vars["format"]),
            ],
        )

        callback_state = ttk.LabelFrame(summary, text="Callback", padding=6)
        callback_state.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._build_live_metric_rows(
            callback_state,
            [
                ("Buffers", self.high_callback_state_vars["buffers"]),
                ("Frames", self.high_callback_state_vars["frames"]),
                ("Bytes", self.high_callback_state_vars["bytes"]),
                ("Last Buffer", self.high_callback_state_vars["last_size"]),
                ("Updated", self.high_callback_state_vars["last_update"]),
            ],
        )

        ttk.Label(
            live,
            textvariable=self.playback_status_var,
            justify=tk.LEFT,
            wraplength=360,
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))
        playback_actions = ttk.Frame(live)
        playback_actions.grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Button(
            playback_actions,
            text="Play",
            command=self.play_high_output,
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            playback_actions,
            text="Stop",
            command=self.stop_playback,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(
            playback_actions,
            text="Reveal",
            command=self.reveal_high_output,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

    def _build_low_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)

        settings = ttk.LabelFrame(parent, text="Tap + Recorder Controls", padding=12)
        settings.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        settings.columnconfigure(1, weight=1)

        ttk.Label(
            settings,
            text=(
                "This tab exercises TapDescription, create_process_tap, "
                "AudioRecorder.start/stop, and destroy_process_tap."
            ),
            wraplength=760,
        ).grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        ttk.Label(settings, text="Source").grid(row=1, column=0, sticky="w")
        source_frame = ttk.Frame(settings)
        source_frame.grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(
            source_frame,
            text="Single App",
            variable=self.low_mode_var,
            value="app",
            command=self._update_low_mode_controls,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            source_frame,
            text="System Audio",
            variable=self.low_mode_var,
            value="system",
            command=self._update_low_mode_controls,
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))

        ttk.Label(settings, text="App Query").grid(row=2, column=0, sticky="w")
        self.low_app_entry = ttk.Entry(
            settings,
            textvariable=self.low_app_query_var,
            width=36,
        )
        self.low_app_entry.grid(row=2, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(
            settings,
            text="Use Browser Selection",
            command=self.use_selection_for_low_app,
        ).grid(row=2, column=2, sticky="ew")

        ttk.Label(settings, text="Tap Name").grid(row=3, column=0, sticky="w")
        ttk.Entry(
            settings,
            textvariable=self.low_name_var,
        ).grid(row=3, column=1, sticky="ew", padx=(8, 8))

        ttk.Label(settings, text="Channels").grid(row=4, column=0, sticky="w")
        ttk.Checkbutton(
            settings,
            text="Mono Mixdown",
            variable=self.low_mono_var,
        ).grid(row=4, column=1, sticky="w", padx=(8, 0))

        ttk.Label(settings, text="Mute Behavior").grid(row=5, column=0, sticky="w")
        self.low_mute_combo = ttk.Combobox(
            settings,
            state="readonly",
            values=[behavior.name for behavior in TapMuteBehavior],
            textvariable=self.low_mute_behavior_var,
            width=20,
        )
        self.low_mute_combo.grid(row=5, column=1, sticky="w", padx=(8, 0))

        ttk.Checkbutton(
            settings,
            text="Private Tap",
            variable=self.low_private_var,
        ).grid(row=6, column=1, sticky="w", pady=(6, 0), padx=(8, 0))

        ttk.Separator(settings, orient=tk.HORIZONTAL).grid(
            row=7,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=12,
        )

        self.low_write_file_check = ttk.Checkbutton(
            settings,
            text="Write WAV file",
            variable=self.low_write_file_var,
            command=self._update_low_output_controls,
        )
        self.low_write_file_check.grid(row=8, column=0, sticky="w")
        ttk.Checkbutton(
            settings,
            text="Enable callback telemetry",
            variable=self.low_enable_callback_var,
        ).grid(row=8, column=1, sticky="w")

        ttk.Label(settings, text="Output Path").grid(row=9, column=0, sticky="w")
        self.low_output_entry = ttk.Entry(
            settings,
            textvariable=self.low_output_path_var,
        )
        self.low_output_entry.grid(row=9, column=1, sticky="ew", padx=(8, 8))
        self.low_output_button = ttk.Button(
            settings,
            text="Browse",
            command=lambda: self._choose_output_path(self.low_output_path_var),
        )
        self.low_output_button.grid(row=9, column=2, sticky="ew")

        ttk.Label(settings, text="Duration (sec)").grid(row=10, column=0, sticky="w")
        ttk.Entry(
            settings,
            textvariable=self.low_duration_var,
            width=14,
        ).grid(row=10, column=1, sticky="w", padx=(8, 8))

        ttk.Label(settings, text="Max Pending Buffers").grid(
            row=11,
            column=0,
            sticky="w",
        )
        ttk.Entry(
            settings,
            textvariable=self.low_max_pending_var,
            width=14,
        ).grid(row=11, column=1, sticky="w", padx=(8, 8))

        exclusions = ttk.LabelFrame(settings, text="System Exclusions", padding=8)
        exclusions.grid(row=12, column=0, columnspan=3, sticky="nsew", pady=(12, 0))
        exclusions.columnconfigure(0, weight=1)
        exclusions.rowconfigure(0, weight=1)
        self.low_exclusions_listbox = self._create_exclusions_listbox(exclusions)
        self.low_exclusions_listbox.grid(row=0, column=0, sticky="nsew")
        exclusions_buttons = ttk.Frame(exclusions)
        exclusions_buttons.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        ttk.Button(
            exclusions_buttons,
            text="Add Browser Selection",
            command=self.add_selection_to_low_exclusions,
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            exclusions_buttons,
            text="Remove",
            command=self.remove_selected_low_exclusion,
        ).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(
            exclusions_buttons,
            text="Clear",
            command=self.clear_low_exclusions,
        ).grid(row=2, column=0, sticky="ew", pady=(6, 0))

        actions = ttk.Frame(settings)
        actions.grid(row=13, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="Create Tap", command=self.create_low_tap).grid(
            row=0,
            column=0,
            sticky="ew",
        )
        ttk.Button(
            actions,
            text="Start Recorder",
            command=self.start_low_recorder,
        ).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(
            actions,
            text="Stop Recorder",
            command=self.stop_low_recorder,
        ).grid(row=0, column=2, sticky="ew", padx=(8, 0))
        ttk.Button(
            actions,
            text="Destroy Tap",
            command=self.destroy_low_tap,
        ).grid(row=0, column=3, sticky="ew", padx=(8, 0))

        live = ttk.LabelFrame(parent, text="Live Tap State", padding=12)
        live.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        live.columnconfigure(0, weight=1)
        live.rowconfigure(1, weight=1)
        ttk.Label(
            live,
            textvariable=self.low_status_var,
            font=("", 12, "bold"),
        ).grid(row=0, column=0, sticky="w")

        summary = ttk.Frame(live)
        summary.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        summary.columnconfigure(0, weight=1)

        tap_details = ttk.LabelFrame(summary, text="Tap Details", padding=6)
        tap_details.grid(row=0, column=0, sticky="ew")
        self._build_live_metric_rows(
            tap_details,
            [
                ("Tap ID", self.low_tap_state_vars["tap_id"]),
                ("Name", self.low_tap_state_vars["name"]),
                ("Scope", self.low_tap_state_vars["scope"]),
                ("Private", self.low_tap_state_vars["private"]),
                ("Mute", self.low_tap_state_vars["mute_behavior"]),
            ],
        )

        recorder_state = ttk.LabelFrame(summary, text="Recorder", padding=6)
        recorder_state.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._build_live_metric_rows(
            recorder_state,
            [
                ("Active", self.low_recorder_state_vars["active"]),
                ("Output", self.low_recorder_state_vars["output"]),
                ("Frames", self.low_recorder_state_vars["frames"]),
                ("Duration", self.low_recorder_state_vars["duration"]),
                ("Format", self.low_recorder_state_vars["format"]),
            ],
        )

        callback_state = ttk.LabelFrame(
            summary,
            text="Callback",
            padding=6,
        )
        callback_state.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._build_live_metric_rows(
            callback_state,
            [
                ("Buffers", self.low_callback_state_vars["buffers"]),
                ("Frames", self.low_callback_state_vars["frames"]),
                ("Bytes", self.low_callback_state_vars["bytes"]),
                ("Last Buffer", self.low_callback_state_vars["last_size"]),
                ("Updated", self.low_callback_state_vars["last_update"]),
            ],
        )
        ttk.Label(
            live,
            textvariable=self.playback_status_var,
            justify=tk.LEFT,
            wraplength=360,
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))
        playback_actions = ttk.Frame(live)
        playback_actions.grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Button(
            playback_actions,
            text="Play",
            command=self.play_low_output,
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            playback_actions,
            text="Stop",
            command=self.stop_playback,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(
            playback_actions,
            text="Reveal",
            command=self.reveal_low_output,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            callback()

        self.root.after(100, self._drain_ui_queue)

    def _post_ui(self, callback: Callable[[], None]) -> None:
        self._ui_queue.put(callback)

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _run_background(
        self,
        action_name: str,
        worker: Callable[[], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        def target() -> None:
            try:
                worker()
            except Exception as exc:
                if on_error is None:
                    self._post_ui(
                        lambda exc=exc: self._report_error(action_name, exc)
                    )
                else:
                    self._post_ui(lambda exc=exc: on_error(exc))

        threading.Thread(target=target, daemon=True).start()

    def _report_error(self, action_name: str, exc: Exception) -> None:
        self._log(f"{action_name} failed: {exc}")
        messagebox.showerror(f"{action_name} Failed", str(exc))

    def _choose_output_path(self, variable: tk.StringVar) -> None:
        chosen = filedialog.asksaveasfilename(
            defaultextension=".wav",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
            initialfile=Path(variable.get()).name or "recording.wav",
        )
        if chosen:
            variable.set(chosen)

    def _cancel_playback_poll(self) -> None:
        if self.playback_poll_after_id is not None:
            self.root.after_cancel(self.playback_poll_after_id)
            self.playback_poll_after_id = None

    def _schedule_playback_poll(self) -> None:
        self._cancel_playback_poll()
        self.playback_poll_after_id = self.root.after(250, self._poll_playback)

    def _poll_playback(self) -> None:
        process = self.playback_process
        if process is None:
            self.playback_poll_after_id = None
            return

        return_code = process.poll()
        if return_code is None:
            self.playback_poll_after_id = self.root.after(250, self._poll_playback)
            return

        owner = self.playback_owner or "output"
        path = self.playback_path
        path_label = path.name if path is not None else "recording"
        self.playback_process = None
        self.playback_path = None
        self.playback_owner = None
        self.playback_poll_after_id = None

        if return_code == 0:
            status = f"Playback finished for {owner}: {path_label}"
        else:
            status = f"Playback exited with code {return_code} for {path_label}"
        self.playback_status_var.set(status)
        self._log(status)

    def _play_output(self, output_path: Path | None, owner: str) -> None:
        if output_path is None:
            messagebox.showinfo(
                "Playback",
                "There is no recorded WAV file available for playback yet.",
            )
            return

        path = output_path.expanduser()
        if not path.exists():
            messagebox.showinfo(
                "Playback",
                f"The last recorded output is missing:\n{path}",
            )
            return

        self.stop_playback(silent=True)

        try:
            process = subprocess.Popen(["afplay", str(path)])
        except FileNotFoundError as exc:
            self._report_error("Start Playback", exc)
            return

        self.playback_process = process
        self.playback_path = path
        self.playback_owner = owner
        self.playback_status_var.set(f"Playing {owner}: {path.name}")
        self._log(f"Started playback for {owner}: {path}")
        self._schedule_playback_poll()

    def play_high_output(self) -> None:
        self._play_output(self.high_last_output_path, "high-level output")

    def play_low_output(self) -> None:
        self._play_output(self.low_last_output_path, "low-level output")

    def _reveal_output(self, output_path: Path | None, owner: str) -> None:
        if output_path is None:
            messagebox.showinfo(
                "Reveal Output",
                "There is no recorded WAV file available to reveal yet.",
            )
            return

        path = output_path.expanduser()
        if not path.exists():
            messagebox.showinfo(
                "Reveal Output",
                f"The last recorded output is missing:\n{path}",
            )
            return

        try:
            subprocess.run(["open", "-R", str(path)], check=True)
        except FileNotFoundError as exc:
            self._report_error("Reveal Output In Finder", exc)
            return
        except subprocess.CalledProcessError as exc:
            self._report_error("Reveal Output In Finder", exc)
            return

        self._log(f"Revealed {owner} in Finder: {path}")

    def reveal_high_output(self) -> None:
        self._reveal_output(self.high_last_output_path, "high-level output")

    def reveal_low_output(self) -> None:
        self._reveal_output(self.low_last_output_path, "low-level output")

    def stop_playback(self, *, silent: bool = False) -> None:
        process = self.playback_process
        if process is None:
            if not silent:
                self.playback_status_var.set("Playback idle.")
            self._cancel_playback_poll()
            return

        self._cancel_playback_poll()
        if process.poll() is None:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
            if process.poll() is None:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)

        owner = self.playback_owner or "output"
        path = self.playback_path
        path_label = path.name if path is not None else "recording"
        self.playback_process = None
        self.playback_path = None
        self.playback_owner = None

        if silent:
            self.playback_status_var.set("Playback idle.")
            return

        status = f"Playback stopped for {owner}: {path_label}"
        self.playback_status_var.set(status)
        self._log(status)

    def _selected_process(self) -> AudioProcess | None:
        selection = self.process_tree.selection()
        if not selection:
            return None
        return self._process_index.get(selection[0])

    def _format_process(self, process: AudioProcess) -> str:
        bundle_id = process.bundle_id or "N/A"
        status = "outputting" if process.is_outputting else "idle"
        return (
            f"{process.name} (PID {process.pid}, Bundle ID {bundle_id}, {status})"
        )

    def refresh_processes(self) -> None:
        try:
            processes = list_audio_processes()
        except Exception as exc:
            self.browser_status_var.set("Refresh failed.")
            self._report_error("Refresh Audio Processes", exc)
            return

        if not self.browser_show_all_var.get():
            processes = [process for process in processes if process.is_outputting]

        self._processes = processes
        self._process_index.clear()
        self.process_tree.delete(*self.process_tree.get_children())

        for index, process in enumerate(processes):
            iid = f"process-{index}"
            self._process_index[iid] = process
            self.process_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    "Outputting" if process.is_outputting else "Idle",
                    process.name,
                    process.bundle_id or "N/A",
                    process.audio_object_id,
                    process.pid,
                ),
            )

        process_count = len(processes)
        noun = "process" if process_count == 1 else "processes"
        self.browser_status_var.set(f"{process_count} {noun} shown.")
        self._log(
            "Refreshed audio process list "
            f"({len(processes)} visible, show_all={self.browser_show_all_var.get()})."
        )

    def find_browser_match(self) -> None:
        query = self.browser_query_var.get().strip()
        if not query:
            messagebox.showinfo("Find Match", "Enter an app name or bundle ID first.")
            return

        try:
            process = find_process_by_name(query)
        except AmbiguousAudioProcessError as exc:
            self._report_error("Find Audio Process", exc)
            return
        except Exception as exc:
            self._report_error("Find Audio Process", exc)
            return

        if process is None:
            messagebox.showinfo(
                "Find Match",
                f"No audio process matched {query!r}.",
            )
            return

        for iid, listed_process in self._process_index.items():
            if (
                listed_process.audio_object_id == process.audio_object_id
                and listed_process.pid == process.pid
            ):
                self.process_tree.selection_set(iid)
                self.process_tree.focus(iid)
                self.process_tree.see(iid)
                break

        self._log(
            f"Matched browser query {query!r} "
            f"to {self._format_process(process)}."
        )

    def _on_process_double_click(self, _event: tk.Event[tk.Misc]) -> None:
        self.use_selection_for_high_app()
        self.use_selection_for_low_app()

    def use_selection_for_high_app(self) -> None:
        process = self._selected_process()
        if process is None:
            messagebox.showinfo(
                "High-Level App",
                "Select a process in the browser first.",
            )
            return

        self.high_app_query_var.set(process.bundle_id or process.name)
        self.high_mode_var.set("app")
        self._update_high_mode_controls()
        self._log(f"High-level app query set to {process.bundle_id or process.name!r}.")

    def use_selection_for_low_app(self) -> None:
        process = self._selected_process()
        if process is None:
            messagebox.showinfo(
                "Low-Level App",
                "Select a process in the browser first.",
            )
            return

        self.low_app_query_var.set(process.bundle_id or process.name)
        self.low_mode_var.set("app")
        self._update_low_mode_controls()
        self._log(f"Low-level app query set to {process.bundle_id or process.name!r}.")

    def _add_process_to_exclusions(
        self,
        process: AudioProcess,
        excluded_processes: list[AudioProcess],
        listbox: tk.Listbox,
        label: str,
    ) -> None:
        if any(
            existing.audio_object_id == process.audio_object_id
            for existing in excluded_processes
        ):
            return

        excluded_processes.append(process)
        excluded_processes.sort(key=lambda item: (item.name.casefold(), item.pid))
        self._refresh_exclusions_listbox(listbox, excluded_processes)
        self._log(f"Added {self._format_process(process)} to {label} exclusions.")

    def _refresh_exclusions_listbox(
        self,
        listbox: tk.Listbox,
        excluded_processes: list[AudioProcess],
    ) -> None:
        listbox.configure(fg=self._listbox_foreground)
        listbox.delete(0, tk.END)
        if not excluded_processes:
            listbox.configure(fg=self._listbox_placeholder_foreground)
            listbox.insert(tk.END, "No excluded apps.")
            return
        for process in excluded_processes:
            listbox.insert(tk.END, self._format_process(process))

    def add_selection_to_high_exclusions(self) -> None:
        process = self._selected_process()
        if process is None:
            messagebox.showinfo("High-Level Exclusions", "Select a process first.")
            return
        self._add_process_to_exclusions(
            process,
            self.high_excluded_processes,
            self.high_exclusions_listbox,
            "high-level",
        )

    def add_selection_to_low_exclusions(self) -> None:
        process = self._selected_process()
        if process is None:
            messagebox.showinfo("Low-Level Exclusions", "Select a process first.")
            return
        self._add_process_to_exclusions(
            process,
            self.low_excluded_processes,
            self.low_exclusions_listbox,
            "low-level",
        )

    def remove_selected_high_exclusion(self) -> None:
        if not self.high_excluded_processes:
            return
        selection = self.high_exclusions_listbox.curselection()
        if not selection:
            return
        del self.high_excluded_processes[selection[0]]
        self._refresh_exclusions_listbox(
            self.high_exclusions_listbox,
            self.high_excluded_processes,
        )

    def remove_selected_low_exclusion(self) -> None:
        if not self.low_excluded_processes:
            return
        selection = self.low_exclusions_listbox.curselection()
        if not selection:
            return
        del self.low_excluded_processes[selection[0]]
        self._refresh_exclusions_listbox(
            self.low_exclusions_listbox,
            self.low_excluded_processes,
        )

    def clear_high_exclusions(self) -> None:
        self.high_excluded_processes.clear()
        self._refresh_exclusions_listbox(
            self.high_exclusions_listbox,
            self.high_excluded_processes,
        )

    def clear_low_exclusions(self) -> None:
        self.low_excluded_processes.clear()
        self._refresh_exclusions_listbox(
            self.low_exclusions_listbox,
            self.low_excluded_processes,
        )

    def _parse_duration(self, raw_value: str) -> float | None:
        stripped = raw_value.strip()
        if not stripped:
            return None
        value = float(stripped)
        if value <= 0:
            raise ValueError("Duration must be greater than 0 seconds.")
        return value

    def _parse_max_pending_buffers(self, raw_value: str) -> int:
        value = int(raw_value.strip())
        if value <= 0:
            raise ValueError("Max pending buffers must be greater than 0.")
        return value

    def _resolve_output_path(
        self,
        write_file: bool,
        raw_output_path: str,
    ) -> Path | None:
        if not write_file:
            return None
        stripped = raw_output_path.strip()
        if not stripped:
            raise ValueError("Choose an output path or disable WAV writing.")
        return Path(stripped)

    def _update_high_mode_controls(self) -> None:
        app_mode = self.high_mode_var.get() == "app"
        if app_mode:
            self.high_app_entry.state(["!disabled"])
            self.high_mute_check.state(["!disabled"])
        else:
            self.high_app_entry.state(["disabled"])
            self.high_mute_check.state(["disabled"])

    def _update_low_mode_controls(self) -> None:
        app_mode = self.low_mode_var.get() == "app"
        if app_mode:
            self.low_app_entry.state(["!disabled"])
        else:
            self.low_app_entry.state(["disabled"])

    def _update_high_output_controls(self) -> None:
        if self.high_write_file_var.get():
            self.high_output_entry.state(["!disabled"])
            self.high_output_button.state(["!disabled"])
        else:
            self.high_output_entry.state(["disabled"])
            self.high_output_button.state(["disabled"])

    def _update_low_output_controls(self) -> None:
        if self.low_write_file_var.get():
            self.low_output_entry.state(["!disabled"])
            self.low_output_button.state(["!disabled"])
        else:
            self.low_output_entry.state(["disabled"])
            self.low_output_button.state(["disabled"])

    def _schedule_high_auto_stop(self, duration: float | None) -> None:
        self._cancel_high_auto_stop()
        if duration is None:
            return
        self.high_stop_after_id = self.root.after(
            int(duration * 1000),
            self.stop_high_session,
        )

    def _cancel_high_auto_stop(self) -> None:
        if self.high_stop_after_id is not None:
            self.root.after_cancel(self.high_stop_after_id)
            self.high_stop_after_id = None

    def _schedule_low_auto_stop(self, duration: float | None) -> None:
        self._cancel_low_auto_stop()
        if duration is None:
            return
        self.low_stop_after_id = self.root.after(
            int(duration * 1000),
            self.stop_low_recorder,
        )

    def _cancel_low_auto_stop(self) -> None:
        if self.low_stop_after_id is not None:
            self.root.after_cancel(self.low_stop_after_id)
            self.low_stop_after_id = None

    def _build_high_session(self) -> tuple[RecordingSession, float | None]:
        duration = self._parse_duration(self.high_duration_var.get())
        max_pending_buffers = self._parse_max_pending_buffers(
            self.high_max_pending_var.get()
        )
        output_path = self._resolve_output_path(
            self.high_write_file_var.get(),
            self.high_output_path_var.get(),
        )
        on_data = (
            self.high_telemetry.callback
            if self.high_enable_callback_var.get()
            else None
        )
        self.high_telemetry.reset()

        if self.high_mode_var.get() == "app":
            app_query = self.high_app_query_var.get().strip()
            if not app_query:
                raise ValueError("Enter an app name or bundle ID first.")
            session = record_process(
                app_query,
                output_path=output_path,
                mute=self.high_mute_var.get(),
                on_data=on_data,
                max_pending_buffers=max_pending_buffers,
            )
        else:
            session = record_system_audio(
                output_path=output_path,
                exclude=self.high_excluded_processes,
                on_data=on_data,
                max_pending_buffers=max_pending_buffers,
            )

        return session, duration

    def start_high_session(self) -> None:
        if self.high_session is not None and self.high_session.is_recording:
            messagebox.showinfo(
                "High-Level Session",
                "A high-level session is already running.",
            )
            return

        try:
            session, duration = self._build_high_session()
        except Exception as exc:
            self._report_error("Prepare High-Level Session", exc)
            return

        self.high_status_var.set("Starting high-level session...")
        self.high_session = session
        self._log("Starting high-level session.")

        def worker() -> None:
            session.start()
            self._post_ui(
                lambda: self._on_high_session_started(session, duration, "manual")
            )

        def on_error(exc: Exception) -> None:
            self.high_telemetry.freeze()
            self.high_session = None
            self.high_status_var.set("High-level session failed to start.")
            self._report_error("Start High-Level Session", exc)

        self._run_background("Start High-Level Session", worker, on_error=on_error)

    def record_high_for_duration(self) -> None:
        if self.high_session is not None and self.high_session.is_recording:
            messagebox.showinfo("High-Level Session", "Stop the current session first.")
            return

        try:
            session, duration = self._build_high_session()
        except Exception as exc:
            self._report_error("Prepare High-Level Session", exc)
            return

        if duration is None:
            messagebox.showinfo(
                "Record For Duration",
                "Enter a duration first for record_for().",
            )
            return

        self.high_status_var.set(f"Running record_for({duration:g})...")
        self.high_session = session
        self._log(f"Calling RecordingSession.record_for({duration:g}).")

        def worker() -> None:
            session.record_for(duration)
            self._post_ui(
                lambda: self._on_high_session_finished(
                    session,
                    f"High-level record_for completed after {duration:g} seconds.",
                )
            )

        def on_error(exc: Exception) -> None:
            self.high_telemetry.freeze()
            self.high_session = None
            self.high_status_var.set("High-level record_for failed.")
            self._report_error("High-Level record_for", exc)

        self._run_background("High-Level record_for", worker, on_error=on_error)

    def stop_high_session(self) -> None:
        session = self.high_session
        if session is None:
            return

        self.high_status_var.set("Stopping high-level session...")
        self._cancel_high_auto_stop()

        def worker() -> None:
            session.close()
            self._post_ui(
                lambda: self._on_high_session_finished(
                    session,
                    "High-level session stopped.",
                )
            )

        def on_error(exc: Exception) -> None:
            if not session.is_recording:
                self.high_telemetry.freeze()
            self.high_status_var.set("Stopping the high-level session failed.")
            self._report_error("Stop High-Level Session", exc)

        self._run_background("Stop High-Level Session", worker, on_error=on_error)

    def _on_high_session_started(
        self,
        session: RecordingSession,
        duration: float | None,
        mode: str,
    ) -> None:
        self.high_session = session
        self.high_last_output_path = session.output_path
        status_suffix = (
            f" Auto-stop in {duration:g}s." if duration is not None else ""
        )
        self.high_status_var.set(f"Recording via {mode} start.{status_suffix}")
        self._schedule_high_auto_stop(duration)
        self._log(
            "High-level session started "
            f"(tap_id={session.tap_id}, duration={duration})."
        )

    def _on_high_session_finished(
        self,
        session: RecordingSession,
        message: str,
    ) -> None:
        self.high_session = session
        self.high_last_output_path = session.output_path
        self.high_telemetry.freeze()
        self.high_status_var.set(message)
        self._cancel_high_auto_stop()
        self._log(message)

    def _build_low_level_description(self) -> TapDescription:
        mode = self.low_mode_var.get()
        mono = self.low_mono_var.get()

        if mode == "app":
            app_query = self.low_app_query_var.get().strip()
            if not app_query:
                raise ValueError("Enter an app name or bundle ID first.")
            process = find_process_by_name(app_query)
            if process is None:
                raise ValueError(f"No audio process found matching {app_query!r}.")
            process_ids = [process.audio_object_id]
            description = (
                TapDescription.mono_mixdown_of_processes(process_ids)
                if mono
                else TapDescription.stereo_mixdown_of_processes(process_ids)
            )
        else:
            excluded_ids = [
                process.audio_object_id for process in self.low_excluded_processes
            ]
            description = (
                TapDescription.mono_global_tap_excluding(excluded_ids)
                if mono
                else TapDescription.stereo_global_tap_excluding(excluded_ids)
            )

        description.name = self.low_name_var.get().strip() or "catap demo tap"
        description.is_private = self.low_private_var.get()
        description.mute_behavior = TapMuteBehavior[self.low_mute_behavior_var.get()]
        return description

    def _prepare_low_recorder_args(
        self,
    ) -> tuple[Path | None, Callable[[bytes, int], None] | None, float | None, int]:
        duration = self._parse_duration(self.low_duration_var.get())
        max_pending_buffers = self._parse_max_pending_buffers(
            self.low_max_pending_var.get()
        )
        output_path = self._resolve_output_path(
            self.low_write_file_var.get(),
            self.low_output_path_var.get(),
        )
        on_data = (
            self.low_telemetry.callback if self.low_enable_callback_var.get() else None
        )
        self.low_telemetry.reset()
        return output_path, on_data, duration, max_pending_buffers

    def create_low_tap(self) -> None:
        if self.low_recorder is not None and self.low_recorder.is_recording:
            messagebox.showinfo("Create Tap", "Stop the low-level recorder first.")
            return

        try:
            description = self._build_low_level_description()
        except Exception as exc:
            self._report_error("Prepare Low-Level Tap", exc)
            return

        previous_tap_id = self.low_tap_id
        self.low_status_var.set("Creating tap...")

        def worker() -> None:
            tap_id = create_process_tap(description)
            try:
                if previous_tap_id is not None:
                    destroy_process_tap(previous_tap_id)
            except Exception:
                with contextlib.suppress(Exception):
                    destroy_process_tap(tap_id)
                raise
            self._post_ui(
                lambda: self._on_low_tap_created(description, tap_id)
            )

        def on_error(exc: Exception) -> None:
            self.low_status_var.set("Low-level tap creation failed.")
            self._report_error("Create Low-Level Tap", exc)

        self._run_background("Create Low-Level Tap", worker, on_error=on_error)

    def _on_low_tap_created(self, description: TapDescription, tap_id: int) -> None:
        self.low_tap_description = description
        self.low_tap_id = tap_id
        self.low_status_var.set(f"Tap created (ID {tap_id}).")
        self._log(
            "Created low-level tap "
            f"(tap_id={tap_id}, uuid={description.uuid}, name={description.name!r})."
        )

    def start_low_recorder(self) -> None:
        if self.low_recorder is not None and self.low_recorder.is_recording:
            messagebox.showinfo(
                "Low-Level Recorder",
                "The low-level recorder is already running.",
            )
            return

        try:
            output_path, on_data, duration, max_pending_buffers = (
                self._prepare_low_recorder_args()
            )
            description = (
                self.low_tap_description or self._build_low_level_description()
            )
        except Exception as exc:
            self._report_error("Prepare Low-Level Recorder", exc)
            return

        existing_tap_id = self.low_tap_id
        self.low_status_var.set("Starting low-level recorder...")

        def worker() -> None:
            created_tap_here = False
            tap_id = existing_tap_id
            if tap_id is None:
                tap_id = create_process_tap(description)
                created_tap_here = True

            recorder = AudioRecorder(
                tap_id,
                output_path=output_path,
                on_data=on_data,
                max_pending_buffers=max_pending_buffers,
            )
            try:
                recorder.start()
            except Exception:
                if created_tap_here:
                    with contextlib.suppress(Exception):
                        destroy_process_tap(tap_id)
                raise

            self._post_ui(
                lambda: self._on_low_recorder_started(
                    description,
                    tap_id,
                    recorder,
                    duration,
                )
            )

        def on_error(exc: Exception) -> None:
            self.low_telemetry.freeze()
            self.low_status_var.set("Low-level recorder failed to start.")
            self._report_error("Start Low-Level Recorder", exc)

        self._run_background("Start Low-Level Recorder", worker, on_error=on_error)

    def _on_low_recorder_started(
        self,
        description: TapDescription,
        tap_id: int,
        recorder: AudioRecorder,
        duration: float | None,
    ) -> None:
        self.low_tap_description = description
        self.low_tap_id = tap_id
        self.low_recorder = recorder
        self.low_last_output_path = recorder.output_path
        self.low_status_var.set(f"Low-level recorder running on tap {tap_id}.")
        self._schedule_low_auto_stop(duration)
        self._log(
            "Low-level recorder started "
            f"(tap_id={tap_id}, duration={duration}, output={recorder.output_path})."
        )

    def stop_low_recorder(self) -> None:
        recorder = self.low_recorder
        if recorder is None:
            return

        self.low_status_var.set("Stopping low-level recorder...")
        self._cancel_low_auto_stop()

        def worker() -> None:
            recorder.stop()
            self._post_ui(self._on_low_recorder_stopped)

        def on_error(exc: Exception) -> None:
            if not recorder.is_recording:
                self.low_telemetry.freeze()
            self.low_status_var.set("Stopping the low-level recorder failed.")
            self._report_error("Stop Low-Level Recorder", exc)

        self._run_background("Stop Low-Level Recorder", worker, on_error=on_error)

    def _on_low_recorder_stopped(self) -> None:
        if self.low_recorder is not None:
            self.low_last_output_path = self.low_recorder.output_path
        self.low_telemetry.freeze()
        self.low_status_var.set("Low-level recorder stopped.")
        self._cancel_low_auto_stop()
        self._log("Low-level recorder stopped.")

    def destroy_low_tap(self) -> None:
        if self.low_recorder is not None and self.low_recorder.is_recording:
            messagebox.showinfo(
                "Destroy Tap",
                "Stop the recorder before destroying the tap.",
            )
            return
        if self.low_tap_id is None:
            return

        tap_id = self.low_tap_id
        self.low_status_var.set(f"Destroying tap {tap_id}...")

        def worker() -> None:
            destroy_process_tap(tap_id)
            self._post_ui(lambda: self._on_low_tap_destroyed(tap_id))

        def on_error(exc: Exception) -> None:
            self.low_status_var.set("Destroying the low-level tap failed.")
            self._report_error("Destroy Low-Level Tap", exc)

        self._run_background("Destroy Low-Level Tap", worker, on_error=on_error)

    def _on_low_tap_destroyed(self, tap_id: int) -> None:
        self.low_tap_id = None
        self.low_tap_description = None
        self.low_recorder = None
        self.low_status_var.set(f"Destroyed tap {tap_id}.")
        self._log(f"Destroyed low-level tap {tap_id}.")

    def _format_optional_value(self, value: object | None) -> str:
        return "n/a" if value is None else str(value)

    def _format_path_display(self, path: Path | None) -> str:
        if path is None:
            return "streaming only"

        display = str(path.expanduser())
        home = str(Path.home())
        if display.startswith(home):
            display = display.replace(home, "~", 1)
        if len(display) > 42:
            display = f"...{display[-39:]}"
        return display

    def _format_audio_format(
        self,
        sample_rate: float | None,
        num_channels: int | None,
        is_float: bool | None,
    ) -> str:
        if sample_rate is None or num_channels is None or is_float is None:
            return "n/a"
        sample_rate_khz = sample_rate / 1000
        return f"{sample_rate_khz:.1f} kHz / {num_channels} ch / float={is_float}"

    def _format_low_tap_scope(self, description: TapDescription | None) -> str:
        if description is None:
            return "n/a"

        process_count = len(description.processes)
        if description.is_exclusive:
            scope = (
                "System audio"
                if process_count == 0
                else f"System audio excluding {process_count} process(es)"
            )
        else:
            scope = (
                "No target processes set"
                if process_count == 0
                else f"{process_count} selected process(es)"
            )

        mixdown = "mono mixdown" if description.is_mono else "stereo mixdown"
        return f"{scope}, {mixdown}"

    def _refresh_high_live_state(self) -> None:
        session = self.high_session
        (
            buffers,
            byte_count,
            frame_count,
            last_size,
            last_update_time,
            reference_time,
        ) = (
            self.high_telemetry.snapshot()
        )

        callback_age = (
            f"{reference_time - last_update_time:.1f}s ago"
            if last_update_time
            else "n/a"
        )
        if session is None:
            self.high_session_state_vars["source"].set("n/a")
            self.high_session_state_vars["tap_id"].set("n/a")
            self.high_session_state_vars["active"].set("False")
            self.high_session_state_vars["output"].set(
                self._format_path_display(self.high_last_output_path)
                if self.high_last_output_path is not None
                else "n/a"
            )
            self.high_session_state_vars["excluded"].set("None")
            self.high_recorder_state_vars["frames"].set("0")
            self.high_recorder_state_vars["duration"].set("0.00s")
            self.high_recorder_state_vars["format"].set("n/a")
        else:
            source = (
                self._format_process(session.source_process)
                if session.source_process is not None
                else "System audio"
            )
            excluded = (
                ", ".join(process.name for process in session.excluded_processes)
                if session.excluded_processes
                else "None"
            )
            self.high_session_state_vars["source"].set(source)
            self.high_session_state_vars["tap_id"].set(
                self._format_optional_value(session.tap_id)
            )
            self.high_session_state_vars["active"].set(str(session.is_recording))
            self.high_session_state_vars["output"].set(
                self._format_path_display(session.output_path)
            )
            self.high_session_state_vars["excluded"].set(excluded)
            self.high_recorder_state_vars["frames"].set(str(session.frames_recorded))
            self.high_recorder_state_vars["duration"].set(
                f"{session.duration_seconds:.2f}s"
            )
            self.high_recorder_state_vars["format"].set(
                self._format_audio_format(
                    session.sample_rate,
                    session.num_channels,
                    session.is_float,
                )
            )

        self.high_callback_state_vars["buffers"].set(str(buffers))
        self.high_callback_state_vars["frames"].set(str(frame_count))
        self.high_callback_state_vars["bytes"].set(str(byte_count))
        self.high_callback_state_vars["last_size"].set(str(last_size))
        self.high_callback_state_vars["last_update"].set(callback_age)

    def _refresh_low_live_state(self) -> None:
        recorder = self.low_recorder
        description = self.low_tap_description
        (
            buffers,
            byte_count,
            frame_count,
            last_size,
            last_update_time,
            reference_time,
        ) = self.low_telemetry.snapshot()
        callback_age = (
            f"{reference_time - last_update_time:.1f}s ago"
            if last_update_time
            else "n/a"
        )

        self.low_tap_state_vars["tap_id"].set(
            self._format_optional_value(self.low_tap_id)
        )
        self.low_tap_state_vars["name"].set(
            description.name if description is not None else "n/a"
        )
        self.low_tap_state_vars["scope"].set(
            self._format_low_tap_scope(description)
        )
        self.low_tap_state_vars["private"].set(
            self._format_optional_value(
                description.is_private if description is not None else None
            )
        )
        self.low_tap_state_vars["mute_behavior"].set(
            description.mute_behavior.name if description is not None else "n/a"
        )

        output_path = (
            self.low_last_output_path
            if recorder is None
            else recorder.output_path
        )
        self.low_recorder_state_vars["active"].set(
            str(recorder.is_recording) if recorder is not None else "False"
        )
        self.low_recorder_state_vars["output"].set(
            self._format_path_display(output_path)
        )
        self.low_recorder_state_vars["frames"].set(
            str(recorder.frames_recorded) if recorder is not None else "0"
        )
        self.low_recorder_state_vars["duration"].set(
            f"{recorder.duration_seconds:.2f}s" if recorder is not None else "0.00s"
        )
        self.low_recorder_state_vars["format"].set(
            self._format_audio_format(
                recorder.sample_rate if recorder is not None else None,
                recorder.num_channels if recorder is not None else None,
                recorder.is_float if recorder is not None else None,
            )
        )

        self.low_callback_state_vars["buffers"].set(str(buffers))
        self.low_callback_state_vars["frames"].set(str(frame_count))
        self.low_callback_state_vars["bytes"].set(str(byte_count))
        self.low_callback_state_vars["last_size"].set(str(last_size))
        self.low_callback_state_vars["last_update"].set(callback_age)

    def _refresh_stats(self) -> None:
        self._refresh_high_live_state()
        self._refresh_low_live_state()
        self.root.after(250, self._refresh_stats)

    def _on_close(self) -> None:
        self._cancel_high_auto_stop()
        self._cancel_low_auto_stop()
        self.stop_playback(silent=True)

        if self.high_session is not None:
            with contextlib.suppress(Exception):
                self.high_session.close()
        if self.low_recorder is not None and self.low_recorder.is_recording:
            with contextlib.suppress(Exception):
                self.low_recorder.stop()
        if self.low_tap_id is not None:
            with contextlib.suppress(Exception):
                destroy_process_tap(self.low_tap_id)

        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ttk.Style(root).theme_use("clam")
    app = CatapDemoApp(root)
    app._log(
        "catap demo ready. Start in Process Browser, then use the recording tabs "
        "to try high-level and low-level flows."
    )
    root.mainloop()


if __name__ == "__main__":
    main()
