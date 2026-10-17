import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from video_converter.core import (
    ConversionError,
    ConversionJob,
    convert,
    find_ffmpeg,
    format_time,
    parse_media_info,
    parse_time,
    probe_media,
)
from video_converter.video import VideoEdit, VideoJob, convert_video, plan_geometry, render_frame, render_source_frames

FFMPEG = find_ffmpeg()
ROOT = Path(__file__).resolve().parent.parent


def make_video(ffmpeg, path, width=640, height=360, seconds=4, audio=True, rotation=None):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={width}x{height}:rate=15"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    if rotation is not None:
        rotated = path.with_name(f"rotated-{path.name}")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", str(rotation),
                        "-i", str(path), "-c", "copy", str(rotated)], check=True)
        rotated.replace(path)


class TimeParsingTests(unittest.TestCase):
    def test_parse_time(self):
        self.assertIsNone(parse_time("  "))
        self.assertEqual(parse_time("90"), 90)
        self.assertEqual(parse_time("1:30"), 90)
        self.assertEqual(parse_time("1:02:03.5"), 3723.5)
        for bad in ("abc", "1:2:3:4", "-5"):
            with self.assertRaises(ValueError):
                parse_time(bad)

    def test_format_time(self):
        self.assertEqual(format_time(75.25), "1:15.2")
        self.assertEqual(format_time(3723), "1:02:03.0")


class MediaInfoParsingTests(unittest.TestCase):
    SAMPLE = """
  Duration: 00:01:05.50, start: 0.000000, bitrate: 3000 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, 1920x1080 [SAR 1:1 DAR 16:9], 30 fps
      Side data:
        displaymatrix: rotation of -90.00 degrees
  Stream #0:1[0x2](und): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s
"""

    def test_reads_rotated_phone_video_as_portrait(self):
        info = parse_media_info(self.SAMPLE)
        self.assertEqual((info.width, info.height), (1080, 1920))
        self.assertAlmostEqual(info.duration, 65.5)
        self.assertTrue(info.has_audio)

    def test_audio_only_file(self):
        info = parse_media_info("  Duration: 00:00:10.00\n  Stream #0:0: Audio: mp3, 44100 Hz\n")
        self.assertFalse(info.has_video)


class GeometryTests(unittest.TestCase):
    def test_landscape_to_phone_crops_center(self):
        g = plan_geometry(1920, 1080, VideoEdit(aspect=(9, 16)))
        self.assertEqual((g.out_width, g.out_height), (1080, 1920))
        cw, ch, x, y = g.crop
        self.assertEqual(ch, 1080)
        self.assertAlmostEqual(cw / ch, 9 / 16, delta=0.01)
        self.assertAlmostEqual(x, (1920 - cw) / 2, delta=1)
        self.assertEqual(y, 0)

    def test_crop_position_and_zoom(self):
        left = plan_geometry(1920, 1080, VideoEdit(aspect=(9, 16), pos_x=0.0)).crop
        right = plan_geometry(1920, 1080, VideoEdit(aspect=(9, 16), pos_x=1.0)).crop
        self.assertEqual(left[2], 0)
        self.assertEqual(right[2] + right[0], 1920)
        zoomed = plan_geometry(1920, 1080, VideoEdit(aspect=(9, 16), zoom=2.0)).crop
        self.assertEqual(zoomed[1], 540)

    def test_fit_modes_do_not_crop(self):
        for fit in ("blur", "bars"):
            g = plan_geometry(1920, 1080, VideoEdit(aspect=(9, 16), fit=fit))
            self.assertIsNone(g.crop)
            self.assertEqual((g.out_width, g.out_height), (1080, 1920))

    def test_resolution_rotation_and_even_sizes(self):
        g = plan_geometry(1920, 1080, VideoEdit(aspect=(1, 1), resolution=720))
        self.assertEqual((g.out_width, g.out_height), (720, 720))
        g = plan_geometry(1920, 1080, VideoEdit(aspect=None, rotate=90))
        self.assertEqual((g.width, g.height, g.out_width, g.out_height), (1080, 1920, 1080, 1920))
        g = plan_geometry(641, 359, VideoEdit(aspect=(9, 16)))
        self.assertTrue(all(v % 2 == 0 for v in (g.out_width, g.out_height, g.crop[0], g.crop[1])))


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class VideoExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.landscape = cls.tmp / "landscape.mp4"
        make_video(FFMPEG, cls.landscape)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def export(self, name, edit, source=None):
        return probe_media(FFMPEG, convert_video(FFMPEG, VideoJob(source or self.landscape, self.tmp / name, edit)))

    def test_phone_export_in_every_fit_mode(self):
        for fit in ("crop", "blur", "bars"):
            with self.subTest(fit=fit):
                info = self.export(f"phone-{fit}.mp4", VideoEdit(aspect=(9, 16), fit=fit))
                self.assertEqual((info.width, info.height), (360, 640))
                self.assertTrue(info.has_audio)

    def test_trim_rotate_mute_and_size(self):
        edit = VideoEdit(aspect=None, rotate=90, flip=True, mute=True, resolution=480, start=1.0, end=2.5)
        info = self.export("edited.mp4", edit)
        self.assertEqual((info.width, info.height), (480, 854))
        self.assertAlmostEqual(info.duration, 1.5, delta=0.2)
        self.assertFalse(info.has_audio)

    def test_rotated_phone_recording_to_square(self):
        source = self.tmp / "portrait.mp4"
        make_video(FFMPEG, source, rotation=90)
        self.assertEqual(probe_media(FFMPEG, source).height, 640)
        info = self.export("square.mp4", VideoEdit(aspect=(1, 1)), source)
        self.assertEqual((info.width, info.height), (360, 360))

    def test_video_without_audio_track(self):
        source = self.tmp / "silent.mp4"
        make_video(FFMPEG, source, audio=False)
        self.assertFalse(self.export("silent-phone.mp4", VideoEdit(), source).has_audio)

    def test_rejects_bad_trim(self):
        with self.assertRaises(ConversionError):
            convert_video(FFMPEG, VideoJob(self.landscape, self.tmp / "bad.mp4", VideoEdit(start=3, end=2)))

    def test_preview_frames(self):
        info = probe_media(FFMPEG, self.landscape)
        result = render_frame(FFMPEG, self.landscape, self.tmp / "res.png", 1.0, (200, 200), VideoEdit(), info)
        render_source_frames(FFMPEG, self.landscape, self.tmp / "src.png", self.tmp / "dim.png", 1.0,
                             (320, 200), VideoEdit())
        for png in (result, self.tmp / "src.png", self.tmp / "dim.png"):
            self.assertGreater(png.stat().st_size, 0)

    def test_trimmed_mp3(self):
        output = convert(FFMPEG, ConversionJob(self.landscape, self.tmp / "clip.mp3", start=1.0, end=3.0))
        self.assertAlmostEqual(probe_media(FFMPEG, output).duration, 2.0, delta=0.2)

    def test_command_line_video_export(self):
        result = subprocess.run(
            [sys.executable, "-m", "video_converter", str(self.landscape), "--video", "square", "--fit", "bars",
             "--end", "2", "-o", str(self.tmp / "cli")],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        info = probe_media(FFMPEG, self.tmp / "cli" / "landscape (square).mp4")
        self.assertEqual((info.width, info.height), (360, 360))


if __name__ == "__main__":
    unittest.main()
