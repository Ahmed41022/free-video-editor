"""The preview: still pictures, smooth copies of big videos, player errors."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage

from ...core import ConversionCancelled
from ...i18n import tr
from ...media import extract_still
from ...proxy import needs_proxy
from .items import MediaItem

log = logging.getLogger(__name__)


class PreviewsMixin:
    """Pictures and smooth copies of the files shown in the preview."""

    def _preview_copy(self, item: MediaItem) -> Optional[Path]:
        """The small copy to play instead of `item`'s file, if there is one and it should be
        used."""
        if self.smooth_previews and item.proxy is not None and item.proxy.is_file():
            return item.proxy
        return None

    def _awaiting_copy(self, item: MediaItem) -> bool:
        """Whether `item` is a video too big (or too fast) to play as it is, whose smooth preview
        copy isn't ready yet. Playing the original meanwhile is what made the app crash inside Qt's
        decoder, so it waits."""
        return bool(
            self.smooth_previews and not item.loading and not item.error and not item.proxy_failed
            and self._preview_copy(item) is None and needs_proxy(item.info))

    def _begin_playback(self, item: MediaItem) -> None:
        """Load into the player what `item` should play: the smooth copy if there is one, nothing
        while the file's details are still being read or while its copy is made (the first picture
        is shown meanwhile), otherwise the file itself."""
        copy = self._preview_copy(item)
        if item.loading:
            self.player.load(None)
        elif copy is not None:
            self.player.load(copy, upright=True)
        elif self._awaiting_copy(item):
            self.player.load(None)
            self._show_still(item)
        else:
            self.player.load(item.path)

    def _switch_player(self, path: Path, upright: bool) -> None:
        """Play `path` from here on: carrying on from the same moment, or from the start if nothing
        was loaded."""
        if self.player.source is None:
            self.player.load(path, upright=upright)
        else:
            self.player.swap_source(path, upright=upright)

    def _show_still(self, item: MediaItem) -> None:
        """Show the first picture of a video whose smooth copy is being made (read in the
        background)."""
        target = self._thumb_folder(item) / "still.png"

        def work() -> None:
            image = None
            try:
                if not self.closed and extract_still(self.ffmpeg, item.path, target, 0.0,
                                                     registry=self.procs):
                    image = QImage(str(target))
                    target.unlink(missing_ok=True)
            except ConversionCancelled:
                pass
            except Exception:
                log.info("Couldn't read the first picture of %s", item.path, exc_info=True)
            if not self.closed and image is not None and not image.isNull():
                self.signals.stillReady.emit(item, image)
            self._remove_tmpdir_if_closed()

        try:
            self.loader.submit(work)
        except RuntimeError:  # the loader is already shut down
            pass

    def _on_still_ready(self, item: MediaItem, image: QImage) -> None:
        if self.closed or item is not self.current or self.player.source is not None or self.view.raw is not None:
            return
        self.view.set_frame(image, True)  # ffmpeg turned it upright already

    def _prepare_preview_copy(self, item: MediaItem) -> None:
        """Start making a smooth preview copy of `item` if it is a big video and has none yet."""
        if (self.closed or not self.smooth_previews or item.loading or item.error
                or item.proxy is not None or item.proxy_failed or item.proxy_progress is not None
                or not needs_proxy(item.info)):
            self._update_preview_status()
            return
        item.proxy_progress = 0.0
        if not self.proxies.request(item, item.path, item.info):
            item.proxy_progress = None
        self._update_preview_status()

    def _update_preview_status(self) -> None:
        item = self.current
        progress = item.proxy_progress if item is not None else None
        self.preview_status.setText(
            tr("Preparing a smooth preview… {percent:.0%}", percent=progress
               ) if progress is not None else "")

    def _on_proxy_progress(self, item: MediaItem, fraction: float) -> None:
        if item in self.items and item.proxy_progress is not None:
            item.proxy_progress = fraction
            if item is self.current:
                self._update_preview_status()

    def _on_proxy_done(self, item: MediaItem, path: Optional[Path], error: str) -> None:
        item.proxy_progress = None
        if self.closed or item not in self.items:
            if path is not None:
                self._delete_soon(path)
            return
        if path is None:
            item.proxy_failed = True
            if item is self.current:
                # the original was held back for the copy: play it after all
                if self.player.source is None:
                    self.player.load(item.path)
                self.statusBar().showMessage(
                    tr("Couldn't prepare a smooth preview, so the original plays instead."), 6000)
        else:
            item.proxy = path
            if item is self.current and self.smooth_previews:
                self._switch_player(path, upright=True)
                self.statusBar().showMessage(tr("The smooth preview is ready."), 4000)
        self._update_preview_status()

    def set_smooth_previews(self, on: bool) -> None:
        """The View menu's switch: play small copies of big videos, or always the originals."""
        if on == self.smooth_previews:
            return
        self.smooth_previews = on
        self.store.setValue("smoothPreviews", on)
        item = self.current
        if not on:
            self.proxies.cancel_all()
            for other in self.items:
                other.proxy_progress = None
            if item is not None and not item.loading:
                self._switch_player(item.path, upright=False)
        elif item is not None:
            copy = self._preview_copy(item)
            if copy is not None:
                self._switch_player(copy, upright=True)
            else:
                self._prepare_preview_copy(item)
        self._update_preview_status()

    def _drop_preview_copy(self, item: MediaItem) -> None:
        """Stop making (and delete) the smooth preview, and delete the thumbnails, of a file that
        was removed from the list."""
        self.proxies.cancel(item)
        item.proxy_progress = None
        if item.proxy is not None:
            if self.player.source == item.proxy:
                self.player.load(None)  # let go of the file first
            self._delete_soon(item.proxy)
            item.proxy = None
        self._remove_folder(self._thumb_folder(item))

    def _delete_soon(self, path: Path, attempts: int = 20) -> None:
        """Delete a file the player may keep open for a moment after being told to let go of it."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if attempts > 0 and not self.closed:
                QTimer.singleShot(250, lambda: self._delete_soon(path, attempts - 1))

    def _on_player_error(self, message: str) -> None:
        item = self.current
        if item is not None and item.proxy is not None and self.player.source == item.proxy:
            # The copy won't play: forget it and carry on with the original.
            item.proxy_failed = True
            item.proxy = None
            self.player.swap_source(item.path, upright=False)
            self.statusBar().showMessage(
                tr("The smooth preview wouldn't play, so the original plays instead."), 6000)
            return
        if (item is not None and self.smooth_previews and item.proxy is not None
                and item.proxy.is_file() and self.player.source == item.path):
            # The original won't play (or the decoder gave up on it) but there is a smooth copy:
            # play that.
            self.player.swap_source(item.proxy, upright=True)
            return
        self.statusBar().showMessage(tr("Can't play this file: {error}", error=message))
