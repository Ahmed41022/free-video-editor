"""Exporting with the graphics card (2.4): finding encoders that work, their options, and the fallback."""

import io
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from video_converter import __main__ as cli
from video_converter import gpu
from video_converter.core import ConversionCancelled, ConversionError, MediaInfo, find_ffmpeg, probe_media
from video_converter.jobs import Job, run_job
from video_converter.project import ClipSettings
from video_converter.video import (
    JoinJob, JoinPart, VideoEdit, VideoJob, convert_video, join_clips, join_command, video_codec_args, video_command,
)

FFMPEG = find_ffmpeg()
INFO = MediaInfo(duration=4.0, width=640, height=360, has_audio=True, fps=15.0)
BOGUS = {"h264_bogus": lambda crf, fps: ["-c:v", "h264_does_not_exist", "-pix_fmt", "yuv420p"]}


def make_video(path, width=320, height=180, seconds=2):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={width}x{height}:rate=15",
                    "-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)


def after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class EncoderOptionTests(unittest.TestCase):
    def test_nvenc_uses_constant_quality(self):
        args = gpu.encoder_args("h264_nvenc", 23, 30.0)
        self.assertEqual(args[:2], ["-c:v", "h264_nvenc"])
        self.assertEqual(after(args, "-rc"), "vbr")
        self.assertEqual(after(args, "-b:v"), "0")
        self.assertEqual(after(args, "-preset"), "p5")
        self.assertEqual(after(args, "-pix_fmt"), "yuv420p")
        self.assertEqual(int(after(args, "-cq")), 23 + 8)

    def test_intel_uses_global_quality_and_says_the_frame_rate(self):
        args = gpu.encoder_args("h264_qsv", 23, 29.97)
        self.assertEqual(args[:2], ["-c:v", "h264_qsv"])
        self.assertEqual(after(args, "-global_quality"), "23")
        self.assertEqual(after(args, "-r"), "29.97")

    def test_amd_uses_a_fixed_quantizer(self):
        args = gpu.encoder_args("h264_amf", 23, 30.0)
        self.assertEqual(args[:2], ["-c:v", "h264_amf"])
        self.assertEqual(after(args, "-rc"), "cqp")
        self.assertEqual(after(args, "-qp_i"), after(args, "-qp_p"))
        self.assertEqual(after(args, "-pix_fmt"), "yuv420p")

    def test_better_quality_always_means_a_lower_number(self):
        for encoder, flag in (("h264_nvenc", "-cq"), ("h264_qsv", "-global_quality"), ("h264_amf", "-qp_i")):
            with self.subTest(encoder=encoder):
                numbers = [int(after(gpu.encoder_args(encoder, crf, 30.0), flag)) for crf in (18, 23, 28)]
                self.assertEqual(numbers, sorted(numbers))
                self.assertEqual(len(set(numbers)), 3)

    def test_every_encoder_has_a_label(self):
        self.assertEqual(set(gpu.LABELS), set(gpu.ENCODERS))


class CommandTests(unittest.TestCase):
    def job(self, **edit):
        return VideoJob(Path("in.mp4"), Path("out.mp4"), VideoEdit(**edit))

    def without_codec(self, cmd):
        """The command minus the video encoder's own options."""
        start = cmd.index("-c:v")
        end = cmd.index("-movflags")
        return cmd[:start] + cmd[end:]

    def test_the_cpu_command_is_x264_as_before(self):
        cmd = video_command("ffmpeg", self.job(crf=23), INFO)
        self.assertEqual(after(cmd, "-c:v"), "libx264")
        self.assertEqual(after(cmd, "-crf"), "23")
        self.assertEqual(after(cmd, "-pix_fmt"), "yuv420p")
        self.assertEqual(after(cmd, "-movflags"), "+faststart")

    def test_only_the_video_encoder_changes_on_the_card(self):
        cpu = video_command("ffmpeg", self.job(crf=18, speed=2.0, fade_in=1.0), INFO)
        for encoder in gpu.ENCODERS:
            with self.subTest(encoder=encoder):
                card = video_command("ffmpeg", self.job(crf=18, speed=2.0, fade_in=1.0), INFO, encoder=encoder)
                self.assertEqual(after(card, "-c:v"), encoder)
                self.assertNotIn("libx264", card)
                self.assertNotIn("-crf", card)
                self.assertEqual(after(card, "-movflags"), "+faststart")
                # Filters, audio, metadata and the output are the same.
                self.assertEqual(after(card, "-filter_complex"), after(cpu, "-filter_complex"))
                self.assertEqual(after(card, "-c:a"), after(cpu, "-c:a"))
                self.assertEqual(card[-1], cpu[-1])
                self.assertEqual(card[:cpu.index("-c:v")], cpu[:cpu.index("-c:v")])

    def test_the_quality_choices_reach_the_card(self):
        numbers = [after(video_command("ffmpeg", self.job(crf=crf), INFO, encoder="h264_nvenc"), "-cq")
                   for crf in (18, 23, 28)]
        self.assertEqual(numbers, ["26", "31", "36"])

    def test_the_frame_rate_comes_from_the_video(self):
        cmd = video_command("ffmpeg", self.job(), INFO, encoder="h264_qsv")
        self.assertEqual(after(cmd, "-r"), "15")
        unknown = MediaInfo(duration=4.0, width=640, height=360, has_audio=True)
        self.assertEqual(after(video_command("ffmpeg", self.job(), unknown, encoder="h264_qsv"), "-r"), "30")

    def test_a_join_uses_the_card_for_the_whole_video(self):
        parts = [JoinPart(Path("a.mp4"), VideoEdit(crf=28), INFO), JoinPart(Path("b.mp4"), VideoEdit(), INFO)]
        job = JoinJob(parts, Path("out.mp4"))
        cpu, _ = join_command("ffmpeg", job)
        card, _ = join_command("ffmpeg", job, encoder="h264_nvenc")
        self.assertEqual(after(cpu, "-c:v"), "libx264")
        self.assertEqual(after(card, "-c:v"), "h264_nvenc")
        self.assertEqual(after(card, "-cq"), "36")
        self.assertEqual(after(card, "-filter_complex"), after(cpu, "-filter_complex"))
        self.assertEqual(after(card, "-movflags"), "+faststart")

    def test_codec_args_without_an_encoder_are_the_cpu_ones(self):
        self.assertEqual(video_codec_args(23)[:2], ["-c:v", "libx264"])
        self.assertEqual(video_codec_args(23, None), video_codec_args(23))


class SettingTests(unittest.TestCase):
    def test_it_is_on_by_default(self):
        self.assertTrue(VideoEdit().use_gpu)
        self.assertTrue(ClipSettings().edit.use_gpu)

    def test_it_survives_a_round_trip(self):
        settings = ClipSettings()
        settings.edit.use_gpu = False
        loaded = ClipSettings.from_dict(settings.to_dict())
        self.assertFalse(loaded.edit.use_gpu)
        self.assertEqual(loaded, settings)

    def test_old_presets_without_it_get_the_default(self):
        data = ClipSettings().to_dict()
        del data["edit"]["use_gpu"]
        self.assertTrue(ClipSettings.from_dict(data).edit.use_gpu)

    def test_nonsense_values_are_ignored(self):
        for junk in ("no", 0, 1, None, [], {}):
            with self.subTest(junk=junk):
                data = ClipSettings().to_dict()
                data["edit"]["use_gpu"] = junk
                self.assertTrue(ClipSettings.from_dict(data).edit.use_gpu)


class DetectionTests(unittest.TestCase):
    def setUp(self):
        gpu.forget_detection()
        self.addCleanup(gpu.forget_detection)

    def test_only_encoders_that_really_work_count_and_the_best_comes_first(self):
        with mock.patch.object(gpu, "_encoder_works", side_effect=lambda ff, name: name != "h264_nvenc") as works:
            self.assertEqual(gpu.detect_encoders("fake-ffmpeg"), ("h264_qsv", "h264_amf"))
            self.assertEqual(works.call_count, 3)
            self.assertEqual(gpu.best_encoder("fake-ffmpeg"), "h264_qsv")

    def test_it_is_checked_once_per_run(self):
        with mock.patch.object(gpu, "_encoder_works", return_value=True) as works:
            for _ in range(3):
                gpu.detect_encoders("fake-ffmpeg")
            self.assertEqual(works.call_count, len(gpu.ENCODERS))
            gpu.detect_encoders("another-ffmpeg")
            self.assertEqual(works.call_count, 2 * len(gpu.ENCODERS))

    def test_no_working_encoder_means_none(self):
        with mock.patch.object(gpu, "_encoder_works", return_value=False):
            self.assertEqual(gpu.detect_encoders("fake-ffmpeg"), ())
            self.assertIsNone(gpu.best_encoder("fake-ffmpeg"))
            self.assertEqual(gpu.describe_gpu("fake-ffmpeg"), "No supported graphics card found")

    def test_the_setting_decides_whether_the_card_is_used(self):
        with mock.patch.object(gpu, "_encoder_works", return_value=True):
            self.assertEqual(gpu.choose_encoder("fake-ffmpeg", True), "h264_nvenc")
            self.assertIsNone(gpu.choose_encoder("fake-ffmpeg", False))

    def test_the_card_is_named(self):
        with mock.patch.object(gpu, "_encoder_works", side_effect=lambda ff, name: name == "h264_amf"):
            self.assertEqual(gpu.describe_gpu("fake-ffmpeg"), "AMD graphics card")

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_an_encoder_that_is_missing_or_broken_does_not_count(self):
        with mock.patch.dict(gpu.ENCODERS, BOGUS):
            self.assertFalse(gpu._encoder_works(FFMPEG, "h264_bogus"))
        self.assertFalse(gpu._encoder_works("no-such-ffmpeg-program", "h264_nvenc"))

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_a_probe_that_takes_too_long_does_not_count(self):
        with mock.patch.object(gpu.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            self.assertFalse(gpu._encoder_works(FFMPEG, "h264_nvenc"))


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class FallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.source = cls.tmp / "clip.mp4"
        make_video(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def export(self, name="out.mp4", encoder="h264_bogus", cancel=None, notes=None, edit=None):
        out = self.tmp / name
        with mock.patch.dict(gpu.ENCODERS, BOGUS):
            return out, convert_video(FFMPEG, VideoJob(self.source, out, edit or VideoEdit()), cancel_event=cancel,
                                      encoder=encoder, on_note=(notes.append if notes is not None else None))

    def test_a_failing_card_means_a_second_try_on_the_cpu(self):
        notes = []
        out, result = self.export(notes=notes)
        self.assertEqual(result, out)
        self.assertEqual(notes, [gpu.CPU_FALLBACK_NOTE])
        info = probe_media(FFMPEG, out)
        self.assertAlmostEqual(info.duration, 2.0, delta=0.3)
        self.assertLess(info.width, info.height)

    def test_a_working_encoder_is_not_retried(self):
        notes = []
        with mock.patch("video_converter.video.run_ffmpeg", side_effect=lambda cmd, *a, **k: Path(cmd[-1])) as run:
            convert_video(FFMPEG, VideoJob(self.source, self.tmp / "x.mp4", VideoEdit()), encoder="h264_nvenc",
                          on_note=notes.append)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(notes, [])

    def test_the_cpu_is_never_retried(self):
        with mock.patch("video_converter.video.run_ffmpeg", side_effect=ConversionError("boom")) as run:
            with self.assertRaises(ConversionError):
                convert_video(FFMPEG, VideoJob(self.source, self.tmp / "x.mp4", VideoEdit()), encoder=None)
        self.assertEqual(run.call_count, 1)

    def test_when_the_cpu_fails_too_the_error_is_the_cpus(self):
        errors = [ConversionError("card said no"), ConversionError("cpu said no")]
        with mock.patch("video_converter.video.run_ffmpeg", side_effect=errors) as run:
            with self.assertRaisesRegex(ConversionError, "cpu said no"):
                convert_video(FFMPEG, VideoJob(self.source, self.tmp / "x.mp4", VideoEdit()), encoder="h264_nvenc")
        self.assertEqual(run.call_count, 2)
        first, second = (call.args[0] for call in run.call_args_list)
        self.assertEqual(after(first, "-c:v"), "h264_nvenc")
        self.assertEqual(after(second, "-c:v"), "libx264")

    def test_the_retry_is_the_same_job(self):
        with mock.patch("video_converter.video.run_ffmpeg",
                        side_effect=[ConversionError("x"), Path("out.mp4")]) as run:
            convert_video(FFMPEG, VideoJob(self.source, self.tmp / "x.mp4", VideoEdit(crf=18, speed=2.0)),
                          encoder="h264_nvenc")
        first, second = (call.args[0] for call in run.call_args_list)
        self.assertEqual(after(first, "-filter_complex"), after(second, "-filter_complex"))
        self.assertEqual(first[-1], second[-1])
        self.assertEqual(after(second, "-crf"), "18")

    def test_cancelling_before_the_first_try_leaves_nothing(self):
        cancel = threading.Event()
        cancel.set()
        out = self.tmp / "cancelled-1.mp4"
        with self.assertRaises(ConversionCancelled):
            self.export("cancelled-1.mp4", cancel=cancel)
        self.assertFalse(out.exists())

    def test_cancelling_after_the_card_failed_stops_instead_of_retrying(self):
        cancel = threading.Event()

        def fail_and_cancel(cmd, *args, **kwargs):
            cancel.set()
            raise ConversionError("card failed")

        with mock.patch("video_converter.video.run_ffmpeg", side_effect=fail_and_cancel) as run:
            with self.assertRaises(ConversionCancelled):
                convert_video(FFMPEG, VideoJob(self.source, self.tmp / "x.mp4", VideoEdit()),
                              cancel_event=cancel, encoder="h264_nvenc")
        self.assertEqual(run.call_count, 1)

    def test_cancelling_during_the_cpu_retry_removes_the_partial_file(self):
        cancel = threading.Event()
        out = self.tmp / "cancelled-2.mp4"
        real_run = __import__("video_converter.core", fromlist=["run_ffmpeg"]).run_ffmpeg
        calls = []

        def run(cmd, output, duration, on_progress, cancel_event, cwd=None):
            calls.append(after(cmd, "-c:v"))
            if len(calls) == 2:
                # The CPU's run: start it, and have the user cancel as soon as there is progress.
                def progress(fraction):
                    cancel.set()
                return real_run(cmd, output, duration, progress, cancel_event, cwd)
            return real_run(cmd, output, duration, on_progress, cancel_event, cwd)

        with mock.patch("video_converter.video.run_ffmpeg", side_effect=run):
            with mock.patch.dict(gpu.ENCODERS, BOGUS):
                with self.assertRaises(ConversionCancelled):
                    convert_video(FFMPEG, VideoJob(self.source, out, VideoEdit()), cancel_event=cancel,
                                  encoder="h264_bogus")
        self.assertEqual(calls, ["h264_does_not_exist", "libx264"])
        self.assertFalse(out.exists())

    def test_a_join_falls_back_too(self):
        notes = []
        second = self.tmp / "second.mp4"
        make_video(second)
        out = self.tmp / "joined.mp4"
        with mock.patch.dict(gpu.ENCODERS, BOGUS):
            join_clips(FFMPEG, JoinJob([JoinPart(self.source, VideoEdit()), JoinPart(second, VideoEdit())], out),
                       encoder="h264_bogus", on_note=notes.append)
        self.assertEqual(notes, [gpu.CPU_FALLBACK_NOTE])
        self.assertAlmostEqual(probe_media(FFMPEG, out).duration, 4.0, delta=0.4)

    def test_a_queue_job_records_the_note(self):
        job = Job(self.source, ClipSettings(), self.tmp, probe_media(FFMPEG, self.source))
        with mock.patch.object(gpu, "_encoder_works", return_value=True), \
                mock.patch.dict(gpu.ENCODERS, {"h264_nvenc": BOGUS["h264_bogus"]}):
            gpu.forget_detection()
            self.addCleanup(gpu.forget_detection)
            output = run_job(FFMPEG, job, lambda fraction: None)
        self.assertTrue(output.is_file())
        self.assertEqual(job.note, gpu.CPU_FALLBACK_NOTE)

    def test_a_job_with_the_card_turned_off_never_asks_for_it(self):
        settings = ClipSettings()
        settings.edit.use_gpu = False
        job = Job(self.source, settings, self.tmp, probe_media(FFMPEG, self.source))
        with mock.patch("video_converter.jobs.convert_video", return_value=Path("x.mp4")) as export, \
                mock.patch.object(gpu, "_encoder_works", return_value=True):
            gpu.forget_detection()
            self.addCleanup(gpu.forget_detection)
            run_job(FFMPEG, job, lambda fraction: None)
        self.assertIsNone(export.call_args.args[5])         # the encoder
        self.assertEqual(job.note, "")


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RealExportTests(unittest.TestCase):
    """An export with whatever graphics encoder this PC has (skipped when it has none)."""

    def test_the_detected_encoder_makes_a_playable_video(self):
        encoder = gpu.best_encoder(FFMPEG)
        if encoder is None:
            self.skipTest("no working graphics card encoder on this PC")
        with tempfile.TemporaryDirectory() as folder:
            source, out = Path(folder) / "clip.mp4", Path(folder) / "out.mp4"
            make_video(source, 640, 360, 3)
            notes = []
            convert_video(FFMPEG, VideoJob(source, out, VideoEdit(crf=23, speed=1.5)), encoder=encoder,
                          on_note=notes.append)
            self.assertEqual(notes, [], "the card should have worked, not fallen back to the CPU")
            info = probe_media(FFMPEG, out)
            self.assertEqual((info.width, info.height), (360, 640))
            self.assertAlmostEqual(info.duration, 2.0, delta=0.3)
            self.assertTrue(info.has_audio)
            self.assertGreater(out.stat().st_size, 1000)
            decoded = subprocess.run([FFMPEG, "-v", "error", "-i", str(out), "-f", "null", "-"],
                                     capture_output=True, text=True)
            self.assertEqual((decoded.returncode, decoded.stderr.strip()), (0, ""))
            header = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
            self.assertIn("h264", header)
            self.assertIn("yuv420p", header)


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            source.write_bytes(b"x")
            with mock.patch.object(cli, "find_ffmpeg", return_value="ffmpeg"), \
                    mock.patch.object(cli, "choose_encoder", return_value="h264_nvenc") as choose, \
                    mock.patch.object(cli, "convert_video", side_effect=lambda f, job, **kw: job.output) as export:
                with redirect_stdout(io.StringIO()):
                    code = cli.run_cli([str(source), "--video", "phone", *args])
        return code, choose, export

    def test_the_card_is_used_by_default(self):
        code, choose, export = self.run_cli()
        self.assertEqual(code, 0)
        choose.assert_called_once_with("ffmpeg", True)
        self.assertEqual(export.call_args.kwargs["encoder"], "h264_nvenc")

    def test_gpu_off_turns_it_off(self):
        _, choose, export = self.run_cli("--gpu", "off")
        choose.assert_called_once_with("ffmpeg", False)
        self.assertFalse(export.call_args.args[1].edit.use_gpu)

    def test_gpu_auto_is_accepted_and_other_words_are_not(self):
        self.assertEqual(self.run_cli("--gpu", "auto")[0], 0)
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                self.run_cli("--gpu", "sometimes")


if __name__ == "__main__":
    unittest.main()
