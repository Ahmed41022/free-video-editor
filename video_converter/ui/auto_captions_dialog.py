"""The "Make captions automatically" window: pick the language and how accurate to be, then listen to the
video's speech (downloading the speech model first, the one time it is needed) and hand back the cues."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QProgressBar, QVBoxLayout, QWidget,
)

from ..autocaptions import (
    DOWNLOADING, LANGUAGES, LISTENING, LOADING, PREPARING, CaptionOptions, CaptionResult, make_captions,
)
from ..core import ConversionCancelled
from ..speech_models import DEFAULT_MODEL, MODELS, is_downloaded

# What the window remembers between runs.
LANGUAGE_KEY, MODEL_KEY, TRANSLATE_KEY = "captionsLanguage", "captionsModel", "captionsTranslate"

HELP = ("Listens to the speech in the video and writes it down as captions that you can fix afterwards. "
        "It runs entirely on this PC: the first time you use an accuracy level its speech model is downloaded "
        "once, and after that no internet is needed and nothing is sent anywhere.")

Runner = Callable[..., CaptionResult]


class AutoCaptionsDialog(QDialog):
    """Runs the captions job in the background. After `accept()`, `outcome` holds the cues (in the source
    video's time) and the language that was heard."""

    worked = Signal(int, object, str)           # which run it was, the result (or None), what went wrong
    progressed = Signal(int, str, float, str)   # which run it was, the phase, how far through it, a few words

    def __init__(self, ffmpeg: str, source: Path, duration: Optional[float], start: Optional[float],
                 end: Optional[float], store=None, parent: Optional[QWidget] = None, models_root: Optional[Path] = None,
                 runner: Runner = make_captions):
        super().__init__(parent)
        self.setWindowTitle("Make captions automatically")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.ffmpeg, self.source, self.duration = ffmpeg, source, duration
        self.start, self.end = start, end
        self.store, self.models_root, self.runner = store, models_root, runner
        self.outcome: Optional[CaptionResult] = None
        self._run = 0
        self._cancel: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        intro = QLabel(HELP)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        self.language_box = QComboBox()
        for code, name in LANGUAGES.items():
            self.language_box.addItem(name, code)
        self.language_box.setToolTip("The language spoken in the video. \"Detect automatically\" works for most "
                                     "videos; choose the language if the guess is wrong.")
        self.model_box = QComboBox()
        for key in MODELS:
            self.model_box.addItem("", key)
        self.model_box.setToolTip("Higher accuracy takes longer and needs a bigger one-time download")
        self.model_note = QLabel()
        self.model_note.setObjectName("muted")
        self.model_note.setWordWrap(True)
        self.translate_box = QCheckBox("Translate to English")
        self.translate_box.setToolTip("Write the captions in English, whatever language is spoken. The Balanced and "
                                      "Accurate levels translate much better than Fast.")
        for n, (label, widget) in enumerate((("Language", self.language_box), ("Accuracy", self.model_box))):
            text = QLabel(label)
            text.setObjectName("muted")
            grid.addWidget(text, n, 0)
            grid.addWidget(widget, n, 1)
        grid.addWidget(self.model_note, 2, 1)
        grid.addWidget(self.translate_box, 3, 1)
        layout.addLayout(grid)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        layout.addWidget(self.bar)
        self.error = QLabel()
        self.error.setObjectName("error")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.start_button = self.buttons.addButton("Start", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText("Cancel")
        self.start_button.clicked.connect(self.start_work)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.buttons)

        self.worked.connect(self._worked)
        self.progressed.connect(self._progressed)
        self.model_box.currentIndexChanged.connect(self._show_model)
        self._load_choices()
        self._show_model()

    # ----- choices -------------------------------------------------------------

    def _saved(self, key: str, default):
        if self.store is None:
            return default
        value = self.store.value(key, default)
        return value if value is not None else default

    def _load_choices(self) -> None:
        language = self._saved(LANGUAGE_KEY, "")
        self.language_box.setCurrentIndex(max(self.language_box.findData(language), 0))
        model = self._saved(MODEL_KEY, DEFAULT_MODEL)
        index = self.model_box.findData(model)
        self.model_box.setCurrentIndex(index if index >= 0 else self.model_box.findData(DEFAULT_MODEL))
        translate = self._saved(TRANSLATE_KEY, False)
        self.translate_box.setChecked(translate is True or str(translate).lower() == "true")

    def _save_choices(self) -> None:
        if self.store is not None:
            self.store.setValue(LANGUAGE_KEY, self.language)
            self.store.setValue(MODEL_KEY, self.model_key)
            self.store.setValue(TRANSLATE_KEY, self.translate)

    @property
    def language(self) -> str:
        return self.language_box.currentData() or ""

    @property
    def model_key(self) -> str:
        return self.model_box.currentData() or DEFAULT_MODEL

    @property
    def translate(self) -> bool:
        return self.translate_box.isChecked()

    def options(self) -> CaptionOptions:
        return CaptionOptions(self.model_key, self.language, self.translate, self.start, self.end)

    def _show_model(self) -> None:
        """Say, for each accuracy level, how big it is and whether it is on this PC already."""
        current = self.model_box.currentData()
        self.model_box.blockSignals(True)
        for i, (key, model) in enumerate(MODELS.items()):
            ready = is_downloaded(model, self.models_root)
            self.model_box.setItemText(i, f"{model.label} ({model.size_text}"
                                          f"{', downloaded' if ready else ', needs a download'})")
        self.model_box.setCurrentIndex(max(self.model_box.findData(current), 0))
        self.model_box.blockSignals(False)
        model = MODELS[self.model_key]
        ready = is_downloaded(model, self.models_root)
        self.model_note.setText(model.note + (" Already downloaded."
                                              if ready else f" A one-time download of {model.size_text} is needed."))

    # ----- working -------------------------------------------------------------

    def start_work(self) -> None:
        if self._running:
            return
        self._save_choices()
        self._running = True
        self._run += 1
        run, cancel = self._run, threading.Event()
        self._cancel = cancel
        options = self.options()
        self.error.hide()
        for widget in (self.language_box, self.model_box, self.translate_box, self.start_button):
            widget.setEnabled(False)
        self.status.setText("Getting the sound ready…")
        self.status.show()
        self.bar.setValue(0)
        self.bar.setRange(0, 1000)
        self.bar.show()

        def work() -> None:
            result, problem = None, ""
            try:
                result = self.runner(
                    self.ffmpeg, self.source, options, self.duration, root=self.models_root,
                    on_progress=lambda phase, fraction, text: self._emit(self.progressed, run, cancel, phase,
                                                                         fraction, text),
                    cancel_event=cancel)
            except ConversionCancelled:
                return
            except Exception as exc:         # a message in the window beats a thread dying quietly
                problem = str(exc) or "Something went wrong while making the captions."
            self._emit(self.worked, run, cancel, result, problem)

        self._thread = threading.Thread(target=work, name="auto-captions", daemon=True)
        self._thread.start()

    @staticmethod
    def _emit(signal, run: int, cancel: threading.Event, *values) -> None:
        if cancel.is_set():
            return
        try:
            signal.emit(run, *values)
        except RuntimeError:               # the window is already gone
            pass

    def _progressed(self, run: int, phase: str, fraction: float, text: str) -> None:
        if run != self._run or not self._running:
            return
        if phase == LOADING:
            self.status.setText("Checking the speech model…")
            self.bar.setRange(0, 0)
            return
        self.bar.setRange(0, 1000)
        self.bar.setValue(int(max(0.0, min(fraction, 1.0)) * 1000))
        if phase == PREPARING:
            self.status.setText("Getting the sound ready…")
        elif phase == DOWNLOADING:
            self.status.setText(f"Downloading the speech model, one time only… {text}")
        elif phase == LISTENING:
            self.status.setText(f"Listening… {fraction:.0%}")

    def _worked(self, run: int, result: Optional[CaptionResult], problem: str) -> None:
        if run != self._run:
            return
        self._running = False
        self._thread = None
        self.bar.hide()
        self.status.hide()
        for widget in (self.language_box, self.model_box, self.translate_box, self.start_button):
            widget.setEnabled(True)
        self._show_model()
        if problem:
            self._fail(problem)
        elif result is None or not result.cues:
            self._fail("No speech was found in this video, so there is nothing to write down. If it does have "
                       "speech, try a more accurate level or choose the language.")
        else:
            self.outcome = result
            self.accept()

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    # ----- closing ------------------------------------------------------------

    def done(self, result: int) -> None:
        """Stop the work however the window closes (a half-listened video is not worth waiting for)."""
        if self._cancel is not None:
            self._cancel.set()
        self._running = False
        super().done(result)
