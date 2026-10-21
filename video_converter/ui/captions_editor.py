"""The "Edit captions" window: a table of the captions (start, end, text) to fix words, nudge times or
delete a caption. The times are those of the original video, the same as in the subtitle file."""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..captions import Cue
from ..core import parse_time
from ..i18n import ntr, tr

TIME_COLUMNS = (0, 1)
TEXT_COLUMN = 2
DURATION_MARGIN = 60.0      # a caption may be placed this many seconds past the end of the video


def clock(seconds: float) -> str:
    """A time as the table shows it: m:ss.mmm, or h:mm:ss.mmm from an hour on (0:00.000 for a time that
    isn't a number)."""
    if not math.isfinite(seconds):
        seconds = 0.0
    ms = int(round(max(seconds, 0.0) * 1000))
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    return (f"{hours}:{minutes:02d}:" if hours else f"{minutes}:") + f"{rest / 1000:06.3f}"


class _TextDelegate(QStyledItemDelegate):
    """Edits a caption in a box that takes more than one line (Enter starts a new line; clicking away finishes)."""

    def createEditor(self, parent: QWidget, option, index: QModelIndex) -> QWidget:
        editor = QPlainTextEdit(parent)
        editor.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        return editor

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        editor.setPlainText(index.data(Qt.ItemDataRole.EditRole) or "")
        editor.selectAll()

    def setModelData(self, editor: QWidget, model, index: QModelIndex) -> None:
        model.setData(index, editor.toPlainText().strip("\n"), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor: QWidget, option, index: QModelIndex) -> None:
        editor.setGeometry(option.rect.adjusted(0, 0, 0, max(0, 52 - option.rect.height())))


class CaptionsEditor(QDialog):
    """Shows `cues` in a table. After `accept()`, `cues` holds them as edited: in time order, with the
    ones that were deleted gone."""

    def __init__(self, cues: list[Cue], parent: Optional[QWidget] = None, duration: Optional[float] = None):
        super().__init__(parent)
        self.duration = duration if duration is not None and math.isfinite(duration) and duration > 0 else None
        self.setWindowTitle(tr("Edit captions"))
        self.setModal(True)
        self.resize(720, 460)
        self.cues: list[Cue] = [Cue(c.start, c.end, c.text) for c in cues]
        self._filling = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        intro = QLabel(tr("Double-click a time or the words to change them. Times are those of the original video "
                          "(before any cuts or speed changes). In the text, Enter starts a second line; click "
                          "somewhere else to finish."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("Start"), tr("End"), tr("Text")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setItemDelegateForColumn(TEXT_COLUMN, _TextDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(TEXT_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 100)
        self.table.setColumnWidth(1, 100)
        self.table.itemChanged.connect(self._changed)
        layout.addWidget(self.table, 1)

        self.message = QLabel()
        self.message.setObjectName("error")
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)

        row = QHBoxLayout()
        self.delete_button = QPushButton(tr("Delete caption"))
        self.delete_button.setToolTip(tr("Remove the selected captions"))
        self.delete_button.clicked.connect(self.delete_selected)
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        row.addWidget(self.delete_button)
        row.addWidget(self.count_label, 1)
        layout.addLayout(row)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("Save"))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Cancel"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._fill()
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._update_buttons()

    # ----- the table -----------------------------------------------------------

    def _fill(self) -> None:
        self._filling = True
        try:
            self.table.setRowCount(len(self.cues))
            for row, cue in enumerate(self.cues):
                self.table.setItem(row, 0, QTableWidgetItem(clock(cue.start)))
                self.table.setItem(row, 1, QTableWidgetItem(clock(cue.end)))
                text = QTableWidgetItem(cue.text)
                self.table.setItem(row, TEXT_COLUMN, text)
            self.table.resizeRowsToContents()
        finally:
            self._filling = False

    def _changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        row, column = item.row(), item.column()
        cue = self.cues[row]
        if column == TEXT_COLUMN:
            cue.text = item.text().strip("\n")
            self.table.resizeRowToContents(row)
            self.message.hide()
            return
        try:
            value = parse_time(item.text())
            if value is None or not self._allowed(value):
                raise ValueError(tr("Type a time such as 1:23.5 or 83.5"))
        except ValueError as exc:
            self._reject_time(item, cue, column, str(exc))
            return
        start, end = (value, cue.end) if column == 0 else (cue.start, value)
        if column == 0 and end <= start:
            end = start + (cue.end - cue.start)         # moved past its end: the whole caption moves later
            if not self._allowed(end):
                self._reject_time(item, cue, column, tr("Type a time such as 1:23.5 or 83.5"))
                return
            self._show_time(self.table.item(item.row(), 1), end)
        if end <= start:
            self._reject_time(item, cue, column, tr("A caption has to end after it starts."))
            return
        cue.start, cue.end = start, end
        self.message.hide()
        self._show_time(item, value)

    def _allowed(self, seconds: float) -> bool:
        """Whether a typed time makes sense: a real, non-negative number, not far past the end of the video."""
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            return False
        return self.duration is None or seconds <= self.duration + DURATION_MARGIN

    def _reject_time(self, item: QTableWidgetItem, cue: Cue, column: int, why: str) -> None:
        self.message.setText(why)
        self.message.show()
        self._show_time(item, cue.start if column == 0 else cue.end)

    def _show_time(self, item: QTableWidgetItem, seconds: float) -> None:
        self._filling = True
        try:
            item.setText(clock(seconds))
        finally:
            self._filling = False

    def selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.table.selectedIndexes()})

    def delete_selected(self) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        for row in reversed(rows):
            del self.cues[row]
            self.table.removeRow(row)
        self.message.hide()
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.delete_button.setEnabled(bool(self.selected_rows()))
        count = len(self.cues)
        self.count_label.setText(ntr(count, "{n} caption", "{n} captions"))

    def accept(self) -> None:
        # A time that isn't a real number can never be stored (the cells check it), but never write one either.
        self.cues = sorted((c for c in self.cues if c.text.strip() and math.isfinite(c.start) and math.isfinite(c.end)),
                           key=lambda c: (c.start, c.end))
        super().accept()
