import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from video_converter.core import (
    ConversionCancelled,
    ConversionError,
    ConversionJob,
    convert,
    find_ffmpeg,
    output_path_for,
    parse_media_info,
    probe_duration,
    probe_media,
)

FFMPEG = find_ffmpeg()


def make_video(ffmpeg: str, path: Path, seconds: int = 3, audio: bool = True) -> None:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=10"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)


class OutputPathTests(unittest.TestCase):
    def test_avoids_overwriting_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            self.assertEqual(output_path_for(source).name, "clip.mp3")
            (Path(tmp) / "clip.mp3").touch()
            self.assertEqual(output_path_for(source).name, "clip (1).mp3")
            self.assertEqual(output_path_for(source, overwrite=True).name, "clip.mp3")

    def test_uses_custom_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            self.assertEqual(output_path_for(Path(tmp) / "a.mp4", out), out / "a.mp3")


class FrameRateParsingTests(unittest.TestCase):
    def fps(self, video_line):
        return parse_media_info(f"  Duration: 00:00:04.00\n  Stream #0:0: Video: {video_line}\n").fps

    def test_reads_the_fps_figure(self):
        self.assertEqual(self.fps("h264, yuv420p, 1920x1080, 30 fps, 30 tbr, 15360 tbn"), 30.0)
        self.assertEqual(self.fps("h264, yuv420p, 1920x1080, 29.97 fps, 29.97 tbr, 30k tbn"), 29.97)
        self.assertEqual(self.fps("h264, yuv420p, 1280x720, 59.94 fps, 59.94 tbr, 90k tbn"), 59.94)

    def test_falls_back_to_tbr_when_there_is_no_fps(self):
        self.assertEqual(self.fps("mpeg4, yuv420p, 640x360, 25 tbr, 25 tbn"), 25.0)
        self.assertEqual(self.fps("h264, yuv420p, 640x360, 23.98 tbr, 1k tbn"), 23.98)

    def test_fps_wins_over_tbr(self):
        self.assertEqual(self.fps("h264, yuv420p, 640x360, 15 fps, 30 tbr, 15360 tbn"), 15.0)

    def test_a_k_suffix_means_thousands_and_is_range_checked(self):
        self.assertEqual(self.fps("h264, yuv420p, 640x360, 1k tbr, 1k tbn"), 1000.0)
        self.assertIsNone(self.fps("h264, yuv420p, 640x360, 1.2k tbr, 1k tbn"))
        self.assertIsNone(self.fps("h264, yuv420p, 640x360, 90k tbr, 90k tbn"))

    def test_the_time_base_is_not_a_frame_rate(self):
        self.assertIsNone(self.fps("h264, yuv420p, 640x360, 15360 tbn"))
        self.assertIsNone(self.fps("h264, yuv420p, 640x360, 90k tbn"))

    def test_nonsense_rates_are_dropped(self):
        self.assertIsNone(self.fps("h264, yuv420p, 640x360, 0 fps, 0 tbr"))
        self.assertIsNone(self.fps("h264, yuv420p, 640x360, 5000 fps"))

    def test_no_rate_at_all(self):
        self.assertIsNone(self.fps("h264, yuv420p, 640x360"))

    def test_audio_only_files_have_no_frame_rate(self):
        info = parse_media_info("  Duration: 00:00:10.00\n  Stream #0:0: Audio: mp3, 44100 Hz, stereo, 25 fps\n")
        self.assertIsNone(info.fps)

    def test_the_rate_comes_from_the_video_stream_line(self):
        text = ("  Duration: 00:00:04.00\n"
                "  Stream #0:0: Audio: aac, 48000 Hz, stereo, fltp, 128 kb/s\n"
                "  Stream #0:1: Video: h264, yuv420p, 640x360, 24 fps, 24 tbr, 12288 tbn\n")
        self.assertEqual(parse_media_info(text).fps, 24.0)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class ProbedFrameRateTests(unittest.TestCase):
    def test_probe_reads_the_real_frame_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            make_video(FFMPEG, source)               # 10 fps
            self.assertEqual(probe_media(FFMPEG, source).fps, 10.0)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class ConvertTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_converts_video_to_mp3(self):
        source = self.tmp / "sample video.mp4"
        make_video(FFMPEG, source)
        progress = []
        output = convert(FFMPEG, ConversionJob(source, self.tmp / "sample video.mp3"), on_progress=progress.append)

        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 0)
        self.assertAlmostEqual(probe_duration(FFMPEG, output), 3.0, delta=0.3)
        self.assertEqual(progress[-1], 1.0)

    def test_video_without_audio_fails_cleanly(self):
        source = self.tmp / "silent.mp4"
        make_video(FFMPEG, source, audio=False)
        output = self.tmp / "silent.mp3"
        with self.assertRaisesRegex(ConversionError, "no audio track"):
            convert(FFMPEG, ConversionJob(source, output))
        self.assertFalse(output.exists())

    def test_missing_file(self):
        with self.assertRaises(ConversionError):
            convert(FFMPEG, ConversionJob(self.tmp / "nope.mp4", self.tmp / "nope.mp3"))

    def test_cancel_removes_partial_output(self):
        source = self.tmp / "long.mp4"
        make_video(FFMPEG, source, seconds=20)
        output = self.tmp / "long.mp3"
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ConversionCancelled):
            convert(FFMPEG, ConversionJob(source, output), cancel_event=cancel)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
