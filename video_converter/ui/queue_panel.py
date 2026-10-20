"""The export queue panel at the bottom of the window."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QScrollArea, QToolButton, QVBoxLayout,
    QWidget,
)

from .. import diagnostics
from ..core import format_time
from ..gpu import CPU_FALLBACK_NOTE
from ..i18n import tr
from ..jobs import Job, JobQueue, JobState
from . import icons


class QueueBridge(QObject):
    """Carries JobQueue updates from its worker thread to the UI thread."""
    jobUpdated = Signal(object)


def status_text(job: Job) -> str:
    if job.state is JobState.RUNNING:
        remaining = job.remaining_seconds()
        left = " · " + tr("{time} left", time=format_time(remaining)) if remaining is not None else ""
        retry = " · " + tr("the graphics card failed, trying the CPU") if tr(CPU_FALLBACK_NOTE) in job.note else ""
        return f"{job.progress:.0%}{left}{retry}"
    if job.state is JobState.FAILED:
        return tr("Failed: {error}", error=job.error.splitlines()[-1] if job.error else tr("unknown error"))
    if job.state is JobState.DONE and job.output:
        return tr("Saved as {name}", name=job.output.name) + (f" · {job.note}" if job.note else "")
    return job.state.label


def details_box(job: Job, parent: Optional[QWidget] = None) -> QMessageBox:
    """A box telling what went wrong with a failed export and where the log is (not shown yet)."""
    box = QMessageBox(QMessageBox.Icon.Warning, tr("Export failed"),
                      tr("{title} couldn't be exported.", title=job.title), QMessageBox.StandardButton.Close, parent)
    box.setInformativeText(job.error.strip().splitlines()[-1] if job.error.strip() else tr("The reason isn't known."))
    box.setDetailedText(f"{job.error.strip() or tr('No further details.')}\n\n" + tr(
        "The log file, which has the full ffmpeg command and output, is here:\n{path}", path=diagnostics.log_path()))
    box.addButton(tr("Open the log folder"), QMessageBox.ButtonRole.ActionRole).clicked.connect(
        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(diagnostics.log_path().parent))))
    return box


class JobRow(QFrame):
    cancelRequested = Signal(int)
    detailsRequested = Signal(int)

    def __init__(self, job: Job, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.job_id = job.id
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = QLabel(f"{job.title}  ·  {job.summary}")
        self.status = QLabel()
        self.status.setObjectName("muted")
        text.addWidget(self.title)
        text.addWidget(self.status)
        layout.addLayout(text, 3)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(6)
        layout.addWidget(self.bar, 2)
        self.details_button = QPushButton(tr("Details"))
        self.details_button.setToolTip(tr("See what went wrong and where the log is"))
        self.details_button.clicked.connect(lambda: self.detailsRequested.emit(self.job_id))
        self.open_button = QToolButton()
        self.open_button.setIcon(icons.icon("open"))
        self.open_button.setToolTip(tr("Show in folder"))
        self.open_button.clicked.connect(self._open)
        self.cancel_button = QToolButton()
        self.cancel_button.setIcon(icons.icon("close"))
        self.cancel_button.setToolTip(tr("Cancel"))
        self.cancel_button.clicked.connect(lambda: self.cancelRequested.emit(self.job_id))
        layout.addWidget(self.details_button)
        layout.addWidget(self.open_button)
        layout.addWidget(self.cancel_button)
        self.output: Optional[Path] = None
        self.update_job(job)

    def update_job(self, job: Job) -> None:
        self.bar.setValue(int(job.progress * 1000))
        self.status.setText(status_text(job))
        self.status.setObjectName("error" if job.state is JobState.FAILED else "muted")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        if job.state is JobState.FAILED:
            self.status.setToolTip(job.error)
        self.output = job.output
        self.details_button.setVisible(job.state is JobState.FAILED)
        self.open_button.setVisible(job.state is JobState.DONE)
        self.cancel_button.setVisible(not job.state.finished)

    def _open(self) -> None:
        if self.output and self.output.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output.parent)))


class QueuePanel(QFrame):
    def __init__(self, queue: JobQueue, bridge: QueueBridge, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.queue = queue
        self.rows: dict[int, JobRow] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        header = QHBoxLayout()
        title = QLabel(tr("Export queue"))
        title.setObjectName("heading")
        self.summary = QLabel(tr("Nothing exported yet. Edits you export appear here."))
        self.summary.setObjectName("muted")
        header.addWidget(title)
        header.addWidget(self.summary, 1)
        clear = QPushButton(tr("Clear finished"))
        clear.clicked.connect(self.clear_finished)
        cancel_all = QPushButton(tr("Cancel all"))
        cancel_all.clicked.connect(self.queue.cancel_all)
        header.addWidget(clear)
        header.addWidget(cancel_all)
        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.list_layout = QVBoxLayout(holder)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(2)
        self.list_layout.addStretch(1)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        bridge.jobUpdated.connect(self.on_job_updated)

    def on_job_updated(self, job: Job) -> None:
        row = self.rows.get(job.id)
        if row is None:
            row = JobRow(job)
            row.cancelRequested.connect(self.queue.cancel)
            row.detailsRequested.connect(self.show_details)
            self.rows[job.id] = row
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)
        row.update_job(job)
        self._update_summary()

    def show_details(self, job_id: int) -> None:
        job = next((j for j in self.queue.jobs if j.id == job_id), None)
        if job is not None:
            details_box(job, self).exec()

    def clear_finished(self) -> None:
        for job in self.queue.clear_finished():
            row = self.rows.pop(job.id, None)
            if row is not None:
                row.deleteLater()
        self._update_summary()

    def _update_summary(self) -> None:
        jobs = self.queue.jobs
        if not jobs:
            self.summary.setText(tr("Nothing in the queue."))
            return
        waiting = sum(j.state is JobState.QUEUED for j in jobs)
        running = sum(j.state is JobState.RUNNING for j in jobs)
        done = sum(j.state is JobState.DONE for j in jobs)
        failed = sum(j.state is JobState.FAILED for j in jobs)
        parts = []
        if running:
            parts.append(tr("exporting"))
        if waiting:
            parts.append(tr("{n} waiting", n=waiting))
        if done:
            parts.append(tr("{n} done", n=done))
        if failed:
            parts.append(tr("{n} failed", n=failed))
        self.summary.setText(tr(", ").join(parts).capitalize() if parts else tr("All cancelled."))
