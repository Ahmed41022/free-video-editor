"""What is remembered between runs: the theme, the language, the window."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QApplication, QMessageBox

from ... import APP_NAME, i18n, updates
from ...i18n import tr
from .. import language, theme


class PreferencesMixin:
    """Saved preferences, the theme and the language."""

    def set_language_choice(self, choice: str) -> None:
        """Remember the language the user picked. It is used from the next start, so offer to
        restart now."""
        self.store.setValue(i18n.SETTING_KEY, choice)
        if i18n.resolve(choice) != i18n.language() and self._ask_restart():
            self.restart_app()

    def _ask_restart(self) -> bool:
        """Say that the language changes at the next start; True if the user wants to restart
        now."""
        box = QMessageBox(
            QMessageBox.Icon.Question, tr("Language"),
            tr("The new language is used the next time the app starts."), parent=self)
        box.setInformativeText(
            tr("Restart now? Your work is saved first and opened again just as it is."))
        restart = box.addButton(tr("Restart now"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("Later"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        answer = box.clickedButton() is restart
        box.deleteLater()
        return answer

    def restart_app(self) -> None:
        """Start the app again with the same files and close this window (asking first about
        exports still running). If the new copy can't be started, this window stays open and says
        so."""
        paths = [str(item.path) for item in self.items]
        if self.queue.busy and QMessageBox.question(
                self, tr("Exports still running"),
                tr("Some exports haven't finished. Stop them and quit?"
                   )) != QMessageBox.StandardButton.Yes:
            return
        resume = self._hand_over_work()
        if resume is False:
            return
        # Free the "one window per user" name first, so the new copy takes it instead of handing
        # its files to this window while it closes. Then start the new copy before closing: if it
        # can't be started, this window stays open (and listens again) instead of just vanishing.
        if self.instance is not None:
            self.instance.close()
        started = language.restart(paths, resume=resume) if resume else language.restart(paths)
        if not started:
            self._work_handed_over = False
            if self.instance is not None:
                self.instance.listen()
            QMessageBox.warning(
                self, APP_NAME,
                tr(
                    "Couldn't restart the app. Close it and open it again yourself "
                    "to use the new language."))
            return
        self._restarting = True  # the question about running exports was answered above
        if not self.close():
            self._restarting = False
            self._work_handed_over = False

    def _hand_over_work(self):
        """Save the work for the copy that is about to start: the project (by its id), or the
        recovery copy of work outside a project (by its file). Returns what to pass to the new
        copy, None when nothing is open, or False when it couldn't be saved."""
        if self.project is None and not self.items:
            return None
        if not self.save_work_now(feedback=False):
            QMessageBox.warning(
                self, APP_NAME, tr("Your work couldn't be saved, so the app was not restarted."))
            return False
        self._work_handed_over = True
        if self.project is not None:
            return {"project": self.project.id}
        return {"work": str(self.projects.recovery_path(self.session))}

    def _load_preferences(self) -> None:
        mode = self._saved_text("theme", "system")
        self.theme_choice = mode if mode in ("dark", "light") else "system"
        self.theme_mode = theme.apply_theme(QApplication.instance(), self.theme_choice)
        geometry = self._saved_bytes("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        for key, splitter in (("splitTop", self.top_split), ("splitMain", self.main_split)):
            state = self._saved_bytes(key)
            if state is not None:
                splitter.restoreState(state)
        folder = self._saved_text("outputDir")
        self.output_dir = Path(folder) if folder and Path(folder).is_dir() else None
        try:
            presets = json.loads(self._saved_text("presets", "{}") or "{}")
            self.user_presets = presets if isinstance(presets, dict) else {}
        except ValueError:
            self.user_presets = {}
        self.inspector.set_presets(self.user_presets)
        self.smooth_previews = self._saved_bool("smoothPreviews", True)
        self.smooth_action.setChecked(self.smooth_previews)
        self.check_updates = self._saved_bool(updates.ENABLED_KEY, True)
        self.updates_action.setChecked(self.check_updates)
        self._show_output_folder()
        self.refresh_icons()

    def _saved_text(self, key: str, default: str = "") -> str:
        """A saved text setting. Anything else a damaged settings file holds is ignored."""
        value = self.store.value(key, default)
        return value if isinstance(value, str) else default

    def _saved_bool(self, key: str, default: bool) -> bool:
        """A saved on/off setting (the settings file may keep it as text)."""
        value = self.store.value(key, default)
        if isinstance(value, bool):
            return value
        return {
            "true": True,
            "false": False
        }.get(value.strip().lower(), default) if isinstance(value, str) else default

    def _saved_bytes(self, key: str) -> Optional[QByteArray]:
        """A saved window layout, or None if there is none or the settings file holds something
        else."""
        value = self.store.value(key)
        return QByteArray(value) if isinstance(value, (QByteArray, bytes)) and len(value) else None

    def _save_preferences(self) -> None:
        self.store.setValue("geometry", self.saveGeometry())
        self.store.setValue("splitTop", self.top_split.saveState())
        self.store.setValue("splitMain", self.main_split.saveState())
        self.store.setValue("outputDir", str(self.output_dir) if self.output_dir else "")
        self.store.setValue("presets", json.dumps(self.user_presets))
        self.store.setValue(
            "theme", self.theme_choice)  # what was asked for ("system" too), not what it came to
        self.store.setValue("smoothPreviews", self.smooth_previews)
        self.store.setValue(updates.ENABLED_KEY, self.check_updates)

    def toggle_theme(self) -> None:
        # the button is an explicit choice
        self.theme_choice = "light" if self.theme_mode == "dark" else "dark"
        self.theme_mode = theme.apply_theme(QApplication.instance(), self.theme_choice)
        self.refresh_icons()
        for widget in (self.view, self.timeline):
            widget.update()
