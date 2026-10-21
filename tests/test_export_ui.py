"""The 2.9 pieces of the window: the format and file size options, Save frame, and the update notice.
Run without a visible window; skipped when PySide6 isn't installed."""

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("VIDEO_EDITOR_NO_GPU_SEARCH", "1")      # a window never searches for graphics cards (see tests/__init__.py)
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    import shiboken6
    from PySide6.QtCore import QEvent, QSettings, QUrl
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter import APP_NAME, REPO_URL, updates
from video_converter.core import ConversionError, MediaInfo, find_ffmpeg
from video_converter.output import OUTPUT_FORMATS
from video_converter.project import OUTPUT_AUDIO, ClipSettings
from video_converter.updates import Release
from video_converter.video import VideoEdit, plan_geometry

FFMPEG = find_ffmpeg()
TIMEOUT = 30


def setUpModule():
    if QApplication is None:
        raise unittest.SkipTest("PySide6 is not installed")
    global app, _network
    app = QApplication.instance() or QApplication([])
    # Nothing in here may reach the network: a stray request fails the test that made it.
    _network = mock.patch("urllib.request.urlopen", side_effect=AssertionError("the network was used"))
    _network.start()


def tearDownModule():
    _network.stop()


def pump(seconds, until=lambda: False):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        QApplication.processEvents()
        time.sleep(0.01)
    return until()


def make_video(path, width=320, height=180, seconds=3):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={width}x{height}:rate=15",
                    "-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)


def visible(widget, parent):
    return widget.isVisibleTo(parent)


class FormatOptionsTests(unittest.TestCase):
    """The Video quality section on its own."""

    def setUp(self):
        from video_converter.ui.inspector import Inspector
        self.inspector = Inspector()
        self.edits = []
        self.inspector.settingsEdited.connect(lambda s, label, final: self.edits.append((s, label, final)))
        self.show()

    def tearDown(self):
        self.inspector.close()
        shiboken6.delete(self.inspector)     # free it now: every widget left alive slows each later theme change

    def show(self, **edit):
        self.inspector.set_settings(ClipSettings(edit=VideoEdit(**edit)), 10.0)

    def shown(self, name):
        return all(visible(widget, self.inspector) for widget in self.inspector.quality_rows[name])

    def choose(self, box, data):
        box.setCurrentIndex(box.findData(data))

    def test_showing_settings_reports_no_edits(self):
        for edit in ({}, {"video_format": "gif", "gif_fps": 10, "gif_width": 320},
                     {"video_format": "webm", "target_mb": 16.0}, {"target_mb": 12.5}):
            self.show(**edit)
        self.assertEqual(self.edits, [])

    def test_every_format_is_offered_and_the_current_one_is_shown(self):
        box = self.inspector.format_box
        self.assertEqual([box.itemData(i) for i in range(box.count())], list(OUTPUT_FORMATS))
        self.assertEqual([box.itemText(i) for i in range(box.count())],
                         [f.label for f in OUTPUT_FORMATS.values()])
        self.assertEqual(box.currentData(), "mp4")
        self.show(video_format="webm")
        self.assertEqual(box.currentData(), "webm")

    def test_choosing_a_format_edits_the_settings(self):
        self.choose(self.inspector.format_box, "hevc")
        settings, label, final = self.edits[-1]
        self.assertEqual((settings.edit.video_format, label, final), ("hevc", "Format", True))

    def test_h265_warns_that_older_phones_cannot_play_it(self):
        self.show(video_format="hevc")
        self.assertTrue(visible(self.inspector.format_hint, self.inspector))
        self.assertIn("older phones", self.inspector.format_hint.text())
        self.show()
        self.assertFalse(visible(self.inspector.format_hint, self.inspector))

    def test_webm_has_no_graphics_card_option_but_the_others_do(self):
        for key, expected in (("mp4", True), ("hevc", True), ("mov", True), ("webm", False), ("gif", False)):
            with self.subTest(format=key):
                self.show(video_format=key)
                self.assertEqual(self.shown("gpu"), expected)

    def test_a_gif_has_its_own_options_and_none_of_the_video_ones(self):
        self.show(video_format="gif", target_mb=10.0)
        self.assertTrue(self.shown("gif_fps") and self.shown("gif_width"))
        for name in ("rate", "quality", "limit", "custom", "gpu"):
            self.assertFalse(self.shown(name), name)
        self.assertTrue(self.shown("size") and self.shown("format"))
        self.show()
        self.assertFalse(self.shown("gif_fps") or self.shown("gif_width"))
        self.assertTrue(self.shown("rate") and self.shown("quality"))

    def test_the_gif_options_are_limits_you_can_change(self):
        self.show(video_format="gif")
        self.assertEqual((self.inspector.gif_fps_box.currentData(), self.inspector.gif_width_box.currentData()),
                         (15, 480))
        self.choose(self.inspector.gif_fps_box, 10)
        self.assertEqual((self.edits[-1][0].edit.gif_fps, self.edits[-1][1]), (10, "GIF frame rate"))
        self.choose(self.inspector.gif_width_box, 320)
        self.assertEqual((self.edits[-1][0].edit.gif_width, self.edits[-1][1]), (320, "GIF width"))

    def test_a_gif_value_from_a_saved_preset_that_the_list_lacks_is_still_shown(self):
        self.show(video_format="gif", gif_fps=12, gif_width=555)
        self.assertEqual((self.inspector.gif_fps_box.currentData(), self.inspector.gif_width_box.currentData()),
                         (12, 555))
        self.assertEqual(self.edits, [])

    def test_the_quality_box_ends_with_fit_in_a_file_size(self):
        box = self.inspector.crf_box
        self.assertEqual([box.itemText(i) for i in range(box.count())],
                         ["High", "Balanced", "Smaller file", "Fit in a file size"])
        self.assertFalse(self.shown("limit"))

    def test_choosing_fit_in_a_file_size_starts_at_discords_limit(self):
        self.choose(self.inspector.crf_box, "fit")
        settings, label, _ = self.edits[-1]
        self.assertEqual((settings.edit.target_mb, label), (10.0, "File size"))
        self.inspector.set_settings(settings, 10.0)
        self.assertEqual(self.inspector.crf_box.currentData(), "fit")
        self.assertTrue(self.shown("limit"))
        self.assertEqual(self.inspector.limit_box.currentText(), "Discord (10 MB)")
        self.assertFalse(self.shown("custom"))

    def test_the_other_qualities_turn_the_file_size_off(self):
        self.show(target_mb=16.0, crf=28)
        self.choose(self.inspector.crf_box, 18)
        settings, label, _ = self.edits[-1]
        self.assertEqual((settings.edit.crf, settings.edit.target_mb, label), (18, 0.0, "Quality"))
        self.inspector.set_settings(settings, 10.0)
        self.assertFalse(self.shown("limit"))

    def test_the_limits_are_the_places_people_send_videos_to(self):
        box = self.inspector.limit_box
        self.assertEqual([box.itemText(i) for i in range(box.count())],
                         ["Discord (10 MB)", "WhatsApp (16 MB)", "Email (25 MB)", "Custom…"])
        self.show(target_mb=10.0)
        for text, megabytes in (("WhatsApp (16 MB)", 16.0), ("Email (25 MB)", 25.0), ("Discord (10 MB)", 10.0)):
            self.show(target_mb=10.0 if megabytes != 10.0 else 16.0)
            box.setCurrentIndex(box.findText(text))
            self.assertEqual(self.edits[-1][0].edit.target_mb, megabytes)

    def test_a_saved_size_selects_its_limit(self):
        for megabytes, text in ((10.0, "Discord (10 MB)"), (16.0, "WhatsApp (16 MB)"), (25.0, "Email (25 MB)")):
            self.show(target_mb=megabytes)
            self.assertEqual(self.inspector.limit_box.currentText(), text)
            self.assertFalse(self.shown("custom"))

    def test_any_other_size_is_custom_and_can_be_typed(self):
        self.show(target_mb=12.5)
        self.assertEqual(self.inspector.limit_box.currentText(), "Custom…")
        self.assertTrue(self.shown("custom"))
        self.assertEqual(self.inspector.limit_spin.value(), 12.5)
        self.inspector.limit_spin.setValue(33.3)
        settings, label, _ = self.edits[-1]
        self.assertEqual((settings.edit.target_mb, label), (33.3, "File size"))

    def test_choosing_custom_starts_from_the_number_in_the_box(self):
        self.show(target_mb=10.0)
        box = self.inspector.limit_box
        box.setCurrentIndex(box.count() - 1)
        self.assertEqual(self.edits[-1][0].edit.target_mb, 20.0)

    def test_the_sizes_the_box_takes_are_the_ones_the_settings_take(self):
        from video_converter.output import TARGET_RANGE
        self.assertEqual((self.inspector.limit_spin.minimum(), self.inspector.limit_spin.maximum()), TARGET_RANGE)

    def test_the_note_about_the_picture_shows_only_with_a_file_size(self):
        self.show(target_mb=10.0)
        self.inspector.set_size_note("The picture gets about 1.3 Mbps.")
        self.assertTrue(visible(self.inspector.size_note, self.inspector))
        self.assertEqual(self.inspector.size_note.text(), "The picture gets about 1.3 Mbps.")
        self.assertFalse(visible(self.inspector.size_error, self.inspector))
        self.show()
        self.assertFalse(visible(self.inspector.size_note, self.inspector))
        self.show(target_mb=10.0)
        self.assertTrue(visible(self.inspector.size_note, self.inspector))
        self.inspector.set_size_note("")
        self.assertFalse(visible(self.inspector.size_note, self.inspector))

    def test_a_size_that_cannot_be_fitted_is_shown_as_an_error(self):
        self.show(target_mb=10.0)
        self.inspector.set_size_note("This video is too long to fit in 10 MB.", error=True)
        self.assertTrue(visible(self.inspector.size_error, self.inspector))
        self.assertEqual(self.inspector.size_error.objectName(), "error")
        self.assertFalse(visible(self.inspector.size_note, self.inspector))
        self.assertIn("too long", self.inspector.size_error.text())

    def test_the_format_warning(self):
        self.inspector.set_format_warning("This GIF runs 1:00.0, and GIFs get big fast.")
        self.assertTrue(visible(self.inspector.format_warning, self.inspector))
        self.inspector.set_format_warning("")
        self.assertFalse(visible(self.inspector.format_warning, self.inspector))

    def test_the_output_buttons_no_longer_say_mp4(self):
        texts = [button.text() for button in self.inspector.output_group.buttons()]
        self.assertEqual(texts, ["Video", "Audio (MP3)"])

    def test_an_mp3_hides_all_of_it(self):
        self.inspector.set_settings(ClipSettings(output=OUTPUT_AUDIO), 10.0)
        self.assertFalse(visible(self.inspector.quality_section, self.inspector))


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "clip.mp4"
        make_video(cls.clip)
        cls.second = cls.tmp / "second.mp4"
        make_video(cls.second, seconds=2)
        cls.song = cls.tmp / "song.mp3"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=duration=2", str(cls.song)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.ini = self.tmp / f"settings-{self.id().split('.')[-1]}.ini"
        self.window = self.new_window()

    def new_window(self):
        from video_converter.ui.main_window import MainWindow
        window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        window.resize(1200, 800)
        window.show()
        return window

    def tearDown(self):
        self.close(self.window)
        self.window = None

    def close(self, window):
        window.close()
        window.loader.shutdown(wait=True)
        window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(0.1)

    def load(self, *paths):
        self.window.add_paths(list(paths))
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)), "files never loaded")

    def edit(self):
        return self.window.current.settings.edit

    # ----- format and file size

    def test_choosing_a_format_changes_the_file_and_can_be_undone(self):
        self.load(self.clip)
        box = self.window.inspector.format_box
        box.setCurrentIndex(box.findData("hevc"))
        self.assertEqual(self.edit().video_format, "hevc")
        self.assertIn("H.265", self.window.current.list_item.text())
        self.assertEqual(self.window.undo_stack.undoText(), "Format")
        self.window.undo_stack.undo()
        self.assertEqual(self.edit().video_format, "mp4")
        self.assertEqual(box.currentData(), "mp4")
        self.window.undo_stack.redo()
        self.assertEqual((self.edit().video_format, box.currentData()), ("hevc", "hevc"))

    def test_a_file_size_can_be_chosen_changed_and_undone(self):
        self.load(self.clip)
        inspector = self.window.inspector
        inspector.crf_box.setCurrentIndex(inspector.crf_box.findData("fit"))
        self.assertEqual(self.edit().target_mb, 10.0)
        self.assertIn("fits 10 MB", self.window.current.list_item.text())
        inspector.limit_box.setCurrentIndex(inspector.limit_box.findText("Email (25 MB)"))
        self.assertEqual(self.edit().target_mb, 25.0)
        self.assertEqual(self.window.undo_stack.undoText(), "File size")
        self.window.undo_stack.undo()
        self.assertEqual((self.edit().target_mb, inspector.limit_box.currentText()), (10.0, "Discord (10 MB)"))
        self.window.undo_stack.undo()
        self.assertEqual((self.edit().target_mb, inspector.crf_box.currentData()), (0.0, 23))

    def test_every_new_setting_is_kept_per_file(self):
        self.load(self.clip, self.second)
        inspector = self.window.inspector
        inspector.format_box.setCurrentIndex(inspector.format_box.findData("webm"))
        self.window.media_list.setCurrentRow(1)
        self.assertEqual(self.edit().video_format, "mp4")
        self.assertEqual(inspector.format_box.currentData(), "mp4")
        self.window.media_list.setCurrentRow(0)
        self.assertEqual(inspector.format_box.currentData(), "webm")

    def test_the_panel_says_what_the_picture_will_get(self):
        self.load(self.clip)
        inspector = self.window.inspector
        inspector.crf_box.setCurrentIndex(inspector.crf_box.findData("fit"))
        self.assertTrue(visible(inspector.size_note, inspector))
        self.assertRegex(inspector.size_note.text(), r"The picture gets about [\d.]+ [Mk]bps")
        self.assertFalse(visible(inspector.size_error, inspector))

    def test_a_size_that_cannot_be_fitted_is_said_in_the_panel_before_exporting(self):
        self.load(self.clip)
        item = self.window.current
        item.info = MediaInfo(duration=1800.0, width=1920, height=1080, has_audio=True, fps=30.0)
        inspector = self.window.inspector
        inspector.crf_box.setCurrentIndex(inspector.crf_box.findData("fit"))
        self.assertTrue(visible(inspector.size_error, inspector))
        self.assertIn("too long to fit in 10 MB", inspector.size_error.text())
        self.assertFalse(visible(inspector.size_note, inspector))

    def test_a_long_gif_gets_a_warning_in_the_panel(self):
        self.load(self.clip)
        self.window.current.info = MediaInfo(duration=60.0, width=640, height=360, has_audio=True, fps=15.0)
        inspector = self.window.inspector
        inspector.format_box.setCurrentIndex(inspector.format_box.findData("gif"))
        self.assertTrue(visible(inspector.format_warning, inspector))
        self.assertIn("GIF", inspector.format_warning.text())
        inspector.format_box.setCurrentIndex(inspector.format_box.findData("webm"))
        self.assertFalse(visible(inspector.format_warning, inspector))

    def test_a_short_gif_gets_none(self):
        self.load(self.clip)
        inspector = self.window.inspector
        inspector.format_box.setCurrentIndex(inspector.format_box.findData("gif"))
        self.assertFalse(visible(inspector.format_warning, inspector))

    def test_exporting_makes_the_chosen_format(self):
        self.load(self.clip)
        self.window.output_dir = self.tmp / "exports"
        inspector = self.window.inspector
        inspector.format_box.setCurrentIndex(inspector.format_box.findData("webm"))
        self.window.export_current()
        self.assertTrue(self.window.queue.wait(TIMEOUT))
        job = self.window.queue.jobs[-1]
        self.assertEqual(job.state.value, "Done", job.error)
        self.assertEqual(job.output.name, "clip (phone).webm")
        self.assertIn("webm", job.summary.lower())

    def test_exporting_a_gif_and_a_file_size(self):
        self.load(self.clip)
        self.window.output_dir = self.tmp / "exports-gif"
        inspector = self.window.inspector
        inspector.format_box.setCurrentIndex(inspector.format_box.findData("gif"))
        self.window.export_current()
        inspector.format_box.setCurrentIndex(inspector.format_box.findData("mp4"))
        inspector.crf_box.setCurrentIndex(inspector.crf_box.findData("fit"))
        inspector.limit_spin.setValue(0.5)
        inspector.limit_box.setCurrentIndex(inspector.limit_box.count() - 1)
        self.window.export_current()
        self.assertTrue(self.window.queue.wait(TIMEOUT))
        gif, sized = self.window.queue.jobs[-2:]
        self.assertEqual((gif.state.value, gif.output.suffix), ("Done", ".gif"))
        self.assertEqual(sized.state.value, "Done", sized.error)
        self.assertLessEqual(sized.output.stat().st_size, 500_000)

    def test_a_failed_size_says_why_in_the_queue(self):
        self.load(self.clip)
        self.window.output_dir = self.tmp / "exports-fail"
        self.window.current.info = MediaInfo(duration=7200.0, width=1920, height=1080, has_audio=True, fps=30.0)
        inspector = self.window.inspector
        inspector.crf_box.setCurrentIndex(inspector.crf_box.findData("fit"))
        self.window.export_current()
        self.assertTrue(self.window.queue.wait(TIMEOUT))
        job = self.window.queue.jobs[-1]
        self.assertEqual(job.state.value, "Failed")
        self.assertTrue(job.error)

    def test_joining_with_a_gif_first_is_refused_with_a_message(self):
        self.load(self.clip, self.second)
        inspector = self.window.inspector
        inspector.format_box.setCurrentIndex(inspector.format_box.findData("gif"))
        self.window.join_clips()
        self.assertIn("GIF", self.window.statusBar().currentMessage())
        self.assertEqual(self.window.queue.jobs, [])

    def test_the_summary_in_the_queue_names_the_format(self):
        from video_converter.jobs import Job
        job = Job(self.clip, ClipSettings(edit=VideoEdit(video_format="hevc", target_mb=16.0)))
        self.assertTrue(job.summary.startswith("H.265"))
        self.assertIn("fits 16 MB", job.summary)

    def test_the_queue_shows_the_note_when_the_picture_was_made_smaller(self):
        from video_converter.jobs import Job, JobState
        from video_converter.ui.queue_panel import status_text
        job = Job(self.clip, ClipSettings())
        job.state, job.output, job.note = JobState.DONE, Path("clip (phone).mp4"), "Made smaller (720p) so it fits in 10 MB"
        self.assertEqual(status_text(job), "Saved as clip (phone).mp4 · Made smaller (720p) so it fits in 10 MB")
        job.state, job.started_at = JobState.RUNNING, time.monotonic()
        self.assertNotIn("graphics card", status_text(job))               # that note is not about the card

    # ----- save frame

    def dialog(self, path):
        return mock.patch("video_converter.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(path), ""))

    def test_the_action_has_its_shortcut_and_name(self):
        self.assertEqual(self.window.frame_action.shortcut().toString(), "Ctrl+Shift+S")
        self.assertIn("Save frame", self.window.frame_action.text())
        self.assertIn("Ctrl+Shift+S", self.window.frame_action.toolTip())

    def test_it_needs_a_picture(self):
        self.assertFalse(self.window.frame_action.isEnabled())
        self.window.save_frame_image()
        self.assertIn("Open a video", self.window.statusBar().currentMessage())
        self.load(self.clip)
        self.assertTrue(self.window.frame_action.isEnabled())
        self.load(self.song)
        self.window.media_list.setCurrentRow(1)
        self.assertFalse(self.window.frame_action.isEnabled())
        self.window.media_list.setCurrentRow(0)
        self.assertTrue(self.window.frame_action.isEnabled())

    def test_it_saves_the_frame_at_the_playhead_with_the_edits_at_the_export_size(self):
        self.load(self.clip)
        item = self.window.current
        new = item.settings.copy()
        new.edit.look = "bw"
        new.edit.resolution = 720
        self.window.push_settings(new, "Look")
        target = self.tmp / "shot.png"
        target.unlink(missing_ok=True)
        with self.dialog(target):
            self.window.frame_action.trigger()
        self.assertTrue(pump(TIMEOUT, target.is_file))
        self.assertTrue(pump(TIMEOUT, lambda: "Saved" in self.window.statusBar().currentMessage()))
        geometry = plan_geometry(320, 180, item.settings.edit)
        image = QImage(str(target))
        self.assertEqual((image.width(), image.height()), (geometry.out_width, geometry.out_height))
        self.assertEqual((geometry.out_width, geometry.out_height), (720, 1280))        # not the preview's size
        self.assertIn("Saved the frame as shot.png", self.window.statusBar().currentMessage())
        color = image.pixelColor(image.width() // 2, image.height() // 2)
        self.assertLess(abs(color.red() - color.green()) + abs(color.green() - color.blue()), 12)     # black and white

    def test_it_hands_the_right_things_to_the_saver(self):
        self.load(self.clip)
        self.window.player.seek(1.5)
        position = self.window.player.position
        saved = []

        def fake(ffmpeg, source, target, at, edit, info):
            saved.append((source, target, at, edit, info))
            return target
        with self.dialog(self.tmp / "args.jpg"), mock.patch("video_converter.ui.main_window.save_frame", fake):
            self.window.save_frame_image()
            self.assertTrue(pump(TIMEOUT, lambda: bool(saved)))
        source, target, at, edit, info = saved[0]
        self.assertEqual((source, target, at), (self.clip, self.tmp / "args.jpg", position))
        self.assertEqual(edit, self.edit())
        self.assertIs(info, self.window.current.info)

    def test_the_suggested_name_and_folder(self):
        self.load(self.clip)
        with self.dialog("") as dialog:
            self.window.save_frame_image()
        suggested = Path(dialog.call_args.args[2])
        self.assertEqual(suggested.parent, self.clip.parent)
        self.assertTrue(suggested.name.startswith("clip (frame ") and suggested.name.endswith(".png"))
        self.window.output_dir = self.tmp / "elsewhere"
        with self.dialog("") as dialog:
            self.window.save_frame_image()
        self.assertEqual(Path(dialog.call_args.args[2]).parent, self.tmp / "elsewhere")
        self.assertIn("PNG", dialog.call_args.args[3])
        self.assertIn("JPEG", dialog.call_args.args[3])

    def test_cancelling_the_dialog_saves_nothing(self):
        self.load(self.clip)
        with self.dialog(""), mock.patch("video_converter.ui.main_window.save_frame") as saver:
            self.window.save_frame_image()
            pump(0.2)
        saver.assert_not_called()
        self.assertEqual(self.window.store.value("lastFrameDir", ""), "")

    def test_a_name_without_a_picture_extension_gets_png(self):
        self.load(self.clip)
        saved = []
        with self.dialog(self.tmp / "oddly.bmp"), mock.patch(
                "video_converter.ui.main_window.save_frame", lambda *a: saved.append(a[2]) or a[2]):
            self.window.save_frame_image()
            self.assertTrue(pump(TIMEOUT, lambda: bool(saved)))
        self.assertEqual(saved[0], self.tmp / "oddly.bmp.png")

    def test_the_folder_is_remembered(self):
        self.load(self.clip)
        with self.dialog(self.tmp / "remembered" / "f.png"), mock.patch(
                "video_converter.ui.main_window.save_frame", lambda *a: a[2]):
            self.window.save_frame_image()
        self.assertEqual(self.window.store.value("lastFrameDir"), str(self.tmp / "remembered"))

    def test_a_failure_is_said_in_the_status_bar(self):
        self.load(self.clip)
        with self.dialog(self.tmp / "fail.png"), mock.patch(
                "video_converter.ui.main_window.save_frame", side_effect=ConversionError("Couldn't read a frame")):
            self.window.save_frame_image()
            self.assertTrue(pump(TIMEOUT, lambda: "Couldn't save" in self.window.statusBar().currentMessage()))
        self.assertEqual(self.window.statusBar().currentMessage(), "Couldn't save the frame: Couldn't read a frame")

    # ----- the update notice

    RELEASE = Release("99.0.0", f"{REPO_URL}/releases/tag/v99.0.0")

    def check(self, window=None, release=None, error=None):
        """Run an update check on `window` with a fake request, and wait for it. Returns how often it asked."""
        window = window or self.window
        asked = []

        def fetch():
            asked.append(1)
            if error:
                raise error
            return release or self.RELEASE
        window.start_update_check(fetch)
        pump(TIMEOUT, lambda: not any(t.name == "update-check" and t.is_alive() for t in threading.enumerate()))
        pump(0.2)
        return len(asked)

    def test_opening_the_window_asks_nothing(self):
        with mock.patch.object(updates, "fetch_latest", side_effect=AssertionError("asked")) as fetch:
            window = self.new_window()
            pump(0.4)
            self.close(window)
        fetch.assert_not_called()

    def test_the_help_menu_has_the_switch_and_it_starts_on(self):
        action = self.window.updates_action
        self.assertEqual((action.text(), action.isCheckable(), action.isChecked()), ("Check for updates", True, True))
        menus = {a.text().replace("&", ""): a.menu() for a in self.window.menuBar().actions()}
        self.assertIn(action, menus["Help"].actions())

    def test_a_newer_version_shows_a_small_banner_with_two_buttons(self):
        self.assertFalse(self.window.update_banner.isVisible())
        self.assertEqual(self.check(), 1)
        banner = self.window.update_banner
        self.assertTrue(banner.isVisible())
        self.assertEqual(self.window.update_label.text(), f"{APP_NAME} 99.0.0 is available")
        self.assertEqual((self.window.update_download.text(), self.window.update_skip.text()), ("Download", "Not now"))
        self.assertTrue(self.window.update_download.isVisible() and self.window.update_skip.isVisible())

    def test_the_banner_does_not_block_anything(self):
        self.check()
        from PySide6.QtWidgets import QDialog
        self.assertFalse(any(isinstance(w, QDialog) and w.isVisible() for w in QApplication.topLevelWidgets()))
        self.assertTrue(self.window.isEnabled())
        self.load(self.clip)                               # the window works as usual
        self.assertTrue(self.window.export_button.isEnabled())

    def test_the_time_of_the_check_is_remembered(self):
        before = time.time()
        self.check()
        self.assertGreaterEqual(float(self.window.store.value(updates.LAST_CHECK_KEY)), before)

    def test_the_same_or_an_older_version_shows_nothing(self):
        from video_converter import __version__
        for version in (__version__, "1.0.0"):
            self.window.store.setValue(updates.LAST_CHECK_KEY, 0.0)
            self.assertEqual(self.check(release=Release(version, REPO_URL)), 1)
            self.assertFalse(self.window.update_banner.isVisible(), version)

    def test_a_failed_check_is_silent(self):
        import urllib.error
        for error in (urllib.error.URLError("offline"), TimeoutError("slow"), ValueError("not json")):
            self.window.store.setValue(updates.LAST_CHECK_KEY, 0.0)
            before = self.window.statusBar().currentMessage()
            with mock.patch("video_converter.ui.main_window.QMessageBox.exec", side_effect=AssertionError("a box")):
                self.assertEqual(self.check(error=error), 1)
            self.assertFalse(self.window.update_banner.isVisible())
            self.assertEqual(self.window.statusBar().currentMessage(), before)

    def test_it_asks_only_once_a_day(self):
        self.assertEqual(self.check(), 1)
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.check(), 0)
        self.window.store.setValue(updates.LAST_CHECK_KEY, time.time() - 25 * 3600)
        self.assertEqual(self.check(), 1)

    def test_the_day_counts_across_runs(self):
        self.assertEqual(self.check(), 1)
        self.window.store.sync()
        self.close(self.window)
        self.window = self.new_window()
        self.assertEqual(self.check(), 0)

    def test_a_failed_attempt_also_uses_up_the_day(self):
        self.assertEqual(self.check(error=OSError("offline")), 1)
        self.assertEqual(self.check(), 0)

    def test_download_opens_the_release_page_and_hides_the_banner(self):
        self.check()
        with mock.patch("video_converter.ui.main_window.QDesktopServices.openUrl") as opener:
            self.window.update_download.click()
        opener.assert_called_once_with(QUrl(self.RELEASE.url))
        self.assertFalse(self.window.update_banner.isVisible())
        self.assertEqual(self.window.store.value(updates.SKIPPED_KEY, ""), "")      # not skipped: still the newest

    def test_not_now_skips_that_version_for_good(self):
        self.check()
        self.window.update_skip.click()
        self.assertFalse(self.window.update_banner.isVisible())
        self.assertEqual(self.window.store.value(updates.SKIPPED_KEY), "99.0.0")
        self.window.store.setValue(updates.LAST_CHECK_KEY, 0.0)
        self.assertEqual(self.check(), 1)
        self.assertFalse(self.window.update_banner.isVisible())            # it asked, and kept quiet
        newer = Release("99.0.1", f"{REPO_URL}/releases/tag/v99.0.1")
        self.window.store.setValue(updates.LAST_CHECK_KEY, 0.0)
        self.assertEqual(self.check(release=newer), 1)
        self.assertTrue(self.window.update_banner.isVisible())            # but a later version is another matter
        self.assertIn("99.0.1", self.window.update_label.text())

    def test_the_skipped_version_is_remembered_by_the_next_run(self):
        self.check()
        self.window.update_skip.click()
        self.window.store.setValue(updates.LAST_CHECK_KEY, 0.0)
        self.window.store.sync()
        self.close(self.window)
        self.window = self.new_window()
        self.assertEqual(self.check(), 1)
        self.assertFalse(self.window.update_banner.isVisible())

    def test_the_switch_turns_checks_off_and_remembers(self):
        self.check()
        self.assertTrue(self.window.update_banner.isVisible())
        self.window.updates_action.setChecked(False)
        self.assertFalse(self.window.update_banner.isVisible())            # nothing to see once it is off
        self.assertFalse(self.window.check_updates)
        self.window.store.setValue(updates.LAST_CHECK_KEY, 0.0)
        self.assertEqual(self.check(), 0)
        self.window.store.sync()
        self.close(self.window)
        self.window = self.new_window()
        self.assertFalse(self.window.updates_action.isChecked())
        self.assertEqual(self.check(), 0)
        self.window.updates_action.setChecked(True)
        self.assertEqual(self.check(), 1)
        self.assertTrue(self.window.update_banner.isVisible())

    def test_a_damaged_settings_file_does_not_stop_the_check(self):
        self.window.store.setValue(updates.LAST_CHECK_KEY, "yesterday")
        self.window.store.setValue(updates.SKIPPED_KEY, 5)
        self.assertEqual(self.check(), 1)
        self.assertTrue(self.window.update_banner.isVisible())

    def test_the_check_is_logged(self):
        with self.assertLogs("video_converter.updates", level="INFO") as logged:
            self.check()
        self.assertTrue(any("99.0.0 is out" in line for line in logged.output))


if __name__ == "__main__":
    unittest.main()
