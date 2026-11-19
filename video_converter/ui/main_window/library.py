"""The list of files: adding, removing, loading each one's details."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFileDialog, QListWidgetItem, QMenu

from ...core import (
    AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, ConversionCancelled, MediaInfo, format_time, is_media_file,
)
from ...i18n import isolate, join_parts, ntr, tr
from ...media import ReadTimeout, extract_thumbnails, extract_waveform, probe_file
from ...project import OUTPUT_AUDIO, describe
from .. import icons
from .commands import RemoveFilesCommand
from .graphics import GPU_SEARCH_DELAY_MS
from .items import MediaItem

log = logging.getLogger(__name__)

FILE_FILTER = "Videos and audio ({});;All files (*.*)".format(
    " ".join(f"*{ext}"
             for ext in VIDEO_EXTENSIONS + AUDIO_EXTENSIONS))  # the English one; see file_filter()


def file_filter() -> str:
    """The Add files dialog's filter, in the user's language."""
    patterns = " ".join(f"*{ext}" for ext in VIDEO_EXTENSIONS + AUDIO_EXTENSIONS)
    return tr("Videos and audio ({patterns})", patterns=patterns) + ";;" + tr("All files (*.*)")


class LibraryMixin:
    """Adding, removing and loading files."""

    def choose_files(self) -> None:
        last = self._saved_text("lastDir")
        names, _ = QFileDialog.getOpenFileNames(
            self, tr("Add videos or audio"), last, file_filter())
        if names:
            self.store.setValue("lastDir", str(Path(names[0]).parent))
            self.add_paths([Path(n) for n in names])

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, tr("Add a folder of videos or audio"), self._saved_text("lastDir"))
        if folder:
            self.store.setValue("lastDir", folder)
            self.add_paths([Path(folder)])

    def add_paths(self, paths: list[Path]) -> int:
        """Add files (and the videos and audio files inside any folders). Returns how many were
        new."""
        files: list[Path] = []
        skipped: list[Path] = []  # files dropped or chosen that are not a video or audio file
        for path in paths:
            if path.is_dir():
                files.extend(sorted(p for p in path.iterdir() if p.is_file() and is_media_file(p)))
            elif path.is_file():
                (files if is_media_file(path) else skipped).append(path)
        known = {
            item.resolved
            for item in self.items
        }  # each item resolves its path once, not on every add
        added = 0
        for path in files:
            item = MediaItem(path)
            if item.resolved in known:
                continue
            known.add(item.resolved)
            item.list_item = QListWidgetItem(icons.icon("film", size=48), "")
            self._show_item_text(item)
            item.list_item.setData(Qt.ItemDataRole.UserRole, id(item))
            self.items.append(item)
            self.media_list.addItem(item.list_item)
            self.loader.submit(self._load_item, item)
            added += 1
        note = ""
        if skipped:
            names = ", ".join(isolate(p.name) for p in skipped[:3])
            more = " " + tr("and {n} more", n=len(skipped) - 3) if len(skipped) > 3 else ""
            note = tr("Skipped, not video or audio: {names}{more}", names=names, more=more)
        if added:
            self._update_list_actions()
            self.statusBar().showMessage(
                " ".join(filter(None, [ntr(added, "Added {n} file.", "Added {n} files."), note])),
                8000 if note else 4000)
            if self.current is None:
                self.media_list.setCurrentRow(len(self.items) - added)
        elif skipped:
            self.statusBar().showMessage(note, 8000)
        elif paths:
            self.statusBar().showMessage(tr("No new video or audio files there."), 4000)
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
        """Take the selected files (or the current one) out of the list. It is an undoable step, so
        the history of the other files is kept and Undo brings the files back."""
        rows = sorted(
            {self.media_list.row(selected)
             for selected in self.media_list.selectedItems()})
        entries = [
            (row, item) for row in (rows or [self.media_list.currentRow()])
            if (item := self._item_at(row)) is not None
        ]
        if entries:
            self.undo_stack.push(RemoveFilesCommand(self, entries, tr("Remove from list")))

    def take_items(self, items: list[MediaItem]) -> None:
        """Take `items` out of the list (the redo of RemoveFilesCommand). The last row goes first,
        so the selection never lands on a file that is about to go too."""
        first_row = min(self.media_list.row(item.list_item) for item in items)
        for item in sorted(items, key=lambda i: self.media_list.row(i.list_item), reverse=True):
            self.items.remove(item)
            self.media_list.takeItem(self.media_list.row(item.list_item))
            self._drop_preview_copy(item)
        self._update_list_actions()
        if self.current in items:
            self.current = None
            self.select(self.items[min(first_row, len(self.items) - 1)] if self.items else None)

    def restore_items(self, entries: list[tuple[int, MediaItem]]) -> None:
        """Put removed files back, each at the row it had (the undo of RemoveFilesCommand), and
        show the first."""
        ordered = sorted(entries, key=lambda entry: entry[0])
        for row, item in ordered:
            self.items.insert(min(row, len(self.items)), item)
            self.media_list.insertItem(min(row, self.media_list.count()), item.list_item)
        self._update_list_actions()
        self.media_list.setCurrentRow(self.items.index(ordered[0][1]))

    def reveal(self, item: MediaItem) -> None:
        """Select `item` in the list, so that a change undo or redo made to it is seen."""
        if item is not self.current and item in self.items:
            self.media_list.setCurrentRow(self.items.index(item))

    def _media_menu(self, point) -> None:
        if self.media_list.itemAt(point) is None:
            return
        menu = QMenu(self)
        menu.addAction(tr("Export"), self.export_current)
        many = len(self.media_list.selectedItems()) > 1
        join = menu.addAction(
            tr("Join the selected files into one") if many else tr("Join all files into one"),
            self.join_clips)
        join.setEnabled(len(self.items) > 1)
        menu.addAction(tr("Remove from list"), self.remove_selected)
        clicked = next(
            (item for item in self.items if item.list_item is self.media_list.itemAt(point)), None)
        if clicked is not None:
            menu.addSeparator()
            as_is = menu.addAction(tr("Play this file as it is"))
            as_is.setCheckable(True)
            as_is.setChecked(clicked.play_original)
            as_is.setEnabled(self.smooth_previews)
            as_is.setToolTip(self.as_is_action.toolTip())
            as_is.toggled.connect(lambda on, item=clicked: self.set_play_original(item, on))
        menu.exec(self.media_list.mapToGlobal(point))

    def _item_text(self, item: MediaItem) -> str:
        if item.error:
            # no summary of settings it can't be exported with
            return f"{isolate(item.path.name)}\n{item.error}"
        if item.loading or item.info is None:
            meta = tr("Loading…")
        else:
            info = item.info
            size = f"{info.width}×{info.height}" if info.has_video else tr("audio only")
            meta = join_parts([format_time(info.duration or 0), size])
        return (
            f"{isolate(item.path.name)}\n{meta}\n"
            f"{describe(item.settings, item.info.duration if item.info else None)}")

    def _show_item_text(self, item: MediaItem) -> None:
        # The summary line is often cut short in the list, so the tooltip repeats it in full.
        text = self._item_text(item)
        item.list_item.setText(text)
        item.list_item.setToolTip(f"{item.path}\n{text.splitlines()[-1]}")

    def _thumb_folder(self, item: MediaItem) -> Path:
        """Where `item`'s thumbnails are kept: a folder of its own, named by a number that is never
        reused."""
        return self.tmpdir / f"thumbs-{item.serial}"

    def _load_item(self, item: MediaItem) -> None:
        """Background thread: read the file's details, thumbnails and waveform. However it goes,
        the window is told (with an error to show when the file couldn't be read), so a file is
        never left on "Loading…"."""
        # the window closed, or the file was removed while it waited
        if self.closed or item not in self.items:
            return
        info, thumbs, wave = MediaInfo(duration=None), [], []
        try:
            info = probe_file(self.ffmpeg, item.path, self.procs)
            if info.has_video and info.duration:
                thumbs = self._optionally(
                    extract_thumbnails, self.ffmpeg, item.path, self._thumb_folder(item),
                    info.duration, registry=self.procs) or []
            if info.has_audio:
                wave = self._optionally(
                    extract_waveform, self.ffmpeg, item.path, info.duration,
                    registry=self.procs) or []
        except ConversionCancelled:  # the window is closing
            self._remove_tmpdir_if_closed()
            return
        except ReadTimeout as exc:
            info, thumbs, wave = MediaInfo(duration=None), [], []
            item.error = str(exc)
        except Exception as exc:  # whatever it was, the file must not stay on "Loading…"
            log.error("Couldn't read %s", item.path, exc_info=True)
            info, thumbs, wave = MediaInfo(duration=None), [], []
            item.error = tr("Can't read this file ({error})", error=exc)
        try:
            if self.closed or item not in self.items:
                self._remove_folder(self._thumb_folder(item))  # removed while it was being read
            else:
                self.signals.loaded.emit(item, info, thumbs, wave)
        finally:
            self._remove_tmpdir_if_closed()

    def _optionally(self, read, *args, **kwargs):
        """Run one of the readings that only add to the picture (thumbnails, waveform): if it fails
        or takes too long the file is still fine, just shown without them."""
        try:
            return read(*args, **kwargs)
        except ConversionCancelled:
            raise
        except Exception as exc:
            log.info(
                "%s for %s didn't work (%s: %s)", getattr(read, "__name__", "reading"), args[1],
                type(exc).__name__, exc)
            return None

    def _remove_folder(self, folder: Path) -> None:
        shutil.rmtree(folder, ignore_errors=True)

    def _on_loaded(
            self, item: MediaItem, info: MediaInfo, thumbs: list[Path], wave: list[float]) -> None:
        if self.closed or item not in self.items:
            self._remove_folder(self._thumb_folder(item))
            return
        item.info, item.loading = info, False
        if not self._gpu_started:
            self._gpu_timer.start(
                GPU_SEARCH_DELAY_MS)  # a few seconds from now, not while playback begins
        if not item.error and not info.has_video and not info.has_audio:
            item.error = tr("Not a video or audio file")
        if not info.has_video and info.has_audio:
            item.settings.output = OUTPUT_AUDIO
        item.thumbs = [QPixmap(str(p)) for p in thumbs]
        item.waveform = wave
        if item.list_item is not None:
            if item.thumbs:
                item.list_item.setIcon(item.thumbs[len(item.thumbs) // 2])
            self._show_item_text(item)
        if item is self.current:
            self._begin_playback(
                item)  # the file's details are known now: the right file to play can be chosen
            self.select(item, reload=False)
