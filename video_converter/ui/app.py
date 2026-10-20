"""Starts the Qt application."""

from __future__ import annotations

import faulthandler
import sys
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import APP_NAME, diagnostics
from ..core import find_ffmpeg

CRASH_DIALOG_GAP = 10.0       # seconds; a burst of errors shows one box, not a hundred


def _icon_path() -> Path:
    # Inside the .exe, bundled files live in sys._MEIPASS; otherwise next to the package.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    return base / "assets" / "icon.ico"


class CrashReporter(QObject):
    """Tells the user, once, when something unexpected goes wrong. `report` can be called from any
    thread; the box appears on the UI thread."""

    crashed = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.crashed.connect(self._show)
        self._last_shown = float("-inf")
        self.showing = False

    def report(self, summary: str) -> None:
        self.crashed.emit(summary)

    def _show(self, summary: str) -> None:
        now = time.monotonic()
        if self.showing or now - self._last_shown < CRASH_DIALOG_GAP:
            return
        self.showing = True
        try:
            box = crash_box(summary)
            box.exec()
        finally:
            self.showing = False
            self._last_shown = time.monotonic()


def crash_box(summary: str, parent=None) -> QMessageBox:
    """The friendly box for an unexpected error (not shown yet)."""
    box = QMessageBox(QMessageBox.Icon.Warning, APP_NAME, "Something unexpected went wrong.",
                      QMessageBox.StandardButton.Close, parent)
    box.setInformativeText(
        "It has been written to the log, so it can be looked at. You can carry on, but if the app starts "
        "behaving oddly, save your work and restart it.\n\n"
        f"The log is here:\n{diagnostics.log_path()}\n\n"
        "To tell the developer, use Help > Report a problem.")
    box.setDetailedText(summary)
    return box


CAPTIONS_SELF_TEST = "--self-test-captions"


def captions_self_test(report: Optional[str] = None) -> int:
    """Check that the speech engine behind automatic captions works in this build, without opening a window:
    0 if it does. The one-line result (or the error) is written to the file `report`, since the built program
    has no console to print to."""
    try:
        from ..autocaptions import self_test

        text, code = self_test(), 0
    except Exception:
        import traceback

        text, code = "FAILED\n" + traceback.format_exc(), 1
    if report:
        Path(report).write_text(text + "\n", encoding="utf-8")
    return code


def main(paths: Optional[list[str]] = None) -> int:
    if paths and paths[0] == CAPTIONS_SELF_TEST:
        return captions_self_test(paths[1] if len(paths) > 1 else None)
    if sys.platform == "win32":
        try:
            import ctypes

            # Give the window its own taskbar entry and icon instead of Python's.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VideoConverter.Editor")
        except (OSError, AttributeError):
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("VideoConverter")
    if _icon_path().is_file():
        app.setWindowIcon(QIcon(str(_icon_path())))

    try:
        log_file = diagnostics.setup_logging()
        # A crash in Qt's or ffmpeg's own code can't be caught; this at least notes where it happened.
        faulthandler.enable(open(log_file.with_name("crashes.log"), "a", encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        pass       # no log is better than no app
    reporter = CrashReporter(app)
    diagnostics.install_excepthooks(reporter.report)

    ffmpeg = find_ffmpeg()
    diagnostics.log_startup(ffmpeg)
    if not ffmpeg:
        QMessageBox.critical(None, "ffmpeg not found",
                             "This app needs ffmpeg to work with videos.\n\n"
                             "Install it (for example: winget install Gyan.FFmpeg) or run\n"
                             "    pip install imageio-ffmpeg\n"
                             "and start the app again.")
        return 1

    from .main_window import MainWindow

    window = MainWindow(ffmpeg)
    window.show()
    if paths:
        window.add_paths([Path(p) for p in paths])
    window.start_update_check()
    return app.exec()
