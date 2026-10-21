"""The automatic captions window, the caption editor, and how they work in the main window (undo included).
They run without a visible window and are skipped when PySide6 isn't installed."""

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    import shiboken6
    from PySide6.QtCore import QEvent, QSettings, Qt
    from PySide6.QtWidgets import QApplication, QDialog
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from tests.test_speech_models import FILES, tiny_model
from video_converter import autocaptions as ac
from video_converter import speech_models as sm
from video_converter.captions import Cue, format_srt, parse_cues, read_text
from video_converter.core import ConversionError, find_ffmpeg

FFMPEG = find_ffmpeg()
TIMEOUT = 30
CUES = [Cue(1.0, 2.5, "Hello there"), Cue(3.0, 4.0, "Second\nline"), Cue(5.0, 6.0, "مرحبا")]


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


def free(widget):
    widget.close()
    shiboken6.delete(widget)


class DialogTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.auto_captions_dialog import AutoCaptionsDialog
        self.cls = AutoCaptionsDialog
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = QSettings(str(self.tmp / "settings.ini"), QSettings.Format.IniFormat)
        self.models = self.tmp / "models"
        self.model = tiny_model()
        patcher = mock.patch.dict(sm.MODELS, {"fast": self.model})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            free(dialog)
        self._tmp.cleanup()

    def make(self, runner=None, store="default", **kwargs):
        def default_runner(ffmpeg, source, options, duration=None, **extra):
            self.calls.append((source, options, duration))
            return ac.CaptionResult(list(CUES), "en")

        dialog = self.cls("ffmpeg", Path("talk.mp4"), 12.0, kwargs.pop("start", None), kwargs.pop("end", None),
                          self.store if store == "default" else store, None, models_root=self.models,
                          runner=runner or default_runner)
        self.dialogs.append(dialog)
        return dialog

    def put_model(self):
        folder = sm.model_path(self.model, self.models)
        folder.mkdir(parents=True)
        for name, data in FILES.items():
            (folder / name).write_bytes(data)

    def test_it_starts_on_automatic_language_and_the_balanced_level(self):
        dialog = self.make()
        self.assertEqual((dialog.language, dialog.model_key, dialog.translate), ("", "balanced", False))
        self.assertEqual(dialog.language_box.itemText(0), "Detect automatically")

    def test_the_languages_include_the_ones_asked_for(self):
        names = [self.make().language_box.itemText(i) for i in range(self.make().language_box.count())]
        for name in ("Arabic", "English", "French", "Spanish", "German", "Turkish", "Hindi", "Urdu"):
            self.assertIn(name, names)

    def test_each_level_shows_its_download_size_and_whether_it_is_there_already(self):
        dialog = self.make()
        texts = {key: dialog.model_box.itemText(i) for i, key in enumerate(sm.MODELS)}
        self.assertEqual(texts["fast"], f"Fast ({self.model.size_text}, needs a download)")
        self.assertIn("141 MB", texts["balanced"])
        self.assertIn("464 MB", texts["accurate"])
        self.assertIn("needs a download", texts["accurate"])
        self.put_model()
        again = self.make()
        self.assertEqual(again.model_box.itemText(0), f"Fast ({self.model.size_text}, downloaded)")
        again.model_box.setCurrentIndex(0)
        self.assertIn("Already downloaded", again.model_note.text())
        again.model_box.setCurrentIndex(1)
        self.assertIn("one-time download of 141 MB", again.model_note.text())

    def test_the_help_text_says_it_runs_offline_after_a_one_time_download(self):
        text = " ".join(label.text() for label in self.make().findChildren(type(self.make().status)))
        self.assertIn("entirely on this PC", text)
        self.assertIn("once", text)
        self.assertIn("no internet is needed", text)

    def test_start_hands_the_choices_to_the_job_and_accepts_with_the_cues(self):
        dialog = self.make(start=2.0, end=9.0)
        dialog.language_box.setCurrentIndex(dialog.language_box.findData("ar"))
        dialog.model_box.setCurrentIndex(dialog.model_box.findData("accurate"))
        dialog.translate_box.setChecked(True)
        dialog.show()
        dialog.start_button.click()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.result() == QDialog.DialogCode.Accepted))
        source, options, duration = self.calls[0]
        self.assertEqual((options.model, options.language, options.translate, options.start, options.end),
                         ("accurate", "ar", True, 2.0, 9.0))
        self.assertEqual((source, duration), (Path("talk.mp4"), 12.0))
        self.assertEqual([c.text for c in dialog.outcome.cues], [c.text for c in CUES])

    def test_the_choices_are_remembered_for_next_time(self):
        dialog = self.make()
        dialog.language_box.setCurrentIndex(dialog.language_box.findData("tr"))
        dialog.model_box.setCurrentIndex(dialog.model_box.findData("fast"))
        dialog.translate_box.setChecked(True)
        dialog.start_button.click()
        pump(TIMEOUT, lambda: dialog.outcome is not None)
        self.store.sync()
        again = self.make(store=QSettings(str(self.tmp / "settings.ini"), QSettings.Format.IniFormat))
        self.assertEqual((again.language, again.model_key, again.translate), ("tr", "fast", True))

    def test_odd_saved_values_fall_back_to_the_defaults(self):
        self.store.setValue("captionsLanguage", "klingon")
        self.store.setValue("captionsModel", "gigantic")
        self.store.setValue("captionsTranslate", "banana")
        dialog = self.make()
        self.assertEqual((dialog.language, dialog.model_key, dialog.translate), ("", "balanced", False))

    def test_it_works_without_a_settings_store(self):
        dialog = self.make(store=None)
        dialog.start_button.click()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.outcome is not None))

    def test_progress_is_shown_for_each_step_in_turn(self):
        step = threading.Event()

        def runner(ffmpeg, source, options, duration=None, on_progress=None, cancel_event=None, **extra):
            for phase, fraction, text in (("prepare", 0.5, ""), ("download", 0.25, "30 of 120 MB"), ("load", 0.0, ""),
                                          ("listen", 0.4, "")):
                on_progress(phase, fraction, text)
                step.wait(TIMEOUT)
                step.clear()
            return ac.CaptionResult(list(CUES), "en")

        dialog = self.make(runner)
        dialog.show()
        dialog.start_button.click()
        for expected, value in (("Getting the sound ready", 500),
                                ("Downloading the speech model, one time only… 30 of 120 MB", 250),
                                ("Checking the speech model", None), ("Listening… 40%", 400)):
            self.assertTrue(pump(TIMEOUT, lambda: dialog.status.text().startswith(expected)
                                 and (value is None or dialog.bar.value() == value)),
                            f"never showed {expected!r}; showed {dialog.status.text()!r} at {dialog.bar.value()}")
            self.assertFalse(dialog.start_button.isEnabled())
            self.assertFalse(dialog.model_box.isEnabled())
            step.set()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.outcome is not None))

    def test_an_error_is_shown_and_the_window_stays_open_for_another_try(self):
        def runner(ffmpeg, source, options, duration=None, **extra):
            raise ConversionError("Couldn't download the speech model. Check that this PC is connected to the internet.")

        dialog = self.make(runner)
        dialog.show()
        dialog.start_button.click()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.error.isVisible()))
        self.assertIn("internet", dialog.error.text())
        self.assertTrue(dialog.isVisible())
        self.assertIsNone(dialog.outcome)
        self.assertTrue(dialog.start_button.isEnabled() and dialog.model_box.isEnabled())
        self.assertFalse(dialog.bar.isVisible())

    def test_an_unexpected_exception_is_shown_too(self):
        def runner(*args, **kwargs):
            raise ValueError("kaboom")

        dialog = self.make(runner)
        dialog.show()
        dialog.start_button.click()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.error.isVisible()))
        self.assertIn("kaboom", dialog.error.text())

    def test_a_video_with_no_speech_says_so_instead_of_closing(self):
        dialog = self.make(lambda *a, **k: ac.CaptionResult([], "en"))
        dialog.show()
        dialog.start_button.click()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.error.isVisible()))
        self.assertIn("No speech was found", dialog.error.text())
        self.assertIsNone(dialog.outcome)

    def test_a_second_try_after_an_error_works(self):
        answers = [ConversionError("first fails"), ac.CaptionResult(list(CUES), "en")]

        def runner(*args, **kwargs):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        dialog = self.make(runner)
        dialog.show()
        dialog.start_button.click()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.error.isVisible()))
        dialog.start_button.click()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.outcome is not None))
        self.assertFalse(dialog.error.isVisible())

    def test_cancel_while_working_stops_the_job_and_closes(self):
        started, stopped = threading.Event(), threading.Event()

        def runner(ffmpeg, source, options, duration=None, on_progress=None, cancel_event=None, **extra):
            started.set()
            cancel_event.wait(TIMEOUT)
            stopped.set()
            raise ac.ConversionCancelled()

        dialog = self.make(runner)
        dialog.show()
        dialog.start_button.click()
        self.assertTrue(started.wait(TIMEOUT))
        dialog.cancel_button.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertTrue(stopped.wait(TIMEOUT))
        self.assertIsNone(dialog.outcome)

    def test_a_result_arriving_after_cancel_is_ignored(self):
        release = threading.Event()

        def runner(*args, **kwargs):
            release.wait(TIMEOUT)
            return ac.CaptionResult(list(CUES), "en")

        dialog = self.make(runner)
        dialog.show()
        dialog.start_button.click()
        dialog.reject()
        release.set()
        pump(0.3)
        self.assertIsNone(dialog.outcome)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)


    def test_a_second_run_waits_while_the_previous_worker_is_still_finishing(self):
        started, block = threading.Event(), threading.Event()

        def slow(ffmpeg, source, options, duration=None, cancel_event=None, **extra):
            started.set()
            block.wait(TIMEOUT)                         # a worker that takes a while to notice the cancel
            raise ac.ConversionCancelled()

        first = self.make(slow)
        first.show()
        first.start_button.click()
        self.assertTrue(started.wait(TIMEOUT))
        first.reject()                                  # the window is closed; the worker is still running
        self.assertTrue(self.cls.previous_run_active())

        second = self.make()
        second.show()
        second.start_button.click()
        self.assertEqual(second.status.text(), "Stopping the previous run…")
        self.assertEqual(self.calls, [])                # not started
        pump(0.5)
        self.assertEqual(self.calls, [])                # still waiting
        block.set()                                     # the old worker lets go
        self.assertTrue(pump(TIMEOUT, lambda: second.outcome is not None))
        self.assertEqual(len(self.calls), 1)            # exactly one run happened

    def test_closing_the_window_while_it_waits_starts_nothing(self):
        started, block = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            started.set()
            block.wait(TIMEOUT)
            raise ac.ConversionCancelled()

        first = self.make(slow)
        first.show()
        first.start_button.click()
        self.assertTrue(started.wait(TIMEOUT))
        first.reject()
        second = self.make()
        second.show()
        second.start_button.click()
        second.reject()
        block.set()
        pump(0.6)
        self.assertEqual(self.calls, [])
        self.assertIsNone(second.outcome)

    def test_without_an_earlier_worker_a_run_starts_at_once(self):
        dialog = self.make()
        dialog.show()
        self.assertFalse(self.cls.previous_run_active())
        dialog.start_button.click()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.outcome is not None))

class EditorTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.captions_editor import CaptionsEditor
        self.editor = CaptionsEditor(CUES)
        self.table = self.editor.table

    def tearDown(self):
        free(self.editor)

    def test_every_cue_is_a_row_with_start_end_and_text(self):
        self.assertEqual(self.table.rowCount(), 3)
        self.assertEqual([self.table.item(0, c).text() for c in range(3)], ["0:01.000", "0:02.500", "Hello there"])
        self.assertEqual(self.table.item(1, 2).text(), "Second\nline")
        self.assertEqual(self.table.item(2, 2).text(), "مرحبا")
        self.assertIn("3 captions", self.editor.count_label.text())

    def test_times_over_an_hour_show_the_hour(self):
        from video_converter.ui.captions_editor import clock
        self.assertEqual(clock(3661.5), "1:01:01.500")
        self.assertEqual(clock(59.9996), "1:00.000")
        self.assertEqual(clock(0), "0:00.000")

    def test_editing_the_words_changes_the_cue(self):
        self.table.item(0, 2).setText("Hello, world")
        self.editor.accept()
        self.assertEqual(self.editor.cues[0].text, "Hello, world")

    def test_editing_a_time_accepts_clock_and_seconds_forms(self):
        self.table.item(0, 0).setText("0:00.5")
        self.table.item(0, 1).setText("3")
        self.assertEqual(self.table.item(0, 0).text(), "0:00.500")           # shown in the standard form
        self.editor.accept()
        self.assertEqual((self.editor.cues[0].start, self.editor.cues[0].end), (0.5, 3.0))

    def test_a_time_that_is_not_a_time_is_put_back_with_a_message(self):
        self.table.item(0, 0).setText("soon")
        self.assertEqual(self.table.item(0, 0).text(), "0:01.000")
        self.assertFalse(self.editor.message.isHidden())
        self.editor.accept()
        self.assertEqual(self.editor.cues[0].start, 1.0)

    def test_an_end_before_the_start_is_refused(self):
        self.table.item(0, 1).setText("0:00.500")
        self.assertEqual(self.table.item(0, 1).text(), "0:02.500")
        self.assertIn("end after", self.editor.message.text())

    def test_moving_the_start_past_the_end_moves_the_whole_caption(self):
        self.table.item(0, 0).setText("0:03")
        self.assertEqual([self.table.item(0, c).text() for c in (0, 1)], ["0:03.000", "0:04.500"])
        self.editor.accept()
        self.assertEqual([(c.start, c.end) for c in self.editor.cues][1], (3.0, 4.5))

    def test_a_good_edit_clears_the_message(self):
        self.table.item(0, 0).setText("soon")
        self.table.item(0, 0).setText("0:00.9")
        self.assertTrue(self.editor.message.isHidden())

    def test_delete_removes_the_selected_captions(self):
        self.table.selectRow(1)
        self.assertTrue(self.editor.delete_button.isEnabled())
        self.editor.delete_button.click()
        self.assertEqual(self.table.rowCount(), 2)
        self.assertEqual([self.table.item(r, 2).text() for r in range(2)], ["Hello there", "مرحبا"])
        self.editor.accept()
        self.assertEqual([c.text for c in self.editor.cues], ["Hello there", "مرحبا"])
        self.assertIn("2 captions", self.editor.count_label.text())

    def test_edits_after_a_delete_land_on_the_right_cue(self):
        self.table.selectRow(0)
        self.editor.delete_selected()
        self.table.item(0, 2).setText("changed")
        self.editor.accept()
        self.assertEqual([c.text for c in self.editor.cues], ["changed", "مرحبا"])

    def test_several_rows_can_be_deleted_at_once(self):
        self.table.selectAll()
        self.editor.delete_selected()
        self.assertEqual(self.table.rowCount(), 0)
        self.assertFalse(self.editor.delete_button.isEnabled())

    def test_delete_is_off_with_nothing_selected(self):
        self.assertFalse(self.editor.delete_button.isEnabled())

    def test_saving_sorts_by_time_and_drops_captions_with_no_words(self):
        self.table.item(0, 0).setText("0:10")
        self.table.item(0, 1).setText("0:12")
        self.table.item(1, 2).setText("   ")
        self.editor.accept()
        self.assertEqual([c.text for c in self.editor.cues], ["مرحبا", "Hello there"])

    def test_the_text_editor_takes_more_than_one_line(self):
        from video_converter.ui.captions_editor import _TextDelegate
        delegate = self.table.itemDelegateForColumn(2)
        self.assertIsInstance(delegate, _TextDelegate)
        index = self.table.model().index(1, 2)
        box = delegate.createEditor(self.table, None, index)
        delegate.setEditorData(box, index)
        self.assertEqual(box.toPlainText(), "Second\nline")
        box.setPlainText("one\ntwo\n")
        delegate.setModelData(box, self.table.model(), index)
        shiboken6.delete(box)
        self.assertEqual(self.table.item(1, 2).text(), "one\ntwo")

    def test_the_cues_passed_in_are_not_changed_by_editing(self):
        self.table.item(0, 2).setText("different")
        self.assertEqual(CUES[0].text, "Hello there")


    def test_times_that_are_not_real_numbers_are_refused(self):
        from video_converter.ui.captions_editor import clock
        for column in (0, 1):
            for text in ("nan", "NaN", "inf", "-inf", "1e999", "-5", "0:-10"):
                with self.subTest(column=column, text=text):
                    before = (self.editor.cues[0].start, self.editor.cues[0].end)
                    shown = self.table.item(0, column).text()
                    self.table.item(0, column).setText(text)
                    self.assertEqual((self.editor.cues[0].start, self.editor.cues[0].end), before)
                    self.assertEqual(self.table.item(0, column).text(), shown)           # put back
                    self.assertFalse(self.editor.message.isHidden())
                    self.assertTrue(self.editor.message.text())            # some message says why
                    self.editor.message.hide()
        self.assertEqual(clock(float("nan")), "0:00.000")
        self.assertEqual(clock(float("inf")), "0:00.000")
        self.assertEqual(clock(-float("inf")), "0:00.000")

    def test_nothing_that_is_not_finite_can_be_saved(self):
        from video_converter.captions import format_srt
        self.editor.cues[0].start = float("nan")
        self.editor.cues[1].end = float("inf")
        self.editor.accept()
        self.assertEqual([c.text for c in self.editor.cues], ["مرحبا"])
        format_srt(self.editor.cues)
        self.assertIn("00:00:00,000", format_srt([Cue(float("nan"), float("inf"), "x")]))          # defensive: no crash

    def test_times_far_past_the_end_of_the_video_are_refused(self):
        self.editor.duration = 10.0
        self.table.item(0, 1).setText("70")                      # 60 s past the end is the limit
        self.assertEqual(self.editor.cues[0].end, 70.0)
        self.assertTrue(self.editor.message.isHidden())
        self.table.item(0, 1).setText("70.5")
        self.assertEqual(self.editor.cues[0].end, 70.0)
        self.assertFalse(self.editor.message.isHidden())
        self.table.item(1, 0).setText("500")
        self.assertEqual(self.editor.cues[1].start, 3.0)
        self.table.item(2, 0).setText("69.5")                    # would push the end past the limit too
        self.assertEqual(self.editor.cues[2].start, 5.0)

    def test_without_a_duration_only_the_real_numbers_check_applies(self):
        self.table.item(0, 1).setText("100000")
        self.assertEqual(self.editor.cues[0].end, 100000.0)

class PanelTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.inspector import Inspector
        from video_converter.project import ClipSettings
        from video_converter.video import VideoEdit
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.inspector = Inspector()
        self.panel = self.inspector.captions
        self.asked = []
        self.inspector.autoCaptionsRequested.connect(lambda: self.asked.append("auto"))
        self.inspector.editCaptionsRequested.connect(lambda: self.asked.append("edit"))
        self.ClipSettings, self.VideoEdit = ClipSettings, VideoEdit

    def tearDown(self):
        free(self.inspector)
        self._tmp.cleanup()

    def show(self, file=""):
        edit = self.VideoEdit()
        edit.subtitles.file = str(file) if file else ""
        self.inspector.set_settings(self.ClipSettings(edit=edit), 10.0)

    def test_the_buttons_ask_the_window_to_make_or_edit_captions(self):
        self.show()
        self.panel.auto_button.click()
        self.assertEqual(self.asked, ["auto"])
        srt = self.tmp / "a.srt"
        srt.write_text(format_srt(CUES), encoding="utf-8")
        self.show(srt)
        self.panel.edit_captions_button.click()
        self.assertEqual(self.asked, ["auto", "edit"])

    def test_the_help_under_the_button_mentions_the_one_time_download(self):
        self.assertIn("one-time download", self.panel.auto_hint.text())

    def test_edit_captions_is_hidden_without_a_subtitle_file(self):
        self.show()
        self.assertTrue(self.panel.edit_captions_button.isHidden())

    def test_edit_captions_is_available_for_srt_and_vtt_files_that_exist(self):
        for suffix in (".srt", ".vtt", ".SRT"):
            with self.subTest(suffix=suffix):
                path = self.tmp / f"a{suffix}"
                path.write_text("x", encoding="utf-8")
                self.show(path)
                self.assertFalse(self.panel.edit_captions_button.isHidden())
                self.assertTrue(self.panel.edit_captions_button.isEnabled())

    def test_it_is_off_for_ass_files_and_for_a_file_that_is_gone(self):
        ass = self.tmp / "a.ass"
        ass.write_text("x", encoding="utf-8")
        self.show(ass)
        self.assertFalse(self.panel.edit_captions_button.isEnabled())
        self.assertIn("Only .srt and .vtt", self.panel.edit_captions_button.toolTip())
        self.show(self.tmp / "gone.srt")
        self.assertFalse(self.panel.edit_captions_button.isEnabled())
        self.assertIn("can't be found", self.panel.edit_captions_button.toolTip())


@unittest.skipUnless(FFMPEG, "ffmpeg is needed")
class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @staticmethod
    def make_video(path, sound=True):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
               "color=c=0x203040:s=320x180:d=4:r=15"]
        if sound:
            cmd += ["-f", "lavfi", "-i", "sine=frequency=300:duration=4", "-c:a", "aac"]
        subprocess.run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)

    def setUp(self):
        from video_converter.ui import main_window
        from video_converter.ui.main_window import MainWindow
        self.folder = Path(tempfile.mkdtemp(dir=self.tmp))
        self.video = self.folder / "clip.mp4"
        self.make_video(self.video)
        self.main_window = main_window
        self.window = MainWindow(FFMPEG, QSettings(str(self.folder / "s.ini"), QSettings.Format.IniFormat))
        self.window.models_root = self.folder / "models"
        self.window.resize(1200, 800)
        self.window.show()
        self.window.add_paths([self.video])
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)), "files never loaded")
        self.item = self.window.current
        self.window.undo_stack.clear()

    def tearDown(self):
        self.window.close()
        self.window.loader.shutdown(wait=True)
        self.window.deleteLater()
        self.window = None
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(0.1)

    def fake_dialog(self, cues=None, accept=True):
        """A stand-in for the captions window that answers at once."""
        outcome = ac.CaptionResult(list(CUES if cues is None else cues), "en") if cues != [] else None
        shown = []

        class Dialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, ffmpeg, source, duration, start, end, store, parent, models_root=None):
                shown.append(SimpleNamespace(source=source, duration=duration, start=start, end=end,
                                             models_root=models_root))
                self.outcome = outcome

            def exec(self):
                return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

            def deleteLater(self):
                pass

        return mock.patch.object(self.main_window, "AutoCaptionsDialog", Dialog), shown

    def fake_editor(self, cues, accept=True):
        class Editor:
            DialogCode = QDialog.DialogCode

            def __init__(self, original, parent):
                self.cues = cues

            def exec(self):
                return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

            def deleteLater(self):
                pass

        return mock.patch.object(self.main_window, "CaptionsEditor", Editor)

    def subtitles(self):
        return self.item.settings.edit.subtitles.file

    # ----- making captions -------------------------------------------------------------

    def test_making_captions_saves_the_srt_next_to_the_video_and_burns_it_in(self):
        patch, shown = self.fake_dialog()
        with patch:
            self.window.make_captions()
        srt = self.folder / "clip.srt"
        self.assertEqual(Path(self.subtitles()), srt)
        self.assertEqual([(c.start, c.end, c.text) for c in parse_cues(srt.read_text("utf-8"))],
                         [(c.start, c.end, c.text) for c in CUES])
        self.assertEqual(shown[0].source, self.video)
        self.assertEqual(shown[0].models_root, self.folder / "models")
        self.assertIn("clip.srt", self.window.statusBar().currentMessage())

    def test_the_window_gets_the_trim_so_only_that_part_is_listened_to(self):
        new = self.item.settings.copy()
        new.edit.start, new.edit.end = 1.0, 3.0
        self.window.push_settings(new, "Trim")
        self.window.undo_stack.clear()
        patch, shown = self.fake_dialog()
        with patch:
            self.window.make_captions()
        self.assertEqual((shown[0].start, shown[0].end), (1.0, 3.0))

    def test_making_captions_is_one_undo_step_and_redo_brings_them_back(self):
        patch, _ = self.fake_dialog()
        with patch:
            self.window.make_captions()
        self.assertEqual(self.window.undo_stack.count(), 1)
        self.assertEqual(self.window.undo_stack.undoText(), "Make captions")
        self.window.undo_stack.undo()
        self.assertEqual(self.subtitles(), "")
        self.assertTrue((self.folder / "clip.srt").is_file())            # the saved file is the person's to keep
        self.window.undo_stack.redo()
        self.assertEqual(Path(self.subtitles()), self.folder / "clip.srt")

    def test_a_second_run_does_not_overwrite_the_first_srt(self):
        patch, _ = self.fake_dialog()
        with patch:
            self.window.make_captions()
            self.window.make_captions()
        self.assertEqual(Path(self.subtitles()), self.folder / "clip (1).srt")
        self.assertTrue((self.folder / "clip.srt").is_file())
        self.assertEqual(self.window.undo_stack.count(), 2)
        self.window.undo_stack.undo()
        self.assertEqual(Path(self.subtitles()), self.folder / "clip.srt")

    def test_cancelling_the_window_changes_nothing(self):
        patch, _ = self.fake_dialog(accept=False)
        with patch:
            self.window.make_captions()
        self.assertEqual(self.subtitles(), "")
        self.assertEqual(self.window.undo_stack.count(), 0)
        self.assertFalse((self.folder / "clip.srt").exists())

    def test_a_file_without_sound_says_so_and_opens_no_window(self):
        silent = self.folder / "mute.mp4"
        self.make_video(silent, sound=False)
        self.window.add_paths([silent])
        pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items))
        self.window.select(self.window.items[1])
        patch, shown = self.fake_dialog()
        with patch:
            self.window.make_captions()
        self.assertEqual(shown, [])
        self.assertIn("no sound", self.window.statusBar().currentMessage())

    def test_an_srt_that_cannot_be_saved_is_reported_and_nothing_changes(self):
        patch, _ = self.fake_dialog()
        with patch, mock.patch.object(self.main_window, "save_captions",
                                      side_effect=ConversionError("Couldn't save the captions: disk full")), \
                mock.patch.object(self.main_window.QMessageBox, "warning") as warning:
            self.window.make_captions()
        warning.assert_called_once()
        self.assertIn("disk full", warning.call_args.args[2])
        self.assertEqual(self.subtitles(), "")
        self.assertEqual(self.window.undo_stack.count(), 0)

    def test_the_captions_button_opens_the_window_from_the_panel(self):
        patch, shown = self.fake_dialog()
        with patch:
            self.window.inspector.captions.auto_button.click()
        self.assertEqual(len(shown), 1)
        self.assertTrue(self.window.inspector.captions.edit_captions_button.isEnabled())

    # ----- editing captions --------------------------------------------------------------

    def attach(self, cues=CUES, name="clip.srt"):
        srt = self.folder / name
        srt.write_text(format_srt(cues), encoding="utf-8")
        new = self.item.settings.copy()
        new.edit.subtitles.file = str(srt)
        self.window.push_settings(new, "Choose subtitles")
        self.window.undo_stack.clear()
        return srt

    def test_saving_edited_captions_rewrites_the_srt_as_one_undo_step(self):
        srt = self.attach()
        original = srt.read_bytes()
        edited = [Cue(1.0, 2.5, "Hello, world"), Cue(5.0, 6.5, "مرحبا")]
        with self.fake_editor(edited):
            self.window.edit_captions()
        self.assertEqual([(c.start, c.end, c.text) for c in parse_cues(srt.read_text("utf-8"))],
                         [(c.start, c.end, c.text) for c in edited])
        self.assertEqual(self.window.undo_stack.count(), 1)
        self.assertEqual(self.window.undo_stack.undoText(), "Edit captions")
        self.window.undo_stack.undo()
        self.assertEqual(srt.read_bytes(), original)
        self.window.undo_stack.redo()
        self.assertEqual(len(parse_cues(srt.read_text("utf-8"))), 2)
        self.assertEqual(Path(self.subtitles()), srt)

    def test_the_editor_gets_the_captions_that_are_in_the_file(self):
        self.attach()
        opened = []

        class Editor:
            DialogCode = QDialog.DialogCode

            def __init__(self, cues, parent):
                opened.append(cues)
                self.cues = cues

            def exec(self):
                return QDialog.DialogCode.Rejected

            def deleteLater(self):
                pass

        with mock.patch.object(self.main_window, "CaptionsEditor", Editor):
            self.window.edit_captions()
        self.assertEqual([c.text for c in opened[0]], [c.text for c in CUES])

    def test_leaving_the_editor_without_a_change_makes_no_undo_step(self):
        srt = self.attach()
        before = srt.read_bytes()
        with self.fake_editor(list(CUES)):
            self.window.edit_captions()
        with self.fake_editor([Cue(0, 1, "x")], accept=False):
            self.window.edit_captions()
        self.assertEqual(self.window.undo_stack.count(), 0)
        self.assertEqual(srt.read_bytes(), before)

    def test_deleting_every_caption_is_allowed_and_undoable(self):
        srt = self.attach()
        with self.fake_editor([]):
            self.window.edit_captions()
        self.assertEqual(parse_cues(srt.read_text("utf-8")), [])
        self.window.undo_stack.undo()
        self.assertEqual(len(parse_cues(srt.read_text("utf-8"))), 3)

    def test_edits_to_a_vtt_file_go_to_a_new_srt_and_leave_the_vtt_alone(self):
        vtt = self.folder / "clip.vtt"
        vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n", encoding="utf-8")
        new = self.item.settings.copy()
        new.edit.subtitles.file = str(vtt)
        self.window.push_settings(new, "Choose subtitles")
        self.window.undo_stack.clear()
        with self.fake_editor([Cue(1.0, 2.0, "Hello!")]):
            self.window.edit_captions()
        self.assertEqual(Path(self.subtitles()), self.folder / "clip.srt")
        self.assertIn("Hello\n", vtt.read_text("utf-8"))
        self.assertEqual(self.window.undo_stack.count(), 1)
        self.window.undo_stack.undo()
        self.assertEqual(Path(self.subtitles()), vtt)

    def test_a_missing_subtitle_file_gives_a_message_not_a_window(self):
        new = self.item.settings.copy()
        new.edit.subtitles.file = str(self.folder / "gone.srt")
        self.window.push_settings(new, "Choose subtitles")
        self.window.undo_stack.clear()
        with self.fake_editor([]) as _:
            self.window.edit_captions()
        self.assertIn("can't be read", self.window.statusBar().currentMessage())

    def test_the_whole_chain_make_edit_undo_undo(self):
        patch, _ = self.fake_dialog()
        with patch:
            self.window.make_captions()
        srt = self.folder / "clip.srt"
        with self.fake_editor([Cue(1.0, 2.0, "only")]):
            self.window.edit_captions()
        self.assertEqual(self.window.undo_stack.count(), 2)
        self.window.undo_stack.undo()
        self.assertEqual(len(parse_cues(srt.read_text("utf-8"))), 3)
        self.window.undo_stack.undo()
        self.assertEqual(self.subtitles(), "")


    # ----- fixes ---------------------------------------------------------------------------

    def attach_bytes(self, data: bytes, name="clip.srt"):
        srt = self.folder / name
        srt.write_bytes(data)
        new = self.item.settings.copy()
        new.edit.subtitles.file = str(srt)
        self.window.push_settings(new, "Choose subtitles")
        self.window.undo_stack.clear()
        return srt

    def capturing_editor(self, opened, change=lambda cues: cues):
        class Editor:
            DialogCode = QDialog.DialogCode

            def __init__(self, cues, parent):
                opened.append([Cue(c.start, c.end, c.text) for c in cues])
                self.cues = change([Cue(c.start, c.end, c.text) for c in cues])

            def exec(self):
                return QDialog.DialogCode.Accepted

            def deleteLater(self):
                pass

        return mock.patch.object(self.main_window, "CaptionsEditor", Editor)

    ARABIC = [Cue(1.0, 2.5, "مرحبا بكم في قناتنا"), Cue(3.0, 4.0, "شكرا لكم على المتابعة")]

    def test_editing_a_windows_1256_file_shows_arabic_and_keeps_a_backup_of_the_original(self):
        original = format_srt(self.ARABIC).encode("cp1256")
        srt = self.attach_bytes(original)
        opened = []

        def reword(cues):
            cues[0].text += "!"
            return cues

        with self.capturing_editor(opened, reword):
            self.window.edit_captions()
        self.assertEqual([c.text for c in opened[0]], [c.text for c in self.ARABIC])         # not mojibake
        data = srt.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))                  # UTF-8 with a byte order mark
        self.assertEqual([c.text for c in parse_cues(data.decode("utf-8-sig"))],
                         [self.ARABIC[0].text + "!", self.ARABIC[1].text])
        backup = self.folder / "clip.srt.bak"
        self.assertEqual(backup.read_bytes(), original)
        # a second edit leaves the very first backup alone
        with self.capturing_editor([], lambda cues: [Cue(0.0, 1.0, "again")]):
            self.window.edit_captions()
        self.assertEqual(backup.read_bytes(), original)
        self.window.undo_stack.undo()
        self.window.undo_stack.undo()
        self.assertEqual(srt.read_bytes(), original)

    def test_a_utf16_file_without_a_byte_order_mark_is_read_properly(self):
        srt = self.attach_bytes(format_srt(self.ARABIC).encode("utf-16-le"))
        opened = []
        with self.capturing_editor(opened, lambda cues: cues[:1]):
            self.window.edit_captions()
        self.assertEqual([c.text for c in opened[0]], [c.text for c in self.ARABIC])
        self.assertEqual([c.text for c in parse_cues(srt.read_bytes().decode("utf-8-sig"))], [self.ARABIC[0].text])

    def test_an_edited_file_always_has_a_byte_order_mark(self):
        srt = self.attach()
        with self.fake_editor([Cue(1.0, 2.0, "مرحبا")]):
            self.window.edit_captions()
        self.assertTrue(srt.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_editing_a_vtt_file_says_a_new_srt_was_made(self):
        vtt = self.folder / "clip.vtt"
        vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n", encoding="utf-8")
        new = self.item.settings.copy()
        new.edit.subtitles.file = str(vtt)
        self.window.push_settings(new, "Choose subtitles")
        self.window.undo_stack.clear()
        with self.fake_editor([Cue(1.0, 2.0, "Hello!")]):
            self.window.edit_captions()
        message = self.window.statusBar().currentMessage()
        self.assertIn("clip.srt", message)
        self.assertIn("left as it is", message)

    def test_captions_that_cannot_be_saved_next_to_the_video_are_saved_elsewhere(self):
        elsewhere = self.folder / "elsewhere"
        real = self.main_window.save_captions

        def save(cues, source, output_dir=None, overwrite=False):
            if output_dir is None:
                raise ConversionError("Couldn't save the captions: access denied")
            return real(cues, source, output_dir, overwrite)

        patch, _ = self.fake_dialog()
        with patch, mock.patch.object(self.main_window, "save_captions", save), \
                mock.patch.object(ac, "fallback_folders", return_value=[elsewhere]), \
                mock.patch.object(self.main_window.QMessageBox, "warning") as warning:
            self.window.make_captions()
        warning.assert_not_called()
        saved = elsewhere / "clip.srt"
        self.assertEqual(Path(self.subtitles()), saved)                      # the cues are in use, not lost
        self.assertEqual([c.text for c in parse_cues(read_text(saved))], [c.text for c in CUES])
        self.assertEqual(self.window.undo_stack.count(), 1)
        self.assertIn("saved here instead", self.window.statusBar().currentMessage())

if __name__ == "__main__":
    unittest.main()
