"""Tests for the Qt editor. They run without a visible window and are skipped when
PySide6 isn't installed (e.g. `python -m unittest` outside the build environment)."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter.core import find_ffmpeg, probe_media
from video_converter.project import OUTPUT_AUDIO, ClipSettings

FFMPEG = find_ffmpeg()
TIMEOUT = 30


def setUpModule():
    if QApplication is None:
        raise unittest.SkipTest("PySide6 is not installed")
    global app
    app = QApplication.instance() or QApplication([])


def pump(seconds, until=lambda: False):
    """Process Qt events until `until()` is true or time runs out. Returns until()."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        QApplication.processEvents()
        time.sleep(0.01)
    return until()


def make_video(path, width=320, height=180, seconds=3, audio=True, rotation=None):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={width}x{height}:rate=15"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    if rotation is not None:
        tmp = path.with_name("tmp-" + path.name)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", str(rotation),
                        "-i", str(path), "-c", "copy", str(tmp)], check=True)
        tmp.replace(path)


class TimelineMathTests(unittest.TestCase):
    def test_time_and_x_are_inverse_and_clamped(self):
        from video_converter.ui.timeline import time_to_x, x_to_time
        for t in (0.0, 1.25, 7.5, 10.0):
            self.assertAlmostEqual(x_to_time(time_to_x(t, 10, 12, 500), 10, 12, 500), t)
        self.assertEqual(time_to_x(-3, 10, 12, 500), 12)
        self.assertEqual(time_to_x(99, 10, 12, 500), 512)
        self.assertEqual(x_to_time(-100, 10, 12, 500), 0.0)
        self.assertEqual(x_to_time(9999, 10, 12, 500), 10.0)
        self.assertEqual(x_to_time(50, 0, 12, 500), 0.0)

    def test_trims_at_the_edges_mean_no_trim(self):
        from video_converter.ui.timeline import normalize_trim
        self.assertEqual(normalize_trim(0.01, 9.99, 10), (None, None))
        self.assertEqual(normalize_trim(2.0, 8.0, 10), (2.0, 8.0))
        self.assertEqual(normalize_trim(None, None, 10), (None, None))

    def test_ruler_steps_leave_room_for_labels(self):
        from video_converter.ui.timeline import tick_step
        for duration in (2, 30, 600, 7200):
            step = tick_step(duration, 800)
            self.assertGreaterEqual(step / duration * 800, 70)
        self.assertGreaterEqual(tick_step(3600, 800), tick_step(60, 800))


class TimelineWidgetTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.timeline import Timeline
        self.timeline = Timeline()
        self.timeline.resize(524, 130)        # 500 px of track between the 12 px margins
        self.timeline.setDuration(10.0)
        self.timeline.show()
        self.seeks, self.trims, self.finished = [], [], []
        self.timeline.seekRequested.connect(self.seeks.append)
        self.timeline.trimChanged.connect(lambda s, e: self.trims.append((s, e)))
        self.timeline.trimEditFinished.connect(lambda: self.finished.append(True))

    def tearDown(self):
        self.timeline.close()

    def drag(self, x0, x1):
        y = 60
        QTest.mousePress(self.timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x0, y))
        QTest.mouseMove(self.timeline, QPoint(x1, y))
        QTest.mouseRelease(self.timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x1, y))

    def test_clicking_the_track_seeks(self):
        self.drag(262, 262)                   # the middle of the track
        self.assertAlmostEqual(self.seeks[-1], 5.0, delta=0.05)
        self.assertEqual(self.trims, [])

    def test_dragging_the_start_handle_trims(self):
        self.drag(12, 112)                    # from 0 s to 2 s
        start, end = self.trims[-1]
        self.assertAlmostEqual(start, 2.0, delta=0.05)
        self.assertIsNone(end)
        self.assertEqual(self.finished, [True])

    def test_dragging_the_end_handle_trims(self):
        self.drag(512, 412)                   # from 10 s to 8 s
        self.assertAlmostEqual(self.trims[-1][1], 8.0, delta=0.05)

    def test_handles_cannot_cross(self):
        self.timeline.setTrim(4.0, 6.0)
        self.drag(12 + 200, 12 + 450)         # drag the start handle past the end
        start, end = self.trims[-1]
        self.assertLess(start, end)

    def test_external_trim_updates_are_ignored_mid_drag(self):
        QTest.mousePress(self.timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(12, 60))
        self.timeline.setTrim(5.0, None)      # e.g. an undo arriving while dragging
        self.assertIsNone(self.timeline.start)
        QTest.mouseRelease(self.timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(12, 60))


class OrientationTests(unittest.TestCase):
    def image(self):
        # 2 x 1: red on the left, blue on the right
        img = QImage(2, 1, QImage.Format.Format_RGB32)
        img.setPixelColor(0, 0, QColor("red"))
        img.setPixelColor(1, 0, QColor("blue"))
        return img

    def test_rotate_clockwise_then_mirror(self):
        from video_converter.ui.player import orient_image
        turned = orient_image(self.image(), 90, False)
        self.assertEqual((turned.width(), turned.height()), (1, 2))
        self.assertEqual(turned.pixelColor(0, 0), QColor("red"))     # left edge goes to the top
        upside_down = orient_image(self.image(), 180, False)
        self.assertEqual(upside_down.pixelColor(0, 0), QColor("blue"))
        mirrored = orient_image(self.image(), 0, True)
        self.assertEqual(mirrored.pixelColor(0, 0), QColor("blue"))
        self.assertEqual(orient_image(self.image(), 360, False).pixelColor(0, 0), QColor("red"))


class VideoViewTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.player import VideoView
        self.view = VideoView()
        self.view.resize(416, 250)
        image = QImage(1280, 720, QImage.Format.Format_RGB32)
        image.fill(QColor("gray"))
        self.view.set_frame(image)
        # 9:16 crop of a 1280x720 frame, centered
        self.view.set_crop((1280, 720, 404, 720, 438, 0), 1.0)
        self.view.show()
        self.moves, self.zooms = [], []
        self.view.cropMoved.connect(lambda x, y: self.moves.append((x, y)))
        self.view.zoomChanged.connect(self.zooms.append)

    def tearDown(self):
        self.view.close()

    def test_dragging_the_box_moves_the_crop(self):
        center = self.view.crop_rect().center().toPoint()
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        QTest.mouseMove(self.view, center + QPoint(60, 0))
        QTest.mouseRelease(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           center + QPoint(60, 0))
        self.assertTrue(self.moves)
        self.assertGreater(self.moves[-1][0], 0.5)

    def test_dragging_far_left_clamps_to_the_edge(self):
        center = self.view.crop_rect().center().toPoint()
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        QTest.mouseMove(self.view, center - QPoint(1000, 0))
        self.assertEqual(self.moves[-1][0], 0.0)

    def test_clicking_outside_the_box_moves_it_there(self):
        frame = self.view.frame_rect()
        QTest.mouseClick(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(int(frame.right()) - 3, int(frame.center().y())))
        self.assertEqual(self.moves[-1][0], 1.0)

    def test_dragging_a_corner_inward_zooms_in(self):
        corner = self.view.crop_rect().topRight().toPoint()
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, corner)
        QTest.mouseMove(self.view, corner - QPoint(25, 0))
        self.assertTrue(self.zooms)
        self.assertGreater(self.zooms[-1], 1.0)

    def test_no_crop_interaction_in_after_mode(self):
        self.view.set_after(None, True)
        center = self.view.crop_rect().center().toPoint()
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        QTest.mouseMove(self.view, center + QPoint(60, 0))
        self.assertEqual(self.moves, [])


class InspectorTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.inspector import Inspector
        self.inspector = Inspector()
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.edits = []
        self.inspector.settingsEdited.connect(lambda s, label, final: self.edits.append((s, label, final)))

    def tearDown(self):
        self.inspector.close()

    def test_showing_settings_does_not_report_edits(self):
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.assertEqual(self.edits, [])

    def test_choosing_a_shape(self):
        self.inspector.shape_group.button(2).click()          # 1:1
        settings = self.edits[-1][0]
        self.assertEqual(settings.edit.aspect, (1, 1))

    def test_switching_to_mp3_hides_video_only_sections(self):
        self.inspector.output_group.button(1).click()
        settings = self.edits[-1][0]
        self.assertEqual(settings.output, OUTPUT_AUDIO)
        self.inspector.set_settings(settings, 10.0)
        self.assertTrue(self.inspector.frame_section.isHidden())
        self.assertTrue(self.inspector.quality_section.isHidden())
        self.assertFalse(self.inspector.mp3_box.isHidden())

    def type_trim(self, widget, text):
        widget.setText(text)
        widget.editingFinished.emit()

    def test_typed_trim_times(self):
        self.type_trim(self.inspector.start_edit, "0:02.5")
        self.assertEqual(self.edits[-1][0].edit.start, 2.5)

    def test_bad_trim_times_are_rejected_with_a_message(self):
        for text in ("abc", "0:20"):                          # not a time; past the 10 s end
            self.type_trim(self.inspector.start_edit, text)
            self.assertEqual(self.edits, [])
            self.assertFalse(self.inspector.trim_error.isHidden())
        settings = ClipSettings()
        settings.edit.start = 5.0
        self.inspector.set_settings(settings, 10.0)
        self.type_trim(self.inspector.end_edit, "4")          # end before start
        self.assertEqual(self.edits, [])

    def test_builtin_preset(self):
        index = self.inspector.preset_box.findText("Square post (1:1)")
        self.inspector.preset_box.setCurrentIndex(index)
        settings, label, _ = self.edits[-1]
        self.assertEqual(settings.edit.aspect, (1, 1))
        self.assertIn("Square", label)
        self.assertEqual(self.inspector.preset_box.currentIndex(), 0)   # ready for the next pick

    def test_zoom_slider_drag_reports_one_final_edit(self):
        slider = self.inspector.zoom_slider
        slider.setSliderDown(True)
        slider.setValue(150)
        slider.setValue(180)
        slider.setSliderDown(False)
        slider.sliderReleased.emit()
        finals = [final for _, _, final in self.edits]
        self.assertEqual(finals[-1], True)
        self.assertNotIn(True, finals[:-1])
        self.assertAlmostEqual(self.edits[-1][0].edit.zoom, 1.8)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.landscape = cls.tmp / "landscape.mp4"
        cls.portrait = cls.tmp / "portrait.mp4"
        make_video(cls.landscape)
        make_video(cls.portrait, rotation=-90)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        from video_converter.ui.main_window import MainWindow
        self.ini = self.tmp / f"settings-{self.id().split('.')[-1]}.ini"
        self.window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        self.window.resize(1200, 800)
        self.window.show()

    def tearDown(self):
        self.window.close()
        pump(0.1)

    def load(self, *paths):
        self.window.add_paths(list(paths))
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)), "files never loaded")

    def test_adding_files_and_folders_skips_duplicates(self):
        self.load(self.landscape)
        self.assertEqual(self.window.add_paths([self.landscape]), 0)
        self.window.add_paths([self.tmp])                     # the folder holds both videos
        self.assertEqual(len(self.window.items), 2)
        item = self.window.items[0]
        self.assertEqual((item.info.width, item.info.height), (320, 180))
        self.assertTrue(item.thumbs)
        self.assertTrue(item.waveform)

    def test_rotated_phone_video_is_previewed_upright(self):
        self.load(self.portrait)
        self.assertTrue(pump(TIMEOUT, lambda: self.window.view.raw is not None), "no frame decoded")
        image = self.window.view.oriented_image()
        self.assertLess(image.width(), image.height())

    def test_edits_can_be_undone_and_redone(self):
        self.load(self.landscape)
        item = self.window.current
        self.window.inspector.shape_group.button(2).click()   # 1:1
        self.assertEqual(item.settings.edit.aspect, (1, 1))
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.aspect, (9, 16))
        self.assertTrue(self.window.inspector.shape_group.button(1).isChecked())  # the panel follows
        self.window.undo_stack.redo()
        self.assertEqual(item.settings.edit.aspect, (1, 1))

    def test_a_crop_drag_is_one_undo_step(self):
        self.load(self.landscape)
        item = self.window.current
        for x in (0.4, 0.3, 0.2, 0.1):
            self.window._crop_moved(x, 0.5)
        self.window._edit_finished()
        self.window._crop_moved(0.9, 0.5)                      # a second, separate drag
        self.window._edit_finished()
        self.assertEqual(self.window.undo_stack.count(), 2)
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.pos_x, 0.1)
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.pos_x, 0.5)

    def test_apply_to_all_keeps_each_files_framing(self):
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        second.settings.edit.pos_x, second.settings.edit.start = 0.9, 1.0
        self.window.media_list.setCurrentRow(0)
        self.window.inspector.shape_group.button(2).click()   # 1:1 on the first file
        self.window.apply_to_all()
        self.assertEqual(second.settings.edit.aspect, (1, 1))
        self.assertEqual((second.settings.edit.pos_x, second.settings.edit.start), (0.9, 1.0))
        self.window.undo_stack.undo()                          # one step undoes the whole batch
        self.assertEqual(second.settings.edit.aspect, (9, 16))

    def test_trim_at_playhead_refuses_an_end_before_the_start(self):
        self.load(self.landscape)
        item = self.window.current
        item.settings.edit.start = 2.0
        self.window.player.seek(1.0)
        pump(2, lambda: abs(self.window.player.position - 1.0) < 0.05)
        self.window.trim_at_playhead("end")
        self.assertIsNone(item.settings.edit.end)

    def test_export_video_and_mp3(self):
        self.load(self.landscape)
        out_dir = self.tmp / "exports"
        out_dir.mkdir(exist_ok=True)
        self.window._set_output_folder(out_dir)
        self.window.export_current()
        audio = self.window.current.settings.copy()
        audio.output = OUTPUT_AUDIO
        self.window.push_settings(audio, "Output")
        self.window.export_current()
        self.assertTrue(pump(60, lambda: not self.window.queue.busy), "exports never finished")
        outputs = sorted(p.name for p in out_dir.iterdir())
        self.assertEqual(outputs, ["landscape (phone).mp4", "landscape.mp3"])
        info = probe_media(FFMPEG, out_dir / "landscape (phone).mp4")
        self.assertLess(info.width, info.height)

    def test_preferences_survive_a_restart(self):
        from video_converter.ui.main_window import MainWindow
        self.load(self.landscape)
        self.window._set_output_folder(self.tmp)
        self.window.user_presets["Mine"] = ClipSettings().to_dict()
        self.window.close()
        pump(0.1)
        self.window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        self.assertEqual(self.window.output_dir, self.tmp)
        self.assertIn("Mine", self.window.user_presets)
        self.assertGreaterEqual(self.window.inspector.preset_box.findText("★ Mine"), 0)

    def test_corrupt_saved_presets_do_not_break_startup(self):
        from video_converter.ui.main_window import MainWindow
        store = QSettings(str(self.ini), QSettings.Format.IniFormat)
        store.setValue("presets", "{not json")
        store.sync()
        self.window.close()
        self.window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        self.assertEqual(self.window.user_presets, {})

    def test_keyboard_shortcuts_skip_text_fields(self):
        self.load(self.landscape)
        self.window.activateWindow()
        item = self.window.current
        field = self.window.inspector.start_edit
        field.setFocus()
        QTest.keyClick(field, Qt.Key.Key_I)                    # typing an "i" must not set the trim
        self.assertIsNone(item.settings.edit.start)
        self.assertEqual(field.text(), "i")

        # ...but the same key anywhere else marks the trim start at the playhead.
        self.window.player.seek(1.0)
        pump(2, lambda: abs(self.window.player.position - 1.0) < 0.05)
        self.window.media_list.setFocus()
        QTest.keyClick(self.window.media_list, Qt.Key.Key_I)
        self.assertAlmostEqual(item.settings.edit.start or 0.0, 1.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
