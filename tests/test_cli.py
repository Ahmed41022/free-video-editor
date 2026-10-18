"""The command line: checked options and the memory warning for reverse (2.3.1)."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from video_converter import __main__ as cli
from video_converter.core import MediaInfo

BIG = MediaInfo(duration=600.0, width=1920, height=1080, has_audio=True, fps=30.0)     # about 50 GB reversed
SMALL = MediaInfo(duration=2.0, width=320, height=180, has_audio=True, fps=15.0)


def run(*args):
    """Run the CLI in this process: (exit code, stdout, stderr). Option errors raise SystemExit(2)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.run_cli([str(a) for a in args])
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue(), err.getvalue()


class OptionCheckTests(unittest.TestCase):
    def test_zoom_outside_1_to_3_is_refused_with_a_message(self):
        for value in ("0.5", "3.1", "10", "-1", "nan"):
            with self.subTest(zoom=value):
                code, _, err = run("clip.mp4", "--video", "phone", "--zoom", value)
                self.assertEqual(code, 2)
                self.assertIn("--zoom must be between 1 and 3", err)

    def test_zoom_that_is_not_a_number_is_refused(self):
        code, _, err = run("clip.mp4", "--video", "phone", "--zoom", "tight")
        self.assertEqual(code, 2)
        self.assertIn("not a number", err)

    def test_position_outside_0_to_1_is_refused_with_a_message(self):
        for values in (("1.5", "0.5"), ("0.5", "-0.1"), ("2", "2")):
            with self.subTest(position=values):
                code, _, err = run("clip.mp4", "--video", "phone", "--position", *values)
                self.assertEqual(code, 2)
                self.assertIn("--position must be between 0 and 1", err)

    def test_the_limits_themselves_are_fine(self):
        # Parsing gets past the options and stops at the missing ffmpeg instead.
        with mock.patch.object(cli, "find_ffmpeg", return_value=None):
            for args in (("--zoom", "1"), ("--zoom", "3.0"), ("--position", "0", "1"), ("--position", "1", "0")):
                with self.subTest(args=args):
                    code, _, err = run("clip.mp4", "--video", "phone", *args)
                    self.assertEqual(code, 2)
                    self.assertIn("ffmpeg not found", err)


class ReverseWarningTests(unittest.TestCase):
    def run_with(self, info, *args, join=False):
        """Run the CLI on clip.mp4 (and b.mp4) with probing and exporting faked."""
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for name in ("clip.mp4", "b.mp4") if join else ("clip.mp4",):
                (Path(tmp) / name).write_bytes(b"x")
                files.append(Path(tmp) / name)
            with mock.patch.object(cli, "find_ffmpeg", return_value="ffmpeg"), \
                    mock.patch.object(cli, "probe_media", return_value=info), \
                    mock.patch.object(cli, "convert_video", side_effect=lambda f, job, **kw: job.output) as export, \
                    mock.patch.object(cli, "convert", side_effect=lambda f, job, **kw: job.output) as mp3, \
                    mock.patch.object(cli, "join_clips", side_effect=lambda f, job, **kw: job.output) as joined:
                code, out, err = run(*files, *args, *(["--join"] if join else []))
            return code, err, export, mp3, joined

    def test_a_big_reverse_warns_on_stderr_and_still_exports(self):
        code, err, export, _, _ = self.run_with(BIG, "--video", "original", "--reverse")
        self.assertEqual(code, 0)
        self.assertIn("clip.mp4: warning - Reversing this needs about", err)
        self.assertIn("GB of memory", err)
        export.assert_called_once()

    def test_a_small_reverse_or_no_reverse_says_nothing(self):
        for info, args in ((SMALL, ("--video", "original", "--reverse")), (BIG, ("--video", "original"))):
            with self.subTest(args=args):
                code, err, export, _, _ = self.run_with(info, *args)
                self.assertEqual((code, err), (0, ""))
                export.assert_called_once()

    def test_an_mp3_reverse_warns_about_the_sound_only_when_it_is_big(self):
        # 10 hours of stereo floats is about 13 GB; ten minutes is not worth mentioning.
        long = MediaInfo(duration=36000.0, has_audio=True)
        code, err, _, mp3, _ = self.run_with(long, "--reverse")
        self.assertEqual(code, 0)
        self.assertIn("warning", err)
        mp3.assert_called_once()
        self.assertEqual(self.run_with(MediaInfo(duration=600.0, has_audio=True), "--reverse")[1], "")

    def test_a_join_warns_for_the_clip_that_is_too_big(self):
        code, err, _, _, joined = self.run_with(BIG, "--video", "original", "--reverse", join=True)
        self.assertEqual(code, 0)
        self.assertEqual(err.count("warning"), 2)
        joined.assert_called_once()


if __name__ == "__main__":
    unittest.main()
