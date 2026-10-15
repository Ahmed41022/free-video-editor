"""Tkinter desktop interface."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import __version__
from .core import (
    DEFAULT_QUALITY,
    QUALITY_PRESETS,
    VIDEO_EXTENSIONS,
    ConversionCancelled,
    ConversionError,
    ConversionJob,
    convert,
    find_ffmpeg,
    is_video_file,
    output_path_for,
)

_FILE_TYPES = [
    ("Video files", " ".join(f"*{ext}" for ext in VIDEO_EXTENSIONS)),
    ("All files", "*.*"),
]


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class ConverterApp(ttk.Frame):
    def __init__(self, master: tk.Tk, ffmpeg: str):
        super().__init__(master, padding=12)
        self.master = master
        self.ffmpeg = ffmpeg
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.last_output_dir: Optional[Path] = None

        self.output_mode = tk.StringVar(value="same")
        self.output_dir = tk.StringVar()
        self.quality = tk.StringVar(value=DEFAULT_QUALITY)
        self.status = tk.StringVar(value="Add one or more videos to get started.")

        self._build()
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)

    # ----- layout -------------------------------------------------------------

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.add_files_btn = ttk.Button(toolbar, text="Add files…", command=self._add_files)
        self.add_folder_btn = ttk.Button(toolbar, text="Add folder…", command=self._add_folder)
        self.remove_btn = ttk.Button(toolbar, text="Remove selected", command=self._remove_selected)
        self.clear_btn = ttk.Button(toolbar, text="Clear list", command=self._clear)
        for btn in (self.add_files_btn, self.add_folder_btn, self.remove_btn, self.clear_btn):
            btn.pack(side="left", padx=(0, 6))

        list_frame = ttk.Frame(self)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(list_frame, columns=("file", "status"), show="headings", height=10)
        self.tree.heading("file", text="File")
        self.tree.heading("status", text="Status")
        self.tree.column("file", width=440, anchor="w")
        self.tree.column("status", width=160, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Delete>", lambda _e: self._remove_selected())
        self.master.bind("<Return>", lambda _e: None if self._busy() else self._start())

        options = ttk.LabelFrame(self, text="Options", padding=8)
        options.grid(row=2, column=0, sticky="ew", pady=8)
        options.columnconfigure(2, weight=1)

        ttk.Label(options, text="Quality:").grid(row=0, column=0, sticky="w")
        self.quality_box = ttk.Combobox(
            options, textvariable=self.quality, values=list(QUALITY_PRESETS), state="readonly", width=20
        )
        self.quality_box.grid(row=0, column=1, columnspan=2, sticky="w", padx=6, pady=(0, 6))

        ttk.Label(options, text="Save to:").grid(row=1, column=0, sticky="w")
        self.same_radio = ttk.Radiobutton(
            options, text="Same folder as each video", value="same",
            variable=self.output_mode, command=self._update_output_widgets,
        )
        self.same_radio.grid(row=1, column=1, columnspan=3, sticky="w", padx=6)
        self.custom_radio = ttk.Radiobutton(
            options, text="This folder:", value="custom",
            variable=self.output_mode, command=self._update_output_widgets,
        )
        self.custom_radio.grid(row=2, column=1, sticky="w", padx=6)
        self.output_entry = ttk.Entry(options, textvariable=self.output_dir)
        self.output_entry.grid(row=2, column=2, sticky="ew")
        self.browse_btn = ttk.Button(options, text="Browse…", command=self._choose_output_dir)
        self.browse_btn.grid(row=2, column=3, padx=(6, 0))

        progress = ttk.Frame(self)
        progress.grid(row=3, column=0, sticky="ew")
        progress.columnconfigure(1, weight=1)
        ttk.Label(progress, text="Current file:").grid(row=0, column=0, sticky="w")
        self.file_progress = ttk.Progressbar(progress, maximum=100)
        self.file_progress.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        ttk.Label(progress, text="Overall:").grid(row=1, column=0, sticky="w")
        self.total_progress = ttk.Progressbar(progress, maximum=100)
        self.total_progress.grid(row=1, column=1, sticky="ew", padx=6, pady=2)

        bottom = ttk.Frame(self)
        bottom.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.open_btn = ttk.Button(bottom, text="Open output folder", command=self._open_output, state="disabled")
        self.open_btn.grid(row=0, column=1, padx=(6, 0))
        self.cancel_btn = ttk.Button(bottom, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=2, padx=(6, 0))
        self.convert_btn = ttk.Button(bottom, text="Convert to MP3", command=self._start)
        self.convert_btn.grid(row=0, column=3, padx=(6, 0))

        self._update_output_widgets()

    def _update_output_widgets(self) -> None:
        state = "normal" if self.output_mode.get() == "custom" and not self._busy() else "disabled"
        self.output_entry.configure(state=state)
        self.browse_btn.configure(state=state)

    # ----- file list ----------------------------------------------------------

    def _queued_paths(self) -> set[str]:
        return {self.tree.set(item, "file") for item in self.tree.get_children()}

    def _add_paths(self, paths: list[Path]) -> None:
        existing = self._queued_paths()
        added = 0
        for path in paths:
            key = str(path)
            if key not in existing:
                self.tree.insert("", "end", values=(key, "Waiting"))
                existing.add(key)
                added += 1
        count = len(existing)
        self.status.set(f"{count} file{'s' if count != 1 else ''} in the list." if added or count else
                        "No video files found.")

    def _add_files(self) -> None:
        names = filedialog.askopenfilenames(title="Choose videos", filetypes=_FILE_TYPES)
        self._add_paths([Path(n) for n in names])

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose a folder of videos")
        if folder:
            videos = sorted(p for p in Path(folder).iterdir() if p.is_file() and is_video_file(p))
            if not videos:
                messagebox.showinfo("No videos", "That folder doesn't contain any video files.")
            self._add_paths(videos)

    def _remove_selected(self) -> None:
        if not self._busy():
            for item in self.tree.selection():
                self.tree.delete(item)

    def _clear(self) -> None:
        if not self._busy():
            self.tree.delete(*self.tree.get_children())
            self.file_progress["value"] = self.total_progress["value"] = 0
            self.status.set("Add one or more videos to get started.")

    def _choose_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="Save MP3 files to")
        if folder:
            self.output_dir.set(folder)

    # ----- conversion ---------------------------------------------------------

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.add_files_btn, self.add_folder_btn, self.remove_btn, self.clear_btn,
                       self.convert_btn, self.same_radio, self.custom_radio):
            widget.configure(state=state)
        self.quality_box.configure(state="disabled" if busy else "readonly")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        self._update_output_widgets()

    def _start(self) -> None:
        items = list(self.tree.get_children())
        if not items:
            messagebox.showinfo("Nothing to convert", "Add at least one video first.")
            return

        output_dir: Optional[Path] = None
        if self.output_mode.get() == "custom":
            if not self.output_dir.get().strip():
                messagebox.showwarning("Choose a folder", "Pick a folder to save the MP3 files to.")
                return
            output_dir = Path(self.output_dir.get().strip())

        for item in items:
            self.tree.set(item, "status", "Waiting")
        self.cancel_event.clear()
        self.file_progress["value"] = self.total_progress["value"] = 0
        self.open_btn.configure(state="disabled")
        self._set_busy(True)

        jobs = [(item, Path(self.tree.set(item, "file"))) for item in items]
        self.worker = threading.Thread(
            target=self._run_jobs, args=(jobs, output_dir, self.quality.get()), daemon=True
        )
        self.worker.start()

    def _run_jobs(self, jobs: list[tuple[str, Path]], output_dir: Optional[Path], quality: str) -> None:
        """Runs on the worker thread; reports back to the UI only through self.events."""
        done = failed = 0
        total = len(jobs)
        for index, (item, source) in enumerate(jobs):
            if self.cancel_event.is_set():
                self.events.put(("status", item, "Cancelled"))
                continue
            self.events.put(("start", item, index, total, source.name))
            job = ConversionJob(source, output_path_for(source, output_dir), quality)
            try:
                convert(
                    self.ffmpeg, job,
                    on_progress=lambda f, i=index: self.events.put(("progress", f, i, total)),
                    cancel_event=self.cancel_event,
                )
            except ConversionCancelled:
                self.events.put(("status", item, "Cancelled"))
            except (ConversionError, OSError) as exc:
                failed += 1
                self.events.put(("status", item, "Failed"))
                self.events.put(("error", source.name, str(exc)))
            else:
                done += 1
                self.events.put(("status", item, "Done"))
                self.events.put(("output", job.output.parent))
        self.events.put(("finished", done, failed, total))

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "start":
            _, item, index, total, name = event
            self.tree.set(item, "status", "Converting…")
            self.tree.see(item)
            self.file_progress["value"] = 0
            self.status.set(f"Converting {index + 1} of {total}: {name}")
        elif kind == "progress":
            _, fraction, index, total = event
            self.file_progress["value"] = fraction * 100
            self.total_progress["value"] = (index + fraction) / total * 100
        elif kind == "status":
            _, item, text = event
            self.tree.set(item, "status", text)
        elif kind == "output":
            self.last_output_dir = event[1]
            self.open_btn.configure(state="normal")
        elif kind == "error":
            _, name, message = event
            messagebox.showerror("Conversion failed", f"{name}\n\n{message}")
        elif kind == "finished":
            _, done, failed, total = event
            self._set_busy(False)
            if self.cancel_event.is_set():
                self.status.set(f"Cancelled. {done} of {total} converted.")
            else:
                self.total_progress["value"] = 100
                summary = f"Finished: {done} of {total} converted"
                self.status.set(summary + (f", {failed} failed." if failed else "."))

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status.set("Cancelling…")

    def _open_output(self) -> None:
        if self.last_output_dir and self.last_output_dir.exists():
            open_folder(self.last_output_dir)

    def _on_close(self) -> None:
        if self._busy():
            if not messagebox.askyesno("Conversion running", "A conversion is still running. Stop it and quit?"):
                return
            self.cancel_event.set()
            self.worker.join(timeout=5)
        self.master.destroy()


def _set_window_icon(root: tk.Tk) -> None:
    # Inside the .exe, bundled files live in sys._MEIPASS; otherwise next to the package.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    icon = base / "assets" / "icon.ico"
    if sys.platform == "win32" and icon.is_file():
        try:
            # Give the window its own taskbar entry and icon instead of Python's.
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VideoConverter.MP4toMP3")
            root.iconbitmap(default=str(icon))
        except (OSError, tk.TclError, AttributeError):
            pass


def main(paths: Optional[list[str]] = None) -> None:
    """Open the app. `paths` (e.g. videos dropped onto the .exe) are added to the list."""
    root = tk.Tk()
    root.title(f"Video Converter {__version__} - MP4 to MP3")
    root.minsize(640, 460)
    _set_window_icon(root)

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        root.withdraw()
        messagebox.showerror(
            "ffmpeg not found",
            "This app needs ffmpeg to convert files.\n\n"
            "Install it (for example: winget install Gyan.FFmpeg) or run:\n"
            "    pip install imageio-ffmpeg\n\n"
            "then start the app again.",
        )
        root.destroy()
        return

    app = ConverterApp(root, ffmpeg)
    if paths:
        app._add_paths([Path(p) for p in paths if Path(p).is_file()])
    root.mainloop()
