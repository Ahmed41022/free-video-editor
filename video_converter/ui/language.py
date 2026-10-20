"""Puts the chosen language into the Qt application: the layout direction, the number format, fonts that can
show Arabic, Qt's own texts (the standard buttons), and restarting the app after the language was changed."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QCoreApplication, QLibraryInfo, QLocale, QProcess, QProcessEnvironment, Qt, QTranslator,
)
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .. import i18n

log = logging.getLogger(__name__)

# Segoe UI shows Arabic on every Windows; the others are fallbacks for letters it lacks and for odd systems.
FONT_FAMILIES = ["Segoe UI", "Tahoma", "Arial", "Noto Sans Arabic", "Noto Naskh Arabic"]

_translator: Optional[QTranslator] = None
_original_font: Optional[QFont] = None
_original_locale: Optional[QLocale] = None


def number_locale(code: str) -> QLocale:
    """A locale for the language that writes numbers with the digits 0-9: Qt's Arabic locales may use
    Arabic-Indic digits, and times, sizes and counts here are always written with Western ones."""
    if code == "ar":
        for name in ("ar_AE", "ar_MA"):
            locale = QLocale(name)
            if locale.toString(1234) in ("1234", "1,234"):
                return locale
    return QLocale(QLocale.Language.English, QLocale.Country.UnitedStates)


def apply_to_app(app: Optional[QApplication] = None) -> None:
    """Make `app` follow `i18n.language()`. Call it after `i18n.set_language`, before the window is made."""
    global _translator, _original_font, _original_locale
    app = app or QApplication.instance()
    if app is None:
        return
    code = i18n.language()
    if _original_font is None:
        _original_font, _original_locale = QFont(app.font()), QLocale()
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft if i18n.is_rtl() else Qt.LayoutDirection.LeftToRight)
    if _translator is not None:
        QCoreApplication.removeTranslator(_translator)
        _translator = None
    if code == "en":
        QLocale.setDefault(_original_locale)
        app.setFont(_original_font)
        return
    QLocale.setDefault(number_locale(code))
    font = QFont(_original_font)
    font.setFamilies(FONT_FAMILIES)
    app.setFont(font)
    translator = QTranslator(app)
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load(f"qtbase_{code}", folder):
        QCoreApplication.installTranslator(translator)
        _translator = translator


def restart_command(paths: list[str]) -> tuple[str, list[str], str]:
    """The program, arguments and folder that start the app again with `paths` opened."""
    if getattr(sys, "frozen", False):
        return sys.executable, [*paths], str(Path(sys.executable).parent)
    root = Path(__file__).resolve().parents[2]
    code = "import sys; from video_converter.ui.app import main; sys.exit(main(sys.argv[1:]))"
    return sys.executable, ["-c", code, *paths], str(root)


def restart(paths: list[str]) -> bool:
    """Start the app again (the caller closes this copy). Returns whether it could be started."""
    program, args, folder = restart_command(paths)
    process = QProcess()
    process.setProgram(program)
    process.setArguments(args)
    process.setWorkingDirectory(folder)
    environment = QProcessEnvironment.systemEnvironment()
    environment.insert("PYINSTALLER_RESET_ENVIRONMENT", "1")      # a one-file exe must unpack itself again
    process.setProcessEnvironment(environment)
    started, _pid = process.startDetached()
    if not started:
        log.error("Couldn't restart the app: %s", process.errorString())
    return started
