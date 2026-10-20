"""The Sound section of the editor (2.7). Runs without a visible window; skipped without PySide6."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    import shiboken6
    from PySide6.QtCore import QEvent, QSettings
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter.core import find_ffmpeg
from video_converter.project import OUTPUT_AUDIO, ClipSettings
from video_converter.video import VideoEdit

FFMPEG = find_ffmpeg()
TIMEOUT = 30


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


class SoundSectionTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.inspector import Inspector
        self.inspector = Inspector()
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.edits = []
        self.inspector.settingsEdited.connect(lambda s, label, final: self.edits.append((s, label, final)))
        self.music = Path(tempfile.gettempdir()) / "a song.mp3"

    def tearDown(self):
        self.inspector.close()
        shiboken6.delete(self.inspector)     # free it now: every widget left alive slows each later theme change

    def show(self, **edit):
        self.inspector.set_settings(ClipSettings(edit=VideoEdit(**edit)), 10.0)

    def choose(self, path):
        with mock.patch("video_converter.ui.inspector.QFileDialog.getOpenFileName", return_value=(path, "")) as dialog:
            self.inspector.music_choose.click()
        return dialog

    def test_showing_the_settings_reports_no_edits(self):
        self.show(volume=6.0, normalize=True, audio_denoise="strong", music=str(self.music), music_volume=-30.0,
                  music_duck=True)
        self.assertEqual(self.edits, [])

    def test_the_panel_shows_the_settings(self):
        self.show(volume=-7.0, normalize=True, audio_denoise="light", music=str(self.music), music_volume=-30.0,
                  music_duck=True)
        inspector = self.inspector
        self.assertEqual(inspector.volume_slider.value(), -7)
        self.assertEqual(inspector.volume_label.text(), "−7 dB")
        self.assertTrue(inspector.normalize_box.isChecked())
        self.assertEqual(inspector.noise_box.currentData(), "light")
        self.assertEqual(inspector.music_name.text(), "a song.mp3")
        self.assertEqual(inspector.music_slider.value(), -30)
        self.assertEqual(inspector.music_label.text(), "−30 dB")
        self.assertTrue(inspector.duck_box.isChecked())

    def test_the_defaults(self):
        inspector = self.inspector
        self.assertEqual((inspector.volume_slider.value(), inspector.volume_slider.minimum(),
                          inspector.volume_slider.maximum()), (0, -20, 20))
        self.assertEqual(inspector.volume_label.text(), "0 dB")
        self.assertFalse(inspector.normalize_box.isChecked())
        self.assertEqual([inspector.noise_box.itemText(i) for i in range(inspector.noise_box.count())],
                         ["Off", "Light", "Strong"])
        self.assertEqual(inspector.noise_box.currentData(), "off")
        self.assertEqual(inspector.music_name.text(), "None")
        self.assertEqual((inspector.music_slider.value(), inspector.music_slider.minimum(),
                          inspector.music_slider.maximum()), (-18, -40, 0))
        self.assertFalse(inspector.duck_box.isChecked())

    def test_the_volume_slider_edits_the_volume(self):
        self.inspector.volume_slider.setValue(6)
        settings, label, final = self.edits[-1]
        self.assertEqual((settings.edit.volume, label, final), (6.0, "Volume", True))
        self.assertEqual(self.inspector.volume_label.text(), "+6 dB")
        self.inspector.volume_slider.setValue(-20)
        self.assertEqual(self.edits[-1][0].edit.volume, -20.0)
        self.inspector.volume_slider.setValue(20)
        self.assertEqual(self.edits[-1][0].edit.volume, 20.0)

    def test_a_slider_drag_is_one_complete_edit(self):
        slider = self.inspector.volume_slider
        slider.setSliderDown(True)
        slider.setValue(3)
        slider.setValue(9)
        slider.setSliderDown(False)
        slider.sliderReleased.emit()
        finals = [final for _, _, final in self.edits]
        self.assertEqual(finals[-1], True)
        self.assertNotIn(True, finals[:-1])
        self.assertEqual(self.edits[-1][0].edit.volume, 9.0)

    def test_normalize_noise_and_ducking_edits(self):
        self.inspector.normalize_box.setChecked(True)
        self.assertEqual((self.edits[-1][0].edit.normalize, self.edits[-1][1]), (True, "Even loudness"))
        box = self.inspector.noise_box
        box.setCurrentIndex(box.findData("strong"))
        self.assertEqual((self.edits[-1][0].edit.audio_denoise, self.edits[-1][1]), ("strong", "Noise reduction"))
        self.show(music=str(self.music))
        self.inspector.duck_box.setChecked(True)
        self.assertEqual((self.edits[-1][0].edit.music_duck, self.edits[-1][1]), (True, "Music ducking"))

    def test_choosing_music_stores_its_path(self):
        dialog = self.choose(str(self.music))
        self.assertEqual(self.edits[-1][0].edit.music, str(Path(self.music)))
        self.assertEqual(self.edits[-1][1], "Choose music")
        filters = dialog.call_args.args[3]
        for extension in (".mp3", ".wav", ".m4a", ".flac"):
            self.assertIn(f"*{extension}", filters)

    def test_cancelling_the_file_dialog_changes_nothing(self):
        self.choose("")
        self.assertEqual(self.edits, [])

    def test_removing_the_music(self):
        self.show(music=str(self.music), music_duck=True, music_volume=-9.0)
        self.assertTrue(self.inspector.music_remove.isEnabled())
        self.inspector.music_remove.click()
        edit = self.edits[-1][0].edit
        self.assertEqual((edit.music, edit.music_duck, edit.music_volume), ("", True, -9.0))      # its other settings stay
        self.assertEqual(self.edits[-1][1], "Remove music")

    def test_the_music_volume_slider(self):
        self.show(music=str(self.music))
        self.inspector.music_slider.setValue(-6)
        self.assertEqual((self.edits[-1][0].edit.music_volume, self.edits[-1][1]), (-6.0, "Music volume"))
        self.assertEqual(self.inspector.music_label.text(), "−6 dB")

    def test_the_music_controls_wait_for_a_music_file(self):
        inspector = self.inspector
        self.assertFalse(inspector.music_holder.isEnabled())
        self.assertFalse(inspector.duck_box.isEnabled())
        self.assertFalse(inspector.music_remove.isEnabled())
        self.show(music=str(self.music))
        self.assertTrue(inspector.music_holder.isEnabled())
        self.assertTrue(inspector.duck_box.isEnabled())
        self.assertTrue(inspector.music_remove.isEnabled())

    def test_a_music_file_that_is_gone_is_pointed_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real.mp3"
            real.write_bytes(b"x")
            self.show(music=str(real))
            self.assertTrue(self.inspector.music_missing.isHidden())
            self.show(music=str(Path(tmp) / "gone.mp3"))
            self.assertFalse(self.inspector.music_missing.isHidden())
            self.show()
            self.assertTrue(self.inspector.music_missing.isHidden())

    def test_mute_wins_so_the_sound_options_are_off(self):
        self.show(mute=True, music=str(self.music))
        for widget in (self.inspector.volume_holder, self.inspector.normalize_box, self.inspector.noise_box,
                       self.inspector.music_choose, self.inspector.music_holder, self.inspector.duck_box):
            self.assertFalse(widget.isEnabled())
        self.show(mute=False, music=str(self.music))
        for widget in (self.inspector.volume_holder, self.inspector.normalize_box, self.inspector.noise_box,
                       self.inspector.music_choose, self.inspector.music_holder, self.inspector.duck_box):
            self.assertTrue(widget.isEnabled())

    def test_an_mp3_has_the_same_options_and_ignores_a_leftover_mute(self):
        self.inspector.set_settings(ClipSettings(output=OUTPUT_AUDIO, edit=VideoEdit(mute=True)), 10.0)
        self.assertFalse(self.inspector.audio_section.isHidden())
        self.assertTrue(self.inspector.mute_box.isHidden())
        self.assertTrue(self.inspector.volume_holder.isEnabled())
        self.assertTrue(self.inspector.normalize_box.isEnabled())

    def test_the_texts_are_plain_english(self):
        self.assertEqual(self.inspector.normalize_box.text(), "Make the loudness even (−14 LUFS)")
        self.assertEqual(self.inspector.duck_box.text(), "Lower the music when people talk")
        labels = {label.text() for label in self.inspector.audio_section.findChildren(QLabel)}
        for text in ("Volume", "Noise reduction", "Music", "Music volume"):
            self.assertIn(text, labels)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class MainWindowSoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "clip.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=duration=2:size=160x90:rate=10",
                        "-f", "lavfi", "-i", "sine=duration=2", "-c:a", "aac", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-shortest", str(cls.clip)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        from video_converter.ui.main_window import MainWindow
        ini = self.tmp / f"settings-{self.id().split('.')[-1]}.ini"
        self.window = MainWindow(FFMPEG, QSettings(str(ini), QSettings.Format.IniFormat))
        self.window.resize(1200, 800)
        self.window.show()
        self.window.add_paths([self.clip])
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)), "file never loaded")

    def tearDown(self):
        self.window.close()
        self.window.loader.shutdown(wait=True)
        self.window.deleteLater()
        self.window = None
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(0.1)

    def test_sound_edits_are_undoable_and_follow_the_selected_file(self):
        item, inspector = self.window.current, self.window.inspector
        inspector.volume_slider.setValue(6)
        inspector.normalize_box.setChecked(True)
        inspector.noise_box.setCurrentIndex(inspector.noise_box.findData("light"))
        self.assertEqual((item.settings.edit.volume, item.settings.edit.normalize, item.settings.edit.audio_denoise),
                         (6.0, True, "light"))
        self.assertEqual(self.window.undo_stack.count(), 3)
        self.assertIn("+6 dB", item.list_item.text())
        self.assertIn("even loudness", item.list_item.text())
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.audio_denoise, "off")
        self.assertEqual(inspector.noise_box.currentData(), "off")          # the panel follows
        self.window.undo_stack.undo()
        self.assertFalse(item.settings.edit.normalize)
        self.assertFalse(inspector.normalize_box.isChecked())
        self.window.undo_stack.undo()
        self.assertEqual((item.settings.edit.volume, inspector.volume_slider.value()), (0.0, 0))
        self.window.undo_stack.redo()
        self.assertEqual(item.settings.edit.volume, 6.0)

    def test_music_and_ducking_are_undoable(self):
        item, inspector = self.window.current, self.window.inspector
        with mock.patch("video_converter.ui.inspector.QFileDialog.getOpenFileName",
                        return_value=(str(self.clip), "")):
            inspector.music_choose.click()
        self.assertEqual(item.settings.edit.music, str(self.clip))
        inspector.duck_box.setChecked(True)
        inspector.music_slider.setValue(-25)
        edit = item.settings.edit
        self.assertEqual((edit.music_duck, edit.music_volume), (True, -25.0))
        self.window.undo_stack.undo()
        self.window.undo_stack.undo()
        self.assertEqual((item.settings.edit.music, item.settings.edit.music_duck), (str(self.clip), False))
        self.window.undo_stack.undo()
        self.assertEqual((item.settings.edit.music, inspector.music_name.text()), ("", "None"))

    def test_an_export_job_takes_the_sound_settings_with_it(self):
        item, inspector = self.window.current, self.window.inspector
        inspector.volume_slider.setValue(-4)
        inspector.normalize_box.setChecked(True)
        job = self.window._make_job(item)
        self.assertEqual((job.settings.edit.volume, job.settings.edit.normalize), (-4.0, True))
        inspector.volume_slider.setValue(5)                           # later edits don't reach the snapshot
        self.assertEqual(job.settings.edit.volume, -4.0)

    def test_each_file_keeps_its_own_sound_settings(self):
        other = self.tmp / "other.mp4"
        other.write_bytes(self.clip.read_bytes())
        self.window.add_paths([other])
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)))
        first, second = self.window.items
        self.window.media_list.setCurrentRow(0)
        self.window.inspector.volume_slider.setValue(8)
        self.window.media_list.setCurrentRow(1)
        self.assertEqual(self.window.inspector.volume_slider.value(), 0)
        self.assertEqual((first.settings.edit.volume, second.settings.edit.volume), (8.0, 0.0))
        self.window.media_list.setCurrentRow(0)
        self.assertEqual(self.window.inspector.volume_slider.value(), 8)


if __name__ == "__main__":
    unittest.main()
