"""Changing the selected file's settings: from the panel, the preview and the timeline."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox

from ...i18n import ntr, tr, trc
from ...project import OUTPUT_AUDIO, ClipSettings, apply_to
from ...timing import part_index, remove_part_at, restore_part_at, split_at
from ..silence_dialog import SilenceDialog
from .commands import SettingsCommand
from .items import MediaItem


class EditingMixin:
    """Edits of the selected file's settings."""

    def apply_settings(self, item: MediaItem, settings: ClipSettings) -> None:
        """Set a file's settings and refresh whatever shows them (used by undo/redo too)."""
        item.settings = settings.copy()
        if item.info is not None and item.info.has_audio and not item.info.has_video:
            item.settings.output = OUTPUT_AUDIO  # a sound-only file has no picture to export, whatever an old undo step says
        if item.list_item is not None:
            self._show_item_text(item)
        if item is self.current:
            self._show_settings(item)

    def push_settings(self, new: ClipSettings, label: str, final: bool = True) -> None:
        item = self.current
        if item is None or new == item.settings and final and not self.merge_open:
            return
        self.undo_stack.push(
            SettingsCommand(self, item, item.settings, new, label, mergeable=True))
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
            self.push_settings(new, tr("Move crop"), final=False)

    def _overlay_picked(self, kind: str, index: int) -> None:
        if kind == "text":
            self.inspector.captions.select(index)

    def _overlay_moved(self, kind: str, index: int, x: float, y: float) -> None:
        item = self.current
        if item is None:
            return
        new = item.settings.copy()
        if kind == "text" and index < len(new.edit.texts):
            new.edit.texts[index].x, new.edit.texts[index].y = x, y
            self.push_settings(new, tr("Move text"), final=False)
        elif kind == "logo":
            new.edit.logo.corner, new.edit.logo.x, new.edit.logo.y = "custom", x, y
            self.push_settings(new, tr("Move logo"), final=False)

    def _text_selected(self, index: int) -> None:
        self.view.set_selected_overlay(("text", index) if index >= 0 else None)
        if self.current is not None:
            self._show_text_spans(self.current)

    def text_time_at_playhead(self, which: str) -> None:
        """Make the playhead the start or end of the selected text."""
        item, index = self.current, self.inspector.captions.selected
        if item is None or index < 0 or index >= len(item.settings.edit.texts):
            return
        t = self._overlay_time(item)
        if t is None:
            self.statusBar().showMessage(tr("The playhead is on a part that is cut out."), 4000)
            return
        t = round(t, 3)
        new = item.settings.copy()
        text = new.edit.texts[index]
        if which == "start":
            if text.end is not None and t >= text.end:
                self.statusBar().showMessage(tr("The text has to start before it ends."), 4000)
                return
            text.start = t
        else:
            if t <= text.start:
                self.statusBar().showMessage(tr("The text has to end after it starts."), 4000)
                return
            text.end = t
        self.push_settings(new, tr("Text time"))

    def _zoom_changed(self, zoom: float) -> None:
        if self.current:
            new = self.current.settings.copy()
            new.edit.zoom = zoom
            self.push_settings(new, tr("Zoom"), final=False)

    def _timeline_trim(self, start: Optional[float], end: Optional[float]) -> None:
        if self.current:
            new = self.current.settings.copy()
            new.edit.start, new.edit.end = start, end
            self.push_settings(new, tr("Trim"), final=False)

    def _timeline_keep(self, keep: list) -> None:
        if self.current:
            new = self.current.settings.copy()
            new.edit.keep = list(keep)
            self.push_settings(new, trc("undo", "Cut"), final=False)

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
            new_keep, label = split_at(keep, t, duration), tr("Split")
            problem = (
                tr("This moment is cut out.") if part_index(keep, t, duration) is None else
                tr("Too close to the start or end of a part to split there."))
        elif action == "remove":
            new_keep, label = remove_part_at(keep, t, duration), tr("Cut out part")
            problem = (
                tr("This moment is already cut out.") if part_index(keep, t, duration) is None else
                tr("Split the video first (S), then remove the part you don't want."))
        elif action == "restore":
            new_keep, label, problem = restore_part_at(keep, t, duration), tr("Put back part"), ""
        else:
            new_keep, label, problem = [], tr("Put back all parts"), ""
        if new_keep == keep:
            if problem:
                self.statusBar().showMessage(problem, 4000)
            return
        new = item.settings.copy()
        new.edit.keep = new_keep
        self.push_settings(new, label)

    def remove_silences(self) -> None:
        """Ask how quiet and how long a pause is, then cut the pauses out as one undoable change."""
        item = self.current
        if item is None or item.info is None or not item.info.duration:
            return
        if not item.info.has_audio:
            self.statusBar().showMessage(
                tr("This file has no sound, so there are no silences to remove."), 4000)
            return
        edit = item.settings.edit
        dialog = SilenceDialog(
            self.ffmpeg, item.path, item.info.duration, edit.keep, edit.start, edit.end, self)
        accepted = dialog.exec() == SilenceDialog.DialogCode.Accepted
        dialog.deleteLater()
        if accepted and item is self.current:
            new = item.settings.copy()
            new.edit.keep = dialog.keep
            self.push_settings(new, tr("Remove silences"))

    def _timeline_menu(self, t: float, pos) -> None:
        item = self.current
        if item is None or item.info is None or not item.info.duration:
            return
        keep, duration = item.settings.edit.keep, item.info.duration
        menu = QMenu(self)
        if part_index(keep, t, duration) is None:
            menu.addAction(tr("Put back this part"), lambda: self.cut_at("restore", t))
        else:
            split = menu.addAction(tr("Split here"), lambda: self.cut_at("split", t))
            split.setEnabled(split_at(keep, t, duration) != keep)
            remove = menu.addAction(tr("Cut out this part"), lambda: self.cut_at("remove", t))
            remove.setEnabled(remove_part_at(keep, t, duration) != keep)
        menu.addSeparator()
        restore = menu.addAction(tr("Put back all cut parts"), lambda: self.cut_at("restore_all"))
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
                self.statusBar().showMessage(
                    tr("The start has to be before the end of the trim."), 4000)
                return
            new.edit.start = t if t > 0.05 else None
        else:
            if t <= (start or 0.0):
                self.statusBar().showMessage(
                    tr("The end has to be after the start of the trim."), 4000)
                return
            new.edit.end = t if t < item.info.duration - 0.05 else None
        self.push_settings(new, tr("Trim"))

    def apply_to_all(self) -> None:
        source = self.current
        if source is None or len(self.items) < 2:
            self.statusBar().showMessage(
                tr("Add more files to copy these settings to them."), 4000)
            return
        self.undo_stack.beginMacro(tr("Apply to all files"))
        for item in self.items:
            if item is not source:
                new = apply_to(item.settings, source.settings)
                if item.info is not None and not item.info.has_video:
                    new.output = OUTPUT_AUDIO
                self.undo_stack.push(
                    SettingsCommand(
                        self, item, item.settings, new, tr("Apply to all"), False, reveal=False))
        self.undo_stack.endMacro()
        self.statusBar().showMessage(
            ntr(
                len(self.items) - 1, "Copied the settings to {n} other file.",
                "Copied the settings to {n} other files."), 4000)

    def save_preset(self) -> None:
        if self.current is None:
            return
        name, ok = QInputDialog.getText(self, tr("Save preset"), tr("Name for these settings:"))
        name = name.strip()
        if ok and name:
            if name in self.user_presets and QMessageBox.question(
                    self, tr("Save preset"), tr(
                        "A preset called “{name}” already exists. Replace it?",
                        name=name), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            self.user_presets[name] = self.current.settings.to_dict()
            self.inspector.set_presets(self.user_presets)
            self._save_preferences()
            self.statusBar().showMessage(tr("Saved the preset “{name}”.", name=name), 4000)
