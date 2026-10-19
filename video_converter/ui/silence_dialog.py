"""The "Remove silences" window: pick how quiet and how long a pause is, see what it would cut, apply."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget,
)

from ..core import ConversionCancelled
from ..silence import (
    DB_RANGE, DEFAULT_DB, DEFAULT_MIN, DEFAULT_PAD, MIN_RANGE, PAD_RANGE, SilencePlan, detect_silences,
    plan_silence_removal, summary,
)
from ..timing import Range

# The sliders count whole steps: tenths of a second for the length, 0.05 s for the padding.
MIN_STEP, PAD_STEP = 0.1, 0.05


class SilenceDialog(QDialog):
    """Listens to the file in the background and shows what cutting its pauses would do.

    After `accept()`, `keep` holds the new kept ranges (one change for the whole job)."""

    analysed = Signal(int, object, str)      # which run it was, the silences found (or None), what went wrong
    DEBOUNCE_MS = 400                        # wait this long after a slider moves before listening again

    def __init__(self, ffmpeg: str, source: Path, duration: float, keep: list[Range], start: Optional[float],
                 end: Optional[float], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Remove silences")
        self.setModal(True)
        self.setMinimumWidth(440)
        self.ffmpeg, self.source, self.duration = ffmpeg, source, duration
        self.original_keep, self.start, self.end = list(keep), start, end
        self.keep: list[Range] = list(keep)
        self.plan: Optional[SilencePlan] = None
        self.silences: Optional[list[Range]] = None      # what the last finished listening found
        self._run = 0
        self._cancel: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        intro = QLabel("Cuts out the quiet stretches, like pauses between sentences. "
                       "You can still adjust the cuts on the timeline afterwards.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        self.db_slider, self.db_label = self._slider(grid, 0, "Quieter than", DB_RANGE[0], DB_RANGE[1], 1.0, DEFAULT_DB)
        self.db_slider.setToolTip("Sound below this level counts as silence. Raise it if background noise "
                                  "keeps pauses from being found.")
        self.min_slider, self.min_label = self._slider(grid, 1, "Longer than", MIN_RANGE[0], MIN_RANGE[1], MIN_STEP,
                                                       DEFAULT_MIN)
        self.min_slider.setToolTip("Only pauses at least this long are cut")
        self.pad_slider, self.pad_label = self._slider(grid, 2, "Keep a little silence", PAD_RANGE[0], PAD_RANGE[1],
                                                       PAD_STEP, DEFAULT_PAD)
        self.pad_slider.setToolTip("Silence left on each side of the speech, so words aren't clipped")
        layout.addLayout(grid)

        self.summary_label = QLabel("Listening…")
        self.summary_label.setWordWrap(True)
        self.summary_label.setMinimumHeight(40)
        layout.addWidget(self.summary_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText("Apply")
        self.apply_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel")
        self.buttons.clicked.connect(self._button_clicked)
        layout.addWidget(self.buttons)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self._listen)
        self.analysed.connect(self._analysed)
        self.db_slider.valueChanged.connect(self._level_changed)
        self.min_slider.valueChanged.connect(self._level_changed)
        self.pad_slider.valueChanged.connect(self._padding_changed)
        self._show_values()
        self._listen()

    def _slider(self, grid: QGridLayout, row: int, name: str, low: float, high: float, step: float, value: float):
        text = QLabel(name)
        text.setObjectName("muted")
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(round(low / step), round(high / step))
        slider.setValue(round(value / step))
        shown = QLabel()
        shown.setMinimumWidth(56)
        shown.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        holder = QHBoxLayout()
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addWidget(slider, 1)
        holder.addWidget(shown)
        grid.addWidget(text, row, 0)
        grid.addLayout(holder, row, 1)
        return slider, shown

    # ----- the values ---------------------------------------------------------

    @property
    def noise_db(self) -> float:
        return float(self.db_slider.value())

    @property
    def min_length(self) -> float:
        return round(self.min_slider.value() * MIN_STEP, 2)

    @property
    def padding(self) -> float:
        return round(self.pad_slider.value() * PAD_STEP, 2)

    def _show_values(self) -> None:
        self.db_label.setText(f"{self.noise_db:g} dB".replace("-", "−"))
        self.min_label.setText(f"{self.min_length:g} s")
        self.pad_label.setText(f"{self.padding:g} s")

    # ----- listening ----------------------------------------------------------

    def _level_changed(self) -> None:
        """The level or length changed: what was heard no longer applies, so listen again shortly."""
        self._show_values()
        self._stop_listening()
        self.silences = None
        self.plan = None
        self.apply_button.setEnabled(False)
        self._say("Listening…")
        self._debounce.start()

    def _padding_changed(self) -> None:
        self._show_values()
        self._refresh()

    def _listen(self) -> None:
        self._stop_listening()
        self._run += 1
        run, cancel = self._run, threading.Event()
        self._cancel = cancel
        level, length = self.noise_db, self.min_length

        def work() -> None:
            found, problem = None, ""
            try:
                found = detect_silences(self.ffmpeg, self.source, level, length, self.duration, cancel)
            except ConversionCancelled:
                return
            except Exception as exc:     # a message in the window beats a thread dying quietly
                problem = str(exc) or "Couldn't listen to this file."
            if not cancel.is_set():
                try:
                    self.analysed.emit(run, found, problem)
                except RuntimeError:     # the window is already gone
                    pass

        self._thread = threading.Thread(target=work, name="silence-detect", daemon=True)
        self._thread.start()

    def _stop_listening(self) -> None:
        self._debounce.stop()
        if self._cancel is not None:
            self._cancel.set()
        self._cancel = None

    def _analysed(self, run: int, silences: Optional[list], problem: str) -> None:
        if run != self._run:
            return
        if problem:
            self._say(problem, error=True)
            return
        self.silences = silences
        self._refresh()

    def _refresh(self) -> None:
        if self.silences is None:
            return
        self.plan = plan_silence_removal(self.original_keep, self.silences, self.start, self.end, self.duration,
                                         self.padding)
        self._say(summary(self.plan))
        self.apply_button.setEnabled(bool(self.plan.cuts))

    def _say(self, text: str, error: bool = False) -> None:
        self.summary_label.setObjectName("error" if error else "")
        self.summary_label.setText(text)
        self.summary_label.style().unpolish(self.summary_label)
        self.summary_label.style().polish(self.summary_label)

    # ----- closing ------------------------------------------------------------

    def _button_clicked(self, button) -> None:
        if button is self.apply_button:
            self.accept()
        else:
            self.reject()

    def accept(self) -> None:
        if self.plan is None or not self.plan.cuts:
            return
        self.keep = list(self.plan.keep)
        super().accept()

    def done(self, result: int) -> None:
        """Stop ffmpeg however the window closes."""
        self._stop_listening()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(2.0)
        super().done(result)
