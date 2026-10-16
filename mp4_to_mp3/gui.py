"""Tkinter desktop interface."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from dataclasses import dataclass
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
    MediaInfo,
    convert,
    find_ffmpeg,
    format_time,
    is_video_file,
    output_path_for,
    parse_time,
    probe_media,
)
from .video import (
    DEFAULT_FORMAT,
    DEFAULT_VIDEO_QUALITY,
    FIT_MODES,
    FORMATS,
    RESOLUTIONS,
    ROTATIONS,
    VIDEO_QUALITY,
    VideoEdit,
    VideoJob,
    convert_video,
    output_tag,
    plan_geometry,
    render_frame,
    render_source_frames,
)

_FILE_TYPES = [
    ("Video files", " ".join(f"*{ext}" for ext in VIDEO_EXTENSIONS)),
    ("All files", "*.*"),
]
_FIT_BY_LABEL = {label: key for key, label in FIT_MODES.items()}
_PREVIEW_BG = "#202124"


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


@dataclass
class FileEntry:
    """A file in the list, plus its own crop position and zoom."""
    path: Path
    info: Optional[MediaInfo] = None
    probing: bool = False
    pos_x: float = 0.5
    pos_y: float = 0.5
    zoom: float = 1.0


class ConverterApp(ttk.Frame):
    def __init__(self, master: tk.Tk, ffmpeg: str):
        super().__init__(master, padding=10)
        self.master = master
        self.ffmpeg = ffmpeg
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.last_output_dir: Optional[Path] = None
        self.files: dict[str, FileEntry] = {}
        self.tmpdir = Path(tempfile.mkdtemp(prefix="videoconverter-"))
        # Preview sizes follow the Windows display scaling so they stay readable on high-DPI screens.
        ui_scale = max(1.0, self.winfo_fpixels("1i") / 96)
        self.src_size = (int(320 * ui_scale), int(200 * ui_scale))
        self.res_size = (int(200 * ui_scale), int(200 * ui_scale))

        self.output_mode = tk.StringVar(value="same")
        self.output_dir = tk.StringVar()
        self.quality = tk.StringVar(value=DEFAULT_QUALITY)
        self.trim_start = tk.StringVar()
        self.trim_end = tk.StringVar()
        self.status = tk.StringVar(value="Add one or more videos to get started.")

        self.v_format = tk.StringVar(value=DEFAULT_FORMAT)
        self.v_fit = tk.StringVar(value=FIT_MODES["crop"])
        self.v_zoom = tk.DoubleVar(value=1.0)
        self.v_rotate = tk.StringVar(value="None")
        self.v_flip = tk.BooleanVar(value=False)
        self.v_resolution = tk.StringVar(value="Same as source")
        self.v_quality = tk.StringVar(value=DEFAULT_VIDEO_QUALITY)
        self.v_mute = tk.BooleanVar(value=False)
        self.zoom_text = tk.StringVar(value="1.0×")
        self.preview_time = tk.DoubleVar(value=1.0)
        self.preview_time_text = tk.StringVar(value=format_time(1.0))
        self.result_text = tk.StringVar()

        self._preview_after: Optional[str] = None
        self._preview_gen = 0
        self._photos: dict[str, tk.PhotoImage] = {}  # keep references so Tk doesn't drop the images
        self._source_layout: Optional[tuple[float, float, float]] = None  # x offset, y offset, scale
        self._drag: Optional[tuple[float, float, int, int]] = None
        self._syncing = False

        self._build()
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        master.protocol("WM_DELETE_WINDOW", self._on_close)
        for var in (self.v_format, self.v_fit, self.v_rotate, self.v_flip, self.v_resolution, self.v_quality,
                    self.v_mute):
            var.trace_add("write", lambda *_: self._on_video_option_changed())
        self.v_zoom.trace_add("write", lambda *_: self._on_zoom_changed())
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

        body = ttk.Panedwindow(self, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew")

        list_frame = ttk.Frame(body)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(list_frame, columns=("name", "status"), show="headings", height=12)
        self.tree.heading("name", text="File")
        self.tree.heading("status", text="Status")
        self.tree.column("name", width=190, anchor="w")
        self.tree.column("status", width=90, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Delete>", lambda _e: self._remove_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_selection_changed())
        body.add(list_frame, weight=1)

        self.tabs = ttk.Notebook(body)
        self.audio_tab = ttk.Frame(self.tabs, padding=12)
        self.video_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.audio_tab, text="  MP3 audio  ")
        self.tabs.add(self.video_tab, text="  Edit video (MP4)  ")
        self.tabs.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab_changed())
        self.tabs.enable_traversal()  # Ctrl+Tab switches tabs
        body.add(self.tabs, weight=3)
        self._build_audio_tab()
        self._build_video_tab()

        options = ttk.LabelFrame(self, text="Options", padding=8)
        options.grid(row=2, column=0, sticky="ew", pady=8)
        options.columnconfigure(2, weight=1)

        ttk.Label(options, text="Trim:").grid(row=0, column=0, sticky="w")
        trim = ttk.Frame(options)
        trim.grid(row=0, column=1, columnspan=3, sticky="w", padx=6, pady=(0, 6))
        ttk.Label(trim, text="from").pack(side="left")
        self.start_entry = ttk.Entry(trim, textvariable=self.trim_start, width=10)
        self.start_entry.pack(side="left", padx=4)
        ttk.Label(trim, text="to").pack(side="left")
        self.end_entry = ttk.Entry(trim, textvariable=self.trim_end, width=10)
        self.end_entry.pack(side="left", padx=4)
        ttk.Label(trim, text="e.g. 0:15 or 1:02:30. Leave empty to keep the whole video.",
                  foreground="gray").pack(side="left", padx=(6, 0))

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

        self.master.bind("<Return>", lambda _e: None if self._busy() else self._start())
        self._update_output_widgets()
        self._update_video_widgets()

    def _build_audio_tab(self) -> None:
        tab = self.audio_tab
        ttk.Label(tab, text="Saves the sound from each video as an MP3 file.").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(tab, text="Quality:").grid(row=1, column=0, sticky="w")
        self.quality_box = ttk.Combobox(
            tab, textvariable=self.quality, values=list(QUALITY_PRESETS), state="readonly", width=20
        )
        self.quality_box.grid(row=1, column=1, sticky="w", padx=6)

    def _build_video_tab(self) -> None:
        tab = self.video_tab
        tab.columnconfigure(0, weight=1)

        previews = ttk.Frame(tab)
        previews.grid(row=0, column=0, sticky="ew")
        previews.columnconfigure(0, minsize=self.src_size[0])
        self.source_hint = ttk.Label(previews, text="Original: drag the box to choose what to keep")
        self.source_hint.grid(row=0, column=0, sticky="w")
        ttk.Label(previews, text="Result").grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.src_canvas = tk.Canvas(previews, width=self.src_size[0], height=self.src_size[1],
                                    bg=_PREVIEW_BG, highlightthickness=0, cursor="fleur")
        self.src_canvas.grid(row=1, column=0, sticky="nw")
        self.res_canvas = tk.Canvas(previews, width=self.res_size[0], height=self.res_size[1],
                                    bg=_PREVIEW_BG, highlightthickness=0)
        self.res_canvas.grid(row=1, column=1, sticky="nw", padx=(10, 0))
        ttk.Label(previews, textvariable=self.result_text, foreground="gray").grid(
            row=2, column=1, sticky="w", padx=(10, 0))
        self.src_canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.src_canvas.bind("<B1-Motion>", self._on_drag_move)
        self.src_canvas.bind("<ButtonRelease-1>", lambda _e: self._schedule_preview(50))
        self.src_canvas.bind("<MouseWheel>", self._on_wheel)

        scrub = ttk.Frame(tab)
        scrub.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        scrub.columnconfigure(1, weight=1)
        ttk.Label(scrub, text="Preview at:").grid(row=0, column=0, sticky="w")
        self.preview_scale = ttk.Scale(scrub, from_=0, to=1, variable=self.preview_time,
                                       command=lambda _v: self._on_preview_time())
        self.preview_scale.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(scrub, textvariable=self.preview_time_text, width=8).grid(row=0, column=2)

        controls = ttk.Frame(tab)
        controls.grid(row=2, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)

        def label(text: str, row: int, column: int) -> None:
            ttk.Label(controls, text=text).grid(row=row, column=column, sticky="w", pady=3,
                                                padx=(12 if column else 0, 6))

        label("Shape:", 0, 0)
        ttk.Combobox(controls, textvariable=self.v_format, values=list(FORMATS), state="readonly",
                     width=42).grid(row=0, column=1, columnspan=3, sticky="w")
        label("Fit:", 1, 0)
        self.fit_box = ttk.Combobox(controls, textvariable=self.v_fit, values=list(FIT_MODES.values()),
                                    state="readonly", width=28)
        self.fit_box.grid(row=1, column=1, sticky="w")
        label("Zoom:", 1, 2)
        zoom = ttk.Frame(controls)
        zoom.grid(row=1, column=3, sticky="w")
        self.zoom_scale = ttk.Scale(zoom, from_=1.0, to=3.0, variable=self.v_zoom, length=110)
        self.zoom_scale.pack(side="left")
        ttk.Label(zoom, textvariable=self.zoom_text, width=5).pack(side="left", padx=(4, 0))
        label("Rotate:", 2, 0)
        ttk.Combobox(controls, textvariable=self.v_rotate, values=list(ROTATIONS), state="readonly",
                     width=28).grid(row=2, column=1, sticky="w")
        label("Size:", 2, 2)
        ttk.Combobox(controls, textvariable=self.v_resolution, values=list(RESOLUTIONS), state="readonly",
                     width=16).grid(row=2, column=3, sticky="w")
        label("Quality:", 3, 0)
        ttk.Combobox(controls, textvariable=self.v_quality, values=list(VIDEO_QUALITY), state="readonly",
                     width=28).grid(row=3, column=1, sticky="w")
        checks = ttk.Frame(controls)
        checks.grid(row=3, column=2, columnspan=2, sticky="w", padx=(12, 0))
        ttk.Checkbutton(checks, text="Mirror", variable=self.v_flip).pack(side="left")
        ttk.Checkbutton(checks, text="Remove sound", variable=self.v_mute).pack(side="left", padx=(10, 0))
        ttk.Button(controls, text="Reset video settings", command=self._reset_video_settings).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _update_output_widgets(self) -> None:
        state = "normal" if self.output_mode.get() == "custom" and not self._busy() else "disabled"
        self.output_entry.configure(state=state)
        self.browse_btn.configure(state=state)

    def _update_video_widgets(self) -> None:
        edit = self._edit_template()
        self.fit_box.configure(state="disabled" if edit.aspect is None else "readonly")
        self.zoom_scale.state(["!disabled"] if edit.crops else ["disabled"])
        self.source_hint.configure(
            text="Original: drag the box to choose what to keep" if edit.crops else "Original")

    # ----- file list ----------------------------------------------------------

    def _add_paths(self, paths: list[Path]) -> None:
        for path in paths:
            key = str(path)
            if key not in self.files:
                self.files[key] = FileEntry(path)
                self.tree.insert("", "end", iid=key, values=(path.name, "Waiting"))
        if self.files and not self.tree.selection():
            self.tree.selection_set(next(iter(self.files)))
        count = len(self.files)
        self.status.set(f"{count} file{'s' if count != 1 else ''} in the list." if count else "No video files found.")

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
                self.files.pop(item, None)
            self._schedule_preview()

    def _clear(self) -> None:
        if not self._busy():
            self.tree.delete(*self.tree.get_children())
            self.files.clear()
            self.file_progress["value"] = self.total_progress["value"] = 0
            self.status.set("Add one or more videos to get started.")
            self._schedule_preview()

    def _choose_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="Save files to")
        if folder:
            self.output_dir.set(folder)

    def _selected_entries(self) -> list[FileEntry]:
        return [self.files[item] for item in self.tree.selection() if item in self.files]

    # ----- video settings and preview ----------------------------------------

    def _video_tab_active(self) -> bool:
        return self.tabs.select() == str(self.video_tab)

    def _edit_template(self) -> VideoEdit:
        """The settings from the video tab, without any file's crop position."""
        return VideoEdit(
            aspect=FORMATS[self.v_format.get()],
            fit=_FIT_BY_LABEL.get(self.v_fit.get(), "crop"),
            rotate=ROTATIONS[self.v_rotate.get()],
            flip=self.v_flip.get(),
            resolution=RESOLUTIONS[self.v_resolution.get()],
            crf=VIDEO_QUALITY[self.v_quality.get()],
            mute=self.v_mute.get(),
        )

    def _edit_for(self, entry: FileEntry) -> VideoEdit:
        edit = self._edit_template()
        edit.pos_x, edit.pos_y, edit.zoom = entry.pos_x, entry.pos_y, entry.zoom
        return edit

    def _reset_video_settings(self) -> None:
        self.v_format.set(DEFAULT_FORMAT)
        self.v_fit.set(FIT_MODES["crop"])
        self.v_rotate.set("None")
        self.v_flip.set(False)
        self.v_resolution.set("Same as source")
        self.v_quality.set(DEFAULT_VIDEO_QUALITY)
        self.v_mute.set(False)
        for entry in self.files.values():
            entry.pos_x = entry.pos_y = 0.5
            entry.zoom = 1.0
        self._sync_zoom_from_selection()

    def _on_tab_changed(self) -> None:
        self.convert_btn.configure(text="Export video" if self._video_tab_active() else "Convert to MP3")
        self._schedule_preview(50)

    def _on_selection_changed(self) -> None:
        self._sync_zoom_from_selection()
        self._schedule_preview(50)

    def _sync_zoom_from_selection(self) -> None:
        entries = self._selected_entries()
        self._syncing = True
        try:
            self.v_zoom.set(entries[0].zoom if entries else 1.0)
        finally:
            self._syncing = False
        self.zoom_text.set(f"{self.v_zoom.get():.1f}×")

    def _on_video_option_changed(self) -> None:
        self._update_video_widgets()
        self._schedule_preview()

    def _on_zoom_changed(self) -> None:
        zoom = round(self.v_zoom.get(), 2)
        self.zoom_text.set(f"{zoom:.1f}×")
        if self._syncing:
            return
        for entry in self._selected_entries():
            entry.zoom = zoom
        self._draw_crop_box()
        self._schedule_preview()

    def _on_preview_time(self) -> None:
        self.preview_time_text.set(format_time(self.preview_time.get()))
        self._schedule_preview(300)

    def _schedule_preview(self, delay: int = 250) -> None:
        if self._preview_after:
            self.after_cancel(self._preview_after)
        self._preview_after = self.after(delay, self._refresh_preview)

    def _show_placeholder(self, text: str) -> None:
        self._source_layout = None
        self.result_text.set("")
        for canvas, (w, h) in ((self.src_canvas, self.src_size), (self.res_canvas, self.res_size)):
            canvas.delete("all")
            canvas.create_text(w / 2, h / 2, text=text if canvas is self.src_canvas else "",
                               fill="#9aa0a6", width=w - 20, justify="center")

    def _refresh_preview(self) -> None:
        self._preview_after = None
        if not self._video_tab_active():
            return
        entries = self._selected_entries()
        if not entries:
            self._show_placeholder("Add a video and select it to see a preview.")
            return
        entry = entries[0]
        if entry.info is None:
            self._show_placeholder("Loading preview…")
            if not entry.probing:
                entry.probing = True
                threading.Thread(target=self._probe, args=(entry,), daemon=True).start()
            return
        if not entry.info.has_video:
            self._show_placeholder("This file has no video to preview.")
            return

        duration = entry.info.duration or 0.0
        self.preview_scale.configure(to=max(duration - 0.1, 0.1))
        at = min(self.preview_time.get(), max(duration - 0.1, 0.0))
        self._preview_gen += 1
        gen, edit = self._preview_gen, self._edit_for(entry)
        threading.Thread(target=self._render_previews, args=(gen, entry, edit, at), daemon=True).start()

    def _probe(self, entry: FileEntry) -> None:
        """Worker thread."""
        try:
            info = probe_media(self.ffmpeg, entry.path)
        except OSError:
            info = MediaInfo(duration=None)
        self.events.put(("probed", entry, info))

    def _render_previews(self, gen: int, entry: FileEntry, edit: VideoEdit, at: float) -> None:
        """Worker thread: render the source frame and the edited frame."""
        try:
            source, dimmed = self.tmpdir / f"src{gen}.png", self.tmpdir / f"dim{gen}.png"
            render_source_frames(self.ffmpeg, entry.path, source, dimmed, at, self.src_size, edit)
            result = render_frame(self.ffmpeg, entry.path, self.tmpdir / f"res{gen}.png", at, self.res_size,
                                  edit, entry.info)
        except (ConversionError, OSError, subprocess.TimeoutExpired) as exc:
            self.events.put(("preview_error", gen, str(exc)))
            return
        self.events.put(("preview", gen, entry, edit, (source, dimmed, result)))

    def _load_photo(self, name: str, path: Path) -> tk.PhotoImage:
        photo = tk.PhotoImage(file=str(path))
        self._photos[name] = photo
        path.unlink(missing_ok=True)
        return photo

    def _show_preview(self, entry: FileEntry, edit: VideoEdit, frames: tuple[Path, Path, Path]) -> None:
        source, dimmed, result = frames
        src = self._load_photo("source", source)
        dim = self._load_photo("dimmed", dimmed)
        res = self._load_photo("result", result)

        self.src_canvas.delete("all")
        x0 = (self.src_size[0] - src.width()) / 2
        y0 = (self.src_size[1] - src.height()) / 2
        # With a crop box, the dimmed frame is the backdrop and the box shows the bright frame.
        self.src_canvas.create_image(x0, y0, image=dim if edit.crops else src, anchor="nw")
        geometry = plan_geometry(entry.info.width, entry.info.height, edit)
        self._source_layout = (x0, y0, src.width() / geometry.width)
        self._draw_crop_box()

        self.res_canvas.delete("all")
        self.res_canvas.create_image(self.res_size[0] / 2, self.res_size[1] / 2, image=res, anchor="center")
        self.result_text.set(f"{geometry.out_width} × {geometry.out_height}")

    def _crop_target(self) -> Optional[tuple[FileEntry, VideoEdit]]:
        entries = self._selected_entries()
        if not entries or not entries[0].info or not entries[0].info.has_video or not self._source_layout:
            return None
        edit = self._edit_for(entries[0])
        return (entries[0], edit) if edit.crops else None

    def _draw_crop_box(self) -> None:
        canvas = self.src_canvas
        canvas.delete("box")
        target = self._crop_target()
        if not target:
            return
        entry, edit = target
        geometry = plan_geometry(entry.info.width, entry.info.height, edit)
        x0, y0, scale = self._source_layout
        cw, ch, cx, cy = geometry.crop
        left, top = x0 + cx * scale, y0 + cy * scale
        right, bottom = left + cw * scale, top + ch * scale
        source = self._photos.get("source")
        if source is not None:
            # Show the undimmed frame inside the box (Tk canvases can't draw translucent shading).
            l, t = int(round(left - x0)), int(round(top - y0))
            r, b = min(int(round(right - x0)), source.width()), min(int(round(bottom - y0)), source.height())
            if r > l and b > t:
                bright = tk.PhotoImage(width=r - l, height=b - t)
                bright.tk.call(bright, "copy", source, "-from", l, t, r, b)
                self._photos["bright"] = bright
                canvas.create_image(x0 + l, y0 + t, image=bright, anchor="nw", tags="box")
        canvas.create_rectangle(left, top, right, bottom, outline="white", width=2, tags="box")

    def _move_crop(self, entry: FileEntry, edit: VideoEdit, crop_x: float, crop_y: float) -> None:
        geometry = plan_geometry(entry.info.width, entry.info.height, edit)
        cw, ch, _, _ = geometry.crop
        free_x, free_y = geometry.width - cw, geometry.height - ch
        pos_x = min(max(crop_x / free_x, 0.0), 1.0) if free_x > 0 else 0.5
        pos_y = min(max(crop_y / free_y, 0.0), 1.0) if free_y > 0 else 0.5
        for selected in self._selected_entries():
            selected.pos_x, selected.pos_y = pos_x, pos_y
        self._draw_crop_box()

    def _on_drag_start(self, event: tk.Event) -> None:
        target = self._crop_target()
        if not target:
            self._drag = None
            return
        entry, edit = target
        x0, y0, scale = self._source_layout
        cw, ch, cx, cy = plan_geometry(entry.info.width, entry.info.height, edit).crop
        px, py = (event.x - x0) / scale, (event.y - y0) / scale
        if not (cx <= px <= cx + cw and cy <= py <= cy + ch):
            # Clicking outside the box moves the box there.
            cx, cy = px - cw / 2, py - ch / 2
            self._move_crop(entry, edit, cx, cy)
            edit = self._edit_for(entry)
            _, _, cx, cy = plan_geometry(entry.info.width, entry.info.height, edit).crop
        self._drag = (event.x, event.y, cx, cy)

    def _on_drag_move(self, event: tk.Event) -> None:
        target = self._crop_target()
        if not target or not self._drag:
            return
        entry, edit = target
        start_x, start_y, crop_x, crop_y = self._drag
        scale = self._source_layout[2]
        self._move_crop(entry, edit, crop_x + (event.x - start_x) / scale, crop_y + (event.y - start_y) / scale)

    def _on_wheel(self, event: tk.Event) -> None:
        if self._crop_target():
            step = 0.1 if event.delta > 0 else -0.1
            self.v_zoom.set(min(max(self.v_zoom.get() + step, 1.0), 3.0))

    # ----- conversion ---------------------------------------------------------

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.add_files_btn, self.add_folder_btn, self.remove_btn, self.clear_btn,
                       self.convert_btn, self.same_radio, self.custom_radio, self.start_entry, self.end_entry):
            widget.configure(state=state)
        self.quality_box.configure(state="disabled" if busy else "readonly")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        self._update_output_widgets()

    def _start(self) -> None:
        items = list(self.tree.get_children())
        if not items:
            messagebox.showinfo("Nothing to convert", "Add at least one video first.")
            return

        try:
            start, end = parse_time(self.trim_start.get()), parse_time(self.trim_end.get())
        except ValueError as exc:
            messagebox.showwarning("Check the trim times", str(exc))
            return
        if start is not None and end is not None and end <= start:
            messagebox.showwarning("Check the trim times", "The end time must be after the start time.")
            return

        output_dir: Optional[Path] = None
        if self.output_mode.get() == "custom":
            if not self.output_dir.get().strip():
                messagebox.showwarning("Choose a folder", "Pick a folder to save the files to.")
                return
            output_dir = Path(self.output_dir.get().strip())

        video_mode = self._video_tab_active()
        jobs = []
        for item in items:
            entry = self.files[item]
            self.tree.set(item, "status", "Waiting")
            edit = None
            if video_mode:
                edit = self._edit_for(entry)
                edit.start, edit.end = start, end
            jobs.append((item, entry, edit))

        self.cancel_event.clear()
        self.file_progress["value"] = self.total_progress["value"] = 0
        self.open_btn.configure(state="disabled")
        self._set_busy(True)
        self.worker = threading.Thread(
            target=self._run_jobs, args=(jobs, output_dir, self.quality.get(), start, end), daemon=True
        )
        self.worker.start()

    def _run_jobs(self, jobs: list, output_dir: Optional[Path], quality: str,
                  start: Optional[float], end: Optional[float]) -> None:
        """Runs on the worker thread; reports back to the UI only through self.events."""
        done = failed = 0
        total = len(jobs)
        for index, (item, entry, edit) in enumerate(jobs):
            if self.cancel_event.is_set():
                self.events.put(("status", item, "Cancelled"))
                continue
            source = entry.path
            self.events.put(("start", item, index, total, source.name))
            progress = lambda f, i=index: self.events.put(("progress", f, i, total))
            try:
                if edit is None:
                    job = ConversionJob(source, output_path_for(source, output_dir), quality, start, end)
                    output = convert(self.ffmpeg, job, progress, self.cancel_event)
                else:
                    target = output_path_for(source, output_dir, suffix=".mp4", name_tag=f" ({output_tag(edit)})")
                    output = convert_video(self.ffmpeg, VideoJob(source, target, edit), progress,
                                           self.cancel_event, entry.info)
            except ConversionCancelled:
                self.events.put(("status", item, "Cancelled"))
            except (ConversionError, OSError) as exc:
                failed += 1
                self.events.put(("status", item, "Failed"))
                self.events.put(("error", source.name, str(exc)))
            else:
                done += 1
                self.events.put(("status", item, "Done"))
                self.events.put(("output", output.parent))
        self.events.put(("finished", done, failed, total, edit is not None))

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
            if self.tree.exists(item):
                self.tree.set(item, "status", "Working…")
                self.tree.see(item)
            self.file_progress["value"] = 0
            self.status.set(f"Working on {index + 1} of {total}: {name}")
        elif kind == "progress":
            _, fraction, index, total = event
            self.file_progress["value"] = fraction * 100
            self.total_progress["value"] = (index + fraction) / total * 100
        elif kind == "status":
            _, item, text = event
            if self.tree.exists(item):
                self.tree.set(item, "status", text)
        elif kind == "output":
            self.last_output_dir = event[1]
            self.open_btn.configure(state="normal")
        elif kind == "error":
            _, name, message = event
            messagebox.showerror("Conversion failed", f"{name}\n\n{message}")
        elif kind == "finished":
            _, done, failed, total, video = event
            self._set_busy(False)
            verb = "exported" if video else "converted"
            if self.cancel_event.is_set():
                self.status.set(f"Cancelled. {done} of {total} {verb}.")
            else:
                self.total_progress["value"] = 100
                summary = f"Finished: {done} of {total} {verb}"
                self.status.set(summary + (f", {failed} failed." if failed else "."))
        elif kind == "probed":
            _, entry, info = event
            entry.info, entry.probing = info, False
            if info.duration and self.preview_time.get() > info.duration:
                self.preview_time.set(min(1.0, info.duration / 2))
            self._schedule_preview(0)
        elif kind == "preview":
            _, gen, entry, edit, frames = event
            if gen == self._preview_gen:
                self._show_preview(entry, edit, frames)
            else:
                for frame in frames:
                    frame.unlink(missing_ok=True)
        elif kind == "preview_error":
            _, gen, message = event
            if gen == self._preview_gen:
                self._show_placeholder("Couldn't show a preview of this video.")

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
        shutil.rmtree(self.tmpdir, ignore_errors=True)
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
    if sys.platform == "win32":
        try:
            # Render crisply on scaled displays instead of being stretched (and blurred) by Windows.
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    root.title(f"Video Converter {__version__}")
    root.minsize(860, 640)
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
