"""Starts the Qt application."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from ..core import find_ffmpeg


def _icon_path() -> Path:
    # Inside the .exe, bundled files live in sys._MEIPASS; otherwise next to the package.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    return base / "assets" / "icon.ico"


def main(paths: Optional[list[str]] = None) -> int:
    if sys.platform == "win32":
        try:
            import ctypes

            # Give the window its own taskbar entry and icon instead of Python's.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VideoConverter.Editor")
        except (OSError, AttributeError):
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Video Converter")
    app.setOrganizationName("VideoConverter")
    if _icon_path().is_file():
        app.setWindowIcon(QIcon(str(_icon_path())))

    ffmpeg = find_ffmpeg()
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
    return app.exec()
