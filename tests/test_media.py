import subprocess
import tempfile
import unittest
from array import array
from pathlib import Path

from video_converter.core import find_ffmpeg, probe_media
from video_converter.media import (
    extract_thumbnails,
    extract_waveform,
    peaks,
    thumbnail_times,
    waveform_sample_rate,
)

FFMPEG = find_ffmpeg()


def make_clip(ffmpeg, path, seconds=3, audio="sine", size="320x180"):
    """A short video; `audio` is "sine", "silence" or None (no audio track)."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={size}:rate=10"]
    if audio == "sine":
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    elif audio == "silence":
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)


class ThumbnailTimesTests(unittest.TestCase):
    def test_count_and_spacing(self):
        times = thumbnail_times(12.0, 4)
        self.assertEqual(len(times), 4)
        self.assertEqual(times, [1.5, 4.5, 7.5, 10.5])
        gaps = [b - a for a, b in zip(times, times[1:])]
        for gap in gaps:
            self.assertAlmostEqual(gap, 3.0)

    def test_times_are_strictly_inside_the_video_and_sorted(self):
        for duration, count in ((0.5, 1), (1.0, 12), (65.5, 12), (3600.0, 40), (0.01, 7)):
            with self.subTest(duration=duration, count=count):
                times = thumbnail_times(duration, count)
                self.assertEqual(len(times), count)
                self.assertEqual(times, sorted(times))
                self.assertTrue(all(0 < t < duration for t in times))

    def test_single_thumbnail_is_the_middle(self):
        self.assertEqual(thumbnail_times(10.0, 1), [5.0])

    def test_empty_for_unusable_input(self):
        for duration, count in ((0, 5), (-3.0, 5), (10.0, 0), (10.0, -2), (0, 0)):
            with self.subTest(duration=duration, count=count):
                self.assertEqual(thumbnail_times(duration, count), [])


class WaveformSampleRateTests(unittest.TestCase):
    def test_clamped_to_range(self):
        self.assertEqual(waveform_sample_rate(100000.0, 600), 100)      # long video: floor
        self.assertEqual(waveform_sample_rate(0.1, 600), 8000)          # short video: ceiling
        self.assertEqual(waveform_sample_rate(10.0, 600), 8000)         # 12000 -> 8000
        self.assertEqual(waveform_sample_rate(600.0, 600), 200)         # exactly in range

    def test_missing_or_bad_duration_uses_the_maximum(self):
        for duration in (None, 0, 0.0, -5.0):
            with self.subTest(duration=duration):
                self.assertEqual(waveform_sample_rate(duration, 600), 8000)

    def test_always_an_int_in_range(self):
        for duration in (0.3, 1.0, 7.7, 59.9, 3600.0, 86400.0):
            for buckets in (1, 10, 600, 5000):
                with self.subTest(duration=duration, buckets=buckets):
                    rate = waveform_sample_rate(duration, buckets)
                    self.assertIsInstance(rate, int)
                    self.assertGreaterEqual(rate, 100)
                    self.assertLessEqual(rate, 8000)

    def test_zero_buckets_does_not_crash(self):
        self.assertEqual(waveform_sample_rate(10.0, 0), 100)


class PeaksTests(unittest.TestCase):
    def test_number_of_buckets_and_range(self):
        samples = array("h", [(i * 7919) % 65536 - 32768 for i in range(10_000)])
        for buckets in (1, 3, 600, 9_999, 10_000):
            with self.subTest(buckets=buckets):
                result = peaks(samples, buckets)
                self.assertEqual(len(result), buckets)
                self.assertTrue(all(0.0 <= v <= 1.0 for v in result))

    def test_silence_is_zero(self):
        self.assertEqual(peaks(array("h", [0] * 1000), 10), [0.0] * 10)

    def test_full_scale_negative_is_exactly_one(self):
        self.assertEqual(peaks(array("h", [-32768] * 100), 4), [1.0] * 4)

    def test_full_scale_positive_is_just_under_one(self):
        result = peaks(array("h", [32767] * 100), 4)
        for v in result:
            self.assertAlmostEqual(v, 1.0, places=3)
            self.assertLessEqual(v, 1.0)

    def test_sign_does_not_matter(self):
        self.assertEqual(peaks([1000, -1000], 2), peaks([-1000, 1000], 2))
        self.assertEqual(peaks([-16384] * 8, 2), [0.5, 0.5])

    def test_peak_lands_in_the_right_bucket(self):
        samples = [0] * 100
        samples[55] = 16384
        result = peaks(samples, 10)
        self.assertEqual(result[5], 0.5)
        self.assertEqual(sum(1 for v in result if v), 1)

    def test_takes_the_maximum_not_the_average(self):
        self.assertEqual(peaks([0, 0, 0, 8192], 1), [0.25])

    def test_every_sample_belongs_to_a_bucket(self):
        # Each sample is the loudest in turn; whatever the split, its bucket must report it.
        for length, buckets in ((7, 3), (10, 3), (100, 7), (13, 13), (1000, 600)):
            for i in range(length):
                with self.subTest(length=length, buckets=buckets, index=i):
                    samples = [0] * length
                    samples[i] = 32768
                    self.assertIn(1.0, peaks(samples, buckets))

    def test_more_buckets_than_samples(self):
        result = peaks([0, 32768, 16384], 10)
        self.assertEqual(len(result), 10)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in result))
        self.assertEqual(max(result), 1.0)
        self.assertEqual(peaks([-32768], 5), [1.0] * 5)

    def test_empty_or_invalid_input(self):
        self.assertEqual(peaks(array("h"), 10), [])
        self.assertEqual(peaks([], 10), [])
        self.assertEqual(peaks([1, 2, 3], 0), [])
        self.assertEqual(peaks([1, 2, 3], -4), [])

    def test_accepts_lists_and_arrays(self):
        self.assertEqual(peaks([100, 200, 300, 400], 2), peaks(array("h", [100, 200, 300, 400]), 2))


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class FfmpegMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.tone = cls.tmp / "tone.mp4"
        cls.silent = cls.tmp / "silent.mp4"
        cls.no_audio = cls.tmp / "none.mp4"
        make_clip(FFMPEG, cls.tone, audio="sine")
        make_clip(FFMPEG, cls.silent, audio="silence")
        make_clip(FFMPEG, cls.no_audio, audio=None)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ----- thumbnails -----------------------------------------------------------

    def test_thumbnails_are_jpegs_of_the_requested_height(self):
        for count, height in ((5, 72), (3, 48)):
            with self.subTest(count=count, height=height):
                out = self.tmp / f"thumbs-{count}-{height}"
                paths = extract_thumbnails(FFMPEG, self.tone, out, 3.0, count=count, height=height)
                self.assertEqual(len(paths), count)
                self.assertEqual(paths, sorted(paths))
                for path in paths:
                    self.assertTrue(path.is_file())
                    self.assertEqual(path.suffix, ".jpg")
                    self.assertEqual(path.read_bytes()[:2], b"\xff\xd8")     # JPEG start-of-image marker
                    info = probe_media(FFMPEG, path)
                    self.assertEqual(info.height, height)
                    self.assertEqual(info.width, round(height * 320 / 180 / 2) * 2)

    def test_thumbnails_come_from_different_moments(self):
        paths = extract_thumbnails(FFMPEG, self.tone, self.tmp / "moments", 3.0, count=4)
        contents = {p.read_bytes() for p in paths}
        self.assertEqual(len(contents), 4)

    def test_creates_the_output_folder(self):
        out = self.tmp / "a" / "b" / "thumbs"
        paths = extract_thumbnails(FFMPEG, self.tone, out, 3.0, count=2)
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(p.parent == out for p in paths))

    def test_nothing_to_do_for_unusable_duration_or_count(self):
        out = self.tmp / "empty"
        self.assertEqual(extract_thumbnails(FFMPEG, self.tone, out, 0, count=5), [])
        self.assertEqual(extract_thumbnails(FFMPEG, self.tone, out, 3.0, count=0), [])

    def test_missing_source_gives_no_thumbnails(self):
        self.assertEqual(extract_thumbnails(FFMPEG, self.tmp / "nope.mp4", self.tmp / "none", 3.0, count=3), [])

    # ----- waveform -------------------------------------------------------------

    def test_waveform_of_a_sine_tone(self):
        for buckets in (60, 100):
            with self.subTest(buckets=buckets):
                levels = extract_waveform(FFMPEG, self.tone, 3.0, buckets=buckets)
                self.assertEqual(len(levels), buckets)
                self.assertTrue(all(0.0 <= v <= 1.0 for v in levels))
                # ffmpeg's default sine is about 1/8 of full scale.
                loud = [v for v in levels if v > 0.05]
                self.assertGreaterEqual(len(loud), len(levels) * 0.9)
                self.assertLess(max(levels), 0.4)

    def test_waveform_of_silence_is_flat(self):
        levels = extract_waveform(FFMPEG, self.silent, 3.0, buckets=60)
        self.assertEqual(len(levels), 60)
        self.assertLess(max(levels), 0.01)

    def test_waveform_without_audio_track_is_empty(self):
        self.assertEqual(extract_waveform(FFMPEG, self.no_audio, 3.0, buckets=60), [])

    def test_waveform_of_missing_file_is_empty(self):
        self.assertEqual(extract_waveform(FFMPEG, self.tmp / "nope.mp4", 3.0, buckets=60), [])

    def test_waveform_with_unknown_duration(self):
        levels = extract_waveform(FFMPEG, self.tone, None, buckets=50)
        self.assertEqual(len(levels), 50)
        self.assertGreater(max(levels), 0.05)


if __name__ == "__main__":
    unittest.main()
