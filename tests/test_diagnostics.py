"""The log file, crash handling, "Report a problem" and About (2.4). Logs go to temp folders here."""

import logging
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from video_converter import APP_NAME, REPO_URL, __version__, diagnostics


class LoggingSetupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "logs"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(diagnostics.shutdown_logging)      # release the file before the folder goes

    def test_the_log_is_written_to_app_log_in_the_folder(self):
        path = diagnostics.setup_logging(self.folder)
        self.assertEqual(path, self.folder / "app.log")
        logging.getLogger("video_converter.jobs").info("Export 7 started: clip.mp4")
        diagnostics.shutdown_logging()
        text = path.read_text(encoding="utf-8")
        self.assertIn("Export 7 started: clip.mp4", text)
        self.assertIn("INFO", text)
        self.assertIn("video_converter.jobs", text)

    def test_the_folder_is_made_when_missing(self):
        self.assertFalse(self.folder.exists())
        diagnostics.setup_logging(self.folder)
        self.assertTrue(self.folder.is_dir())

    def test_log_path_follows_the_running_log(self):
        path = diagnostics.setup_logging(self.folder)
        self.assertEqual(diagnostics.log_path(), path)

    def test_setting_it_up_twice_does_not_write_lines_twice(self):
        diagnostics.setup_logging(self.folder / "a")
        path = diagnostics.setup_logging(self.folder / "b")
        logging.getLogger("video_converter").info("only once")
        diagnostics.shutdown_logging()
        self.assertEqual(path.read_text(encoding="utf-8").count("only once"), 1)
        self.assertFalse((self.folder / "a" / "app.log").exists() and "only once" in
                         (self.folder / "a" / "app.log").read_text(encoding="utf-8"))

    def test_the_log_rotates_instead_of_growing_forever(self):
        with mock.patch.object(diagnostics, "MAX_LOG_BYTES", 2000):
            path = diagnostics.setup_logging(self.folder)
            for i in range(100):
                logging.getLogger("video_converter").info("line %03d %s", i, "x" * 50)
            diagnostics.shutdown_logging()
        self.assertTrue((self.folder / "app.log.1").exists())
        self.assertLessEqual(path.stat().st_size, 2500)
        self.assertLessEqual(len(list(self.folder.iterdir())), diagnostics.LOG_FILES_KEPT + 1)

    def test_non_ascii_file_names_are_logged_fine(self):
        path = diagnostics.setup_logging(self.folder)
        logging.getLogger("video_converter").info("Opened C:\\Users\\Zoë\\Videos\\vidéo 日本.mp4")
        diagnostics.shutdown_logging()
        self.assertIn("vidéo 日本.mp4", path.read_text(encoding="utf-8"))

    def test_without_localappdata_the_log_goes_to_the_temp_folder(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCALAPPDATA", None)
            with mock.patch("tempfile.gettempdir", return_value=self._tmp.name):
                folder = diagnostics.log_folder()
        self.assertEqual(folder.parent, Path(self._tmp.name))
        self.assertTrue(folder.is_dir())

    def test_an_unusable_localappdata_falls_back_to_the_temp_folder(self):
        blocker = Path(self._tmp.name) / "file-not-folder"
        blocker.write_text("x")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(blocker)}):
            with mock.patch("tempfile.gettempdir", return_value=self._tmp.name):
                folder = diagnostics.log_folder()
        self.assertEqual(folder.parent, Path(self._tmp.name))

    def test_the_default_place_is_under_localappdata(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmp.name}):
            folder = diagnostics.log_folder()
        self.assertEqual(folder, Path(self._tmp.name) / APP_NAME / "logs")

    def test_recent_lines_are_the_last_ones_in_order(self):
        path = diagnostics.setup_logging(self.folder)
        for i in range(50):
            logging.getLogger("video_converter").info("entry %02d", i)
        diagnostics.shutdown_logging()
        lines = diagnostics.recent_log_lines(30, path)
        self.assertEqual(len(lines), 30)
        self.assertTrue(lines[0].endswith("entry 20"))
        self.assertTrue(lines[-1].endswith("entry 49"))

    def test_recent_lines_of_a_missing_log_is_empty(self):
        self.assertEqual(diagnostics.recent_log_lines(30, self.folder / "nope.log"), [])

    def test_startup_notes_version_windows_and_ffmpeg(self):
        path = diagnostics.setup_logging(self.folder)
        with mock.patch.object(diagnostics, "ffmpeg_version", return_value="7.1-test"):
            diagnostics.log_startup("C:/tools/ffmpeg.exe")
        diagnostics.shutdown_logging()
        text = path.read_text(encoding="utf-8")
        self.assertIn(f"{APP_NAME} {__version__} started", text)
        self.assertIn(diagnostics.windows_version(), text)
        self.assertIn("C:/tools/ffmpeg.exe", text)
        self.assertIn("7.1-test", text)

    def test_the_hardware_search_result_is_logged(self):
        from video_converter import gpu
        path = diagnostics.setup_logging(self.folder)
        gpu.forget_detection()
        self.addCleanup(gpu.forget_detection)
        with mock.patch.object(gpu, "_encoder_works", side_effect=lambda ff, name: name == "h264_nvenc"):
            gpu.detect_encoders("fake-ffmpeg")
        diagnostics.shutdown_logging()
        self.assertIn("Hardware encoders that work: h264_nvenc", path.read_text(encoding="utf-8"))


class FfmpegLoggingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(diagnostics.shutdown_logging)
        self.path = diagnostics.setup_logging(Path(self._tmp.name))

    def test_a_failed_export_logs_the_command_and_the_end_of_ffmpegs_output(self):
        from video_converter.core import ConversionError, find_ffmpeg, run_ffmpeg
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.skipTest("ffmpeg is not installed")
        missing = Path(self._tmp.name) / "missing.mp4"
        out = Path(self._tmp.name) / "out.mp4"
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(missing),
               "-progress", "pipe:1", "-nostats", str(out)]
        with self.assertRaises(ConversionError):
            run_ffmpeg(cmd, out, None)
        diagnostics.shutdown_logging()
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("ffmpeg:", text)
        self.assertIn("missing.mp4", text)
        self.assertIn("ERROR", text)
        self.assertIn("No such file", text)

    def test_the_queue_logs_each_export_start_finish_and_failure(self):
        from video_converter.core import ConversionError
        from video_converter.jobs import Job, JobQueue
        from video_converter.project import ClipSettings

        def runner(ffmpeg, job, progress):
            if job.source.name == "bad.mp4":
                raise ConversionError("boom: the encoder fell over")
            return job.source.with_suffix(".out.mp4")

        queue = JobQueue("ffmpeg", runner=runner)
        queue.add(Job(Path("good.mp4"), ClipSettings()))
        queue.add(Job(Path("bad.mp4"), ClipSettings()))
        self.assertTrue(queue.wait(10))
        queue.shutdown()
        diagnostics.shutdown_logging()
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("started: "), 2)
        self.assertIn("finished in", text)
        self.assertIn("good.out.mp4", text)
        self.assertIn("failed after", text)
        self.assertIn("boom: the encoder fell over", text)


class ExceptionHookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(diagnostics.shutdown_logging)
        self.path = diagnostics.setup_logging(Path(self._tmp.name))
        self.seen = []
        self.restore = diagnostics.install_excepthooks(self.seen.append)
        self.addCleanup(self.restore)

    def raised(self):
        try:
            raise ValueError("something broke")
        except ValueError:
            return sys.exc_info()

    def log_text(self):
        diagnostics.shutdown_logging()
        return self.path.read_text(encoding="utf-8")

    def test_an_uncaught_error_is_logged_with_its_traceback_and_reported(self):
        sys.excepthook(*self.raised())
        text = self.log_text()
        self.assertIn("CRITICAL", text)
        self.assertIn("Traceback", text)
        self.assertIn("ValueError: something broke", text)
        self.assertEqual(self.seen, ["ValueError: something broke"])

    def test_an_error_in_another_thread_is_logged_too(self):
        def work():
            raise RuntimeError("worker died")

        thread = threading.Thread(target=work, name="test-worker")
        thread.start()
        thread.join()
        text = self.log_text()
        self.assertIn("in thread test-worker", text)
        self.assertIn("RuntimeError: worker died", text)
        self.assertEqual(self.seen, ["RuntimeError: worker died"])

    def test_leaving_a_thread_on_purpose_is_not_a_crash(self):
        thread = threading.Thread(target=lambda: sys.exit(0))
        thread.start()
        thread.join()
        self.assertEqual(self.seen, [])

    def test_a_broken_listener_cannot_stop_the_logging(self):
        self.restore()
        self.restore = diagnostics.install_excepthooks(mock.Mock(side_effect=RuntimeError("dialog failed")))
        sys.excepthook(*self.raised())
        self.assertIn("something broke", self.log_text())

    def test_ctrl_c_still_goes_to_the_normal_handler(self):
        self.restore()
        with mock.patch.object(sys, "excepthook") as old:
            restore = diagnostics.install_excepthooks(self.seen.append)
            try:
                sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
            finally:
                restore()
        old.assert_called_once()
        self.assertEqual(self.seen, [])
        self.restore = lambda: None

    def test_restoring_puts_the_old_hooks_back(self):
        hooked = sys.excepthook
        self.restore()
        self.assertIsNot(sys.excepthook, hooked)
        self.restore = lambda: None


class IssueUrlTests(unittest.TestCase):
    SYSTEM = [f"{APP_NAME} {__version__}", "Windows 11 (build 26300)", "ffmpeg 7.1", "Graphics card encoders: h264_nvenc"]

    def fields(self, url):
        return {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}

    def test_it_points_at_the_new_issue_page_of_the_repository(self):
        url = diagnostics.issue_url(self.SYSTEM, ["a line"])
        self.assertTrue(url.startswith(f"{REPO_URL}/issues/new?"))
        self.assertEqual(REPO_URL, "https://github.com/Ahmed41022/free-video-editor")

    def test_the_title_and_body_carry_the_details(self):
        fields = self.fields(diagnostics.issue_url(self.SYSTEM, ["12:00 INFO started", "12:01 ERROR it broke"]))
        self.assertIn(__version__, fields["title"])
        body = fields["body"]
        for line in self.SYSTEM:
            self.assertIn(line, body)
        self.assertIn("12:01 ERROR it broke", body)
        self.assertIn("What happened?", body)

    def test_special_characters_are_encoded_and_decode_back(self):
        lines = ["C:\\Users\\Zoë\\vidéo & more #1 100%.mp4", "a=b?c&d", "line with ``` fence", "日本語"]
        url = diagnostics.issue_url(self.SYSTEM, lines)
        self.assertNotIn(" ", url)
        self.assertNotIn("\n", url)
        self.assertEqual(url.count("?"), 1)
        self.assertEqual(url.count("&"), 1)            # only between title and body
        body = self.fields(url)["body"]
        self.assertIn("C:\\Users\\Zoë\\vidéo & more #1 100%.mp4", body)
        self.assertIn("a=b?c&d", body)
        self.assertIn("日本語", body)
        self.assertEqual(body.count("```"), 2)         # only the report's own fence survives

    def test_a_long_log_is_cut_from_the_old_end_to_stay_under_the_limit(self):
        lines = [f"2026-10-18 12:00:{i % 60:02d} INFO video_converter: entry {i:03d} " + "x" * 120 for i in range(30)]
        url = diagnostics.issue_url(self.SYSTEM, lines)
        self.assertLessEqual(len(url), diagnostics.URL_LIMIT)
        body = self.fields(url)["body"]
        self.assertIn("entry 029", body)             # the newest line is the one that matters
        self.assertNotIn("entry 000", body)

    def test_the_limit_holds_for_awkward_text_that_grows_when_encoded(self):
        lines = ["日本語のファイル名 " * 20 + f"#{i}" for i in range(30)]
        self.assertLessEqual(len(diagnostics.issue_url(self.SYSTEM, lines)), diagnostics.URL_LIMIT)
        self.assertLessEqual(len(diagnostics.issue_url(self.SYSTEM, lines, limit=2500)), 2500)

    def test_very_long_single_lines_are_shortened(self):
        body = self.fields(diagnostics.issue_url(self.SYSTEM, ["y" * 5000]))["body"]
        self.assertLess(len(body), 1500)

    def test_a_short_log_is_kept_whole(self):
        lines = [f"line {i}" for i in range(5)]
        body = self.fields(diagnostics.issue_url(self.SYSTEM, lines))["body"]
        for line in lines:
            self.assertIn(line, body)

    def test_an_empty_log_still_makes_a_report(self):
        url = diagnostics.issue_url(self.SYSTEM, [])
        self.assertIn("the log is empty", self.fields(url)["body"])


class AboutTests(unittest.TestCase):
    def test_it_names_the_version_license_repository_and_credits(self):
        text = diagnostics.about_text()
        self.assertIn(__version__, text)
        self.assertIn("MIT", text)
        self.assertIn(REPO_URL, text)
        self.assertIn("FFmpeg", text)
        self.assertIn("Qt", text)

    def test_the_version_is_2_4_0(self):
        self.assertEqual(__version__, "2.9.0")


class SystemInfoTests(unittest.TestCase):
    def test_the_lines_say_which_encoders_were_found(self):
        with mock.patch.object(diagnostics, "ffmpeg_version", return_value="7.1"):
            found = diagnostics.system_lines("ffmpeg", ("h264_nvenc", "h264_qsv"))
            searching = diagnostics.system_lines("ffmpeg", None)
            none = diagnostics.system_lines(None, ())
        self.assertIn("Graphics card encoders: h264_nvenc, h264_qsv", found)
        self.assertIn("Graphics card encoders: still being checked", searching)
        self.assertIn("Graphics card encoders: none", none)
        self.assertIn("ffmpeg 7.1", found)
        self.assertIn("ffmpeg not found", none)
        self.assertEqual(found[0], f"{APP_NAME} {__version__}")

    def test_ffmpeg_that_cannot_run_is_unknown(self):
        self.assertEqual(diagnostics.ffmpeg_version("no-such-program-here"), "unknown")


if __name__ == "__main__":
    unittest.main()
