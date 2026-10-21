"""The settings panel on the right: collapsible sections that edit one file's ClipSettings."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPointF, Qt, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from ..core import AUDIO_EXTENSIONS, QUALITY_PRESETS, format_time, parse_time
from ..i18n import N_, join_parts, tr
from ..project import OUTPUT_AUDIO, OUTPUT_VIDEO, PRESETS, ClipSettings
from ..output import (
    DEFAULT_TARGET_MB, GIF_FPS_CHOICES, GIF_WIDTH_CHOICES, OUTPUT_FORMATS, TARGET_PRESETS, TARGET_RANGE,
)
from ..sound import AUDIO_DENOISE, MUSIC_VOLUME_MAX, MUSIC_VOLUME_MIN, VOLUME_MAX, VOLUME_MIN
from ..timing import FADE_MAX, SPEED_MAX, SPEED_MIN, output_duration, removed_parts
from ..video import ADJUSTMENTS, FIT_MODES, FRAME_RATES, LOOKS, RESOLUTIONS, STABILIZE, VIDEO_QUALITY
from .captions_panel import CaptionsPanel

FIT_LABEL = N_("Fit in a file size")
FIT_DATA = "fit"       # the Quality box's value for it (the others are x264 quality numbers)

ADJUST_LABELS = {"brightness": N_("Brightness"), "contrast": N_("Contrast"), "saturation": N_("Saturation"),
                 "gamma": N_("Gamma"), "sharpen": N_("Sharpen"), "denoise": N_("Denoise")}
STABILIZE_LABELS = {"off": N_("Off"), "light": N_("Light"), "strong": N_("Strong")}
NOISE_LABELS = {"off": N_("Off"), "light": N_("Light"), "strong": N_("Strong")}
assert set(NOISE_LABELS) == set(AUDIO_DENOISE)
LOOK_CHIPS = {"bw": N_("B&W"), "contrast": N_("Contrast")}      # short names that fit the panel

SHAPES = [(N_("Original"), None), ("9:16", (9, 16)), ("1:1", (1, 1)), ("4:5", (4, 5)), ("16:9", (16, 9))]
SHAPE_HINTS = {None: N_("Keep the video's own shape"), (9, 16): N_("Phone: Stories, Reels, TikTok, Shorts"),
               (1, 1): N_("Square post"), (4, 5): N_("Portrait post"), (16, 9): N_("Widescreen, YouTube")}


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


class Inspector(QScrollArea):
    # new settings, undo label, whether the edit is complete (False while dragging a slider)
    settingsEdited = Signal(object, str, bool)
    applyToAllRequested = Signal()
    savePresetRequested = Signal()
    playheadRequested = Signal(str)          # "start" or "end": use the current playhead time
    cutRequested = Signal(str)               # "split" or "remove": at the current playhead
    removeSilencesRequested = Signal()
    outputFolderRequested = Signal()
    textSelected = Signal(int)               # the selected text's index (-1: none), to show it on the preview
    textTimeRequested = Signal(str)          # "start" or "end": use the playhead as the selected text's time
    autoCaptionsRequested = Signal()         # make captions from the speech
    editCaptionsRequested = Signal()         # open the caption editor

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(330)
        self.settings: Optional[ClipSettings] = None
        self.duration: Optional[float] = None
        self._updating = False
        self._dragging = False
        self._gif = False        # the format is GIF, which has its own options
        self._fits = False       # the quality is Fit in a file size
        self._has_picture = True     # False for a sound-only file, which can only be saved as MP3
        self._size_text, self._size_is_error = "", False
        self.user_presets: dict[str, dict] = {}

        content = QWidget()
        self.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.empty = QLabel(tr("Select a video to edit its settings."))
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        # Presets
        self.preset_section = Section(tr("Presets"))
        self.preset_box = QComboBox()
        self.preset_box.currentIndexChanged.connect(self._preset_chosen)
        self.preset_section.grid.addWidget(self.preset_box, 0, 0, 1, 2)
        buttons = QVBoxLayout()
        save = QPushButton(tr("Save as preset…"))
        save.clicked.connect(self.savePresetRequested)
        apply_all = QPushButton(tr("Apply to all files"))
        apply_all.setToolTip(tr("Copy these settings to every file in the list (each keeps its own crop and trim)"))
        apply_all.clicked.connect(self.applyToAllRequested)
        buttons.addWidget(save)
        buttons.addWidget(apply_all)
        self.preset_section.grid.addLayout(buttons, 1, 0, 1, 2)

        # Output
        self.output_section = Section(tr("Output"))
        out_row = QHBoxLayout()
        self.output_group = QButtonGroup(self)
        for i, (text, value) in enumerate(((N_("Video"), OUTPUT_VIDEO), (N_("Audio (MP3)"), OUTPUT_AUDIO))):
            button = QPushButton(tr(text))
            button.setCheckable(True)
            button.setProperty("value", value)
            self.output_group.addButton(button, i)
            out_row.addWidget(button)
        self.output_group.idClicked.connect(
            lambda i: self._edit(lambda s: setattr(s, "output", self.output_group.button(i).property("value")),
                                 tr("Change output")))
        self.output_section.grid.addLayout(out_row, 0, 0, 1, 2)

        # Frame
        self.frame_section = Section(tr("Frame"))
        shapes = QGridLayout()          # three to a row: five side by side would not fit the panel
        shapes.setSpacing(4)
        shapes.setContentsMargins(0, 0, 0, 0)
        self.shape_group = QButtonGroup(self)
        for i, (text, aspect) in enumerate(SHAPES):
            button = QPushButton(tr(text))
            button.setObjectName("chip")
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setCheckable(True)
            button.setToolTip(tr(SHAPE_HINTS[aspect]))
            self.shape_group.addButton(button, i)
            shapes.addWidget(button, i // 3, i % 3)
        self.shape_group.idClicked.connect(
            lambda i: self._edit(lambda s: setattr(s.edit, "aspect", SHAPES[i][1]), tr("Change shape")))
        self.frame_section.grid.addLayout(shapes, 0, 0, 1, 2)
        self.shape_hint = QLabel()
        self.shape_hint.setObjectName("muted")
        self.frame_section.grid.addWidget(self.shape_hint, 1, 0, 1, 2)

        self.fit_box = QComboBox()
        for key, label in FIT_MODES.items():
            self.fit_box.addItem(tr(label), key)
        self.fit_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "fit", self.fit_box.currentData()), tr("Change fit")))
        self.frame_section.row(tr("Fit"), self.fit_box, 2)

        zoom_row = QHBoxLayout()
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(100, 300)
        self.zoom_label = QLabel("1.0×")
        self.zoom_label.setMinimumWidth(36)
        self.zoom_slider.valueChanged.connect(
            lambda v: self._edit(lambda s: setattr(s.edit, "zoom", v / 100), tr("Zoom"),
                                 final=not self.zoom_slider.isSliderDown()))
        self.zoom_slider.sliderReleased.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "zoom", self.zoom_slider.value() / 100), tr("Zoom")))
        zoom_row.addWidget(self.zoom_slider)
        zoom_row.addWidget(self.zoom_label)
        zoom_widget = QWidget()
        zoom_widget.setLayout(zoom_row)
        zoom_row.setContentsMargins(0, 0, 0, 0)
        self.frame_section.row(tr("Zoom"), zoom_widget, 3)

        rotate_row = QHBoxLayout()
        rotate_row.setContentsMargins(0, 0, 0, 0)
        left = QPushButton("⟲ " + tr("Left"))
        right = QPushButton("⟳ " + tr("Right"))
        left.clicked.connect(lambda: self._edit(lambda s: setattr(s.edit, "rotate", (s.edit.rotate - 90) % 360),
                                                tr("Rotate")))
        right.clicked.connect(lambda: self._edit(lambda s: setattr(s.edit, "rotate", (s.edit.rotate + 90) % 360),
                                                 tr("Rotate")))
        for button in (left, right):
            button.setMinimumWidth(44)         # a real minimum: the style's 75 px would not fit the panel
        self.mirror_box = QCheckBox(tr("Mirror"))
        self.mirror_box.toggled.connect(lambda on: self._edit(lambda s: setattr(s.edit, "flip", on), tr("Mirror")))
        rotate_row.addWidget(left)
        rotate_row.addWidget(right)
        rotate_row.addWidget(self.mirror_box)
        rotate_widget = QWidget()
        rotate_widget.setLayout(rotate_row)
        # The arrows turn the picture counter-clockwise and clockwise: the left button stays on the left.
        rotate_widget.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.frame_section.row(tr("Rotate"), rotate_widget, 4)

        center = QPushButton(tr("Center the crop"))
        center.clicked.connect(lambda: self._edit(self._center, tr("Center crop")))
        self.frame_section.grid.addWidget(center, 5, 1)

        # Adjust
        self.adjust_section = Section(tr("Adjust"))
        self.adjust_sliders: dict[str, tuple[QSlider, QLabel]] = {}
        for row, name in enumerate(ADJUSTMENTS):
            low, high, _ = ADJUSTMENTS[name]
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(round(low * 100), round(high * 100))
            value_label = QLabel()
            value_label.setMinimumWidth(36)
            slider.valueChanged.connect(
                lambda v, name=name, slider=slider: self._edit(
                    lambda s: setattr(s.edit, name, v / 100), tr(ADJUST_LABELS[name]),
                    final=not slider.isSliderDown()))
            slider.sliderReleased.connect(
                lambda name=name, slider=slider: self._edit(
                    lambda s: setattr(s.edit, name, slider.value() / 100), tr(ADJUST_LABELS[name])))
            line = QHBoxLayout()
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(slider)
            line.addWidget(value_label)
            holder = QWidget()
            holder.setLayout(line)
            self.adjust_section.row(tr(ADJUST_LABELS[name]), holder, row)
            self.adjust_sliders[name] = (slider, value_label)
        looks = QGridLayout()
        looks.setSpacing(4)
        self.look_group = QButtonGroup(self)
        for i, (key, (label, _)) in enumerate(LOOKS.items()):
            button = QPushButton(tr(LOOK_CHIPS.get(key, label)).replace("&", "&&"))
            button.setObjectName("chip")
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setCheckable(True)
            button.setToolTip(tr(label))
            button.setProperty("look", key)
            self.look_group.addButton(button, i)
            looks.addWidget(button, i // 3, i % 3)
        self.look_group.idClicked.connect(
            lambda i: self._edit(lambda s: setattr(s.edit, "look", self.look_group.button(i).property("look")),
                                 tr("Look")))
        looks_holder = QWidget()
        looks_holder.setLayout(looks)
        looks.setContentsMargins(0, 0, 0, 0)
        row = len(ADJUSTMENTS)
        self.adjust_section.row(tr("Look"), looks_holder, row)
        reset = QPushButton(tr("Reset the picture"))
        reset.setToolTip(tr("Put every adjustment and the look back to normal"))
        reset.clicked.connect(lambda: self._edit(self._reset_picture, tr("Reset picture")))
        self.adjust_section.grid.addWidget(reset, row + 1, 1)
        self.stabilize_box = QComboBox()
        for key, label in STABILIZE_LABELS.items():
            self.stabilize_box.addItem(tr(label), key)
        self.stabilize_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "stabilize", self.stabilize_box.currentData()),
                               tr("Stabilize")))
        self.adjust_section.row(tr("Stabilize"), self.stabilize_box, row + 2)
        stabilize_hint = QLabel(tr("Steadies shaky footage when exporting, which takes longer. "
                                   "The preview doesn't show it."))
        stabilize_hint.setObjectName("muted")
        stabilize_hint.setWordWrap(True)
        self.adjust_section.grid.addWidget(stabilize_hint, row + 3, 1)

        # Trim and cut
        self.trim_section = Section(tr("Trim and cut"))
        self.start_edit = QLineEdit()
        self.end_edit = QLineEdit()
        for edit, which in ((self.start_edit, "start"), (self.end_edit, "end")):
            edit.setPlaceholderText(tr("Beginning") if which == "start" else tr("End"))
            edit.editingFinished.connect(lambda which=which: self._trim_typed(which))
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            here = QPushButton(tr("Use playhead"))
            here.setToolTip(tr("Keyboard: I sets the start, O sets the end"))
            here.clicked.connect(lambda _=False, which=which: self.playheadRequested.emit(which))
            row.addWidget(edit)
            row.addWidget(here)
            holder = QWidget()
            holder.setLayout(row)
            self.trim_section.row(tr("Start") if which == "start" else tr("End"), holder, 0 if which == "start" else 1)
        self.trim_info = QLabel()
        self.trim_info.setObjectName("muted")
        self.trim_section.grid.addWidget(self.trim_info, 2, 1)
        clear = QPushButton(tr("Clear the trim"))
        clear.clicked.connect(lambda: self._edit(self._clear_trim, tr("Clear trim")))
        self.trim_section.grid.addWidget(clear, 3, 1)
        self.trim_error = QLabel()
        self.trim_error.setObjectName("error")
        self.trim_error.setWordWrap(True)
        self.trim_error.hide()
        self.trim_section.grid.addWidget(self.trim_error, 4, 0, 1, 2)

        cut_row = QHBoxLayout()
        cut_row.setContentsMargins(0, 0, 0, 0)
        self.split_button = QPushButton(tr("Split"))
        self.split_button.setToolTip(tr("Split the video at the playhead (S)"))
        self.split_button.clicked.connect(lambda: self.cutRequested.emit("split"))
        self.remove_part_button = QPushButton(tr("Remove part"))
        self.remove_part_button.setToolTip(tr("Cut out the part under the playhead, between two splits (X)"))
        self.remove_part_button.clicked.connect(lambda: self.cutRequested.emit("remove"))
        cut_row.addWidget(self.split_button)
        cut_row.addWidget(self.remove_part_button)
        cut_holder = QWidget()
        cut_holder.setLayout(cut_row)
        self.trim_section.row(tr("Cut"), cut_holder, 5)
        self.cut_info = QLabel()
        self.cut_info.setObjectName("muted")
        self.cut_info.setWordWrap(True)
        self.trim_section.grid.addWidget(self.cut_info, 6, 1)
        self.restore_button = QPushButton(tr("Put back the cut parts"))
        self.restore_button.clicked.connect(lambda: self._edit(self._clear_cuts, tr("Restore cuts")))
        self.trim_section.grid.addWidget(self.restore_button, 7, 1)
        self.silence_button = QPushButton(tr("Remove silences…"))
        self.silence_button.clicked.connect(self.removeSilencesRequested.emit)
        self.trim_section.grid.addWidget(self.silence_button, 8, 1)
        self.set_has_sound(True)

        # Speed
        self.speed_section = Section(tr("Speed"))
        self.speed_box = QDoubleSpinBox()
        self.speed_box.setRange(SPEED_MIN, SPEED_MAX)
        self.speed_box.setSingleStep(0.25)
        self.speed_box.setDecimals(2)
        self.speed_box.setSuffix("×")
        self.speed_box.setKeyboardTracking(False)
        self.speed_box.setToolTip(tr("0.25× is four times slower, 4× four times faster; the sound keeps its pitch"))
        self.speed_box.valueChanged.connect(
            lambda value: self._edit(lambda s: setattr(s.edit, "speed", round(value, 2)), tr("Speed")))
        speed_row = QHBoxLayout()
        speed_row.setContentsMargins(0, 0, 0, 0)
        speed_row.addWidget(self.speed_box, 1)
        for value in (0.5, 1.0, 2.0):
            chip = QPushButton(f"{value:g}×")
            chip.setObjectName("chip")
            chip.setMinimumWidth(36)           # a real minimum: the style's 75 px would not fit the panel
            chip.clicked.connect(lambda _=False, value=value: self.speed_box.setValue(value))
            speed_row.addWidget(chip)
        speed_holder = QWidget()
        speed_holder.setLayout(speed_row)
        self.speed_section.row(tr("Speed"), speed_holder, 0)
        self.reverse_box = QCheckBox(tr("Play backwards"))
        self.reverse_box.setToolTip(tr("Reversing holds the whole clip in memory, so it suits short clips best"))
        self.reverse_box.toggled.connect(lambda on: self._edit(lambda s: setattr(s.edit, "reverse", on),
                                                               tr("Reverse")))
        self.speed_section.grid.addWidget(self.reverse_box, 1, 1)
        self.reverse_warning = QLabel()
        self.reverse_warning.setObjectName("error")
        self.reverse_warning.setWordWrap(True)
        self.reverse_warning.hide()
        self.speed_section.grid.addWidget(self.reverse_warning, 2, 1)

        # Fades
        self.fade_section = Section(tr("Fades"))
        self.fade_boxes: dict[str, QDoubleSpinBox] = {}
        self.fade_rows: dict[str, tuple[QLabel, QWidget]] = {}
        rows = ((N_("Picture"), "fade_in", "fade_out"), (N_("Sound"), "audio_fade_in", "audio_fade_out"))
        for row_index, (label, fade_in, fade_out) in enumerate(rows):
            line = QHBoxLayout()
            line.setContentsMargins(0, 0, 0, 0)
            for name, word in ((fade_in, "in"), (fade_out, "out")):
                box = QDoubleSpinBox()
                box.setRange(0.0, FADE_MAX)
                box.setSingleStep(0.5)
                box.setDecimals(1)
                box.setSuffix(" s")
                box.setMinimumWidth(64)
                box.setSpecialValueText(tr("Off"))
                box.setKeyboardTracking(False)
                box.setToolTip(tr("Seconds to fade in from black or silence") if word == "in"
                               else tr("Seconds to fade out to black or silence"))
                box.valueChanged.connect(
                    lambda value, name=name: self._edit(lambda s: setattr(s.edit, name, round(value, 1)),
                                                        tr("Fade")))
                caption = QLabel(tr("In") if word == "in" else tr("Out"))
                caption.setObjectName("muted")
                line.addWidget(caption)
                line.addWidget(box, 1)
                self.fade_boxes[name] = box
            holder = QWidget()
            holder.setLayout(line)
            self.fade_section.row(tr(label), holder, row_index)
            self.fade_rows[label] = (self.fade_section.grid.itemAtPosition(row_index, 0).widget(), holder)

        # Text, logo and subtitles
        self.captions_section = Section(tr("Text and logo"), expanded=False)
        self.captions = CaptionsPanel()
        self.captions.edited.connect(lambda change, label, final: self._edit(change, label, final))
        self.captions.selectionChanged.connect(self.textSelected)
        self.captions.timeRequested.connect(self.textTimeRequested)
        self.captions.autoCaptionsRequested.connect(self.autoCaptionsRequested)
        self.captions.editCaptionsRequested.connect(self.editCaptionsRequested)
        self.captions_section.grid.addWidget(self.captions, 0, 0, 1, 2)

        # Sound
        self.audio_section = Section(tr("Sound"))
        self.mute_box = QCheckBox(tr("Remove the sound"))
        self.mute_box.toggled.connect(lambda on: self._edit(lambda s: setattr(s.edit, "mute", on),
                                                            tr("Remove sound")))
        self.audio_section.grid.addWidget(self.mute_box, 0, 0, 1, 2)
        self.mp3_box = QComboBox()
        for label in QUALITY_PRESETS:
            self.mp3_box.addItem(tr(label), label)
        self.mp3_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s, "audio_quality", self.mp3_box.currentData()), tr("MP3 quality")))
        self.mp3_label = QLabel(tr("MP3 quality"))
        self.mp3_label.setObjectName("muted")
        self.audio_section.grid.addWidget(self.mp3_label, 1, 0)
        self.audio_section.grid.addWidget(self.mp3_box, 1, 1)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(round(VOLUME_MIN), round(VOLUME_MAX))
        self.volume_slider.setToolTip(tr("Make the sound louder or quieter. +6 dB doubles its strength, "
                                         "−6 dB halves it."))
        self.volume_label = QLabel("0 dB")
        self.volume_label.setMinimumWidth(48)
        self.volume_slider.valueChanged.connect(
            lambda v: self._edit(lambda s: setattr(s.edit, "volume", float(v)), tr("Volume"),
                                 final=not self.volume_slider.isSliderDown()))
        self.volume_slider.sliderReleased.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "volume", float(self.volume_slider.value())),
                               tr("Volume")))
        self.volume_slider.valueChanged.connect(lambda v: self.volume_label.setText(_db_text(v)))
        self.volume_holder = self._slider_row(self.volume_slider, self.volume_label)
        self.audio_section.row(tr("Volume"), self.volume_holder, 2)

        self.normalize_box = QCheckBox(tr("Make the loudness even (−14 LUFS)"))
        self.normalize_box.setToolTip(tr(
            "Sets the whole sound to the loudness YouTube and Spotify play at, so it isn't too quiet or too "
            "loud next to other videos. It measures the sound first, so exporting takes a little longer."))
        self.normalize_box.toggled.connect(lambda on: self._edit(lambda s: setattr(s.edit, "normalize", on),
                                                                 tr("Even loudness")))
        self.audio_section.grid.addWidget(self.normalize_box, 3, 0, 1, 2)

        self.noise_box = QComboBox()
        for key, label in NOISE_LABELS.items():
            self.noise_box.addItem(tr(label), key)
        self.noise_box.setToolTip(tr("Takes out steady background noise, like hiss or a fan. Light keeps voices "
                                     "natural; Strong removes more but can make them sound thin."))
        self.noise_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "audio_denoise", self.noise_box.currentData()),
                               tr("Noise reduction")))
        self.audio_section.row(tr("Noise reduction"), self.noise_box, 4)

        music_line = QHBoxLayout()
        music_line.setContentsMargins(0, 0, 0, 0)
        music_line.setSpacing(4)
        self.music_name = QLabel()
        self.music_name.setObjectName("muted")
        self.music_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.music_choose = QPushButton(tr("Choose…"))
        self.music_choose.setToolTip(tr("Pick a song or sound to play behind the video"))
        self.music_choose.clicked.connect(self._choose_music)
        self.music_remove = QPushButton(tr("Remove"))
        self.music_remove.setToolTip(tr("Take the background music away"))
        self.music_remove.clicked.connect(lambda: self._edit(lambda s: setattr(s.edit, "music", ""),
                                                             tr("Remove music")))
        music_line.addWidget(self.music_name, 1)
        music_line.addWidget(self.music_choose)
        music_line.addWidget(self.music_remove)
        music_holder = QWidget()
        music_holder.setLayout(music_line)
        self.audio_section.row(tr("Music"), music_holder, 5)
        self.music_missing = QLabel(tr("The music file can't be found. Choose it again."))
        self.music_missing.setObjectName("error")
        self.music_missing.setWordWrap(True)
        self.music_missing.hide()
        self.audio_section.grid.addWidget(self.music_missing, 6, 1)

        self.music_slider = QSlider(Qt.Orientation.Horizontal)
        self.music_slider.setRange(round(MUSIC_VOLUME_MIN), round(MUSIC_VOLUME_MAX))
        self.music_slider.setToolTip(tr(
            "How loud the music is next to the video's own sound. A video with no sound of its own plays "
            "the music at full volume."))
        self.music_label = QLabel()
        self.music_label.setMinimumWidth(48)
        self.music_slider.valueChanged.connect(
            lambda v: self._edit(lambda s: setattr(s.edit, "music_volume", float(v)), tr("Music volume"),
                                 final=not self.music_slider.isSliderDown()))
        self.music_slider.sliderReleased.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "music_volume", float(self.music_slider.value())),
                               tr("Music volume")))
        self.music_slider.valueChanged.connect(lambda v: self.music_label.setText(_db_text(v)))
        self.music_holder = self._slider_row(self.music_slider, self.music_label)
        self.audio_section.row(tr("Music volume"), self.music_holder, 7)
        self.duck_box = QCheckBox(tr("Lower the music when people talk"))
        self.duck_box.setToolTip(tr("The music dips whenever the video's own sound is playing, and comes back "
                                    "up in the quiet parts."))
        self.duck_box.toggled.connect(lambda on: self._edit(lambda s: setattr(s.edit, "music_duck", on),
                                                            tr("Music ducking")))
        self.audio_section.grid.addWidget(self.duck_box, 8, 0, 1, 2)
        music_hint = QLabel(tr("The music plays over the whole result, starts again if it is shorter, and fades "
                               "out over the last 2 seconds."))
        music_hint.setObjectName("muted")
        music_hint.setWordWrap(True)
        self.audio_section.grid.addWidget(music_hint, 9, 0, 1, 2)
        # Everything under Remove the sound: with the sound removed none of it is heard.
        self.sound_options = [self.volume_holder, self.normalize_box, self.noise_box, music_holder]

        # Quality
        self.quality_section = Section(tr("Video quality"))
        self.quality_rows: dict[str, list[QWidget]] = {}      # name -> the widgets of that row, to show or hide

        def row(name: str, label: str, widget: QWidget, index: int) -> None:
            self.quality_rows[name] = [self.quality_section.row(label, widget, index), widget]

        def note(index: int, error: bool = False) -> QLabel:
            label = QLabel()
            label.setObjectName("error" if error else "muted")
            label.setWordWrap(True)
            label.hide()
            self.quality_section.grid.addWidget(label, index, 1)
            return label

        self.format_box = QComboBox()
        for key, fmt in OUTPUT_FORMATS.items():
            self.format_box.addItem(tr(fmt.label), key)
        self.format_box.setToolTip(tr(
            "MP4 (H.264) plays on everything. H.265 makes smaller files, but older phones and programs can't "
            "play it. WebM is for the web. MOV suits Apple's apps and video editors. A GIF is a short, silent, "
            "looping picture."))
        self.format_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "video_format", self.format_box.currentData()),
                               tr("Format")))
        row("format", tr("Format"), self.format_box, 0)
        self.format_hint = note(1)
        self.format_warning = note(2, error=True)
        self.size_box = QComboBox()
        for label, value in RESOLUTIONS.items():
            self.size_box.addItem(tr(label), value)
        self.size_box.setToolTip(tr(
            "Same as source matches the source's short side, so 1080p makes a 1080x1920 phone video. Phone, "
            "portrait and square videos are capped at 1080 pixels on the short side, so 4K doesn't make huge "
            "files. Up to 1080p does the same for any shape, and never makes a smaller video bigger. "
            "1080p, 720p and 480p set the short side."))
        self.size_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "resolution", self.size_box.currentData()), tr("Size")))
        row("size", tr("Size"), self.size_box, 3)
        self.rate_box = QComboBox()
        for label, value in FRAME_RATES.items():
            self.rate_box.addItem(tr(label), value)
        self.rate_box.setToolTip(tr(
            "Steady makes every frame the same distance apart, which phones need. A steady MP4 or MOV is also "
            "made phone-safe (H.264 High, AAC sound). A video that is slower than the rate you choose keeps "
            "its own rate, so Steady 60 never makes a 30 fps video bigger."))
        self.rate_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "frame_rate", self.rate_box.currentData()),
                               tr("Frame rate")))
        row("rate", tr("Frame rate"), self.rate_box, 4)
        self.rate_hint = note(5)
        self.crf_box = QComboBox()
        for label, value in VIDEO_QUALITY.items():
            self.crf_box.addItem(tr(label), value)
        self.crf_box.addItem(tr(FIT_LABEL), FIT_DATA)
        self.crf_box.setToolTip(tr(
            "Higher quality makes bigger files. Fit in a file size does the opposite: you say how big the file "
            "may be, for Discord, WhatsApp or email, and the picture gets what is left after the sound."))
        self.crf_box.currentIndexChanged.connect(self._quality_chosen)
        row("quality", tr("Quality"), self.crf_box, 6)
        self.limit_box = QComboBox()
        for label, megabytes in TARGET_PRESETS.items():
            self.limit_box.addItem(tr(label), megabytes)
        self.limit_box.addItem(tr("Custom…"), None)
        self.limit_box.currentIndexChanged.connect(self._limit_chosen)
        row("limit", tr("Limit"), self.limit_box, 7)
        self.limit_spin = QDoubleSpinBox()
        self.limit_spin.setRange(*TARGET_RANGE)
        self.limit_spin.setDecimals(1)
        self.limit_spin.setSuffix(" MB")
        self.limit_spin.setKeyboardTracking(False)
        self.limit_spin.setValue(20.0)          # where Custom… starts: not one of the presets
        self.limit_spin.valueChanged.connect(
            lambda value: self._edit(lambda s: setattr(s.edit, "target_mb", round(value, 1)), tr("File size")))
        row("custom", tr("Size in MB"), self.limit_spin, 8)
        self.size_note = note(9)
        self.size_error = note(10, error=True)
        self.gif_fps_box = QComboBox()
        self.gif_fps_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "gif_fps", self.gif_fps_box.currentData()),
                               tr("GIF frame rate")))
        self.gif_fps_box.setToolTip(tr("The most frames a second the GIF gets. A video with fewer keeps its own."))
        row("gif_fps", tr("GIF frame rate"), self.gif_fps_box, 11)
        self.gif_width_box = QComboBox()
        self.gif_width_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "gif_width", self.gif_width_box.currentData()),
                               tr("GIF width")))
        self.gif_width_box.setToolTip(tr("The widest the GIF gets, in pixels. A narrower picture keeps its width."))
        row("gif_width", tr("GIF width"), self.gif_width_box, 12)
        for box, values, text in ((self.gif_fps_box, GIF_FPS_CHOICES, "{} fps"),
                                  (self.gif_width_box, GIF_WIDTH_CHOICES, "{} px")):
            for value in values:
                box.addItem(text.format(value), value)
        self.gpu_box = QCheckBox(tr("Use the graphics card when possible"))
        self.gpu_box.setToolTip(tr("Exports much faster on most PCs. The picture quality is about the same; "
                                   "if the graphics card fails, the export runs on the CPU instead."))
        self.gpu_box.toggled.connect(lambda on: self._edit(lambda s: setattr(s.edit, "use_gpu", on),
                                                           tr("Graphics card")))
        self.quality_section.grid.addWidget(self.gpu_box, 13, 0, 1, 2)
        self.gpu_label = QLabel()
        self.gpu_label.setObjectName("muted")
        self.gpu_label.setWordWrap(True)
        self.quality_section.grid.addWidget(self.gpu_label, 14, 0, 1, 2)
        self.quality_rows["gpu"] = [self.gpu_box, self.gpu_label]
        self.set_gpu_name(None, searching=True)

        # Export location (applies to every export, not one file)
        self.export_section = Section(tr("Save to"))
        self.folder_label = QLabel()
        self.folder_label.setWordWrap(True)
        self.export_section.grid.addWidget(self.folder_label, 0, 0, 1, 2)
        change = self.change_button = QPushButton(tr("Change…"))
        change.clicked.connect(self.outputFolderRequested)
        self.export_section.grid.addWidget(change, 1, 0, 1, 2)

        self.sections = [self.preset_section, self.output_section, self.frame_section, self.adjust_section,
                         self.trim_section,
                         self.speed_section, self.fade_section, self.captions_section, self.audio_section,
                         self.quality_section]
        for section in self.sections + [self.export_section]:
            layout.addWidget(section)
        layout.addStretch(1)
        for combo in self.findChildren(QComboBox):
            # Long option names shouldn't force the panel wider than the window allows.
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
        self.wheel_guard = WheelGuard(self)
        self.wheel_guard.watch(content)
        self.set_presets({})
        self.set_settings(None, None)

    # ----- public -------------------------------------------------------------

    def set_output_folder(self, text: str) -> None:
        self.folder_label.setText(text)

    def set_gpu_name(self, name: Optional[str], searching: bool = False) -> None:
        """Say which graphics card exports will use, or (name None) that there isn't one. While the
        search is still running (`searching`) the option stays off-limits."""
        self.gpu_box.setEnabled(bool(name))
        if searching:
            self.gpu_label.setText(tr("Looking for a graphics card…"))
        else:
            self.gpu_label.setText(name or tr("No supported graphics card found"))

    def set_frame_rate_hint(self, text: str) -> None:
        """Show (or, with "", hide) the note under Frame rate about a source that may not play on a phone."""
        self.rate_hint.setText(text)
        self.rate_hint.setVisible(bool(text) and not self._gif)

    def set_size_note(self, text: str, error: bool = False) -> None:
        """Show what fitting a file size will do to the picture, or (`error`) why it can't be done. "" hides it."""
        self._size_text, self._size_is_error = text, error
        self._refresh_size_note()

    def _refresh_size_note(self) -> None:
        shown = bool(self._size_text) and self._fits
        self.size_note.setText("" if self._size_is_error else self._size_text)
        self.size_note.setVisible(shown and not self._size_is_error)
        self.size_error.setText(self._size_text if self._size_is_error else "")
        self.size_error.setVisible(shown and self._size_is_error)

    def set_format_warning(self, text: str) -> None:
        """Show (or, with "", hide) a warning about the format, such as a GIF that will be big."""
        self.format_warning.setText(text)
        self.format_warning.setVisible(bool(text))

    def set_has_picture(self, has_picture: bool) -> None:
        """A file with no picture (a song) can only be saved as MP3: the Video choice is switched off."""
        self._has_picture = has_picture
        video_button = self.output_group.button(0)
        video_button.setEnabled(has_picture)
        video_button.setToolTip("" if has_picture else tr("This file has no picture, so it can only be saved as MP3"))

    def set_has_sound(self, has_sound: bool) -> None:
        """Remove silences needs a sound track to listen to."""
        self.silence_button.setEnabled(has_sound)
        self.silence_button.setToolTip(tr("Find the pauses in the sound and cut them out") if has_sound
                                       else tr("This file has no sound, so there are no silences to remove"))

    def set_reverse_warning(self, text: str) -> None:
        """Show (or, with "", hide) the note about how much memory playing the clip backwards needs."""
        self.reverse_warning.setText(text)
        self.reverse_warning.setVisible(bool(text))

    def set_presets(self, user_presets: dict[str, dict]) -> None:
        self.user_presets = user_presets
        self._updating = True
        try:
            self.preset_box.clear()
            self.preset_box.addItem(tr("Choose a preset…"), None)
            for name in PRESETS:
                self.preset_box.addItem(tr(name), ("builtin", name))
            for name in user_presets:
                self.preset_box.addItem(f"★ {name}", ("user", name))
        finally:
            self._updating = False

    def set_settings(self, settings: Optional[ClipSettings], duration: Optional[float]) -> None:
        """Show `settings` (None = nothing selected) without emitting edits."""
        self.settings = settings.copy() if settings else None
        self.duration = duration
        self.empty.setVisible(settings is None)
        for section in self.sections:
            section.setVisible(settings is not None)
        if settings is None:
            return

        self._updating = True
        try:
            edit = settings.edit
            video = settings.output == OUTPUT_VIDEO
            self.output_group.button(0 if video else 1).setChecked(True)
            self.frame_section.setVisible(video)
            self.adjust_section.setVisible(video)
            self.captions_section.setVisible(video)
            self.quality_section.setVisible(video)
            self.mute_box.setVisible(video)
            self.mp3_label.setVisible(not video)
            self.mp3_box.setVisible(not video)

            index = next(i for i, (_, aspect) in enumerate(SHAPES) if aspect == edit.aspect) \
                if any(aspect == edit.aspect for _, aspect in SHAPES) else 0
            self.shape_group.button(index).setChecked(True)
            self.shape_hint.setText(tr(SHAPE_HINTS[edit.aspect]) if edit.aspect in SHAPE_HINTS else "")
            self.fit_box.setCurrentIndex(max(self.fit_box.findData(edit.fit), 0))
            self.fit_box.setEnabled(edit.aspect is not None)
            if not self.zoom_slider.isSliderDown():
                self.zoom_slider.setValue(int(round(edit.zoom * 100)))
            self.zoom_slider.setEnabled(edit.crops)
            self.zoom_label.setText(f"{edit.zoom:.1f}×")
            self.mirror_box.setChecked(edit.flip)
            for name, (slider, value_label) in self.adjust_sliders.items():
                value = getattr(edit, name)
                if not slider.isSliderDown():
                    slider.setValue(round(value * 100))
                value_label.setText(f"{value:+.2f}" if name == "brightness" else f"{value:.2f}")
            look_index = list(LOOKS).index(edit.look) if edit.look in LOOKS else 0
            self.look_group.button(look_index).setChecked(True)
            self.stabilize_box.setCurrentIndex(max(self.stabilize_box.findData(edit.stabilize), 0))
            self._show_trim()
            self.speed_box.setValue(edit.speed)
            self.reverse_box.setChecked(edit.reverse)
            for name, box in self.fade_boxes.items():
                box.setValue(getattr(edit, name))
            for widget in self.fade_rows["Picture"]:
                widget.setVisible(video)
            self.captions.set_edit(edit)
            self.mute_box.setChecked(edit.mute)
            self.mp3_box.setCurrentIndex(max(self.mp3_box.findData(settings.audio_quality), 0))
            self._show_sound(settings)
            self.size_box.setCurrentIndex(max(self.size_box.findData(edit.resolution), 0))
            self.rate_box.setCurrentIndex(max(self.rate_box.findData(edit.frame_rate), 0))
            self.gpu_box.setChecked(edit.use_gpu)
            self._show_format(edit)
            self.preset_box.setCurrentIndex(0)
        finally:
            self._updating = False

    def _show_format(self, edit) -> None:
        """Show the format, the quality or file size, and the GIF options, and only the rows that go with them."""
        fmt = edit.output_format
        self.format_box.setCurrentIndex(max(self.format_box.findData(edit.video_format), 0))
        self.format_hint.setText(tr(fmt.hint) if fmt.hint else "")
        self.format_hint.setVisible(bool(fmt.hint))
        self._gif = fmt.codec == "gif"
        self._fits = edit.target_mb > 0 and not self._gif
        self.crf_box.setCurrentIndex(max(self.crf_box.findData(FIT_DATA if edit.target_mb > 0 else edit.crf), 0))
        preset = self.limit_box.findData(edit.target_mb)
        self.limit_box.setCurrentIndex(preset if preset >= 0 else self.limit_box.count() - 1)
        if edit.target_mb > 0 and preset < 0:
            self.limit_spin.setValue(edit.target_mb)       # a preset leaves the box as it was, for Custom… to start from
        for box, value in ((self.gif_fps_box, edit.gif_fps), (self.gif_width_box, edit.gif_width)):
            if box.findData(value) < 0:        # a value from a saved preset that the list doesn't offer
                box.addItem(f"{value} {'fps' if box is self.gif_fps_box else 'px'}", value)
            box.setCurrentIndex(box.findData(value))
        custom = self.limit_box.currentData() is None
        visible = {
            "rate": not self._gif, "quality": not self._gif, "limit": self._fits, "custom": self._fits and custom,
            "gif_fps": self._gif, "gif_width": self._gif, "gpu": fmt.codec in ("h264", "hevc"),
        }
        for name, shown in visible.items():
            for widget in self.quality_rows[name]:
                widget.setVisible(shown)
        if self._gif:
            self.rate_hint.hide()
        self._refresh_size_note()

    def _quality_chosen(self) -> None:
        data = self.crf_box.currentData()

        def change(s: ClipSettings) -> None:
            if data == FIT_DATA:
                s.edit.target_mb = s.edit.target_mb or DEFAULT_TARGET_MB
            else:
                s.edit.crf, s.edit.target_mb = data, 0.0
        self._edit(change, tr("File size") if data == FIT_DATA else tr("Quality"))

    def _limit_chosen(self) -> None:
        megabytes = self.limit_box.currentData()
        self._edit(lambda s: setattr(s.edit, "target_mb", megabytes if megabytes else round(self.limit_spin.value(), 1)),
                   tr("File size"))

    # ----- editing ------------------------------------------------------------

    def _edit(self, change: Callable[[ClipSettings], None], label: str, final: bool = True) -> None:
        if self._updating or self.settings is None:
            return
        new = self.settings.copy()
        change(new)
        # A finished slider drag still reports once, so listeners know the edit is complete.
        if new == self.settings and final and not self._dragging:
            return
        self._dragging = not final
        self.settings = new
        self.settingsEdited.emit(new.copy(), label, final)

    @staticmethod
    def _center(s: ClipSettings) -> None:
        s.edit.pos_x = s.edit.pos_y = 0.5

    @staticmethod
    def _reset_picture(s: ClipSettings) -> None:
        for name, (_, _, neutral) in ADJUSTMENTS.items():
            setattr(s.edit, name, neutral)
        s.edit.look = "none"

    @staticmethod
    def _clear_trim(s: ClipSettings) -> None:
        s.edit.start = s.edit.end = None

    @staticmethod
    def _clear_cuts(s: ClipSettings) -> None:
        s.edit.keep = []

    def _show_sound(self, settings: ClipSettings) -> None:
        edit = settings.edit
        muted = settings.output == OUTPUT_VIDEO and edit.mute      # Mute wins over everything below
        if not self.volume_slider.isSliderDown():
            self.volume_slider.setValue(round(edit.volume))
        self.volume_label.setText(_db_text(round(edit.volume)))
        self.normalize_box.setChecked(edit.normalize)
        self.noise_box.setCurrentIndex(max(self.noise_box.findData(edit.audio_denoise), 0))
        if not self.music_slider.isSliderDown():
            self.music_slider.setValue(round(edit.music_volume))
        self.music_label.setText(_db_text(round(edit.music_volume)))
        self.duck_box.setChecked(edit.music_duck)
        name = Path(edit.music).name if edit.music else ""
        self.music_name.setText(_short_name(name) if name else tr("None"))
        self.music_name.setToolTip(edit.music)
        self.music_remove.setEnabled(bool(edit.music))
        self.music_missing.setVisible(bool(edit.music) and not Path(edit.music).is_file())
        for widget in self.sound_options:
            widget.setEnabled(not muted)
        for widget in (self.music_holder, self.duck_box):
            widget.setEnabled(not muted and bool(edit.music))

    @staticmethod
    def _slider_row(slider: QSlider, label: QLabel) -> QWidget:
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(slider)
        line.addWidget(label)
        holder = QWidget()
        holder.setLayout(line)
        return holder

    def _choose_music(self) -> None:
        current = self.settings.edit.music if self.settings else ""
        patterns = " ".join(f"*{extension}" for extension in AUDIO_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Choose background music"), str(Path(current).parent) if current else "",
            tr("Sound files ({patterns})", patterns=patterns) + ";;" + tr("All files (*)"))
        if path:
            self._edit(lambda s: setattr(s.edit, "music", str(Path(path))), tr("Choose music"))

    def _show_trim(self) -> None:
        edit = self.settings.edit
        self.start_edit.setText(format_time(edit.start) if edit.start is not None else "")
        self.end_edit.setText(format_time(edit.end) if edit.end is not None else "")
        if self.duration:
            length = output_duration(edit.segments(self.duration), edit.speed) or 0.0
            self.trim_info.setText(tr("Result {length} of {total}", length=format_time(length),
                                      total=format_time(self.duration)))
        else:
            self.trim_info.setText("")
        gaps = removed_parts(edit.keep, self.duration)
        if gaps:
            # Each range is isolated, so "0:10.0–0:12.0" keeps its order inside an Arabic sentence.
            shown = join_parts([f"{format_time(a)}–{format_time(b)}" for a, b in gaps[:4]], tr(", "))
            more = " " + tr("and {n} more", n=len(gaps) - 4) if len(gaps) > 4 else ""
            self.cut_info.setText(tr("Cut out: {parts}{more}", parts=shown, more=more))
        elif edit.keep:
            self.cut_info.setText(tr("Split. Put the playhead on a part and click Remove part to cut it out."))
        else:
            self.cut_info.setText(tr("Split at the playhead, then remove the parts you don't want."))
        self.restore_button.setEnabled(bool(edit.keep))

    def _trim_typed(self, which: str) -> None:
        if self._updating or self.settings is None:
            return
        widget = self.start_edit if which == "start" else self.end_edit
        current = getattr(self.settings.edit, which)
        if widget.text().strip() == (format_time(current) if current is not None else ""):
            # Leaving the field untouched must not round a time set elsewhere to the one decimal shown.
            self.trim_error.hide()
            return
        try:
            value = parse_time(widget.text())
        except ValueError as exc:
            self.trim_error.setText(str(exc))
            self.trim_error.show()
            return
        start = value if which == "start" else self.settings.edit.start
        end = value if which == "end" else self.settings.edit.end
        if self.duration and value is not None and value > self.duration:
            self.trim_error.setText(tr("The video is only {length} long.", length=format_time(self.duration)))
            self.trim_error.show()
            return
        if start is not None and end is not None and end <= start:
            self.trim_error.setText(tr("The end has to be after the start."))
            self.trim_error.show()
            return
        self.trim_error.hide()
        self._edit(lambda s: setattr(s.edit, which, value), tr("Trim"))

    def _preset_chosen(self, index: int) -> None:
        data = self.preset_box.itemData(index)
        if self._updating or data is None or self.settings is None:
            return
        kind, name = data
        from ..project import apply_preset, apply_to

        if kind == "builtin":
            new = apply_preset(self.settings, PRESETS[name], self._has_picture)
        else:
            new = apply_to(self.settings, ClipSettings.from_dict(self.user_presets[name]))
            if not self._has_picture:
                new.output = OUTPUT_AUDIO
        self.settings = new
        self.settingsEdited.emit(new.copy(), tr("Preset: {name}", name=tr(name) if kind == "builtin" else name), True)
        self._updating = True
        self.preset_box.setCurrentIndex(0)
        self._updating = False
