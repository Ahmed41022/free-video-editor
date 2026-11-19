"""Undoable steps: a settings change, a rewritten subtitle file, taking files out of the list."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QUndoCommand

from ...i18n import isolate, tr
from ...project import ClipSettings
from .items import MediaItem

if TYPE_CHECKING:
    from .window import MainWindow


class SettingsCommand(QUndoCommand):
    """One undoable settings change. Continuous edits (dragging the crop box or a
    slider) merge into a single step."""

    MERGE_ID = 1001

    def __init__(
            self, window: "MainWindow", item: MediaItem, before: ClipSettings, after: ClipSettings,
            label: str, mergeable: bool, reveal: bool = True):
        super().__init__(label)
        self.window, self.item = window, item
        self.before, self.after = before.copy(), after.copy()
        self.mergeable = mergeable
        self.reveal = reveal  # undo and redo select the file they changed, so the change is seen
        self._pushed = False  # the first redo is the edit itself, on the file already shown

    def id(self) -> int:
        return self.MERGE_ID if self.mergeable else -1

    def mergeWith(self, other: QUndoCommand) -> bool:
        if (isinstance(other, SettingsCommand) and other.item is self.item
                and other.text() == self.text() and self.window.merge_open):
            self.after = other.after.copy()
            return True
        return False

    def redo(self) -> None:
        self.window.apply_settings(self.item, self.after)
        if self._pushed and self.reveal:
            self.window.reveal(self.item)
        self._pushed = True

    def undo(self) -> None:
        self.window.apply_settings(self.item, self.before)
        if self.reveal:
            self.window.reveal(self.item)


class CaptionsFileCommand(QUndoCommand):
    """One undoable rewrite of a subtitle file: undo puts back the bytes it had. The file as it was is
    kept once, next to it as `name.srt.bak`, before the first time it is overwritten (the rewrite is UTF-8,
    and the original may have been in an old code page or a form of Unicode that can't be got back exactly)."""

    def __init__(
            self, window: "MainWindow", item: MediaItem, path: Path, before: bytes, after: bytes,
            label: str):
        super().__init__(label)
        self.window, self.item, self.path = window, item, path
        self.before, self.after = before, after
        self._pushed = False

    def redo(self) -> None:
        self._keep_backup()
        self._write(self.after)
        if self._pushed:
            self.window.reveal(self.item)
        self._pushed = True

    def _keep_backup(self) -> None:
        backup = self.path.with_name(self.path.name + ".bak")
        try:
            if not backup.exists():
                backup.write_bytes(self.before)
        except OSError as exc:
            self.window.statusBar().showMessage(
                tr(
                    "Couldn't keep a copy of {name} first: {reason}", name=isolate(self.path.name),
                    reason=exc.strerror or exc), 6000)

    def undo(self) -> None:
        self._write(self.before)
        self.window.reveal(self.item)

    def _write(self, data: bytes) -> None:
        try:
            temp = self.path.with_name(self.path.name + ".part")
            temp.write_bytes(data)
            temp.replace(self.path)
        except OSError as exc:
            self.window.statusBar().showMessage(
                tr(
                    "Couldn't update {name}: {reason}", name=isolate(self.path.name),
                    reason=exc.strerror or exc), 6000)
        self.window.captions_file_changed(self.item)


class RemoveFilesCommand(QUndoCommand):
    """Taking files out of the list, which undo puts back: each at the row it was in, with its settings and
    thumbnails (the items themselves are kept)."""

    def __init__(self, window: "MainWindow", entries: list[tuple[int, MediaItem]], label: str):
        super().__init__(label)
        self.window, self.entries = window, sorted(entries, key=lambda entry: entry[0])

    def redo(self) -> None:
        self.window.take_items([item for _row, item in self.entries])

    def undo(self) -> None:
        self.window.restore_items(self.entries)
