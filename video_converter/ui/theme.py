"""Dark and light themes for the Qt interface."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

DARK = {
    "window": "#1e1f22", "panel": "#25262a", "base": "#18191c", "raised": "#2d2e33",
    "border": "#3a3b41", "text": "#e9e9ec", "muted": "#9c9da4", "accent": "#4f8cff",
    "accent_text": "#ffffff", "danger": "#ff6b6b", "success": "#4cc38a",
    "video_bg": "#0f1012", "track": "#2a2b30", "selection": "#34507f",
}
LIGHT = {
    "window": "#f2f2f5", "panel": "#fbfbfc", "base": "#ffffff", "raised": "#ffffff",
    "border": "#d5d5dc", "text": "#1c1c1f", "muted": "#696a72", "accent": "#2f6fed",
    "accent_text": "#ffffff", "danger": "#d43c3c", "success": "#1f9d5c",
    "video_bg": "#16171a", "track": "#e6e6ec", "selection": "#cddcfb",
}

_current = dict(DARK)


def colors() -> dict[str, str]:
    """The active theme's colors, for widgets that paint themselves."""
    return _current


def resolve(mode: str, app: QApplication) -> str:
    """Turn "system" into "dark" or "light" using the Windows setting."""
    if mode in ("dark", "light"):
        return mode
    scheme = app.styleHints().colorScheme()
    return "light" if scheme == Qt.ColorScheme.Light else "dark"


def apply_theme(app: QApplication, mode: str = "system") -> str:
    """Apply a theme and return the resolved mode ("dark" or "light")."""
    resolved = resolve(mode, app)
    c = DARK if resolved == "dark" else LIGHT
    _current.clear()
    _current.update(c)

    app.setStyle("Fusion")
    palette = QPalette()
    role = QPalette.ColorRole
    for r, key in ((role.Window, "window"), (role.Base, "base"), (role.AlternateBase, "panel"),
                   (role.Button, "raised"), (role.Text, "text"), (role.WindowText, "text"),
                   (role.ButtonText, "text"), (role.ToolTipBase, "raised"), (role.ToolTipText, "text"),
                   (role.Highlight, "accent"), (role.HighlightedText, "accent_text"),
                   (role.PlaceholderText, "muted"), (role.Link, "accent")):
        palette.setColor(r, QColor(c[key]))
    disabled = QPalette.ColorGroup.Disabled
    for r in (role.Text, role.WindowText, role.ButtonText):
        palette.setColor(disabled, r, QColor(c["muted"]))
    app.setPalette(palette)

    app.setStyleSheet(f"""
        QWidget {{ font-size: 10pt; }}
        QMainWindow, QDialog {{ background: {c['window']}; }}
        QToolBar {{ background: {c['panel']}; border: none; border-bottom: 1px solid {c['border']};
                    padding: 4px; spacing: 4px; }}
        QMenuBar {{ background: {c['panel']}; color: {c['text']}; border-bottom: 1px solid {c['border']}; }}
        QMenuBar::item {{ background: transparent; padding: 4px 10px; border-radius: 4px; }}
        QMenuBar::item:selected {{ background: {c['raised']}; }}
        QToolButton {{ border: 1px solid transparent; border-radius: 6px; padding: 5px 8px; color: {c['text']}; }}
        QToolButton:hover {{ background: {c['raised']}; border-color: {c['border']}; }}
        QToolButton:checked {{ background: {c['selection']}; border-color: {c['accent']}; }}
        QToolButton:disabled {{ color: {c['muted']}; }}
        QPushButton {{ background: {c['raised']}; border: 1px solid {c['border']}; border-radius: 6px;
                       padding: 5px 12px; color: {c['text']}; }}
        QPushButton:hover {{ border-color: {c['accent']}; }}
        QPushButton:checked {{ background: {c['selection']}; border-color: {c['accent']}; }}
        QPushButton:disabled {{ color: {c['muted']}; }}
        QPushButton#primary {{ background: {c['accent']}; border-color: {c['accent']}; color: {c['accent_text']};
                               font-weight: 600; padding: 6px 16px; }}
        QPushButton#primary:hover {{ background: {QColor(c['accent']).lighter(112).name()}; }}
        QLineEdit, QComboBox {{ background: {c['base']}; border: 1px solid {c['border']}; border-radius: 6px;
                                padding: 4px 8px; color: {c['text']}; }}
        QLineEdit:focus, QComboBox:focus {{ border-color: {c['accent']}; }}
        QComboBox QAbstractItemView {{ background: {c['raised']}; border: 1px solid {c['border']};
                                        selection-background-color: {c['selection']}; }}
        QListWidget {{ background: {c['panel']}; border: none; outline: none; }}
        QListWidget::item {{ border-radius: 6px; padding: 4px; margin: 2px 4px; }}
        QListWidget::item:selected {{ background: {c['selection']}; color: {c['text']}; }}
        QScrollArea {{ background: transparent; border: none; }}
        QScrollArea > QWidget > QWidget {{ background: transparent; }}
        QSplitter::handle {{ background: {c['border']}; }}
        QSplitter::handle:horizontal {{ width: 1px; }}
        QSplitter::handle:vertical {{ height: 1px; }}
        QProgressBar {{ background: {c['track']}; border: none; border-radius: 3px; height: 6px;
                        text-align: center; color: transparent; }}
        QProgressBar::chunk {{ background: {c['accent']}; border-radius: 3px; }}
        QSlider::groove:horizontal {{ height: 4px; background: {c['track']}; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ background: {c['text']}; width: 14px; height: 14px; margin: -5px 0;
                                      border-radius: 7px; }}
        QStatusBar {{ background: {c['panel']}; border-top: 1px solid {c['border']}; color: {c['muted']}; }}
        QLabel#muted {{ color: {c['muted']}; }}
        QLabel#heading {{ font-weight: 600; }}
        QLabel#error {{ color: {c['danger']}; }}
        QFrame#section {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px; }}
        QFrame#panel {{ background: {c['panel']}; }}
        QToolButton#sectionHeader {{ font-weight: 600; border: none; padding: 6px 4px; text-align: left; }}
        QToolButton#sectionHeader:hover, QToolButton#sectionHeader:checked {{ background: transparent;
                                                                         border: none; }}
        QPushButton#chip {{ padding: 4px 6px; }}
        QCheckBox {{ spacing: 6px; }}
    """)
    return resolved
