"""Making and editing captions."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from ... import APP_NAME
from ...autocaptions import captions_file_for, save_captions, save_captions_anywhere
from ...captions import encode_srt, parse_cues, read_text_with_encoding, subtitle_kind
from ...core import ConversionError
from ...i18n import isolate, ntr, tr
from ..auto_captions_dialog import AutoCaptionsDialog
from ..captions_editor import CaptionsEditor
from .commands import CaptionsFileCommand, SettingsCommand
from .items import MediaItem


class CaptionsMixin:
    """Making and editing the captions of a file."""

    def make_captions(self) -> None:
        """Ask how to listen, make captions from the video's speech, save them as an .srt next to
        the video and burn them in: one undoable change."""
        item = self.current
        if item is None or item.info is None:
            return
        if not item.info.has_audio:
            self.statusBar().showMessage(
                tr("This file has no sound, so there are no captions to make."), 4000)
            return
        edit = item.settings.edit
        dialog = AutoCaptionsDialog(
            self.ffmpeg, item.path, item.info.duration, edit.start, edit.end, self.store, self,
            models_root=self.models_root)
        accepted = dialog.exec() == AutoCaptionsDialog.DialogCode.Accepted
        outcome = dialog.outcome
        dialog.deleteLater()
        if not accepted or outcome is None or item not in self.items:
            return
        try:
            # Finished work is never thrown away: if the folder of the video won't take the file,
            # another folder does.
            path, moved = save_captions_anywhere(outcome.cues, item.path, save=save_captions)
        except ConversionError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        new = item.settings.copy()
        new.edit.subtitles.file = str(path)
        self.undo_stack.push(
            SettingsCommand(self, item, item.settings, new, tr("Make captions"), mergeable=False))
        self.merge_open = False
        count = len(outcome.cues)
        message = ntr(
            count, "Added {n} caption, saved as {name}. Use Edit captions to fix any words.",
            "Added {n} captions, saved as {name}. Use Edit captions to fix any words.",
            name=isolate(path.name))
        if moved:
            message = tr(
                "The captions couldn't be saved next to the video, so they were saved here "
                "instead: "
                "{path}", path=isolate(str(path))) + " " + message
        self.statusBar().showMessage(message, 15000 if moved else 8000)

    def edit_captions(self) -> None:
        """Open the subtitle file's captions in a table; saving rewrites the file as one undoable
        change."""
        item = self.current
        if item is None:
            return
        path = Path(item.settings.edit.subtitles.file or "")
        try:
            before = path.read_bytes()
            text, _encoding = read_text_with_encoding(path)
            cues = parse_cues(text, subtitle_kind(path, text))
        except OSError:
            self.statusBar().showMessage(tr("The subtitle file can't be read."), 4000)
            return
        dialog = CaptionsEditor(cues, self)
        # times past the end are refused
        dialog.duration = item.info.duration if item.info and item.info.duration else None
        accepted = dialog.exec() == CaptionsEditor.DialogCode.Accepted
        edited = dialog.cues
        dialog.deleteLater()
        if not accepted or item not in self.items:
            return
        if [(c.start, c.end, c.text) for c in edited] == [(c.start, c.end, c.text) for c in cues]:
            return
        data = encode_srt(edited)
        if path.suffix.lower() == ".srt":
            self.undo_stack.push(
                CaptionsFileCommand(self, item, path, before, data, tr("Edit captions")))
        else:
            # Only .srt is written; a WebVTT file is left alone and the edited captions go to a new
            # .srt.
            try:
                new_path = captions_file_for(path)
                new_path.write_bytes(data)
            except OSError as exc:
                QMessageBox.warning(
                    self, APP_NAME,
                    tr("Couldn't save the captions: {reason}", reason=exc.strerror or exc))
                return
            new = item.settings.copy()
            new.edit.subtitles.file = str(new_path)
            self.undo_stack.push(
                SettingsCommand(
                    self, item, item.settings, new, tr("Edit captions"), mergeable=False))
            self.statusBar().showMessage(
                tr(
                    "The {kind} file was left as it is; the edited captions are in a new file, "
                    "{name}.", kind=isolate(path.suffix.lower()), name=isolate(new_path.name)),
                10000)
        self.merge_open = False

    def captions_file_changed(self, item: MediaItem) -> None:
        """The text of the item's subtitle file was rewritten (or restored): refresh what shows
        it."""
        if item is self.current:
            self._show_settings(item)
