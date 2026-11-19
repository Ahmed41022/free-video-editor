"""Dropped files, other copies of the app, closing."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from ...i18n import tr

class LifecycleMixin:
    """Dropped files, other copies of the app, and closing."""

    def watch_for_other_copies(self, instance) -> None:
        """Take the files that another start of the app hands over (see single_instance.py)."""
        self.instance = instance
        instance.pathsReceived.connect(self.open_forwarded)

    def open_forwarded(self, paths: list) -> None:
        """Another copy of the app was started (for example "Open with" on some files): add its files and come
        to the front."""
        if self.closed:
            return
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        if paths:
            self.add_paths([Path(p) for p in paths])

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.add_paths(paths)
            event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        if self.queue.busy and not self._restarting:
            answer = QMessageBox.question(self, tr("Exports still running"),
                                          tr("Some exports haven't finished. Stop them and quit?"))
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._save_preferences()
        QApplication.instance().removeEventFilter(self)
        self.queue.shutdown()
        self.player.load(None)          # lets go of the preview copy's file
        self.closed = True
        if self.instance is not None:
            self.instance.close()       # free the name at once, so a restart's new copy doesn't hand its files to this one
        self.loader.shutdown(wait=False, cancel_futures=True)
        self.procs.kill_all()           # the ffmpeg runs reading files would otherwise keep going for a long time
        self.proxies.shutdown()
        self._remove_tmpdir()
        event.accept()

    def _remove_tmpdir(self) -> None:
        """Remove the temp folder. The player lets go of a preview copy a moment after it is told to, and
        Windows won't delete a file that is still open, so keep trying briefly."""
        for _ in range(40):
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            if not self.tmpdir.exists():
                return
            QApplication.processEvents()
            time.sleep(0.05)

    def _remove_tmpdir_if_closed(self) -> None:
        """A background job still running when the window closed may have made its folder again."""
        if self.closed:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
