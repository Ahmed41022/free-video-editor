"""The home page: the feature cards, what is hidden on it, and how the app starts."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("VIDEO_EDITOR_NO_GPU_SEARCH", "1")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    from PySide6.QtCore import QEvent, QSettings, Qt
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter import i18n  # noqa: E402
from video_converter.core import find_ffmpeg  # noqa: E402
from video_converter.project import OUTPUT_AUDIO, OUTPUT_VIDEO  # noqa: E402

from tests.test_ui import make_video, pump  # noqa: E402

FFMPEG = find_ffmpeg()


def setUpModule():
    if QApplication is None:
        raise unittest.SkipTest("PySide6 is not installed")
    global app
    app = QApplication.instance() or QApplication([])


class HomeCase(unittest.TestCase):
    """A fresh window per test, with its own settings and projects folder."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.video = cls.tmp / "clip.mp4"
        cls.second = cls.tmp / "second.mp4"
        cls.song = cls.tmp / "song.mp3"
        if FFMPEG:
            make_video(cls.video)
            make_video(cls.second)
            subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=duration=2",
                    str(cls.song)
                ], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def new_window(self, load=True, home=False, files=None):
        from video_converter.ui.main_window import MainWindow

        name = self.id().split(".")[-1]
        ini = self.tmp / f"{name}.ini"
        with mock.patch("video_converter.gpu.detect_encoders", return_value=()), \
                mock.patch("video_converter.gpu.gpu_name", return_value=None):
            window = MainWindow(
                FFMPEG, QSettings(str(ini), QSettings.Format.IniFormat), self.tmp / f"{name}-p")
        self.window = window
        window.resize(1300, 820)
        window.show()
        if load:
            window.add_paths(files or [self.video])
            self.assertTrue(pump(30, lambda: all(not i.loading for i in window.items)))
        if home:
            window.show_home()
            window.home.finish()  # the cards are normally made a few at a time
        return window

    def tearDown(self):
        window = getattr(self, "window", None)
        if window is not None:
            window.close()
            window.loader.shutdown(wait=True)
            window.deleteLater()
            self.window = None
            QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            pump(0.1)

    @staticmethod
    def only_open(window, section):
        """Whether `section` is the only open section of the settings panel."""
        return section.header.isChecked() and all(
            not s.header.isChecked() for s in window.inspector.all_sections if s is not section)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class FeatureCardTests(HomeCase):

    def choose(self, window, key):
        window.home.feature_cards[key].click()

    def test_save_as_mp3_really_makes_the_output_mp3(self):
        window = self.new_window(home=True)
        self.choose(window, "mp3")
        self.assertFalse(window.at_home)
        self.assertEqual(window.current.settings.output, OUTPUT_AUDIO)
        self.assertEqual(
            window.inspector.output_group.checkedButton().property("value"), OUTPUT_AUDIO)
        self.assertTrue(self.only_open(window, window.inspector.output_section))

    def test_a_file_size_target_is_really_selected(self):
        window = self.new_window(home=True)
        self.choose(window, "size")
        self.assertGreater(window.current.settings.edit.target_mb, 0)
        self.assertEqual(window.inspector.crf_box.currentData(), "fit")
        self.assertTrue(window.inspector.limit_box.isVisibleTo(window.inspector))
        self.assertTrue(self.only_open(window, window.inspector.quality_section))

    def test_the_other_choices_are_really_made(self):
        window = self.new_window(home=True)
        for key, check in (("gif", lambda s: s.edit.video_format == "gif"),
                           ("vertical", lambda s: s.edit.aspect ==
                            (9, 16)), ("reverse",
                                       lambda s: s.edit.reverse), ("mute", lambda s: s.edit.mute),
                           ("steady", lambda s: s.edit.stabilize == "light"),
                           ):
            window.show_home()
            self.choose(window, key)
            self.assertTrue(check(window.current.settings), key)
        panel = window.inspector
        self.assertEqual(panel.format_box.currentData(), "gif")
        self.assertEqual(panel.stabilize_box.currentData(), "light")
        self.assertTrue(panel.mute_box.isChecked())
        self.assertTrue(panel.reverse_box.isChecked())

    def test_a_size_target_replaces_a_gif(self):
        window = self.new_window(home=True)
        self.choose(window, "gif")
        window.show_home()
        self.choose(window, "size")
        self.assertEqual(window.current.settings.edit.video_format, "mp4")
        self.assertEqual(window.inspector.crf_box.currentData(), "fit")

    def test_a_feature_with_a_choice_can_be_undone(self):
        window = self.new_window(home=True)
        self.choose(window, "mp3")
        window.undo_stack.undo()
        self.assertEqual(window.current.settings.output, OUTPUT_VIDEO)

    def test_a_feature_without_a_choice_opens_its_section_and_folds_the_rest(self):
        window = self.new_window(home=True)
        for key, section in (("trim", "trim_section"), ("silences", "trim_section"),
                             ("speed", "speed_section"), ("look", "adjust_section"),
                             ("sound", "audio_section"), ("text", "captions_section"),
                             ("captions", "captions_section"), ("fades", "fade_section"),
                             ("presets", "preset_section"), ("crop", "frame_section"),
                             ):
            window.show_home()
            self.choose(window, key)
            self.assertTrue(self.only_open(window, getattr(window.inspector, section)), key)

    def test_the_chosen_section_is_first_in_the_panel(self):
        window = self.new_window(home=True)
        self.choose(window, "speed")
        layout = window.inspector.panel_layout
        self.assertIs(layout.itemAt(1).widget(), window.inspector.speed_section)

    def test_a_feature_that_has_no_section_still_folds_all_of_them(self):
        window = self.new_window(home=True, files=[self.video, self.second])
        self.choose(window, "join")
        self.assertTrue(all(not s.header.isChecked() for s in window.inspector.all_sections))
        self.assertIn("Join", window.statusBar().currentMessage())

    def test_start_in_general_opens_every_section_but_text(self):
        window = self.new_window(home=True)
        self.choose(window, "speed")
        window.show_home()
        with mock.patch.object(type(window), "choose_files") as chooser:
            window.home.start_button.click()
        chooser.assert_not_called()  # files are open already
        self.assertFalse(window.at_home)
        panel = window.inspector
        self.assertTrue(
            all(
                s.header.isChecked() for s in panel.all_sections
                if s is not panel.captions_section))
        self.assertIs(panel.panel_layout.itemAt(1).widget(), panel.preset_section)

    def test_a_feature_chosen_before_any_file_waits_for_the_first_one(self):
        window = self.new_window(load=False, home=True)
        with mock.patch.object(type(window), "choose_files") as chooser:
            self.choose(window, "mp3")
        chooser.assert_called_once()
        self.assertFalse(window.at_home)
        window.add_paths([self.video])
        self.assertTrue(
            pump(
                30, lambda: window.current is not None and window.current.settings.output ==
                OUTPUT_AUDIO))
        self.assertTrue(self.only_open(window, window.inspector.output_section))

    def test_a_picture_feature_says_so_for_a_sound_file(self):
        window = self.new_window(home=True, files=[self.song])
        self.choose(window, "reverse")
        self.assertIn("sound only", window.statusBar().currentMessage())
        self.assertFalse(window.current.settings.edit.reverse)

    def test_the_helpers_are_not_features(self):
        from video_converter.ui.features import FEATURES

        titles = " ".join(f.title.lower() for f in FEATURES)
        for helper in ("export", "before", "after"):
            self.assertNotIn(helper, titles)

    def test_every_feature_has_a_card_and_a_real_section(self):
        from video_converter.ui.features import FEATURES

        window = self.new_window(load=False, home=True)
        self.assertEqual(set(window.home.feature_cards), {f.id for f in FEATURES})
        for feature in FEATURES:
            if feature.section:
                self.assertIn(
                    getattr(window.inspector, feature.section), window.inspector.all_sections)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class HomePageTests(HomeCase):

    def test_the_home_page_hides_what_needs_a_file(self):
        window = self.new_window(home=True)
        self.assertTrue(window.at_home)
        for action in window.editor_only_actions:
            self.assertFalse(action.isVisible(), action.text())
        for action in (window.remove_action, window.undo_action, window.redo_action,
                       window.frame_action, window.save_action):
            self.assertFalse(action.isEnabled(), action.text())
        self.assertFalse(window.as_is_action.isEnabled())
        for action in window.editor_shortcuts:
            self.assertFalse(action.isEnabled())
        self.assertTrue(window.add_action.isVisible())
        self.assertTrue(window.theme_action.isVisible())
        self.assertFalse(window.home_action.isVisible())

    def test_they_come_back_in_the_editor_as_they_were(self):
        window = self.new_window()
        window.cut_at("split", 1.0)  # an edit, so that Undo is on
        self.assertTrue(window.undo_action.isEnabled())
        window.show_home()
        window.show_editor()
        for action in (window.save_action, window.remove_action, window.undo_action,
                       window.frame_action, window.join_action, window.export_all_action,
                       window.export_action, window.home_action):
            self.assertTrue(action.isVisible(), action.text())
        self.assertTrue(window.undo_action.isEnabled())
        self.assertFalse(window.redo_action.isEnabled())
        self.assertTrue(window.export_button.isEnabled())
        self.assertTrue(window.remove_action.isEnabled())

    def test_a_file_that_finishes_loading_on_the_home_page_does_not_bring_them_back(self):
        window = self.new_window(load=False)
        window.add_paths([self.video])
        window.show_home()
        self.assertTrue(pump(30, lambda: not window.items[0].loading))
        self.assertFalse(window.remove_action.isEnabled())
        self.assertFalse(window.export_button.isEnabled())
        self.assertFalse(window.frame_action.isEnabled())

    def test_the_project_menu_only_offers_what_applies(self):
        window = self.new_window(home=True)
        window._sync_project_menu()
        self.assertFalse(window.back_action.isVisible())
        self.assertFalse(window.save_as_action.isVisible())
        window.show_editor()
        window._sync_project_menu()
        self.assertTrue(window.back_action.isVisible())
        self.assertFalse(window.rename_action.isVisible())

    def test_the_home_button_and_the_menu_lead_home(self):
        window = self.new_window()
        self.assertFalse(window.at_home)
        window.home_action.trigger()
        self.assertTrue(window.at_home)
        self.assertIs(window.stack.currentWidget(), window.home)
        window.home.back_button.click()
        self.assertFalse(window.at_home)
        self.assertIs(window.stack.currentWidget(), window.main_split)
        window.back_action.trigger()
        self.assertTrue(window.at_home)

    def test_home_offers_a_way_back_only_when_work_is_open(self):
        window = self.new_window(load=False, home=True)
        self.assertTrue(window.home.back_button.isHidden())
        window.add_paths([self.video])
        window.show_home()
        self.assertFalse(window.home.back_button.isHidden())

    def test_files_given_at_launch_skip_the_home_page(self):
        window = self.new_window(load=False)
        window.start_up({}, [self.video])
        self.assertFalse(window.at_home)
        self.assertIsNone(window.home)
        self.assertEqual(len(window.items), 1)

    def test_no_files_at_launch_shows_the_home_page(self):
        window = self.new_window(load=False)
        window.start_up({}, [])
        self.assertTrue(window.at_home)

    def test_forwarded_files_go_straight_to_the_editor(self):
        window = self.new_window(load=False, home=True)
        window.open_forwarded([str(self.video)])
        self.assertFalse(window.at_home)
        self.assertEqual(len(window.items), 1)

    def test_added_files_leave_the_home_page(self):
        window = self.new_window(load=False, home=True)
        window.add_paths([self.second])
        self.assertFalse(window.at_home)

    def test_the_window_title_names_the_file_only_in_the_editor(self):
        window = self.new_window()
        self.assertIn("clip.mp4", window.windowTitle())
        window.show_home()
        self.assertNotIn("clip.mp4", window.windowTitle())

    def test_the_home_page_is_made_only_when_it_is_first_shown(self):
        window = self.new_window(load=False)
        self.assertIsNone(window.home)
        window.show_home()
        self.assertIsNotNone(window.home)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class HomeLanguageTests(HomeCase):

    def use(self, code):
        from video_converter.ui import language

        i18n.set_language(code)
        language.apply_to_app(app)

        def restore():
            i18n.set_language("en")
            language.apply_to_app(app)

        self.addCleanup(restore)

    def test_the_home_page_is_right_to_left_and_all_arabic(self):
        from tests.test_i18n import english_words

        self.use("ar")
        window = self.new_window(load=False, home=True)
        self.assertEqual(window.home.layoutDirection(), Qt.LayoutDirection.RightToLeft)
        texts = [
            w.text()
            for w in [*window.home.findChildren(QLabel), *window.home.findChildren(QPushButton)]
        ]
        left = [t for t in texts if t and english_words(t, ("Free Video Editor", "MP3", "GIF"))]
        self.assertEqual(left, [])
        window.home.resize(1000, 700)
        pump(0.2)
        first = window.home.feature_cards["trim"]
        second = window.home.feature_cards["silences"]
        # the first card is at the right edge, and the next one is to its left
        self.assertGreater(
            first.mapTo(window.home,
                        first.rect().topLeft()).x(),
            second.mapTo(window.home,
                         second.rect().topLeft()).x())


if __name__ == "__main__":
    unittest.main()
