"""Exporting, joining, the output folder and saving a frame."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox

from ...core import ConversionError
from ...i18n import isolate, join_parts, ntr, tr
from ...jobs import Clip, Job
from ...project import OUTPUT_AUDIO
from ...stills import IMAGE_EXTENSIONS, save_frame
from ...video import REVERSE_WARN_BYTES, format_memory
from .items import MediaItem


class ExportingMixin:
    """Export jobs, the output folder and saving a frame."""

    def choose_output_folder(self) -> None:
        menu = QMenu(self)
        menu.addAction(tr("Choose a folder…"), self._pick_output_folder)
        reset = menu.addAction(
            tr("Next to each original file"), lambda: self._set_output_folder(None))
        reset.setEnabled(self.output_dir is not None)
        # Under the Change… button, whether it was clicked or pressed from the keyboard (the mouse
        # may be anywhere).
        button = self.inspector.change_button
        menu.exec(button.mapToGlobal(QPoint(0, button.height())))

    def _pick_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, tr("Save exports to"), str(self.output_dir or ""))
        if folder:
            self._set_output_folder(Path(folder))

    def _set_output_folder(self, folder: Optional[Path]) -> None:
        self.output_dir = folder
        self._show_output_folder()
        self._save_preferences()

    def _show_output_folder(self) -> None:
        self.inspector.set_output_folder(
            str(self.output_dir) if self.output_dir else tr("Next to each original file"))

    def _make_job(self, item: MediaItem) -> Optional[Job]:
        """A job for exporting `item`, or None (with a message) if it can't be exported yet."""
        if item.loading:
            self.statusBar().showMessage(
                tr(
                    "{name} is still loading; try again in a moment.",
                    name=isolate(item.path.name)), 4000)
            return None
        if item.error:
            self.statusBar().showMessage(
                tr("Can't export {name}: {error}", name=isolate(item.path.name), error=item.error),
                6000)
            return None
        return Job(item.path, item.settings.copy(), self.output_dir, item.info)

    def _confirm_heavy(self, jobs: list[Job]) -> bool:
        """Ask whether to go ahead with exports that play something backwards and need a lot of
        memory."""
        if not self.confirm_heavy_exports:
            return True
        if len(jobs) == 1:
            text = tr(
                "Reversing {title} needs about {memory} of memory.", title=jobs[0].title,
                memory=format_memory(jobs[0].reverse_memory))
        else:
            lines = "\n".join(
                tr(
                    "{title}: about {memory}", title=job.title, memory=format_memory(
                        job.reverse_memory)) for job in jobs[:6])
            more = "\n" + tr("and {n} more", n=len(jobs) - 6) if len(jobs) > 6 else ""
            text = tr(
                "These exports play clips backwards, which needs a lot of memory:"
            ) + f"\n\n{lines}{more}"
        answer = QMessageBox.question(
            self, tr("This needs a lot of memory"), text + "\n\n" +
            tr("If your PC has less, the export can fail or make everything slow. Export anyway?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _queue_jobs(self, jobs: list[Job]) -> list[Job]:
        """Add `jobs` to the export queue, after checking with the user about the ones that need
        a lot of memory. Returns the jobs that were queued."""
        heavy = [job for job in jobs if job.reverse_memory > REVERSE_WARN_BYTES]
        if heavy and not self._confirm_heavy(heavy):
            skipped = {job.id for job in heavy}
            jobs = [job for job in jobs if job.id not in skipped]
            self.statusBar().showMessage(
                tr("Left out the exports that need a lot of memory."), 6000)
        return [self.queue.add(job) for job in jobs]

    def export_current(self) -> None:
        job = self._make_job(self.current) if self.current is not None else None
        if job and self._queue_jobs([job]):
            self.statusBar().showMessage(
                tr("Exporting {name}…", name=isolate(self.current.path.name)), 4000)

    def export_all(self) -> None:
        jobs, left_out = [], []
        for item in list(self.items):
            job = self._make_job(item)
            if job:
                jobs.append(job)
            else:
                left_out.append(item)
        count = len(self._queue_jobs(jobs))
        parts = [ntr(count, "Added {n} export to the queue.", "Added {n} exports to the queue.")
                 ] if count else []
        # A single file left out on its own already has _make_job's message; otherwise say which
        # ones were.
        if left_out and (count or len(left_out) > 1):
            names = join_parts(
                [item.path.name for item in left_out[:3]] + (["…"] if len(left_out) > 3 else []))
            parts.append(
                ntr(
                    len(left_out),
                    "Left out {n} file that is still loading or can't be read: {names}",
                    "Left out {n} files that are still loading or can't be read: {names}",
                    names=names))
        if parts:
            self.statusBar().showMessage(" ".join(parts), 8000 if left_out else 4000)

    def join_clips(self) -> None:
        """Export the selected files (or all of them, if one or none is selected) as one file,
        in list order, with the first file's output settings."""
        selected = {
            id(self._item_at(self.media_list.row(li)))
            for li in self.media_list.selectedItems()
        }
        chosen = [item for item in self.items
                  if id(item) in selected] if len(selected) > 1 else list(self.items)
        if len(chosen) < 2:
            self.statusBar().showMessage(tr("Add two or more files to join them into one."), 4000)
            return
        loading = next((item for item in chosen if item.loading), None)
        if loading is not None:
            self.statusBar().showMessage(
                tr(
                    "{name} is still loading; try again in a moment.",
                    name=isolate(loading.path.name)), 4000)
            return
        broken = next((item for item in chosen if item.error), None)
        if broken is not None:
            self.statusBar().showMessage(
                tr(
                    "Can't join {name}: {error}", name=isolate(broken.path.name),
                    error=broken.error), 6000)
            return
        first = chosen[0]
        audio = first.settings.output == OUTPUT_AUDIO
        if not audio and first.settings.edit.output_format.codec == "gif":
            self.statusBar().showMessage(
                tr(
                    "Joined clips can't be saved as a GIF: choose another format for the "
                    "first file."), 6000)
            return
        silent = next((item for item in chosen if not audio and not item.info.has_video), None)
        if silent is not None:
            self.statusBar().showMessage(
                tr(
                    "{name} has no picture, so it can only be joined as MP3: set the "
                    "first file's output to Audio (MP3).", name=isolate(silent.path.name)), 6000)
            return
        clips = [Clip(item.path, item.settings.copy(), item.info) for item in chosen]
        job = Job(first.path, first.settings.copy(), self.output_dir, first.info, clips=clips)
        if self._queue_jobs([job]):
            self.statusBar().showMessage(
                ntr(
                    len(chosen), "Joining {n} file into one {kind}…",
                    "Joining {n} files into one {kind}…", kind="MP3" if audio else tr("video")),
                4000)

    def _job_updated(self, job: Job) -> None:
        if job.state.finished and job.state.value == "Failed":
            self.statusBar().showMessage(
                tr("Export failed: {name}", name=isolate(job.source.name)), 6000)

    def save_frame_image(self) -> None:
        """Ask where, then save the frame at the playhead as a PNG or JPG: at the size the export
        has, with every edit applied (not the smaller picture the preview shows)."""
        item = self.current
        if item is None or item.info is None or not item.info.has_video:
            self.statusBar().showMessage(tr("Open a video to save a frame of it."), 4000)
            return
        at = self.player.position
        folder = self._saved_text("lastFrameDir") or str(self.output_dir or item.path.parent)
        name = f"{item.path.stem} (frame {at:.1f} s).png"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Save frame as image"), str(Path(folder) / name),
            tr("PNG image (*.png)") + ";;" + tr("JPEG image (*.jpg *.jpeg)"))
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() not in IMAGE_EXTENSIONS:
            target = Path(f"{path}.png")
        self.store.setValue("lastFrameDir", str(target.parent))
        edit, info, source = item.settings.copy().edit, item.info, item.path
        self.statusBar().showMessage(tr("Saving the frame…"))

        def work() -> None:
            try:
                save_frame(self.ffmpeg, source, target, at, edit, info)
                self.signals.frameSaved.emit(str(target), "")
            except (ConversionError, OSError, subprocess.SubprocessError) as exc:
                self.signals.frameSaved.emit("", str(exc))
            self._remove_tmpdir_if_closed()

        threading.Thread(target=work, name="save-frame", daemon=True).start()

    def _on_frame_saved(self, path: str, error: str) -> None:
        if self.closed:
            return
        if path:
            self.statusBar().showMessage(
                tr("Saved the frame as {name}.", name=isolate(Path(path).name)), 8000)
        else:
            self.statusBar().showMessage(
                tr(
                    "Couldn't save the frame: {error}",
                    error=error.splitlines()[-1] if error else tr("unknown error")), 8000)
