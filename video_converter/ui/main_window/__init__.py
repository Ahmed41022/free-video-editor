"""The main editor window."""

from __future__ import annotations

from .commands import CaptionsFileCommand, RemoveFilesCommand, SettingsCommand
from .graphics import GPU_SEARCH_DELAY_MS, GPU_SEARCH_LATEST_MS
from .items import MediaItem, MediaList
from .layout import INSPECTOR_WIDTH
from .library import FILE_FILTER, file_filter
from .shortcuts import FRAME_STEP
from .window import UNDO_LIMIT, MainWindow, Signals

__all__ = [
    "FILE_FILTER", "FRAME_STEP", "GPU_SEARCH_DELAY_MS", "GPU_SEARCH_LATEST_MS", "INSPECTOR_WIDTH",
    "UNDO_LIMIT", "CaptionsFileCommand", "MainWindow", "MediaItem", "MediaList",
    "RemoveFilesCommand", "SettingsCommand", "Signals", "file_filter",
]
