"""Projects: saving, opening, autosave, recovery after a crash, names, and quitting."""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("VIDEO_EDITOR_NO_GPU_SEARCH", "1")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    from PySide6.QtCore import QEvent, QSettings, Qt
    from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter import i18n, projects  # noqa: E402
from video_converter.core import find_ffmpeg  # noqa: E402
from video_converter.project import OUTPUT_AUDIO, ClipSettings  # noqa: E402
from video_converter.projects import (  # noqa: E402
    DuplicateName, FileEntry, InvalidName, ProjectData, ProjectStore, UnreadableProject,
)

from tests.test_ui import make_video, pump  # noqa: E402

FFMPEG = find_ffmpeg()


def setUpModule():
    if QApplication is None:
        raise unittest.SkipTest("PySide6 is not installed")
    global app
    app = QApplication.instance() or QApplication([])


class StoreTests(unittest.TestCase):
    """The projects folder, without a window."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ProjectStore(Path(self._tmp.name) / "projects")

    def test_a_project_round_trips_with_its_settings(self):
        settings = ClipSettings(output=OUTPUT_AUDIO)
        settings.edit.target_mb = 12.5
        settings.edit.start = 1.5
        settings.edit.keep = [(0.0, 1.0), (2.0, 3.0)]
        project = self.store.create("First")
        project.files = [
            FileEntry("C:/videos/a.mp4", settings.to_dict(), True),
            FileEntry("C:/videos/b.mp4", {}, False)
        ]
        project.selected = 1
        self.store.save(project)
        loaded = self.store.load(project.id)
        self.assertEqual(loaded.name, "First")
        self.assertEqual(loaded.selected, 1)
        self.assertEqual([f.path for f in loaded.files], ["C:/videos/a.mp4", "C:/videos/b.mp4"])
        self.assertTrue(loaded.files[0].play_original)
        self.assertEqual(ClipSettings.from_dict(loaded.files[0].settings), settings)
        self.assertEqual(
            json.loads(self.store.project_path(project.id).read_text())["format"],
            projects.FORMAT_VERSION)

    def test_a_write_that_fails_leaves_the_old_project_whole(self):
        project = self.store.create("Safe")
        project.files = [FileEntry("a.mp4")]
        self.store.save(project)
        project.files = [FileEntry("a.mp4"), FileEntry("b.mp4")]
        with mock.patch.object(projects.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.save(project)
        self.assertEqual(len(self.store.load(project.id).files), 1)
        self.store.save(project)
        self.assertEqual(len(self.store.load(project.id).files), 2)
        self.assertEqual(list(self.store.folder.glob("*.tmp")), [])

    def test_names_are_unique_without_regard_to_case(self):
        self.store.create("Holiday")
        for clash in ("holiday", "HOLIDAY", "  Holiday  ", "Holi\u0064ay"):
            with self.assertRaises(DuplicateName, msg=clash):
                self.store.create(clash)
        other = self.store.create("Work")
        with self.assertRaises(DuplicateName):
            self.store.rename(other.id, "hOLIDAY")
        self.assertEqual(self.store.rename(other.id, "work"), "work")  # its own name is fine
        self.assertEqual(len(self.store.list()), 2)

    def test_a_name_must_be_usable(self):
        for bad in ("", "   ", "x" * 81, "a\u0000b"):
            with self.assertRaises(InvalidName, msg=repr(bad)):
                self.store.create(bad)
        self.assertEqual(self.store.create("  two   words ").name, "two words")

    def test_the_suggested_name_is_free(self):
        self.assertEqual(self.store.suggest_name("My project"), "My project")
        self.store.create("My project")
        self.store.create("my project 2")
        self.assertEqual(self.store.suggest_name("My project"), "My project 3")

    def test_listing_reads_only_the_json_files_and_skips_damaged_ones(self):
        good = self.store.create("Good")
        good.files = [FileEntry("C:/no/such/video.mp4")]  # a video that isn't there is no problem
        self.store.save(good)
        (self.store.folder / "broken.json").write_text("{not json", encoding="utf-8")
        (self.store.folder / "future.json").write_text(
            json.dumps({
                "format": 99,
                "name": "New"
            }), encoding="utf-8")
        with self.assertLogs("video_converter.projects", "WARNING"):
            found = self.store.list()
        self.assertEqual([p.name for p in found], ["Good"])
        self.assertEqual(found[0].file_count, 1)
        self.assertTrue((self.store.folder / "broken.json").exists())  # never deleted

    def test_a_project_from_a_newer_version_is_not_opened(self):
        path = self.store.folder / "future.json"
        self.store.folder.mkdir(parents=True)
        path.write_text(json.dumps({"format": 99, "id": "future", "name": "N"}), encoding="utf-8")
        with self.assertRaises(UnreadableProject):
            self.store.load("future")

    def test_unknown_keys_and_bad_entries_are_ignored(self):
        data = ProjectData.from_json(
            {
                "format": 1,
                "id": "x",
                "name": "N",
                "extra": 1,
                "selected": "two",
                "files": [{
                    "path": "a.mp4",
                    "settings": {
                        "zzz": 1
                    }
                }, {
                    "path": 3
                }, "no", {}]
            })
        self.assertEqual([f.path for f in data.files], ["a.mp4"])
        self.assertEqual(data.selected, -1)

    def test_the_most_recently_saved_comes_first(self):
        a, b = self.store.create("A"), self.store.create("B")
        a.saved = "2026-01-01T10:00:00Z"
        b.saved = "2026-02-01T10:00:00Z"
        self.store.save(a, stamp=False)
        self.store.save(b, stamp=False)
        self.assertEqual([p.name for p in self.store.list()], ["B", "A"])

    def test_the_date_is_day_month_year_with_slashes(self):
        when = projects.parse_time("2026-11-19T12:05:33Z")
        text = projects.format_date(when)
        self.assertRegex(text, r"^\d\d/\d\d/\d{4}$")
        self.assertRegex(projects.format_date_time(when), r"^\d\d/\d\d/\d{4} \d\d:\d\d$")
        local = when.astimezone()
        self.assertEqual(text, f"{local.day:02d}/{local.month:02d}/{local.year}")
        self.assertEqual(projects.format_date(None), "")
        self.assertIsNone(projects.parse_time("yesterday"))

    def test_recovery_files_are_not_projects(self):
        data = ProjectData("", "", files=[FileEntry("a.mp4")])
        self.store.write_recovery("s1", data)
        self.assertEqual(self.store.list(), [])
        found = self.store.recoveries("other")
        self.assertEqual(len(found), 1)
        self.assertEqual(self.store.recovery_session(found[0].path), "s1")
        self.assertEqual(self.store.recoveries("s1"), [])  # this run's own file is not offered
        self.store.remove_recovery("s1")
        self.assertEqual(self.store.recoveries(), [])

    def test_the_folder_is_the_apps_data_folder_unless_told_otherwise(self):
        with mock.patch.dict(os.environ, {"VIDEO_EDITOR_PROJECTS_DIR": ""}):
            os.environ.pop("VIDEO_EDITOR_PROJECTS_DIR")
            with mock.patch.object(projects.sys, "platform", "win32"), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/me/AppData/Local"}):
                folder = projects.default_folder()
            self.assertEqual(
                folder,
                Path("C:/Users/me/AppData/Local") / "Free Video Editor" / "projects")
            with mock.patch.object(projects.sys, "platform", "linux"), \
                    mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/data"}):
                self.assertEqual(
                    projects.default_folder(),
                    Path("/data") / "free-video-editor" / "projects")
        with mock.patch.dict(os.environ, {"VIDEO_EDITOR_PROJECTS_DIR": "/elsewhere"}):
            self.assertEqual(projects.default_folder(), Path("/elsewhere"))

    def test_a_hundred_files_are_written_in_well_under_ten_milliseconds(self):
        import time

        project = self.store.create("Big")
        project.files = [
            FileEntry(f"C:/videos/clip{n}.mp4",
                      ClipSettings().to_dict()) for n in range(100)
        ]
        self.store.save(project)
        start = time.perf_counter()
        for _ in range(10):
            self.store.save(project)
        each = (time.perf_counter() - start) / 10
        self.assertLess(each, 0.05, f"{each * 1000:.1f} ms")  # a generous bound; measured ~1 ms


class WindowCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.video = cls.tmp / "clip.mp4"
        cls.second = cls.tmp / "second.mp4"
        if FFMPEG:
            make_video(cls.video)
            make_video(cls.second)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.windows = []
        self.folder = self.tmp / (self.id().split(".")[-1] + "-projects")

    def new_window(self, load=None, home=False):
        from video_converter.ui.main_window import MainWindow

        ini = self.tmp / f"{self.id().split('.')[-1]}-{len(self.windows)}.ini"
        with mock.patch("video_converter.gpu.detect_encoders", return_value=()), \
                mock.patch("video_converter.gpu.gpu_name", return_value=None):
            window = MainWindow(
                FFMPEG, QSettings(str(ini), QSettings.Format.IniFormat), self.folder)
        self.windows.append(window)
        window.resize(1300, 820)
        window.show()
        window.autosave_quiet = 0.05
        if load:
            window.add_paths(load)
            self.loaded(window)
        if home:
            window.show_home()
        return window

    def loaded(self, window):
        self.assertTrue(
            pump(30, lambda: window.items and all(not i.loading for i in window.items)),
            "files never loaded")

    def tearDown(self):
        for window in self.windows:
            with mock.patch.dict(os.environ, {"VIDEO_EDITOR_NO_QUIT_PROMPT": "1"}):
                window.close()
            window.loader.shutdown(wait=True)
            window.deleteLater()
        self.windows = []
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(0.1)

    @staticmethod
    def name_is(name):
        return mock.patch.object(QInputDialog, "getText", return_value=(name, True))

    @staticmethod
    def click(text):
        """A QMessageBox.exec that presses the button called `text`."""

        def answer(box):
            next(b for b in box.buttons() if b.text() == text).click()
            return 0

        return mock.patch.object(QMessageBox, "exec", new=answer)

    @staticmethod
    def question(yes):
        """QMessageBox.question answered Yes or No."""
        button = QMessageBox.StandardButton.Yes if yes else QMessageBox.StandardButton.No
        return mock.patch.object(QMessageBox, "question", return_value=button)

    def make_project(self, window, name="Trip"):
        with self.name_is(name):
            self.assertTrue(window.save_as_project())
        return window.project


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class SaveAndOpenTests(WindowCase):

    def test_a_project_comes_back_with_its_files_settings_and_selection(self):
        window = self.new_window([self.video, self.second])
        window.select(window.items[1])
        window.items[0].settings.output = OUTPUT_AUDIO
        window.items[0].settings.edit.target_mb = 7.5
        window.items[1].play_original = True
        project = self.make_project(window)
        self.assertTrue(window.projects.thumbnail_path(project.id).is_file())

        other = self.new_window()
        other.open_project(project.id)
        self.loaded(other)
        self.assertEqual([i.path for i in other.items], [self.video, self.second])
        self.assertEqual(other.items[0].settings.output, OUTPUT_AUDIO)
        self.assertEqual(other.items[0].settings.edit.target_mb, 7.5)
        self.assertTrue(other.items[1].play_original)
        self.assertIs(other.current, other.items[1])
        self.assertEqual(other.project.name, "Trip")
        self.assertIn("Trip", other.windowTitle())
        self.assertEqual(other.undo_stack.count(), 0)  # the undo history is not kept

    def test_opening_reads_each_file_once_with_its_saved_settings_in_place(self):
        window = self.new_window([self.video, self.second])
        window.items[0].settings.edit.target_mb = 9.0
        project = self.make_project(window)
        other = self.new_window()
        from video_converter.ui.main_window import library

        with mock.patch.object(library, "probe_file", wraps=library.probe_file) as probe:
            other.open_project(project.id)
            self.assertEqual(other.items[0].settings.edit.target_mb, 9.0)  # before any reading
            self.loaded(other)
        self.assertEqual(probe.call_count, 2)

    def test_the_project_picture_is_made_once_not_on_every_save(self):
        from video_converter.ui.main_window import projects as mixin

        window = self.new_window([self.video])
        with mock.patch.object(mixin.shutil, "copyfile", wraps=mixin.shutil.copyfile) as copy:
            self.make_project(window)
            for _ in range(3):
                window.save_work_now()
            self.assertEqual(copy.call_count, 1)
            window.add_paths([self.second])
            self.loaded(window)
            window.save_work_now()
            self.assertEqual(copy.call_count, 1)  # the first file is the same

    def test_missing_files_stay_in_the_project_marked_and_are_kept_on_the_next_save(self):
        gone = self.tmp / "gone.mp4"
        gone.write_bytes(self.video.read_bytes())
        window = self.new_window([self.video, gone])
        gone_settings = window.items[1].settings
        gone_settings.edit.start = 0.5
        project = self.make_project(window)
        gone.unlink()

        other = self.new_window()
        other.open_project(project.id)
        self.assertTrue(pump(30, lambda: all(not i.loading for i in other.items)))
        self.assertEqual(len(other.items), 2)
        self.assertTrue(other.items[1].missing)
        self.assertIn("not found", other.items[1].error)
        self.assertEqual(other.items[1].settings.edit.start, 0.5)
        other.items[0].settings.edit.target_mb = 5.0
        other.save_work_now()
        saved = other.projects.load(project.id)
        self.assertEqual([Path(f.path) for f in saved.files], [self.video, gone])
        self.assertEqual(ClipSettings.from_dict(saved.files[1].settings).edit.start, 0.5)
        # and when the file comes back, it simply loads
        gone.write_bytes(self.video.read_bytes())
        third = self.new_window()
        third.open_project(project.id)
        self.loaded(third)
        self.assertFalse(third.items[1].missing)

    def test_the_card_shows_the_name_the_picture_and_the_date_with_slashes(self):
        window = self.new_window([self.video])
        project = self.make_project(window, "Holiday")
        window.show_home()
        self.assertTrue(pump(10, lambda: not window.home.loading and window.home.project_cards))
        card = window.home.project_cards[0]
        self.assertEqual(card.info.id, project.id)
        self.assertIn("Holiday", card.title.text())
        self.assertIsNotNone(card.info.thumbnail)
        self.assertFalse(card.picture.pixmap().isNull())
        self.assertLessEqual(card.picture.pixmap().width(), 172)
        self.assertRegex(card.date.text(), r"\d\d/\d\d/\d{4}$")
        card.click()
        self.assertFalse(window.at_home)

    def test_many_cards_are_made_a_few_at_a_time_and_hold_only_small_pictures(self):
        window = self.new_window()
        store = window.projects
        for n in range(30):
            store.create(f"Project {n}")
        window.show_home()
        self.assertTrue(window.home.loading)  # the page is up before every card is made
        self.assertTrue(pump(10, lambda: not window.home.loading))
        self.assertEqual(len(window.home.project_cards), 30)
        for card in window.home.project_cards:
            pixmap = card.picture.pixmap()
            self.assertLessEqual(pixmap.width() * pixmap.height(), 172 * 97)

    def test_rename_and_delete_from_the_home_page(self):
        window = self.new_window([self.video])
        project = self.make_project(window, "Old")
        window.show_home()
        with self.name_is("New name"):
            window.rename_project(project.id)
        self.assertEqual(window.projects.load(project.id).name, "New name")
        self.assertEqual(window.project.name, "New name")
        with self.question(True):
            window.delete_project(project.id)
        self.assertEqual(window.projects.list(), [])
        self.assertTrue(self.video.exists())  # the video itself is never touched
        self.assertIsNone(window.project)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class NamesTests(WindowCase):

    def test_a_new_project_with_a_used_name_is_refused_with_a_message(self):
        window = self.new_window(home=True)
        with self.name_is("Trip"):
            window.new_project()
        window.show_home()
        answers = iter([("TRIP", True), ("", True), ("Other", True)])
        with mock.patch.object(QInputDialog, "getText", side_effect=answers), \
                mock.patch.object(QMessageBox, "warning") as warning:
            window.new_project()
        self.assertEqual(warning.call_count, 2)  # the same name in capitals, then an empty one
        self.assertIn("already a project called", warning.call_args_list[0].args[2])
        self.assertEqual(sorted(p.name for p in window.projects.list()), ["Other", "Trip"])

    def test_giving_up_on_the_name_makes_no_project(self):
        window = self.new_window(home=True)
        with mock.patch.object(QInputDialog, "getText", return_value=("", False)):
            window.new_project()
        self.assertEqual(window.projects.list(), [])

    def test_renaming_to_another_projects_name_is_refused(self):
        window = self.new_window(home=True)
        a, b = window.projects.create("Alpha"), window.projects.create("Beta")
        answers = iter([("alpha", True), ("Gamma", True)])
        with mock.patch.object(QInputDialog, "getText", side_effect=answers), \
                mock.patch.object(QMessageBox, "warning") as warning:
            window.rename_project(b.id)
        warning.assert_called_once()
        self.assertEqual(window.projects.load(b.id).name, "Gamma")
        self.assertEqual(window.projects.load(a.id).name, "Alpha")


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class AutosaveTests(WindowCase):

    def test_autosave_says_saving_and_then_saved(self):
        window = self.new_window([self.video])
        self.make_project(window)
        pump(10, lambda: window.save_label.text() == "Saved")
        window.save_label.setText("")
        window.autosave_interval = 0.0  # (otherwise the first save above spaces this one out)
        window.cut_at("split", 1.0)
        self.assertTrue(window._autosave_timer.isActive())
        self.assertTrue(pump(10, lambda: window.save_label.text() == "Saving…"))
        self.assertTrue(pump(10, lambda: window.save_label.text() == "Saved"))
        saved = window.projects.load(window.project.id)
        self.assertTrue(ClipSettings.from_dict(saved.files[0].settings).edit.keep)

    def test_a_manual_save_shows_the_same(self):
        window = self.new_window([self.video])
        self.make_project(window)
        pump(10, lambda: window.save_label.text() == "Saved")
        window.save_label.setText("")
        window.save_action.trigger()
        self.assertEqual(window.save_label.text(), "Saving…")
        self.assertTrue(pump(10, lambda: window.save_label.text() == "Saved"))

    def test_nothing_is_written_while_nothing_changes(self):
        window = self.new_window([self.video])
        self.make_project(window)
        pump(10, lambda: not window._dirty)
        with mock.patch.object(type(window.projects), "save") as save:
            pump(0.5)
            self.assertFalse(window._autosave_timer.isActive())
            save.assert_not_called()

    def test_saves_are_spaced_out(self):
        window = self.new_window([self.video])
        self.make_project(window)
        window.autosave_interval = 600.0
        window.save_work_now()
        window.cut_at("split", 1.0)
        remaining = window._autosave_timer.remainingTime()
        self.assertGreater(remaining, 60 * 1000)  # not for ten minutes: a second change is not
        window.cut_at("split", 2.0)  # ...written at once either
        self.assertTrue(window._autosave_timer.isActive())

    def test_a_failed_save_says_so_and_stays_pending(self):
        window = self.new_window([self.video])
        self.make_project(window)
        with mock.patch.object(type(window.projects), "save", side_effect=OSError("full")):
            self.assertFalse(window.save_work_now())
        self.assertEqual(window.save_label.text(), "Couldn't save")
        self.assertTrue(window._dirty)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class QuitTests(WindowCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"VIDEO_EDITOR_NO_QUIT_PROMPT": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cancel_keeps_the_app_open(self):
        window = self.new_window([self.video])
        with self.click("Cancel"):
            self.assertFalse(window.close())
        self.assertFalse(window.closed)

    def test_closing_the_question_keeps_the_app_open(self):
        window = self.new_window([self.video])
        with mock.patch.object(QMessageBox, "exec", return_value=0), \
                mock.patch.object(QMessageBox, "clickedButton", return_value=None):
            self.assertFalse(window.close())
        self.assertFalse(window.closed)

    def test_discard_quits_and_leaves_nothing_to_recover(self):
        window = self.new_window([self.video])
        window.save_work_now()
        self.assertEqual(len(list(window.projects.folder.glob("recovery-*.json"))), 1)
        with self.click("Discard"):
            self.assertTrue(window.close())
        self.assertTrue(window.closed)
        self.assertEqual(list(window.projects.folder.glob("recovery-*.json")), [])
        self.assertEqual(window.projects.list(), [])

    def test_save_as_a_project_names_it_and_quits(self):
        window = self.new_window([self.video])
        with self.click("Save as a project…"), self.name_is("Kept"):
            self.assertTrue(window.close())
        self.assertEqual([p.name for p in window.projects.list()], ["Kept"])
        self.assertEqual(list(window.projects.folder.glob("recovery-*.json")), [])

    def test_giving_up_on_the_name_keeps_the_app_open(self):
        window = self.new_window([self.video])
        with self.click("Save as a project…"), \
                mock.patch.object(QInputDialog, "getText", return_value=("", False)):
            self.assertFalse(window.close())
        self.assertEqual(window.projects.list(), [])

    def test_a_project_is_just_saved_and_there_is_no_question(self):
        window = self.new_window([self.video])
        self.make_project(window)
        window.cut_at("split", 1.0)
        with mock.patch.object(QMessageBox, "exec", side_effect=AssertionError("asked")):
            self.assertTrue(window.close())
        saved = window.projects.list()[0].id
        self.assertTrue(
            ClipSettings.from_dict(window.projects.load(saved).files[0].settings).edit.keep)

    def test_no_files_no_question(self):
        window = self.new_window()
        with mock.patch.object(QMessageBox, "exec", side_effect=AssertionError("asked")):
            self.assertTrue(window.close())

    def test_leaving_unsaved_work_for_another_project_asks_too(self):
        window = self.new_window([self.video])
        other = window.projects.create("Other")
        with self.click("Cancel"):
            window.open_project(other.id)
        self.assertEqual(len(window.items), 1)
        self.assertIsNone(window.project)
        with self.click("Discard"):
            window.open_project(other.id)
        self.assertEqual(window.project.id, other.id)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class RecoveryTests(WindowCase):

    def test_work_outside_a_project_survives_a_crash_and_is_offered_first(self):
        crashed = self.new_window([self.video, self.second])
        crashed.items[0].settings.edit.target_mb = 6.0
        self.assertTrue(pump(10, lambda: crashed.projects.recoveries("x")))  # written by autosave
        crashed.save_work_now()  # (the window is simply abandoned from here: no clean exit)

        fresh = self.new_window(home=True)
        offered = fresh.projects.recoveries(fresh.session)
        self.assertEqual(len(offered), 1)
        self.assertEqual(offered[0].file_count, 2)
        row = fresh.home.recovery_layout.itemAt(0).widget()
        self.assertIn("Recover the work from", row.label.text())
        self.assertRegex(row.label.text(), r"\d\d/\d\d/\d{4} \d\d:\d\d")
        self.assertFalse(fresh.home.recovery_box.isHidden())

        row.recover_button.click()
        self.loaded(fresh)
        self.assertEqual([i.path for i in fresh.items], [self.video, self.second])
        self.assertEqual(fresh.items[0].settings.edit.target_mb, 6.0)
        self.assertIsNone(fresh.project)
        self.assertFalse(fresh.at_home)
        # this run holds its own copy now, and the old one is gone: nothing is offered twice
        left = list(fresh.projects.folder.glob("recovery-*.json"))
        self.assertEqual([p.name for p in left], [f"recovery-{fresh.session}.json"])

    def test_a_clean_exit_removes_the_recovery_copy(self):
        window = self.new_window([self.video])
        window.save_work_now()
        self.assertEqual(len(list(window.projects.folder.glob("recovery-*.json"))), 1)
        window.close()
        self.assertEqual(list(window.projects.folder.glob("recovery-*.json")), [])

    def test_a_project_is_not_written_as_recovery(self):
        window = self.new_window([self.video])
        self.make_project(window)
        window.cut_at("split", 1.0)
        window.save_work_now()
        self.assertEqual(list(window.projects.folder.glob("recovery-*.json")), [])

    def test_removing_every_file_removes_the_recovery_copy(self):
        window = self.new_window([self.video])
        window.save_work_now()
        window.remove_selected()
        self.assertTrue(pump(10, lambda: not list(window.projects.folder.glob("recovery-*.json"))))

    def test_discarding_recovered_work_asks_first(self):
        crashed = self.new_window([self.video])
        crashed.save_work_now()
        fresh = self.new_window(home=True)
        row = fresh.home.recovery_layout.itemAt(0).widget()
        with self.question(False):
            row.discard_button.click()
        self.assertEqual(len(fresh.projects.recoveries(fresh.session)), 1)
        with self.question(True):
            row.discard_button.click()
        self.assertEqual(fresh.projects.recoveries(fresh.session), [])


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class LanguageRestartTests(WindowCase):

    def test_the_restart_arguments_reopen_the_project_or_the_work(self):
        from video_converter.ui import app as app_module
        from video_converter.ui import language

        _, args, _ = language.restart_command(["a.mp4"], {"project": "abc"})
        self.assertEqual(args[-3:], ["--project", "abc", "a.mp4"])
        _, args, _ = language.restart_command([], {"work": "C:/p/recovery-1.json"})
        self.assertEqual(args[-2:], ["--work", "C:/p/recovery-1.json"])
        _, args, _ = language.restart_command(["a.mp4"])
        self.assertEqual(args[-1], "a.mp4")
        self.assertEqual(
            app_module.split_arguments(["--project", "abc", "a.mp4"]),
            ({
                "project": "abc"
            }, ["a.mp4"]))
        self.assertEqual(app_module.split_arguments(["a.mp4", "b.mp4"]), ({}, ["a.mp4", "b.mp4"]))

    def test_a_restart_in_a_project_reopens_the_same_project_with_its_edits(self):
        from video_converter.ui import language

        window = self.new_window([self.video])
        project = self.make_project(window)
        window.items[0].settings.edit.target_mb = 4.0  # not saved yet when the restart starts
        with mock.patch.object(language, "restart", return_value=True) as restart, \
                mock.patch.object(type(window), "close", return_value=True):
            window.restart_app()
        restart.assert_called_once_with([str(self.video)], resume={"project": project.id})
        self.assertEqual(
            ClipSettings.from_dict(window.projects.load(
                project.id).files[0].settings).edit.target_mb, 4.0)

        reopened = self.new_window()
        reopened.start_up({"project": project.id}, [])
        self.loaded(reopened)
        self.assertEqual(reopened.project.id, project.id)
        self.assertEqual(reopened.items[0].settings.edit.target_mb, 4.0)
        self.assertFalse(reopened.at_home)

    def test_a_restart_outside_a_project_reopens_the_same_work(self):
        from video_converter.ui import language

        window = self.new_window([self.video])
        window.items[0].settings.edit.target_mb = 3.0
        with mock.patch.object(language, "restart", return_value=True) as restart, \
                mock.patch.object(type(window), "close", return_value=True):
            window.restart_app()
        resume = restart.call_args.kwargs["resume"]
        self.assertTrue(Path(resume["work"]).is_file())

        reopened = self.new_window()
        reopened.start_up(resume and {"work": resume["work"]}, [])
        self.loaded(reopened)
        self.assertEqual(reopened.items[0].settings.edit.target_mb, 3.0)
        self.assertIsNone(reopened.project)
        self.assertFalse(Path(resume["work"]).exists())  # taken over, not offered again later

    def test_a_restart_that_cannot_start_leaves_the_work_alone(self):
        from video_converter.ui import language

        window = self.new_window([self.video])
        self.make_project(window)
        with mock.patch.object(language, "restart", return_value=False), \
                mock.patch.object(QMessageBox, "warning"):
            window.restart_app()
        self.assertFalse(window._work_handed_over)
        window.cut_at("split", 1.0)
        self.assertTrue(window._autosave_timer.isActive())

    def test_a_project_that_is_gone_shows_the_home_page(self):
        window = self.new_window()
        with mock.patch.object(QMessageBox, "warning") as warning:
            window.start_up({"project": "nothere"}, [])
        warning.assert_called_once()
        self.assertTrue(window.at_home)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class ProjectArabicTests(WindowCase):

    def use_arabic(self):
        from video_converter.ui import language

        i18n.set_language("ar")
        language.apply_to_app(app)

        def restore():
            i18n.set_language("en")
            language.apply_to_app(app)

        self.addCleanup(restore)

    def test_cards_and_messages_are_arabic_and_right_to_left(self):
        self.use_arabic()
        window = self.new_window([self.video])
        self.make_project(window, "رحلة")
        window.show_home()
        self.assertTrue(pump(10, lambda: not window.home.loading and window.home.project_cards))
        card = window.home.project_cards[0]
        self.assertEqual(card.layoutDirection(), Qt.LayoutDirection.RightToLeft)
        self.assertIn("حُفظ", card.date.text())
        self.assertEqual(re.findall(r"[\u0660-\u0669]", card.date.text()), [])  # Western digits
        self.assertEqual(card.rename_button.text(), "إعادة تسمية")
        window.save_label.setText("")
        window.show_editor()
        window.save_action.trigger()
        self.assertEqual(window.save_label.text(), "جارٍ الحفظ…")
        self.assertTrue(pump(10, lambda: window.save_label.text() == "تم الحفظ"))


if __name__ == "__main__":
    unittest.main()
