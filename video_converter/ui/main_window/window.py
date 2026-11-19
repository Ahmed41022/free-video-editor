"""The main editor window."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QSettings, QTimer, Signal
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QApplication, QMainWindow

from ... import APP_NAME, __version__, tempclean, updates
from ...jobs import JobQueue
from ...procs import DaemonPool, ProcessRegistry
from ...proxy import ProxyMaker
from ..player import Player
from ..queue_panel import QueueBridge
from .captions import CaptionsMixin
from .editing import EditingMixin
from .exporting import ExportingMixin
from .graphics import GPU_SEARCH_LATEST_MS, GraphicsMixin
from .help import HelpMixin
from .items import MediaItem
from .layout import LayoutMixin
from .library import LibraryMixin
from .lifecycle import LifecycleMixin
from .playback import PlaybackMixin
from .preferences import PreferencesMixin
from .previews import PreviewsMixin
from .selection import SelectionMixin
from .shortcuts import ShortcutsMixin
from .updates import UpdatesMixin

UNDO_LIMIT = 300


class Signals(QObject):
    """Results from background threads, delivered on the UI thread."""
    loaded = Signal(object, object, object, object)  # item, info, thumbnail paths, waveform
    afterReady = Signal(int, object)  # request token, QImage or error text
    stillReady = Signal(
        object, object)  # item, the first picture of its video (a QImage) to show meanwhile
    gpuReady = Signal(object)  # the graphics card's name, or None if there is none
    proxyProgress = Signal(object, float)  # item, how much of its smooth preview is made
    proxyDone = Signal(object, object, str)  # item, the preview copy or None, why not
    frameSaved = Signal(str, str)  # where the frame was saved ("" if it wasn't), why not
    updateChecked = Signal(
        object, object)  # when the check was made (None = not), the newer release or None


class MainWindow(LayoutMixin, PreferencesMixin, LibraryMixin, SelectionMixin, EditingMixin,
                 CaptionsMixin, PreviewsMixin, PlaybackMixin, GraphicsMixin, HelpMixin,
                 ExportingMixin, UpdatesMixin, ShortcutsMixin, LifecycleMixin, QMainWindow):

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
        self.theme_mode = "dark"  # the theme in use: "dark" or "light"
        self.theme_choice = "system"  # the theme asked for: "system", "dark" or "light" (what is saved)
        self.models_root: Optional[
            Path] = None  # where the speech models are kept (None = the app's own folder)
        self.tmpdir = Path(tempfile.mkdtemp(prefix="videoconverter-"))
        tempclean.mark_owner(
            self.tmpdir)  # so a later start doesn't sweep this folder up while the window is open
        self.closed = False
        self._restarting = False  # closing to start the app again, after the user agreed to stop exports
        self.confirm_heavy_exports = True  # ask before an export that needs a lot of memory
        self.smooth_previews = True  # play a small copy of big videos (View menu)
        self.check_updates = True  # look for a newer version once a day (Help menu)
        self.update_release: Optional[
            updates.Release] = None  # the newer version the banner is about
        self.gpu_encoders: Optional[tuple[str, ...]] = None  # None until the search has finished
        # Daemon threads, and the ffmpeg runs they start are remembered so closing can end them: neither may keep the
        # process alive after the window is gone.
        self.loader = DaemonPool(2, "media-loader")
        self.procs = ProcessRegistry()
        self.instance = None  # listens for other copies of the app (see watch_for_other_copies)
        self.signals = Signals()
        self.signals.loaded.connect(self._on_loaded)
        self.signals.afterReady.connect(self._on_after_ready)
        self.signals.stillReady.connect(self._on_still_ready)
        self.signals.gpuReady.connect(self._on_gpu_ready)
        self.signals.proxyProgress.connect(self._on_proxy_progress)
        self.signals.proxyDone.connect(self._on_proxy_done)
        self.signals.frameSaved.connect(self._on_frame_saved)
        self.signals.updateChecked.connect(self._on_update_checked)
        self.proxies = ProxyMaker(
            ffmpeg, self.tmpdir / "preview", self.signals.proxyProgress.emit,
            self.signals.proxyDone.emit)
        self._after_token = 0
        self._after_busy = False  # a before/after picture is being rendered: at most one at a time
        self._after_again = False  # ...and another was asked for meanwhile
        self._after_timer = QTimer(self, singleShot=True, interval=250, timeout=self._render_after)
        self._gpu_started = False
        self._gpu_timer = QTimer(
            self, singleShot=True, interval=GPU_SEARCH_LATEST_MS, timeout=self.start_gpu_search)

        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(
            UNDO_LIMIT)  # steps hold copies of settings: don't keep them forever
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
        self._gpu_timer.start()
