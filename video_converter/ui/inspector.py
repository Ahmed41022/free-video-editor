"""The settings panel on the right: collapsible sections that edit one file's ClipSettings."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from ..core import QUALITY_PRESETS, format_time, parse_time
from ..project import OUTPUT_AUDIO, OUTPUT_VIDEO, PRESETS, ClipSettings
from ..timing import FADE_MAX, SPEED_MAX, SPEED_MIN, output_duration, removed_parts
from ..video import ADJUSTMENTS, FIT_MODES, FRAME_RATES, LOOKS, RESOLUTIONS, STABILIZE, VIDEO_QUALITY

ADJUST_LABELS = {"brightness": "Brightness", "contrast": "Contrast", "saturation": "Saturation",
                 "gamma": "Gamma", "sharpen": "Sharpen", "denoise": "Denoise"}
STABILIZE_LABELS = {"off": "Off", "light": "Light", "strong": "Strong"}
LOOK_CHIPS = {"bw": "B&W", "contrast": "Contrast"}      # short names that fit the panel

SHAPES = [("Original", None), ("9:16", (9, 16)), ("1:1", (1, 1)), ("4:5", (4, 5)), ("16:9", (16, 9))]
SHAPE_HINTS = {None: "Keep the video's own shape", (9, 16): "Phone: Stories, Reels, TikTok, Shorts",
               (1, 1): "Square post", (4, 5): "Portrait post", (16, 9): "Widescreen, YouTube"}


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
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
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

    def _toggle(self, expanded: bool) -> None:
        self.body.setVisible(expanded)
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def row(self, label: str, widget: QWidget, row: int) -> None:
        text = QLabel(label)
        text.setObjectName("muted")
        self.grid.addWidget(text, row, 0)
        self.grid.addWidget(widget, row, 1)


class Inspector(QScrollArea):
    # new settings, undo label, whether the edit is complete (False while dragging a slider)
    settingsEdited = Signal(object, str, bool)
    applyToAllRequested = Signal()
    savePresetRequested = Signal()
    playheadRequested = Signal(str)          # "start" or "end": use the current playhead time
    cutRequested = Signal(str)               # "split" or "remove": at the current playhead
    outputFolderRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(330)
        self.settings: Optional[ClipSettings] = None
        self.duration: Optional[float] = None
        self._updating = False
        self._dragging = False
        self.user_presets: dict[str, dict] = {}

        content = QWidget()
        self.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.empty = QLabel("Select a video to edit its settings.")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        # Presets
        self.preset_section = Section("Presets")
        self.preset_box = QComboBox()
        self.preset_box.currentIndexChanged.connect(self._preset_chosen)
        self.preset_section.grid.addWidget(self.preset_box, 0, 0, 1, 2)
        buttons = QVBoxLayout()
        save = QPushButton("Save as preset…")
        save.clicked.connect(self.savePresetRequested)
        apply_all = QPushButton("Apply to all files")
        apply_all.setToolTip("Copy these settings to every file in the list (each keeps its own crop and trim)")
        apply_all.clicked.connect(self.applyToAllRequested)
        buttons.addWidget(save)
        buttons.addWidget(apply_all)
        self.preset_section.grid.addLayout(buttons, 1, 0, 1, 2)

        # Output
        self.output_section = Section("Output")
        out_row = QHBoxLayout()
        self.output_group = QButtonGroup(self)
        for i, (text, value) in enumerate((("Video (MP4)", OUTPUT_VIDEO), ("Audio (MP3)", OUTPUT_AUDIO))):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setProperty("value", value)
            self.output_group.addButton(button, i)
            out_row.addWidget(button)
        self.output_group.idClicked.connect(
            lambda i: self._edit(lambda s: setattr(s, "output", self.output_group.button(i).property("value")),
                                 "Change output"))
        self.output_section.grid.addLayout(out_row, 0, 0, 1, 2)

        # Frame
        self.frame_section = Section("Frame")
        shapes = QHBoxLayout()
        shapes.setSpacing(4)
        self.shape_group = QButtonGroup(self)
        for i, (text, aspect) in enumerate(SHAPES):
            button = QPushButton(text)
            button.setObjectName("chip")
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            button.setCheckable(True)
            button.setToolTip(SHAPE_HINTS[aspect])
            self.shape_group.addButton(button, i)
            shapes.addWidget(button)
        self.shape_group.idClicked.connect(
            lambda i: self._edit(lambda s: setattr(s.edit, "aspect", SHAPES[i][1]), "Change shape"))
        self.frame_section.grid.addLayout(shapes, 0, 0, 1, 2)
        self.shape_hint = QLabel()
        self.shape_hint.setObjectName("muted")
        self.frame_section.grid.addWidget(self.shape_hint, 1, 0, 1, 2)

        self.fit_box = QComboBox()
        for key, label in FIT_MODES.items():
            self.fit_box.addItem(label, key)
        self.fit_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "fit", self.fit_box.currentData()), "Change fit"))
        self.frame_section.row("Fit", self.fit_box, 2)

        zoom_row = QHBoxLayout()
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(100, 300)
        self.zoom_label = QLabel("1.0×")
        self.zoom_label.setMinimumWidth(36)
        self.zoom_slider.valueChanged.connect(
            lambda v: self._edit(lambda s: setattr(s.edit, "zoom", v / 100), "Zoom",
                                 final=not self.zoom_slider.isSliderDown()))
        self.zoom_slider.sliderReleased.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "zoom", self.zoom_slider.value() / 100), "Zoom"))
        zoom_row.addWidget(self.zoom_slider)
        zoom_row.addWidget(self.zoom_label)
        zoom_widget = QWidget()
        zoom_widget.setLayout(zoom_row)
        zoom_row.setContentsMargins(0, 0, 0, 0)
        self.frame_section.row("Zoom", zoom_widget, 3)

        rotate_row = QHBoxLayout()
        rotate_row.setContentsMargins(0, 0, 0, 0)
        left = QPushButton("⟲ Left")
        right = QPushButton("⟳ Right")
        left.clicked.connect(lambda: self._edit(lambda s: setattr(s.edit, "rotate", (s.edit.rotate - 90) % 360),
                                                "Rotate"))
        right.clicked.connect(lambda: self._edit(lambda s: setattr(s.edit, "rotate", (s.edit.rotate + 90) % 360),
                                                 "Rotate"))
        self.mirror_box = QCheckBox("Mirror")
        self.mirror_box.toggled.connect(lambda on: self._edit(lambda s: setattr(s.edit, "flip", on), "Mirror"))
        rotate_row.addWidget(left)
        rotate_row.addWidget(right)
        rotate_row.addWidget(self.mirror_box)
        rotate_widget = QWidget()
        rotate_widget.setLayout(rotate_row)
        self.frame_section.row("Rotate", rotate_widget, 4)

        center = QPushButton("Center the crop")
        center.clicked.connect(lambda: self._edit(self._center, "Center crop"))
        self.frame_section.grid.addWidget(center, 5, 1)

        # Adjust
        self.adjust_section = Section("Adjust")
        self.adjust_sliders: dict[str, tuple[QSlider, QLabel]] = {}
        for row, name in enumerate(ADJUSTMENTS):
            low, high, _ = ADJUSTMENTS[name]
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(round(low * 100), round(high * 100))
            value_label = QLabel()
            value_label.setMinimumWidth(36)
            slider.valueChanged.connect(
                lambda v, name=name, slider=slider: self._edit(
                    lambda s: setattr(s.edit, name, v / 100), ADJUST_LABELS[name], final=not slider.isSliderDown()))
            slider.sliderReleased.connect(
                lambda name=name, slider=slider: self._edit(
                    lambda s: setattr(s.edit, name, slider.value() / 100), ADJUST_LABELS[name]))
            line = QHBoxLayout()
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(slider)
            line.addWidget(value_label)
            holder = QWidget()
            holder.setLayout(line)
            self.adjust_section.row(ADJUST_LABELS[name], holder, row)
            self.adjust_sliders[name] = (slider, value_label)
        looks = QGridLayout()
        looks.setSpacing(4)
        self.look_group = QButtonGroup(self)
        for i, (key, (label, _)) in enumerate(LOOKS.items()):
            button = QPushButton(LOOK_CHIPS.get(key, label).replace("&", "&&"))
            button.setObjectName("chip")
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setCheckable(True)
            button.setToolTip(label)
            button.setProperty("look", key)
            self.look_group.addButton(button, i)
            looks.addWidget(button, i // 3, i % 3)
        self.look_group.idClicked.connect(
            lambda i: self._edit(lambda s: setattr(s.edit, "look", self.look_group.button(i).property("look")),
                                 "Look"))
        looks_holder = QWidget()
        looks_holder.setLayout(looks)
        looks.setContentsMargins(0, 0, 0, 0)
        row = len(ADJUSTMENTS)
        self.adjust_section.row("Look", looks_holder, row)
        reset = QPushButton("Reset the picture")
        reset.setToolTip("Put every adjustment and the look back to normal")
        reset.clicked.connect(lambda: self._edit(self._reset_picture, "Reset picture"))
        self.adjust_section.grid.addWidget(reset, row + 1, 1)
        self.stabilize_box = QComboBox()
        for key, label in STABILIZE_LABELS.items():
            self.stabilize_box.addItem(label, key)
        self.stabilize_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "stabilize", self.stabilize_box.currentData()), "Stabilize"))
        self.adjust_section.row("Stabilize", self.stabilize_box, row + 2)
        stabilize_hint = QLabel("Steadies shaky footage when exporting, which takes longer. "
                                "The preview doesn't show it.")
        stabilize_hint.setObjectName("muted")
        stabilize_hint.setWordWrap(True)
        self.adjust_section.grid.addWidget(stabilize_hint, row + 3, 1)

        # Trim and cut
        self.trim_section = Section("Trim and cut")
        self.start_edit = QLineEdit()
        self.end_edit = QLineEdit()
        for edit, which in ((self.start_edit, "start"), (self.end_edit, "end")):
            edit.setPlaceholderText("Beginning" if which == "start" else "End")
            edit.editingFinished.connect(lambda which=which: self._trim_typed(which))
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            here = QPushButton("Use playhead")
            here.setToolTip("Keyboard: I sets the start, O sets the end")
            here.clicked.connect(lambda _=False, which=which: self.playheadRequested.emit(which))
            row.addWidget(edit)
            row.addWidget(here)
            holder = QWidget()
            holder.setLayout(row)
            self.trim_section.row("Start" if which == "start" else "End", holder, 0 if which == "start" else 1)
        self.trim_info = QLabel()
        self.trim_info.setObjectName("muted")
        self.trim_section.grid.addWidget(self.trim_info, 2, 1)
        clear = QPushButton("Clear the trim")
        clear.clicked.connect(lambda: self._edit(self._clear_trim, "Clear trim"))
        self.trim_section.grid.addWidget(clear, 3, 1)
        self.trim_error = QLabel()
        self.trim_error.setObjectName("error")
        self.trim_error.setWordWrap(True)
        self.trim_error.hide()
        self.trim_section.grid.addWidget(self.trim_error, 4, 0, 1, 2)

        cut_row = QHBoxLayout()
        cut_row.setContentsMargins(0, 0, 0, 0)
        self.split_button = QPushButton("Split")
        self.split_button.setToolTip("Split the video at the playhead (S)")
        self.split_button.clicked.connect(lambda: self.cutRequested.emit("split"))
        self.remove_part_button = QPushButton("Remove part")
        self.remove_part_button.setToolTip("Cut out the part under the playhead, between two splits (X)")
        self.remove_part_button.clicked.connect(lambda: self.cutRequested.emit("remove"))
        cut_row.addWidget(self.split_button)
        cut_row.addWidget(self.remove_part_button)
        cut_holder = QWidget()
        cut_holder.setLayout(cut_row)
        self.trim_section.row("Cut", cut_holder, 5)
        self.cut_info = QLabel()
        self.cut_info.setObjectName("muted")
        self.cut_info.setWordWrap(True)
        self.trim_section.grid.addWidget(self.cut_info, 6, 1)
        self.restore_button = QPushButton("Put back the cut parts")
        self.restore_button.clicked.connect(lambda: self._edit(self._clear_cuts, "Restore cuts"))
        self.trim_section.grid.addWidget(self.restore_button, 7, 1)

        # Speed
        self.speed_section = Section("Speed")
        self.speed_box = QDoubleSpinBox()
        self.speed_box.setRange(SPEED_MIN, SPEED_MAX)
        self.speed_box.setSingleStep(0.25)
        self.speed_box.setDecimals(2)
        self.speed_box.setSuffix("×")
        self.speed_box.setKeyboardTracking(False)
        self.speed_box.setToolTip("0.25× is four times slower, 4× four times faster; the sound keeps its pitch")
        self.speed_box.valueChanged.connect(
            lambda value: self._edit(lambda s: setattr(s.edit, "speed", round(value, 2)), "Speed"))
        speed_row = QHBoxLayout()
        speed_row.setContentsMargins(0, 0, 0, 0)
        speed_row.addWidget(self.speed_box, 1)
        for value in (0.5, 1.0, 2.0):
            chip = QPushButton(f"{value:g}×")
            chip.setObjectName("chip")
            chip.setMinimumWidth(0)
            chip.clicked.connect(lambda _=False, value=value: self.speed_box.setValue(value))
            speed_row.addWidget(chip)
        speed_holder = QWidget()
        speed_holder.setLayout(speed_row)
        self.speed_section.row("Speed", speed_holder, 0)
        self.reverse_box = QCheckBox("Play backwards")
        self.reverse_box.setToolTip("Reversing holds the whole clip in memory, so it suits short clips best")
        self.reverse_box.toggled.connect(lambda on: self._edit(lambda s: setattr(s.edit, "reverse", on), "Reverse"))
        self.speed_section.grid.addWidget(self.reverse_box, 1, 1)
        self.reverse_warning = QLabel()
        self.reverse_warning.setObjectName("error")
        self.reverse_warning.setWordWrap(True)
        self.reverse_warning.hide()
        self.speed_section.grid.addWidget(self.reverse_warning, 2, 1)

        # Fades
        self.fade_section = Section("Fades")
        self.fade_boxes: dict[str, QDoubleSpinBox] = {}
        self.fade_rows: dict[str, tuple[QLabel, QWidget]] = {}
        rows = (("Picture", "fade_in", "fade_out"), ("Sound", "audio_fade_in", "audio_fade_out"))
        for row_index, (label, fade_in, fade_out) in enumerate(rows):
            line = QHBoxLayout()
            line.setContentsMargins(0, 0, 0, 0)
            for name, word in ((fade_in, "in"), (fade_out, "out")):
                box = QDoubleSpinBox()
                box.setRange(0.0, FADE_MAX)
                box.setSingleStep(0.5)
                box.setDecimals(1)
                box.setSuffix(" s")
                box.setSpecialValueText("Off")
                box.setKeyboardTracking(False)
                box.setToolTip("Seconds to fade in from black or silence" if word == "in"
                               else "Seconds to fade out to black or silence")
                box.valueChanged.connect(
                    lambda value, name=name: self._edit(lambda s: setattr(s.edit, name, round(value, 1)), "Fade"))
                caption = QLabel(word.capitalize())
                caption.setObjectName("muted")
                line.addWidget(caption)
                line.addWidget(box, 1)
                self.fade_boxes[name] = box
            holder = QWidget()
            holder.setLayout(line)
            self.fade_section.row(label, holder, row_index)
            self.fade_rows[label] = (self.fade_section.grid.itemAtPosition(row_index, 0).widget(), holder)

        # Sound
        self.audio_section = Section("Sound")
        self.mute_box = QCheckBox("Remove the sound")
        self.mute_box.toggled.connect(lambda on: self._edit(lambda s: setattr(s.edit, "mute", on), "Remove sound"))
        self.audio_section.grid.addWidget(self.mute_box, 0, 0, 1, 2)
        self.mp3_box = QComboBox()
        self.mp3_box.addItems(list(QUALITY_PRESETS))
        self.mp3_box.currentTextChanged.connect(
            lambda text: self._edit(lambda s: setattr(s, "audio_quality", text), "MP3 quality"))
        self.mp3_label = QLabel("MP3 quality")
        self.mp3_label.setObjectName("muted")
        self.audio_section.grid.addWidget(self.mp3_label, 1, 0)
        self.audio_section.grid.addWidget(self.mp3_box, 1, 1)

        # Quality
        self.quality_section = Section("Video quality")
        self.size_box = QComboBox()
        for label, value in RESOLUTIONS.items():
            self.size_box.addItem(label, value)
        self.size_box.setToolTip(
            "Same as source matches the source's short side, so 1080p makes a 1080x1920 phone video. Phone, "
            "portrait and square videos are capped at 1080 pixels on the short side, so 4K doesn't make huge "
            "files. Up to 1080p does the same for any shape, and never makes a smaller video bigger. "
            "1080p, 720p and 480p set the short side.")
        self.size_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "resolution", self.size_box.currentData()), "Size"))
        self.quality_section.row("Size", self.size_box, 0)
        self.rate_box = QComboBox()
        for label, value in FRAME_RATES.items():
            self.rate_box.addItem(label, value)
        self.rate_box.setToolTip(
            "Steady makes every frame the same distance apart, which phones need. A steady export is also "
            "made phone-safe (H.264 High, AAC sound). A video that is slower than the rate you choose keeps "
            "its own rate, so Steady 60 never makes a 30 fps video bigger.")
        self.rate_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "frame_rate", self.rate_box.currentData()), "Frame rate"))
        self.quality_section.row("Frame rate", self.rate_box, 1)
        self.rate_hint = QLabel()
        self.rate_hint.setObjectName("muted")
        self.rate_hint.setWordWrap(True)
        self.rate_hint.hide()
        self.quality_section.grid.addWidget(self.rate_hint, 2, 1)
        self.crf_box = QComboBox()
        for label, value in VIDEO_QUALITY.items():
            self.crf_box.addItem(label, value)
        self.crf_box.currentIndexChanged.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "crf", self.crf_box.currentData()), "Quality"))
        self.quality_section.row("Quality", self.crf_box, 3)
        self.gpu_box = QCheckBox("Use the graphics card when possible")
        self.gpu_box.setToolTip("Exports much faster on most PCs. The picture quality is about the same; "
                                "if the graphics card fails, the export runs on the CPU instead.")
        self.gpu_box.toggled.connect(lambda on: self._edit(lambda s: setattr(s.edit, "use_gpu", on), "Graphics card"))
        self.quality_section.grid.addWidget(self.gpu_box, 4, 0, 1, 2)
        self.gpu_label = QLabel()
        self.gpu_label.setObjectName("muted")
        self.gpu_label.setWordWrap(True)
        self.quality_section.grid.addWidget(self.gpu_label, 5, 0, 1, 2)
        self.set_gpu_name(None, searching=True)

        # Export location (applies to every export, not one file)
        self.export_section = Section("Save to")
        self.folder_label = QLabel()
        self.folder_label.setWordWrap(True)
        self.export_section.grid.addWidget(self.folder_label, 0, 0, 1, 2)
        change = QPushButton("Change…")
        change.clicked.connect(self.outputFolderRequested)
        self.export_section.grid.addWidget(change, 1, 0, 1, 2)

        for combo in self.findChildren(QComboBox):
            # Long option names shouldn't force the panel wider than the window allows.
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)

        self.sections = [self.preset_section, self.output_section, self.frame_section, self.adjust_section,
                         self.trim_section,
                         self.speed_section, self.fade_section, self.audio_section, self.quality_section]
        for section in self.sections + [self.export_section]:
            layout.addWidget(section)
        layout.addStretch(1)
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
            self.gpu_label.setText("Looking for a graphics card…")
        else:
            self.gpu_label.setText(name or "No supported graphics card found")

    def set_frame_rate_hint(self, text: str) -> None:
        """Show (or, with "", hide) the note under Frame rate about a source that may not play on a phone."""
        self.rate_hint.setText(text)
        self.rate_hint.setVisible(bool(text))

    def set_reverse_warning(self, text: str) -> None:
        """Show (or, with "", hide) the note about how much memory playing the clip backwards needs."""
        self.reverse_warning.setText(text)
        self.reverse_warning.setVisible(bool(text))

    def set_presets(self, user_presets: dict[str, dict]) -> None:
        self.user_presets = user_presets
        self._updating = True
        try:
            self.preset_box.clear()
            self.preset_box.addItem("Choose a preset…", None)
            for name in PRESETS:
                self.preset_box.addItem(name, ("builtin", name))
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
            self.quality_section.setVisible(video)
            self.mute_box.setVisible(video)
            self.mp3_label.setVisible(not video)
            self.mp3_box.setVisible(not video)

            index = next(i for i, (_, aspect) in enumerate(SHAPES) if aspect == edit.aspect) \
                if any(aspect == edit.aspect for _, aspect in SHAPES) else 0
            self.shape_group.button(index).setChecked(True)
            self.shape_hint.setText(SHAPE_HINTS.get(edit.aspect, ""))
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
            self.mute_box.setChecked(edit.mute)
            self.mp3_box.setCurrentText(settings.audio_quality)
            self.size_box.setCurrentIndex(max(self.size_box.findData(edit.resolution), 0))
            self.rate_box.setCurrentIndex(max(self.rate_box.findData(edit.frame_rate), 0))
            self.crf_box.setCurrentIndex(max(self.crf_box.findData(edit.crf), 0))
            self.gpu_box.setChecked(edit.use_gpu)
            self.preset_box.setCurrentIndex(0)
        finally:
            self._updating = False

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

    def _show_trim(self) -> None:
        edit = self.settings.edit
        self.start_edit.setText(format_time(edit.start) if edit.start is not None else "")
        self.end_edit.setText(format_time(edit.end) if edit.end is not None else "")
        if self.duration:
            length = output_duration(edit.segments(self.duration), edit.speed) or 0.0
            self.trim_info.setText(f"Result {format_time(length)} of {format_time(self.duration)}")
        else:
            self.trim_info.setText("")
        gaps = removed_parts(edit.keep, self.duration)
        if gaps:
            shown = ", ".join(f"{format_time(a)}–{format_time(b)}" for a, b in gaps[:4])
            more = f" and {len(gaps) - 4} more" if len(gaps) > 4 else ""
            self.cut_info.setText(f"Cut out: {shown}{more}")
        elif edit.keep:
            self.cut_info.setText("Split. Put the playhead on a part and click Remove part to cut it out.")
        else:
            self.cut_info.setText("Split at the playhead, then remove the parts you don't want.")
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
            self.trim_error.setText(f"The video is only {format_time(self.duration)} long.")
            self.trim_error.show()
            return
        if start is not None and end is not None and end <= start:
            self.trim_error.setText("The end has to be after the start.")
            self.trim_error.show()
            return
        self.trim_error.hide()
        self._edit(lambda s: setattr(s.edit, which, value), "Trim")

    def _preset_chosen(self, index: int) -> None:
        data = self.preset_box.itemData(index)
        if self._updating or data is None or self.settings is None:
            return
        kind, name = data
        from ..project import apply_preset, apply_to

        if kind == "builtin":
            new = apply_preset(self.settings, PRESETS[name])
        else:
            new = apply_to(self.settings, ClipSettings.from_dict(self.user_presets[name]))
        self.settings = new
        self.settingsEdited.emit(new.copy(), f"Preset: {name}", True)
        self._updating = True
        self.preset_box.setCurrentIndex(0)
        self._updating = False
