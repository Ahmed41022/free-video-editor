"""Building blocks of the panel: a collapsible section, a guard for the mouse wheel, short texts."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox, QFrame, QGridLayout, QLabel,
    QSlider, QToolButton, QVBoxLayout, QWidget,
)

def _db_text(value: float) -> str:
    return f"{value:+g} dB".replace("-", "−") if value else "0 dB"


def _short_name(name: str, longest: int = 24) -> str:
    """A file name that fits the panel: the start and the end of a long one, with … between."""
    return name if len(name) <= longest else f"{name[:longest // 2 - 1]}…{name[-(longest // 2):]}"


class WheelGuard(QObject):
    """Makes the mouse wheel scroll the settings panel instead of changing a value by accident. A combo box, spin
    box or slider only takes the wheel while it has the keyboard focus (click it or Tab to it first); otherwise
    the wheel is passed on to the scrolling panel."""

    def __init__(self, parent: QObject):
        super().__init__(parent)

    def watch(self, root: QWidget) -> None:
        for widget in root.findChildren(QWidget):
            if isinstance(widget, (QComboBox, QAbstractSpinBox, QSlider)):
                widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)       # a wheel turn must not give it the focus
                widget.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.Wheel or not isinstance(obj, QWidget) or obj.hasFocus():
            return False
        # Hand the turn to the scrolling panel ourselves: an ignored event is not reliably passed on from here.
        area = obj.parentWidget()
        while area is not None and not isinstance(area, QAbstractScrollArea):
            area = area.parentWidget()
        if area is not None:
            viewport = area.viewport()
            where = viewport.mapFromGlobal(event.globalPosition().toPoint())
            QApplication.sendEvent(viewport, QWheelEvent(
                QPointF(where), event.globalPosition(), event.pixelDelta(), event.angleDelta(), event.buttons(),
                event.modifiers(), event.phase(), event.inverted(), event.source()))
        return True


class Section(QFrame):
    """A titled box whose content can be collapsed."""

    def __init__(self, title: str, parent: Optional[QWidget] = None, expanded: bool = True):
        super().__init__(parent)
        self.setObjectName("section")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 4, 10, 10)
        outer.setSpacing(6)
        self.header = QToolButton(self)
        self.header.setObjectName("sectionHeader")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setArrowType(self._arrow(expanded))
        self.header.toggled.connect(self._toggle)
        outer.addWidget(self.header)
        self.body = QWidget(self)
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(1, 1)
        outer.addWidget(self.body)
        self.body.setVisible(expanded)

    @staticmethod
    def _arrow(expanded: bool) -> Qt.ArrowType:
        """Down when open; when closed it points the way the text reads on (left in a right-to-left language)."""
        if expanded:
            return Qt.ArrowType.DownArrow
        rtl = QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft
        return Qt.ArrowType.LeftArrow if rtl else Qt.ArrowType.RightArrow

    def _toggle(self, expanded: bool) -> None:
        self.body.setVisible(expanded)
        self.header.setArrowType(self._arrow(expanded))

    def row(self, label: str, widget: QWidget, row: int) -> QLabel:
        text = QLabel(label)
        text.setObjectName("muted")
        self.grid.addWidget(text, row, 0)
        self.grid.addWidget(widget, row, 1)
        return text
