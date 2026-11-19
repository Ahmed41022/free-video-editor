"""The settings panel: its sections."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from ...core import QUALITY_PRESETS
from ...i18n import N_, tr
from ...output import (
    GIF_FPS_CHOICES, GIF_WIDTH_CHOICES, OUTPUT_FORMATS, TARGET_PRESETS, TARGET_RANGE,
)
from ...project import OUTPUT_AUDIO, OUTPUT_VIDEO, ClipSettings
from ...sound import MUSIC_VOLUME_MAX, MUSIC_VOLUME_MIN, VOLUME_MAX, VOLUME_MIN
from ...timing import FADE_MAX, SPEED_MAX, SPEED_MIN
from ...video import ADJUSTMENTS, FIT_MODES, FRAME_RATES, LOOKS, RESOLUTIONS, VIDEO_QUALITY
from ..captions_panel import CaptionsPanel
from .choices import (
    ADJUST_LABELS, FIT_DATA, FIT_LABEL, LOOK_CHIPS, NOISE_LABELS, SHAPES, SHAPE_HINTS,
    STABILIZE_LABELS,
)
from .editing import EditingMixin
from .showing import ShowingMixin
from .widgets import Section, WheelGuard, _db_text


class Inspector(ShowingMixin, EditingMixin, QScrollArea):
    # new settings, undo label, whether the edit is complete (False while dragging a slider)
    settingsEdited = Signal(object, str, bool)
    applyToAllRequested = Signal()
    savePresetRequested = Signal()
    playheadRequested = Signal(str)  # "start" or "end": use the current playhead time
    cutRequested = Signal(str)  # "split" or "remove": at the current playhead
    removeSilencesRequested = Signal()
    outputFolderRequested = Signal()
    textSelected = Signal(int)  # the selected text's index (-1: none), to show it on the preview
    textTimeRequested = Signal(
        str)  # "start" or "end": use the playhead as the selected text's time
    autoCaptionsRequested = Signal()  # make captions from the speech
    editCaptionsRequested = Signal()  # open the caption editor

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(330)
        self.settings: Optional[ClipSettings] = None
        self.duration: Optional[float] = None
        self._updating = False
        self._dragging = False
        self._gif = False  # the format is GIF, which has its own options
        self._fits = False  # the quality is Fit in a file size
        self._has_picture = True  # False for a sound-only file, which can only be saved as MP3
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
        apply_all.setToolTip(
            tr("Copy these settings to every file in the list (each keeps its own crop and trim)"))
        apply_all.clicked.connect(self.applyToAllRequested)
        buttons.addWidget(save)
        buttons.addWidget(apply_all)
        self.preset_section.grid.addLayout(buttons, 1, 0, 1, 2)

        # Output
        self.output_section = Section(tr("Output"))
        out_row = QHBoxLayout()
        self.output_group = QButtonGroup(self)
        for i, (text, value) in enumerate(
            ((N_("Video"), OUTPUT_VIDEO), (N_("Audio (MP3)"), OUTPUT_AUDIO))):
            button = QPushButton(tr(text))
            button.setCheckable(True)
            button.setProperty("value", value)
            self.output_group.addButton(button, i)
            out_row.addWidget(button)
        self.output_group.idClicked.connect(
            lambda i: self._edit(
                lambda s: setattr(s, "output",
                                  self.output_group.button(i).property("value")),
                tr("Change output")))
        self.output_section.grid.addLayout(out_row, 0, 0, 1, 2)

        # Frame
        self.frame_section = Section(tr("Frame"))
        shapes = QGridLayout()  # three to a row: five side by side would not fit the panel
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
            lambda i: self._edit(
                lambda s: setattr(s.edit, "aspect", SHAPES[i][1]), tr("Change shape")))
        self.frame_section.grid.addLayout(shapes, 0, 0, 1, 2)
        self.shape_hint = QLabel()
        self.shape_hint.setObjectName("muted")
        self.frame_section.grid.addWidget(self.shape_hint, 1, 0, 1, 2)

        self.fit_box = QComboBox()
        for key, label in FIT_MODES.items():
            self.fit_box.addItem(tr(label), key)
        self.fit_box.currentIndexChanged.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "fit", self.fit_box.currentData()), tr("Change fit")))
        self.frame_section.row(tr("Fit"), self.fit_box, 2)

        zoom_row = QHBoxLayout()
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(100, 300)
        self.zoom_label = QLabel("1.0×")
        self.zoom_label.setMinimumWidth(36)
        self.zoom_slider.valueChanged.connect(
            lambda v: self._edit(
                lambda s: setattr(s.edit, "zoom", v / 100), tr("Zoom"), final=not self.zoom_slider.
                isSliderDown()))
        self.zoom_slider.sliderReleased.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "zoom",
                                  self.zoom_slider.value() / 100), tr("Zoom")))
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
        left.clicked.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "rotate", (s.edit.rotate - 90) % 360), tr("Rotate")))
        right.clicked.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "rotate", (s.edit.rotate + 90) % 360), tr("Rotate")))
        for button in (left, right):
            button.setMinimumWidth(44)  # a real minimum: the style's 75 px would not fit the panel
        self.mirror_box = QCheckBox(tr("Mirror"))
        self.mirror_box.toggled.connect(
            lambda on: self._edit(lambda s: setattr(s.edit, "flip", on), tr("Mirror")))
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
                    lambda s: setattr(s.edit, name,
                                      slider.value() / 100), tr(ADJUST_LABELS[name])))
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
            lambda i: self._edit(
                lambda s: setattr(s.edit, "look",
                                  self.look_group.button(i).property("look")), tr("Look")))
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
            lambda: self._edit(
                lambda s: setattr(s.edit, "stabilize", self.stabilize_box.currentData()),
                tr("Stabilize")))
        self.adjust_section.row(tr("Stabilize"), self.stabilize_box, row + 2)
        stabilize_hint = QLabel(
            tr(
                "Steadies shaky footage when exporting, which takes longer. "
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
            self.trim_section.row(
                tr("Start") if which == "start" else tr("End"), holder,
                0 if which == "start" else 1)
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
        self.remove_part_button.setToolTip(
            tr("Cut out the part under the playhead, between two splits (X)"))
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
        self.restore_button.clicked.connect(
            lambda: self._edit(self._clear_cuts, tr("Restore cuts")))
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
        self.speed_box.setToolTip(
            tr("0.25× is four times slower, 4× four times faster; the sound keeps its pitch"))
        self.speed_box.valueChanged.connect(
            lambda value: self._edit(
                lambda s: setattr(s.edit, "speed", round(value, 2)), tr("Speed")))
        speed_row = QHBoxLayout()
        speed_row.setContentsMargins(0, 0, 0, 0)
        speed_row.addWidget(self.speed_box, 1)
        for value in (0.5, 1.0, 2.0):
            chip = QPushButton(f"{value:g}×")
            chip.setObjectName("chip")
            chip.setMinimumWidth(36)  # a real minimum: the style's 75 px would not fit the panel
            chip.clicked.connect(lambda _=False, value=value: self.speed_box.setValue(value))
            speed_row.addWidget(chip)
        speed_holder = QWidget()
        speed_holder.setLayout(speed_row)
        self.speed_section.row(tr("Speed"), speed_holder, 0)
        self.reverse_box = QCheckBox(tr("Play backwards"))
        self.reverse_box.setToolTip(
            tr("Reversing holds the whole clip in memory, so it suits short clips best"))
        self.reverse_box.toggled.connect(
            lambda on: self._edit(lambda s: setattr(s.edit, "reverse", on), tr("Reverse")))
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
        rows = (
            (N_("Picture"), "fade_in", "fade_out"),
            (N_("Sound"), "audio_fade_in", "audio_fade_out"))
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
                box.setToolTip(
                    tr("Seconds to fade in from black or silence") if word ==
                    "in" else tr("Seconds to fade out to black or silence"))
                box.valueChanged.connect(
                    lambda value, name=name: self._edit(
                        lambda s: setattr(s.edit, name, round(value, 1)), tr("Fade")))
                caption = QLabel(tr("In") if word == "in" else tr("Out"))
                caption.setObjectName("muted")
                line.addWidget(caption)
                line.addWidget(box, 1)
                self.fade_boxes[name] = box
            holder = QWidget()
            holder.setLayout(line)
            self.fade_section.row(tr(label), holder, row_index)
            self.fade_rows[label] = (
                self.fade_section.grid.itemAtPosition(row_index, 0).widget(), holder)

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
        self.mute_box.toggled.connect(
            lambda on: self._edit(lambda s: setattr(s.edit, "mute", on), tr("Remove sound")))
        self.audio_section.grid.addWidget(self.mute_box, 0, 0, 1, 2)
        self.mp3_box = QComboBox()
        for label in QUALITY_PRESETS:
            self.mp3_box.addItem(tr(label), label)
        self.mp3_box.currentIndexChanged.connect(
            lambda: self._edit(
                lambda s: setattr(s, "audio_quality", self.mp3_box.currentData()), tr(
                    "MP3 quality")))
        self.mp3_label = QLabel(tr("MP3 quality"))
        self.mp3_label.setObjectName("muted")
        self.audio_section.grid.addWidget(self.mp3_label, 1, 0)
        self.audio_section.grid.addWidget(self.mp3_box, 1, 1)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(round(VOLUME_MIN), round(VOLUME_MAX))
        self.volume_slider.setToolTip(
            tr(
                "Make the sound louder or quieter. +6 dB doubles its strength, "
                "−6 dB halves it."))
        self.volume_label = QLabel("0 dB")
        self.volume_label.setMinimumWidth(48)
        self.volume_slider.valueChanged.connect(
            lambda v: self._edit(
                lambda s: setattr(s.edit, "volume", float(v)), tr("Volume"), final=not self.
                volume_slider.isSliderDown()))
        self.volume_slider.sliderReleased.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "volume", float(self.volume_slider.value())), tr(
                    "Volume")))
        self.volume_slider.valueChanged.connect(lambda v: self.volume_label.setText(_db_text(v)))
        self.volume_holder = self._slider_row(self.volume_slider, self.volume_label)
        self.audio_section.row(tr("Volume"), self.volume_holder, 2)

        self.normalize_box = QCheckBox(tr("Make the loudness even (−14 LUFS)"))
        self.normalize_box.setToolTip(
            tr(
                "Sets the whole sound to the loudness YouTube and Spotify play at, so it isn't too quiet or too "
                "loud next to other videos. It measures the sound first, so exporting takes a little longer."
            ))
        self.normalize_box.toggled.connect(
            lambda on: self._edit(lambda s: setattr(s.edit, "normalize", on), tr("Even loudness")))
        self.audio_section.grid.addWidget(self.normalize_box, 3, 0, 1, 2)

        self.noise_box = QComboBox()
        for key, label in NOISE_LABELS.items():
            self.noise_box.addItem(tr(label), key)
        self.noise_box.setToolTip(
            tr(
                "Takes out steady background noise, like hiss or a fan. Light keeps voices "
                "natural; Strong removes more but can make them sound thin."))
        self.noise_box.currentIndexChanged.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "audio_denoise", self.noise_box.currentData()),
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
        self.music_remove.clicked.connect(
            lambda: self._edit(lambda s: setattr(s.edit, "music", ""), tr("Remove music")))
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
        self.music_slider.setToolTip(
            tr(
                "How loud the music is next to the video's own sound. A video with no sound of its own plays "
                "the music at full volume."))
        self.music_label = QLabel()
        self.music_label.setMinimumWidth(48)
        self.music_slider.valueChanged.connect(
            lambda v: self._edit(
                lambda s: setattr(s.edit, "music_volume", float(v)), tr("Music volume"),
                final=not self.music_slider.isSliderDown()))
        self.music_slider.sliderReleased.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "music_volume", float(self.music_slider.value())),
                tr("Music volume")))
        self.music_slider.valueChanged.connect(lambda v: self.music_label.setText(_db_text(v)))
        self.music_holder = self._slider_row(self.music_slider, self.music_label)
        self.audio_section.row(tr("Music volume"), self.music_holder, 7)
        self.duck_box = QCheckBox(tr("Lower the music when people talk"))
        self.duck_box.setToolTip(
            tr(
                "The music dips whenever the video's own sound is playing, and comes back "
                "up in the quiet parts."))
        self.duck_box.toggled.connect(
            lambda on: self._edit(
                lambda s: setattr(s.edit, "music_duck", on), tr("Music ducking")))
        self.audio_section.grid.addWidget(self.duck_box, 8, 0, 1, 2)
        music_hint = QLabel(
            tr(
                "The music plays over the whole result, starts again if it is shorter, and fades "
                "out over the last 2 seconds."))
        music_hint.setObjectName("muted")
        music_hint.setWordWrap(True)
        self.audio_section.grid.addWidget(music_hint, 9, 0, 1, 2)
        # Everything under Remove the sound: with the sound removed none of it is heard.
        self.sound_options = [self.volume_holder, self.normalize_box, self.noise_box, music_holder]

        # Quality
        self.quality_section = Section(tr("Video quality"))
        self.quality_rows: dict[str, list[QWidget]] = {
        }  # name -> the widgets of that row, to show or hide

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
        self.format_box.setToolTip(
            tr(
                "MP4 (H.264) plays on everything. H.265 makes smaller files, but older phones and programs can't "
                "play it. WebM is for the web. MOV suits Apple's apps and video editors. A GIF is a short, silent, "
                "looping picture."))
        self.format_box.currentIndexChanged.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "video_format", self.format_box.currentData()),
                tr("Format")))
        row("format", tr("Format"), self.format_box, 0)
        self.format_hint = note(1)
        self.format_warning = note(2, error=True)
        self.size_box = QComboBox()
        for label, value in RESOLUTIONS.items():
            self.size_box.addItem(tr(label), value)
        self.size_box.setToolTip(
            tr(
                "Same as source matches the source's short side, so 1080p makes a 1080x1920 phone video. Phone, "
                "portrait and square videos are capped at 1080 pixels on the short side, so 4K doesn't make huge "
                "files. Up to 1080p does the same for any shape, and never makes a smaller video bigger. "
                "1080p, 720p and 480p set the short side."))
        self.size_box.currentIndexChanged.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "resolution", self.size_box.currentData()), tr("Size")))
        row("size", tr("Size"), self.size_box, 3)
        self.rate_box = QComboBox()
        for label, value in FRAME_RATES.items():
            self.rate_box.addItem(tr(label), value)
        self.rate_box.setToolTip(
            tr(
                "Steady makes every frame the same distance apart, which phones need. A steady MP4 or MOV is also "
                "made phone-safe (H.264 High, AAC sound). A video that is slower than the rate you choose keeps "
                "its own rate, so Steady 60 never makes a 30 fps video bigger."))
        self.rate_box.currentIndexChanged.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "frame_rate", self.rate_box.currentData()),
                tr("Frame rate")))
        row("rate", tr("Frame rate"), self.rate_box, 4)
        self.rate_hint = note(5)
        self.crf_box = QComboBox()
        for label, value in VIDEO_QUALITY.items():
            self.crf_box.addItem(tr(label), value)
        self.crf_box.addItem(tr(FIT_LABEL), FIT_DATA)
        self.crf_box.setToolTip(
            tr(
                "Higher quality makes bigger files. Fit in a file size does the opposite: you say how big the file "
                "may be, for Discord, WhatsApp or email, and the picture gets what is left after the sound."
            ))
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
        self.limit_spin.setValue(20.0)  # where Custom… starts: not one of the presets
        self.limit_spin.valueChanged.connect(
            lambda value: self._edit(
                lambda s: setattr(s.edit, "target_mb", round(value, 1)), tr("File size")))
        row("custom", tr("Size in MB"), self.limit_spin, 8)
        self.size_note = note(9)
        self.size_error = note(10, error=True)
        self.gif_fps_box = QComboBox()
        self.gif_fps_box.currentIndexChanged.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "gif_fps", self.gif_fps_box.currentData()),
                tr("GIF frame rate")))
        self.gif_fps_box.setToolTip(
            tr("The most frames a second the GIF gets. A video with fewer keeps its own."))
        row("gif_fps", tr("GIF frame rate"), self.gif_fps_box, 11)
        self.gif_width_box = QComboBox()
        self.gif_width_box.currentIndexChanged.connect(
            lambda: self._edit(
                lambda s: setattr(s.edit, "gif_width", self.gif_width_box.currentData()),
                tr("GIF width")))
        self.gif_width_box.setToolTip(
            tr("The widest the GIF gets, in pixels. A narrower picture keeps its width."))
        row("gif_width", tr("GIF width"), self.gif_width_box, 12)
        for box, values, text in ((self.gif_fps_box, GIF_FPS_CHOICES, "{} fps"),
                                  (self.gif_width_box, GIF_WIDTH_CHOICES, "{} px")):
            for value in values:
                box.addItem(text.format(value), value)
        self.gpu_box = QCheckBox(tr("Use the graphics card when possible"))
        self.gpu_box.setToolTip(
            tr(
                "Exports much faster on most PCs. The picture quality is about the same; "
                "if the graphics card fails, the export runs on the CPU instead."))
        self.gpu_box.toggled.connect(
            lambda on: self._edit(lambda s: setattr(s.edit, "use_gpu", on), tr("Graphics card")))
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

        self.sections = [
            self.preset_section, self.output_section, self.frame_section, self.adjust_section,
            self.trim_section, self.speed_section, self.fade_section, self.captions_section,
            self.audio_section, self.quality_section
        ]
        for section in self.sections + [self.export_section]:
            layout.addWidget(section)
        layout.addStretch(1)
        for combo in self.findChildren(QComboBox):
            # Long option names shouldn't force the panel wider than the window allows.
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
        self.wheel_guard = WheelGuard(self)
        self.wheel_guard.watch(content)
        self.set_presets({})
        self.set_settings(None, None)
