import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from mp4_to_mp3.core import (
    ConversionCancelled,
    ConversionError,
    ConversionJob,
    convert,
    find_ffmpeg,
    output_path_for,
    probe_duration,
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
