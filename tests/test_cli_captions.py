"""The command line's automatic captions: --auto-captions, --captions-only and the --caption-* options.
Listening and exporting are faked; what is checked is what the command line passes on and writes."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_cli import run
from video_converter import __main__ as cli
from video_converter.autocaptions import CaptionResult
from video_converter.captions import Cue, parse_cues
from video_converter.core import ConversionError, MediaInfo

INFO = MediaInfo(duration=10.0, width=320, height=180, has_audio=True, fps=30.0)
CUES = [Cue(1.0, 2.5, "Hello there"), Cue(3.0, 4.0, "مرحبا")]


class CaptionCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.video = self.tmp / "clip.mp4"
        self.video.write_bytes(b"not really a video")
        self.info = INFO
        self.result = CaptionResult(list(CUES), "en")
        self.listened, self.exported = [], []

        def listen(ffmpeg, source, options, duration=None, **kwargs):
            self.listened.append((source, options, duration))
            kwargs["on_progress"]("listen", 0.5, "")
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        def export(ffmpeg, job, **kwargs):
            self.exported.append(job)
            return job.output

        for patcher in (mock.patch.object(cli, "find_ffmpeg", return_value="ffmpeg"),
                        mock.patch.object(cli, "probe_media", side_effect=lambda *a: self.info),
                        mock.patch.object(cli, "make_captions", side_effect=listen),
                        mock.patch.object(cli, "convert_video", side_effect=export),
                        mock.patch.object(cli, "choose_encoder", return_value=None),
                        mock.patch.object(cli, "is_downloaded", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    # ----- --captions-only ------------------------------------------------------------

    def test_captions_only_writes_the_srt_next_to_the_video_and_exports_nothing(self):
        code, out, err = run(self.video, "--captions-only")
        self.assertEqual(code, 0, err)
        srt = self.tmp / "clip.srt"
        self.assertTrue(srt.is_file())
        self.assertEqual([(c.start, c.end, c.text) for c in parse_cues(srt.read_text("utf-8"))],
                         [(c.start, c.end, c.text) for c in CUES])
        self.assertIn("clip.srt", out)
        self.assertEqual(self.exported, [])

    def test_the_options_reach_the_listener(self):
        code, _, err = run(self.video, "--captions-only", "--caption-language", "ar", "--caption-model", "accurate",
                           "--translate-captions", "--start", "2", "--end", "8")
        self.assertEqual(code, 0, err)
        source, options, duration = self.listened[0]
        self.assertEqual((source, duration), (self.video, 10.0))
        self.assertEqual((options.model, options.language, options.translate, options.start, options.end),
                         ("accurate", "ar", True, 2.0, 8.0))

    def test_the_defaults_are_the_balanced_model_and_automatic_language(self):
        run(self.video, "--captions-only")
        options = self.listened[0][1]
        self.assertEqual((options.model, options.language, options.translate), ("balanced", "", False))

    def test_the_language_can_be_auto_or_upper_case(self):
        run(self.video, "--captions-only", "--caption-language", "auto")
        run(self.video, "--captions-only", "--caption-language", "FR")
        self.assertEqual([c[1].language for c in self.listened], ["", "fr"])

    def test_an_existing_srt_is_not_replaced_unless_asked(self):
        (self.tmp / "clip.srt").write_text("old", encoding="utf-8")
        run(self.video, "--captions-only")
        self.assertEqual((self.tmp / "clip.srt").read_text(encoding="utf-8"), "old")
        self.assertTrue((self.tmp / "clip (1).srt").is_file())
        run(self.video, "--captions-only", "--overwrite")
        self.assertNotEqual((self.tmp / "clip.srt").read_text(encoding="utf-8"), "old")

    def test_the_output_folder_gets_the_srt(self):
        out_dir = self.tmp / "out"
        code, _, _ = run(self.video, "--captions-only", "-o", out_dir)
        self.assertEqual(code, 0)
        self.assertTrue((out_dir / "clip.srt").is_file())

    def test_several_files_each_get_their_own_srt(self):
        other = self.tmp / "other.mp4"
        other.write_bytes(b"x")
        code, _, _ = run(self.video, other, "--captions-only")
        self.assertEqual(code, 0)
        self.assertTrue((self.tmp / "clip.srt").is_file() and (self.tmp / "other.srt").is_file())

    def test_no_speech_is_a_failure_with_a_message_and_no_file(self):
        self.result = CaptionResult([], "en")
        code, _, err = run(self.video, "--captions-only")
        self.assertEqual(code, 1)
        self.assertIn("no speech", err)
        self.assertFalse((self.tmp / "clip.srt").exists())

    def test_a_video_without_sound_is_refused_before_listening(self):
        self.info = MediaInfo(duration=10.0, width=320, height=180, has_audio=False)
        code, _, err = run(self.video, "--captions-only")
        self.assertEqual(code, 1)
        self.assertIn("no sound", err)
        self.assertEqual(self.listened, [])

    def test_an_error_from_the_listener_is_shown_and_fails_the_run(self):
        self.result = ConversionError("Couldn't download the speech model. Check that this PC is connected.")
        code, _, err = run(self.video, "--captions-only")
        self.assertEqual(code, 1)
        self.assertIn("FAILED - Couldn't download the speech model", err)

    def test_a_missing_file_fails_without_listening(self):
        code, _, err = run(self.tmp / "nope.mp4", "--captions-only")
        self.assertEqual(code, 1)
        self.assertIn("File not found", err)
        self.assertEqual(self.listened, [])

    def test_a_model_that_is_not_downloaded_yet_is_announced(self):
        with mock.patch.object(cli, "is_downloaded", return_value=False):
            _, _, err = run(self.video, "--captions-only", "--caption-model", "fast")
        self.assertIn("downloading the Fast speech model (75 MB)", err)

    # ----- --auto-captions with a video export ----------------------------------------------

    def test_auto_captions_with_video_burns_the_new_srt_into_the_export(self):
        code, _, err = run(self.video, "--video", "original", "--auto-captions")
        self.assertEqual(code, 0, err)
        job = self.exported[0]
        self.assertEqual(Path(job.edit.subtitles.file), self.tmp / "clip.srt")
        self.assertTrue((self.tmp / "clip.srt").is_file())

    def test_the_subtitle_look_options_apply_to_the_automatic_captions(self):
        run(self.video, "--video", "original", "--auto-captions", "--subtitle-size", "0.08",
            "--subtitle-position", "top", "--subtitle-color", "yellow")
        sub = self.exported[0].edit.subtitles
        self.assertEqual((sub.size, sub.position, sub.color), (0.08, "top", "#ffe600"))

    def test_cues_stay_in_source_time_when_the_export_is_cut(self):
        run(self.video, "--video", "original", "--auto-captions", "--cut", "0:03-0:05")
        job = self.exported[0]
        self.assertEqual(Path(job.edit.subtitles.file), self.tmp / "clip.srt")
        self.assertTrue(job.edit.keep)                      # the cut is in the edit; retiming handles the rest
        self.assertEqual(parse_cues((self.tmp / "clip.srt").read_text("utf-8"))[1].start, 3.0)

    def test_without_speech_the_video_is_still_exported_without_captions(self):
        self.result = CaptionResult([], "en")
        code, _, err = run(self.video, "--video", "original", "--auto-captions")
        self.assertEqual(code, 0)
        self.assertEqual(self.exported[0].edit.subtitles.file, "")
        self.assertIn("without captions", err)

    def test_a_failed_listen_does_not_export(self):
        self.result = ConversionError("boom")
        code, _, _ = run(self.video, "--video", "original", "--auto-captions")
        self.assertEqual(code, 1)
        self.assertEqual(self.exported, [])

    # ----- checking the options ---------------------------------------------------------------

    def test_auto_captions_alone_asks_for_a_video_or_captions_only(self):
        code, _, err = run(self.video, "--auto-captions")
        self.assertEqual(code, 2)
        self.assertIn("--video", err)
        self.assertIn("--captions-only", err)

    def test_the_caption_options_need_auto_captions(self):
        for args in (("--caption-language", "ar"), ("--caption-model", "fast"), ("--translate-captions",)):
            with self.subTest(args=args):
                code, _, err = run(self.video, "--video", "original", *args)
                self.assertEqual(code, 2)
                self.assertIn("need --auto-captions", err)

    def test_an_unknown_language_or_model_is_refused(self):
        code, _, err = run(self.video, "--captions-only", "--caption-language", "xx")
        self.assertEqual(code, 2)
        self.assertIn("not a language code", err)
        code, _, err = run(self.video, "--captions-only", "--caption-model", "huge")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_it_cannot_be_combined_with_a_subtitle_file_or_join_or_a_frame(self):
        for args, word in (((str(self.video), "--video", "original", "--auto-captions", "--subtitles", "a.srt"),
                            "--subtitles"),
                           ((str(self.video), str(self.video), "--video", "original", "--auto-captions", "--join"),
                            "--join"),
                           ((str(self.video), "--auto-captions", "--frame-at", "1", "--frame-out", "f.png"),
                            "--frame-at")):
            with self.subTest(word=word):
                code, _, err = run(*args)
                self.assertEqual(code, 2)
                self.assertIn(word, err)

    def test_captions_only_cannot_also_export_a_video(self):
        code, _, err = run(self.video, "--captions-only", "--video", "phone")
        self.assertEqual(code, 2)
        self.assertIn("only writes the .srt", err)

    def test_the_subtitle_look_options_are_fine_with_auto_captions_and_still_refused_without_either(self):
        code, _, _ = run(self.video, "--video", "original", "--auto-captions", "--subtitle-size", "0.07")
        self.assertEqual(code, 0)
        code, _, err = run(self.video, "--video", "original", "--subtitle-size", "0.07")
        self.assertEqual(code, 2)
        self.assertIn("need --subtitles", err)


if __name__ == "__main__":
    unittest.main()
