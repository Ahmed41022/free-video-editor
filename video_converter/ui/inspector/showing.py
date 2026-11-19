"""Showing a file's settings in the panel and what the export will do."""

from __future__ import annotations

from typing import Optional

from ...i18n import tr
from ...output import DEFAULT_TARGET_MB
from ...project import OUTPUT_VIDEO, PRESETS, ClipSettings
from ...video import LOOKS
from .choices import FIT_DATA, SHAPES, SHAPE_HINTS


class ShowingMixin:
    """What the panel shows: the file's settings, notes and warnings."""

    def focus_section(self, section=None) -> None:
        """Open `section` at the top of the panel and fold every other one (no section: fold them
        all). Used when a feature is chosen on the home page."""
        order = self.all_sections if section is None else [
            section, *(s for s in self.all_sections if s is not section)
        ]
        self._arrange(order)
        for other in self.all_sections:
            other.header.setChecked(other is section)
        if section is not None:
            self.ensureWidgetVisible(section, 0, 0)

    def reset_sections(self) -> None:
        """The panel as it starts: the usual order, with every section open but Text and logo."""
        self._arrange(self.all_sections)
        for section in self.all_sections:
            section.header.setChecked(section is not self.captions_section)

    def _arrange(self, order) -> None:
        for section in order:
            self.panel_layout.removeWidget(section)
        for position, section in enumerate(order):
            self.panel_layout.insertWidget(
                1 + position, section)  # after the "select a video" note

    def set_output_folder(self, text: str) -> None:
        self.folder_label.setText(text)

    def set_gpu_name(self, name: Optional[str], searching: bool = False) -> None:
        """Say which graphics card exports will use, or (name None) that there isn't one. While the
        search is still running (`searching`) the option stays off-limits."""
        self.gpu_box.setEnabled(bool(name))
        if searching:
            self.gpu_label.setText(tr("Looking for a graphics card…"))
        else:
            self.gpu_label.setText(name or tr("No supported graphics card found"))

    def set_frame_rate_hint(self, text: str) -> None:
        """Show (or, with "", hide) the note under Frame rate about a source that may not play on a
        phone."""
        self.rate_hint.setText(text)
        self.rate_hint.setVisible(bool(text) and not self._gif)

    def set_size_note(self, text: str, error: bool = False) -> None:
        """Show what fitting a file size will do to the picture, or (`error`) why it can't be done.
        "" hides it."""
        self._size_text, self._size_is_error = text, error
        self._refresh_size_note()

    def _refresh_size_note(self) -> None:
        shown = bool(self._size_text) and self._fits
        self.size_note.setText("" if self._size_is_error else self._size_text)
        self.size_note.setVisible(shown and not self._size_is_error)
        self.size_error.setText(self._size_text if self._size_is_error else "")
        self.size_error.setVisible(shown and self._size_is_error)

    def set_format_warning(self, text: str) -> None:
        """Show (or, with "", hide) a warning about the format, such as a GIF that will be big."""
        self.format_warning.setText(text)
        self.format_warning.setVisible(bool(text))

    def set_has_picture(self, has_picture: bool) -> None:
        """A file with no picture (a song) can only be saved as MP3: the Video choice is switched
        off."""
        self._has_picture = has_picture
        video_button = self.output_group.button(0)
        video_button.setEnabled(has_picture)
        video_button.setToolTip(
            "" if has_picture else tr("This file has no picture, so it can only be saved as MP3"))

    def set_has_sound(self, has_sound: bool) -> None:
        """Remove silences needs a sound track to listen to."""
        self.silence_button.setEnabled(has_sound)
        self.silence_button.setToolTip(
            tr("Find the pauses in the sound and cut them out") if has_sound else tr(
                "This file has no sound, so there are no silences to remove"))

    def set_reverse_warning(self, text: str) -> None:
        """Show (or, with "", hide) the note about how much memory playing the clip backwards
        needs."""
        self.reverse_warning.setText(text)
        self.reverse_warning.setVisible(bool(text))

    def set_presets(self, user_presets: dict[str, dict]) -> None:
        self.user_presets = user_presets
        self._updating = True
        try:
            self.preset_box.clear()
            self.preset_box.addItem(tr("Choose a preset…"), None)
            for name in PRESETS:
                self.preset_box.addItem(tr(name), ("builtin", name))
            for name in user_presets:
                self.preset_box.addItem(f"★ {name}", ("user", name))
        finally:
            self._updating = False

    def set_settings(self, settings: Optional[ClipSettings], duration: Optional[float]) -> None:
        """Show `settings` (None = nothing selected) without emitting edits."""
        self.settings = settings.copy() if settings else None
        self.duration = duration
        self.empty.setVisible(settings is None)
        for section in self.sections:
            section.setVisible(settings is not None)
        if settings is None:
            return

        self._updating = True
        try:
            edit = settings.edit
            video = settings.output == OUTPUT_VIDEO
            self.output_group.button(0 if video else 1).setChecked(True)
            self.frame_section.setVisible(video)
            self.adjust_section.setVisible(video)
            self.captions_section.setVisible(video)
            self.quality_section.setVisible(video)
            self.mute_box.setVisible(video)
            self.mp3_label.setVisible(not video)
            self.mp3_box.setVisible(not video)

            index = next(i for i, (_, aspect) in enumerate(SHAPES) if aspect == edit.aspect) \
                if any(aspect == edit.aspect for _, aspect in SHAPES) else 0
            self.shape_group.button(index).setChecked(True)
            self.shape_hint.setText(
                tr(SHAPE_HINTS[edit.aspect]) if edit.aspect in SHAPE_HINTS else "")
            self.fit_box.setCurrentIndex(max(self.fit_box.findData(edit.fit), 0))
            self.fit_box.setEnabled(edit.aspect is not None)
            if not self.zoom_slider.isSliderDown():
                self.zoom_slider.setValue(int(round(edit.zoom * 100)))
            self.zoom_slider.setEnabled(edit.crops)
            self.zoom_label.setText(f"{edit.zoom:.1f}×")
            self.mirror_box.setChecked(edit.flip)
            for name, (slider, value_label) in self.adjust_sliders.items():
                value = getattr(edit, name)
                if not slider.isSliderDown():
                    slider.setValue(round(value * 100))
                value_label.setText(f"{value:+.2f}" if name == "brightness" else f"{value:.2f}")
            look_index = list(LOOKS).index(edit.look) if edit.look in LOOKS else 0
            self.look_group.button(look_index).setChecked(True)
            self.stabilize_box.setCurrentIndex(max(self.stabilize_box.findData(edit.stabilize), 0))
            self._show_trim()
            self.speed_box.setValue(edit.speed)
            self.reverse_box.setChecked(edit.reverse)
            for name, box in self.fade_boxes.items():
                box.setValue(getattr(edit, name))
            for widget in self.fade_rows["Picture"]:
                widget.setVisible(video)
            self.captions.set_edit(edit)
            self.mute_box.setChecked(edit.mute)
            self.mp3_box.setCurrentIndex(max(self.mp3_box.findData(settings.audio_quality), 0))
            self._show_sound(settings)
            self.size_box.setCurrentIndex(max(self.size_box.findData(edit.resolution), 0))
            self.rate_box.setCurrentIndex(max(self.rate_box.findData(edit.frame_rate), 0))
            self.gpu_box.setChecked(edit.use_gpu)
            self._show_format(edit)
            self.preset_box.setCurrentIndex(0)
        finally:
            self._updating = False

    def _show_format(self, edit) -> None:
        """Show the format, the quality or file size, and the GIF options, and only the rows that
        go with them."""
        fmt = edit.output_format
        self.format_box.setCurrentIndex(max(self.format_box.findData(edit.video_format), 0))
        self.format_hint.setText(tr(fmt.hint) if fmt.hint else "")
        self.format_hint.setVisible(bool(fmt.hint))
        self._gif = fmt.codec == "gif"
        self._fits = edit.target_mb > 0 and not self._gif
        self.crf_box.setCurrentIndex(
            max(self.crf_box.findData(FIT_DATA if edit.target_mb > 0 else edit.crf), 0))
        preset = self.limit_box.findData(edit.target_mb)
        self.limit_box.setCurrentIndex(preset if preset >= 0 else self.limit_box.count() - 1)
        if edit.target_mb > 0 and preset < 0:
            self.limit_spin.setValue(
                edit.target_mb)  # a preset leaves the box as it was, for Custom… to start from
        for box, value in ((self.gif_fps_box, edit.gif_fps), (self.gif_width_box, edit.gif_width)):
            if box.findData(value) < 0:  # a value from a saved preset that the list doesn't offer
                box.addItem(f"{value} {'fps' if box is self.gif_fps_box else 'px'}", value)
            box.setCurrentIndex(box.findData(value))
        custom = self.limit_box.currentData() is None
        visible = {
            "rate": not self._gif,
            "quality": not self._gif,
            "limit": self._fits,
            "custom": self._fits and custom,
            "gif_fps": self._gif,
            "gif_width": self._gif,
            "gpu": fmt.codec in ("h264", "hevc"),
        }
        for name, shown in visible.items():
            for widget in self.quality_rows[name]:
                widget.setVisible(shown)
        if self._gif:
            self.rate_hint.hide()
        self._refresh_size_note()

    def _quality_chosen(self) -> None:
        data = self.crf_box.currentData()

        def change(s: ClipSettings) -> None:
            if data == FIT_DATA:
                s.edit.target_mb = s.edit.target_mb or DEFAULT_TARGET_MB
            else:
                s.edit.crf, s.edit.target_mb = data, 0.0

        self._edit(change, tr("File size") if data == FIT_DATA else tr("Quality"))

    def _limit_chosen(self) -> None:
        megabytes = self.limit_box.currentData()
        self._edit(
            lambda s: setattr(
                s.edit, "target_mb", megabytes
                if megabytes else round(self.limit_spin.value(), 1)), tr("File size"))
