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
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    import shiboken6  # noqa: F401
    from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox
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
