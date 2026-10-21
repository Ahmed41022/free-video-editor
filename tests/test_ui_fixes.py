"""Regression tests for editor bugs: keys typed into the settings panel, the mouse wheel, undo and redo across files,
removing files, audio-only files, files that can't be read, and the export queue. They run without a visible window
and are skipped when PySide6 isn't installed."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("VIDEO_EDITOR_NO_GPU_SEARCH", "1")      # a window never searches for graphics cards (see tests/__init__.py)
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    import shiboken6  # noqa: F401
    from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QCheckBox, QInputDialog, QMenu, QMessageBox
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter.core import find_ffmpeg
from video_converter.project import OUTPUT_AUDIO, OUTPUT_VIDEO

from tests.test_ui import make_video, pump

FFMPEG = find_ffmpeg()
TIMEOUT = 30


def setUpModule():
    if QApplication is None:
        raise unittest.SkipTest("PySide6 is not installed")
    global app
    app = QApplication.instance() or QApplication([])


def wheel(widget, delta=-120):
    """Send one turn of the mouse wheel over the middle of `widget`, the way the system would."""
    center = QPointF(widget.rect().center())
    event = QWheelEvent(center, QPointF(widget.mapToGlobal(widget.rect().center())), QPoint(0, 0), QPoint(0, delta),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)
    return event


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class WindowTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.first = cls.tmp / "first.mp4"
        cls.second = cls.tmp / "second.mp4"
        make_video(cls.first, seconds=2)
        make_video(cls.second, seconds=2)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        from video_converter.ui.main_window import MainWindow
        ini = self.tmp / f"settings-{self.id().split('.')[-1]}.ini"
        with mock.patch("video_converter.gpu.detect_encoders", return_value=()), \
                mock.patch("video_converter.gpu.gpu_name", return_value=None):
            self.window = MainWindow(FFMPEG, QSettings(str(ini), QSettings.Format.IniFormat))
        self.window.resize(1200, 800)
        self.window.show()
        self.window.activateWindow()

    def tearDown(self):
        window = self.window
        tmpdir = window.tmpdir
        window.queue.cancel_all()
        pump(30, lambda: not window.queue.busy)
        window.close()
        window.loader.shutdown(wait=True)
        self.assertFalse(tmpdir.exists(), "the window left its temp folder behind")
        window.deleteLater()
        self.window = None
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(0.1)

    def load(self, *paths):
        self.window.add_paths(list(paths))
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)), "files never loaded")

    def labels(self):
        stack = self.window.undo_stack
        return [stack.text(i) for i in range(stack.count())]


class ShortcutKeyTests(WindowTestCase):
    SENTENCE = "Six tasks look like Bob loves it"

    def add_text(self):
        self.load(self.first)
        panel = self.window.inspector.captions
        self.window.inspector.captions_section.header.setChecked(True)
        panel.add_button.click()
        self.assertEqual(len(self.window.current.settings.edit.texts), 1)
        self.assertTrue(panel.text_box.isVisible())
        return panel

    def test_typing_in_the_text_box_is_typing_not_shortcuts(self):
        panel = self.add_text()
        box = panel.text_box
        box.setFocus()
        box.selectAll()
        before = self.window.undo_stack.count()
        QTest.keyClicks(box, self.SENTENCE)
        self.assertEqual(box.toPlainText(), self.SENTENCE)
        self.assertEqual(self.window.current.settings.edit.texts[0].text, self.SENTENCE)
        for label in self.labels():
            self.assertIn(label, ("Add text", "Edit text"))
        self.assertLessEqual(self.window.undo_stack.count() - before, 1)
        self.assertEqual(self.window.current.settings.edit.keep, [])
        self.assertIsNone(self.window.current.settings.edit.start)
        self.assertIsNone(self.window.current.settings.edit.end)

    def test_space_on_a_check_box_toggles_it_and_does_not_play(self):
        self.load(self.first)
        box = self.window.inspector.mirror_box
        box.setFocus()
        self.assertTrue(box.hasFocus())
        QTest.keyClick(box, Qt.Key.Key_Space)
        self.assertTrue(box.isChecked())
        self.assertFalse(self.window.player.playing)
        self.assertTrue(self.window.current.settings.edit.flip)

    def test_keys_are_left_to_sliders_combos_and_buttons(self):
        self.load(self.first)
        window = self.window
        item = window.current
        window.player.seek(1.0)
        pump(2, lambda: abs(window.player.position - 1.0) < 0.05)
        position = window.player.position
        slider = window.inspector.volume_slider
        slider.setFocus()
        QTest.keyClick(slider, Qt.Key.Key_Right)
        self.assertEqual(slider.value(), 1)                       # the slider moved, the playhead did not
        self.assertAlmostEqual(window.player.position, position, delta=0.02)
        combo = window.inspector.format_box
        combo.setFocus()
        QTest.keyClick(combo, Qt.Key.Key_S)                       # type-ahead, not a split
        self.assertEqual(item.settings.edit.keep, [])
        button = window.inspector.remove_part_button
        button.setFocus()
        for key in (Qt.Key.Key_X, Qt.Key.Key_I, Qt.Key.Key_O, Qt.Key.Key_J, Qt.Key.Key_L):
            QTest.keyClick(button, key)
        self.assertIsNone(item.settings.edit.start)
        self.assertIsNone(item.settings.edit.end)
        self.assertAlmostEqual(window.player.position, position, delta=0.02)

    def test_the_arabic_keyboard_fallback_does_not_steal_keys_from_the_text_box(self):
        panel = self.add_text()
        box = panel.text_box
        box.setFocus()
        # a letter key typed on an Arabic layout: Qt reports a non-Latin key but the physical key is "S" (0x53)
        event = __import__("PySide6.QtGui", fromlist=["QKeyEvent"]).QKeyEvent(
            QEvent.Type.KeyPress, 0x0633, Qt.KeyboardModifier.NoModifier, 0x1f, 0x53, 0, "س")
        QApplication.sendEvent(box, event)
        self.assertIn("س", box.toPlainText())
        self.assertEqual(self.window.current.settings.edit.keep, [])

    def test_the_shortcuts_still_work_on_the_list_the_video_and_the_window(self):
        self.load(self.first)
        window = self.window
        window.player.seek(1.0)
        pump(2, lambda: abs(window.player.position - 1.0) < 0.05)
        for widget in (window.media_list, window.view):
            window.setUpdatesEnabled(True)
            widget.setFocus()
            window.undo_stack.clear()
            window.current.settings.edit.keep = []
            QTest.keyClick(widget, Qt.Key.Key_S)
            self.assertEqual(len(window.current.settings.edit.keep), 2, type(widget).__name__)
        window.undo_stack.undo()

    def test_tab_leaves_the_text_box(self):
        panel = self.add_text()
        self.assertTrue(panel.text_box.tabChangesFocus())


class WheelTests(WindowTestCase):
    def test_the_wheel_over_a_setting_scrolls_the_panel_instead_of_changing_it(self):
        self.load(self.first)
        inspector = self.window.inspector
        pump(0.3)
        combo = inspector.format_box
        bar = inspector.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0, "the panel has nothing to scroll")
        inspector.ensureWidgetVisible(combo)
        pump(0.1)
        combo.setCurrentIndex(0)
        index, count, scrolled = combo.currentIndex(), self.window.undo_stack.count(), bar.value()
        bar.setValue(bar.maximum() // 2)
        scrolled = bar.value()
        wheel(combo, -120)
        self.assertEqual(combo.currentIndex(), index)
        self.assertEqual(self.window.undo_stack.count(), count)
        self.assertGreater(bar.value(), scrolled, "the wheel did not scroll the panel")

    def test_the_wheel_over_every_kind_of_setting_leaves_it_alone(self):
        self.load(self.first)
        inspector = self.window.inspector
        pump(0.3)
        count = self.window.undo_stack.count()
        for widget in (inspector.speed_box, inspector.volume_slider, inspector.size_box, inspector.rate_box,
                       inspector.zoom_slider, inspector.crf_box):
            before = widget.value() if hasattr(widget, "value") else widget.currentIndex()
            wheel(widget, -120)
            wheel(widget, 120)
            after = widget.value() if hasattr(widget, "value") else widget.currentIndex()
            self.assertEqual(before, after, type(widget).__name__)
        self.assertEqual(self.window.undo_stack.count(), count)

    def test_a_setting_with_the_keyboard_focus_still_takes_the_wheel(self):
        self.load(self.first)
        inspector = self.window.inspector
        pump(0.3)
        combo = inspector.format_box
        combo.setFocus()
        self.assertTrue(combo.hasFocus())
        index = combo.currentIndex()
        wheel(combo, -120)
        self.assertNotEqual(combo.currentIndex(), index)


class UndoAcrossFilesTests(WindowTestCase):
    def test_undo_and_redo_show_the_file_they_change(self):
        self.load(self.first, self.second)
        window = self.window
        first, second = window.items
        self.assertIs(window.current, first)
        window.inspector.shape_group.button(2).click()           # 1:1, on the first file
        self.assertEqual(first.settings.edit.aspect, (1, 1))
        window.media_list.setCurrentRow(1)
        self.assertIs(window.current, second)
        window.undo_stack.undo()
        self.assertIs(window.current, first, "undo changed a file that is not on show")
        self.assertEqual(window.media_list.currentRow(), 0)
        self.assertEqual(first.settings.edit.aspect, (9, 16))
        self.assertEqual(window.inspector.settings.edit.aspect, (9, 16))
        window.media_list.setCurrentRow(1)
        window.undo_stack.redo()
        self.assertIs(window.current, first)
        self.assertEqual(window.inspector.settings.edit.aspect, (1, 1))

    def test_a_new_edit_does_not_jump_to_another_file(self):
        self.load(self.first, self.second)
        window = self.window
        window.media_list.setCurrentRow(1)
        window.inspector.shape_group.button(2).click()
        self.assertIs(window.current, window.items[1])

    def test_apply_to_all_leaves_the_selection_alone(self):
        self.load(self.first, self.second)
        window = self.window
        window.inspector.shape_group.button(2).click()
        window.apply_to_all()
        self.assertIs(window.current, window.items[0])
        window.undo_stack.undo()
        self.assertIs(window.current, window.items[0])


class RemovingFilesTests(WindowTestCase):
    def names(self):
        return [item.path.name for item in self.window.items]

    def listed(self):
        window = self.window
        return [window.media_list.item(row).text().split("\n")[0] for row in range(window.media_list.count())]

    def test_removing_a_file_keeps_the_history_of_the_others(self):
        self.load(self.first, self.second)
        window = self.window
        first, second = window.items
        window.media_list.setCurrentRow(1)
        window.inspector.shape_group.button(2).click()            # an edit of the second file
        window.media_list.setCurrentRow(0)
        window.remove_selected()
        self.assertEqual(self.names(), ["second.mp4"])
        self.assertTrue(window.undo_stack.canUndo())
        self.assertEqual(window.undo_stack.count(), 2)
        window.undo_stack.undo()                                    # takes the removal back
        self.assertEqual(self.names(), ["first.mp4", "second.mp4"])
        window.undo_stack.undo()                                    # and the edit of the second file is still there
        self.assertEqual(second.settings.edit.aspect, (9, 16))

    def test_undo_puts_the_file_back_where_it_was_with_its_settings(self):
        self.load(self.first, self.second)
        window = self.window
        first, second = window.items
        window.inspector.shape_group.button(2).click()
        thumbs = len(first.thumbs)
        self.assertGreater(thumbs, 0)
        window.remove_selected()
        self.assertEqual(self.names(), ["second.mp4"])
        self.assertEqual(self.listed(), ["second.mp4"])
        self.assertIs(window.current, second)
        window.undo_stack.undo()
        self.assertEqual(self.names(), ["first.mp4", "second.mp4"])
        self.assertEqual(self.listed(), ["first.mp4", "second.mp4"])
        self.assertIs(window.items[0], first)
        self.assertEqual(first.settings.edit.aspect, (1, 1))
        self.assertEqual(len(first.thumbs), thumbs)
        self.assertIs(window.current, first)
        self.assertEqual(window.inspector.settings.edit.aspect, (1, 1))
        window.undo_stack.redo()
        self.assertEqual(self.names(), ["second.mp4"])
        self.assertIs(window.current, second)

    def test_removing_the_last_file_and_bringing_it_back(self):
        self.load(self.first)
        window = self.window
        window.remove_selected()
        self.assertEqual(window.items, [])
        self.assertIsNone(window.current)
        self.assertFalse(window.export_button.isEnabled())
        window.undo_stack.undo()
        self.assertEqual(self.names(), ["first.mp4"])
        self.assertIs(window.current, window.items[0])
        self.assertTrue(window.export_button.isEnabled())

    def test_several_selected_files_are_removed_and_restored_in_order(self):
        third = self.tmp / "third.mp4"
        make_video(third, seconds=1)
        self.load(self.first, self.second, third)
        window = self.window
        window.media_list.item(0).setSelected(True)
        window.media_list.item(2).setSelected(True)
        window.remove_selected()
        self.assertEqual(self.names(), ["second.mp4"])
        window.undo_stack.undo()
        self.assertEqual(self.names(), ["first.mp4", "second.mp4", "third.mp4"])
        self.assertEqual(self.listed(), ["first.mp4", "second.mp4", "third.mp4"])


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class AudioOnlyTests(WindowTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.song = cls.tmp / "song.mp3"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1",
                        str(cls.song)], check=True)

    def test_the_video_choice_is_off_for_a_song(self):
        self.load(self.song)
        inspector = self.window.inspector
        video, audio = inspector.output_group.button(0), inspector.output_group.button(1)
        self.assertEqual(self.window.current.settings.output, OUTPUT_AUDIO)
        self.assertFalse(video.isEnabled())
        self.assertTrue(video.toolTip())
        self.assertTrue(audio.isEnabled())
        self.load(self.first)
        self.window.media_list.setCurrentRow(1)
        self.assertTrue(video.isEnabled(), "a video must be able to choose Video")
        self.assertEqual(video.toolTip(), "")

    def test_a_preset_keeps_a_song_an_mp3(self):
        from video_converter.project import PRESETS, apply_preset, ClipSettings
        names = list(PRESETS)
        self.assertEqual(apply_preset(ClipSettings(), PRESETS[names[1]], has_picture=False).output, OUTPUT_AUDIO)
        self.assertEqual(apply_preset(ClipSettings(), PRESETS[names[1]]).output, OUTPUT_VIDEO)
        self.load(self.song)
        box = self.window.inspector.preset_box
        box.setCurrentIndex(box.findData(("builtin", names[1])))
        self.assertEqual(self.window.current.settings.output, OUTPUT_AUDIO)
        job = self.window._make_job(self.window.current)
        self.assertEqual(job.settings.output, OUTPUT_AUDIO)

    def test_undo_never_turns_a_song_into_a_video_export(self):
        from video_converter.project import ClipSettings
        from video_converter.ui.main_window import SettingsCommand
        self.load(self.song)
        window = self.window
        item = window.current
        # An edit made before the file had finished loading remembers the output the file had by default: video.
        early = ClipSettings()
        self.assertEqual(early.output, OUTPUT_VIDEO)
        later = early.copy()
        later.audio_quality = "320 kbps" if early.audio_quality != "320 kbps" else "128 kbps"
        window.undo_stack.push(SettingsCommand(window, item, early, later, "early edit", mergeable=False))
        window.undo_stack.undo()
        self.assertEqual(item.settings.output, OUTPUT_AUDIO)
        window.undo_stack.redo()
        self.assertEqual(item.settings.output, OUTPUT_AUDIO)


class BrokenFileTests(WindowTestCase):
    def setUp(self):
        super().setUp()
        self.broken = self.tmp / "broken.mp4"
        self.broken.write_bytes(b"this is not a video at all" * 20)

    def test_export_is_off_for_a_file_that_cannot_be_read_and_says_why(self):
        self.load(self.broken)
        window = self.window
        item = window.current
        self.assertTrue(item.error)
        self.assertFalse(window.export_button.isEnabled())
        self.assertIn("Can't export broken.mp4", window.export_button.toolTip())
        window.export_current()
        self.assertEqual(window.queue.jobs, [])
        self.assertIn("Can't export broken.mp4", window.statusBar().currentMessage())
        self.assertIn(item.error, window.statusBar().currentMessage())

    def test_export_all_names_the_file_it_left_out(self):
        self.load(self.broken, self.first)
        window = self.window
        window.export_all()
        self.assertEqual(len(window.queue.jobs), 1)
        message = window.statusBar().currentMessage()
        self.assertIn("Added 1 export to the queue.", message)
        self.assertIn("Left out 1 file that is still loading or can't be read:", message)
        self.assertIn("broken.mp4", message)

    def test_export_is_on_again_for_a_good_file(self):
        self.load(self.broken, self.first)
        window = self.window
        window.media_list.setCurrentRow(1)
        self.assertTrue(window.export_button.isEnabled())
        self.assertEqual(window.export_button.toolTip(), "Export the selected file (Ctrl+E)")
        window.media_list.setCurrentRow(0)
        self.assertFalse(window.export_button.isEnabled())

    def test_a_row_that_cannot_be_read_has_no_settings_summary(self):
        self.load(self.broken, self.first)
        broken, good = (item.list_item.text() for item in self.window.items)
        self.assertNotIn("MP4", broken)
        self.assertNotIn("original shape", broken)
        self.assertIn("MP4", good)
        self.assertEqual(len(broken.splitlines()), 2)

    def test_files_that_are_not_media_are_not_added(self):
        notes = self.tmp / "notes.txt"
        notes.write_text("hello")
        photo = self.tmp / "photo.jpg"
        photo.write_bytes(b"\xff\xd8\xff")
        window = self.window
        self.assertEqual(window.add_paths([notes, photo]), 0)
        self.assertEqual(window.items, [])
        message = window.statusBar().currentMessage()
        self.assertIn("notes.txt", message)
        self.assertIn("photo.jpg", message)
        self.assertEqual(window.add_paths([self.first, notes]), 1)
        self.assertEqual([i.path.name for i in window.items], ["first.mp4"])
        self.assertIn("notes.txt", window.statusBar().currentMessage())
        self.assertIn("Added 1 file", window.statusBar().currentMessage())


class QueuePanelTests(unittest.TestCase):
    def setUp(self):
        from video_converter.jobs import JobQueue
        from video_converter.ui.queue_panel import QueueBridge, QueuePanel
        self.queue = JobQueue("ffmpeg")
        self.panel = QueuePanel(self.queue, QueueBridge())
        self.addCleanup(self.panel.deleteLater)

    def job(self, state):
        from video_converter.jobs import Job
        from video_converter.project import ClipSettings
        job = Job(Path("clip.mp4"), ClipSettings())
        job.state = state
        self.queue.jobs.append(job)
        return job

    def test_an_update_that_arrives_after_clear_finished_does_not_bring_the_row_back(self):
        from video_converter.jobs import JobState
        job = self.job(JobState.DONE)
        self.panel.on_job_updated(job)
        self.assertEqual(len(self.panel.rows), 1)
        self.panel.clear_finished()                 # the worker's last update is still waiting in the event queue
        self.assertEqual(self.panel.rows, {})
        self.panel.on_job_updated(job)              # ...and now it is delivered
        self.assertEqual(self.panel.rows, {})
        pump(0.1)
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(self.panel.list_layout.count(), 1)          # only the stretch
        self.assertEqual(self.queue.jobs, [])

    def test_a_job_still_in_the_queue_gets_its_row(self):
        from video_converter.jobs import JobState
        job = self.job(JobState.QUEUED)
        self.panel.on_job_updated(job)
        self.assertIn(job.id, self.panel.rows)

    def test_the_progress_bars_line_up_whatever_buttons_a_row_shows(self):
        from PySide6.QtWidgets import QWidget, QVBoxLayout
        from video_converter.jobs import JobState
        from video_converter.ui.queue_panel import JobRow
        holder = QWidget()
        self.addCleanup(holder.deleteLater)
        layout = QVBoxLayout(holder)
        rows = []
        for state in (JobState.RUNNING, JobState.FAILED, JobState.DONE, JobState.CANCELLED):
            job = self.job(state)
            job.error = "boom"
            row = JobRow(job)
            layout.addWidget(row)
            rows.append(row)
        holder.resize(900, 400)
        holder.show()
        pump(0.3)
        spans = {(row.bar.x(), row.bar.width()) for row in rows}
        self.assertEqual(len(spans), 1, spans)
        shown = {name: getattr(rows[0], name).isVisible() for name in ("cancel_button", "details_button")}
        self.assertTrue(shown["cancel_button"])
        self.assertFalse(shown["details_button"])


def use_arabic(test):
    """Switch the whole app to Arabic (right to left) for the length of one test."""
    from video_converter import i18n
    from video_converter.ui import language

    i18n.set_language("ar")
    language.apply_to_app(app)

    def restore():
        i18n.set_language("en")
        language.apply_to_app(app)

    test.addCleanup(restore)


class QueueArabicTests(unittest.TestCase):
    def test_the_texts_of_a_row_line_up_on_the_same_edge_in_arabic(self):
        from video_converter.jobs import Job, JobState
        from video_converter.project import ClipSettings
        from video_converter.ui.queue_panel import JobRow
        use_arabic(self)
        job = Job(Path("clip.mp4"), ClipSettings())
        job.state = JobState.QUEUED
        row = JobRow(job)
        self.addCleanup(row.deleteLater)
        for label in (row.title, row.status):
            alignment = label.alignment()
            self.assertTrue(alignment & Qt.AlignmentFlag.AlignRight, label.text())
            self.assertTrue(alignment & Qt.AlignmentFlag.AlignAbsolute, label.text())

    def test_english_rows_keep_the_usual_alignment(self):
        from video_converter.jobs import Job
        from video_converter.project import ClipSettings
        from video_converter.ui.queue_panel import JobRow
        row = JobRow(Job(Path("clip.mp4"), ClipSettings()))
        self.addCleanup(row.deleteLater)
        self.assertFalse(row.title.alignment() & Qt.AlignmentFlag.AlignAbsolute)


class TimelineTextTests(unittest.TestCase):
    def test_the_empty_lane_says_loading_until_the_sound_has_been_read(self):
        from video_converter.ui.timeline import Timeline
        timeline = Timeline()
        self.addCleanup(timeline.deleteLater)
        self.assertEqual(timeline.wave_message(), "No sound")
        timeline.setWaveform([], loading=True)
        self.assertEqual(timeline.wave_message(), "Loading…")
        timeline.setWaveform([0.5, 0.7])
        timeline.setWaveform([], loading=False)
        self.assertEqual(timeline.wave_message(), "No sound")
        timeline.setWaveform([], loading=True)
        timeline.clear()
        self.assertEqual(timeline.wave_message(), "No sound")

    def test_the_thumbnails_and_the_waveform_fill_the_track_from_its_left_edge(self):
        from PySide6.QtGui import QColor, QPixmap
        from video_converter.ui import theme
        from video_converter.ui.timeline import RULER_H, THUMBS_H, WAVE_H, Timeline
        timeline = Timeline()
        self.addCleanup(timeline.deleteLater)
        timeline.resize(800, 160)
        timeline.setDuration(10.0)          # a ruler with many labels, the last one near the right edge
        red = QPixmap(80, 46)
        red.fill(QColor("red"))
        timeline.setThumbnails([red] * 12)
        timeline.setWaveform([1.0] * 600)
        image = timeline.grab().toImage()
        left, width = int(timeline.track_left), int(timeline.track_width)
        thumbs_y, wave_y = RULER_H + THUMBS_H // 2, RULER_H + THUMBS_H + 4 + WAVE_H // 2
        accent = QColor(theme.colors()["accent"])
        for x in (left + 4, left + width // 4, left + width // 2, left + width - 4):
            self.assertEqual(image.pixelColor(x, thumbs_y).name(), "#ff0000", f"no thumbnail at x={x}")
            self.assertEqual(image.pixelColor(x, wave_y).name(), accent.name(), f"no waveform at x={x}")

    def test_the_timeline_texts_are_translated(self):
        from video_converter.ui.timeline import Timeline
        use_arabic(self)
        timeline = Timeline()
        self.addCleanup(timeline.deleteLater)
        self.assertNotEqual(timeline.wave_message(), "No sound")
        timeline.setWaveform([], loading=True)
        self.assertNotEqual(timeline.wave_message(), "Loading…")
        from video_converter import i18n
        self.assertNotEqual(i18n.tr("The timeline appears when you select a video"),
                            "The timeline appears when you select a video")
        timeline.resize(600, 120)
        timeline.grab()                       # painting the empty timeline, and then a loaded one, works
        timeline.setDuration(10.0)
        timeline.grab()

    def test_the_last_ruler_label_stays_inside_the_widget(self):
        from video_converter.ui.timeline import MARGIN, ruler_label_left
        width, text = 500, 38
        self.assertEqual(ruler_label_left(MARGIN, text, width), MARGIN + 3)             # a tick at the start
        self.assertEqual(ruler_label_left(250, text, width), 253)                       # one in the middle
        for x in (width - MARGIN, width - MARGIN - 20, width - 5):
            left = ruler_label_left(x, text, width)
            self.assertLessEqual(left + text, width - 2, x)
            self.assertGreaterEqual(left, 0)
        self.assertEqual(ruler_label_left(10, 600, 100), 0)           # never off the left edge either

    def test_the_labels_of_a_real_timeline_fit(self):
        from PySide6.QtGui import QFontMetrics
        from video_converter.core import format_time
        from video_converter.ui.timeline import Timeline, ruler_label_left, tick_step
        timeline = Timeline()
        self.addCleanup(timeline.deleteLater)
        timeline.resize(640, 120)
        timeline.setDuration(60.0)
        metrics = QFontMetrics(timeline.font())
        step, t = tick_step(60.0, timeline.track_width), 0.0
        while t <= 60.0 + 1e-6:
            label = format_time(t)
            left = ruler_label_left(timeline._x(t), metrics.horizontalAdvance(label), timeline.width())
            self.assertLessEqual(left + metrics.horizontalAdvance(label), timeline.width() - 2, label)
            t += step


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class CutListArabicTests(WindowTestCase):
    def cut_info(self):
        from video_converter.video import VideoEdit
        from video_converter.project import ClipSettings
        edit = VideoEdit(keep=[(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)])
        self.window.inspector.set_settings(ClipSettings(edit=edit), 10.0)
        return self.window.inspector.cut_info.text()

    def test_english_cut_list_is_unchanged(self):
        text = self.cut_info()
        self.assertIn("Cut out: 0:01.0–0:02.0, 0:03.0–0:04.0, 0:05.0–0:10.0", text)

    def test_each_range_is_isolated_and_the_separator_is_arabic(self):
        from video_converter.i18n import FSI, PDI
        use_arabic(self)
        text = self.cut_info()
        for span in ("0:01.0–0:02.0", "0:03.0–0:04.0", "0:05.0–0:10.0"):
            self.assertIn(f"{FSI}{span}{PDI}", text)
        self.assertIn(f"{PDI}، {FSI}", text)
        self.assertNotIn(", ", text)


class SmallFixesTests(WindowTestCase):
    def test_a_theme_that_follows_the_system_stays_that_way(self):
        window = self.window
        self.assertEqual(window.theme_choice, "system")
        window._save_preferences()
        self.assertEqual(window.store.value("theme"), "system")
        self.assertIn(window.theme_mode, ("dark", "light"))
        window.toggle_theme()                       # the button is an explicit choice
        window._save_preferences()
        self.assertEqual(window.store.value("theme"), window.theme_mode)
        self.assertIn(window.store.value("theme"), ("dark", "light"))

    def test_the_saved_system_theme_survives_a_restart(self):
        from video_converter.ui.main_window import MainWindow
        self.window._save_preferences()
        self.window.store.sync()
        again = MainWindow(FFMPEG, QSettings(self.window.store.fileName(), QSettings.Format.IniFormat))
        self.addCleanup(lambda: (again.close(), again.loader.shutdown(wait=True), again.deleteLater()))
        self.assertEqual(again.theme_choice, "system")
        again._save_preferences()
        self.assertEqual(again.store.value("theme"), "system")

    def test_wheel_zoom_ticks_are_one_undo_step(self):
        self.load(self.first)
        window, view = self.window, self.window.view
        item = window.current
        self.assertIsNotNone(view.crop, "the preview has no crop box to zoom")
        count = window.undo_stack.count()
        for _ in range(5):
            wheel(view, 120)
        self.assertEqual(window.undo_stack.count(), count + 1)
        self.assertAlmostEqual(item.settings.edit.zoom, 1.5)
        self.assertEqual(window.undo_stack.text(count), "Zoom")
        self.assertTrue(pump(3, lambda: not window.merge_open), "the zoom never finished")
        wheel(view, 120)                              # after a pause it is a new step
        self.assertEqual(window.undo_stack.count(), count + 2)
        window.undo_stack.undo()
        self.assertAlmostEqual(item.settings.edit.zoom, 1.5)
        window.undo_stack.undo()
        self.assertAlmostEqual(item.settings.edit.zoom, 1.0)

    def test_a_wheel_turn_that_changes_nothing_adds_no_step(self):
        self.load(self.first)
        window = self.window
        count = window.undo_stack.count()
        wheel(window.view, -120)                       # already at the smallest zoom
        self.assertEqual(window.undo_stack.count(), count)

    def test_a_failed_restart_keeps_the_window_open(self):
        from video_converter.ui import language
        window = self.window
        with mock.patch.object(language, "restart", return_value=False) as restart, \
                mock.patch.object(QMessageBox, "warning") as warning, \
                mock.patch.object(type(window), "close") as close:
            window.restart_app()
        restart.assert_called_once()
        warning.assert_called_once()
        close.assert_not_called()

    def test_a_restart_that_starts_closes_the_window(self):
        from video_converter.ui import language
        window = self.window
        with mock.patch.object(language, "restart", return_value=True), \
                mock.patch.object(type(window), "close", return_value=True) as close:
            window.restart_app()
        close.assert_called_once()

    def test_saving_a_preset_under_an_old_name_asks_before_replacing(self):
        self.load(self.first)
        window = self.window
        with mock.patch.object(QInputDialog, "getText", return_value=("Mine", True)), \
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as asked:
            window.save_preset()                                     # a new name: no question
            asked.assert_not_called()
            self.assertEqual(window.user_presets["Mine"]["edit"]["speed"], 1.0)
            window.inspector.speed_box.setValue(2.0)
            window.save_preset()                                     # the name is taken: asked, and the answer is no
            asked.assert_called_once()
            self.assertEqual(window.user_presets["Mine"]["edit"]["speed"], 1.0)
            asked.return_value = QMessageBox.StandardButton.Yes
            window.save_preset()
            self.assertEqual(window.user_presets["Mine"]["edit"]["speed"], 2.0)

    def test_the_output_folder_menu_opens_under_its_button(self):
        window = self.window
        shown = []
        button = window.inspector.change_button
        class FakeMenu:
            def __init__(self, *args): pass
            def addAction(self, *args): return mock.MagicMock()
            def exec(self, pos=None): shown.append(pos)

        with mock.patch("video_converter.ui.main_window.QMenu", FakeMenu):
            button.click()                    # the same as pressing Space on the focused button
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0], button.mapToGlobal(QPoint(0, button.height())))
