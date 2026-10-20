"""A small built-in line-icon set, drawn as SVG and tinted to the current theme."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme

# 24x24 outline shapes; "C" is replaced with the theme color.
_SHAPES = {
    "play": '<path d="M8 5v14l11-7z" fill="C" stroke="none"/>',
    "pause": '<rect x="6" y="5" width="4" height="14" rx="1" fill="C" stroke="none"/>'
             '<rect x="14" y="5" width="4" height="14" rx="1" fill="C" stroke="none"/>',
    "back": '<path d="M11 7l-5 5 5 5M18 7l-5 5 5 5"/>',
    "forward": '<path d="M13 7l5 5-5 5M6 7l5 5-5 5"/>',
    "add": '<path d="M12 5v14M5 12h14"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
    "undo": '<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>',
    "redo": '<path d="M15 14l5-5-5-5"/><path d="M20 9H10a6 6 0 0 0 0 12h3"/>',
    "export": '<path d="M12 15V3M7 8l5-5 5 5"/><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
           'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "open": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
    "volume": '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/>',
    "mute": '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9l5 6M21 9l-5 6"/>',
    "mark_in": '<path d="M9 4H6v16h3M10 12h9M15 8l4 4-4 4"/>',
    "mark_out": '<path d="M15 4h3v16h-3M14 12H5M9 8l-4 4 4 4"/>',
    "split": '<path d="M12 3v18"/><path d="M8 8l-4 4 4 4M16 8l4 4-4 4"/>',
    "scissors": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/>'
                '<path d="M20 4L8.1 15.9M14.5 14.5L20 20M8.1 8.1L12 12"/>',
    "compare": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M12 3v18"/>',
    "rotate_left": '<path d="M4 4v5h5"/><path d="M5 9a8 8 0 1 1 1.5 7.5"/>',
    "rotate_right": '<path d="M20 4v5h-5"/><path d="M19 9a8 8 0 1 0-1.5 7.5"/>',
    "film": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    "music": '<path d="M9 18V6l11-2v12"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
    "camera": '<path d="M3 8a2 2 0 0 1 2-2h2l2-2h6l2 2h2a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
              '<circle cx="12" cy="13" r="4"/>',
}


def icon(name: str, color: str | None = None, size: int = 20) -> QIcon:
    color = color or theme.colors()["text"]
    shape = _SHAPES[name].replace('"C"', f'"{color}"')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{shape}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    result = QIcon()
    for scale in (1, 2):
        pixmap = QPixmap(size * scale, size * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter, QRectF(0, 0, size * scale, size * scale))
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        result.addPixmap(pixmap)
    return result
