"""The files in the list."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QWidget

from ...core import MediaInfo
from ...project import ClipSettings

_item_numbers = itertools.count(1)


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
    proxy: Optional[Path] = None  # the smooth preview copy, once it is ready
    proxy_progress: Optional[float] = None  # how far along the copy is, while it is being made
    proxy_failed: bool = False
    play_original: bool = False  # play the file as it is, not its smooth copy (this file only)
    thumb_paths: list[Path] = field(default_factory=list)  # the thumbnail files, until exit
    restored: bool = False  # it came back with a project or recovered work
    missing: bool = False  # the file isn't there (moved or deleted); a project still keeps it
    serial: int = field(
        default_factory=lambda: next(_item_numbers))  # never reused, unlike id(): names its folder
    _resolved: Optional[Path] = field(default=None, repr=False)

    @property
    def resolved(self) -> Path:
        """The file's full path, worked out once: on a network drive each look-up is slow."""
        if self._resolved is None:
            try:
                self._resolved = self.path.resolve()
            except OSError:
                self._resolved = self.path
        return self._resolved


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
