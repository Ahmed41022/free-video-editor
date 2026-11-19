"""The Help menu: the log folder, reporting a problem, About."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from ... import APP_NAME, diagnostics
from ...i18n import tr


class HelpMixin:
    """Help menu actions."""

    def open_log_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(diagnostics.log_path().parent)))

    def problem_report_url(self) -> str:
        """The link that opens a new GitHub issue with this PC's details and the end of the log
        filled in."""
        return diagnostics.issue_url(
            diagnostics.system_lines(self.ffmpeg, self.gpu_encoders),
            diagnostics.recent_log_lines())

    def report_problem(self) -> None:
        """Open GitHub's new-issue page in the browser, once the user has seen what it will
        contain."""
        answer = QMessageBox.question(
            self, tr("Report a problem"),
            tr(
                "This opens GitHub in your browser with a new problem report already filled "
                "in: the app and "
                "Windows versions, and the last lines of the log (which can include the names "
                "of files you "
                "worked on).\n\nNothing is sent until you press Submit there, and you can edit "
                "or delete "
                "anything first. You need a GitHub account to submit it.\n\nOpen it?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl(self.problem_report_url()))

    def show_about(self) -> None:
        QMessageBox.about(self, tr("About {app}", app=APP_NAME), diagnostics.about_text())
