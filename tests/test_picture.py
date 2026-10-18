"""Picture adjustments, color looks and stabilization (2.3)."""

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from video_converter import video
from video_converter.core import ConversionCancelled, MediaInfo, find_ffmpeg, probe_media
from video_converter.video import (
    ADJUSTMENTS,
    DETECT_SHARE,
    LOOKS,
    STABILIZE,
    JoinJob,
    JoinPart,
    VideoEdit,
    VideoJob,
    convert_video,
    detect_command,
    join_clips,
    picture_filters,
    render_frame,
    video_command,
)

FFMPEG = find_ffmpeg()
ROOT = Path(__file__).resolve().parent.parent
SOURCE = MediaInfo(duration=6.0, width=640, height=360, has_audio=True, fps=15.0)


def has_vidstab() -> bool:
    if not FFMPEG:
        return False
    result = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True)
    return "vidstabdetect" in result.stdout


def make_shaky_video(path, seconds=3):
    """A moving test pattern seen through a jittering crop, with sound."""
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=400x240:rate=15",
                    "-f", "lavfi", "-i", f"sine=duration={seconds}",
                    "-vf", "crop=360:216:20+12*sin(n*1.7):12+10*cos(n*2.3)", "-c:a", "aac",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)], check=True)


def picture_stats(path, at=1.0):
    """Average saturation and brightness (luma) of one frame, from ffmpeg's signalstats."""
    seek = ["-ss", str(at)] if at is not None else []
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", *seek, "-i", str(path), "-frames:v", "1",
         "-vf", "signalstats,metadata=print:file=-", "-f", "null", "-"],
        capture_output=True, text=True, check=True)
    stats = dict(line.split("=", 1) for line in result.stdout.splitlines() if line.startswith("lavfi.signalstats."))
    return float(stats["lavfi.signalstats.SATAVG"]), float(stats["lavfi.signalstats.YAVG"])


def graph_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


class PictureFilterTests(unittest.TestCase):
    def test_neutral_settings_add_no_filters(self):
        self.assertEqual(picture_filters(VideoEdit()), [])
        self.assertNotIn("eq=", graph_of(video_command("ffmpeg", VideoJob(Path("a.mp4"), Path("b.mp4"),
                                                                          VideoEdit()), SOURCE)))

    def test_filters_come_in_a_fixed_order(self):
        edit = VideoEdit(denoise=4, brightness=0.1, contrast=1.2, look="warm", sharpen=0.8)
        filters = picture_filters(edit)
        self.assertEqual(filters[0], "hqdn3d=4")
        self.assertEqual(filters[1], "eq=brightness=0.1:contrast=1.2")
        self.assertEqual(filters[2:-1], LOOKS["warm"][1])
        self.assertEqual(filters[-1], "unsharp=5:5:0.8:5:5:0")

    def test_only_changed_eq_values_are_set(self):
        self.assertEqual(picture_filters(VideoEdit(saturation=0.0)), ["eq=saturation=0"])
        self.assertEqual(picture_filters(VideoEdit(gamma=1.25)), ["eq=gamma=1.25"])

    def test_values_outside_the_ranges_are_clamped(self):
        filters = picture_filters(VideoEdit(brightness=5, contrast=0.0, sharpen=9, denoise=-3))
        self.assertEqual(filters, ["eq=brightness=0.5:contrast=0.5", "unsharp=5:5:1.5:5:5:0"])

    def test_every_look(self):
        self.assertEqual(LOOKS["none"][1], [])
        for key, (label, filters) in LOOKS.items():
            with self.subTest(look=key):
                self.assertEqual(picture_filters(VideoEdit(look=key)), filters)
                self.assertTrue(label)
        self.assertEqual(picture_filters(VideoEdit(look="sepia")), [])     # unknown: no look

    def test_ranges_hold_their_neutral_values(self):
        for name, (low, high, neutral) in ADJUSTMENTS.items():
            with self.subTest(field=name):
                self.assertTrue(low <= neutral <= high)
                self.assertEqual(getattr(VideoEdit(), name), neutral)

    def test_adjustments_come_after_reframing_and_before_reverse_and_fades(self):
        edit = VideoEdit(look="bw", reverse=True, fade_in=1.0)
        graph = graph_of(video_command("ffmpeg", VideoJob(Path("a.mp4"), Path("b.mp4"), edit), SOURCE))
        self.assertLess(graph.index("scale="), graph.index("hue=s=0"))
        self.assertLess(graph.index("hue=s=0"), graph.index("reverse"))
        self.assertLess(graph.index("reverse"), graph.index("fade=t=in"))


class StabilizeCommandTests(unittest.TestCase):
    def command(self, edit, transforms="shake0.trf"):
        return video_command("ffmpeg", VideoJob(Path("a.mp4"), Path("b.mp4"), edit), SOURCE, transforms)

    def test_the_transform_comes_right_after_the_cuts_and_before_the_speed(self):
        graph = graph_of(self.command(VideoEdit(stabilize="strong", speed=2, keep=[(0, 2), (3, 6)])))
        self.assertIn("vidstabtransform=input=shake0.trf:smoothing=30", graph)
        self.assertLess(graph.index("concat="), graph.index("vidstabtransform"))
        self.assertLess(graph.index("vidstabtransform"), graph.index("setpts=PTS/2"))
        self.assertLess(graph.index("vidstabtransform"), graph.index("crop="))

    def test_no_transform_without_a_detect_pass_or_when_off(self):
        self.assertNotIn("vidstab", graph_of(self.command(VideoEdit(stabilize="light"), None)))
        self.assertNotIn("vidstab", graph_of(self.command(VideoEdit(stabilize="off"))))

    def test_detect_pass_sees_the_same_frames_as_the_export(self):
        edit = VideoEdit(stabilize="light", keep=[(1, 2), (3, 5)])
        segments = edit.segments(SOURCE.duration)
        detect = detect_command("ffmpeg", Path("a.mp4"), edit, segments, "shake0.trf")
        export = self.command(edit)
        self.assertEqual(detect[:detect.index("-i") + 2], export[:export.index("-i") + 2])   # same seek and input
        self.assertIn("trim=start=0:end=1", graph_of(detect))
        self.assertIn(f"vidstabdetect=shakiness={STABILIZE['light'][0]}", graph_of(detect))
        self.assertIn("result=shake0.trf", graph_of(detect))
        self.assertEqual(detect[detect.index("-f") + 1], "null")         # measures only, writes no video

    def test_strong_is_stronger_than_light(self):
        self.assertIsNone(STABILIZE["off"])
        self.assertGreater(STABILIZE["strong"][0], STABILIZE["light"][0])
        self.assertGreater(STABILIZE["strong"][1], STABILIZE["light"][1])


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class PictureExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.source = cls.tmp / "clip.mp4"
        make_shaky_video(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def export(self, name, **edit):
        return convert_video(FFMPEG, VideoJob(self.source, self.tmp / name, VideoEdit(aspect=None, **edit)))

    def test_black_and_white_has_no_color_left(self):
        source_saturation, _ = picture_stats(self.source)
        saturation, _ = picture_stats(self.export("bw.mp4", look="bw"))
        self.assertGreater(source_saturation, 30)
        self.assertLess(saturation, 1.0)

    def test_saturation_and_brightness_sliders(self):
        source_saturation, source_luma = picture_stats(self.source)
        saturation, _ = picture_stats(self.export("vivid.mp4", saturation=2.0))
        _, luma = picture_stats(self.export("bright.mp4", brightness=0.2))
        self.assertGreater(saturation, source_saturation * 1.3)
        self.assertGreater(luma, source_luma + 20)

    def test_every_look_and_the_detail_filters_export(self):
        for look in LOOKS:
            with self.subTest(look=look):
                info = probe_media(FFMPEG, self.export(f"look-{look}.mp4", look=look, sharpen=0.5, denoise=2))
                self.assertAlmostEqual(info.duration, 3.0, delta=0.2)

    def test_the_preview_shows_the_adjustments_like_the_export(self):
        info = probe_media(FFMPEG, self.source)
        edit = VideoEdit(aspect=None, look="bw", brightness=0.2)
        png = render_frame(FFMPEG, self.source, self.tmp / "preview.png", 1.0, (360, 360), edit, info)
        exported = self.export("preview-match.mp4", look="bw", brightness=0.2)
        # Compare the same frame of the export, saved as a PNG the same way the preview is.
        export_png = self.tmp / "export-frame.png"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", "1.0", "-i", str(exported),
                        "-frames:v", "1", "-update", "1", str(export_png)], check=True)
        preview_saturation, preview_luma = picture_stats(png, None)
        export_saturation, export_luma = picture_stats(export_png, None)
        _, plain_luma = picture_stats(render_frame(FFMPEG, self.source, self.tmp / "plain.png", 1.0, (360, 360),
                                                   VideoEdit(aspect=None), info), None)
        self.assertLess(preview_saturation, 1.0)
        self.assertLess(export_saturation, 1.0)
        self.assertAlmostEqual(preview_luma, export_luma, delta=4)
        self.assertGreater(preview_luma, plain_luma + 20)               # the brightness change shows


@unittest.skipUnless(has_vidstab(), "this ffmpeg has no libvidstab")
class StabilizeExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.source = cls.tmp / "shaky.mp4"
        make_shaky_video(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def recorded_run(self, action):
        """Run `action()` while recording every ffmpeg run: (command, working folder, output)."""
        calls, real = [], video.run_ffmpeg

        def record(cmd, output, duration, on_progress=None, cancel_event=None, cwd=None):
            calls.append((cmd, cwd, output))
            return real(cmd, output, duration, on_progress, cancel_event, cwd)

        with mock.patch.object(video, "run_ffmpeg", record):
            result = action()
        return result, calls

    def test_both_passes_run_in_one_temporary_folder_that_is_removed(self):
        progress = []
        job = VideoJob(self.source, self.tmp / "steady.mp4", VideoEdit(aspect=None, stabilize="strong"))
        output, calls = self.recorded_run(lambda: convert_video(FFMPEG, job, on_progress=progress.append))
        self.assertEqual(len(calls), 2)
        (detect, detect_cwd, _), (export, export_cwd, _) = calls
        self.assertIn("vidstabdetect", graph_of(detect))
        self.assertIn("vidstabtransform", graph_of(export))
        self.assertEqual(detect_cwd, export_cwd)
        self.assertFalse(detect_cwd.exists())
        self.assertAlmostEqual(probe_media(FFMPEG, output).duration, 3.0, delta=0.2)
        self.assertEqual(progress, sorted(progress))                    # never goes backwards
        self.assertTrue(any(0 < p < DETECT_SHARE for p in progress))    # the detect pass reports progress
        self.assertTrue(any(DETECT_SHARE < p < 1 for p in progress))
        self.assertEqual(progress[-1], 1.0)

    def test_relative_paths_still_work_although_ffmpeg_runs_elsewhere(self):
        # ffmpeg runs in the transforms folder, so relative paths must not be taken from there.
        out_dir = self.tmp / "relative"
        out_dir.mkdir()
        (out_dir / "shaky.mp4").write_bytes(self.source.read_bytes())
        job = VideoJob(Path("shaky.mp4"), Path("steady-relative.mp4"), VideoEdit(aspect=None, stabilize="light"))
        original = Path.cwd()
        os.chdir(out_dir)
        try:
            convert_video(FFMPEG, job)
        finally:
            os.chdir(original)
        self.assertTrue((out_dir / "steady-relative.mp4").is_file())

    def cancel_when(self, condition, name):
        cancel = threading.Event()
        job = VideoJob(self.source, self.tmp / name, VideoEdit(aspect=None, stabilize="light"))

        def progress(fraction):
            if condition(fraction):
                cancel.set()

        def action():
            with self.assertRaises(ConversionCancelled):
                convert_video(FFMPEG, job, on_progress=progress, cancel_event=cancel)
        _, calls = self.recorded_run(action)
        self.assertFalse((self.tmp / name).exists())
        self.assertFalse(calls[0][1].exists())          # the transforms folder is gone too
        return calls

    def test_cancelling_during_the_detect_pass(self):
        calls = self.cancel_when(lambda f: f > 0, "cancel-detect.mp4")
        self.assertEqual(len(calls), 1)                  # the export pass never started

    def test_cancelling_during_the_export_pass(self):
        calls = self.cancel_when(lambda f: f > DETECT_SHARE, "cancel-export.mp4")
        self.assertEqual(len(calls), 2)

    def test_joining_stabilizes_only_the_clips_that_ask(self):
        job = JoinJob([JoinPart(self.source, VideoEdit()), JoinPart(self.source, VideoEdit(stabilize="light"))],
                      self.tmp / "joined.mp4")
        output, calls = self.recorded_run(lambda: join_clips(FFMPEG, job))
        self.assertEqual(len(calls), 2)
        self.assertIn("result=shake1.trf", graph_of(calls[0][0]))
        export = graph_of(calls[1][0])
        self.assertEqual(export.count("vidstabtransform"), 1)
        self.assertIn("input=shake1.trf", export)
        self.assertAlmostEqual(probe_media(FFMPEG, output).duration, 6.0, delta=0.3)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class PictureCommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "video_converter", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True)

    def test_look_and_adjustments(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            make_shaky_video(tmp / "clip.mp4", seconds=2)
            result = self.run_cli(tmp / "clip.mp4", "--video", "original", "--look", "bw", "--contrast", "1.2",
                                  "-o", tmp / "out")
            self.assertEqual(result.returncode, 0, result.stderr)
            saturation, _ = picture_stats(tmp / "out" / "clip (edited).mp4")
            self.assertLess(saturation, 1.0)

    def test_bad_picture_options_are_refused(self):
        for args in (["--look", "bw"], ["--video", "phone", "--contrast", "5"], ["--video", "phone", "--look", "sepia"],
                     ["--video", "phone", "--stabilize", "max"]):
            with self.subTest(args=args):
                result = self.run_cli("missing.mp4", *args)
                self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
