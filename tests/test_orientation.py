import subprocess
import tempfile
import unittest
from pathlib import Path

from video_converter.core import find_ffmpeg, parse_media_info, probe_media

FFMPEG = find_ffmpeg()


def stream_text(rotation_line: str, size: str = "1920x1080") -> str:
    return f"""
  Duration: 00:00:10.00, start: 0.000000, bitrate: 3000 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, {size} [SAR 1:1 DAR 16:9], 30 fps
{rotation_line}
  Stream #0:1[0x2](und): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s
"""


class RotationParsingTests(unittest.TestCase):
    def test_ffmpeg_angles_are_counter_clockwise(self):
        # "rotation of -90" means players turn the stored frame 90 degrees clockwise.
        cases = {
            "      displaymatrix: rotation of -90.00 degrees": 90,
            "      displaymatrix: rotation of 90.00 degrees": 270,
            "      displaymatrix: rotation of 180.00 degrees": 180,
            "      displaymatrix: rotation of -180.00 degrees": 180,
            "      displaymatrix: rotation of 270.00 degrees": 90,
            "      displaymatrix: rotation of -270.00 degrees": 270,
            "      displaymatrix: rotation of 0.00 degrees": 0,
            "      displaymatrix: rotation of 360.00 degrees": 0,
        }
        for line, expected in cases.items():
            with self.subTest(line=line.strip()):
                self.assertEqual(parse_media_info(stream_text(line)).display_rotation, expected)

    def test_newer_ffmpeg_prints_display_matrix(self):
        for line, expected in (
            ("      Display Matrix: rotation of 180.00 degrees", 180),
            ("      Display Matrix: rotation of -90.00 degrees", 90),
            ("      Display Matrix: rotation of 90.00 degrees", 270),
        ):
            with self.subTest(line=line.strip()):
                text = stream_text("    Side data:\n" + line)
                self.assertEqual(parse_media_info(text).display_rotation, expected)

    def test_slightly_off_angles_snap_to_the_nearest_quarter_turn(self):
        for angle, expected in (("-89.60", 90), ("-90.40", 90), ("90.30", 270), ("179.70", 180), ("0.40", 0)):
            with self.subTest(angle=angle):
                text = stream_text(f"      displaymatrix: rotation of {angle} degrees")
                self.assertEqual(parse_media_info(text).display_rotation, expected)

    def test_no_rotation_metadata(self):
        info = parse_media_info(stream_text("      Metadata:\n        handler_name: VideoHandler"))
        self.assertEqual(info.display_rotation, 0)
        self.assertEqual((info.width, info.height), (1920, 1080))

    def test_display_size_follows_the_rotation(self):
        for angle, expected in (("-90.00", (1080, 1920)), ("90.00", (1080, 1920)), ("180.00", (1920, 1080)),
                                ("0.00", (1920, 1080))):
            with self.subTest(angle=angle):
                info = parse_media_info(stream_text(f"      displaymatrix: rotation of {angle} degrees"))
                self.assertEqual((info.width, info.height), expected)
                self.assertTrue(info.has_video)

    def test_audio_only_file_has_no_rotation(self):
        info = parse_media_info("  Duration: 00:00:10.00\n  Stream #0:0: Audio: mp3, 44100 Hz\n")
        self.assertEqual(info.display_rotation, 0)
        self.assertFalse(info.has_video)

    def test_rotation_of_the_audio_stream_text_is_not_used_when_there_is_no_video(self):
        text = "  Duration: 00:00:10.00\n  Stream #0:0: Audio: mp3, 44100 Hz\n    displaymatrix: rotation of 90.00 degrees\n"
        self.assertEqual(parse_media_info(text).display_rotation, 0)

    def test_rotation_of_a_later_stream_is_not_applied_to_the_first_video(self):
        # Suspected bug: core.py parse_media_info searches for "rotation of ..." in all the text after the
        # first video stream, so rotation metadata belonging to a LATER stream (here a second video
        # track such as embedded cover art) is attributed to the first one.
        # Input: first video stream has no rotation, second video stream says "rotation of -90.00 degrees".
        # Expected display_rotation == 0 and 1920x1080; actual 90 and 1080x1920.
        text = """
  Duration: 00:00:10.00, start: 0.000000, bitrate: 3000 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, 1920x1080 [SAR 1:1 DAR 16:9], 30 fps
  Stream #0:1[0x2](und): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s
  Stream #0:2[0x3](und): Video: mjpeg, yuvj420p, 600x600, 90k tbr
      Side data:
        displaymatrix: rotation of -90.00 degrees
"""
        info = parse_media_info(text)
        self.assertEqual(info.display_rotation, 0)
        self.assertEqual((info.width, info.height), (1920, 1080))


# ----- checking the direction against ffmpeg itself -----------------------------

def rotate_clockwise(frame: bytes, width: int, height: int, degrees: int) -> tuple[bytes, int, int]:
    """Turn a packed RGB24 frame clockwise by 0, 90, 180 or 270 degrees."""
    pixels = [frame[i:i + 3] for i in range(0, len(frame), 3)]
    rows = [pixels[y * width:(y + 1) * width] for y in range(height)]
    if degrees == 0:
        out = rows
    elif degrees == 90:
        out = [[rows[height - 1 - c][r] for c in range(height)] for r in range(width)]
    elif degrees == 180:
        out = [row[::-1] for row in rows[::-1]]
    elif degrees == 270:
        out = [[rows[c][width - 1 - r] for c in range(height)] for r in range(width)]
    else:
        raise ValueError(degrees)
    return b"".join(b"".join(row) for row in out), len(out[0]), len(out)


def mean_difference(a: bytes, b: bytes) -> float:
    assert len(a) == len(b)
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def raw_frame(ffmpeg, source: Path, auto_rotate: bool, size: tuple[int, int]) -> bytes:
    """The first frame as packed RGB24, with or without ffmpeg applying rotation metadata."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if not auto_rotate:
        cmd.append("-noautorotate")
    cmd += ["-i", str(source), "-frames:v", "1", "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    data = subprocess.run(cmd, capture_output=True, check=True).stdout
    assert len(data) == size[0] * size[1] * 3, (len(data), size)
    return data


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class DisplayRotationMatchesFfmpegTests(unittest.TestCase):
    WIDTH, HEIGHT = 320, 180

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.plain = cls.tmp / "plain.mp4"
        # testsrc2 has no symmetry, so a wrong direction cannot look right.
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", f"testsrc2=duration=1:size={cls.WIDTH}x{cls.HEIGHT}:rate=10",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.plain)],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def tagged(self, rotation) -> Path:
        path = self.tmp / f"rot_{str(rotation).replace('-', 'm')}.mp4"
        if not path.exists():
            subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", str(rotation),
                 "-i", str(self.plain), "-c", "copy", str(path)],
                check=True,
            )
        return path

    def check(self, rotation, expected_cw):
        source = self.tagged(rotation)
        info = probe_media(FFMPEG, source)
        self.assertEqual(info.display_rotation, expected_cw, f"-display_rotation {rotation}")

        raw_w, raw_h = self.WIDTH, self.HEIGHT
        shown_w, shown_h = (raw_h, raw_w) if expected_cw in (90, 270) else (raw_w, raw_h)
        self.assertEqual((info.width, info.height), (shown_w, shown_h))

        auto = raw_frame(FFMPEG, source, True, (shown_w, shown_h))
        raw = raw_frame(FFMPEG, source, False, (raw_w, raw_h))

        turned, turned_w, turned_h = rotate_clockwise(raw, raw_w, raw_h, info.display_rotation)
        self.assertEqual((turned_w, turned_h), (shown_w, shown_h))
        # Chroma is resampled in a different order for the two paths, so allow a little noise.
        difference = mean_difference(auto, turned)
        self.assertLess(difference, 3.0, f"rotation {rotation}: frames differ by {difference:.1f}")

        # The test pattern is distinctive enough that the opposite turn is clearly different.
        if expected_cw:
            other, _, _ = rotate_clockwise(raw, raw_w, raw_h, (expected_cw + 180) % 360)
            self.assertGreater(mean_difference(auto, other), 10 * max(difference, 0.3))

    def test_no_rotation(self):
        info = probe_media(FFMPEG, self.plain)
        self.assertEqual(info.display_rotation, 0)
        self.assertEqual((info.width, info.height), (self.WIDTH, self.HEIGHT))
        self.assertEqual(raw_frame(FFMPEG, self.plain, True, (self.WIDTH, self.HEIGHT)),
                         raw_frame(FFMPEG, self.plain, False, (self.WIDTH, self.HEIGHT)))

    def test_display_rotation_90(self):
        self.check(90, 270)

    def test_display_rotation_minus_90(self):
        self.check(-90, 90)

    def test_display_rotation_270(self):
        self.check(270, 90)

    def test_display_rotation_180(self):
        self.check(180, 180)

    def test_clockwise_helper_is_correct(self):
        # 2 wide, 3 tall; each pixel is one distinct byte triple.
        frame = bytes(b for n in range(6) for b in (n, n, n))
        # rows: [0 1] [2 3] [4 5]
        turned, w, h = rotate_clockwise(frame, 2, 3, 90)
        self.assertEqual((w, h), (3, 2))
        self.assertEqual(turned[::3], bytes([4, 2, 0, 5, 3, 1]))
        turned, w, h = rotate_clockwise(frame, 2, 3, 270)
        self.assertEqual((w, h), (3, 2))
        self.assertEqual(turned[::3], bytes([1, 3, 5, 0, 2, 4]))
        turned, w, h = rotate_clockwise(frame, 2, 3, 180)
        self.assertEqual((w, h), (2, 3))
        self.assertEqual(turned[::3], bytes([5, 4, 3, 2, 1, 0]))
        self.assertEqual(rotate_clockwise(frame, 2, 3, 0)[0], frame)


if __name__ == "__main__":
    unittest.main()
