"""Building the window: toolbar, menus, panels, and the signals between them."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QSplitter, QToolBar, QToolButton,
    QVBoxLayout, QWidget,
)

from ... import APP_NAME, i18n
from ...i18n import tr, trc
from .. import icons
from ..inspector import Inspector
from ..player import VideoView
from ..queue_panel import QueuePanel
from ..timeline import Timeline
from .items import MediaList

# The width the settings panel starts with. Every section fits in it, in English and in Arabic (it fits even in
# the narrowest the panel can be made, 330), with room for longer words.
INSPECTOR_WIDTH = 380


class LayoutMixin:
    """The window's widgets."""

    def _build(self) -> None:
        toolbar = QToolBar(tr("Main"))
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

        self.add_action = action(tr("Add files"), "add", self.choose_files, "Ctrl+O")
        self.folder_action = action(tr("Add folder"), "folder", self.choose_folder)
        self.remove_action = action(trc("toolbar", "Remove"), "trash", self.remove_selected, tip=tr("Remove the selected file"))
        toolbar.addSeparator()
        self.undo_action = self.undo_stack.createUndoAction(self, tr("Undo"))
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.setData("undo")
        self.redo_action = self.undo_stack.createRedoAction(self, tr("Redo"))
        self.redo_action.setShortcuts([QKeySequence.StandardKey.Redo, QKeySequence("Ctrl+Y")])
        self.redo_action.setData("redo")
        toolbar.addAction(self.undo_action)
        toolbar.addAction(self.redo_action)
        toolbar.addSeparator()
        self.frame_action = action(tr("Save frame"), "camera", self.save_frame_image, "Ctrl+Shift+S",
                                   tip=tr("Save the frame at the playhead as an image, with your edits"))
        self.frame_action.setEnabled(False)
        toolbar.addSeparator()
        self.theme_action = action(tr("Theme"), "moon", self.toggle_theme, tip=tr("Switch between dark and light"))

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.join_button = QPushButton(tr("Join into one"))
        self.join_button.setToolTip(tr("Export the files as one video, in list order: the selected files, or all of "
                                       "them. Drag files in the list to reorder them. (Ctrl+J)"))
        self.join_button.clicked.connect(self.join_clips)
        toolbar.addWidget(self.join_button)
        self.export_all_button = QPushButton(tr("Export all"))
        self.export_all_button.setToolTip(tr("Export every file in the list (Ctrl+Shift+E)"))
        self.export_all_button.clicked.connect(self.export_all)
        self.export_button = QPushButton(tr("Export"))
        self.export_button.setObjectName("primary")
        self.export_button.setToolTip(tr("Export the selected file (Ctrl+E)"))
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
        center.setLayoutDirection(Qt.LayoutDirection.LeftToRight)     # time runs left to right in every language
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        self.view = VideoView()
        center_layout.addWidget(self.view, 1)
        transport = self.transport = QWidget()
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

        self.back_button = tool("back", tr("Back 5 seconds (J)"), lambda: self.player.step(-5))
        self.play_button = tool("play", tr("Play / pause (Space)"), self.player.toggle)
        self.forward_button = tool("forward", tr("Forward 5 seconds (L)"), lambda: self.player.step(5))
        self.time_label = QLabel("0:00.0 / 0:00.0")
        row.addWidget(self.time_label)
        row.addStretch(1)
        self.in_button = tool("mark_in", tr("Start the trim here (I)"), lambda: self.trim_at_playhead("start"))
        self.out_button = tool("mark_out", tr("End the trim here (O)"), lambda: self.trim_at_playhead("end"))
        self.split_button = tool("split", tr("Split at the playhead (S)"), lambda: self.cut_at("split"))
        self.cut_button = tool("scissors", tr("Cut out the part under the playhead (X)"),
                               lambda: self.cut_at("remove"))
        self.compare_button = tool("compare", tr("Show the result (B)"), self._compare_toggled, checkable=True)
        self.compare_button.setText(tr("Before / after"))
        self.compare_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.mute_button = tool("volume", tr("Mute the preview"), self._preview_mute_toggled, checkable=True)
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
        top.setSizes([230, 1320 - 230 - INSPECTOR_WIDTH, INSPECTOR_WIDTH])
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
        self._build_update_banner()
        self._build_menus()
        self.statusBar().showMessage(tr("Add videos to get started: drop them here or press Ctrl+O."))
        self.refresh_icons()

    def _build_menus(self) -> None:
        view = self.menuBar().addMenu(tr("&View"))
        self.smooth_action = view.addAction(tr("Smooth previews for large videos"))
        self.smooth_action.setCheckable(True)
        self.smooth_action.setChecked(self.smooth_previews)
        self.smooth_action.setToolTip(tr("Play a small copy of 4K and other very large videos, so the preview "
                                         "doesn't stutter. Exports always use the original."))
        self.smooth_action.setStatusTip(self.smooth_action.toolTip())
        self.smooth_action.toggled.connect(self.set_smooth_previews)
        self._build_language_menu(view)
        help_menu = self.menuBar().addMenu(tr("&Help"))
        help_menu.addAction(tr("Open the log folder"), self.open_log_folder)
        help_menu.addAction(tr("Report a problem…"), self.report_problem)
        help_menu.addSeparator()
        self.updates_action = help_menu.addAction(tr("Check for updates"))
        self.updates_action.setCheckable(True)
        self.updates_action.setChecked(self.check_updates)
        self.updates_action.setToolTip(tr("Once a day, ask GitHub whether a newer version is out. "
                                          "Nothing else is sent."))
        self.updates_action.setStatusTip(self.updates_action.toolTip())
        self.updates_action.toggled.connect(self.set_check_updates)
        help_menu.addSeparator()
        help_menu.addAction(tr("About {app}", app=APP_NAME), self.show_about)

    def _build_language_menu(self, view: QMenu) -> None:
        """View > Language: English, Arabic, or whatever Windows uses. The choice takes effect on the next start."""
        menu = view.addMenu(tr("&Language"))
        menu.setToolTip(tr("The language of the app's menus and messages"))
        self.language_actions: dict[str, QAction] = {}
        group = QActionGroup(self)
        group.setExclusive(True)
        choice = i18n.saved_choice(self.store)
        for code, label in ((i18n.SYSTEM, tr("Same as Windows")), *i18n.LANGUAGES.items()):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(code == choice)
            act.setProperty("language", code)
            group.addAction(act)
            act.triggered.connect(lambda _=False, code=code: self.set_language_choice(code))
            self.language_actions[code] = act

    def _connect(self) -> None:
        self.media_list.currentRowChanged.connect(lambda row: self.select(self._item_at(row)))
        self.media_list.model().rowsMoved.connect(self._rows_moved)
        self.media_list.customContextMenuRequested.connect(self._media_menu)
        self.player.frameReady.connect(self.view.set_frame)
        self.view.framePainted.connect(self.player.frame_painted)
        self.player.frame_limit = self._frame_limit
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
        self.view.overlayPicked.connect(self._overlay_picked)
        self.view.overlayMoved.connect(self._overlay_moved)
        self.view.overlayEditFinished.connect(self._edit_finished)
        self.inspector.settingsEdited.connect(self._inspector_edited)
        self.inspector.applyToAllRequested.connect(self.apply_to_all)
        self.inspector.savePresetRequested.connect(self.save_preset)
        self.inspector.playheadRequested.connect(self.trim_at_playhead)
        self.inspector.cutRequested.connect(self.cut_at)
        self.inspector.removeSilencesRequested.connect(self.remove_silences)
        self.inspector.outputFolderRequested.connect(self.choose_output_folder)
        self.inspector.autoCaptionsRequested.connect(self.make_captions)
        self.inspector.editCaptionsRequested.connect(self.edit_captions)
        self.inspector.textSelected.connect(self._text_selected)
        self.inspector.textTimeRequested.connect(self.text_time_at_playhead)
        self.bridge.jobUpdated.connect(self._job_updated)

    def _frame_limit(self) -> int:
        """How many pixels on its longer side a video frame can usefully have: what the preview shows, more when
        it is zoomed in. Bigger frames are shrunk when they arrive."""
        return int(max(self.view.width(), self.view.height()) * self.view.devicePixelRatioF() * max(self.view.zoom, 1.0))

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
