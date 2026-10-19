"""The 2.4 pieces of the window: the graphics card option, the Help menu, the failed-export details and
the friendly crash box. Run without a visible window; skipped when PySide6 isn't installed."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    from PySide6.QtCore import QEvent, QSettings, QUrl
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter import APP_NAME, REPO_URL, __version__, diagnostics, gpu
from video_converter.core import find_ffmpeg
from video_converter.jobs import Job, JobState
from video_converter.project import ClipSettings

FFMPEG = find_ffmpeg()


def setUpModule():
    if QApplication is None:
        raise unittest.SkipTest("PySide6 is not installed")
    global app
    app = QApplication.instance() or QApplication([])


def pump(seconds, until=lambda: False):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        QApplication.processEvents()
        time.sleep(0.01)
    return until()


class GraphicsCardOptionTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.inspector import Inspector
        self.inspector = Inspector()
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.edits = []
        self.inspector.settingsEdited.connect(lambda s, label, final: self.edits.append((s, label, final)))

    def test_it_lives_in_the_video_quality_section_and_is_on_by_default(self):
        self.assertTrue(self.inspector.quality_section.isAncestorOf(self.inspector.gpu_box))
        self.assertEqual(self.inspector.gpu_box.text(), "Use the graphics card when possible")
        self.assertTrue(self.inspector.gpu_box.isChecked())

    def test_it_says_while_it_is_still_looking(self):
        self.assertEqual(self.inspector.gpu_label.text(), "Looking for a graphics card…")
        self.assertFalse(self.inspector.gpu_box.isEnabled())

    def test_it_names_the_card_that_was_found(self):
        self.inspector.set_gpu_name("NVIDIA GeForce RTX 4060 Laptop GPU")
        self.assertEqual(self.inspector.gpu_label.text(), "NVIDIA GeForce RTX 4060 Laptop GPU")
        self.assertTrue(self.inspector.gpu_box.isEnabled())

    def test_without_a_card_it_says_so_and_is_disabled(self):
        self.inspector.set_gpu_name(None)
        self.assertEqual(self.inspector.gpu_label.text(), "No supported graphics card found")
        self.assertFalse(self.inspector.gpu_box.isEnabled())

    def test_switching_it_off_is_an_edit_of_the_file_settings(self):
        self.inspector.set_gpu_name("NVIDIA graphics card")
        self.inspector.gpu_box.setChecked(False)
        settings, label, final = self.edits[-1]
        self.assertFalse(settings.edit.use_gpu)
        self.assertEqual(label, "Graphics card")
        self.assertTrue(final)

    def test_it_shows_each_files_own_choice(self):
        settings = ClipSettings()
        settings.edit.use_gpu = False
        self.inspector.set_settings(settings, 10.0)
        self.assertFalse(self.inspector.gpu_box.isChecked())
        self.assertEqual(self.edits, [], "showing settings is not an edit")
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.assertTrue(self.inspector.gpu_box.isChecked())

    def test_it_is_part_of_the_video_settings_only(self):
        audio = ClipSettings(output="audio")
        self.inspector.set_settings(audio, 10.0)
        self.assertTrue(self.inspector.quality_section.isHidden())


class FailedExportDetailsTests(unittest.TestCase):
    def failed_job(self):
        job = Job(Path("C:/videos/holiday.mp4"), ClipSettings())
        job.state = JobState.FAILED
        job.error = "[h264_nvenc @ 0123] Cannot load nvcuda.dll\nError while opening encoder\nConversion failed!"
        return job

    def test_a_failed_row_has_a_details_button_and_others_do_not(self):
        from video_converter.ui.queue_panel import JobRow
        job = self.failed_job()
        row = JobRow(job)
        row.show()
        self.assertTrue(row.details_button.isVisibleTo(row))
        job.state = JobState.DONE
        row.update_job(job)
        self.assertFalse(row.details_button.isVisibleTo(row))
        job.state = JobState.RUNNING
        row.update_job(job)
        self.assertFalse(row.details_button.isVisibleTo(row))

    def test_the_button_asks_for_this_jobs_details(self):
        from video_converter.ui.queue_panel import JobRow
        job = self.failed_job()
        row = JobRow(job)
        asked = []
        row.detailsRequested.connect(asked.append)
        row.details_button.click()
        self.assertEqual(asked, [job.id])

    def test_the_box_has_the_error_and_the_log_location(self):
        from video_converter.ui.queue_panel import details_box
        job = self.failed_job()
        with tempfile.TemporaryDirectory() as folder:
            try:
                path = diagnostics.setup_logging(Path(folder))
                box = details_box(job)
                self.assertIn("holiday.mp4", box.text())
                self.assertEqual(box.informativeText(), "Conversion failed!")
                self.assertIn("Cannot load nvcuda.dll", box.detailedText())
                self.assertIn(str(path), box.detailedText())
                self.assertTrue(any(b.text() == "Open the log folder" for b in box.buttons()))
            finally:
                diagnostics.shutdown_logging()

    def test_a_failure_with_no_message_still_gets_a_box(self):
        from video_converter.ui.queue_panel import details_box
        job = self.failed_job()
        job.error = ""
        box = details_box(job)
        self.assertTrue(box.informativeText())
        self.assertIn("No further details", box.detailedText())

    def test_the_queue_panel_shows_the_box_for_the_job_asked_about(self):
        from video_converter.jobs import JobQueue
        from video_converter.ui.queue_panel import QueueBridge, QueuePanel
        queue = JobQueue("ffmpeg")
        job = self.failed_job()
        queue.jobs.append(job)
        panel = QueuePanel(queue, QueueBridge())
        with mock.patch.object(QMessageBox, "exec", return_value=0) as shown:
            panel.show_details(job.id)
            panel.show_details(99999)
        self.assertEqual(shown.call_count, 1)

    def test_a_fallback_to_the_cpu_is_mentioned_in_the_status(self):
        from video_converter.ui.queue_panel import status_text
        job = Job(Path("clip.mp4"), ClipSettings())
        job.state, job.output, job.note = JobState.DONE, Path("clip (phone).mp4"), gpu.CPU_FALLBACK_NOTE
        self.assertEqual(status_text(job), "Saved as clip (phone).mp4 · Exported on the CPU because the graphics card failed")
        job.note = ""
        self.assertEqual(status_text(job), "Saved as clip (phone).mp4")
        job.state, job.note, job.started_at = JobState.RUNNING, gpu.CPU_FALLBACK_NOTE, time.monotonic()
        self.assertIn("trying the CPU", status_text(job))


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def new_window(self, encoders=(), name=None):
        from video_converter.ui.main_window import MainWindow
        ini = self.tmp / f"{self.id().split('.')[-1]}.ini"
        with mock.patch.object(gpu, "detect_encoders", return_value=encoders), \
                mock.patch.object(gpu, "gpu_name", return_value=name):
            window = MainWindow(FFMPEG, QSettings(str(ini), QSettings.Format.IniFormat))
            window.resize(1000, 700)
            window.show()
            pump(5, lambda: window.gpu_encoders is not None)
        self.addCleanup(self.close, window)
        return window

    def close(self, window):
        window.close()
        window.loader.shutdown(wait=True)
        window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_the_window_shows_the_card_it_found(self):
        window = self.new_window(("h264_nvenc",), name="NVIDIA GeForce RTX 4060 Laptop GPU")
        self.assertEqual(window.gpu_encoders, ("h264_nvenc",))
        self.assertEqual(window.inspector.gpu_label.text(), "NVIDIA GeForce RTX 4060 Laptop GPU")
        self.assertTrue(window.inspector.gpu_box.isEnabled())

    def test_the_window_says_when_there_is_no_card(self):
        window = self.new_window(())
        self.assertEqual(window.gpu_encoders, ())
        self.assertEqual(window.inspector.gpu_label.text(), "No supported graphics card found")
        self.assertFalse(window.inspector.gpu_box.isEnabled())

    def test_the_help_menu_has_the_three_entries(self):
        window = self.new_window()
        menus = {action.text().replace("&", ""): action.menu() for action in window.menuBar().actions()}
        self.assertIn("Help", menus)
        entries = [a.text() for a in menus["Help"].actions() if a.text()]
        self.assertEqual(entries, ["Open the log folder", "Report a problem…", f"About {APP_NAME}"])

    def test_report_a_problem_opens_the_prefilled_issue_after_the_user_agrees(self):
        window = self.new_window(("h264_nvenc",), name="NVIDIA")
        with tempfile.TemporaryDirectory() as folder:
            try:
                diagnostics.setup_logging(Path(folder))
                import logging
                logging.getLogger("video_converter").info("Export 3 started: holiday.mp4")
                with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                        mock.patch("video_converter.ui.main_window.QDesktopServices.openUrl") as opened:
                    window.report_problem()
                opened.assert_called_once()
                url = opened.call_args.args[0].toString()
            finally:
                diagnostics.shutdown_logging()
        self.assertTrue(url.startswith(f"{REPO_URL}/issues/new?title="))
        self.assertLessEqual(len(url), diagnostics.URL_LIMIT)
        from urllib.parse import parse_qs, urlparse
        body = parse_qs(urlparse(url).query)["body"][0]
        self.assertIn(__version__, body)
        self.assertIn("Windows", body)
        self.assertIn("h264_nvenc", body)
        self.assertIn("Export 3 started: holiday.mp4", body)

    def test_saying_no_sends_the_user_nowhere(self):
        window = self.new_window()
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as asked, \
                mock.patch("video_converter.ui.main_window.QDesktopServices.openUrl") as opened:
            window.report_problem()
        asked.assert_called_once()
        self.assertIn("Nothing is sent until you press Submit", asked.call_args.args[2])
        opened.assert_not_called()

    def test_the_log_folder_opens(self):
        window = self.new_window()
        with tempfile.TemporaryDirectory() as folder:
            try:
                diagnostics.setup_logging(Path(folder) / "logs")
                with mock.patch("video_converter.ui.main_window.QDesktopServices.openUrl") as opened:
                    window.open_log_folder()
            finally:
                diagnostics.shutdown_logging()
        self.assertEqual(opened.call_args.args[0], QUrl.fromLocalFile(str(Path(folder) / "logs")))

    def test_about_shows_the_version(self):
        window = self.new_window()
        with mock.patch.object(QMessageBox, "about") as about:
            window.show_about()
        self.assertIn(__version__, about.call_args.args[2])
        self.assertIn(APP_NAME, about.call_args.args[1])

    def test_the_window_title_has_the_new_version(self):
        self.assertIn("2.6.0", self.new_window().windowTitle())


class CrashBoxTests(unittest.TestCase):
    def test_the_box_is_friendly_and_points_at_the_log_and_the_report(self):
        from video_converter.ui.app import crash_box
        box = crash_box("ValueError: bad thing")
        self.assertIn("Something unexpected", box.text())
        self.assertIn("log", box.informativeText())
        self.assertIn("Report a problem", box.informativeText())
        self.assertEqual(box.detailedText(), "ValueError: bad thing")
        self.assertNotIn("Traceback", box.text() + box.informativeText())

    def test_a_burst_of_errors_shows_one_box(self):
        from video_converter.ui.app import CrashReporter
        reporter = CrashReporter()
        with mock.patch.object(QMessageBox, "exec", return_value=0) as shown:
            for i in range(5):
                reporter.report(f"error {i}")
        self.assertEqual(shown.call_count, 1)

    def test_an_error_in_another_thread_reaches_the_ui_thread(self):
        from video_converter.ui.app import CrashReporter
        reporter = CrashReporter()
        threads = []
        with mock.patch.object(QMessageBox, "exec", side_effect=lambda: threads.append(threading.current_thread()) or 0):
            worker = threading.Thread(target=lambda: reporter.report("from a worker"))
            worker.start()
            worker.join()
            self.assertTrue(pump(5, lambda: threads))
        self.assertIs(threads[0], threading.main_thread())

    def test_the_hooks_feed_the_box(self):
        from video_converter.ui.app import CrashReporter
        reporter = CrashReporter()
        with tempfile.TemporaryDirectory() as folder:
            diagnostics.setup_logging(Path(folder))
            restore = diagnostics.install_excepthooks(reporter.report)
            try:
                with mock.patch.object(QMessageBox, "exec", return_value=0) as shown:
                    try:
                        raise RuntimeError("slot blew up")
                    except RuntimeError:
                        import sys
                        sys.excepthook(*sys.exc_info())
                    self.assertTrue(pump(5, lambda: shown.called))
            finally:
                restore()
                diagnostics.shutdown_logging()


if __name__ == "__main__":
    unittest.main()
