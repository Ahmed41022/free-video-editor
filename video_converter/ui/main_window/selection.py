"""Choosing a file: showing its settings, overlays and export state."""

from __future__ import annotations

from typing import Optional

from ... import APP_NAME, __version__
from ...captions import Logo
from ...core import format_time
from ...i18n import isolate, tr
from ...project import OUTPUT_VIDEO
from ...timing import output_spans_to_source, source_to_output
from ...video import (
    fit_summary, format_warning, frame_rate_hint, plan_geometry, reverse_memory, reverse_warning,
)
from .items import MediaItem


class SelectionMixin:
    """What the window shows for the selected file."""

    def select(self, item: Optional[MediaItem], reload: bool = True) -> None:
        changed = item is not self.current
        self.current = item
        has_file = item is not None
        for widget in (self.back_button, self.play_button, self.forward_button, self.in_button,
                       self.out_button, self.split_button, self.cut_button, self.compare_button):
            widget.setEnabled(has_file)
        self._show_export_state(item)
        self.remove_action.setEnabled(has_file)
        self.frame_action.setEnabled(False)  # until the file's details say it has a picture
        self._update_list_actions()
        self._sync_as_is_action()
        if changed:
            # Don't keep a queue of big copies for files the user has already moved on from. A copy
            # that has started is only paused (see _apply_copy_priorities): it carries on later.
            for other in self.proxies.cancel_waiting(keep=item):
                other.proxy_progress = None
        if item is None:
            self._apply_copy_priorities()
            self.player.load(None)
            self._update_preview_status()
            self.view.clear(tr("Drop videos here, or click Add files"))
            self.timeline.clear()
            self.inspector.set_settings(None, None)
            self.inspector.set_reverse_warning("")
            self.inspector.set_frame_rate_hint("")
            self.setWindowTitle(f"{APP_NAME} {__version__}")
            return
        self.setWindowTitle(f"{item.path.name} · {APP_NAME} {__version__}")
        if changed or reload:
            self.view.clear(tr("Loading…"))
            self.compare_button.setChecked(False)
            self._begin_playback(item)
        info = item.info
        self.timeline.setDuration(info.duration or 0.0 if info else 0.0)
        self.time_label.setText(
            f"{format_time(self.player.position)} / "
            f"{format_time(info.duration or 0 if info else 0)}")
        self.timeline.setThumbnails(item.thumbs)
        self.timeline.setWaveform(item.waveform, loading=item.loading)
        if info is not None and not info.has_video:
            self.view.clear(item.error or tr("Audio only: there's no picture to preview."))
        self._show_settings(item)
        self._prepare_preview_copy(item)

    def _show_export_state(self, item: Optional[MediaItem]) -> None:
        """Export is off for no file and for a file that couldn't be read, and its tooltip says
        why."""
        broken = item is not None and bool(item.error)
        self.export_button.setEnabled(item is not None and not broken)
        self.export_button.setToolTip(
            tr("Can't export {name}: {error}", name=isolate(item.path.name), error=item.error
               ) if broken else tr("Export the selected file (Ctrl+E)"))

    def _show_settings(self, item: MediaItem) -> None:
        settings, info = item.settings, item.info
        self.inspector.set_settings(settings, info.duration if info else None)
        self.inspector.set_has_picture(info is None or info.has_video or not info.has_audio)
        edit = settings.edit
        video_out = settings.output == OUTPUT_VIDEO
        self.inspector.set_reverse_warning(
            reverse_warning(reverse_memory(edit, info, sound_only=not video_out)) if info else "")
        self.inspector.set_has_sound(bool(info and info.has_audio))
        self.inspector.set_frame_rate_hint(
            frame_rate_hint(info) if info and edit.frame_rate is None else "")
        has_picture = bool(info and info.has_video)
        self.frame_action.setEnabled(has_picture)
        self.inspector.set_size_note(
            *(fit_summary(edit, info) if has_picture and video_out else ("", False)))
        self.inspector.set_format_warning(
            format_warning(edit, info) if has_picture and video_out else "")
        self.timeline.setTrim(edit.start, edit.end)
        self.timeline.setKeep(edit.keep)
        self.player.set_segments(edit.segments(info.duration if info else None))
        self.player.set_rate(edit.speed)
        self.compare_button.setEnabled(video_out and bool(info and info.has_video))
        if info is None or not info.has_video:
            self.view.set_crop(None, 1.0)
            self.view.set_overlays([], Logo(), None, None)
            self.timeline.setTexts([])
            return
        self.view.set_orientation(
            info.display_rotation, edit.rotate if video_out else 0, edit.flip and video_out)
        if video_out and edit.crops:
            geometry = plan_geometry(info.width, info.height, edit)
            self.view.set_crop((geometry.width, geometry.height, *geometry.crop), edit.zoom)
        else:
            self.view.set_crop(None, edit.zoom)
        self._show_overlays(item)
        if self.compare_button.isChecked():
            self._after_timer.start()

    def _overlay_time(self, item: MediaItem) -> Optional[float]:
        """Where the playhead is on the export's timeline, or None when it is on a part that is cut
        out."""
        edit, info = item.settings.edit, item.info
        if info is None or not info.duration:
            return None
        return source_to_output(
            self.player.position, edit.segments(info.duration), edit.speed, edit.reverse)

    def _show_overlays(self, item: MediaItem) -> None:
        """Hand the texts and the logo to the preview, and the texts' times to the timeline."""
        edit, info = item.settings.edit, item.info
        if item.settings.output != OUTPUT_VIDEO:
            self.view.set_overlays([], Logo(), None, None)
            self.timeline.setTexts([])
            return
        geometry = plan_geometry(info.width, info.height, edit)
        # In the fit modes the export's frame is bigger than the picture, so the preview shows the
        # frame.
        self.view.set_overlays(
            edit.texts, edit.logo, (geometry.out_width, geometry.out_height),
            self._overlay_time(item), canvas=not edit.crops and bool(edit.texts or edit.logo.file))
        if self.view.selected is None or self.view.selected[0] == "text":
            chosen = self.inspector.captions.selected
            self.view.set_selected_overlay(
                ("text", chosen) if 0 <= chosen < len(edit.texts) else None)
        self._show_text_spans(item)

    def _show_text_spans(self, item: MediaItem) -> None:
        edit, info = item.settings.edit, item.info
        segments = edit.segments(info.duration) if info and info.duration else []
        spans = [
            output_spans_to_source(t.start, t.end, segments, edit.speed, edit.reverse)
            if segments else [] for t in edit.texts
        ]
        self.timeline.setTexts(spans, self.inspector.captions.selected)
