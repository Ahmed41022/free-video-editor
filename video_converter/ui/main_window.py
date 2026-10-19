"""The main editor window."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, QEvent, QObject, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QImage, QKeySequence, QPixmap, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSizePolicy, QSplitter,
    QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__, diagnostics, gpu
from ..core import (
    AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, ConversionError, MediaInfo, format_time, is_media_file, probe_media,
)
from ..jobs import Clip, Job, JobQueue
from ..media import extract_thumbnails, extract_waveform
from ..project import OUTPUT_AUDIO, OUTPUT_VIDEO, ClipSettings, apply_to, describe
from ..proxy import ProxyMaker, needs_proxy
from ..timing import part_index, remove_part_at, restore_part_at, split_at
from ..video import (
    REVERSE_WARN_BYTES, format_memory, frame_rate_hint, plan_geometry, render_frame, reverse_memory, reverse_warning,
)
from . import icons, theme
from .inspector import Inspector
from .player import Player, VideoView
from .queue_panel import QueueBridge, QueuePanel
from .timeline import Timeline

FRAME_STEP = 1 / 30
FILE_FILTER = "Videos and audio ({});;All files (*.*)".format(
    " ".join(f"*{ext}" for ext in VIDEO_EXTENSIONS + AUDIO_EXTENSIONS))


@dataclass(eq=False)
class MediaItem:
    path: Path
    settings: ClipSettings = field(default_factory=ClipSettings)
    info: Optional[MediaInfo] = None
    thumbs: list[QPixmap] = field(default_factory=list)
    waveform: list[float] = field(default_factory=list)
    list_item: Optional[QListWidgetItem] = None
    loading: bool = True
    error: str = ""
    proxy: Optional[Path] = None             # the smooth preview copy, once it is ready
    proxy_progress: Optional[float] = None   # how far along the copy is, while it is being made
    proxy_failed: bool = False


class SettingsCommand(QUndoCommand):
    """One undoable settings change. Continuous edits (dragging the crop box or a
    slider) merge into a single step."""

    MERGE_ID = 1001

    def __init__(self, window: "MainWindow", item: MediaItem, before: ClipSettings, after: ClipSettings,
                 label: str, mergeable: bool):
        super().__init__(label)
        self.window, self.item = window, item
        self.before, self.after = before.copy(), after.copy()
        self.mergeable = mergeable

    def id(self) -> int:
        return self.MERGE_ID if self.mergeable else -1

    def mergeWith(self, other: QUndoCommand) -> bool:
        if (isinstance(other, SettingsCommand) and other.item is self.item and other.text() == self.text()
                and self.window.merge_open):
            self.after = other.after.copy()
            return True
        return False

    def redo(self) -> None:
        self.window.apply_settings(self.item, self.after)

    def undo(self) -> None:
        self.window.apply_settings(self.item, self.before)


class MediaList(QListWidget):
    """The list of files. Dragging an item reorders the list (the order of a joined export);
    files dragged in from outside are left to the window."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def dragEnterEvent(self, event) -> None:
        if event.source() is self:
            super().dragEnterEvent(event)
        else:
            event.ignore()


class Signals(QObject):
    """Results from background threads, delivered on the UI thread."""
    loaded = Signal(object, object, object, object)   # item, info, thumbnail paths, waveform
    afterReady = Signal(int, object)                   # request token, QImage or error text
    gpuReady = Signal(object)                          # the graphics card's name, or None if there is none
    proxyProgress = Signal(object, float)              # item, how much of its smooth preview is made
    proxyDone = Signal(object, object, str)            # item, the preview copy or None, why not


class MainWindow(QMainWindow):
    def __init__(self, ffmpeg: str, settings: Optional[QSettings] = None):
        super().__init__()
        self.ffmpeg = ffmpeg
        # The storage key keeps the app's original name so saved settings and presets carry over.
        self.store = settings or QSettings("VideoConverter", "Video Converter")
        self.items: list[MediaItem] = []
        self.current: Optional[MediaItem] = None
        self.output_dir: Optional[Path] = None
        self.user_presets: dict[str, dict] = {}
        self.merge_open = False
        self.theme_mode = "dark"
        self.tmpdir = Path(tempfile.mkdtemp(prefix="videoconverter-"))
        self.closed = False
        self.confirm_heavy_exports = True     # ask before an export that needs a lot of memory
        self.smooth_previews = True           # play a small copy of big videos (View menu)
        self.gpu_encoders: Optional[tuple[str, ...]] = None     # None until the search has finished
        self.loader = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-loader")
        self.signals = Signals()
        self.signals.loaded.connect(self._on_loaded)
        self.signals.afterReady.connect(self._on_after_ready)
        self.signals.gpuReady.connect(self._on_gpu_ready)
        self.signals.proxyProgress.connect(self._on_proxy_progress)
        self.signals.proxyDone.connect(self._on_proxy_done)
        self.proxies = ProxyMaker(ffmpeg, self.tmpdir / "preview", self.signals.proxyProgress.emit,
                                  self.signals.proxyDone.emit)
        self._after_token = 0
        self._after_timer = QTimer(self, singleShot=True, interval=250, timeout=self._render_after)

        self.undo_stack = QUndoStack(self)
        self.bridge = QueueBridge()
        self.queue = JobQueue(ffmpeg, on_update=self.bridge.jobUpdated.emit)
        self.player = Player(self)

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setAcceptDrops(True)
        self.resize(1320, 840)
        self._build()
        self._load_preferences()
        self._connect()
        QApplication.instance().installEventFilter(self)
        self.select(None)
        threading.Thread(target=self._find_gpu, name="gpu-search", daemon=True).start()

    # ----- layout -------------------------------------------------------------

    def _build(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        def action(text: str, icon: str, slot, shortcut=None, tip: str = "") -> QAction:
            act = QAction(text, self)
            act.setData(icon)
            act.triggered.connect(slot)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            act.setToolTip(f"{tip or text} ({QKeySequence(shortcut).toString()})" if shortcut else (tip or text))
            toolbar.addAction(act)
            return act

        self.add_action = action("Add files", "add", self.choose_files, "Ctrl+O")
        self.folder_action = action("Add folder", "folder", self.choose_folder)
        self.remove_action = action("Remove", "trash", self.remove_selected, tip="Remove the selected file")
        toolbar.addSeparator()
        self.undo_action = self.undo_stack.createUndoAction(self, "Undo")
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.setData("undo")
        self.redo_action = self.undo_stack.createRedoAction(self, "Redo")
        self.redo_action.setShortcuts([QKeySequence.StandardKey.Redo, QKeySequence("Ctrl+Y")])
        self.redo_action.setData("redo")
        toolbar.addAction(self.undo_action)
        toolbar.addAction(self.redo_action)
        toolbar.addSeparator()
        self.theme_action = action("Theme", "moon", self.toggle_theme, tip="Switch between dark and light")

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.join_button = QPushButton("Join into one")
        self.join_button.setToolTip("Export the files as one video, in list order: the selected files, or all of "
                                    "them. Drag files in the list to reorder them. (Ctrl+J)")
        self.join_button.clicked.connect(self.join_clips)
        toolbar.addWidget(self.join_button)
        self.export_all_button = QPushButton("Export all")
        self.export_all_button.setToolTip("Export every file in the list (Ctrl+Shift+E)")
        self.export_all_button.clicked.connect(self.export_all)
        self.export_button = QPushButton("Export")
        self.export_button.setObjectName("primary")
        self.export_button.setToolTip("Export the selected file (Ctrl+E)")
        self.export_button.clicked.connect(self.export_current)
        toolbar.addWidget(self.export_all_button)
        toolbar.addWidget(self.export_button)
        for shortcut, slot in (("Ctrl+E", self.export_current), ("Ctrl+Shift+E", self.export_all),
                               ("Ctrl+J", self.join_clips)):
            act = QAction(self)
            act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(slot)
            self.addAction(act)

        # media list
        self.media_list = MediaList()
        self.media_list.setIconSize(QSize(96, 54))
        self.media_list.setMinimumWidth(210)
        self.media_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.media_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.media_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.media_list.setWordWrap(False)

        # center: preview, transport, timeline
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        self.view = VideoView()
        center_layout.addWidget(self.view, 1)
        transport = QWidget()
        transport.setObjectName("panel")
        row = QHBoxLayout(transport)
        row.setContentsMargins(10, 4, 10, 4)

        def tool(icon: str, tip: str, slot, checkable: bool = False) -> QToolButton:
            button = QToolButton()
            button.setProperty("iconName", icon)
            button.setToolTip(tip)
            button.setCheckable(checkable)
            button.clicked.connect(slot)
            row.addWidget(button)
            return button

        self.back_button = tool("back", "Back 5 seconds (J)", lambda: self.player.step(-5))
        self.play_button = tool("play", "Play / pause (Space)", self.player.toggle)
        self.forward_button = tool("forward", "Forward 5 seconds (L)", lambda: self.player.step(5))
        self.time_label = QLabel("0:00.0 / 0:00.0")
        row.addWidget(self.time_label)
        row.addStretch(1)
        self.in_button = tool("mark_in", "Start the trim here (I)", lambda: self.trim_at_playhead("start"))
        self.out_button = tool("mark_out", "End the trim here (O)", lambda: self.trim_at_playhead("end"))
        self.split_button = tool("split", "Split at the playhead (S)", lambda: self.cut_at("split"))
        self.cut_button = tool("scissors", "Cut out the part under the playhead (X)", lambda: self.cut_at("remove"))
        self.compare_button = tool("compare", "Show the result (B)", self._compare_toggled, checkable=True)
        self.compare_button.setText("Before / after")
        self.compare_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.mute_button = tool("volume", "Mute the preview", self._preview_mute_toggled, checkable=True)
        center_layout.addWidget(transport)
        self.timeline = Timeline()
        center_layout.addWidget(self.timeline)

        self.inspector = Inspector()

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self.media_list)
        top.addWidget(center)
        top.addWidget(self.inspector)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setStretchFactor(2, 0)
        top.setSizes([230, 760, 330])
        top.setChildrenCollapsible(False)

        self.queue_panel = QueuePanel(self.queue, self.bridge)
        self.queue_panel.setMinimumHeight(70)
        self.main_split = QSplitter(Qt.Orientation.Vertical)
        self.main_split.addWidget(top)
        self.main_split.addWidget(self.queue_panel)
        self.main_split.setStretchFactor(0, 1)
        self.main_split.setSizes([700, 150])
        self.top_split = top
        self.setCentralWidget(self.main_split)
        self.preview_status = QLabel()      # "Preparing a smooth preview…", while there is one
        self.statusBar().addPermanentWidget(self.preview_status)
        self._build_menus()
        self.statusBar().showMessage("Add videos to get started: drop them here or press Ctrl+O.")
        self.refresh_icons()

    def _build_menus(self) -> None:
        view = self.menuBar().addMenu("&View")
        self.smooth_action = view.addAction("Smooth previews for large videos")
        self.smooth_action.setCheckable(True)
        self.smooth_action.setChecked(self.smooth_previews)
        self.smooth_action.setToolTip("Play a small copy of 4K and other very large videos, so the preview "
                                      "doesn't stutter. Exports always use the original.")
        self.smooth_action.setStatusTip(self.smooth_action.toolTip())
        self.smooth_action.toggled.connect(self.set_smooth_previews)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("Open the log folder", self.open_log_folder)
        help_menu.addAction("Report a problem…", self.report_problem)
        help_menu.addSeparator()
        help_menu.addAction(f"About {APP_NAME}", self.show_about)

    def _connect(self) -> None:
        self.media_list.currentRowChanged.connect(lambda row: self.select(self._item_at(row)))
        self.media_list.model().rowsMoved.connect(self._rows_moved)
        self.media_list.customContextMenuRequested.connect(self._media_menu)
        self.player.frameReady.connect(self.view.set_frame)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playingChanged.connect(self._on_playing)
        self.player.errorOccurred.connect(self._on_player_error)
        self.timeline.seekRequested.connect(self.player.seek)
        self.timeline.trimChanged.connect(self._timeline_trim)
        self.timeline.trimEditFinished.connect(self._edit_finished)
        self.timeline.keepChanged.connect(self._timeline_keep)
        self.timeline.keepEditFinished.connect(self._edit_finished)
        self.timeline.menuRequested.connect(self._timeline_menu)
        self.view.cropMoved.connect(self._crop_moved)
        self.view.zoomChanged.connect(self._zoom_changed)
        self.view.cropEditFinished.connect(self._edit_finished)
        self.inspector.settingsEdited.connect(self._inspector_edited)
        self.inspector.applyToAllRequested.connect(self.apply_to_all)
        self.inspector.savePresetRequested.connect(self.save_preset)
        self.inspector.playheadRequested.connect(self.trim_at_playhead)
        self.inspector.cutRequested.connect(self.cut_at)
        self.inspector.outputFolderRequested.connect(self.choose_output_folder)
        self.bridge.jobUpdated.connect(self._job_updated)

    def refresh_icons(self) -> None:
        for act in self.findChildren(QAction):
            if act.data():
                act.setIcon(icons.icon(act.data()))
        for button in self.findChildren(QToolButton):
            name = button.property("iconName")
            if name:
                if button is self.play_button:
                    name = "pause" if self.player.playing else "play"
                if button is self.mute_button:
                    name = "mute" if button.isChecked() else "volume"
                button.setIcon(icons.icon(name))
        self.theme_action.setIcon(icons.icon("sun" if self.theme_mode == "dark" else "moon"))

    # ----- preferences --------------------------------------------------------

    def _load_preferences(self) -> None:
        mode = self._saved_text("theme", "system")
        self.theme_mode = theme.apply_theme(QApplication.instance(), mode if mode in ("dark", "light") else "system")
        geometry = self._saved_bytes("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        for key, splitter in (("splitTop", self.top_split), ("splitMain", self.main_split)):
            state = self._saved_bytes(key)
            if state is not None:
                splitter.restoreState(state)
        folder = self._saved_text("outputDir")
        self.output_dir = Path(folder) if folder and Path(folder).is_dir() else None
        try:
            presets = json.loads(self._saved_text("presets", "{}") or "{}")
            self.user_presets = presets if isinstance(presets, dict) else {}
        except ValueError:
            self.user_presets = {}
        self.inspector.set_presets(self.user_presets)
        self.smooth_previews = self._saved_bool("smoothPreviews", True)
        self.smooth_action.setChecked(self.smooth_previews)
        self._show_output_folder()
        self.refresh_icons()

    def _saved_text(self, key: str, default: str = "") -> str:
        """A saved text setting. Anything else a damaged settings file holds is ignored."""
        value = self.store.value(key, default)
        return value if isinstance(value, str) else default

    def _saved_bool(self, key: str, default: bool) -> bool:
        """A saved on/off setting (the settings file may keep it as text)."""
        value = self.store.value(key, default)
        if isinstance(value, bool):
            return value
        return {"true": True, "false": False}.get(value.strip().lower(), default) if isinstance(value, str) else default

    def _saved_bytes(self, key: str) -> Optional[QByteArray]:
        """A saved window layout, or None if there is none or the settings file holds something else."""
        value = self.store.value(key)
        return QByteArray(value) if isinstance(value, (QByteArray, bytes)) and len(value) else None

    def _save_preferences(self) -> None:
        self.store.setValue("geometry", self.saveGeometry())
        self.store.setValue("splitTop", self.top_split.saveState())
        self.store.setValue("splitMain", self.main_split.saveState())
        self.store.setValue("outputDir", str(self.output_dir) if self.output_dir else "")
        self.store.setValue("presets", json.dumps(self.user_presets))
        self.store.setValue("theme", self.theme_mode)
        self.store.setValue("smoothPreviews", self.smooth_previews)

    def toggle_theme(self) -> None:
        self.theme_mode = theme.apply_theme(QApplication.instance(), "light" if self.theme_mode == "dark" else "dark")
        self.refresh_icons()
        for widget in (self.view, self.timeline):
            widget.update()

    # ----- adding and removing files -----------------------------------------

    def choose_files(self) -> None:
        last = self._saved_text("lastDir")
        names, _ = QFileDialog.getOpenFileNames(self, "Add videos or audio", last, FILE_FILTER)
        if names:
            self.store.setValue("lastDir", str(Path(names[0]).parent))
            self.add_paths([Path(n) for n in names])

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add a folder of videos or audio",
                                                  self._saved_text("lastDir"))
        if folder:
            self.store.setValue("lastDir", folder)
            self.add_paths([Path(folder)])

    def add_paths(self, paths: list[Path]) -> int:
        """Add files (and the videos and audio files inside any folders). Returns how many were new."""
        files: list[Path] = []
        for path in paths:
            if path.is_dir():
                files.extend(sorted(p for p in path.iterdir() if p.is_file() and is_media_file(p)))
            elif path.is_file():
                files.append(path)
        known = {item.path.resolve() for item in self.items}
        added = 0
        for path in files:
            if path.resolve() in known:
                continue
            known.add(path.resolve())
            item = MediaItem(path)
            item.list_item = QListWidgetItem(icons.icon("film", size=48), "")
            self._show_item_text(item)
            item.list_item.setData(Qt.ItemDataRole.UserRole, id(item))
            self.items.append(item)
            self.media_list.addItem(item.list_item)
            self.loader.submit(self._load_item, item)
            added += 1
        if added:
            self._update_list_actions()
            self.statusBar().showMessage(f"Added {added} file{'s' if added != 1 else ''}.", 4000)
            if self.current is None:
                self.media_list.setCurrentRow(len(self.items) - added)
        elif paths:
            self.statusBar().showMessage("No new video or audio files there.", 4000)
        return added

    def _update_list_actions(self) -> None:
        """Enable the buttons that work on the whole list."""
        self.export_all_button.setEnabled(bool(self.items))
        self.join_button.setEnabled(len(self.items) > 1)

    def _item_at(self, row: int) -> Optional[MediaItem]:
        """The file shown in list row `row` (the list can be reordered by dragging)."""
        list_item = self.media_list.item(row)
        if list_item is None:
            return None
        key = list_item.data(Qt.ItemDataRole.UserRole)
        return next((item for item in self.items if id(item) == key), None)

    def _rows_moved(self, *_args) -> None:
        """Keep `items` in the same order as the list after a drag."""
        moved = [self._item_at(row) for row in range(self.media_list.count())]
        if all(moved) and len(moved) == len(self.items):
            self.items = moved

    def remove_selected(self) -> None:
        row = self.media_list.currentRow()
        item = self._item_at(row)
        if item is None:
            return
        self.items.remove(item)
        self.media_list.takeItem(row)
        self._drop_preview_copy(item)
        self.undo_stack.clear()   # undo steps may refer to the removed file
        self._update_list_actions()
        if item is self.current:
            self.current = None
            self.select(self.items[min(row, len(self.items) - 1)] if self.items else None)

    def _media_menu(self, point) -> None:
        if self.media_list.itemAt(point) is None:
            return
        menu = QMenu(self)
        menu.addAction("Export", self.export_current)
        many = len(self.media_list.selectedItems()) > 1
        join = menu.addAction("Join the selected files into one" if many else "Join all files into one",
                              self.join_clips)
        join.setEnabled(len(self.items) > 1)
        menu.addAction("Remove from list", self.remove_selected)
        menu.exec(self.media_list.mapToGlobal(point))

    def _item_text(self, item: MediaItem) -> str:
        if item.error:
            meta = item.error
        elif item.loading or item.info is None:
            meta = "Loading…"
        else:
            info = item.info
            size = f" · {info.width}×{info.height}" if info.has_video else " · audio only"
            meta = f"{format_time(info.duration or 0)}{size}"
        return f"{item.path.name}\n{meta}\n{describe(item.settings, item.info.duration if item.info else None)}"

    def _show_item_text(self, item: MediaItem) -> None:
        # The summary line is often cut short in the list, so the tooltip repeats it in full.
        text = self._item_text(item)
        item.list_item.setText(text)
        item.list_item.setToolTip(f"{item.path}\n{text.splitlines()[-1]}")

    def _load_item(self, item: MediaItem) -> None:
        """Background thread: read the file's details, thumbnails and waveform."""
        try:
            info = probe_media(self.ffmpeg, item.path)
            thumbs, wave = [], []
            if info.has_video and info.duration:
                thumbs = extract_thumbnails(self.ffmpeg, item.path, self.tmpdir / f"{id(item)}", info.duration)
            if info.has_audio:
                wave = extract_waveform(self.ffmpeg, item.path, info.duration)
        except (OSError, ConversionError, ValueError) as exc:
            info, thumbs, wave = MediaInfo(duration=None), [], []
            item.error = f"Can't read this file ({exc})"
        self.signals.loaded.emit(item, info, thumbs, wave)
        self._remove_tmpdir_if_closed()

    def _on_loaded(self, item: MediaItem, info: MediaInfo, thumbs: list[Path], wave: list[float]) -> None:
        item.info, item.loading = info, False
        if not item.error and not info.has_video and not info.has_audio:
            item.error = "Not a video or audio file"
        if not info.has_video and info.has_audio:
            item.settings.output = OUTPUT_AUDIO
        item.thumbs = [QPixmap(str(p)) for p in thumbs]
        item.waveform = wave
        if item.list_item is not None:
            if item.thumbs:
                item.list_item.setIcon(item.thumbs[len(item.thumbs) // 2])
            self._show_item_text(item)
        if item is self.current:
            self.select(item, reload=False)

    # ----- selection ----------------------------------------------------------

    def select(self, item: Optional[MediaItem], reload: bool = True) -> None:
        changed = item is not self.current
        self.current = item
        has_file = item is not None
        for widget in (self.export_button, self.back_button, self.play_button, self.forward_button,
                       self.in_button, self.out_button, self.split_button, self.cut_button, self.compare_button):
            widget.setEnabled(has_file)
        self.remove_action.setEnabled(has_file)
        self._update_list_actions()
        if changed:
            # Don't keep a queue of big copies for files the user has already moved on from.
            for other in self.proxies.cancel_waiting(keep=item):
                other.proxy_progress = None
        if item is None:
            self.player.load(None)
            self._update_preview_status()
            self.view.clear("Drop videos here, or click Add files")
            self.timeline.clear()
            self.inspector.set_settings(None, None)
            self.inspector.set_reverse_warning("")
            self.inspector.set_frame_rate_hint("")
            self.setWindowTitle(f"{APP_NAME} {__version__}")
            return
        self.setWindowTitle(f"{item.path.name} · {APP_NAME} {__version__}")
        if changed or reload:
            self.view.clear("Loading…")
            self.compare_button.setChecked(False)
            copy = self._preview_copy(item)
            self.player.load(copy or item.path, upright=copy is not None)
        info = item.info
        self.timeline.setDuration(info.duration or 0.0 if info else 0.0)
        self.time_label.setText(f"{format_time(self.player.position)} / {format_time(info.duration or 0 if info else 0)}")
        self.timeline.setThumbnails(item.thumbs)
        self.timeline.setWaveform(item.waveform)
        if info is not None and not info.has_video:
            self.view.clear(item.error or "Audio only: there's no picture to preview.")
        self._show_settings(item)
        self._prepare_preview_copy(item)

    def _show_settings(self, item: MediaItem) -> None:
        settings, info = item.settings, item.info
        self.inspector.set_settings(settings, info.duration if info else None)
        edit = settings.edit
        video_out = settings.output == OUTPUT_VIDEO
        self.inspector.set_reverse_warning(
            reverse_warning(reverse_memory(edit, info, sound_only=not video_out)) if info else "")
        self.inspector.set_frame_rate_hint(frame_rate_hint(info) if info and edit.frame_rate is None else "")
        self.timeline.setTrim(edit.start, edit.end)
        self.timeline.setKeep(edit.keep)
        self.player.set_segments(edit.segments(info.duration if info else None))
        self.player.set_rate(edit.speed)
        self.compare_button.setEnabled(video_out and bool(info and info.has_video))
        if info is None or not info.has_video:
            self.view.set_crop(None, 1.0)
            return
        self.view.set_orientation(info.display_rotation, edit.rotate if video_out else 0, edit.flip and video_out)
        if video_out and edit.crops:
            geometry = plan_geometry(info.width, info.height, edit)
            self.view.set_crop((geometry.width, geometry.height, *geometry.crop), edit.zoom)
        else:
            self.view.set_crop(None, edit.zoom)
        if self.compare_button.isChecked():
            self._after_timer.start()

    # ----- editing ------------------------------------------------------------

    def apply_settings(self, item: MediaItem, settings: ClipSettings) -> None:
        """Set a file's settings and refresh whatever shows them (used by undo/redo too)."""
        item.settings = settings.copy()
        if item.list_item is not None:
            self._show_item_text(item)
        if item is self.current:
            self._show_settings(item)

    def push_settings(self, new: ClipSettings, label: str, final: bool = True) -> None:
        item = self.current
        if item is None or new == item.settings and final and not self.merge_open:
            return
        self.undo_stack.push(SettingsCommand(self, item, item.settings, new, label, mergeable=True))
        self.merge_open = not final

    def _edit_finished(self) -> None:
        self.merge_open = False
        if self.compare_button.isChecked():
            self._after_timer.start()

    def _inspector_edited(self, new: ClipSettings, label: str, final: bool) -> None:
        self.push_settings(new, label, final)

    def _crop_moved(self, pos_x: float, pos_y: float) -> None:
        if self.current:
            new = self.current.settings.copy()
            new.edit.pos_x, new.edit.pos_y = pos_x, pos_y
            self.push_settings(new, "Move crop", final=False)

    def _zoom_changed(self, zoom: float) -> None:
        if self.current:
            new = self.current.settings.copy()
            new.edit.zoom = zoom
            self.push_settings(new, "Zoom", final=False)

    def _timeline_trim(self, start: Optional[float], end: Optional[float]) -> None:
        if self.current:
            new = self.current.settings.copy()
            new.edit.start, new.edit.end = start, end
            self.push_settings(new, "Trim", final=False)

    def _timeline_keep(self, keep: list) -> None:
        if self.current:
            new = self.current.settings.copy()
            new.edit.keep = list(keep)
            self.push_settings(new, "Cut", final=False)

    def cut_at(self, action: str, t: Optional[float] = None) -> None:
        """Split, cut out ("remove") or put back ("restore") the part at `t` (default: the
        playhead), or put back every cut part ("restore_all")."""
        item = self.current
        if item is None or item.info is None or not item.info.duration:
            return
        duration = item.info.duration
        t = self.player.position if t is None else t
        keep = item.settings.edit.keep
        if action == "split":
            new_keep, label = split_at(keep, t, duration), "Split"
            problem = ("This moment is cut out." if part_index(keep, t, duration) is None
                       else "Too close to the start or end of a part to split there.")
        elif action == "remove":
            new_keep, label = remove_part_at(keep, t, duration), "Cut out part"
            problem = ("This moment is already cut out." if part_index(keep, t, duration) is None
                       else "Split the video first (S), then remove the part you don't want.")
        elif action == "restore":
            new_keep, label, problem = restore_part_at(keep, t, duration), "Put back part", ""
        else:
            new_keep, label, problem = [], "Put back all parts", ""
        if new_keep == keep:
            if problem:
                self.statusBar().showMessage(problem, 4000)
            return
        new = item.settings.copy()
        new.edit.keep = new_keep
        self.push_settings(new, label)

    def _timeline_menu(self, t: float, pos) -> None:
        item = self.current
        if item is None or item.info is None or not item.info.duration:
            return
        keep, duration = item.settings.edit.keep, item.info.duration
        menu = QMenu(self)
        if part_index(keep, t, duration) is None:
            menu.addAction("Put back this part", lambda: self.cut_at("restore", t))
        else:
            split = menu.addAction("Split here", lambda: self.cut_at("split", t))
            split.setEnabled(split_at(keep, t, duration) != keep)
            remove = menu.addAction("Cut out this part", lambda: self.cut_at("remove", t))
            remove.setEnabled(remove_part_at(keep, t, duration) != keep)
        menu.addSeparator()
        restore = menu.addAction("Put back all cut parts", lambda: self.cut_at("restore_all"))
        restore.setEnabled(bool(keep))
        menu.exec(pos)

    def trim_at_playhead(self, which: str) -> None:
        item = self.current
        if item is None or item.info is None or not item.info.duration:
            return
        t = self.player.position
        new = item.settings.copy()
        start, end = new.edit.start, new.edit.end
        if which == "start":
            if end is not None and t >= end:
                self.statusBar().showMessage("The start has to be before the end of the trim.", 4000)
                return
            new.edit.start = t if t > 0.05 else None
        else:
            if t <= (start or 0.0):
                self.statusBar().showMessage("The end has to be after the start of the trim.", 4000)
                return
            new.edit.end = t if t < item.info.duration - 0.05 else None
        self.push_settings(new, "Trim")

    def apply_to_all(self) -> None:
        source = self.current
        if source is None or len(self.items) < 2:
            self.statusBar().showMessage("Add more files to copy these settings to them.", 4000)
            return
        self.undo_stack.beginMacro("Apply to all files")
        for item in self.items:
            if item is not source:
                new = apply_to(item.settings, source.settings)
                if item.info is not None and not item.info.has_video:
                    new.output = OUTPUT_AUDIO
                self.undo_stack.push(SettingsCommand(self, item, item.settings, new, "Apply to all", False))
        self.undo_stack.endMacro()
        self.statusBar().showMessage(f"Copied the settings to {len(self.items) - 1} other files.", 4000)

    def save_preset(self) -> None:
        if self.current is None:
            return
        name, ok = QInputDialog.getText(self, "Save preset", "Name for these settings:")
        name = name.strip()
        if ok and name:
            self.user_presets[name] = self.current.settings.to_dict()
            self.inspector.set_presets(self.user_presets)
            self._save_preferences()
            self.statusBar().showMessage(f"Saved the preset “{name}”.", 4000)

    def choose_output_folder(self) -> None:
        menu = QMenu(self)
        menu.addAction("Choose a folder…", self._pick_output_folder)
        reset = menu.addAction("Next to each original file", lambda: self._set_output_folder(None))
        reset.setEnabled(self.output_dir is not None)
        menu.exec(self.cursor().pos())

    def _pick_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Save exports to", str(self.output_dir or ""))
        if folder:
            self._set_output_folder(Path(folder))

    def _set_output_folder(self, folder: Optional[Path]) -> None:
        self.output_dir = folder
        self._show_output_folder()
        self._save_preferences()

    def _show_output_folder(self) -> None:
        self.inspector.set_output_folder(str(self.output_dir) if self.output_dir else "Next to each original file")

    # ----- smooth previews ------------------------------------------------------

    def _preview_copy(self, item: MediaItem) -> Optional[Path]:
        """The small copy to play instead of `item`'s file, if there is one and it should be used."""
        if self.smooth_previews and item.proxy is not None and item.proxy.is_file():
            return item.proxy
        return None

    def _prepare_preview_copy(self, item: MediaItem) -> None:
        """Start making a smooth preview copy of `item` if it is a big video and has none yet."""
        if (self.closed or not self.smooth_previews or item.loading or item.error or item.proxy is not None
                or item.proxy_failed or item.proxy_progress is not None or not needs_proxy(item.info)):
            self._update_preview_status()
            return
        item.proxy_progress = 0.0
        if not self.proxies.request(item, item.path, item.info):
            item.proxy_progress = None
        self._update_preview_status()

    def _update_preview_status(self) -> None:
        item = self.current
        progress = item.proxy_progress if item is not None else None
        self.preview_status.setText(f"Preparing a smooth preview… {progress:.0%}" if progress is not None else "")

    def _on_proxy_progress(self, item: MediaItem, fraction: float) -> None:
        if item in self.items and item.proxy_progress is not None:
            item.proxy_progress = fraction
            if item is self.current:
                self._update_preview_status()

    def _on_proxy_done(self, item: MediaItem, path: Optional[Path], error: str) -> None:
        item.proxy_progress = None
        if self.closed or item not in self.items:
            if path is not None:
                self._delete_soon(path)
            return
        if path is None:
            item.proxy_failed = True
            if item is self.current:
                self.statusBar().showMessage("Couldn't prepare a smooth preview, so the original plays instead.", 6000)
        else:
            item.proxy = path
            if item is self.current and self.smooth_previews:
                self.player.swap_source(path, upright=True)
                self.statusBar().showMessage("The smooth preview is ready.", 4000)
        self._update_preview_status()

    def set_smooth_previews(self, on: bool) -> None:
        """The View menu's switch: play small copies of big videos, or always the originals."""
        if on == self.smooth_previews:
            return
        self.smooth_previews = on
        self.store.setValue("smoothPreviews", on)
        item = self.current
        if not on:
            self.proxies.cancel_all()
            for other in self.items:
                other.proxy_progress = None
            if item is not None:
                self.player.swap_source(item.path, upright=False)
        elif item is not None:
            copy = self._preview_copy(item)
            if copy is not None:
                self.player.swap_source(copy, upright=True)
            else:
                self._prepare_preview_copy(item)
        self._update_preview_status()

    def _drop_preview_copy(self, item: MediaItem) -> None:
        """Stop making (and delete) the smooth preview of a file that was removed from the list."""
        self.proxies.cancel(item)
        item.proxy_progress = None
        if item.proxy is not None:
            if self.player.source == item.proxy:
                self.player.load(None)      # let go of the file first
            self._delete_soon(item.proxy)
            item.proxy = None

    def _delete_soon(self, path: Path, attempts: int = 20) -> None:
        """Delete a file the player may keep open for a moment after being told to let go of it."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if attempts > 0 and not self.closed:
                QTimer.singleShot(250, lambda: self._delete_soon(path, attempts - 1))

    def _on_player_error(self, message: str) -> None:
        item = self.current
        if item is not None and item.proxy is not None and self.player.source == item.proxy:
            # The copy won't play: forget it and carry on with the original.
            item.proxy_failed = True
            item.proxy = None
            self.player.swap_source(item.path, upright=False)
            self.statusBar().showMessage("The smooth preview wouldn't play, so the original plays instead.", 6000)
            return
        self.statusBar().showMessage(f"Can't play this file: {message}")

    # ----- graphics card and help -----------------------------------------------

    def _find_gpu(self) -> None:
        """Background thread: find out which hardware encoders work (checked once per run)."""
        try:
            found = gpu.detect_encoders(self.ffmpeg)
            name = gpu.gpu_name(found[0]) if found else None
        except Exception:      # never let this stop the app
            found, name = (), None
        self.signals.gpuReady.emit((found, name))

    def _on_gpu_ready(self, result) -> None:
        found, name = result
        self.gpu_encoders = found
        self.inspector.set_gpu_name(name)

    def open_log_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(diagnostics.log_path().parent)))

    def problem_report_url(self) -> str:
        """The link that opens a new GitHub issue with this PC's details and the end of the log filled in."""
        return diagnostics.issue_url(diagnostics.system_lines(self.ffmpeg, self.gpu_encoders),
                                     diagnostics.recent_log_lines())

    def report_problem(self) -> None:
        """Open GitHub's new-issue page in the browser, once the user has seen what it will contain."""
        answer = QMessageBox.question(
            self, "Report a problem",
            "This opens GitHub in your browser with a new problem report already filled in: the app and "
            "Windows versions, and the last lines of the log (which can include the names of files you "
            "worked on).\n\nNothing is sent until you press Submit there, and you can edit or delete "
            "anything first. You need a GitHub account to submit it.\n\nOpen it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        if answer == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl(self.problem_report_url()))

    def show_about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}", diagnostics.about_text())

    # ----- playback -----------------------------------------------------------

    def _on_position(self, seconds: float) -> None:
        self.timeline.setPosition(seconds)
        duration = self.current.info.duration if self.current and self.current.info else None
        self.time_label.setText(f"{format_time(seconds)} / {format_time(duration or 0)}")
        if self.compare_button.isChecked() and not self.player.playing:
            self._after_timer.start()

    def _on_duration(self, seconds: float) -> None:
        if self.current and (self.current.info is None or not self.current.info.duration):
            self.timeline.setDuration(seconds)

    def _on_playing(self, playing: bool) -> None:
        self.play_button.setIcon(icons.icon("pause" if playing else "play"))
        if playing and self.compare_button.isChecked():
            self.compare_button.setChecked(False)
            self._compare_toggled(False)

    def _preview_mute_toggled(self, muted: bool) -> None:
        self.player.set_muted(muted)
        self.mute_button.setIcon(icons.icon("mute" if muted else "volume"))

    def _compare_toggled(self, on: bool) -> None:
        if on:
            self.player.pause()
            self.view.set_after(None, True)
            self._after_timer.start(0)
        else:
            self.view.set_after(None, False)

    def _render_after(self) -> None:
        item = self.current
        if (self.closed or item is None or item.info is None or not item.info.has_video
                or not self.compare_button.isChecked()):
            return
        self._after_token += 1
        token, settings, at = self._after_token, item.settings.copy(), self.player.position
        ratio = self.view.devicePixelRatioF()
        size = (int(min(self.view.width() * ratio, 1600)), int(min(self.view.height() * ratio, 1600)))
        target = self.tmpdir / f"after_{token}.png"

        def work() -> None:
            try:
                render_frame(self.ffmpeg, item.path, target, at, size, settings.edit, item.info)
                image = QImage(str(target))
                target.unlink(missing_ok=True)
                self.signals.afterReady.emit(token, image)
            except (ConversionError, OSError) as exc:
                self.signals.afterReady.emit(token, str(exc))
            self._remove_tmpdir_if_closed()
        threading.Thread(target=work, daemon=True).start()

    def _on_after_ready(self, token: int, result) -> None:
        if token != self._after_token or not self.compare_button.isChecked():
            return
        if isinstance(result, QImage) and not result.isNull():
            self.view.set_after(result, True)
        else:
            self.statusBar().showMessage("Couldn't render the preview.", 4000)

    # ----- exporting ----------------------------------------------------------

    def _make_job(self, item: MediaItem) -> Optional[Job]:
        """A job for exporting `item`, or None (with a message) if it can't be exported yet."""
        if item.loading:
            self.statusBar().showMessage(f"{item.path.name} is still loading; try again in a moment.", 4000)
            return None
        if item.error:
            return None
        return Job(item.path, item.settings.copy(), self.output_dir, item.info)

    def _confirm_heavy(self, jobs: list[Job]) -> bool:
        """Ask whether to go ahead with exports that play something backwards and need a lot of memory."""
        if not self.confirm_heavy_exports:
            return True
        if len(jobs) == 1:
            text = f"Reversing {jobs[0].title} needs about {format_memory(jobs[0].reverse_memory)} of memory."
        else:
            lines = "\n".join(f"{job.title}: about {format_memory(job.reverse_memory)}" for job in jobs[:6])
            more = f"\nand {len(jobs) - 6} more" if len(jobs) > 6 else ""
            text = f"These exports play clips backwards, which needs a lot of memory:\n\n{lines}{more}"
        answer = QMessageBox.question(
            self, "This needs a lot of memory",
            f"{text}\n\nIf your PC has less, the export can fail or make everything slow. Export anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _queue_jobs(self, jobs: list[Job]) -> list[Job]:
        """Add `jobs` to the export queue, after checking with the user about the ones that need
        a lot of memory. Returns the jobs that were queued."""
        heavy = [job for job in jobs if job.reverse_memory > REVERSE_WARN_BYTES]
        if heavy and not self._confirm_heavy(heavy):
            skipped = {job.id for job in heavy}
            jobs = [job for job in jobs if job.id not in skipped]
            self.statusBar().showMessage("Left out the exports that need a lot of memory.", 6000)
        return [self.queue.add(job) for job in jobs]

    def export_current(self) -> None:
        job = self._make_job(self.current) if self.current is not None else None
        if job and self._queue_jobs([job]):
            self.statusBar().showMessage(f"Exporting {self.current.path.name}…", 4000)

    def export_all(self) -> None:
        jobs = [job for job in (self._make_job(item) for item in list(self.items)) if job]
        count = len(self._queue_jobs(jobs))
        if count:
            self.statusBar().showMessage(f"Added {count} export{'s' if count != 1 else ''} to the queue.", 4000)

    def join_clips(self) -> None:
        """Export the selected files (or all of them, if one or none is selected) as one file,
        in list order, with the first file's output settings."""
        selected = {id(self._item_at(self.media_list.row(li))) for li in self.media_list.selectedItems()}
        chosen = [item for item in self.items if id(item) in selected] if len(selected) > 1 else list(self.items)
        if len(chosen) < 2:
            self.statusBar().showMessage("Add two or more files to join them into one.", 4000)
            return
        loading = next((item for item in chosen if item.loading), None)
        if loading is not None:
            self.statusBar().showMessage(f"{loading.path.name} is still loading; try again in a moment.", 4000)
            return
        broken = next((item for item in chosen if item.error), None)
        if broken is not None:
            self.statusBar().showMessage(f"Can't join {broken.path.name}: {broken.error}", 6000)
            return
        first = chosen[0]
        audio = first.settings.output == OUTPUT_AUDIO
        silent = next((item for item in chosen if not audio and not item.info.has_video), None)
        if silent is not None:
            self.statusBar().showMessage(f"{silent.path.name} has no picture, so it can only be joined as "
                                         "MP3: set the first file's output to Audio (MP3).", 6000)
            return
        clips = [Clip(item.path, item.settings.copy(), item.info) for item in chosen]
        job = Job(first.path, first.settings.copy(), self.output_dir, first.info, clips=clips)
        if self._queue_jobs([job]):
            self.statusBar().showMessage(f"Joining {len(chosen)} files into one {'MP3' if audio else 'video'}…", 4000)

    def _job_updated(self, job: Job) -> None:
        if job.state.finished and job.state.value == "Failed":
            self.statusBar().showMessage(f"Export failed: {job.source.name}", 6000)

    # ----- keyboard, drag and drop, closing ------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not self.isActiveWindow() or event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            return super().eventFilter(obj, event)
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QAbstractSpinBox)) or (isinstance(focus, QComboBox) and focus.isEditable()):
            return super().eventFilter(obj, event)
        if self.current is None:
            return super().eventFilter(obj, event)
        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        handlers = {
            Qt.Key.Key_Space: self.player.toggle,
            Qt.Key.Key_K: self.player.pause,
            Qt.Key.Key_J: lambda: self.player.step(-5),
            Qt.Key.Key_L: lambda: self.player.step(5),
            Qt.Key.Key_Left: lambda: self.player.step(-1 if shift else -FRAME_STEP),
            Qt.Key.Key_Right: lambda: self.player.step(1 if shift else FRAME_STEP),
            Qt.Key.Key_I: lambda: self.trim_at_playhead("start"),
            Qt.Key.Key_O: lambda: self.trim_at_playhead("end"),
            Qt.Key.Key_S: lambda: self.cut_at("split"),
            Qt.Key.Key_X: lambda: self.cut_at("remove"),
            Qt.Key.Key_B: self.compare_button.click,
            Qt.Key.Key_Home: lambda: self.player.seek(self.current.settings.edit.start or 0.0),
        }
        if key == Qt.Key.Key_Delete and focus is self.media_list:
            self.remove_selected()
            return True
        handler = handlers.get(key)
        if handler is None:
            return super().eventFilter(obj, event)
        handler()
        return True

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.add_paths(paths)
            event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        if self.queue.busy:
            answer = QMessageBox.question(self, "Exports still running",
                                          "Some exports haven't finished. Stop them and quit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._save_preferences()
        QApplication.instance().removeEventFilter(self)
        self.queue.shutdown()
        self.player.load(None)          # lets go of the preview copy's file
        self.closed = True
        self.proxies.shutdown()
        self.loader.shutdown(wait=False, cancel_futures=True)
        self._remove_tmpdir()
        event.accept()

    def _remove_tmpdir(self) -> None:
        """Remove the temp folder. The player lets go of a preview copy a moment after it is told to, and
        Windows won't delete a file that is still open, so keep trying briefly."""
        for _ in range(40):
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            if not self.tmpdir.exists():
                return
            QApplication.processEvents()
            time.sleep(0.05)

    def _remove_tmpdir_if_closed(self) -> None:
        """A background job still running when the window closed may have made its folder again."""
        if self.closed:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
