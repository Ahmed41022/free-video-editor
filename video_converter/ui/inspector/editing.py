"""Turning what the user does in the panel into changes of the settings."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QSlider, QWidget

from ...core import AUDIO_EXTENSIONS, format_time, parse_time
from ...i18n import join_parts, tr
from ...project import OUTPUT_AUDIO, OUTPUT_VIDEO, PRESETS, ClipSettings
from ...timing import output_duration, removed_parts
from ...video import ADJUSTMENTS
from .widgets import _db_text, _short_name


class EditingMixin:
    """What the panel does with the user's changes."""

    def _edit(
            self, change: Callable[[ClipSettings], None], label: str, final: bool = True) -> None:
        if self._updating or self.settings is None:
            return
        new = self.settings.copy()
        change(new)
        # A finished slider drag still reports once, so listeners know the edit is complete.
        if new == self.settings and final and not self._dragging:
            return
        self._dragging = not final
        self.settings = new
        self.settingsEdited.emit(new.copy(), label, final)

    @staticmethod
    def _center(s: ClipSettings) -> None:
        s.edit.pos_x = s.edit.pos_y = 0.5

    @staticmethod
    def _reset_picture(s: ClipSettings) -> None:
        for name, (_, _, neutral) in ADJUSTMENTS.items():
            setattr(s.edit, name, neutral)
        s.edit.look = "none"

    @staticmethod
    def _clear_trim(s: ClipSettings) -> None:
        s.edit.start = s.edit.end = None

    @staticmethod
    def _clear_cuts(s: ClipSettings) -> None:
        s.edit.keep = []

    def _show_sound(self, settings: ClipSettings) -> None:
        edit = settings.edit
        muted = settings.output == OUTPUT_VIDEO and edit.mute  # Mute wins over everything below
        if not self.volume_slider.isSliderDown():
            self.volume_slider.setValue(round(edit.volume))
        self.volume_label.setText(_db_text(round(edit.volume)))
        self.normalize_box.setChecked(edit.normalize)
        self.noise_box.setCurrentIndex(max(self.noise_box.findData(edit.audio_denoise), 0))
        if not self.music_slider.isSliderDown():
            self.music_slider.setValue(round(edit.music_volume))
        self.music_label.setText(_db_text(round(edit.music_volume)))
        self.duck_box.setChecked(edit.music_duck)
        name = Path(edit.music).name if edit.music else ""
        self.music_name.setText(_short_name(name) if name else tr("None"))
        self.music_name.setToolTip(edit.music)
        self.music_remove.setEnabled(bool(edit.music))
        self.music_missing.setVisible(bool(edit.music) and not Path(edit.music).is_file())
        for widget in self.sound_options:
            widget.setEnabled(not muted)
        for widget in (self.music_holder, self.duck_box):
            widget.setEnabled(not muted and bool(edit.music))

    @staticmethod
    def _slider_row(slider: QSlider, label: QLabel) -> QWidget:
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(slider)
        line.addWidget(label)
        holder = QWidget()
        holder.setLayout(line)
        return holder

    def _choose_music(self) -> None:
        current = self.settings.edit.music if self.settings else ""
        patterns = " ".join(f"*{extension}" for extension in AUDIO_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Choose background music"),
            str(Path(current).parent) if current else "",
            tr("Sound files ({patterns})", patterns=patterns) + ";;" + tr("All files (*)"))
        if path:
            self._edit(lambda s: setattr(s.edit, "music", str(Path(path))), tr("Choose music"))

    def _show_trim(self) -> None:
        edit = self.settings.edit
        self.start_edit.setText(format_time(edit.start) if edit.start is not None else "")
        self.end_edit.setText(format_time(edit.end) if edit.end is not None else "")
        if self.duration:
            length = output_duration(edit.segments(self.duration), edit.speed) or 0.0
            self.trim_info.setText(
                tr(
                    "Result {length} of {total}", length=format_time(length),
                    total=format_time(self.duration)))
        else:
            self.trim_info.setText("")
        gaps = removed_parts(edit.keep, self.duration)
        if gaps:
            # Each range is isolated, so "0:10.0–0:12.0" keeps its order inside an Arabic sentence.
            shown = join_parts(
                [f"{format_time(a)}–{format_time(b)}" for a, b in gaps[:4]], tr(", "))
            more = " " + tr("and {n} more", n=len(gaps) - 4) if len(gaps) > 4 else ""
            self.cut_info.setText(tr("Cut out: {parts}{more}", parts=shown, more=more))
        elif edit.keep:
            self.cut_info.setText(
                tr("Split. Put the playhead on a part and click Remove part to cut it out."))
        else:
            self.cut_info.setText(
                tr("Split at the playhead, then remove the parts you don't want."))
        self.restore_button.setEnabled(bool(edit.keep))

    def _trim_typed(self, which: str) -> None:
        if self._updating or self.settings is None:
            return
        widget = self.start_edit if which == "start" else self.end_edit
        current = getattr(self.settings.edit, which)
        if widget.text().strip() == (format_time(current) if current is not None else ""):
            # Leaving the field untouched must not round a time set elsewhere to the one decimal shown.
            self.trim_error.hide()
            return
        try:
            value = parse_time(widget.text())
        except ValueError as exc:
            self.trim_error.setText(str(exc))
            self.trim_error.show()
            return
        start = value if which == "start" else self.settings.edit.start
        end = value if which == "end" else self.settings.edit.end
        if self.duration and value is not None and value > self.duration:
            self.trim_error.setText(
                tr("The video is only {length} long.", length=format_time(self.duration)))
            self.trim_error.show()
            return
        if start is not None and end is not None and end <= start:
            self.trim_error.setText(tr("The end has to be after the start."))
            self.trim_error.show()
            return
        self.trim_error.hide()
        self._edit(lambda s: setattr(s.edit, which, value), tr("Trim"))

    def _preset_chosen(self, index: int) -> None:
        data = self.preset_box.itemData(index)
        if self._updating or data is None or self.settings is None:
            return
        kind, name = data
        from ...project import apply_preset, apply_to

        if kind == "builtin":
            new = apply_preset(self.settings, PRESETS[name], self._has_picture)
        else:
            new = apply_to(self.settings, ClipSettings.from_dict(self.user_presets[name]))
            if not self._has_picture:
                new.output = OUTPUT_AUDIO
        self.settings = new
        self.settingsEdited.emit(
            new.copy(), tr("Preset: {name}", name=tr(name) if kind == "builtin" else name), True)
        self._updating = True
        self.preset_box.setCurrentIndex(0)
        self._updating = False
