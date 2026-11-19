"""Projects, autosave and recovery: keeping the work between runs."""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QInputDialog, QMessageBox

from ... import APP_NAME
from ...i18n import isolate, tr
from ...project import ClipSettings
from ...projects import (
    DuplicateName, FileEntry, InvalidName, ProjectData, ProjectError, ProjectStore, Recovery,
    clean_name, default_folder, format_date_time, quit_prompt_enabled,
)
from .items import MediaItem

log = logging.getLogger(__name__)

AUTOSAVE_SECONDS = 30.0  # a project is written at most this often
AUTOSAVE_QUIET = 1.5  # ...but the first change after a rest is written after this long
SAVED_AFTER_MS = 400  # how long "Saving…" shows before "Saved" (the write itself is instant)
SAVED_SHOWN_MS = 5000


def projects_folder(explicit: Optional[Path], settings: Optional[QSettings]) -> Path:
    """Where the projects are kept. A window made with its own settings file (the tests do that)
    keeps them next to that file, so nothing but the real app ever uses the real folder."""
    if explicit is not None:
        return Path(explicit)
    if settings is not None and settings.format() != QSettings.Format.NativeFormat:
        name = Path(settings.fileName())
        return name.with_name(name.stem + "-projects")
    return default_folder()


class ProjectsMixin:
    """The open project (if any), writing it, and the recovery copy of work outside a project."""

    def _init_projects(self, folder: Path) -> None:
        self.projects = ProjectStore(folder)
        self.session = uuid.uuid4().hex  # names this run's recovery file
        self.project: Optional[ProjectData] = None
        self.autosave_interval = AUTOSAVE_SECONDS
        self.autosave_quiet = AUTOSAVE_QUIET
        self._dirty = False
        self._last_save = float("-inf")
        self._suspend_dirty = False  # a project is being opened: that is not a change
        self._work_handed_over = False  # a restarted copy took the work: write nothing more
        self._recovery_written = False
        self._autosave_timer = QTimer(self, singleShot=True, timeout=self._autosave)
        self._saved_timer = QTimer(self, singleShot=True, timeout=self._show_saved)
        self._clear_timer = QTimer(self, singleShot=True, timeout=self._clear_saved)

    # ----- what is saved

    def _snapshot(self) -> ProjectData:
        base = self.project or ProjectData("", "")
        files = [
            FileEntry(str(item.path), item.settings.to_dict(), item.play_original)
            for item in self.items
        ]
        selected = self.items.index(self.current) if self.current in self.items else -1
        return ProjectData(
            base.id, base.name, base.created, base.saved, selected, files, base.thumbnail_of)

    def _write_thumbnail(self, data: ProjectData) -> None:
        """The project's picture is one of the thumbnails the first file already has: copied (not
        decoded), once, and again only when the first file changes."""
        first = next((item for item in self.items if item.thumb_paths), None)
        if first is None or (data.thumbnail_of == str(first.path)
                             and self.projects.thumbnail_path(data.id).is_file()):
            return
        middle = first.thumb_paths[len(first.thumb_paths) // 2]
        target = self.projects.thumbnail_path(data.id)
        temp = target.with_name(target.name + ".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(middle, temp)
            temp.replace(target)
            data.thumbnail_of = str(first.path)
        except OSError as exc:
            log.info("Could not keep the project picture: %s", exc)

    def _thumbnails_ready(self, item: MediaItem) -> None:
        """A file's thumbnails exist now: a project that has no picture yet gets one."""
        if self.project is not None and not self.projects.thumbnail_path(
                self.project.id).is_file():
            self._mark_dirty()

    # ----- when it is saved

    def _mark_dirty(self) -> None:
        """Something changed that a project keeps: write it soon, but not more than every
        `autosave_interval` seconds. Nothing is scheduled when nothing changed."""
        if self.closed or self._work_handed_over or self._suspend_dirty:
            return
        if self.project is None and not self.items:
            if self._recovery_written:
                self.save_work_now(feedback=False)  # no work left: nothing to recover
            return
        self._dirty = True
        if self._autosave_timer.isActive():
            return
        wait = max(
            self.autosave_quiet, self._last_save + self.autosave_interval - time.monotonic())
        self._autosave_timer.start(int(wait * 1000))

    def _autosave(self) -> None:
        if self._dirty:
            self.save_work_now()

    def save_work_now(self, feedback: bool = True) -> bool:
        """Write the open project, or (outside a project) the recovery copy. True if it worked
        (or there was nothing to write). With `feedback` the status bar says Saving… and Saved."""
        if self._work_handed_over:
            return True
        self._autosave_timer.stop()
        if self.project is None and not self.items:
            self.projects.remove_recovery(self.session)
            self._recovery_written = False
            self._dirty = False
            return True
        if feedback:
            self._set_save_label(tr("Saving…"))
        try:
            data = self._snapshot()
            if self.project is not None:
                self._write_thumbnail(data)
                self.projects.save(data)
                self.project.saved, self.project.thumbnail_of = data.saved, data.thumbnail_of
            else:
                self.projects.write_recovery(self.session, data)
                self._recovery_written = True
        except OSError as exc:
            log.error("Couldn't save the work", exc_info=True)
            self._set_save_label(tr("Couldn't save"))
            self.statusBar().showMessage(
                tr("Couldn't save your work: {reason}", reason=exc.strerror or exc), 8000)
            self._dirty = True
            return False
        self._dirty = False
        self._last_save = time.monotonic()
        if feedback:
            self._saved_timer.start(SAVED_AFTER_MS)
        return True

    def _set_save_label(self, text: str) -> None:
        self._saved_timer.stop()
        self._clear_timer.stop()
        self.save_label.setText(text)

    def _show_saved(self) -> None:
        self.save_label.setText(tr("Saved"))
        self._clear_timer.start(SAVED_SHOWN_MS)

    def _clear_saved(self) -> None:
        self.save_label.setText("")

    def save_project(self) -> None:
        """Ctrl+S: save the project, or (outside a project) make one."""
        if self.at_home:
            return
        if self.project is None:
            if self.items:
                self.save_as_project()
            return
        self.save_work_now()

    # ----- names

    def _ask_name(self, title: str, default: str, exclude: Optional[str] = None) -> Optional[str]:
        """Ask for a project name until it is usable (not empty, not another project's name, in
        any capitals). None if the person gave up."""
        while True:
            text, ok = QInputDialog.getText(self, title, tr("Project name"), text=default)
            if not ok:
                return None
            default = text
            try:
                name = clean_name(text)
            except InvalidName:
                QMessageBox.warning(
                    self, APP_NAME, tr("Type a name for the project (up to 80 characters)."))
                continue
            if self.projects.name_taken(name, exclude):
                QMessageBox.warning(
                    self, APP_NAME,
                    tr(
                        "There is already a project called {name}. Two projects can't have "
                        "the same name, so choose another.", name=isolate(name)))
                continue
            return name

    # ----- making, opening, renaming, deleting

    def save_as_project(self) -> bool:
        """Make a project of the files that are open (they are not in one yet)."""
        base = self.items[0].path.stem if self.items else tr("My project")
        name = self._ask_name(tr("Save as a project"), self.projects.suggest_name(base))
        if name is None:
            return False
        try:
            data = self.projects.create(name)
        except DuplicateName:  # another copy of the app made one with this name meanwhile
            QMessageBox.warning(self, APP_NAME, tr("That name was just taken. Try another."))
            return False
        except (ProjectError, OSError) as exc:
            self._warn_not_saved(exc)
            return False
        self.project = data
        if not self.save_work_now():
            self.project = None
            self.projects.delete(data.id)
            return False
        self.projects.remove_recovery(self.session)
        self._recovery_written = False
        self._update_title()
        return True

    def _warn_not_saved(self, exc: Exception) -> None:
        QMessageBox.warning(
            self, APP_NAME,
            tr("Couldn't save your work: {reason}", reason=getattr(exc, "strerror", None) or exc))

    def new_project(self) -> None:
        if not self._leave_work():
            return
        name = self._ask_name(tr("New project"), self.projects.suggest_name(tr("My project")))
        if name is None:
            return
        try:
            data = self.projects.create(name)
        except (ProjectError, OSError) as exc:
            self._warn_not_saved(exc)
            return
        self._open_data(data)

    def open_project(self, ident: str) -> None:
        if self.project is not None and self.project.id == ident:
            self.show_editor()
            return
        if not self._leave_work():
            return
        try:
            data = self.projects.load(ident)
        except ProjectError as exc:
            QMessageBox.warning(
                self, APP_NAME, tr("Couldn't open this project: {reason}", reason=exc))
            self.refresh_home()
            return
        self._open_data(data)

    def _open_data(self, data: ProjectData, project: bool = True) -> None:
        """Show a project's files (or recovered work, with `project` False): each goes through the
        same loading as an added file, with its saved settings in place from the start."""
        self._suspend_dirty = True
        try:
            self._clear_work()
            self.project = data if project else None
            for entry in data.files:
                item = MediaItem(Path(entry.path))
                item.settings = ClipSettings.from_dict(entry.settings)
                item.play_original = entry.play_original
                item.restored = True
                self._add_item(item)
            self.show_editor()
            self.inspector.reset_sections()
            if self.items:
                row = data.selected if 0 <= data.selected < len(self.items) else 0
                self.media_list.setCurrentRow(row)
            self._update_title()
            self._update_list_actions()
        finally:
            self._suspend_dirty = False
        self._dirty = False
        self.save_label.setText("")
        self.statusBar().showMessage(
            tr("Opened {name}.", name=isolate(data.name))
            if project else tr("The work was recovered."), 4000)

    def _clear_work(self) -> None:
        """Close the files without asking (the caller has dealt with unsaved work)."""
        items = list(self.items)
        suspended, self._suspend_dirty = self._suspend_dirty, True
        try:
            if items:
                self.take_items(items)
        finally:
            self._suspend_dirty = suspended
        for item in items:
            self._remove_folder(self._thumb_folder(item))
        self.undo_stack.clear()
        self.project = None
        self._autosave_timer.stop()
        self._dirty = False

    def rename_project(self, ident: Optional[str] = None) -> None:
        ident = ident or (self.project.id if self.project else None)
        if ident is None:
            return
        current = next((i.name for i in self.projects.list() if i.id == ident), "")
        name = self._ask_name(tr("Rename the project"), current, exclude=ident)
        if name is None or name == current:
            return
        try:
            self.projects.rename(ident, name)
        except DuplicateName:
            QMessageBox.warning(self, APP_NAME, tr("That name was just taken. Try another."))
            return
        except (ProjectError, OSError) as exc:
            self._warn_not_saved(exc)
            return
        if self.project is not None and self.project.id == ident:
            self.project.name = name
        self._update_title()
        if self.at_home:
            self.refresh_home()

    def delete_project(self, ident: str) -> None:
        name = next((i.name for i in self.projects.list() if i.id == ident), "")
        answer = QMessageBox.question(
            self, tr("Delete the project"),
            tr(
                "Delete the project {name} from your history? This removes only the project "
                "(its list of files, its settings and its picture). Your videos are not "
                "deleted or changed.", name=isolate(name)))
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.project is not None and self.project.id == ident:
            self._clear_work()
        self.projects.delete(ident)
        if self.at_home:
            self.refresh_home()

    # ----- leaving work, quitting

    def _leave_work(self) -> bool:
        """Close what is open before something else is opened: a project is saved, work outside
        a project asks first. False if the person cancelled."""
        if self.project is not None:
            if not self.save_work_now(feedback=False):
                return False
        elif self.items and not self._decide_unsaved():
            return False
        self._clear_work()
        return True

    def _decide_unsaved(self) -> bool:
        """Files are open outside a project: Save as a project, Discard, or Cancel. True if it
        is settled (saved or thrown away); closing the box means Cancel."""
        box = QMessageBox(
            QMessageBox.Icon.Question, APP_NAME, tr("Save this work as a project?"), parent=self)
        box.setInformativeText(
            tr(
                "These files are open without a project, so your editing would be lost. "
                "Save it as a project to carry on later, or discard it."))
        save = box.addButton(tr("Save as a project…"), QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton(tr("Discard"), QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        box.deleteLater()
        if clicked is save:
            return self.save_as_project()
        if clicked is discard:
            self.projects.remove_recovery(self.session)
            self._recovery_written = False
            self._dirty = False
            return True
        return False

    def _finish_work(self) -> bool:
        """The window is closing: save the project, or settle the work outside one. False keeps
        the window open."""
        self._autosave_timer.stop()
        if self.project is not None:
            if self.save_work_now(feedback=False):
                return True
            answer = QMessageBox.question(
                self, tr("Couldn't save"),
                tr("Your project couldn't be saved. Quit without saving it?"))
            return answer == QMessageBox.StandardButton.Yes
        if self.items and quit_prompt_enabled() and not self._decide_unsaved():
            return False
        self.projects.remove_recovery(self.session)  # a clean exit leaves nothing to recover
        self._recovery_written = False
        return True

    # ----- recovery

    def recover_work(self, recovery: Recovery) -> None:
        """Open work a run left behind (it was not in a project)."""
        if not self._leave_work():
            return
        try:
            data = self.projects.load_recovery(recovery.path)
        except ProjectError as exc:
            QMessageBox.warning(
                self, APP_NAME, tr("Couldn't recover this work: {reason}", reason=exc))
            return
        self._open_data(data, project=False)
        # This run keeps its own copy now; the old one can go once that is written.
        if self.save_work_now(feedback=False):
            self.projects.remove_recovery(self.projects.recovery_session(recovery.path))
        if self.at_home:
            self.refresh_home()

    def discard_recovery(self, recovery: Recovery) -> None:
        answer = QMessageBox.question(
            self, tr("Discard recovered work"),
            tr(
                "Discard the work from {date}? It can't be brought back.",
                date=isolate(format_date_time(recovery.saved))))
        if answer == QMessageBox.StandardButton.Yes:
            self.projects.remove_recovery(self.projects.recovery_session(recovery.path))
            if self.at_home:
                self.refresh_home()
