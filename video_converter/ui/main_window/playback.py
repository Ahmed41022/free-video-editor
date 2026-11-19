"""Playback position and the before/after comparison."""

from __future__ import annotations

import threading

from PySide6.QtGui import QImage

from ...core import format_time
from ...i18n import tr
from ...video import render_frame
from .. import icons


class PlaybackMixin:
    """Playback and before/after."""

    def _on_position(self, seconds: float) -> None:
        self.timeline.setPosition(seconds)
        if self.current is not None and self.current.settings.edit.texts:
            self.view.set_overlay_time(self._overlay_time(self.current))
        duration = self.current.info.duration if self.current and self.current.info else None
        self.time_label.setText(f"{format_time(seconds)} / {format_time(duration or 0)}")
        if self.compare_button.isChecked() and not self.player.playing:
            self._after_timer.start()

    def _on_duration(self, seconds: float) -> None:
        if self.current and (self.current.info is None or not self.current.info.duration):
            self.timeline.setDuration(seconds)

    def _on_playing(self, playing: bool) -> None:
        self.play_button.setIcon(icons.icon("pause" if playing else "play"))
        if playing and self.compare_button.isChecked():
            self.compare_button.setChecked(False)
            self._compare_toggled(False)

    def _preview_mute_toggled(self, muted: bool) -> None:
        self.player.set_muted(muted)
        self.mute_button.setIcon(icons.icon("mute" if muted else "volume"))

    def _compare_toggled(self, on: bool) -> None:
        if on:
            self.player.pause()
            self.view.set_after(None, True)
            self._after_timer.start(0)
        else:
            self.view.set_after(None, False)

    def _render_after(self) -> None:
        item = self.current
        if (self.closed or item is None or item.info is None or not item.info.has_video
                or not self.compare_button.isChecked()):
            return
        if self._after_busy:
            # One render at a time: when it finishes, the newest request (this one, or a later one)
            # is rendered.
            self._after_again = True
            return
        self._after_token += 1
        token, settings, at = self._after_token, item.settings.copy(), self.player.position
        ratio = self.view.devicePixelRatioF()
        size = (
            int(min(self.view.width() * ratio, 1600)), int(min(self.view.height() * ratio, 1600)))
        target = self.tmpdir / f"after_{token}.png"
        self._after_busy, self._after_again = True, False

        def work() -> None:
            try:
                render_frame(self.ffmpeg, item.path, target, at, size, settings.edit, item.info)
                image = QImage(str(target))
                target.unlink(missing_ok=True)
                result = image
            # whatever happens, the window must hear back or it never renders again
            except Exception as exc:
                result = str(exc)
            if not self.closed:
                self.signals.afterReady.emit(token, result)
            self._remove_tmpdir_if_closed()

        threading.Thread(target=work, name="render-after", daemon=True).start()

    def _on_after_ready(self, token: int, result) -> None:
        self._after_busy = False
        if self._after_again and not self.closed:
            self._after_again = False
            self._render_after()
            return
        if token != self._after_token or not self.compare_button.isChecked():
            return
        if isinstance(result, QImage) and not result.isNull():
            self.view.set_after(result, True)
        else:
            self.statusBar().showMessage(tr("Couldn't render the preview."), 4000)
