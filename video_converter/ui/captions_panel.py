"""The "Text and logo" controls: a list of texts with an editor for the selected one, the logo, and the
subtitle file. It is shown inside a section of the inspector and reports every change as a function
that edits a copy of the settings (see `Inspector._edit`)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFontComboBox, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ..captions import (
    ALIGNMENTS, BOX_OPACITY, IMAGE_EXTENSIONS, LOGO_CORNERS, LOGO_MARGIN, LOGO_OPACITY, LOGO_SIZE, MAX_TEXTS,
    OUTLINE_SIZE, SUBTITLE_EXTENSIONS, SUBTITLE_POSITIONS, SUBTITLE_SIZE, TEXT_SIZE, TextItem,
)
from ..core import format_time, parse_time
from ..timing import FADE_MAX
from ..video import VideoEdit

TYPING_PAUSE = 700      # milliseconds without typing before the text counts as one finished edit

# Where the quick position buttons put a text: (label, y).
POSITIONS = (("Top", 0.12), ("Middle", 0.5), ("Bottom", 0.85))


def _percent_box(low: float, high: float, tip: str = "") -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(low * 100, high * 100)
    box.setDecimals(1)
    box.setSingleStep(1.0)
    box.setSuffix(" %")
    box.setKeyboardTracking(False)
    box.setToolTip(tip)
    return box


def _muted(text: str = "", wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(wrap)
    return label


def _row(*widgets: QWidget, stretch_first: bool = True) -> QWidget:
    line = QHBoxLayout()
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(4)
    for i, widget in enumerate(widgets):
        line.addWidget(widget, 1 if i == 0 and stretch_first else 0)
    holder = QWidget()
    holder.setLayout(line)
    return holder


class ColorButton(QPushButton):
    """A button showing a color; clicking it opens the color picker."""
    picked = Signal(str)

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.title, self.color = title, "#ffffff"
        self.setFixedWidth(44)
        self.setToolTip(title)
        self.clicked.connect(self._choose)

    def set_color(self, color: str) -> None:
        self.color = color
        pixmap = QPixmap(22, 12)
        pixmap.fill(QColor(color))
        self.setIcon(pixmap)

    def _choose(self) -> None:
        chosen = QColorDialog.getColor(QColor(self.color), self, self.title)
        if chosen.isValid():
            self.picked.emit(chosen.name())


class TextBox(QPlainTextEdit):
    """The box a text is typed in; it says when the person has left it."""
    leftBox = Signal()

    def focusOutEvent(self, event) -> None:
        self.leftBox.emit()
        super().focusOutEvent(event)


class CaptionsPanel(QWidget):
    # a function that changes a copy of the settings, the undo label, whether the edit is complete
    edited = Signal(object, str, bool)
    selectionChanged = Signal(int)          # the selected text's index, or -1
    timeRequested = Signal(str)             # "start" or "end": use the playhead as the selected text's time

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.edit: Optional[VideoEdit] = None
        self.selected = -1
        self._updating = False
        self._select_after: Optional[int] = None
        self._typing = QTimer(self, singleShot=True, interval=TYPING_PAUSE, timeout=self._finish_typing)
        self._typing_pending = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # --- the texts
        layout.addWidget(self._heading("Text"))
        self.list = QListWidget()
        self.list.setMaximumHeight(92)
        self.list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.list.currentRowChanged.connect(self._row_chosen)
        layout.addWidget(self.list)
        self.add_button = QPushButton("Add text")
        self.add_button.setToolTip("Write something on the video, like a title or a caption")
        self.add_button.clicked.connect(self._add)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self._duplicate)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        layout.addWidget(_row(self.add_button, self.duplicate_button, self.remove_button, stretch_first=False))
        self.empty_hint = _muted("Add text to put a title or captions on the video. Drag it on the preview to "
                                 "move it.")
        layout.addWidget(self.empty_hint)

        self.editor = QWidget()
        grid = QGridLayout(self.editor)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        layout.addWidget(self.editor)

        def row(label: str, widget: QWidget) -> None:
            n = grid.rowCount()
            grid.addWidget(_muted(label, wrap=False), n, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(widget, n, 1)

        self.text_box = TextBox()
        self.text_box.leftBox.connect(self._finish_typing)
        self.text_box.setFixedHeight(64)
        self.text_box.setToolTip("What to write. Press Enter for a new line. Arabic, Hebrew and other scripts "
                                 "that run right to left work too.")
        self.text_box.textChanged.connect(self._text_typed)
        row("Text", self.text_box)
        self.font_box = QFontComboBox()
        self.font_box.setEditable(False)
        self.font_box.setToolTip("Any font installed on this PC. If it can't show the text (Arabic in a font "
                                 "with no Arabic letters, say), another font is used for it.")
        self.font_box.currentFontChanged.connect(self._font_chosen)
        row("Font", self.font_box)
        self.size_box = _percent_box(*TEXT_SIZE, tip="The height of the text as a share of the video's height, "
                                                     "so it looks the same at any size")
        self.size_box.valueChanged.connect(lambda v: self._edit_text(lambda t: setattr(t, "size", round(v / 100, 4)),
                                                                     "Text size"))
        self.bold_box = QCheckBox("Bold")
        self.bold_box.toggled.connect(lambda on: self._edit_text(lambda t: setattr(t, "bold", on), "Text bold"))
        row("Size", _row(self.size_box, self.bold_box))
        self.color_button = ColorButton("Text color")
        self.color_button.picked.connect(lambda c: self._edit_text(lambda t: setattr(t, "color", c), "Text color"))
        row("Color", _row(self.color_button, QWidget(), stretch_first=False))
        self.outline_button = ColorButton("Outline color")
        self.outline_button.picked.connect(
            lambda c: self._edit_text(lambda t: setattr(t, "outline_color", c), "Outline color"))
        self.outline_box = _percent_box(*OUTLINE_SIZE, tip="How thick the outline is, as a share of the text size. "
                                                           "0 means no outline.")
        self.outline_box.setSpecialValueText("None")
        self.outline_box.valueChanged.connect(
            lambda v: self._edit_text(lambda t: setattr(t, "outline", round(v / 100, 4)), "Outline"))
        row("Outline", _row(self.outline_button, self.outline_box, stretch_first=False))
        self.box_check = QCheckBox("Box behind the text")
        self.box_check.toggled.connect(lambda on: self._edit_text(lambda t: setattr(t, "box", on), "Text box"))
        self.box_button = ColorButton("Box color")
        self.box_button.picked.connect(lambda c: self._edit_text(lambda t: setattr(t, "box_color", c), "Box color"))
        self.box_opacity = _percent_box(*BOX_OPACITY, tip="How solid the box is")
        self.box_opacity.valueChanged.connect(
            lambda v: self._edit_text(lambda t: setattr(t, "box_opacity", round(v / 100, 3)), "Box opacity"))
        row("Box", _row(self.box_check, self.box_button, self.box_opacity, stretch_first=False))
        self.align_group = QButtonGroup(self)
        aligns = QHBoxLayout()
        aligns.setSpacing(4)
        aligns.setContentsMargins(0, 0, 0, 0)
        for i, (key, label) in enumerate(ALIGNMENTS.items()):
            button = QPushButton(label)
            button.setObjectName("chip")
            button.setMinimumWidth(0)
            button.setCheckable(True)
            button.setProperty("align", key)
            button.setToolTip("How the lines line up when the text has more than one")
            self.align_group.addButton(button, i)
            aligns.addWidget(button)
        self.align_group.idClicked.connect(
            lambda i: self._edit_text(lambda t: setattr(t, "align", list(ALIGNMENTS)[i]), "Text alignment"))
        holder = QWidget()
        holder.setLayout(aligns)
        row("Lines", holder)
        self.position_group = QButtonGroup(self)
        places = QHBoxLayout()
        places.setSpacing(4)
        places.setContentsMargins(0, 0, 0, 0)
        for i, (label, y) in enumerate(POSITIONS):
            button = QPushButton(label)
            button.setObjectName("chip")
            button.setMinimumWidth(0)
            button.setToolTip("Put the text here, in the middle. You can also drag it on the preview.")
            button.clicked.connect(lambda _=False, y=y: self._edit_text(
                lambda t: (setattr(t, "x", 0.5), setattr(t, "y", y)), "Move text"))
            self.position_group.addButton(button, i)
            places.addWidget(button)
        holder = QWidget()
        holder.setLayout(places)
        row("Place", holder)
        self.start_edit, self.end_edit = QLineEdit(), QLineEdit()
        self.start_edit.setPlaceholderText("Beginning")
        self.end_edit.setPlaceholderText("End of the video")
        for edit, which in ((self.start_edit, "start"), (self.end_edit, "end")):
            edit.editingFinished.connect(lambda which=which: self._time_typed(which))
            here = QPushButton("Use playhead")
            here.setToolTip("Use where the playhead is now on the finished video")
            here.clicked.connect(lambda _=False, which=which: self.timeRequested.emit(which))
            row("Show from" if which == "start" else "Until", _row(edit, here))
        self.time_error = QLabel()
        self.time_error.setObjectName("error")
        self.time_error.setWordWrap(True)
        self.time_error.hide()
        grid.addWidget(self.time_error, grid.rowCount(), 1)
        self.fade_in_box, self.fade_out_box = QDoubleSpinBox(), QDoubleSpinBox()
        for box, name, word in ((self.fade_in_box, "fade_in", "in"), (self.fade_out_box, "fade_out", "out")):
            box.setRange(0.0, FADE_MAX)
            box.setDecimals(1)
            box.setSingleStep(0.5)
            box.setSuffix(" s")
            box.setSpecialValueText("Off")
            box.setKeyboardTracking(False)
            box.setToolTip(f"Seconds the text takes to fade {word}")
            box.valueChanged.connect(lambda v, name=name: self._edit_text(
                lambda t: setattr(t, name, round(v, 1)), "Text fade"))
        row("Fade", _row(_muted("In", wrap=False), self.fade_in_box, _muted("Out", wrap=False), self.fade_out_box))
        self.times_hint = _muted("Times are on the finished video, after cuts and speed changes.")
        grid.addWidget(self.times_hint, grid.rowCount(), 0, 1, 2)

        # --- the logo
        layout.addWidget(self._heading("Logo"))
        self.logo_name = _muted("None", wrap=False)
        self.logo_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.logo_choose = QPushButton("Choose…")
        self.logo_choose.setToolTip("Pick a picture to show over the whole video. A PNG with see-through "
                                    "parts works best.")
        self.logo_choose.clicked.connect(self._choose_logo)
        self.logo_remove = QPushButton("Remove")
        self.logo_remove.clicked.connect(lambda: self.edited.emit(
            lambda s: setattr(s.edit.logo, "file", ""), "Remove logo", True))
        layout.addWidget(_row(self.logo_name, self.logo_choose, self.logo_remove))
        self.logo_missing = QLabel("The logo file can't be found. Choose it again.")
        self.logo_missing.setObjectName("error")
        self.logo_missing.setWordWrap(True)
        layout.addWidget(self.logo_missing)
        self.logo_options = QWidget()
        logo_grid = QGridLayout(self.logo_options)
        logo_grid.setContentsMargins(0, 0, 0, 0)
        logo_grid.setHorizontalSpacing(8)
        logo_grid.setVerticalSpacing(6)
        logo_grid.setColumnStretch(1, 1)
        self.corner_box = QComboBox()
        for key, label in LOGO_CORNERS.items():
            self.corner_box.addItem(label, key)
        self.corner_box.setToolTip("Pick a corner, or drag the logo on the preview to put it anywhere")
        self.corner_box.currentIndexChanged.connect(lambda: self._edit_logo(
            lambda logo: setattr(logo, "corner", self.corner_box.currentData()), "Logo position"))
        self.logo_size = _percent_box(*LOGO_SIZE, tip="The logo's width as a share of the video's width")
        self.logo_size.valueChanged.connect(lambda v: self._edit_logo(
            lambda logo: setattr(logo, "size", round(v / 100, 4)), "Logo size"))
        self.logo_opacity = _percent_box(*LOGO_OPACITY, tip="100 is solid; lower lets the video show through")
        self.logo_opacity.setDecimals(0)
        self.logo_opacity.valueChanged.connect(lambda v: self._edit_logo(
            lambda logo: setattr(logo, "opacity", round(v / 100, 3)), "Logo opacity"))
        self.logo_margin = _percent_box(*LOGO_MARGIN, tip="How far a logo in a corner sits from the edge")
        self.logo_margin.valueChanged.connect(lambda v: self._edit_logo(
            lambda logo: setattr(logo, "margin", round(v / 100, 4)), "Logo margin"))
        for n, (label, widget) in enumerate((("Position", self.corner_box), ("Size", self.logo_size),
                                             ("Opacity", self.logo_opacity), ("Margin", self.logo_margin))):
            logo_grid.addWidget(_muted(label, wrap=False), n, 0)
            logo_grid.addWidget(widget, n, 1)
        layout.addWidget(self.logo_options)

        # --- subtitles
        layout.addWidget(self._heading("Subtitles"))
        self.sub_name = _muted("None", wrap=False)
        self.sub_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sub_choose = QPushButton("Choose…")
        self.sub_choose.setToolTip("Burn the words of an .srt, .vtt or .ass file into the picture")
        self.sub_choose.clicked.connect(self._choose_subtitles)
        self.sub_remove = QPushButton("Remove")
        self.sub_remove.clicked.connect(lambda: self.edited.emit(
            lambda s: setattr(s.edit.subtitles, "file", ""), "Remove subtitles", True))
        layout.addWidget(_row(self.sub_name, self.sub_choose, self.sub_remove))
        self.sub_message = QLabel("The subtitle file can't be found. Choose it again.")
        self.sub_message.setObjectName("error")
        self.sub_message.setWordWrap(True)
        layout.addWidget(self.sub_message)
        self.sub_options = QWidget()
        sub_grid = QGridLayout(self.sub_options)
        sub_grid.setContentsMargins(0, 0, 0, 0)
        sub_grid.setHorizontalSpacing(8)
        sub_grid.setVerticalSpacing(6)
        sub_grid.setColumnStretch(1, 1)
        self.sub_font = QFontComboBox()
        self.sub_font.setEditable(False)
        self.sub_font.currentFontChanged.connect(lambda font: self._edit_subtitles(
            lambda sub: setattr(sub, "font", font.family()), "Subtitle font"))
        self.sub_size = _percent_box(*SUBTITLE_SIZE, tip="The height of the subtitle text as a share of the "
                                                         "video's height")
        self.sub_size.valueChanged.connect(lambda v: self._edit_subtitles(
            lambda sub: setattr(sub, "size", round(v / 100, 4)), "Subtitle size"))
        self.sub_bold = QCheckBox("Bold")
        self.sub_bold.toggled.connect(lambda on: self._edit_subtitles(lambda sub: setattr(sub, "bold", on),
                                                                      "Subtitle bold"))
        self.sub_color = ColorButton("Subtitle color")
        self.sub_color.picked.connect(lambda c: self._edit_subtitles(lambda sub: setattr(sub, "color", c),
                                                                     "Subtitle color"))
        self.sub_outline_color = ColorButton("Subtitle outline color")
        self.sub_outline_color.picked.connect(lambda c: self._edit_subtitles(
            lambda sub: setattr(sub, "outline_color", c), "Subtitle outline"))
        self.sub_outline = _percent_box(*OUTLINE_SIZE, tip="How thick the outline is, as a share of the text size")
        self.sub_outline.setSpecialValueText("None")
        self.sub_outline.valueChanged.connect(lambda v: self._edit_subtitles(
            lambda sub: setattr(sub, "outline", round(v / 100, 4)), "Subtitle outline"))
        self.sub_position = QComboBox()
        for key, label in SUBTITLE_POSITIONS.items():
            self.sub_position.addItem(label, key)
        self.sub_position.currentIndexChanged.connect(lambda: self._edit_subtitles(
            lambda sub: setattr(sub, "position", self.sub_position.currentData()), "Subtitle position"))
        for n, (label, widget) in enumerate((
                ("Font", self.sub_font), ("Size", _row(self.sub_size, self.sub_bold)),
                ("Color", _row(self.sub_color, QWidget(), stretch_first=False)),
                ("Outline", _row(self.sub_outline_color, self.sub_outline, stretch_first=False)),
                ("Place", self.sub_position))):
            sub_grid.addWidget(_muted(label, wrap=False), n, 0)
            sub_grid.addWidget(widget, n, 1)
        layout.addWidget(self.sub_options)
        layout.addWidget(_muted("Subtitles are timed for the original video; cuts and speed changes are taken "
                                "into account. The preview shows them under Before / after."))

        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    # ----- showing settings ----------------------------------------------------

    @property
    def texts(self) -> list[TextItem]:
        return self.edit.texts if self.edit else []

    def set_edit(self, edit: Optional[VideoEdit]) -> None:
        """Show these settings without reporting any edit."""
        self.edit = copy.deepcopy(edit) if edit else None
        self._updating = True
        try:
            texts = self.texts
            previous = self.selected
            if self._select_after is not None:
                wanted, self._select_after = self._select_after, None
            else:
                wanted = previous
            self.selected = wanted if 0 <= wanted < len(texts) else (0 if texts else -1)
            self.list.clear()
            for i, item in enumerate(texts):
                self.list.addItem(f"{i + 1}.  {self._summary(item)}")
            if self.selected >= 0:
                self.list.setCurrentRow(self.selected)
            self._show_selected()
            self._show_logo()
            self._show_subtitles()
        finally:
            self._updating = False
        if self.selected != previous:
            self.selectionChanged.emit(self.selected)

    @staticmethod
    def _summary(item: TextItem) -> str:
        line = " ".join(item.text.split()) or "(empty)"
        return line if len(line) <= 34 else line[:33] + "…"

    def _show_selected(self) -> None:
        texts = self.texts
        have = self.selected >= 0
        self.editor.setVisible(have)
        self.empty_hint.setVisible(not texts)
        self.duplicate_button.setEnabled(have and len(texts) < MAX_TEXTS)
        self.remove_button.setEnabled(have)
        self.add_button.setEnabled(len(texts) < MAX_TEXTS)
        self.list.setVisible(bool(texts))
        if not have:
            return
        item = texts[self.selected]
        if self.text_box.toPlainText() != item.text:
            self.text_box.setPlainText(item.text)
        index = self.font_box.findText(item.font)
        if index >= 0:
            self.font_box.setCurrentIndex(index)
        self.size_box.setValue(item.size * 100)
        self.bold_box.setChecked(item.bold)
        self.color_button.set_color(item.color)
        self.outline_button.set_color(item.outline_color)
        self.outline_box.setValue(item.outline * 100)
        self.box_check.setChecked(item.box)
        self.box_button.set_color(item.box_color)
        self.box_opacity.setValue(item.box_opacity * 100)
        self.box_button.setEnabled(item.box)
        self.box_opacity.setEnabled(item.box)
        self.align_group.button(list(ALIGNMENTS).index(item.align)).setChecked(True)
        for edit, which in ((self.start_edit, "start"), (self.end_edit, "end")):
            if not edit.hasFocus():
                edit.setText(_time_text(item, which))
        self.fade_in_box.setValue(item.fade_in)
        self.fade_out_box.setValue(item.fade_out)

    def _show_logo(self) -> None:
        logo = self.edit.logo if self.edit else None
        has = bool(logo and logo.file)
        self.logo_name.setText(_short(Path(logo.file).name) if has else "None")
        self.logo_name.setToolTip(logo.file if has else "")
        self.logo_remove.setEnabled(has)
        self.logo_options.setVisible(has)
        self.logo_missing.setVisible(has and not Path(logo.file).is_file())
        if logo:
            self.corner_box.setCurrentIndex(max(self.corner_box.findData(logo.corner), 0))
            self.logo_size.setValue(logo.size * 100)
            self.logo_opacity.setValue(logo.opacity * 100)
            self.logo_margin.setValue(logo.margin * 100)
            self.logo_margin.setEnabled(logo.corner != "custom" and logo.corner != "center")

    def _show_subtitles(self) -> None:
        sub = self.edit.subtitles if self.edit else None
        has = bool(sub and sub.file)
        self.sub_name.setText(_short(Path(sub.file).name) if has else "None")
        self.sub_name.setToolTip(sub.file if has else "")
        self.sub_remove.setEnabled(has)
        self.sub_options.setVisible(has)
        self.sub_message.setVisible(has and not Path(sub.file).is_file())
        if sub:
            index = self.sub_font.findText(sub.font)
            if index >= 0:
                self.sub_font.setCurrentIndex(index)
            self.sub_size.setValue(sub.size * 100)
            self.sub_bold.setChecked(sub.bold)
            self.sub_color.set_color(sub.color)
            self.sub_outline_color.set_color(sub.outline_color)
            self.sub_outline.setValue(sub.outline * 100)
            self.sub_position.setCurrentIndex(max(self.sub_position.findData(sub.position), 0))

    def select(self, index: int) -> None:
        """Select a text from outside (a click on it in the preview)."""
        if 0 <= index < len(self.texts) and index != self.selected:
            self.list.setCurrentRow(index)

    # ----- editing -------------------------------------------------------------

    def _row_chosen(self, row: int) -> None:
        if self._updating or row == self.selected:
            return
        self._finish_typing()
        self.selected = row
        self._updating = True
        try:
            self._show_selected()
        finally:
            self._updating = False
        self.selectionChanged.emit(self.selected)

    def _edit_text(self, change: Callable[[TextItem], None], label: str, final: bool = True) -> None:
        if self._updating or self.selected < 0:
            return
        index = self.selected

        def apply(settings) -> None:
            if index < len(settings.edit.texts):
                change(settings.edit.texts[index])

        self.edited.emit(apply, label, final)

    def _edit_logo(self, change, label: str) -> None:
        if not self._updating:
            self.edited.emit(lambda s: change(s.edit.logo), label, True)

    def _edit_subtitles(self, change, label: str) -> None:
        if not self._updating:
            self.edited.emit(lambda s: change(s.edit.subtitles), label, True)

    def _add(self) -> None:
        count = len(self.texts)
        if count >= MAX_TEXTS:
            return
        self._select_after = count
        self.edited.emit(lambda s: s.edit.texts.append(TextItem()), "Add text", True)

    def _duplicate(self) -> None:
        index = self.selected
        if index < 0 or len(self.texts) >= MAX_TEXTS:
            return

        def apply(settings) -> None:
            if index < len(settings.edit.texts):
                twin = copy.deepcopy(settings.edit.texts[index])
                twin.y = min(twin.y + 0.07, 0.95)       # a little lower, so the copy doesn't hide the original
                settings.edit.texts.insert(index + 1, twin)

        self._select_after = index + 1
        self.edited.emit(apply, "Duplicate text", True)

    def _remove(self) -> None:
        index = self.selected
        if index < 0:
            return
        self._select_after = max(index - 1, 0)
        self.edited.emit(lambda s: s.edit.texts.pop(index) if index < len(s.edit.texts) else None,
                         "Remove text", True)

    def _text_typed(self) -> None:
        if self._updating or self.selected < 0:
            return
        text = self.text_box.toPlainText()
        # Typing is one undo step: the edits merge until a pause (or leaving the box) finishes it.
        self._edit_text(lambda t: setattr(t, "text", text), "Edit text", final=False)
        self._typing_pending = True
        self._typing.start()

    def _finish_typing(self) -> None:
        self._typing.stop()
        if self._typing_pending:
            self._typing_pending = False
            text = self.text_box.toPlainText()
            self._edit_text(lambda t: setattr(t, "text", text), "Edit text", final=True)

    def _font_chosen(self, font: QFont) -> None:
        self._edit_text(lambda t: setattr(t, "font", font.family()), "Text font")

    def _time_typed(self, which: str) -> None:
        if self._updating or self.selected < 0:
            return
        item = self.texts[self.selected]
        widget = self.start_edit if which == "start" else self.end_edit
        if widget.text().strip() == _time_text(item, which):
            self.time_error.hide()
            return
        try:
            value = parse_time(widget.text())
        except ValueError as exc:
            self.time_error.setText(str(exc))
            self.time_error.show()
            return
        start = (value or 0.0) if which == "start" else item.start
        end = value if which == "end" else item.end
        if end is not None and end <= start:
            self.time_error.setText("The end has to be after the start.")
            self.time_error.show()
            return
        self.time_error.hide()
        self._edit_text(lambda t: setattr(t, which, (value or 0.0) if which == "start" else value), "Text time")

    def _choose_logo(self) -> None:
        current = self.edit.logo.file if self.edit else ""
        patterns = " ".join(f"*{extension}" for extension in IMAGE_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a logo", str(Path(current).parent) if current else "", f"Pictures ({patterns});;All files (*)")
        if path:
            self.edited.emit(lambda s: setattr(s.edit.logo, "file", str(Path(path))), "Choose logo", True)

    def _choose_subtitles(self) -> None:
        current = self.edit.subtitles.file if self.edit else ""
        patterns = " ".join(f"*{extension}" for extension in SUBTITLE_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a subtitle file", str(Path(current).parent) if current else "",
            f"Subtitles ({patterns});;All files (*)")
        if path:
            self.edited.emit(lambda s: setattr(s.edit.subtitles, "file", str(Path(path))), "Choose subtitles", True)


def _time_text(item: TextItem, which: str) -> str:
    """What the start or end box shows: nothing for "from the beginning" and "to the end"."""
    if which == "start":
        return format_time(item.start) if item.start > 0 else ""
    return format_time(item.end) if item.end is not None else ""


def _short(name: str, longest: int = 24) -> str:
    return name if len(name) <= longest else f"{name[:longest // 2 - 1]}…{name[-(longest // 2):]}"
