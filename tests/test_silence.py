"""Remove silences (2.6.0): the range math, reading silencedetect, a real export, the window and the command line."""

import copy
import io
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter import __main__ as cli
from video_converter.core import ConversionCancelled, ConversionError, MediaInfo, find_ffmpeg, probe_media
from video_converter.project import ClipSettings
from video_converter.silence import (
    detect_silences, parse_silences, plan_silence_removal, silence_cuts, summary,
)
from video_converter.timing import kept_segments, segments_length
from video_converter.video import VideoEdit

FFMPEG = find_ffmpeg()
TIMEOUT = 30

SAMPLE = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'talk.mp4':
  Duration: 00:00:10.00, start: 0.000000, bitrate: 95 kb/s
[silencedetect @ 000001f0b7e3c0c0] silence_start: -0.00133333
[silencedetect @ 000001f0b7e3c0c0] silence_end: 1.5 | silence_duration: 1.50133
[silencedetect @ 000001f0b7e3c0c0] silence_start: 4.2
[silencedetect @ 000001f0b7e3c0c0] silence_end: 5.25 | silence_duration: 1.05
[silencedetect @ 000001f0b7e3c0c0] silence_start: 9
size=N/A time=00:00:10.00 bitrate=N/A speed= 300x
"""


def setUpModule():
    if QApplication is not None:
        global app
        app = QApplication.instance() or QApplication([])


def pump(seconds, until=lambda: False):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        QApplication.processEvents()
        time.sleep(0.01)
    return until()


def make_talk(path, tone=2, quiet=2, video=True):
    """tone, silence, tone, silence, tone: 3 tones of `tone` seconds with `quiet` seconds between."""
    pieces = [f"sine=frequency=440:duration={tone}:sample_rate=44100", f"anullsrc=channel_layout=mono:sample_rate=44100:duration={quiet}"]
    order = [0, 1, 0, 1, 0]
    graph = ";".join(f"{pieces[kind]}[a{n}]" for n, kind in enumerate(order))
    graph += ";" + "".join(f"[a{n}]" for n in range(5)) + "concat=n=5:v=0:a=1[out]"
    total = tone * 3 + quiet * 2
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        cmd += ["-f", "lavfi", "-i", f"testsrc2=duration={total}:size=160x90:rate=10"]
    cmd += ["-filter_complex", graph, "-map", "[out]"]
    if video:
        cmd += ["-map", "0:v", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", str(path)]
    subprocess.run(cmd, check=True)


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.run_cli([str(a) for a in args])
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue(), err.getvalue()


def length(keep, start=None, end=None, duration=10.0):
    return segments_length(kept_segments(keep, start, end, duration))


class ReadingSilencedetectTests(unittest.TestCase):
    def test_starts_and_ends_are_paired(self):
        found = parse_silences(SAMPLE, 10.0)
        self.assertEqual(found[1], (4.2, 5.25))

    def test_a_start_just_below_zero_is_zero(self):
        self.assertEqual(parse_silences(SAMPLE, 10.0)[0], (0.0, 1.5))

    def test_a_silence_that_runs_to_the_end_of_the_file_ends_with_it(self):
        self.assertEqual(parse_silences(SAMPLE, 10.0)[-1], (9.0, 10.0))
        self.assertEqual(len(parse_silences(SAMPLE, 10.0)), 3)

    def test_without_a_length_the_last_silence_runs_forever(self):
        self.assertEqual(parse_silences(SAMPLE)[-1][0], 9.0)
        self.assertEqual(parse_silences(SAMPLE)[-1][1], float("inf"))

    def test_a_silence_that_ends_at_the_end_is_read_normally(self):
        text = "[silencedetect @ 0x1] silence_start: 8.5\n[silencedetect @ 0x1] silence_end: 10 | silence_duration: 1.5\n"
        self.assertEqual(parse_silences(text, 10.0), [(8.5, 10.0)])

    def test_output_with_no_silences_gives_nothing(self):
        self.assertEqual(parse_silences("Duration: 00:00:10.00\nsize=N/A time=00:00:10.00\n", 10.0), [])
        self.assertEqual(parse_silences(""), [])

    def test_other_lines_that_mention_numbers_are_ignored(self):
        text = "silence_start: 3\n[silencedetect @ 0x1] silence_start: 5\n[silencedetect @ 0x1] silence_end: 6\n"
        self.assertEqual(parse_silences(text, 10.0), [(5.0, 6.0)])


class RangeMathTests(unittest.TestCase):
    def test_each_silence_is_cut_with_padding_left_on_both_sides(self):
        cuts = silence_cuts([(2.0, 4.0), (6.0, 8.0)], None, None, 10.0, 0.15)
        self.assertEqual([(round(a, 2), round(b, 2)) for a, b in cuts], [(2.15, 3.85), (6.15, 7.85)])

    def test_no_padding_cuts_the_whole_silence(self):
        self.assertEqual(silence_cuts([(2.0, 4.0)], None, None, 10.0, 0.0), [(2.0, 4.0)])

    def test_silence_at_the_start_is_cut_from_the_very_start(self):
        cuts = silence_cuts([(0.0, 1.5)], None, None, 10.0, 0.15)
        self.assertEqual([(a, round(b, 2)) for a, b in cuts], [(0.0, 1.35)])

    def test_silence_at_the_end_is_cut_to_the_very_end(self):
        cuts = silence_cuts([(9.0, 10.0)], None, None, 10.0, 0.15)
        self.assertEqual([(round(a, 2), b) for a, b in cuts], [(9.15, 10.0)])

    def test_a_silence_barely_longer_than_the_padding_is_left_alone(self):
        self.assertEqual(silence_cuts([(2.0, 2.35)], None, None, 10.0, 0.15), [])      # 0.05 s would be left to cut
        self.assertEqual(silence_cuts([(2.0, 2.3)], None, None, 10.0, 0.15), [])

    def test_a_short_blip_of_speech_between_two_pauses_is_cut_with_them(self):
        cuts = silence_cuts([(1.0, 3.0), (3.05, 5.0)], None, None, 10.0, 0.0)
        self.assertEqual(cuts, [(1.0, 5.0)])

    def test_padding_keeps_speech_between_pauses_that_would_otherwise_touch(self):
        cuts = silence_cuts([(1.0, 3.0), (3.5, 5.0)], None, None, 10.0, 0.15)
        self.assertEqual(len(cuts), 2)

    def test_speech_too_short_at_the_edges_is_cut_too(self):
        cuts = silence_cuts([(0.05, 3.0), (7.0, 9.95)], None, None, 10.0, 0.0)
        self.assertEqual(cuts, [(0.0, 3.0), (7.0, 10.0)])

    def test_a_file_that_is_all_silence_is_not_cut(self):
        self.assertEqual(silence_cuts([(0.0, 10.0)], None, None, 10.0), [])
        plan = plan_silence_removal([], [(0.0, 10.0)], None, None, 10.0)
        self.assertEqual((plan.keep, plan.cuts), ([], []))

    def test_overlapping_and_unsorted_silences_merge(self):
        cuts = silence_cuts([(5.0, 7.0), (1.0, 3.0), (2.5, 4.0)], None, None, 10.0, 0.0)
        self.assertEqual(cuts, [(1.0, 4.0), (5.0, 7.0)])
        merged = silence_cuts([(1.0, 3.0), (2.5, 6.0)], None, None, 10.0, 0.0)
        self.assertEqual(merged, [(1.0, 6.0)])

    def test_silence_past_the_end_of_the_file_is_clipped(self):
        self.assertEqual(silence_cuts([(8.0, float("inf"))], None, None, 10.0, 0.0), [(8.0, 10.0)])

    def test_the_plan_turns_cuts_into_kept_ranges(self):
        plan = plan_silence_removal([], [(2.0, 4.0), (6.0, 8.0)], None, None, 10.0, 0.0)
        self.assertEqual(plan.keep, [(0.0, 2.0), (4.0, 6.0), (8.0, 10.0)])
        self.assertEqual(len(plan.cuts), 2)
        self.assertAlmostEqual(plan.before, 10.0)
        self.assertAlmostEqual(plan.after, 6.0)
        self.assertAlmostEqual(plan.removed, 4.0)

    def test_a_trim_limits_the_work_to_inside_it(self):
        silences = [(0.0, 1.0), (2.0, 4.0), (8.0, 10.0)]
        cuts = silence_cuts(silences, 3.0, 9.0, 10.0, 0.0)
        self.assertEqual(cuts, [(3.0, 4.0), (8.0, 9.0)])
        plan = plan_silence_removal([], silences, 3.0, 9.0, 10.0, 0.0)
        self.assertAlmostEqual(plan.before, 6.0)
        self.assertAlmostEqual(plan.after, 4.0)
        # Outside the trim nothing is touched, so clearing the trim later shows the video as it was there.
        self.assertEqual(plan.keep[0][0], 0.0)
        self.assertEqual(plan.keep[-1][1], 10.0)
        self.assertEqual(plan.keep, [(0.0, 3.0), (4.0, 8.0), (9.0, 10.0)])

    def test_a_silence_that_straddles_the_trim_only_counts_inside_it(self):
        self.assertEqual(silence_cuts([(1.0, 6.0)], 3.0, 8.0, 10.0, 0.15), [(3.0, round(6.0 - 0.15, 2))])

    def test_an_end_past_the_file_is_the_end_of_the_file(self):
        self.assertEqual(silence_cuts([(8.0, 10.0)], 2.0, 99.0, 10.0, 0.0), [(8.0, 10.0)])

    def test_the_users_own_cuts_stay(self):
        keep = [(0.0, 3.0), (5.0, 10.0)]            # 3..5 was cut by hand
        plan = plan_silence_removal(keep, [(2.0, 4.0), (6.0, 8.0)], None, None, 10.0, 0.0)
        self.assertEqual(plan.keep, [(0.0, 2.0), (5.0, 6.0), (8.0, 10.0)])
        self.assertEqual(len(plan.cuts), 2)
        self.assertAlmostEqual(plan.before, 8.0)
        self.assertAlmostEqual(plan.after, 5.0)

    def test_the_users_splits_stay_and_a_silence_cuts_across_them(self):
        keep = [(0.0, 5.0), (5.0, 10.0)]            # one split at 5
        plan = plan_silence_removal(keep, [(4.0, 6.0)], None, None, 10.0, 0.0)
        self.assertEqual(plan.keep, [(0.0, 4.0), (6.0, 10.0)])
        untouched = plan_silence_removal(keep, [(1.0, 2.0)], None, None, 10.0, 0.0)
        self.assertEqual(untouched.keep, [(0.0, 1.0), (2.0, 5.0), (5.0, 10.0)])

    def test_a_silence_inside_a_part_the_user_cut_changes_nothing(self):
        keep = [(0.0, 3.0), (7.0, 10.0)]
        plan = plan_silence_removal(keep, [(4.0, 6.0)], None, None, 10.0, 0.0)
        self.assertEqual((plan.keep, plan.cuts), (keep, []))
        self.assertEqual(summary(plan)[:15], "No pauses found")

    def test_pieces_the_cuts_would_leave_too_short_to_use_are_dropped(self):
        keep = [(0.0, 4.0), (4.05, 10.0)]
        plan = plan_silence_removal(keep, [(0.0, 3.0), (4.0, 4.0 + 0.01)], None, None, 10.0, 0.0)
        self.assertTrue(all(b - a >= 0.1 for a, b in plan.keep))

    def test_cutting_twice_changes_nothing_the_second_time(self):
        silences = [(2.0, 4.0), (6.0, 8.0)]
        first = plan_silence_removal([], silences, None, None, 10.0, 0.15)
        second = plan_silence_removal(first.keep, silences, None, None, 10.0, 0.15)
        self.assertEqual((second.keep, second.cuts), (first.keep, []))

    def test_more_padding_cuts_less(self):
        silences = [(2.0, 4.0)]
        tight = plan_silence_removal([], silences, None, None, 10.0, 0.0)
        loose = plan_silence_removal([], silences, None, None, 10.0, 0.5)
        self.assertGreater(tight.removed, loose.removed)
        self.assertAlmostEqual(loose.removed, 1.0)

    def test_the_summary_says_what_it_found(self):
        plan = plan_silence_removal([], [(5 + 9 * n, 5 + 9 * n + 38 / 14) for n in range(14)], None, None, 130.0, 0.0)
        self.assertEqual(summary(plan), "Found 14 pauses, 38 s in total. The video goes from 2:10 to 1:32.")
        one = plan_silence_removal([], [(2.0, 4.0)], None, None, 10.0, 0.0)
        self.assertTrue(summary(one).startswith("Found 1 pause, 2.0 s in total."))


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RealAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.talk = cls.tmp / "talk.mp4"
        make_talk(cls.talk)                                    # 10 s: tone, 2 s quiet, tone, 2 s quiet, tone
        cls.talk_info = probe_media(FFMPEG, cls.talk)
        cls.silent = cls.tmp / "silent.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=duration=2:size=160x90:rate=10", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        str(cls.silent)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def pauses(self, path):
        """The silences of at least half a second in a file, by ffmpeg's own measure."""
        return detect_silences(FFMPEG, path, -35.0, 0.5, probe_media(FFMPEG, path).duration)

    def test_the_two_quiet_stretches_are_found_where_they_are(self):
        found = detect_silences(FFMPEG, self.talk, -35.0, 0.6, self.talk_info.duration)
        self.assertEqual(len(found), 2)
        for (a, b), (want_a, want_b) in zip(found, ((2.0, 4.0), (6.0, 8.0))):
            self.assertAlmostEqual(a, want_a, delta=0.15)
            self.assertAlmostEqual(b, want_b, delta=0.15)

    def test_a_tone_is_not_silence_but_a_higher_threshold_makes_everything_silent(self):
        self.assertEqual(len(detect_silences(FFMPEG, self.talk, -60.0, 0.6, 10.0)), 2)
        self.assertEqual(len(detect_silences(FFMPEG, self.talk, 0.0, 0.6, 10.0)), 1)      # 0 dB: all of it

    def test_a_file_without_sound_says_so(self):
        with self.assertRaises(ConversionError) as caught:
            detect_silences(FFMPEG, self.silent, -35.0, 0.6, 2.0)
        self.assertIn("no sound", str(caught.exception))

    def test_listening_can_be_stopped(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(ConversionCancelled):
            detect_silences(FFMPEG, self.talk, -35.0, 0.6, 10.0, stop)

    def test_an_mp3_export_is_shorter_by_the_pauses_and_what_is_left_is_the_tones(self):
        out = self.tmp / "talk-out"
        out.mkdir(exist_ok=True)
        code, _, err = run(self.talk, "-o", out, "--remove-silences", "--overwrite")
        self.assertEqual(code, 0, err)
        result = probe_media(FFMPEG, out / "talk.mp3")
        # 10 s less two pauses of 2 s, each leaving 0.15 s on both sides: 10 - 2 * 1.7
        self.assertAlmostEqual(result.duration, 6.6, delta=0.35)
        self.assertEqual(self.pauses(out / "talk.mp3"), [], "what's left should hold no pause longer than 0.5 s")
        levels = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out / "talk.mp3"), "-af", "volumedetect", "-f", "null",
                                 "-"], capture_output=True, text=True).stderr
        mean = float(re.search(r"mean_volume: (-?[\d.]+) dB", levels).group(1))
        self.assertGreater(mean, -22.0, "the remaining sound should be the tones, not silence")

    def test_a_video_export_is_shorter_by_the_pauses_too(self):
        out = self.tmp / "video-out"
        out.mkdir(exist_ok=True)
        code, _, err = run(self.talk, "-o", out, "--video", "original", "--remove-silences", "--silence-pad", "0",
                           "--silence-min", "1", "--overwrite")
        self.assertEqual(code, 0, err)
        (result,) = out.glob("*.mp4")
        info = probe_media(FFMPEG, result)
        self.assertAlmostEqual(info.duration, 6.0, delta=0.4)         # with no padding both pauses go entirely
        self.assertEqual(self.pauses(result), [])
        self.assertTrue(info.has_video and info.has_audio)

    def test_music_and_a_volume_change_fit_the_shortened_mp3(self):
        music = self.tmp / "song.mp3"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=220:duration=20", str(music)], check=True)
        out = self.tmp / "music-out"
        out.mkdir(exist_ok=True)
        code, _, err = run(self.talk, "-o", out, "--remove-silences", "--volume", "-6", "--normalize",
                           "--music", music, "--overwrite")
        self.assertEqual(code, 0, err)
        result = probe_media(FFMPEG, out / "talk.mp3")
        # the music is longer than the talk, so it is cut to the shortened length (about 6.6 s), not 10 or 20
        self.assertAlmostEqual(result.duration, 6.6, delta=0.4)

    def test_music_is_cut_to_the_shortened_video(self):
        music = self.tmp / "song2.mp3"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=220:duration=20", str(music)], check=True)
        out = self.tmp / "music-video-out"
        out.mkdir(exist_ok=True)
        code, _, err = run(self.talk, "-o", out, "--video", "original", "--remove-silences", "--silence-pad", "0",
                           "--silence-min", "1", "--music", music, "--duck", "--overwrite")
        self.assertEqual(code, 0, err)
        (result,) = out.glob("*.mp4")
        info = probe_media(FFMPEG, result)
        self.assertAlmostEqual(info.duration, 6.0, delta=0.4)
        self.assertTrue(info.has_video and info.has_audio)

    def test_a_trim_and_the_users_cuts_are_respected_in_the_export(self):
        out = self.tmp / "trim-out"
        out.mkdir(exist_ok=True)
        # Trim to 1..9 and cut 4.5..5.5 by hand; the pauses are 2..4 and 6..8.
        code, _, err = run(self.talk, "-o", out, "--start", "1", "--end", "9", "--cut", "4.5-5.5",
                           "--remove-silences", "--silence-pad", "0", "--overwrite")
        self.assertEqual(code, 0, err)
        # What's left of 1..9: 1..2, 4..4.5, 5.5..6 and 8..9
        self.assertAlmostEqual(probe_media(FFMPEG, out / "talk.mp3").duration, 3.0, delta=0.4)

    def test_a_file_without_sound_fails_with_a_clear_message(self):
        code, _, err = run(self.silent, "-o", self.tmp, "--video", "original", "--remove-silences")
        self.assertEqual(code, 1)
        self.assertIn("no sound", err)


class CommandLineTests(unittest.TestCase):
    INFO = MediaInfo(duration=10.0, width=160, height=90, has_audio=True, fps=10.0)

    def run_with(self, *args, info=INFO, silences=((2.0, 4.0), (6.0, 8.0))):
        """Run the CLI on clip.mp4 with listening, probing and exporting faked: (code, err, listen calls, job)."""
        jobs, listened = [], []

        def fake_convert(ffmpeg, job, on_progress=None, **kwargs):
            jobs.append(job)
            return Path("out.mp3")

        def fake_video(ffmpeg, job, **kwargs):
            jobs.append(job)
            return Path("out.mp4")

        def fake_detect(ffmpeg, source, db, length, duration, cancel=None):
            listened.append((db, length))
            return list(silences)

        with mock.patch.object(cli, "find_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(cli, "probe_media", return_value=info), \
                mock.patch.object(cli, "detect_silences", side_effect=fake_detect), \
                mock.patch.object(cli, "convert", side_effect=fake_convert), \
                mock.patch.object(cli, "convert_video", side_effect=fake_video), \
                mock.patch.object(cli, "choose_encoder", return_value=None), \
                mock.patch.object(Path, "is_file", return_value=True):
            code, _, err = run("clip.mp4", *args)
        return code, err, listened, jobs

    def test_the_defaults_are_35_db_and_a_pause_of_0_6_s_with_0_15_s_left(self):
        code, err, listened, jobs = self.run_with("--remove-silences")
        self.assertEqual(code, 0, err)
        self.assertEqual(listened, [(-35.0, 0.6)])
        (job,) = jobs
        self.assertEqual([(round(a, 2), round(b, 2)) for a, b in job.keep], [(0.0, 2.15), (3.85, 6.15), (7.85, 10.0)])

    def test_the_options_change_what_is_listened_for_and_kept(self):
        code, err, listened, jobs = self.run_with("--remove-silences", "--silence-db", "-40", "--silence-min", "1.5",
                                                  "--silence-pad", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual(listened, [(-40.0, 1.5)])
        self.assertEqual(jobs[0].keep, [(0.0, 2.0), (4.0, 6.0), (8.0, 10.0)])

    def test_video_exports_get_the_cuts_too(self):
        code, err, _, jobs = self.run_with("--video", "original", "--remove-silences", "--silence-pad", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual(jobs[0].edit.keep, [(0.0, 2.0), (4.0, 6.0), (8.0, 10.0)])

    def test_cuts_given_with_cut_are_kept(self):
        _, _, _, jobs = self.run_with("--remove-silences", "--silence-pad", "0", "--cut", "3-5")
        self.assertEqual(jobs[0].keep, [(0.0, 2.0), (5.0, 6.0), (8.0, 10.0)])

    def test_the_trim_is_respected(self):
        _, _, _, jobs = self.run_with("--remove-silences", "--silence-pad", "0", "--start", "3", "--end", "9")
        self.assertEqual(jobs[0].keep, [(0.0, 3.0), (4.0, 6.0), (8.0, 10.0)])

    def test_without_the_flag_nothing_is_listened_to(self):
        _, _, listened, jobs = self.run_with()
        self.assertEqual((listened, jobs[0].keep), ([], []))

    def test_the_silence_options_need_the_flag(self):
        code, _, err = run("clip.mp4", "--silence-db", "-30")
        self.assertEqual(code, 2)
        self.assertIn("need --remove-silences", err)

    def test_values_out_of_range_are_refused(self):
        for args, message in ((("--silence-db", "-10"), "--silence-db must be between -60 and -20"),
                              (("--silence-min", "0.1"), "--silence-min must be between 0.3 and 3"),
                              (("--silence-pad", "1"), "--silence-pad must be between 0 and 0.5")):
            with self.subTest(args=args):
                code, _, err = run("clip.mp4", "--remove-silences", *args)
                self.assertEqual(code, 2)
                self.assertIn(message, err)

    def test_a_file_without_sound_is_reported_not_exported(self):
        info = MediaInfo(duration=10.0, width=160, height=90, has_audio=False, fps=10.0)
        code, err, listened, jobs = self.run_with("--video", "original", "--remove-silences", info=info)
        self.assertEqual((code, listened, jobs), (1, [], []))
        self.assertIn("no sound", err)

    def test_joined_files_each_lose_their_own_pauses(self):
        jobs = []
        with mock.patch.object(cli, "find_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(cli, "probe_media", return_value=self.INFO), \
                mock.patch.object(cli, "detect_silences", return_value=[(2.0, 4.0)]), \
                mock.patch.object(cli, "join_clips", side_effect=lambda ffmpeg, job, **kw: jobs.append(job) or Path("j.mp3")), \
                mock.patch.object(cli, "choose_encoder", return_value=None), \
                mock.patch.object(Path, "is_file", return_value=True):
            code, _, err = run("a.mp4", "b.mp4", "--join", "--remove-silences", "--silence-pad", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual([p.edit.keep for p in jobs[0].parts], [[(0.0, 2.0), (4.0, 10.0)]] * 2)

    def test_the_sound_options_reach_an_mp3_export_that_also_loses_its_pauses(self):
        code, err, _, jobs = self.run_with("--remove-silences", "--silence-pad", "0", "--volume", "3", "--normalize",
                                           "--denoise-audio", "light", "--music", "song.mp3", "--duck")
        self.assertEqual(code, 0, err)
        job = jobs[0]
        self.assertEqual(job.keep, [(0.0, 2.0), (4.0, 6.0), (8.0, 10.0)])
        self.assertEqual((job.sound.volume, job.sound.normalize, job.sound.denoise, job.sound.duck),
                         (3.0, True, "light", True))
        self.assertTrue(job.sound.music.endswith("song.mp3"))

    def test_the_sound_options_reach_a_video_export_and_a_join_that_lose_their_pauses(self):
        code, err, _, jobs = self.run_with("--video", "original", "--remove-silences", "--silence-pad", "0",
                                           "--volume", "-4", "--music", "song.mp3")
        self.assertEqual(code, 0, err)
        self.assertEqual(jobs[0].edit.keep, [(0.0, 2.0), (4.0, 6.0), (8.0, 10.0)])
        self.assertEqual(jobs[0].edit.volume, -4.0)
        self.assertTrue(jobs[0].edit.music.endswith("song.mp3"))
        joined = []
        with mock.patch.object(cli, "find_ffmpeg", return_value="ffmpeg"),                 mock.patch.object(cli, "probe_media", return_value=self.INFO),                 mock.patch.object(cli, "detect_silences", return_value=[(2.0, 4.0)]),                 mock.patch.object(cli, "join_clips", side_effect=lambda ffmpeg, job, **kw: joined.append(job) or Path("j.mp3")),                 mock.patch.object(cli, "choose_encoder", return_value=None),                 mock.patch.object(Path, "is_file", return_value=True):
            code, _, err = run("a.mp4", "b.mp4", "--join", "--remove-silences", "--silence-pad", "0", "--normalize")
        self.assertEqual(code, 0, err)
        self.assertEqual([p.edit.keep for p in joined[0].parts], [[(0.0, 2.0), (4.0, 10.0)]] * 2)
        self.assertTrue(all(p.edit.normalize for p in joined[0].parts))


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class DialogTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui import silence_dialog
        self.module = silence_dialog
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()

        def fake_detect(ffmpeg, source, db, length, duration, cancel=None):
            self.calls.append((db, length))
            self.gate.wait(TIMEOUT)
            if cancel is not None and cancel.is_set():
                raise ConversionCancelled()
            return [(2.0, 4.0), (6.0, 8.0)]

        patcher = mock.patch.object(silence_dialog, "detect_silences", side_effect=fake_detect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, keep=(), start=None, end=None, debounce=20):
        self.module.SilenceDialog.DEBOUNCE_MS = debounce
        self.addCleanup(setattr, self.module.SilenceDialog, "DEBOUNCE_MS", 400)
        dialog = self.module.SilenceDialog("ffmpeg", Path("talk.mp4"), 10.0, list(keep), start, end)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.reject)
        return dialog

    def test_it_starts_with_the_defaults_and_listens_straight_away(self):
        dialog = self.open()
        self.assertEqual((dialog.noise_db, dialog.min_length, dialog.padding), (-35.0, 0.6, 0.15))
        self.assertEqual(dialog.db_label.text(), "−35 dB")
        self.assertEqual(dialog.min_label.text(), "0.6 s")
        self.assertEqual(dialog.pad_label.text(), "0.15 s")
        self.assertEqual((dialog.db_slider.minimum(), dialog.db_slider.maximum()), (-60, -20))
        self.assertEqual((dialog.min_slider.minimum(), dialog.min_slider.maximum()), (3, 30))
        self.assertEqual((dialog.pad_slider.minimum(), dialog.pad_slider.maximum()), (0, 10))
        self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None))
        self.assertEqual(self.calls, [(-35.0, 0.6)])

    def test_the_summary_and_apply_button_follow_the_analysis(self):
        dialog = self.open()
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertEqual(dialog.summary_label.text(), "Listening…")
        self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None))
        self.assertEqual(dialog.summary_label.text(),
                         "Found 2 pauses, 3.4 s in total. The video goes from 0:10 to 0:07.")
        self.assertTrue(dialog.apply_button.isEnabled())

    def test_moving_the_padding_updates_the_summary_without_listening_again(self):
        dialog = self.open()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None))
        dialog.pad_slider.setValue(0)
        self.assertEqual(dialog.padding, 0.0)
        self.assertIn("4.0 s in total", dialog.summary_label.text())
        self.assertEqual(len(self.calls), 1)

    def test_moving_a_level_slider_listens_again_once_after_a_short_wait(self):
        dialog = self.open()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None))
        for value in (-30, -28, -25):
            dialog.db_slider.setValue(value)
        self.assertIsNone(dialog.plan)
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertEqual(dialog.summary_label.text(), "Listening…")
        self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None))
        self.assertEqual(self.calls, [(-35.0, 0.6), (-25.0, 0.6)])

    def test_an_answer_that_arrives_late_is_ignored(self):
        self.gate.clear()
        dialog = self.open()
        pump(0.2)
        dialog.min_slider.setValue(15)
        pump(0.2)                                  # the first listening is cancelled, the second is waiting
        self.gate.set()
        self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None))
        self.assertEqual(self.calls[-1], (-35.0, 1.5))
        self.assertEqual(dialog.min_label.text(), "1.5 s")

    def test_apply_gives_the_new_kept_ranges_and_cancel_gives_nothing(self):
        dialog = self.open(keep=[(0.0, 5.0), (5.0, 10.0)])
        self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None))
        dialog.apply_button.click()
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        self.assertEqual([(round(a, 2), round(b, 2)) for a, b in dialog.keep],
                         [(0.0, 2.15), (3.85, 5.0), (5.0, 6.15), (7.85, 10.0)])      # the split at 5 is still there
        other = self.open()
        self.assertTrue(pump(TIMEOUT, lambda: other.plan is not None))
        other.buttons.button(other.buttons.StandardButton.Cancel).click()
        self.assertEqual(other.result(), other.DialogCode.Rejected)
        self.assertEqual(other.keep, [])

    def test_nothing_to_cut_says_so_and_cannot_be_applied(self):
        with mock.patch.object(self.module, "detect_silences", return_value=[]):
            dialog = self.open()
            self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None))
        self.assertTrue(dialog.summary_label.text().startswith("No pauses found."))
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.accept()
        self.assertNotEqual(dialog.result(), dialog.DialogCode.Accepted)

    def test_a_failure_is_shown_in_the_window(self):
        with mock.patch.object(self.module, "detect_silences", side_effect=ConversionError("No sound here.")):
            dialog = self.open()
            self.assertTrue(pump(TIMEOUT, lambda: dialog.summary_label.text() != "Listening…"))
        self.assertEqual(dialog.summary_label.text(), "No sound here.")
        self.assertFalse(dialog.apply_button.isEnabled())

    def test_closing_stops_the_listening(self):
        self.gate.clear()
        dialog = self.open()
        pump(0.2)
        cancel = dialog._cancel
        self.gate.set()
        dialog.reject()
        self.assertTrue(cancel.is_set())
        self.assertFalse(dialog._thread.is_alive())

    def test_the_trim_and_cuts_it_was_given_are_used(self):
        dialog = self.open(keep=[(0.0, 3.0), (5.0, 10.0)], start=1.0, end=9.0)
        self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None))
        self.assertEqual(len(dialog.plan.cuts), 2)
        self.assertAlmostEqual(dialog.plan.before, 6.0)       # 1..3 and 5..9


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.talk = cls.tmp / "talk.mp4"
        make_talk(cls.talk)
        cls.mute = cls.tmp / "mute.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=duration=2:size=160x90:rate=10", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        str(cls.mute)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        from PySide6.QtCore import QEvent
        from video_converter.ui.main_window import MainWindow
        self.QEvent = QEvent
        ini = self.tmp / f"settings-{self.id().split('.')[-1]}.ini"
        self.window = MainWindow(FFMPEG, QSettings(str(ini), QSettings.Format.IniFormat))
        self.window.resize(1200, 800)
        self.window.show()

    def tearDown(self):
        self.window.close()
        self.window.loader.shutdown(wait=True)
        self.window.deleteLater()
        self.window = None
        QApplication.sendPostedEvents(None, self.QEvent.Type.DeferredDelete)
        pump(0.1)

    def load(self, path):
        self.window.add_paths([path])
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)), "file never loaded")

    def apply_dialog(self):
        """Stand in for exec(): wait for the real listening to finish, then click Apply."""
        def fake_exec(dialog):
            self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None), "listening never finished")
            self.dialog = dialog
            dialog.apply_button.click()
            return dialog.result()
        return mock.patch("video_converter.ui.silence_dialog.SilenceDialog.exec", fake_exec)

    def test_the_button_is_in_trim_and_cut_and_asks_the_window(self):
        self.load(self.talk)
        button = self.window.inspector.silence_button
        self.assertEqual(button.text(), "Remove silences…")
        self.assertTrue(button.isEnabled())
        self.assertIs(self.window.inspector.trim_section.body, button.parentWidget())
        with mock.patch.object(self.window, "remove_silences") as asked:
            self.window.inspector.removeSilencesRequested.disconnect()
            self.window.inspector.removeSilencesRequested.connect(asked)
            button.click()
        asked.assert_called_once()

    def test_it_is_off_with_a_reason_for_a_file_without_sound(self):
        self.load(self.mute)
        button = self.window.inspector.silence_button
        self.assertFalse(button.isEnabled())
        self.assertIn("no sound", button.toolTip())
        self.window.remove_silences()
        self.assertEqual(self.window.undo_stack.count(), 0)
        self.assertIn("no sound", self.window.statusBar().currentMessage())
        self.load(self.talk)
        self.window.media_list.setCurrentRow(1)
        self.assertTrue(pump(TIMEOUT, lambda: self.window.current.path == self.talk))
        self.assertTrue(button.isEnabled())

    def test_applying_is_one_undo_step_and_the_timeline_shows_the_cuts(self):
        self.load(self.talk)
        item = self.window.current
        with self.apply_dialog():
            self.window.remove_silences()
        keep = item.settings.edit.keep
        self.assertEqual(len(keep), 3)
        self.assertAlmostEqual(keep[0][1], 2.15, delta=0.2)
        self.assertEqual(self.window.undo_stack.count(), 1)
        self.assertEqual(self.window.undo_stack.undoText(), "Remove silences")
        self.assertEqual(self.window.timeline.keep, keep)           # the same cuts a person would make by hand
        self.assertEqual(self.window.player.segments, keep)
        self.assertTrue(self.window.inspector.restore_button.isEnabled())
        self.assertIn("Cut out", self.window.inspector.cut_info.text())

        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.keep, [])
        self.assertEqual(self.window.timeline.keep, [])
        self.window.undo_stack.redo()
        self.assertEqual(item.settings.edit.keep, keep)

    def test_cancelling_changes_nothing(self):
        self.load(self.talk)

        def cancel(dialog):
            self.assertTrue(pump(TIMEOUT, lambda: dialog.plan is not None))
            dialog.reject()
            return dialog.result()

        with mock.patch("video_converter.ui.silence_dialog.SilenceDialog.exec", cancel):
            self.window.remove_silences()
        self.assertEqual(self.window.current.settings.edit.keep, [])
        self.assertEqual(self.window.undo_stack.count(), 0)

    def test_the_users_own_cut_is_kept_when_silences_are_removed(self):
        self.load(self.talk)
        item = self.window.current
        self.window.push_settings(ClipSettings(edit=VideoEdit(keep=[(0.0, 4.5), (5.0, 10.0)])), "Cut")      # 4.5..5 gone
        with self.apply_dialog():
            self.window.remove_silences()
        keep = item.settings.edit.keep
        self.assertTrue(all(not (a < 4.7 < b) for a, b in keep), "the cut the user made should still be there")
        self.assertEqual(len(keep), 4)
        self.assertEqual(self.window.undo_stack.count(), 2)
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.keep, [(0.0, 4.5), (5.0, 10.0)])


if __name__ == "__main__":
    unittest.main()
