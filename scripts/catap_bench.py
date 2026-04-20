#!/usr/bin/env python3
"""catap bench — parallel smoke-test workbench for catap's public API.

Three lanes, one shared target:
    1. Helper        — record_process / record_system_audio
    2. Custom        — RecordingSession(TapDescription(...))
    3. Raw           — create_process_tap + AudioRecorder

Each lane shows its own liveness (heartbeat, frames/sec, peak meter) and a
post-run assertion row. Env chrome on top, log at the bottom.
"""

from __future__ import annotations

import contextlib
import math
import platform
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from array import array
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import catap  # noqa: E402
from catap import (  # noqa: E402
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


REFRESH_MS = 100
DRAIN_MS = 50
DEFAULT_DIR = Path.home() / "Desktop"
if not DEFAULT_DIR.exists():
    DEFAULT_DIR = Path.home()


# --- palette -----------------------------------------------------------------

BG = "#15171e"
PANEL = "#1d2029"
PANEL_ALT = "#262a36"
BORDER = "#2f3340"
ACCENT = "#7aa2f7"
ACCENT_BG = "#24334f"
INK = "#e0e2ea"
MUTED = "#7d838f"
OK_CLR = "#73d083"
WARN_CLR = "#e5a05a"
ERR_CLR = "#e36a70"
LOG_BG = "#101218"

BODY = ("Helvetica Neue", 12)
LABEL = ("Helvetica Neue", 11)
MONO = ("Menlo", 11)
MONO_S = ("Menlo", 10)
SECT = ("Helvetica Neue", 12, "bold")


# --- helpers -----------------------------------------------------------------


def parse_positive_float(text: str) -> float | None:
    s = text.strip()
    if not s:
        return None
    v = float(s)
    if v <= 0:
        raise ValueError("Duration must be greater than 0.")
    return v


def parse_positive_int(text: str) -> int:
    v = int(text.strip())
    if v <= 0:
        raise ValueError("Max pending must be greater than 0.")
    return v


def fmt_size(path: Path | None) -> str:
    if path is None or not path.exists():
        return "—"
    size = path.stat().st_size
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def fmt_path(path: Path | None) -> str:
    if path is None:
        return "streaming only"
    home = str(Path.home())
    text = str(path)
    if text.startswith(home):
        text = "~" + text[len(home):]
    return text


def fmt_format(sr: float | None, ch: int | None, fl: bool | None) -> str:
    if sr is None or ch is None or fl is None:
        return "—"
    return f"{sr / 1000:.1f}k / {ch}ch / float={fl}"


def compute_peak(data: bytes) -> float:
    """Peak amplitude assuming float32 samples. Subsamples for UI speed."""
    if not data or len(data) % 4 != 0:
        return 0.0
    try:
        arr = array("f")
        arr.frombytes(data)
    except (ValueError, BufferError):
        return 0.0
    stride = max(1, len(arr) // 512)
    peak = 0.0
    for i in range(0, len(arr), stride):
        v = arr[i]
        if v < 0.0:
            v = -v
        if v > peak:
            peak = v
    return peak


def configure_styles(root: tk.Tk) -> None:
    s = ttk.Style(root)
    s.theme_use("clam")
    root.configure(background=BG)

    s.configure(".", background=BG, foreground=INK, fieldbackground=LOG_BG, font=BODY)
    s.configure("TFrame", background=BG)
    s.configure("Panel.TFrame", background=PANEL)
    s.configure("Alt.TFrame", background=PANEL_ALT)
    s.configure("Env.TFrame", background=PANEL_ALT)

    s.configure("TLabel", background=BG, foreground=INK, font=BODY)
    s.configure("Muted.TLabel", background=BG, foreground=MUTED, font=LABEL)
    s.configure("Panel.TLabel", background=PANEL, foreground=INK, font=BODY)
    s.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED, font=LABEL)
    s.configure("Section.TLabel", background=PANEL, foreground=INK, font=SECT)
    s.configure("Mono.TLabel", background=PANEL, foreground=INK, font=MONO)
    s.configure("MonoMuted.TLabel", background=PANEL, foreground=MUTED, font=MONO)
    s.configure("OK.TLabel", background=PANEL, foreground=OK_CLR, font=MONO)
    s.configure("Warn.TLabel", background=PANEL, foreground=WARN_CLR, font=MONO)
    s.configure("Err.TLabel", background=PANEL, foreground=ERR_CLR, font=MONO)
    s.configure("Env.TLabel", background=PANEL_ALT, foreground=MUTED, font=MONO_S)
    s.configure("EnvValue.TLabel", background=PANEL_ALT, foreground=ACCENT, font=MONO_S)

    s.configure(
        "Panel.TLabelframe",
        background=PANEL,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        relief="solid",
    )
    s.configure("Panel.TLabelframe.Label", background=PANEL, foreground=INK, font=SECT)

    s.configure(
        "TButton",
        background=PANEL_ALT,
        foreground=INK,
        bordercolor=BORDER,
        padding=(10, 5),
        font=LABEL,
    )
    s.map("TButton", background=[("active", BORDER), ("pressed", BORDER)])
    s.configure(
        "Primary.TButton",
        background=ACCENT_BG,
        foreground=INK,
        bordercolor=ACCENT,
        padding=(10, 5),
        font=LABEL,
    )
    s.map("Primary.TButton", background=[("active", ACCENT), ("pressed", ACCENT)])

    s.configure(
        "TEntry",
        fieldbackground=LOG_BG,
        foreground=INK,
        insertcolor=INK,
        bordercolor=BORDER,
    )
    s.configure("TCombobox", fieldbackground=LOG_BG, foreground=INK, bordercolor=BORDER)
    s.configure("TCheckbutton", background=PANEL, foreground=INK, font=LABEL)
    s.map("TCheckbutton", background=[("active", PANEL)])
    s.configure("TRadiobutton", background=PANEL, foreground=INK, font=LABEL)
    s.map("TRadiobutton", background=[("active", PANEL)])

    s.configure(
        "Treeview",
        background=LOG_BG,
        fieldbackground=LOG_BG,
        foreground=INK,
        bordercolor=BORDER,
        rowheight=22,
        font=MONO_S,
    )
    s.configure(
        "Treeview.Heading",
        background=PANEL_ALT,
        foreground=INK,
        relief="flat",
        font=(BODY[0], BODY[1] - 1, "bold"),
    )
    s.map(
        "Treeview",
        background=[("selected", ACCENT_BG)],
        foreground=[("selected", INK)],
    )


# --- widgets -----------------------------------------------------------------


class PeakMeter(tk.Canvas):
    def __init__(self, parent: tk.Widget, width: int = 140, height: int = 8) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=PANEL_ALT,
            highlightthickness=0,
            bd=0,
        )
        self._width_px, self._height_px = width, height
        self._bar = self.create_rectangle(0, 0, 0, height, fill=OK_CLR, outline="")

    def set(self, level: float) -> None:
        if level <= 0:
            norm = 0.0
        else:
            db = 20 * math.log10(level)
            norm = max(0.0, min(1.0, (db + 60) / 60))
        w = int(self._width_px * norm)
        self.coords(self._bar, 0, 0, w, self._height_px)
        color = ERR_CLR if norm > 0.9 else (WARN_CLR if norm > 0.65 else OK_CLR)
        self.itemconfig(self._bar, fill=color)


class Dot(tk.Canvas):
    def __init__(self, parent: tk.Widget, size: int = 10, bg: str = PANEL) -> None:
        super().__init__(
            parent,
            width=size + 2,
            height=size + 2,
            bg=bg,
            highlightthickness=0,
            bd=0,
        )
        self._dot = self.create_oval(1, 1, size + 1, size + 1, fill=MUTED, outline="")

    def set(self, color: str) -> None:
        self.itemconfig(self._dot, fill=color)


# --- telemetry ---------------------------------------------------------------


class Telemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.buffers = 0
            self.frames = 0
            self.bytes = 0
            self._peak_instant = 0.0
            self.last_update = 0.0
            self._last_frame_mark = 0
            self._last_time_mark = 0.0
            self._rate = 0.0

    def callback(self, data: bytes, num_frames: int) -> None:
        peak = compute_peak(data)
        with self._lock:
            self.buffers += 1
            self.frames += num_frames
            self.bytes += len(data)
            if peak > self._peak_instant:
                self._peak_instant = peak
            self.last_update = time.time()

    def tick(self) -> tuple[float, float, float]:
        """UI-thread tick. Returns (rate_fps, peak, seconds_since_last_buffer)."""
        now = time.time()
        with self._lock:
            dt = now - self._last_time_mark if self._last_time_mark else 0.0
            df = self.frames - self._last_frame_mark
            if dt > 0:
                self._rate = df / dt
            self._last_frame_mark = self.frames
            self._last_time_mark = now
            peak = self._peak_instant
            self._peak_instant *= 0.75
            age = (now - self.last_update) if self.last_update else float("inf")
            return self._rate, peak, age

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "buffers": self.buffers,
                "frames": self.frames,
                "bytes": self.bytes,
                "rate": self._rate,
            }


# --- environment chrome ------------------------------------------------------


class EnvBar(ttk.Frame):
    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent, style="Env.TFrame", padding=(12, 7))
        version = getattr(catap, "__version__", "—")
        py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        mac = platform.mac_ver()[0] or "—"
        arch = platform.machine() or "—"
        text = f"catap {version}   ·   Python {py}   ·   macOS {mac}   ·   {arch}"
        ttk.Label(self, text=text, style="Env.TLabel").pack(side=tk.LEFT)
        ttk.Label(self, text="      last capture: ", style="Env.TLabel").pack(side=tk.LEFT)
        self.last_format = tk.StringVar(value="—")
        ttk.Label(self, textvariable=self.last_format, style="EnvValue.TLabel").pack(side=tk.LEFT)

    def update_last(
        self,
        sr: float | None,
        ch: int | None,
        fl: bool | None,
        dur: float,
    ) -> None:
        if sr is None or ch is None:
            self.last_format.set("—")
            return
        self.last_format.set(f"{sr / 1000:.1f}k / {ch}ch / float={fl} / {dur:.2f}s")


# --- target panel ------------------------------------------------------------


class TargetPanel:
    def __init__(self, parent: tk.Widget, app: BenchApp) -> None:
        self.app = app
        self.mode = tk.StringVar(value="app")
        self.query = tk.StringVar()
        self.show_idle = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="0 shown")
        self.selected = tk.StringVar(value="nothing selected")
        self.processes: list[AudioProcess] = []
        self.index: dict[str, AudioProcess] = {}
        self.exclusions: list[AudioProcess] = []
        self._build(parent)

    def _build(self, parent: tk.Widget) -> None:
        frame = ttk.LabelFrame(parent, text="Target", style="Panel.TLabelframe", padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=3)
        frame.rowconfigure(6, weight=1)

        mode = ttk.Frame(frame, style="Panel.TFrame")
        mode.grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Radiobutton(mode, text="App", variable=self.mode, value="app").pack(side=tk.LEFT)
        ttk.Radiobutton(mode, text="System", variable=self.mode, value="system").pack(
            side=tk.LEFT, padx=(14, 0)
        )

        search = ttk.Frame(frame, style="Panel.TFrame")
        search.grid(row=1, column=0, sticky="ew")
        search.columnconfigure(0, weight=1)
        entry = ttk.Entry(search, textvariable=self.query)
        entry.grid(row=0, column=0, sticky="ew")
        entry.bind("<Return>", lambda _e: self.resolve())
        ttk.Button(search, text="Resolve", command=self.resolve).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(search, text="↻", command=self.refresh, width=3).grid(row=0, column=2, padx=(6, 0))

        toggles = ttk.Frame(frame, style="Panel.TFrame")
        toggles.grid(row=2, column=0, sticky="ew", pady=(6, 6))
        toggles.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            toggles, text="show idle", variable=self.show_idle, command=self.refresh
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(toggles, textvariable=self.status, style="PanelMuted.TLabel").grid(
            row=0, column=1, sticky="e"
        )

        tree_frame = ttk.Frame(frame, style="Panel.TFrame")
        tree_frame.grid(row=3, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("st", "name", "pid"),
            show="headings",
            selectmode="extended",
            height=12,
        )
        self.tree.heading("st", text="·")
        self.tree.heading("name", text="process")
        self.tree.heading("pid", text="pid")
        self.tree.column("st", width=22, anchor="center")
        self.tree.column("name", width=150, anchor="w")
        self.tree.column("pid", width=55, anchor="e")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        ttk.Label(
            frame,
            textvariable=self.selected,
            style="MonoMuted.TLabel",
            wraplength=255,
            justify=tk.LEFT,
        ).grid(row=4, column=0, sticky="ew", pady=(8, 10))

        ttk.Label(
            frame, text="Exclusions (system mode)", style="PanelMuted.TLabel"
        ).grid(row=5, column=0, sticky="w")
        excl_frame = ttk.Frame(frame, style="Panel.TFrame")
        excl_frame.grid(row=6, column=0, sticky="nsew", pady=(4, 0))
        excl_frame.columnconfigure(0, weight=1)
        excl_frame.rowconfigure(0, weight=1)
        self.excl_list = tk.Listbox(
            excl_frame,
            height=4,
            bg=LOG_BG,
            fg=INK,
            activestyle=tk.NONE,
            exportselection=False,
            relief=tk.SOLID,
            borderwidth=1,
            highlightthickness=0,
            selectbackground=ACCENT_BG,
            selectforeground=INK,
            font=MONO_S,
        )
        self.excl_list.grid(row=0, column=0, sticky="nsew")
        excl_btns = ttk.Frame(frame, style="Panel.TFrame")
        excl_btns.grid(row=7, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(excl_btns, text="+ selected", command=self._add_excl).pack(side=tk.LEFT)
        ttk.Button(excl_btns, text="−", command=self._rm_excl, width=3).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(excl_btns, text="clear", command=self._clear_excl).pack(side=tk.LEFT, padx=(6, 0))

        self._refresh_excl_list()

    # -- public --

    def is_app_mode(self) -> bool:
        return self.mode.get() == "app"

    def selected_process(self) -> AudioProcess | None:
        sel = self.tree.selection()
        return self.index.get(sel[0]) if sel else None

    def selected_processes(self) -> list[AudioProcess]:
        return [self.index[iid] for iid in self.tree.selection() if iid in self.index]

    def require_process(self) -> AudioProcess:
        p = self.selected_process()
        if p is None:
            raise ValueError("Select a process in the Target panel first.")
        return p

    def require_processes(self) -> list[AudioProcess]:
        procs = self.selected_processes()
        if not procs:
            raise ValueError("Select one or more processes in the Target panel first.")
        return procs

    # -- actions --

    def refresh(self) -> None:
        prior_keys = {(p.audio_object_id, p.pid) for p in self.selected_processes()}
        try:
            all_procs = list_audio_processes()
        except Exception as exc:
            self.status.set("refresh failed")
            self.app.report_error("Refresh", exc)
            return
        procs = all_procs if self.show_idle.get() else [p for p in all_procs if p.is_outputting]
        self.processes = procs
        self.index.clear()
        self.tree.delete(*self.tree.get_children())
        restored: list[str] = []
        for i, p in enumerate(procs):
            iid = f"p{i}"
            self.index[iid] = p
            glyph = "●" if p.is_outputting else "○"
            self.tree.insert("", tk.END, iid=iid, values=(glyph, p.name, p.pid))
            if (p.audio_object_id, p.pid) in prior_keys:
                restored.append(iid)
        if restored:
            self.tree.selection_set(restored)
            self.tree.focus(restored[0])
            self.tree.see(restored[0])
        self.status.set(f"{len(procs)} shown")

    def resolve(self) -> None:
        q = self.query.get().strip()
        if not q:
            return
        try:
            p = find_process_by_name(q)
        except Exception as exc:
            self.app.report_error("Resolve", exc)
            return
        if p is None:
            messagebox.showinfo("Resolve", f"No match for {q!r}.")
            return
        if not self.show_idle.get() and not p.is_outputting:
            self.show_idle.set(True)
            self.refresh()
        for iid, listed in self.index.items():
            if listed.audio_object_id == p.audio_object_id and listed.pid == p.pid:
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.see(iid)
                return
        messagebox.showinfo("Resolve", f"{p.name!r} found but not listed.")

    def _on_select(self, _evt: tk.Event[tk.Misc]) -> None:
        procs = self.selected_processes()
        if not procs:
            self.selected.set("nothing selected")
        elif len(procs) == 1:
            p = procs[0]
            self.selected.set(
                f"{p.name}\n{p.bundle_id or '—'}   ·   pid {p.pid}   ·   audio {p.audio_object_id}"
            )
        else:
            names = ", ".join(p.name for p in procs[:3])
            if len(procs) > 3:
                names += f", +{len(procs) - 3} more"
            self.selected.set(f"{len(procs)} selected\n{names}")

    def _add_excl(self) -> None:
        existing = {e.audio_object_id for e in self.exclusions}
        added = 0
        for p in self.selected_processes():
            if p.audio_object_id in existing:
                continue
            self.exclusions.append(p)
            existing.add(p.audio_object_id)
            added += 1
        if added:
            self.exclusions.sort(key=lambda x: (x.name.casefold(), x.pid))
            self._refresh_excl_list()

    def _rm_excl(self) -> None:
        sel = self.excl_list.curselection()
        if not sel or not self.exclusions:
            return
        del self.exclusions[sel[0]]
        self._refresh_excl_list()

    def _clear_excl(self) -> None:
        self.exclusions.clear()
        self._refresh_excl_list()

    def _refresh_excl_list(self) -> None:
        self.excl_list.delete(0, tk.END)
        if not self.exclusions:
            self.excl_list.configure(fg=MUTED)
            self.excl_list.insert(tk.END, "(none)")
        else:
            self.excl_list.configure(fg=INK)
            for p in self.exclusions:
                self.excl_list.insert(tk.END, f"{p.name}  ·  pid {p.pid}")


# --- shared capture defaults -------------------------------------------------


class DefaultsBar(ttk.Frame):
    def __init__(self, parent: tk.Widget, app: BenchApp) -> None:
        super().__init__(parent, style="Panel.TFrame", padding=(10, 8))
        self.app = app
        self.output_dir = tk.StringVar(value=str(DEFAULT_DIR))
        self.duration = tk.StringVar(value="5")
        self.max_pending = tk.StringVar(value="256")
        self.write_wav = tk.BooleanVar(value=True)
        self.enable_cb = tk.BooleanVar(value=True)
        self._build()

    def _build(self) -> None:
        self.columnconfigure(1, weight=1)

        ttk.Label(self, text="output", style="PanelMuted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.output_dir).grid(
            row=0, column=1, sticky="ew", padx=(8, 4)
        )
        ttk.Button(self, text="…", width=2, command=self._pick_dir).grid(
            row=0, column=2, padx=(0, 14)
        )

        ttk.Label(self, text="duration", style="PanelMuted.TLabel").grid(row=0, column=3, sticky="w")
        ttk.Entry(self, textvariable=self.duration, width=6).grid(
            row=0, column=4, sticky="w", padx=(8, 14)
        )

        ttk.Label(self, text="max pending", style="PanelMuted.TLabel").grid(
            row=0, column=5, sticky="w"
        )
        ttk.Entry(self, textvariable=self.max_pending, width=6).grid(
            row=0, column=6, sticky="w", padx=(8, 14)
        )

        ttk.Checkbutton(self, text="write WAV", variable=self.write_wav).grid(
            row=0, column=7, sticky="w", padx=(0, 10)
        )
        ttk.Checkbutton(self, text="callback", variable=self.enable_cb).grid(
            row=0, column=8, sticky="w"
        )

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(
            initialdir=self.output_dir.get() or str(DEFAULT_DIR), mustexist=True
        )
        if d:
            self.output_dir.set(d)

    def common_args(
        self, lane_filename: str
    ) -> tuple[Path | None, float | None, int, bool]:
        duration = parse_positive_float(self.duration.get())
        max_pending = parse_positive_int(self.max_pending.get())
        cb_on = self.enable_cb.get()
        if self.write_wav.get():
            out_dir = Path(self.output_dir.get().strip()).expanduser()
            if not out_dir.is_dir():
                raise ValueError(f"Output folder doesn't exist: {out_dir}")
            output: Path | None = out_dir / lane_filename
        else:
            output = None
        if output is None and not cb_on:
            raise ValueError("Enable WAV writing or callback before starting.")
        return output, duration, max_pending, cb_on


# --- lane base ---------------------------------------------------------------


class Lane:
    """Base for a capture lane. One public API surface per subclass."""

    title: str = ""
    signature: str = ""
    output_filename: str = "catap-bench.wav"

    def __init__(self, parent: tk.Widget, app: BenchApp) -> None:
        self.app = app
        self.telemetry = Telemetry()
        self.running = False
        self.last_output: Path | None = None
        self.stop_after_id: str | None = None
        self._stopper: Callable[[], None] | None = None
        self._shared_args: tuple[Path | None, float | None, int, bool] | None = None
        self._build(parent)

    # -- subclass hooks --

    def _build_options(self, parent: tk.Widget) -> None:
        return

    def _start_impl(self) -> Callable[[], None]:
        raise NotImplementedError

    def _read_format(self) -> tuple[float | None, int | None, bool | None, float]:
        return None, None, None, 0.0

    # -- UI --

    def _build(self, parent: tk.Widget) -> None:
        frame = ttk.LabelFrame(parent, text=self.title, style="Panel.TLabelframe", padding=10)
        frame.pack(fill=tk.X, pady=(0, 8))
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text=self.signature,
            style="MonoMuted.TLabel",
            wraplength=900,
            justify=tk.LEFT,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        opts = ttk.Frame(frame, style="Panel.TFrame")
        opts.grid(row=1, column=0, sticky="w", pady=(0, 8))
        self._build_options(opts)

        action = ttk.Frame(frame, style="Panel.TFrame")
        action.grid(row=2, column=0, sticky="ew")
        action.columnconfigure(4, weight=1)

        self.btn_start = ttk.Button(action, text="Start", style="Primary.TButton", command=self.start)
        self.btn_start.grid(row=0, column=0)
        self.btn_stop = ttk.Button(action, text="Stop", command=self.stop, state=tk.DISABLED)
        self.btn_stop.grid(row=0, column=1, padx=(6, 14))

        self.dot = Dot(action, size=10)
        self.dot.grid(row=0, column=2)
        self.rate_var = tk.StringVar(value="—")
        ttk.Label(action, textvariable=self.rate_var, style="Mono.TLabel", width=11).grid(
            row=0, column=3, padx=(8, 10)
        )
        self.meter = PeakMeter(action, width=160, height=10)
        self.meter.grid(row=0, column=4, sticky="w")

        self.btn_play = ttk.Button(action, text="▶ play", command=self.play)
        self.btn_play.grid(row=0, column=5, padx=(10, 0))
        self.btn_reveal = ttk.Button(action, text="reveal", command=self.reveal)
        self.btn_reveal.grid(row=0, column=6, padx=(6, 0))

        self.assert_var = tk.StringVar(value="idle")
        self.assert_label = ttk.Label(
            frame,
            textvariable=self.assert_var,
            style="MonoMuted.TLabel",
            justify=tk.LEFT,
            wraplength=900,
        )
        self.assert_label.grid(row=3, column=0, sticky="w", pady=(10, 0))

    # -- lifecycle --

    def start(self) -> None:
        if self.running:
            return
        try:
            self.telemetry.reset()
            output, duration, max_pending, cb_on = self.app.defaults.common_args(self.output_filename)
            self._shared_args = (output, duration, max_pending, cb_on)
        except Exception as exc:
            self._set_status(f"✗ {exc}", "Err.TLabel")
            self.app.log(f"{self.title}: prepare failed: {exc}")
            return

        self.running = True
        self.btn_start.state(["disabled"])
        self.btn_stop.state(["!disabled"])
        self._set_status("starting…", "MonoMuted.TLabel")
        self.dot.set(WARN_CLR)

        def worker() -> None:
            try:
                stopper = self._start_impl()
                self.app.post_ui(lambda: self._on_started(stopper, duration))
            except Exception as exc:
                self.app.post_ui(lambda exc=exc: self._on_fail(exc))

        self.app.run_background(f"{self.title} start", worker)

    def _on_started(self, stopper: Callable[[], None], duration: float | None) -> None:
        self._stopper = stopper
        target = fmt_path(self.last_output) if self.last_output else "streaming"
        tail = f"   auto-stop in {duration:g}s" if duration is not None else ""
        self._set_status(f"recording   →   {target}{tail}", "Mono.TLabel")
        self.dot.set(OK_CLR)
        self.app.log(f"{self.title}: started")
        if duration is not None:
            self.stop_after_id = self.app.root.after(int(duration * 1000), self.stop)

    def _on_fail(self, exc: Exception) -> None:
        self.running = False
        self._stopper = None
        self.btn_start.state(["!disabled"])
        self.btn_stop.state(["disabled"])
        self.dot.set(ERR_CLR)
        self.meter.set(0.0)
        self.rate_var.set("—")
        self._set_status(f"✗ {exc}", "Err.TLabel")
        self.app.log(f"{self.title}: {exc}")

    def stop(self) -> None:
        if not self.running:
            return
        if self.stop_after_id is not None:
            self.app.root.after_cancel(self.stop_after_id)
            self.stop_after_id = None
        stopper = self._stopper
        if stopper is None:
            return
        self._set_status("stopping…", "MonoMuted.TLabel")
        self.dot.set(WARN_CLR)

        def worker() -> None:
            try:
                stopper()
                self.app.post_ui(self._on_stopped)
            except Exception as exc:
                self.app.post_ui(lambda exc=exc: self._on_fail(exc))

        self.app.run_background(f"{self.title} stop", worker)

    def _on_stopped(self) -> None:
        self.running = False
        self._stopper = None
        self.btn_start.state(["!disabled"])
        self.btn_stop.state(["disabled"])
        self.dot.set(MUTED)
        self.meter.set(0.0)
        self.rate_var.set("—")
        sr, ch, fl, dur = self._read_format_safe()
        assertion, ok = self._build_assertion(sr, ch, fl, dur)
        self._set_status(assertion, "OK.TLabel" if ok else "Warn.TLabel")
        self.app.env.update_last(sr, ch, fl, dur)
        self.app.log(f"{self.title}: {assertion}")

    def _read_format_safe(self) -> tuple[float | None, int | None, bool | None, float]:
        try:
            return self._read_format()
        except Exception:
            return None, None, None, 0.0

    def _build_assertion(
        self,
        sr: float | None,
        ch: int | None,
        fl: bool | None,
        dur: float,
    ) -> tuple[str, bool]:
        parts: list[str] = []
        ok = dur > 0
        if self.last_output is None:
            parts.append("streaming only")
        elif self.last_output.exists():
            size = self.last_output.stat().st_size
            parts.append(f"✓ file {fmt_size(self.last_output)}")
            if size <= 44:
                ok = False
        else:
            parts.append("✗ file missing")
            ok = False
        parts.append(f"{dur:.2f}s")
        parts.append(fmt_format(sr, ch, fl))
        snap = self.telemetry.snapshot()
        parts.append(f"{snap['buffers']} buf · {snap['frames']} fr")
        return "   ·   ".join(parts), ok

    def _set_status(self, text: str, style: str) -> None:
        self.assert_var.set(text)
        self.assert_label.configure(style=style)

    def tick(self) -> None:
        if not self.running:
            return
        rate, peak, age = self.telemetry.tick()
        self.meter.set(peak)
        if rate > 0:
            self.rate_var.set(f"{int(rate):,} fr/s")
        else:
            self.rate_var.set("0 fr/s")
        if age > 1.0:
            self.dot.set(WARN_CLR)
        elif age < 0.3:
            self.dot.set(OK_CLR)

    def play(self) -> None:
        self.app.player.play(self.last_output, self.title)

    def reveal(self) -> None:
        self.app.player.reveal(self.last_output, self.title)

    def cleanup(self) -> None:
        if self.running and self._stopper is not None:
            with contextlib.suppress(Exception):
                self._stopper()


# --- lanes -------------------------------------------------------------------


class HelperLane(Lane):
    title = "Helper   ·   record_process / record_system_audio"
    signature = (
        "record_process(target, output_path=…, mute=…, on_data=…)"
        "      record_system_audio(output_path=…, exclude=…, on_data=…)"
    )
    output_filename = "catap-bench-helper.wav"

    def __init__(self, parent: tk.Widget, app: BenchApp) -> None:
        self.mute = tk.BooleanVar(value=False)
        self.session: RecordingSession | None = None
        super().__init__(parent, app)

    def _build_options(self, parent: tk.Widget) -> None:
        ttk.Checkbutton(
            parent, text="mute target while capturing (app mode)", variable=self.mute
        ).pack(side=tk.LEFT)

    def _start_impl(self) -> Callable[[], None]:
        assert self._shared_args is not None
        output, _duration, max_pending, cb_on = self._shared_args
        on_data = self.telemetry.callback if cb_on else None
        target = self.app.target
        if target.is_app_mode():
            procs = target.require_processes()
            if len(procs) > 1:
                self.app.log(
                    f"Helper takes one process; using {procs[0].name!r} (ignored {len(procs) - 1})."
                )
            p = procs[0]
            session = record_process(
                p,
                output_path=output,
                mute=self.mute.get(),
                on_data=on_data,
                max_pending_buffers=max_pending,
            )
        else:
            session = record_system_audio(
                output_path=output,
                exclude=target.exclusions,
                on_data=on_data,
                max_pending_buffers=max_pending,
            )
        session.start()
        self.session = session
        self.last_output = session.output_path
        return session.close

    def _read_format(self) -> tuple[float | None, int | None, bool | None, float]:
        s = self.session
        if s is None:
            return None, None, None, 0.0
        return s.sample_rate, s.num_channels, s.is_float, s.duration_seconds


class CustomLane(Lane):
    title = "Custom   ·   RecordingSession(TapDescription)"
    signature = (
        "RecordingSession("
        "TapDescription.[mono|stereo]_{mixdown_of_processes|global_tap_excluding}([…]),"
        " output_path=…, on_data=…)"
    )
    output_filename = "catap-bench-custom.wav"

    def __init__(self, parent: tk.Widget, app: BenchApp) -> None:
        self.mono = tk.BooleanVar(value=False)
        self.private = tk.BooleanVar(value=True)
        self.mute_behavior = tk.StringVar(value="UNMUTED")
        self.session: RecordingSession | None = None
        super().__init__(parent, app)

    def _build_options(self, parent: tk.Widget) -> None:
        ttk.Checkbutton(parent, text="mono", variable=self.mono).pack(side=tk.LEFT)
        ttk.Checkbutton(parent, text="private", variable=self.private).pack(
            side=tk.LEFT, padx=(14, 0)
        )
        ttk.Label(parent, text="mute", style="PanelMuted.TLabel").pack(
            side=tk.LEFT, padx=(14, 6)
        )
        ttk.Combobox(
            parent,
            textvariable=self.mute_behavior,
            state="readonly",
            values=[b.name for b in TapMuteBehavior],
            width=20,
        ).pack(side=tk.LEFT)

    def _build_description(self) -> TapDescription:
        t = self.app.target
        if t.is_app_mode():
            procs = t.require_processes()
            ids = [p.audio_object_id for p in procs]
            desc = (
                TapDescription.mono_mixdown_of_processes(ids)
                if self.mono.get()
                else TapDescription.stereo_mixdown_of_processes(ids)
            )
        else:
            ids = [e.audio_object_id for e in t.exclusions]
            desc = (
                TapDescription.mono_global_tap_excluding(ids)
                if self.mono.get()
                else TapDescription.stereo_global_tap_excluding(ids)
            )
        desc.name = "catap bench custom"
        desc.is_private = self.private.get()
        desc.mute_behavior = TapMuteBehavior[self.mute_behavior.get()]
        return desc

    def _start_impl(self) -> Callable[[], None]:
        assert self._shared_args is not None
        output, _duration, max_pending, cb_on = self._shared_args
        on_data = self.telemetry.callback if cb_on else None
        desc = self._build_description()
        session = RecordingSession(
            desc,
            output_path=output,
            on_data=on_data,
            max_pending_buffers=max_pending,
        )
        session.start()
        self.session = session
        self.last_output = session.output_path
        return session.close

    def _read_format(self) -> tuple[float | None, int | None, bool | None, float]:
        s = self.session
        if s is None:
            return None, None, None, 0.0
        return s.sample_rate, s.num_channels, s.is_float, s.duration_seconds


class RawLane(Lane):
    title = "Raw   ·   create_process_tap + AudioRecorder"
    signature = (
        "tap_id = create_process_tap(desc)"
        "      AudioRecorder(tap_id, output_path=…, on_data=…).start()"
        "      destroy_process_tap(tap_id)"
    )
    output_filename = "catap-bench-raw.wav"

    def __init__(self, parent: tk.Widget, app: BenchApp) -> None:
        self.mono = tk.BooleanVar(value=False)
        self.private = tk.BooleanVar(value=True)
        self.mute_behavior = tk.StringVar(value="UNMUTED")
        self.tap_id: int | None = None
        self.recorder: AudioRecorder | None = None
        super().__init__(parent, app)

    def _build_options(self, parent: tk.Widget) -> None:
        ttk.Checkbutton(parent, text="mono", variable=self.mono).pack(side=tk.LEFT)
        ttk.Checkbutton(parent, text="private", variable=self.private).pack(
            side=tk.LEFT, padx=(14, 0)
        )
        ttk.Label(parent, text="mute", style="PanelMuted.TLabel").pack(
            side=tk.LEFT, padx=(14, 6)
        )
        ttk.Combobox(
            parent,
            textvariable=self.mute_behavior,
            state="readonly",
            values=[b.name for b in TapMuteBehavior],
            width=20,
        ).pack(side=tk.LEFT)

    def _build_description(self) -> TapDescription:
        t = self.app.target
        if t.is_app_mode():
            procs = t.require_processes()
            ids = [p.audio_object_id for p in procs]
            desc = (
                TapDescription.mono_mixdown_of_processes(ids)
                if self.mono.get()
                else TapDescription.stereo_mixdown_of_processes(ids)
            )
        else:
            ids = [e.audio_object_id for e in t.exclusions]
            desc = (
                TapDescription.mono_global_tap_excluding(ids)
                if self.mono.get()
                else TapDescription.stereo_global_tap_excluding(ids)
            )
        desc.name = "catap bench raw"
        desc.is_private = self.private.get()
        desc.mute_behavior = TapMuteBehavior[self.mute_behavior.get()]
        return desc

    def _start_impl(self) -> Callable[[], None]:
        assert self._shared_args is not None
        output, _duration, max_pending, cb_on = self._shared_args
        on_data = self.telemetry.callback if cb_on else None
        desc = self._build_description()
        tap_id = create_process_tap(desc)
        try:
            recorder = AudioRecorder(
                tap_id,
                output_path=output,
                on_data=on_data,
                max_pending_buffers=max_pending,
            )
            recorder.start()
        except Exception:
            with contextlib.suppress(Exception):
                destroy_process_tap(tap_id)
            raise
        self.tap_id = tap_id
        self.recorder = recorder
        self.last_output = recorder.output_path

        def stop() -> None:
            with contextlib.suppress(Exception):
                recorder.stop()
            with contextlib.suppress(Exception):
                destroy_process_tap(tap_id)

        return stop

    def _read_format(self) -> tuple[float | None, int | None, bool | None, float]:
        r = self.recorder
        if r is None:
            return None, None, None, 0.0
        return r.sample_rate, r.num_channels, r.is_float, r.duration_seconds


# --- playback ----------------------------------------------------------------


class Player:
    def __init__(
        self, root: tk.Tk, status: tk.StringVar, log: Callable[[str], None]
    ) -> None:
        self.root = root
        self.status = status
        self.log = log
        self.process: subprocess.Popen[bytes] | None = None
        self.path: Path | None = None
        self._poll_id: str | None = None

    def play(self, path: Path | None, label: str) -> None:
        if path is None or not path.exists():
            messagebox.showinfo("Play", "No output file to play yet.")
            return
        self.stop(silent=True)
        try:
            self.process = subprocess.Popen(["afplay", str(path)])
        except FileNotFoundError as exc:
            messagebox.showerror("Play", str(exc))
            return
        self.path = path
        self.status.set(f"▶ {label}   {path.name}")
        self.log(f"play {path}")
        self._schedule_poll()

    def reveal(self, path: Path | None, _label: str) -> None:
        if path is None or not path.exists():
            messagebox.showinfo("Reveal", "No output file to reveal.")
            return
        subprocess.run(["open", "-R", str(path)], check=False)

    def stop(self, *, silent: bool = False) -> None:
        self._cancel_poll()
        p = self.process
        if p is None:
            if not silent:
                self.status.set("playback idle")
            return
        if p.poll() is None:
            p.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(timeout=0.4)
            if p.poll() is None:
                p.kill()
        self.process = None
        self.path = None
        if not silent:
            self.status.set("playback idle")

    def _schedule_poll(self) -> None:
        self._cancel_poll()
        self._poll_id = self.root.after(200, self._poll)

    def _cancel_poll(self) -> None:
        if self._poll_id is not None:
            with contextlib.suppress(Exception):
                self.root.after_cancel(self._poll_id)
            self._poll_id = None

    def _poll(self) -> None:
        p = self.process
        if p is None:
            self._poll_id = None
            return
        if p.poll() is None:
            self._poll_id = self.root.after(200, self._poll)
            return
        name = self.path.name if self.path else "?"
        self.status.set(f"playback done   {name}")
        self.process = None
        self.path = None
        self._poll_id = None


# --- app shell ---------------------------------------------------------------


class BenchApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("catap bench")
        root.geometry("1360x900")
        root.minsize(1180, 760)
        configure_styles(root)

        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.playback_status = tk.StringVar(value="playback idle")
        self.player = Player(root, self.playback_status, self.log)

        self._build()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(DRAIN_MS, self._drain)
        root.after(REFRESH_MS, self._tick)
        self.target.refresh()

    def _build(self) -> None:
        self.env = EnvBar(self.root)
        self.env.pack(side=tk.TOP, fill=tk.X)

        log_wrap = ttk.LabelFrame(
            self.root, text="event log", style="Panel.TLabelframe", padding=8
        )
        log_wrap.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 10))
        log_wrap.columnconfigure(0, weight=1)
        ttk.Label(
            log_wrap, textvariable=self.playback_status, style="MonoMuted.TLabel"
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.log_text = scrolledtext.ScrolledText(
            log_wrap,
            height=6,
            wrap=tk.WORD,
            state=tk.DISABLED,
            background=LOG_BG,
            foreground=INK,
            insertbackground=INK,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
            font=MONO_S,
        )
        self.log_text.grid(row=1, column=0, sticky="ew")

        main = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        self.defaults = DefaultsBar(main, self)
        self.defaults.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        sidebar = ttk.Frame(main, width=290)
        sidebar.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        sidebar.grid_propagate(False)
        self.target = TargetPanel(sidebar, self)

        workspace = ttk.Frame(main)
        workspace.grid(row=1, column=1, sticky="nsew")
        workspace.columnconfigure(0, weight=1)
        self.lanes: list[Lane] = [
            HelperLane(workspace, self),
            CustomLane(workspace, self),
            RawLane(workspace, self),
        ]

    # -- threading glue --

    def post_ui(self, cb: Callable[[], None]) -> None:
        self._ui_queue.put(cb)

    def run_background(
        self,
        name: str,
        worker: Callable[[], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        def runner() -> None:
            try:
                worker()
            except Exception as exc:
                if on_error is None:
                    self.post_ui(lambda exc=exc: self.report_error(name, exc))
                else:
                    self.post_ui(lambda exc=exc: on_error(exc))

        threading.Thread(target=runner, daemon=True).start()

    def report_error(self, action: str, exc: Exception) -> None:
        self.log(f"{action} failed: {exc}")
        messagebox.showerror(f"{action} failed", str(exc))

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _drain(self) -> None:
        while True:
            try:
                cb = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            cb()
        self.root.after(DRAIN_MS, self._drain)

    def _tick(self) -> None:
        for lane in self.lanes:
            lane.tick()
        self.root.after(REFRESH_MS, self._tick)

    def _on_close(self) -> None:
        for lane in self.lanes:
            with contextlib.suppress(Exception):
                lane.cleanup()
        self.player.stop(silent=True)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = BenchApp(root)
    app.log(f"catap bench ready   ·   catap {getattr(catap, '__version__', '?')}")
    root.mainloop()


if __name__ == "__main__":
    main()
