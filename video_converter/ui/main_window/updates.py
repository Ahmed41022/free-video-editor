"""Looking for a newer version, and the banner that tells about it."""

from __future__ import annotations

import threading

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ... import APP_NAME, updates
from ...i18n import tr


class UpdatesMixin:
    """Checking for a newer version."""

    def _build_update_banner(self) -> None:
        """A small bar in the status line that appears when a newer version is out. It never blocks
        anything."""
        self.update_banner = QWidget()
        row = QHBoxLayout(self.update_banner)
        row.setContentsMargins(6, 0, 6, 0)
        row.setSpacing(6)
        self.update_label = QLabel()
        self.update_download = QPushButton(tr("Download"))
        self.update_skip = QPushButton(tr("Not now"))
        for button in (self.update_download, self.update_skip):
            button.setObjectName("chip")
            button.setMinimumWidth(0)
        self.update_download.setToolTip(tr("Open the download page in your browser"))
        self.update_skip.setToolTip(tr("Don't mention this version again"))
        self.update_download.clicked.connect(self._download_update)
        self.update_skip.clicked.connect(self._skip_update)
        for widget in (self.update_label, self.update_download, self.update_skip):
            row.addWidget(widget)
        self.statusBar().addPermanentWidget(self.update_banner)
        self.update_banner.hide()

    def set_check_updates(self, on: bool) -> None:
        """The Help menu's switch: look for a newer version once a day, or never."""
        if on == self.check_updates:
            return
        self.check_updates = on
        self.store.setValue(updates.ENABLED_KEY, on)
        if not on:
            self.update_banner.hide()

    def start_update_check(self, fetch=None) -> None:
        """Look for a newer version in the background (at most once a day, and never if the Help
        menu's switch is off). Whatever goes wrong is only logged. `fetch` stands in for the
        request in tests."""
        if not self.check_updates or self.closed:
            return
        last = self.store.value(updates.LAST_CHECK_KEY, 0.0)
        skipped = self._saved_text(updates.SKIPPED_KEY)
        try:
            last = float(last)
        except (TypeError, ValueError):
            last = 0.0
        # The check works on a copy of the settings: only this thread touches the real ones.
        copy = updates.MemoryStore(
            {
                updates.ENABLED_KEY: True,
                updates.LAST_CHECK_KEY: last,
                updates.SKIPPED_KEY: skipped
            })

        def work() -> None:
            release = updates.check_for_update(copy, fetch)
            made = copy.values[updates.LAST_CHECK_KEY]
            self.signals.updateChecked.emit(made if made != last else None, release)

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _on_update_checked(self, made, release) -> None:
        if self.closed:
            return
        if made is not None:
            self.store.setValue(updates.LAST_CHECK_KEY, made)
        if release is not None and self.check_updates:
            self.update_release = release
            self.update_label.setText(
                tr("{app} {version} is available", app=APP_NAME, version=release.version))
            self.update_banner.show()

    def _download_update(self) -> None:
        if self.update_release is not None:
            QDesktopServices.openUrl(QUrl(self.update_release.url))
        self.update_banner.hide()

    def _skip_update(self) -> None:
        if self.update_release is not None:
            self.store.setValue(updates.SKIPPED_KEY, self.update_release.version)
        self.update_banner.hide()
