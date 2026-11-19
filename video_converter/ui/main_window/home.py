"""The home page inside the window: showing it, hiding what only the editor needs while it is
there, and starting with a feature."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QStackedWidget

from ... import APP_NAME, __version__
from ...i18n import isolate, tr
from ...projects import Recovery
from ..features import BY_ID, Feature
from ..home import HomePage


class HomeMixin:
    """The window is a stack of two pages: the home page (made the first time it is shown) and
    the editor."""

    def _init_home(self) -> None:
        self.at_home = False
        self.home: Optional[HomePage] = None
        self.show_projects = True  # the projects are shown on the home page (a setting)
        self._pending_feature = ""  # a feature chosen before any file was open

    def _build_stack(self) -> QStackedWidget:
        self.stack = QStackedWidget()
        self.stack.addWidget(self.main_split)
        return self.stack

    def _ensure_home(self) -> HomePage:
        if self.home is None:
            page = self.home = HomePage()
            page.newProjectRequested.connect(self.new_project)
            page.openProjectRequested.connect(self.open_project)
            page.renameProjectRequested.connect(self.rename_project)
            page.deleteProjectRequested.connect(self.delete_project)
            page.recoverRequested.connect(self.recover_work)
            page.discardRecoveryRequested.connect(self.discard_recovery)
            page.featureChosen.connect(self.start_with_feature)
            page.startRequested.connect(self.start_editing)
            page.backToEditorRequested.connect(self.show_editor)
            page.showProjectsRequested.connect(self.set_show_projects)
            self.stack.addWidget(page)
        return self.home

    # ----- the two pages

    def show_home(self) -> None:
        """Go to the home page. The open project (if any) is saved first, so its card is right."""
        if self.closed:
            return
        if self.items or self.project is not None:
            self.save_work_now(feedback=False)
        self.player.pause()
        page = self._ensure_home()
        self.at_home = True
        self.refresh_home()
        self.stack.setCurrentWidget(page)
        self._apply_mode()
        self._update_title()

    def refresh_home(self) -> None:
        page = self._ensure_home()
        page.set_work(self.project.name if self.project else "" if self.items else None)
        page.set_recoveries(self.projects.recoveries(self.session))
        page.set_projects_shown(self.show_projects)
        page.set_projects(self.projects.list() if self.show_projects else [])

    def set_show_projects(self, on: bool) -> None:
        """Show or hide the projects on the home page (the Project menu and the page's own link).
        Only the page changes: no project is touched."""
        self.show_projects = bool(on)
        self.store.setValue("showProjects", self.show_projects)
        if self.show_projects_action.isChecked() != self.show_projects:
            self.show_projects_action.setChecked(self.show_projects)
        if self.home is not None:
            self.refresh_home()

    def show_editor(self) -> None:
        """Go to the editor (from the home page)."""
        if self.at_home or self.stack.currentWidget() is not self.main_split:
            self.at_home = False
            self.stack.setCurrentWidget(self.main_split)
            self._apply_mode()
            self._update_title()

    def _apply_mode(self) -> None:
        """Show what the page being shown needs: no Remove, Undo, Save frame, Join or Export on
        the home page (they are back, in the state they had, in the editor)."""
        home = self.at_home
        for action in self.editor_only_actions:
            action.setVisible(not home)
        self.home_action.setVisible(not home)
        self.theme_separator.setVisible(not home)
        self._gate_for_home()
        if not home:
            self._restore_editor_state()

    def _gate_for_home(self) -> None:
        """While the home page is shown, keep the editor's actions (and their shortcuts) off."""
        if not self.at_home:
            return
        for action in (*self.editor_only_actions, *self.editor_shortcuts, self.as_is_action):
            action.setEnabled(False)
        for button in (self.join_button, self.export_all_button, self.export_button):
            button.setEnabled(False)

    def _restore_editor_state(self) -> None:
        item = self.current
        self._show_export_state(item)
        self.remove_action.setEnabled(item is not None)
        self.frame_action.setEnabled(bool(item and item.info and item.info.has_video))
        self.undo_action.setEnabled(self.undo_stack.canUndo())
        self.redo_action.setEnabled(self.undo_stack.canRedo())
        self.save_action.setEnabled(True)
        for action in self.editor_shortcuts:
            action.setEnabled(True)
        self._update_list_actions()
        self._sync_as_is_action()

    def _update_title(self) -> None:
        parts = []
        if not self.at_home and self.current is not None:
            parts.append(self.current.path.name)
        if self.project is not None and not self.at_home:
            parts.append(self.project.name)
        parts.append(f"{APP_NAME} {__version__}")
        self.setWindowTitle(" · ".join(parts))
        self._sync_project_menu()  # (also kept right while the menu is closed)

    # ----- starting with a feature

    def start_editing(self) -> None:
        """Start in general: the editor with every setting open, and the Add files box."""
        self.show_editor()
        self.inspector.reset_sections()
        if not self.items:
            self.choose_files()

    def start_with_feature(self, key: str) -> None:
        """Open the editor on a feature: its section at the top of the settings with its choice
        made, the other sections folded. With no file open yet, ask for one, and make the choice
        as soon as there is one."""
        feature = BY_ID[key]
        self.show_editor()
        if not self.items:
            self._pending_feature = key
            self.inspector.focus_section(self._section_of(feature))
            self.statusBar().showMessage(
                tr("Add a file to start with {feature}.", feature=tr(feature.title)), 8000)
            self.choose_files()
            return
        self._apply_feature(feature)

    def _apply_pending_feature(self) -> None:
        key, self._pending_feature = self._pending_feature, ""
        if key in BY_ID and self.current is not None:
            self._apply_feature(BY_ID[key])

    def _section_of(self, feature: Feature):
        return getattr(self.inspector, feature.section) if feature.section else None

    def _apply_feature(self, feature: Feature) -> None:
        item = self.current
        if feature.needs_picture and item is not None and item.info is not None \
                and not item.info.has_video:
            self.inspector.focus_section(None)
            self.statusBar().showMessage(
                tr(
                    "{feature} needs a video, and this file is sound only.",
                    feature=isolate(tr(feature.title))), 8000)
            return
        self.inspector.focus_section(self._section_of(feature))
        if feature.choice is not None and item is not None:
            new = item.settings.copy()
            feature.choice(new)
            self.push_settings(new, tr(feature.title))
        if feature.focus:
            target = self.inspector
            for name in feature.focus.split("."):
                target = getattr(target, name)
            target.setFocus()
        if feature.id == "join":
            self.statusBar().showMessage(
                tr("Press Join into one to put your files in one video.") if len(self.items) > 1
                else tr("Add two or more files, then press Join into one."), 8000)
            if len(self.items) > 1:
                self.join_button.setFocus()

    # ----- starting the app

    def start_up(self, options: dict, files: list) -> None:
        """What the window shows when the app starts: the project or work a restarted copy was
        asked to reopen, else the files it was given (which go straight to the editor), else
        the home page."""
        if options.get("project"):
            self.open_project(options["project"])
            if self.project is None:
                self.show_home()
        elif options.get("work"):
            path = Path(options["work"])
            self.recover_work(Recovery(path, None, 0))
            if not self.items:
                self.show_home()
        elif files:
            self.add_paths(files)
        else:
            self.show_home()
