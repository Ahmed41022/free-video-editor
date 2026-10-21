"""Regression tests for the engine / export / audio fixes of 3.0.1: frame rate and level of the exports,
joins with rotated clips, loudness of joins, long filter graphs, output names and part files, stopping ffmpeg,
the limiter, noise reduction, cuts, trims and the rest. Each test failed before its fix."""

import io
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from array import array
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path
from unittest import mock

from video_converter import core, gpu, output, sound, video
from video_converter.__main__ import run_cli
from video_converter.core import (
    ConversionCancelled,
    ConversionError,
    ConversionJob,
    MediaInfo,
    convert,
    find_ffmpeg,
    output_path_for,
    parse_media_info,
    parse_time,
    probe_media,
    run_ffmpeg,
    start_process,
    staged_output,
    stop_process,
)
from video_converter.sound import Sound
from video_converter.stills import save_frame
from video_converter.timing import audio_filters, snap_segments
from video_converter.video import (
    UP_TO_1080,
    JoinJob,
    JoinPart,
    VideoEdit,
    VideoJob,
    convert_video,
    join_clips,
    join_command,
    reverse_memory,
    steady_rate,
    video_command,
)

FFMPEG = find_ffmpeg()


def ff(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True)


def text_of(*args):
    return subprocess.run([FFMPEG, "-hide_banner", "-nostats", *map(str, args)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stderr


def cmd_for(edit, info, job_output="out.mp4"):
    return video_command("ffmpeg", VideoJob(Path("in.mp4"), Path(job_output), edit), info)


def after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def loudness(path):
    """(integrated LUFS, true peak dBTP) of the sound of `path`."""
    report = text_of("-i", path, "-vn", "-af", "ebur128=peak=true", "-f", "null", "-")
    summary = report[report.rindex("Summary:"):]
    return (float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1)),
            float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", summary).group(1)))


def level_idc(path, box, offset):
    """The level an MP4 says its picture needs: the byte `offset` into the avcC (H.264) or hvcC (H.265) box."""
    data = Path(path).read_bytes()
    start = data.index(box) + 4
    return data[start + offset]


def gray_frames(path, size="16x16", at=None, count=None):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    if at is not None:
        cmd += ["-ss", str(at)]
    cmd += ["-i", str(path)]
    if count:
        cmd += ["-frames:v", str(count)]
    cmd += ["-vf", f"scale={size},format=gray", "-f", "rawvideo", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def difference(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def counter_clip(path, seconds=4, rate="15"):
    """A tiny clip whose frame number N is its brightness (N * 4), so the frames an export kept can be read."""
    ff("-f", "lavfi", "-i", f"color=gray:size=16x16:rate={rate}:duration={seconds},format=gray,geq=lum='mod(N,64)*4'",
       "-c:v", "libx264", "-qp", "0", "-pix_fmt", "gray", path)


def frame_numbers(path):
    data = gray_frames(path, "16x16")
    return [round(data[i] / 4) for i in range(0, len(data), 256)]


def count_frames(path):
    result = text_of("-i", path, "-map", "0:v:0", "-c", "copy", "-f", "null", "-")
    return int(re.findall(r"frame=\s*(\d+)", result)[-1])


def sample_clip(path, seconds=3, size="160x90", rate=15, audio=True, rotation=None):
    cmd = ["-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={size}:rate={rate}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    ff(*cmd, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", path)
    if rotation is not None:
        rotated = Path(path).with_name("rotated-" + Path(path).name)
        ff("-display_rotation", rotation, "-i", path, "-c", "copy", rotated)
        rotated.replace(path)


def info_of(width, height, fps, **kwargs):
    return MediaInfo(duration=6.0, width=width, height=height, has_audio=True, fps=fps, **kwargs)


# ----- 1. the level and the frame rate of an export ---------------------------------------------

class LevelCommandTests(unittest.TestCase):
    def test_1080p30_is_level_4_1_and_says_its_rate(self):
        cmd = cmd_for(VideoEdit(aspect=None), info_of(1920, 1080, 30.0))
        self.assertEqual((after(cmd, "-level"), after(cmd, "-profile:v"), after(cmd, "-r")), ("4.1", "high", "30"))

    def test_1080p60_is_level_4_2(self):
        cmd = cmd_for(VideoEdit(aspect=None), info_of(1920, 1080, 60.0))
        self.assertEqual((after(cmd, "-level"), after(cmd, "-r")), ("4.2", "60"))

    def test_ntsc_rates_are_exact(self):
        cmd = cmd_for(VideoEdit(aspect=None), info_of(1920, 1080, 29.97))
        self.assertEqual((after(cmd, "-r"), after(cmd, "-level")), ("30000/1001", "4.1"))

    def test_a_mov_is_h264_with_a_level_too(self):
        cmd = cmd_for(VideoEdit(aspect=None, video_format="mov"), info_of(1920, 1080, 60.0), "out.mov")
        self.assertEqual((after(cmd, "-level"), after(cmd, "-r")), ("4.2", "60"))

    def test_h265_is_given_its_frame_rate(self):
        cmd = cmd_for(VideoEdit(aspect=None, video_format="hevc"), info_of(1920, 1080, 30.0))
        self.assertEqual(after(cmd, "-r"), "30")
        self.assertNotIn("-level", cmd)

    def test_a_changing_rate_keeps_changing_but_says_the_level_it_needs(self):
        vfr = info_of(1920, 1080, 57.97, max_fps=120.0)
        cmd = cmd_for(VideoEdit(aspect=None), vfr)
        self.assertNotIn("-r", cmd)
        self.assertEqual(after(cmd, "-level"), "5.1")
        cmd = cmd_for(VideoEdit(aspect=None, video_format="hevc"), vfr)
        self.assertIn("level-idc=5.0", after(cmd, "-x265-params"))

    def test_h265_levels(self):
        self.assertEqual(video.h265_level(1920, 1080, 30.0), "4.0")
        self.assertEqual(video.h265_level(1920, 1080, 60.0), "4.1")
        self.assertEqual(video.h265_level(3840, 2160, 60.0), "5.1")

    def test_a_join_says_its_level_and_rate_too(self):
        parts = [JoinPart(Path("a.mp4"), VideoEdit(aspect=None), info_of(1920, 1080, 30.0)),
                 JoinPart(Path("b.mp4"), VideoEdit(), info_of(1920, 1080, 30.0))]
        cmd, _ = join_command("ffmpeg", JoinJob(parts, Path("out.mp4")))
        self.assertEqual((after(cmd, "-level"), after(cmd, "-r")), ("4.1", "30"))


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class LevelExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "clip.mp4"
        sample_clip(cls.clip, 2, "320x180", 30)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def export(self, name, **edit):
        out = self.tmp / name
        convert_video(FFMPEG, VideoJob(self.clip, out, VideoEdit(aspect=None, use_gpu=False, **edit)))
        return out

    def test_the_default_mp4_is_not_flagged_as_level_6_2(self):
        self.assertEqual(level_idc(self.export("a.mp4"), b"avcC", 3), 41)         # 6.2 was 62

    def test_a_mov_is_not_either(self):
        self.assertEqual(level_idc(self.export("a.mov", video_format="mov"), b"avcC", 3), 41)

    def test_h265_is_not_flagged_as_level_6_1(self):
        level = level_idc(self.export("h.mp4", video_format="hevc"), b"hvcC", 12)
        self.assertLessEqual(level, 4 * 30)             # 6.1 was 183


# ----- 2. joins with a rotated clip ------------------------------------------------------------

class JoinRotationCommandTests(unittest.TestCase):
    def test_a_rotated_clip_is_opened_as_stored_and_turned_in_the_graph(self):
        rotated = JoinPart(Path("a.mp4"), VideoEdit(aspect=None), info_of(180, 320, 15.0, display_rotation=90))
        normal = JoinPart(Path("b.mp4"), VideoEdit(), info_of(320, 180, 15.0))
        cmd, _ = join_command("ffmpeg", JoinJob([rotated, normal], Path("out.mp4")))
        first = cmd[:cmd.index("-i")]
        start = first.index("-noautorotate")
        self.assertEqual(first[start:start + 3], ["-noautorotate", "-display_rotation:v:0", "0"])
        self.assertEqual(cmd.count("-noautorotate"), 1)
        self.assertIn("[0:v:0]transpose=1[up0]", after(cmd, "-filter_complex"))

    def test_the_other_turns(self):
        for turn, filters in ((180, "hflip,vflip"), (270, "transpose=2")):
            with self.subTest(turn=turn):
                rotated = JoinPart(Path("a.mp4"), VideoEdit(aspect=None), info_of(320, 180, 15.0, display_rotation=turn))
                normal = JoinPart(Path("b.mp4"), VideoEdit(), info_of(320, 180, 15.0))
                cmd, _ = join_command("ffmpeg", JoinJob([rotated, normal], Path("out.mp4")))
                self.assertIn(f"[0:v:0]{filters}[up0]", after(cmd, "-filter_complex"))


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class JoinRotationExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.normal = cls.tmp / "normal.mp4"
        sample_clip(cls.normal, 2, "320x180", 15)
        cls.rot90 = cls.tmp / "rot90.mp4"
        sample_clip(cls.rot90, 2, "320x180", 15, rotation=90)
        cls.rot270 = cls.tmp / "rot270.mp4"
        sample_clip(cls.rot270, 2, "320x180", 15, rotation=270)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def join(self, name, *sources):
        out = self.tmp / name
        parts = [JoinPart(src, VideoEdit(aspect=None, use_gpu=False)) for src in sources]
        join_clips(FFMPEG, JoinJob(parts, out))
        return out

    def alone(self, source):
        """The clip exported on its own, which ffmpeg turns upright the usual way."""
        out = self.tmp / ("alone-" + source.name)
        convert_video(FFMPEG, VideoJob(source, out, VideoEdit(aspect=None, use_gpu=False)))
        return out

    def test_the_result_carries_no_rotation_whatever_the_order(self):
        for name, sources in (("a.mp4", (self.rot90, self.normal)), ("b.mp4", (self.normal, self.rot90)),
                              ("c.mp4", (self.rot270, self.normal))):
            with self.subTest(order=name):
                self.assertEqual(probe_media(FFMPEG, self.join(name, *sources)).display_rotation, 0)

    def test_a_rotated_first_clip_is_upright_in_the_result(self):
        for rotated in (self.rot90, self.rot270):
            with self.subTest(clip=rotated.name):
                out = self.join("up-" + rotated.name, rotated, self.normal)
                info = probe_media(FFMPEG, out)
                self.assertEqual((info.width, info.height), (180, 320))
                self.assertLess(difference(gray_frames(out, "16x16", count=1),
                                           gray_frames(self.alone(rotated), "16x16", count=1)), 8)

    def test_a_rotated_second_clip_is_upright_in_the_result(self):
        out = self.join("second.mp4", self.normal, self.rot90)
        # the rotated clip, fitted into the first one's 320x180 frame between black bars
        reference = self.tmp / "ref.png"
        ff("-i", self.alone(self.rot90), "-frames:v", 1, "-vf", "scale=-2:180,pad=320:180:(ow-iw)/2:0", reference)
        self.assertLess(difference(gray_frames(out, "32x18", at=2.2, count=1),
                                   gray_frames(reference, "32x18")), 8)


# ----- 3. even loudness in a join --------------------------------------------------------------

@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class JoinLoudnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clips = []
        for i, gain in enumerate((-9, -3)):
            path = cls.tmp / f"quiet{i}.mp4"
            ff("-f", "lavfi", "-i", "testsrc2=duration=4:size=64x36:rate=10", "-f", "lavfi", "-i",
               f"sine=frequency={300 + 200 * i}:duration=4,volume={gain}dB", "-c:v", "libx264", "-c:a", "aac",
               "-pix_fmt", "yuv420p", "-shortest", path)
            cls.clips.append(path)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_joined_video_is_evened_out(self):
        out = self.tmp / "even.mp4"
        parts = [JoinPart(c, VideoEdit(aspect=None, normalize=True, use_gpu=False)) for c in self.clips]
        join_clips(FFMPEG, JoinJob(parts, out))
        self.assertAlmostEqual(loudness(out)[0], -14.0, delta=1.0)

    def test_a_joined_mp3_is_evened_out(self):
        out = self.tmp / "even.mp3"
        parts = [JoinPart(c, VideoEdit(aspect=None, normalize=True)) for c in self.clips]
        join_clips(FFMPEG, JoinJob(parts, out, audio_only=True))
        self.assertAlmostEqual(loudness(out)[0], -14.0, delta=1.0)

    def test_the_measuring_pass_has_the_loudness_filter(self):
        info = info_of(64, 36, 10.0)
        parts = [JoinPart(Path("a.mp4"), VideoEdit(aspect=None, normalize=True), info),
                 JoinPart(Path("b.mp4"), VideoEdit(), info)]
        cmd, _ = join_command("ffmpeg", JoinJob(parts, Path("o.mp4")), measure=True)
        self.assertIn("loudnorm=I=-14", after(cmd, "-filter_complex"))


# ----- 4. long filter graphs and a command line that doesn't start ------------------------------

def many_ranges(count, length=0.2, gap=0.2):
    return [(round(i * (length + gap), 3), round(i * (length + gap) + length, 3)) for i in range(count)]


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class LongGraphTests(unittest.TestCase):
    COUNT = 400

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "long.mp4"
        sample_clip(cls.clip, 170, "64x36", 15)           # 400 pieces of 0.2 s with 0.2 s between

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_command_is_longer_than_windows_allows(self):
        info = probe_media(FFMPEG, self.clip)
        cmd = video_command(FFMPEG, VideoJob(self.clip, self.tmp / "x.mp4",
                                             VideoEdit(aspect=None, keep=many_ranges(self.COUNT))), info)
        self.assertGreater(len(subprocess.list2cmdline(cmd)), 32767)

    def test_400_cuts_make_a_video(self):
        out = self.tmp / "cuts.mp4"
        convert_video(FFMPEG, VideoJob(self.clip, out, VideoEdit(aspect=None, use_gpu=False,
                                                                  keep=many_ranges(self.COUNT))))
        self.assertAlmostEqual(probe_media(FFMPEG, out).duration, self.COUNT * 0.2, delta=1.5)

    def test_400_cuts_make_an_mp3(self):
        out = self.tmp / "cuts.mp3"
        convert(FFMPEG, ConversionJob(self.clip, out, keep=many_ranges(self.COUNT)))
        self.assertAlmostEqual(probe_media(FFMPEG, out).duration, self.COUNT * 0.2, delta=1.5)

    def test_400_cuts_in_a_join_and_in_its_loudness_pass(self):
        out = self.tmp / "join.mp4"
        parts = [JoinPart(self.clip, VideoEdit(aspect=None, use_gpu=False, normalize=True, keep=many_ranges(200))),
                 JoinPart(self.clip, VideoEdit(aspect=None, keep=many_ranges(200)))]
        join_clips(FFMPEG, JoinJob(parts, out))
        self.assertAlmostEqual(probe_media(FFMPEG, out).duration, 400 * 0.2, delta=1.5)

    def test_a_long_graph_goes_in_a_file_and_a_short_one_stays_on_the_line(self):
        short = [FFMPEG, "-i", "in.mp4", "-filter_complex", "null", "out.mp4"]
        self.assertFalse(core.has_long_graph(short))
        long_graph = [FFMPEG, "-i", "in.mp4", "-filter_complex", "null;" * 1000, "out.mp4"]
        self.assertTrue(core.has_long_graph(long_graph))
        with core.graph_files(long_graph) as run_cmd:
            flag = run_cmd.index(run_cmd[3])
            self.assertIn(run_cmd[3], ("-/filter_complex", "-filter_complex_script"))
            self.assertEqual(Path(run_cmd[4]).read_text(encoding="utf-8"), "null;" * 1000)
            self.assertLess(len(subprocess.list2cmdline(run_cmd)), 500)
            folder = Path(run_cmd[4]).parent
        self.assertFalse(folder.exists())          # the temporary file is gone after the run

    def test_a_command_that_cannot_start_is_a_conversion_error(self):
        too_long = OSError(22, "The filename or extension is too long")
        too_long.winerror = 206
        with mock.patch.object(core.subprocess, "Popen", side_effect=too_long):
            with self.assertRaises(ConversionError) as caught:
                run_ffmpeg([FFMPEG, "-i", "x"], self.tmp / "o.mp4", None)
        self.assertIn("too complicated", str(caught.exception))
        with mock.patch.object(core.subprocess, "Popen", side_effect=FileNotFoundError("no ffmpeg")):
            with self.assertRaises(ConversionError) as caught:
                run_ffmpeg(["nope", "-i", "x"], self.tmp / "o.mp4", None)
        self.assertIn("Couldn't start ffmpeg", str(caught.exception))


# ----- 5 and 6. names of the results, and part files --------------------------------------------

class OutputNameTests(unittest.TestCase):
    def test_overwrite_never_picks_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "song.mp3"
            source.touch()
            target = output_path_for(source, overwrite=True)
            self.assertNotEqual(target, source)
            self.assertEqual(target.name, "song (1).mp3")
            self.assertEqual(output_path_for(Path(tmp) / "SONG.MP3", overwrite=True).name, "SONG (1).mp3")

    def test_the_other_inputs_and_the_outputs_so_far_are_not_picked_either(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            first = output_path_for(Path("a/clip.mp4"), out, overwrite=True)
            second = output_path_for(Path("b/clip.mp4"), out, overwrite=True, avoid=[first])
            self.assertEqual((first.name, second.name), ("clip.mp3", "clip (1).mp3"))
            other_input = out / "clip.mp3"
            self.assertEqual(output_path_for(Path("c/clip.mp4"), out, overwrite=True, avoid=[other_input]).name,
                             "clip (1).mp3")


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class OutputFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clip = self.tmp / "clip.mp4"
        sample_clip(self.clip, 2, "64x36", 10)
        self.mp3 = self.tmp / "song.mp3"
        ff("-f", "lavfi", "-i", "sine=duration=2", self.mp3)

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_export_into_its_own_source_is_refused_and_the_source_survives(self):
        before = self.mp3.read_bytes()
        with self.assertRaises(ConversionError):
            convert(FFMPEG, ConversionJob(self.mp3, self.mp3))
        self.assertEqual(self.mp3.read_bytes(), before)
        with self.assertRaises(ConversionError):
            convert_video(FFMPEG, VideoJob(self.clip, self.clip, VideoEdit(aspect=None)))
        self.assertTrue(self.clip.is_file())

    def test_a_failing_ffmpeg_never_deletes_a_file_it_was_reading(self):
        # even a command that names its input as its output
        with self.assertRaises(ConversionError):
            run_ffmpeg([FFMPEG, "-y", "-i", str(self.mp3), str(self.mp3)], self.mp3, None)
        self.assertTrue(self.mp3.is_file())

    def test_the_cli_with_overwrite_keeps_an_mp3_source(self):
        before = self.mp3.read_bytes()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run_cli([str(self.mp3), "--overwrite"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(self.mp3.read_bytes(), before)
        self.assertTrue((self.tmp / "song (1).mp3").is_file())

    def test_the_cli_with_overwrite_gives_files_of_one_name_their_own_results(self):
        (self.tmp / "a").mkdir()
        (self.tmp / "b").mkdir()
        for folder in "ab":
            sample_clip(self.tmp / folder / "clip.mp4", 1, "64x36", 10)
        results = self.tmp / "results"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run_cli([str(self.tmp / "a" / "clip.mp4"), str(self.tmp / "b" / "clip.mp4"), "-o", str(results),
                            "--overwrite"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(sorted(p.name for p in results.iterdir()), ["clip (1).mp3", "clip.mp3"])

    def test_the_result_is_written_as_a_part_file_and_renamed_when_it_is_done(self):
        final = self.tmp / "result.mp3"
        seen = {}

        def fake(cmd, target, duration, on_progress=None, cancel_event=None, cwd=None, report=None):
            seen["names"] = sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("result"))
            seen["target"] = target.name
            target.write_bytes(b"done")
            return target

        with mock.patch.object(core, "run_ffmpeg", fake):
            self.assertEqual(convert(FFMPEG, ConversionJob(self.mp3, final)), final)
        self.assertEqual(seen["target"], "result.part.mp3")
        self.assertEqual(seen["names"], ["result.mp3", "result.part.mp3"])        # the name is held while it runs
        self.assertEqual(final.read_bytes(), b"done")
        self.assertFalse((self.tmp / "result.part.mp3").exists())

    def test_a_failed_or_cancelled_export_leaves_nothing(self):
        final = self.tmp / "result.mp3"
        for error in (ConversionError("boom"), ConversionCancelled()):
            def fake(cmd, target, *args, **kwargs):
                target.write_bytes(b"half")
                raise error

            with self.subTest(error=type(error).__name__), mock.patch.object(core, "run_ffmpeg", fake):
                with self.assertRaises(type(error)):
                    convert(FFMPEG, ConversionJob(self.mp3, final))
                self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("result")), [])

    def test_a_file_that_is_there_stays_until_the_new_one_replaces_it(self):
        final = self.tmp / "result.mp3"
        final.write_bytes(b"old")
        with self.assertRaises(ConversionError):
            with staged_output(final) as staged:
                self.assertEqual(final.read_bytes(), b"old")
                raise ConversionError("boom")
        self.assertEqual(final.read_bytes(), b"old")
        with staged_output(final) as staged:
            staged.part.write_bytes(b"new")
        self.assertEqual(final.read_bytes(), b"new")

    def test_a_name_taken_meanwhile_is_not_overwritten_unless_asked(self):
        final = self.tmp / "taken.mp3"
        final.write_bytes(b"someone else's")
        with staged_output(final, overwrite=False) as staged:
            staged.part.write_bytes(b"mine")
        self.assertEqual(final.read_bytes(), b"someone else's")
        self.assertEqual(staged.final.name, "taken (1).mp3")
        self.assertEqual(staged.final.read_bytes(), b"mine")

    def test_a_file_size_export_keeps_its_candidates_next_to_the_result_and_leaves_only_the_result(self):
        final = self.tmp / "fit.mp4"
        convert_video(FFMPEG, VideoJob(self.clip, final, VideoEdit(aspect=None, use_gpu=False, target_mb=0.5)))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("fit")), ["fit.mp4"])


class FitProgressTests(unittest.TestCase):
    def test_the_bar_never_goes_back_while_a_size_is_tried_again(self):
        sizes = iter([1_300_000, 700_000, 940_000])
        reported = []

        def fake(cmd, target, duration, on_progress=None, *args, **kwargs):
            if on_progress:
                for fraction in (0.0, 0.5, 1.0):
                    on_progress(fraction)
            target.write_bytes(b"x" * next(sizes))
            return target

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "o.part.mp4"
            fit = output.SizeFit(1_000_000, 1000, 128, 10.0)
            with mock.patch.object(video, "run_ffmpeg", fake):
                result = video._finish(lambda t, e, loudness: ["ffmpeg"], {}, None, None, target, 10.0, reported.append,
                                       None, Path(tmp), None, fit, None)
            self.assertEqual(result, target)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["o.part.mp4"])
        self.assertGreater(len(reported), 3)
        self.assertEqual(reported, sorted(reported))
        self.assertEqual(reported[-1], 1.0)


# ----- 7. stopping ffmpeg ------------------------------------------------------------------------

@unittest.skipUnless(sys.platform == "win32", "job objects are a Windows thing")
class JobObjectTests(unittest.TestCase):
    CHILD = ("import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', "
             "'import time; time.sleep(60)']); time.sleep(60)")

    def test_a_process_is_started_in_a_job_and_the_whole_tree_stops_at_once(self):
        proc = start_process([sys.executable, "-c", self.CHILD], stdout=subprocess.DEVNULL)
        self.assertIsNotNone(proc.fve_job)
        time.sleep(0.5)
        started = time.perf_counter()
        stop_process(proc)
        proc.wait(5)
        self.assertLess(time.perf_counter() - started, 1.0)
        proc.fve_job.close()

    def test_closing_the_job_ends_what_is_in_it(self):
        proc = start_process([sys.executable, "-c", "import time; time.sleep(60)"])
        proc.fve_job.close()
        proc.wait(5)
        self.assertIsNotNone(proc.poll())

    def test_a_process_without_a_job_is_still_stopped_with_its_children(self):
        proc = subprocess.Popen([sys.executable, "-c", self.CHILD])
        time.sleep(0.5)
        stop_process(proc)
        proc.wait(10)
        self.assertIsNotNone(proc.poll())

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_cancelling_a_real_export_is_quick(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.mp4"
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-re", "-f", "lavfi", "-i",
                   "testsrc2=size=320x180:rate=30:duration=30", "-c:v", "libx264", "-preset", "ultrafast",
                   "-progress", "pipe:1", "-nostats", str(out)]
            cancel = threading.Event()
            done = {}

            def go():
                try:
                    run_ffmpeg(cmd, out, 30.0, None, cancel)
                except ConversionCancelled:
                    done["at"] = time.perf_counter()

            thread = threading.Thread(target=go)
            thread.start()
            time.sleep(1.0)
            asked = time.perf_counter()
            cancel.set()
            thread.join(10)
            self.assertLess(done["at"] - asked, 0.8)          # 0.7 s on average before, 0.2 s now
            self.assertFalse(out.exists())


# ----- 8. the limiter ---------------------------------------------------------------------------

class LimiterCommandTests(unittest.TestCase):
    def test_a_boost_music_and_normalizing_end_in_the_limiter(self):
        self.assertEqual(sound.clip_filters(Sound(volume=6.0))[-1], sound.LIMITER)
        self.assertNotIn(sound.LIMITER, sound.clip_filters(Sound(volume=-6.0)))
        self.assertNotIn(sound.LIMITER, sound.clip_filters(Sound()))
        mixed = sound.master_filters(Sound(music="m.mp3"), "[voice]", 1, 10.0, "[a]")
        self.assertTrue(mixed[-1].endswith(f"{sound.LIMITER}[a]"))
        loud = sound.master_filters(Sound(normalize=True), "[voice]", None, 10.0, "[a]",
                                    sound.Loudness(-27.0, -10.0, 5.0, -37.0, 0.0))
        self.assertTrue(loud[-1].endswith(f"aresample=48000,{sound.LIMITER}[a]"))
        measuring = sound.master_filters(Sound(normalize=True), "[voice]", None, 10.0, "[a]", measure=True)
        self.assertNotIn("alimiter", measuring[-1])


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class LimiterExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        # speech-like peaks: a loud burst train that a +6 dB boost pushes far past full scale
        cls.loud = cls.tmp / "loud.mp4"
        ff("-f", "lavfi", "-i", "testsrc2=duration=4:size=64x36:rate=10", "-f", "lavfi", "-i",
           "sine=frequency=330:duration=4,volume=7,tremolo=f=3:d=0.3", "-c:v", "libx264", "-c:a", "aac",
           "-pix_fmt", "yuv420p", "-shortest", cls.loud)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_boost_stays_under_half_a_decibel_of_full_scale_in_an_mp3(self):
        out = self.tmp / "boost.mp3"
        convert(FFMPEG, ConversionJob(self.loud, out, sound=Sound(volume=6.0)))
        self.assertLessEqual(loudness(out)[1], -0.5)

    def test_a_boost_stays_under_half_a_decibel_of_full_scale_in_aac(self):
        out = self.tmp / "boost.mp4"
        convert_video(FFMPEG, VideoJob(self.loud, out, VideoEdit(aspect=None, use_gpu=False, volume=6.0)))
        self.assertLessEqual(loudness(out)[1], -0.5)

    def test_music_on_top_of_a_loud_sound_stays_under_it_too(self):
        out = self.tmp / "music.mp3"
        convert(FFMPEG, ConversionJob(self.loud, out, sound=Sound(volume=6.0, music=str(self.loud), music_volume=0.0)))
        self.assertLessEqual(loudness(out)[1], -0.5)


# ----- 9. noise reduction ------------------------------------------------------------------------

@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class NoiseReductionTests(unittest.TestCase):
    """A voice-like signal (a 140 Hz harmonic tone in syllables) from 2.5 s to 9.5 s over white noise at -36 dB."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.noisy = cls.tmp / "noisy.wav"
        ff("-f", "lavfi", "-i", "aevalsrc='0.22*(sin(2*PI*140*t)+0.6*sin(2*PI*280*t)+0.4*sin(2*PI*560*t)+"
           "0.3*sin(2*PI*1120*t))*gt(sin(2*PI*3.1*t),-0.2)*between(t,2.5,9.5)':s=44100:d=12",
           "-f", "lavfi", "-i", "anoisesrc=d=12:c=white:a=0.5:r=44100:seed=3", "-filter_complex",
           "[1:a]volume=-25dB[n];[0:a][n]amix=inputs=2:normalize=0", "-ac", "1", cls.noisy)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def rms(self, path, start, end):
        report = text_of("-ss", start, "-to", end, "-i", path, "-af", "astats=metadata=1", "-f", "null", "-")
        return float(re.findall(r"RMS level dB:\s*(-?[\d.]+)", report)[-1])

    def cleaned(self, key):
        out = self.tmp / f"clean-{key}.wav"
        ff("-i", self.noisy, "-af", sound.AUDIO_DENOISE[key], out)
        return out

    def test_light_and_strong_take_the_noise_out_of_the_pauses_and_leave_the_voice(self):
        pause_before = self.rms(self.noisy, 0.5, 2.0)
        voice_before = self.rms(self.noisy, 3.0, 9.0)
        reduction = {}
        for key in ("light", "strong"):
            out = self.cleaned(key)
            reduction[key] = pause_before - self.rms(out, 0.5, 2.0)
            self.assertLess(abs(self.rms(out, 3.0, 9.0) - voice_before), 0.5, key)
        self.assertGreaterEqual(reduction["light"], 5.0)
        self.assertGreaterEqual(reduction["strong"], 10.0)
        self.assertGreater(reduction["strong"], reduction["light"] + 5.0)       # Strong is clearly stronger

    def test_the_old_settings_did_next_to_nothing(self):
        pause_before = self.rms(self.noisy, 0.5, 2.0)
        out = self.tmp / "old.wav"
        ff("-i", self.noisy, "-af", "afftdn=nr=18:nf=-35:tn=1", out)
        self.assertLess(pause_before - self.rms(out, 0.5, 2.0), 3.0)


# ----- 10. clicks at cuts -------------------------------------------------------------------------

@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class CutClickTests(unittest.TestCase):
    def test_the_step_at_a_cut_inside_a_tone_is_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            tone = Path(tmp) / "tone.wav"
            ff("-f", "lavfi", "-i", "sine=frequency=1000:duration=3:sample_rate=48000", "-ac", "1", tone)
            # 1/15 s cut out: 66.67 periods of 1000 Hz, so the wave comes back two thirds of a period later
            graph = audio_filters("[0:a:0]", "[a]", [(0.0, 1.0), (1.0 + 1 / 15, 2.0)])
            out = Path(tmp) / "cut.wav"
            ff("-i", tone, "-filter_complex", graph, "-map", "[a]", "-c:a", "pcm_s16le", out)
            data = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(out), "-f", "s16le",
                                   "-ac", "1", "-"], capture_output=True, check=True).stdout
            samples = array("h")
            samples.frombytes(data)
            around = samples[48000 - 400: 48000 + 400]
            steps = max(abs(a - b) for a, b in zip(around, around[1:]))
            plain = max(abs(a - b) for a, b in zip(samples[1000:3000], samples[1001:3001]))
            self.assertLess(steps, plain * 1.3)            # before: about 1.7 times the steepest part of the wave
            self.assertAlmostEqual(len(samples) / 48000, 2 - 1 / 15, delta=0.001)         # the length is unchanged


class CutFadeTests(unittest.TestCase):
    def test_only_the_cuts_get_a_fade(self):
        graph = audio_filters("[0:a:0]", "[a]", [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)])
        self.assertEqual(graph.count("afade=t=in"), 2)
        self.assertEqual(graph.count("afade=t=out"), 2)
        self.assertEqual(audio_filters("[0:a:0]", "[a]", [(1.0, 2.0)], always=True), "[0:a:0]asetpts=PTS-STARTPTS[a]")


# ----- 11. times ----------------------------------------------------------------------------------

class TimeTests(unittest.TestCase):
    def test_not_a_time_is_refused(self):
        for bad in ("nan", "inf", "-inf", "1e999", "Infinity", "0:nan", "1:inf", "NaN"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_time(bad)

    def test_real_times_still_parse(self):
        self.assertEqual(parse_time("1:30"), 90)
        self.assertEqual(parse_time("1e2"), 100)


# ----- 12. a frame at the very end ----------------------------------------------------------------

@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class LastFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "clip.mp4"
        sample_clip(cls.clip, 3, "160x90", 15)
        cls.info = probe_media(FFMPEG, cls.clip)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_last_frames_can_be_saved(self):
        duration = self.info.duration
        for name, at in (("end", duration), ("almost", duration - 0.01), ("last", duration - 1 / 15),
                         ("second", duration - 2 / 15)):
            with self.subTest(at=name):
                target = self.tmp / f"{name}.png"
                save_frame(FFMPEG, self.clip, target, at, VideoEdit(aspect=None), self.info)
                self.assertGreater(target.stat().st_size, 0)

    def test_an_old_picture_is_not_taken_for_the_result_of_a_failed_run(self):
        broken = self.tmp / "broken.mp4"
        broken.write_bytes(b"this is not a video")
        target = self.tmp / "old.png"
        target.write_bytes(b"an old picture")
        with self.assertRaises(ConversionError):
            video.render_frame(FFMPEG, broken, target, 1.0, (160, 90), VideoEdit(aspect=None), self.info)
        self.assertFalse(target.exists())


# ----- 13 and 14. speed, cuts and the steady rate ---------------------------------------------------

@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class SpeedCadenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.counter = cls.tmp / "counter.mp4"
        counter_clip(cls.counter, 4, "15")             # frames 0..59

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def numbers(self, name, **edit):
        out = self.tmp / name
        convert_video(FFMPEG, VideoJob(self.counter, out, VideoEdit(aspect=None, use_gpu=False, **edit)))
        return frame_numbers(out)

    def steps(self, numbers):
        """The distance between neighbours that belong to the same piece of the source (not across a cut)."""
        return [b - a for a, b in zip(numbers, numbers[1:]) if (a < 30) == (b < 30)]

    def test_double_speed_with_cuts_takes_every_other_frame(self):
        numbers = self.numbers("fast.mp4", speed=2.0, keep=[(0.0, 1.0), (2.0, 4.0)])
        self.assertEqual(set(self.steps(numbers)), {2})
        self.assertIn(len(numbers), (22, 23))                        # 45 kept frames, half of them

    def test_reverse_with_cuts_is_regular(self):
        numbers = self.numbers("back.mp4", reverse=True, keep=[(0.0, 1.0), (2.0, 4.0)])
        self.assertEqual(set(self.steps(numbers)), {-1})
        self.assertEqual(len(numbers), 45)

    def test_reverse_double_speed_with_cuts(self):
        numbers = self.numbers("backfast.mp4", reverse=True, speed=2.0, keep=[(0.0, 1.0), (2.0, 4.0)])
        self.assertEqual(set(self.steps(numbers)), {-2})

    def test_the_command_nudges_the_sped_up_frames_off_the_ties(self):
        cmd = cmd_for(VideoEdit(aspect=None, speed=2.0), info_of(1920, 1080, 30.0))
        self.assertIn("setpts=PTS/2+0.008333/TB,fps=30", after(cmd, "-filter_complex"))


class SlowMotionRateTests(unittest.TestCase):
    def test_slow_motion_keeps_the_steady_rate_that_was_asked_for(self):
        source = info_of(1920, 1080, 30.0)
        for speed in (0.5, 0.25):
            for chosen, expected in ((30, 30), (60, 30)):
                with self.subTest(speed=speed, chosen=chosen):
                    self.assertEqual(video.edit_rate(VideoEdit(frame_rate=chosen, speed=speed), source),
                                     Fraction(expected))
        self.assertEqual(video.edit_rate(VideoEdit(frame_rate=60, speed=0.25), info_of(1920, 1080, 60.0)), Fraction(60))
        self.assertEqual(video.edit_rate(VideoEdit(frame_rate=30, speed=0.5), info_of(1920, 1080, 59.94)), Fraction(30))

    def test_speeding_up_still_counts(self):
        self.assertEqual(video.edit_rate(VideoEdit(frame_rate=60, speed=2.0), info_of(640, 360, 25.0)), Fraction(50))

    def test_the_commands_for_slow_motion(self):
        for speed, chosen in ((0.5, 30), (0.25, 30), (0.5, 60)):
            cmd = cmd_for(VideoEdit(aspect=None, frame_rate=chosen, speed=speed), info_of(1920, 1080, float(chosen)))
            self.assertEqual(after(cmd, "-r"), str(chosen))
            self.assertIn(f"fps={chosen}", after(cmd, "-filter_complex"))

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_slow_motion_at_a_steady_rate_exports_at_that_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            sample_clip(clip, 2, "160x90", 30)
            for speed in (0.5, 0.25):
                out = Path(tmp) / f"slow{speed}.mp4"
                convert_video(FFMPEG, VideoJob(clip, out, VideoEdit(aspect=None, use_gpu=False, speed=speed,
                                                                      frame_rate=30)))
                self.assertAlmostEqual(probe_media(FFMPEG, out).fps, 30.0, delta=0.5)


# ----- 15 and 16. the edges of a trim --------------------------------------------------------------

@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class TrimEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def kept(self, rate, name, **edit):
        clip = self.tmp / f"src-{name}.mp4"
        counter_clip(clip, 5, rate)
        out = self.tmp / f"out-{name}.mp4"
        convert_video(FFMPEG, VideoJob(clip, out, VideoEdit(aspect=None, use_gpu=False, **edit)))
        return frame_numbers(out)

    def test_24_fps_a_start_between_two_frames_keeps_the_frame_it_rounds_to(self):
        # 0.04 s is frame 1 (at 0.0417 s); -ss 0.042 used to drop it
        self.assertEqual(self.kept("24", "a", start=0.04, end=1.0), list(range(1, 24)))

    def test_24_fps_from_the_start(self):
        self.assertEqual(self.kept("24", "b", end=1.0), list(range(0, 24)))

    def test_ntsc_does_not_gain_a_frame_at_the_end(self):
        # 100 frames at 30000/1001 end at 3.336667 s; -to 3.337 used to let frame 100 in
        self.assertEqual(self.kept("30000/1001", "c", end=100 * 1001 / 30000), [i % 64 for i in range(100)])

    def test_ntsc_a_start_on_a_frame(self):
        self.assertEqual(self.kept("30000/1001", "d", start=10 * 1001 / 30000, end=40 * 1001 / 30000),
                         list(range(10, 40)))

    def test_the_trim_arguments_have_more_digits_and_a_hair_of_margin(self):
        self.assertEqual(core.trim_args(0.041667, 3.336667), ["-ss", "0.041167", "-to", "3.336167"])
        self.assertEqual(core.trim_args(None, None), [])
        self.assertEqual(core.trim_args(0.0001, None), ["-ss", "0.000000"])

    def test_a_trim_shorter_than_a_frame_still_has_a_picture(self):
        clip = self.tmp / "tiny.mp4"
        sample_clip(clip, 3, "160x90", 15)
        out = self.tmp / "tiny-out.mp4"
        convert_video(FFMPEG, VideoJob(clip, out, VideoEdit(aspect=None, use_gpu=False, start=0.5, end=0.52)))
        info = probe_media(FFMPEG, out)
        self.assertTrue(info.has_video)
        self.assertEqual(count_frames(out), 1)


# ----- 17. memory for reverse -------------------------------------------------------------------------

class ReverseMemoryLooksTests(unittest.TestCase):
    def test_looks_that_work_in_rgb_need_twice_the_memory(self):
        info = MediaInfo(duration=10.0, width=1920, height=1080, has_audio=False, fps=30.0)
        plain = reverse_memory(VideoEdit(aspect=None, reverse=True), info)
        self.assertGreater(plain, 0)
        for look in ("warm", "cool", "vintage", "contrast"):
            with self.subTest(look=look):
                self.assertEqual(reverse_memory(VideoEdit(aspect=None, reverse=True, look=look), info), plain * 2)
        for look in ("none", "bw"):
            self.assertEqual(reverse_memory(VideoEdit(aspect=None, reverse=True, look=look), info), plain)


# ----- 18. fitting a size, and what is said when it can't be done ---------------------------------------

class FitMessageTests(unittest.TestCase):
    def plan(self, seconds, mb=10, audio="aac"):
        rungs = [(None, 1920, 1080), (720, 1280, 720), (480, 854, 480), (360, 640, 360)]
        return output.fit_plan(output.target_bytes(mb), seconds, "h264", audio, 30.0, rungs)

    def test_when_not_even_the_sound_fits_it_says_so(self):
        with self.assertRaises(ConversionError) as caught:
            self.plan(3600)
        self.assertIn("Not even the sound fits in 10 MB", str(caught.exception))
        self.assertIn("remove the sound", str(caught.exception))
        self.assertNotIn("0:00.0", str(caught.exception))

    def test_a_picture_that_does_not_fit_says_how_much_does(self):
        with self.assertRaises(ConversionError) as caught:
            self.plan(1500)
        self.assertRegex(str(caught.exception), r"about \d+:\d+\.\d of it fits")
        self.assertNotIn("0:00.0", str(caught.exception))

    def test_the_sound_goes_below_48_kbps_before_the_fit_is_given_up(self):
        # 10 MB over 700 s is about 111 kbps for everything: 48 kbps of sound leaves the picture 63, under the
        # 80 it needs at least; 24 kbps leaves it 87
        rungs = [(None, 128, 72)]
        roomy = output.fit_plan(output.target_bytes(10), 100, "h264", "aac", 10.0, rungs)
        self.assertEqual(roomy.audio_kbps, 128)
        squeezed = output.fit_plan(output.target_bytes(10), 700, "h264", "aac", 10.0, rungs)
        self.assertEqual(squeezed.audio_kbps, output.MIN_AUDIO_KBPS)
        self.assertGreaterEqual(squeezed.kbps, output.MIN_VIDEO_KBPS)
        cmd = video.audio_codec_args(VideoEdit(aspect=None), True, squeezed.audio_kbps)
        self.assertEqual((after(cmd, "-b:a"), after(cmd, "-ac")), ("24k", "1"))
        self.assertEqual(after(video.audio_codec_args(VideoEdit(aspect=None), True, 48), "-ac"), "2")


class GifWarningTests(unittest.TestCase):
    def test_the_warning_follows_the_estimated_size(self):
        # a short clip that is big: 1920 wide at 25 fps for 5 s is about 30 MB
        self.assertNotEqual(output.gif_warning(5.0, 1920, 1080, 25.0), "")
        self.assertIn("MB", output.gif_warning(5.0, 1920, 1080, 25.0))
        # a long clip that is small: 3 minutes at 160 x 90, 5 frames a second is about 2 MB
        self.assertEqual(output.gif_warning(180.0, 160, 90, 5.0), "")
        # without the size, the length decides, as before
        self.assertNotEqual(output.gif_warning(20.0), "")
        self.assertEqual(output.gif_warning(10.0), "")

    def test_the_warning_of_an_edit(self):
        big = info_of(1920, 1080, 30.0)
        big.duration = 6.0
        edit = VideoEdit(aspect=None, video_format="gif", gif_width=1920, gif_fps=25)
        self.assertNotEqual(video.format_warning(edit, big), "")
        self.assertEqual(video.format_warning(VideoEdit(aspect=None, video_format="gif"), big), "")


# ----- 19. the graphics cards -------------------------------------------------------------------------

class GraphicsCardCalibrationTests(unittest.TestCase):
    def number(self, encoder, crf, flag):
        return int(after(gpu.encoder_args(encoder, crf, 30.0), flag))

    def test_each_label_is_its_own_number_on_each_encoder(self):
        for crf, h264, hevc in ((18, 27, 29), (23, 31, 34), (28, 37, 40)):
            self.assertEqual(self.number("h264_nvenc", crf, "-cq"), h264)
            self.assertEqual(self.number("hevc_nvenc", crf, "-cq"), hevc)

    def test_nvenc_looks_twice_and_spends_bits_where_they_show(self):
        for encoder in ("h264_nvenc", "hevc_nvenc"):
            args = gpu.encoder_args(encoder, 23, 30.0)
            self.assertEqual([after(args, f) for f in ("-rc", "-b:v", "-multipass", "-spatial-aq", "-temporal-aq")],
                             ["vbr", "0", "fullres", "1", "1"])

    def test_a_bitrate_still_replaces_the_quality(self):
        args = gpu.encoder_args("h264_nvenc", 23, 30.0, kbps=2000)
        self.assertNotIn("-cq", args)
        self.assertEqual(after(args, "-b:v"), "2000k")
        self.assertEqual(after(args, "-multipass"), "fullres")

    def test_intel_and_amd_take_conservative_numbers_and_say_the_rate(self):
        for encoder in ("h264_qsv", "hevc_qsv"):
            args = gpu.encoder_args(encoder, 23, 29.97)
            self.assertEqual((after(args, "-r"), after(args, "-pix_fmt")), ("29.97", "nv12"))
            self.assertGreaterEqual(int(after(args, "-global_quality")), 26)
        for encoder in ("h264_amf", "hevc_amf"):
            args = gpu.encoder_args(encoder, 23, 30.0)
            self.assertEqual(after(args, "-rc"), "cqp")
            self.assertEqual(after(args, "-qp_i"), after(args, "-qp_p"))

    def test_numbers_between_and_beyond_the_labels(self):
        self.assertEqual(gpu.card_quality("h264_nvenc", 20), 29)
        self.assertEqual(gpu.card_quality("h264_nvenc", 33), 42)
        self.assertEqual(gpu.card_quality("h264_nvenc", 10), 19)

    def test_h265_files_are_tagged_for_apple(self):
        for make in gpu.HEVC_ENCODERS.values():
            self.assertEqual(make(23, 30.0)[-2:], ["-tag:v", "hvc1"])


# ----- 20. memory of a 4K export -----------------------------------------------------------------------

class BigPictureTests(unittest.TestCase):
    def test_a_4k_source_gets_fewer_threads_and_a_shorter_lookahead(self):
        cmd = cmd_for(VideoEdit(aspect=None, resolution=UP_TO_1080), info_of(3840, 2160, 30.0))
        self.assertEqual(cmd[cmd.index("-threads") - 0:cmd.index("-threads") + 2], ["-threads", str(video.DECODER_THREADS)])
        self.assertLess(cmd.index("-threads"), cmd.index("-i"))                      # the decoder's
        self.assertEqual(after(cmd, "-filter_complex_threads"), str(video.FILTER_THREADS))
        self.assertEqual(after(cmd, "-x264-params"), f"rc-lookahead={video.LEAN_LOOKAHEAD}")
        encoder_threads = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-threads" and i > cmd.index("-i")]
        self.assertEqual(encoder_threads, [str(video.ENCODER_THREADS)])

    def test_a_1080p_source_keeps_its_decoder_and_lookahead(self):
        cmd = cmd_for(VideoEdit(aspect=None), info_of(1920, 1080, 30.0))
        self.assertNotIn("-filter_complex_threads", cmd)
        self.assertNotIn("-x264-params", cmd)
        self.assertEqual(sum(1 for a in cmd if a == "-threads"), 1)                 # the encoder's only
        self.assertLessEqual(video.ENCODER_THREADS, 12)


# ----- 21. the command line -----------------------------------------------------------------------------

class CommandLineFixTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = run_cli(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_mute_without_video_is_an_error_not_ignored(self):
        code, _, err = self.run_cli("clip.mp3", "--mute")
        self.assertEqual(code, 2)
        self.assertIn("--mute needs --video", err)

    def test_a_size_for_a_gif_is_an_error(self):
        code, _, err = self.run_cli("clip.mp4", "--format", "gif", "--target-size", "5")
        self.assertEqual(code, 2)
        self.assertIn("doesn't work for a GIF", err)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class CoverArtTests(unittest.TestCase):
    def test_an_mp3_keeps_the_cover_of_its_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cover = tmp / "cover.png"
            ff("-f", "lavfi", "-i", "testsrc2=size=64x64:duration=1", "-frames:v", 1, cover)
            song = tmp / "song.m4a"
            ff("-f", "lavfi", "-i", "sine=duration=3", "-i", cover, "-map", 0, "-map", 1, "-c:a", "aac", "-c:v", "png",
               "-disposition:v", "attached_pic", song)
            source_info = probe_media(FFMPEG, song)
            self.assertIsNotNone(source_info.cover_stream)
            plain = tmp / "plain.mp3"
            ff("-f", "lavfi", "-i", "sine=duration=3", plain)
            self.assertIsNone(probe_media(FFMPEG, plain).cover_stream)
            for name, edit in (("whole", {}), ("cut", {"start": 0.5, "end": 2.0})):
                with self.subTest(edit=name):
                    out = tmp / f"{name}.mp3"
                    convert(FFMPEG, ConversionJob(song, out, **edit))
                    info = probe_media(FFMPEG, out)
                    self.assertIsNotNone(info.cover_stream)
                    self.assertTrue(info.has_audio)
                    self.assertFalse(info.has_video)                  # the cover is not a picture to edit

    def test_the_cover_is_found_in_the_text_ffmpeg_prints(self):
        text = ("  Duration: 00:00:03.00\n  Stream #0:0: Audio: aac, 44100 Hz, stereo\n"
                "  Stream #0:1: Video: png, rgb24(pc), 64x64, 90k tbr, 90k tbn (attached pic)\n")
        self.assertEqual(parse_media_info(text).cover_stream, 1)

    def test_a_file_without_a_cover_makes_the_old_command(self):
        job = ConversionJob(Path("a.mp3"), Path("b.mp3"))
        cmd = core.mp3_command("ffmpeg", job, [(0.0, 3.0)], True, 3.0)
        self.assertIn("-vn", cmd)
        cmd = core.mp3_command("ffmpeg", job, [(0.0, 3.0)], True, 3.0, cover=1)
        self.assertNotIn("-vn", cmd)
        self.assertEqual((after(cmd, "-disposition:v:0"), after(cmd, "-c:v")), ("attached_pic", "copy"))


if __name__ == "__main__":
    unittest.main()
