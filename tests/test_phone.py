"""Playing on a phone (2.5): steady frame rates, phone-safe encoding, "Up to 1080p" and the preset."""

import io
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path
from unittest import mock

from video_converter import __main__ as cli
from video_converter import gpu
from video_converter.core import MediaInfo, _ffmpeg_info_text, find_ffmpeg, parse_media_info, probe_media
from video_converter.project import (
    OUTPUT_VIDEO, PRESETS, ClipSettings, apply_preset, describe, output_file_for,
)
from video_converter.video import (
    FRAME_RATES, RESOLUTIONS, UP_TO_1080, JoinJob, JoinPart, VideoEdit, VideoJob, convert_video, frame_rate_hint,
    h264_level, join_clips, join_command, output_tag, plan_geometry, steady_rate, video_codec_args, video_command,
)

FFMPEG = find_ffmpeg()
PHONE = "Play on phone (keeps the whole picture)"
GAME_LINE = ("  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), "
             "1728x1080 [SAR 1:1 DAR 8:5], 13976 kb/s, {}, 60k tbn (default)")


def info_from(rates: str) -> MediaInfo:
    """What the app learns from a video stream line that says `rates`, e.g. "57.97 fps, 120 tbr"."""
    return parse_media_info("  Duration: 00:00:13.62, start: 0.000000, bitrate: 14112 kb/s\n" + GAME_LINE.format(rates))


# ----- reading the frame rate -----------------------------------------------------

class FrameRateParsingTests(unittest.TestCase):
    def test_a_game_recording_that_declares_120_fps_is_variable(self):
        info = info_from("57.97 fps, 120 tbr")
        self.assertEqual((info.fps, info.max_fps), (57.97, 120.0))
        self.assertTrue(info.variable_frame_rate)

    def test_constant_videos_are_not_flagged(self):
        for rates in ("30 fps, 30 tbr", "29.97 fps, 29.97 tbr", "23.98 fps, 23.98 tbr", "60 fps, 60 tbr",
                      "25 fps, 25 tbr", "30 fps, 30.03 tbr"):
            with self.subTest(rates=rates):
                self.assertFalse(info_from(rates).variable_frame_rate)

    def test_twice_the_rate_of_a_standard_rate_is_fields_not_a_changing_rate(self):
        info = info_from("25 fps, 50 tbr")
        self.assertEqual(info.max_fps, 50.0)
        self.assertFalse(info.variable_frame_rate)

    def test_only_a_tbr_gives_both_rates(self):
        info = info_from("25 tbr")
        self.assertEqual((info.fps, info.max_fps), (25.0, 25.0))
        self.assertFalse(info.variable_frame_rate)

    def test_a_thousand_tbr_is_read(self):
        self.assertEqual(info_from("1k tbr").max_fps, 1000.0)

    def test_audio_and_unknown_rates_are_never_variable(self):
        self.assertFalse(MediaInfo(duration=5.0).variable_frame_rate)
        self.assertFalse(MediaInfo(duration=5.0, fps=50.0).variable_frame_rate)


class FrameRateHintTests(unittest.TestCase):
    def test_a_variable_video_gets_the_hint_with_its_highest_rate(self):
        self.assertEqual(frame_rate_hint(info_from("57.97 fps, 120 tbr")),
                         "This video's frame rate changes (up to 120 fps), which some phones can't play. "
                         "Choose Steady 60 fps.")

    def test_a_constant_video_above_60_fps_gets_a_hint(self):
        self.assertIn("runs at 120 fps", frame_rate_hint(info_from("120 fps, 120 tbr")))

    def test_ordinary_videos_get_none(self):
        for rates in ("30 fps, 30 tbr", "59.94 fps, 59.94 tbr", "60 fps, 60 tbr"):
            with self.subTest(rates=rates):
                self.assertEqual(frame_rate_hint(info_from(rates)), "")
        self.assertEqual(frame_rate_hint(MediaInfo(duration=3.0)), "")


# ----- the setting ------------------------------------------------------------------

class FrameRateSettingTests(unittest.TestCase):
    def test_default_keeps_the_source_rate(self):
        self.assertIsNone(VideoEdit().frame_rate)
        self.assertEqual(FRAME_RATES, {"Same as source": None, "Steady 60 fps": 60, "Steady 30 fps": 30})

    def test_round_trips_through_json(self):
        for rate in (None, 30, 60):
            settings = ClipSettings(edit=VideoEdit(frame_rate=rate, resolution=UP_TO_1080))
            loaded = ClipSettings.from_dict(json.loads(json.dumps(settings.to_dict())))
            self.assertEqual(loaded, settings)
            self.assertEqual(loaded.edit.frame_rate, rate)

    def test_bad_saved_values_are_ignored(self):
        for junk in (45, 24, 0, -60, "60", True, False, [60], {"rate": 60}, float("nan"), 60.5):
            with self.subTest(junk=junk):
                loaded = ClipSettings.from_dict({"edit": {"frame_rate": junk}})
                self.assertIsNone(loaded.edit.frame_rate)

    def test_a_whole_number_float_is_read_as_an_int(self):
        loaded = ClipSettings.from_dict({"edit": {"frame_rate": 60.0}})
        self.assertEqual(loaded.edit.frame_rate, 60)
        self.assertIsInstance(loaded.edit.frame_rate, int)

    def test_settings_from_before_this_version_still_load(self):
        loaded = ClipSettings.from_dict({"output": "video", "edit": {"aspect": [9, 16], "crf": 18}})
        self.assertIsNone(loaded.edit.frame_rate)
        self.assertEqual(loaded.edit.crf, 18)

    def test_up_to_1080p_is_a_valid_saved_size_but_other_negative_sizes_are_not(self):
        self.assertEqual(ClipSettings.from_dict({"edit": {"resolution": UP_TO_1080}}).edit.resolution, UP_TO_1080)
        self.assertIsNone(ClipSettings.from_dict({"edit": {"resolution": -720}}).edit.resolution)

    def test_the_summary_mentions_it(self):
        text = describe(ClipSettings(edit=VideoEdit(aspect=None, frame_rate=60)))
        self.assertEqual(text, "MP4 · original shape · steady 60 fps")
        self.assertNotIn("fps", describe(ClipSettings(edit=VideoEdit(aspect=None))))


class PresetTests(unittest.TestCase):
    def test_it_is_listed_first(self):
        self.assertEqual(next(iter(PRESETS)), PHONE)

    def test_it_sets_exactly_the_intended_fields(self):
        base = ClipSettings(output="audio", edit=VideoEdit(
            aspect=(4, 5), fit="blur", resolution=480, crf=28, mute=True, frame_rate=None, rotate=90, speed=2.0,
            zoom=1.5, start=1.0, brightness=0.2))
        result = apply_preset(base, PRESETS[PHONE])
        self.assertEqual(result.output, OUTPUT_VIDEO)
        edit = result.edit
        self.assertIsNone(edit.aspect)
        self.assertEqual((edit.resolution, edit.frame_rate, edit.crf, edit.mute), (UP_TO_1080, 60, 18, False))
        # Everything else is the file's own.
        expected = VideoEdit(aspect=None, fit="blur", resolution=UP_TO_1080, crf=18, mute=False, frame_rate=60,
                             rotate=90, speed=2.0, zoom=1.5, start=1.0, brightness=0.2)
        self.assertEqual(edit, expected)

    def test_it_has_no_crop_on_a_file_with_default_framing(self):
        edit = apply_preset(ClipSettings(), PRESETS[PHONE]).edit
        geometry = plan_geometry(1728, 1080, edit)
        self.assertEqual((geometry.out_width, geometry.out_height), (1728, 1080))
        self.assertEqual(geometry.crop, (1728, 1080, 0, 0))


class OutputTagTests(unittest.TestCase):
    def test_original_shape_at_a_steady_rate_is_phone_ready(self):
        for rate in (30, 60):
            self.assertEqual(output_tag(VideoEdit(aspect=None, frame_rate=rate)), "phone-ready")
        settings = ClipSettings(edit=VideoEdit(aspect=None, frame_rate=60))
        self.assertEqual(output_file_for(Path("clip.mkv"), settings, Path("out")), Path("out") / "clip (phone-ready).mp4")

    def test_everything_else_keeps_its_tag(self):
        self.assertEqual(output_tag(VideoEdit(aspect=None)), "edited")
        self.assertEqual(output_tag(VideoEdit(aspect=(9, 16), frame_rate=60)), "phone")
        self.assertEqual(output_tag(VideoEdit(aspect=(1, 1), frame_rate=30)), "square")
        self.assertEqual(output_tag(VideoEdit(aspect=(16, 9))), "widescreen")


# ----- sizes, rates and levels --------------------------------------------------------

class UpTo1080Tests(unittest.TestCase):
    def size(self, width, height, **kw):
        geometry = plan_geometry(width, height, VideoEdit(resolution=UP_TO_1080, **kw))
        return geometry.out_width, geometry.out_height

    def test_it_is_a_choice_after_same_as_source(self):
        self.assertEqual(list(RESOLUTIONS)[:2], ["Same as source", "Up to 1080p"])
        self.assertEqual(RESOLUTIONS["Same as source"], None)
        self.assertEqual((RESOLUTIONS["1080p"], RESOLUTIONS["720p"], RESOLUTIONS["480p"]), (1080, 720, 480))

    def test_smaller_videos_are_never_enlarged(self):
        for size in ((1280, 720), (640, 360), (1728, 1080), (1920, 1080), (854, 480), (1080, 1920), (720, 1280)):
            with self.subTest(size=size):
                self.assertEqual(self.size(*size, aspect=None), size)

    def test_bigger_videos_stop_at_1080(self):
        self.assertEqual(self.size(3840, 2160, aspect=None), (1920, 1080))
        self.assertEqual(self.size(2560, 1440, aspect=None), (1920, 1080))
        self.assertEqual(self.size(2160, 3840, aspect=None), (1080, 1920))
        self.assertEqual(self.size(3440, 1440, aspect=None), (2580, 1080))
        self.assertEqual(self.size(3840, 2160, aspect=(16, 9)), (1920, 1080))
        self.assertEqual(self.size(3840, 2160, aspect=(9, 16)), (1080, 1920))

    def test_a_shape_change_does_not_enlarge_either(self):
        self.assertEqual(self.size(1280, 720, aspect=(9, 16)), (720, 1280))
        self.assertEqual(self.size(1280, 720, aspect=(16, 9)), (1280, 720))

    def test_the_other_sizes_mean_what_they_did(self):
        self.assertEqual(plan_geometry(3840, 2160, VideoEdit(aspect=None)).out_width, 3840)
        geometry = plan_geometry(1280, 720, VideoEdit(aspect=None, resolution=1080))
        self.assertEqual((geometry.out_width, geometry.out_height), (1920, 1080))     # 1080p still enlarges
        geometry = plan_geometry(1920, 1080, VideoEdit(aspect=None, resolution=720))
        self.assertEqual((geometry.out_width, geometry.out_height), (1280, 720))


class SteadyRateTests(unittest.TestCase):
    def test_nothing_chosen_means_nothing_is_forced(self):
        self.assertIsNone(steady_rate(None, 57.97))
        self.assertIsNone(steady_rate(None, None))

    def test_a_fast_or_similar_source_gets_the_chosen_rate(self):
        for chosen, source in ((60, 120.0), (60, 57.97), (60, 60.0), (60, 59.94), (30, 29.97), (30, 60.0), (30, 33.0),
                               (60, None)):
            with self.subTest(chosen=chosen, source=source):
                self.assertEqual(steady_rate(chosen, source), Fraction(chosen))

    def test_a_clearly_slower_source_is_not_stretched(self):
        self.assertEqual(steady_rate(60, 30.0), Fraction(30))
        self.assertEqual(steady_rate(60, 25.0), Fraction(25))
        self.assertEqual(steady_rate(30, 24.0), Fraction(24))
        self.assertEqual(steady_rate(60, 23.98), Fraction(24000, 1001))
        self.assertEqual(steady_rate(60, 50.0), Fraction(50))

    def test_it_is_never_above_the_chosen_rate(self):
        for chosen in (30, 60):
            for source in (5.0, 12.5, 24.0, 29.97, 30.0, 48.0, 59.94, 100.0, 240.0, None):
                self.assertLessEqual(steady_rate(chosen, source), chosen)


class LevelTests(unittest.TestCase):
    def test_level_follows_size_and_rate(self):
        for size, fps, level in (
            ((1920, 1080), 30, "4.1"), ((1920, 1080), 29.97, "4.1"), ((1920, 1080), 60, "4.2"),
            ((1728, 1080), 60, "4.2"), ((1080, 1920), 60, "4.2"), ((1280, 720), 60, "4.1"),
            ((1280, 720), 30, "4.1"), ((640, 360), 30, "4.1"), ((320, 180), 15, "4.1"),
            ((2560, 1440), 30, "5.0"), ((3840, 2160), 30, "5.1"), ((3840, 2160), 60, "5.2"),
            ((7680, 4320), 60, "6.1"),
        ):
            with self.subTest(size=size, fps=fps):
                self.assertEqual(h264_level(*size, fps), level)

    def test_1080p60_does_not_fit_4_1(self):
        # 1080p60 is more macroblocks a second than level 4.1 allows; 1080p30 just fits.
        self.assertGreater(8160 * 60, 245760)
        self.assertLessEqual(8160 * 30, 245760)


# ----- the commands ----------------------------------------------------------------------

SOURCE = MediaInfo(duration=6.0, width=1728, height=1080, has_audio=True, fps=57.97, max_fps=120.0)


def cmd_for(edit, info=SOURCE, encoder=None):
    return video_command("ffmpeg", VideoJob(Path("in.mp4"), Path("out.mp4"), edit), info, encoder=encoder)


def graph_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class CommandTests(unittest.TestCase):
    def test_no_steady_rate_changes_nothing(self):
        cmd = cmd_for(VideoEdit(aspect=None))
        self.assertNotIn("fps=", graph_of(cmd))
        for flag in ("-profile:v", "-level", "-ar", "-ac"):
            self.assertNotIn(flag, cmd)

    def test_a_steady_export_is_high_profile_with_a_matching_level_and_stereo_48k_aac(self):
        cmd = cmd_for(VideoEdit(aspect=None, frame_rate=60))
        self.assertIn("fps=60", graph_of(cmd))
        self.assertEqual((after(cmd, "-profile:v"), after(cmd, "-level")), ("high", "4.2"))
        self.assertEqual((after(cmd, "-ar"), after(cmd, "-ac"), after(cmd, "-c:a")), ("48000", "2", "aac"))
        self.assertEqual(after(cmd, "-pix_fmt"), "yuv420p")
        self.assertEqual(after(cmd, "-movflags"), "+faststart")
        cmd = cmd_for(VideoEdit(aspect=None, frame_rate=30))
        self.assertIn("fps=30", graph_of(cmd))
        self.assertEqual(after(cmd, "-level"), "4.1")

    def test_the_level_is_for_the_size_that_is_written(self):
        edit = VideoEdit(aspect=None, frame_rate=60, resolution=UP_TO_1080)
        big = MediaInfo(duration=6.0, width=3840, height=2160, has_audio=True, fps=60.0)
        self.assertEqual(after(cmd_for(edit, big), "-level"), "4.2")           # 1080p60, not 4K
        self.assertEqual(after(cmd_for(VideoEdit(aspect=None, frame_rate=60), big), "-level"), "5.2")

    def test_the_rate_filter_comes_after_cuts_and_speed(self):
        graph = graph_of(cmd_for(VideoEdit(aspect=None, frame_rate=60, speed=2.0, keep=[(0.0, 1.0), (2.0, 4.0)])))
        self.assertLess(graph.index("concat"), graph.index("setpts=PTS/2"))
        self.assertLess(graph.index("setpts=PTS/2"), graph.index("fps=60"))
        self.assertLess(graph.index("fps=60"), graph.index("crop="))

    def test_reverse_comes_after_the_steady_rate(self):
        graph = graph_of(cmd_for(VideoEdit(aspect=None, frame_rate=60, reverse=True)))
        self.assertLess(graph.index("fps=60"), graph.index("reverse"))

    def test_a_slow_source_keeps_its_own_rate(self):
        slow = MediaInfo(duration=6.0, width=640, height=360, has_audio=True, fps=25.0)
        self.assertIn("fps=25", graph_of(cmd_for(VideoEdit(aspect=None, frame_rate=60), slow)))
        ntsc = MediaInfo(duration=6.0, width=640, height=360, has_audio=True, fps=23.98)
        self.assertIn("fps=24000/1001", graph_of(cmd_for(VideoEdit(aspect=None, frame_rate=60), ntsc)))

    def test_speeding_up_counts_for_the_rate_the_picture_ends_with(self):
        slow = MediaInfo(duration=6.0, width=640, height=360, has_audio=True, fps=25.0)
        self.assertIn("fps=50", graph_of(cmd_for(VideoEdit(aspect=None, frame_rate=60, speed=2.0), slow)))
        self.assertIn("fps=30", graph_of(cmd_for(VideoEdit(aspect=None, frame_rate=30, speed=2.0), slow)))

    def test_no_sound_stays_no_sound(self):
        cmd = cmd_for(VideoEdit(aspect=None, frame_rate=60, mute=True))
        self.assertIn("-an", cmd)
        self.assertNotIn("-ar", cmd)

    def test_every_graphics_card_encoder_gets_the_profile_and_level(self):
        for encoder in gpu.ENCODERS:
            with self.subTest(encoder=encoder):
                args = gpu.encoder_args(encoder, 18, 60.0, "4.2")
                self.assertEqual((after(args, "-profile:v"), after(args, "-level")), ("high", "42"))
                self.assertEqual(args[:len(gpu.ENCODERS[encoder](18, 60.0))], gpu.ENCODERS[encoder](18, 60.0))
                self.assertNotIn("-level", gpu.encoder_args(encoder, 18, 60.0))
                self.assertNotIn("-profile:v", gpu.encoder_args(encoder, 18, 60.0))
                cmd = cmd_for(VideoEdit(aspect=None, frame_rate=60), encoder=encoder)
                self.assertEqual(after(cmd, "-level"), "42")
                self.assertIn("fps=60", graph_of(cmd))

    def test_codec_args_without_a_level_are_what_they_were(self):
        self.assertEqual(video_codec_args(23)[:4], ["-c:v", "libx264", "-preset", "medium"])
        self.assertNotIn("-level", video_codec_args(23))
        self.assertEqual(after(video_codec_args(23, None, 60.0, "4.2"), "-level"), "4.2")


def part(name, width, height, fps, edit=None, audio=True):
    info = MediaInfo(duration=4.0, width=width, height=height, has_audio=audio, fps=fps)
    return JoinPart(Path(name), edit or VideoEdit(aspect=None), info)


class JoinCommandTests(unittest.TestCase):
    def build(self, parts):
        return join_command("ffmpeg", JoinJob(parts, Path("out.mp4")))[0]

    def test_without_a_setting_the_first_clips_rate_still_wins(self):
        cmd = self.build([part("a.mp4", 640, 360, 15.0), part("b.mp4", 640, 360, 60.0)])
        self.assertEqual(graph_of(cmd).count("fps=15"), 2)
        self.assertNotIn("-level", cmd)

    def test_a_steady_rate_wins_over_the_first_clips(self):
        first = part("a.mp4", 1920, 1080, 30.0, VideoEdit(aspect=None, frame_rate=60))
        cmd = self.build([first, part("b.mp4", 1920, 1080, 120.0)])
        self.assertEqual(graph_of(cmd).count("fps=60"), 2)
        self.assertEqual((after(cmd, "-profile:v"), after(cmd, "-level"), after(cmd, "-ar")), ("high", "4.2", "48000"))

    def test_clips_that_are_all_slower_keep_the_fastest_ones_rate(self):
        first = part("a.mp4", 640, 360, 24.0, VideoEdit(aspect=None, frame_rate=60))
        cmd = self.build([first, part("b.mp4", 640, 360, 30.0)])
        self.assertEqual(graph_of(cmd).count("fps=30"), 2)
        self.assertEqual(after(cmd, "-level"), "4.1")

    def test_only_the_first_clips_setting_counts(self):
        second = part("b.mp4", 640, 360, 60.0, VideoEdit(aspect=None, frame_rate=30))
        cmd = self.build([part("a.mp4", 640, 360, 60.0), second])
        self.assertNotIn("-level", cmd)
        self.assertEqual(graph_of(cmd).count("fps=60"), 2)


# ----- the command line ----------------------------------------------------------------------

def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.run_cli([str(a) for a in args])
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue(), err.getvalue()


class CliOptionTests(unittest.TestCase):
    def exported(self, *args):
        """(exit code, the job's edit, its output name) of a faked CLI export of clip.mp4."""
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"x")
            with mock.patch.object(cli, "find_ffmpeg", return_value="ffmpeg"), \
                    mock.patch.object(cli, "choose_encoder", return_value=None), \
                    mock.patch.object(cli, "convert_video", side_effect=lambda f, job, **kw: job.output) as export:
                code, _, err = run(clip, *args)
            if not export.called:
                return code, None, err
            job = export.call_args.args[1]
            return code, job.edit, job.output.name

    def test_video_original_fps_60_makes_the_phone_ready_export(self):
        code, edit, name = self.exported("--video", "original", "--fps", "60")
        self.assertEqual((code, edit.frame_rate, edit.aspect), (0, 60, None))
        self.assertEqual(name, "clip (phone-ready).mp4")

    def test_fps_30_and_the_default(self):
        self.assertEqual(self.exported("--video", "original", "--fps", "30")[1].frame_rate, 30)
        code, edit, name = self.exported("--video", "original")
        self.assertEqual((edit.frame_rate, name), (None, "clip (edited).mp4"))
        self.assertEqual(self.exported("--video", "original", "--fps", "source")[1].frame_rate, None)

    def test_a_shape_keeps_its_name_with_a_steady_rate(self):
        self.assertEqual(self.exported("--video", "phone", "--fps", "60")[2], "clip (phone).mp4")

    def test_sizes(self):
        for text, value in (("up1080", UP_TO_1080), ("1080", 1080), ("720", 720), ("480", 480)):
            with self.subTest(size=text):
                self.assertEqual(self.exported("--video", "original", "--size", text)[1].resolution, value)
        self.assertIsNone(self.exported("--video", "original")[1].resolution)

    def test_bad_values_are_refused(self):
        for args in (("--fps", "45"), ("--fps", "120"), ("--size", "900"), ("--size", "up720")):
            with self.subTest(args=args):
                code, _, err = run("clip.mp4", "--video", "original", *args)
                self.assertEqual(code, 2)
                self.assertIn("invalid choice", err)

    def test_fps_needs_a_video_export(self):
        code, _, err = run("clip.mp4", "--fps", "60")
        self.assertEqual(code, 2)
        self.assertIn("--fps needs --video", err)

    def test_help_explains_both(self):
        code, out, _ = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("--fps {source,60,30}", out)
        self.assertIn("up1080", out)
        self.assertIn("plays on a phone", out)


# ----- real exports ------------------------------------------------------------------------------

def make_variable(path, width=320, height=180, rates=(30, 120), seconds=1.5, channels=1, audio=True):
    """A video whose frame rate really changes: `seconds` at each of `rates`, then sound to match."""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    for rate in rates:
        cmd += ["-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={rate}:duration={seconds}"]
    chain = "".join(f"[{i}:v]" for i in range(len(rates))) + f"concat=n={len(rates)}:v=1:a=0[v]"
    cmd += ["-f", "lavfi", "-i", f"sine=duration={seconds * len(rates)}"]
    cmd += ["-filter_complex", chain, "-map", "[v]"]
    cmd += ["-map", f"{len(rates)}:a", "-ac", str(channels), "-c:a", "aac"] if audio else ["-an"]
    cmd += ["-fps_mode", "vfr", "-video_track_timescale", "90000", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path)]
    subprocess.run(cmd, check=True)


def make_constant(path, rate, seconds=2, width=320, height=180):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size={width}x{height}:rate={rate}:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(path)], check=True)


def frame_times(path):
    """When the picture's frames are shown, in seconds, as stored in the file."""
    shown = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "framecrc",
                            "-"], capture_output=True, text=True).stdout
    rows = [line.split(",") for line in shown.splitlines() if line and not line.startswith("#")]
    scale = next(Fraction(line.split()[-1]) for line in shown.splitlines() if line.startswith("#tb 0:"))
    return sorted(int(row[2]) * scale for row in rows)


def stream_line(path, kind):
    text = _ffmpeg_info_text(FFMPEG, path)
    return next(line for line in text.splitlines() if f"{kind}:" in line and "Stream #" in line)


def top_level_boxes(path):
    """Names of the MP4 file's top-level boxes, in order."""
    data, names, pos = path.read_bytes(), [], 0
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        names.append(data[pos + 4:pos + 8].decode("latin-1"))
        if size < 8:
            break
        pos += size
    return names


def h264_profile_and_level(path):
    """(profile_idc, level_idc) from the file's avcC box: 100 is High, 42 is level 4.2."""
    data = path.read_bytes()
    at = data.index(b"avcC")
    return data[at + 5], data[at + 7]


def sound_seconds(path):
    sound = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "s16le", "-ac", "1",
                            "-ar", "48000", "-"], capture_output=True)
    return len(sound.stdout) / 2 / 48000


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class SteadyExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.vfr = cls.tmp / "vfr.mp4"
        make_variable(cls.vfr)                  # 1.5 s at 30 fps, then 1.5 s at 120 fps, 3 s in all

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def export(self, name, edit, source=None, encoder=None):
        return convert_video(FFMPEG, VideoJob(source or self.vfr, self.tmp / name, edit), encoder=encoder)

    def check_phone_file(self, path, rate, seconds, level):
        """Everything a phone is picky about, for a video `seconds` long at `rate` frames a second."""
        times = frame_times(path)
        step = Fraction(1) / rate
        self.assertEqual({b - a for a, b in zip(times, times[1:])}, {step}, "frames are not evenly spaced")
        line = stream_line(path, "Video")
        self.assertRegex(line, rf"\b{float(rate):g} fps, {float(rate):g} tbr")
        self.assertIn("h264 (High)", line)
        self.assertIn("yuv420p", line)
        self.assertEqual(h264_profile_and_level(path), (100, level))
        self.assertAlmostEqual(float(times[-1] + step), seconds, delta=float(step))
        audio = stream_line(path, "Audio")
        self.assertIn("aac (LC)", audio)
        self.assertIn("48000 Hz, stereo", audio)
        self.assertAlmostEqual(sound_seconds(path), float(times[-1] + step), delta=0.05)
        boxes = top_level_boxes(path)
        self.assertLess(boxes.index("moov"), boxes.index("mdat"), boxes)

    def test_the_test_clip_really_is_variable(self):
        info = probe_media(FFMPEG, self.vfr)
        self.assertTrue(info.variable_frame_rate)
        self.assertEqual(info.max_fps, 120.0)
        self.assertLess(info.fps, 100)
        self.assertGreater(len({b - a for a, b in zip(frame_times(self.vfr), frame_times(self.vfr)[1:])}), 1)

    def test_steady_60(self):
        out = self.export("steady60.mp4", VideoEdit(aspect=None, frame_rate=60))
        self.check_phone_file(out, 60, 3.0, 41)
        info = probe_media(FFMPEG, out)
        self.assertFalse(info.variable_frame_rate)
        self.assertEqual((info.fps, info.max_fps, info.width, info.height), (60.0, 60.0, 320, 180))

    def test_steady_30(self):
        self.check_phone_file(self.export("steady30.mp4", VideoEdit(aspect=None, frame_rate=30)), 30, 3.0, 41)

    def test_without_the_setting_the_rate_still_varies(self):
        out = self.export("asis.mp4", VideoEdit(aspect=None))
        self.assertGreater(len({b - a for a, b in zip(frame_times(out), frame_times(out)[1:])}), 1)

    def test_cuts_and_speed_keep_picture_and_sound_together(self):
        edit = VideoEdit(aspect=None, frame_rate=60, speed=2.0, keep=[(0.0, 1.0), (1.5, 3.0)])
        out = self.export("speed.mp4", edit)
        self.check_phone_file(out, 60, 1.25, 41)           # 2.5 s of source at double speed

    def test_a_phone_shape_with_a_steady_rate(self):
        out = self.export("shape.mp4", VideoEdit(aspect=(9, 16), frame_rate=30))
        self.check_phone_file(out, 30, 3.0, 41)
        info = probe_media(FFMPEG, out)
        self.assertEqual((info.width, info.height), (180, 320))

    def test_reverse_stays_steady(self):
        out = self.export("reverse.mp4", VideoEdit(aspect=None, frame_rate=30, reverse=True))
        self.check_phone_file(out, 30, 3.0, 41)

    def test_a_slower_source_keeps_its_own_rate(self):
        source = self.tmp / "twentyfive.mp4"
        make_constant(source, 25)
        out = self.export("kept25.mp4", VideoEdit(aspect=None, frame_rate=60), source)
        self.check_phone_file(out, 25, 2.0, 41)

    def test_more_than_two_channels_are_mixed_down_to_stereo(self):
        source = self.tmp / "surround.mp4"
        make_variable(source, seconds=0.5, channels=6)
        self.assertIn("5.1", stream_line(source, "Audio"))
        out = self.export("downmix.mp4", VideoEdit(aspect=None, frame_rate=30), source)
        self.assertIn("48000 Hz, stereo", stream_line(out, "Audio"))

    def test_a_video_without_sound(self):
        source = self.tmp / "silent.mp4"
        make_variable(source, seconds=0.5, audio=False)
        out = self.export("silent-out.mp4", VideoEdit(aspect=None, frame_rate=60), source)
        self.assertNotIn("Audio:", _ffmpeg_info_text(FFMPEG, out))
        times = frame_times(out)
        self.assertEqual({b - a for a, b in zip(times, times[1:])}, {Fraction(1, 60)})

    def test_1080p_at_60_says_level_4_2_and_at_30_says_4_1(self):
        source = self.tmp / "big.mp4"
        make_variable(source, width=1920, height=1080, seconds=0.5)
        edit = VideoEdit(aspect=None, frame_rate=60, resolution=UP_TO_1080)
        self.check_phone_file(self.export("big60.mp4", edit, source), 60, 1.0, 42)
        edit = VideoEdit(aspect=None, frame_rate=30, resolution=UP_TO_1080)
        self.check_phone_file(self.export("big30.mp4", edit, source), 30, 1.0, 41)

    def test_up_to_1080p_brings_a_bigger_picture_down_and_leaves_a_smaller_one(self):
        source = self.tmp / "uhd.mp4"
        make_variable(source, width=2560, height=1440, rates=(30,), seconds=0.4, audio=False)
        out = self.export("uhd-down.mp4", VideoEdit(aspect=None, frame_rate=30, resolution=UP_TO_1080), source)
        info = probe_media(FFMPEG, out)
        self.assertEqual((info.width, info.height), (1920, 1080))
        out = self.export("small-stays.mp4", VideoEdit(aspect=None, frame_rate=30, resolution=UP_TO_1080))
        info = probe_media(FFMPEG, out)
        self.assertEqual((info.width, info.height), (320, 180))

    def test_a_join_has_the_steady_rate_whatever_the_clips_are(self):
        other = self.tmp / "other25.mp4"
        make_constant(other, 25, seconds=1)
        parts = [JoinPart(self.vfr, VideoEdit(aspect=None, frame_rate=60)), JoinPart(other, VideoEdit(aspect=None))]
        out = join_clips(FFMPEG, JoinJob(parts, self.tmp / "joined.mp4"))
        self.check_phone_file(out, 60, 4.0, 41)

    def test_a_join_without_the_setting_still_follows_the_first_clip(self):
        other = self.tmp / "other25b.mp4"
        make_constant(other, 25, seconds=1)
        parts = [JoinPart(other, VideoEdit(aspect=None)), JoinPart(self.vfr, VideoEdit(aspect=None))]
        out = join_clips(FFMPEG, JoinJob(parts, self.tmp / "joined-plain.mp4"))
        self.assertIn("25 fps", stream_line(out, "Video"))

    def test_the_command_line_makes_the_phone_ready_file(self):
        folder = self.tmp / "cli"
        code, out, err = run(self.vfr, "--video", "original", "--fps", "60", "--size", "up1080", "-o", folder,
                             "--gpu", "off")
        self.assertEqual((code, err), (0, ""), out)
        made = folder / "vfr (phone-ready).mp4"
        self.assertTrue(made.is_file())
        self.check_phone_file(made, 60, 3.0, 41)

    def test_the_graphics_card_exports_the_same_phone_file(self):
        found = gpu.detect_encoders(FFMPEG)
        if not found:
            self.skipTest("no working graphics card encoder here")
        for encoder in found:
            with self.subTest(encoder=encoder):
                out = self.export(f"gpu-{encoder}.mp4", VideoEdit(aspect=None, frame_rate=60), encoder=encoder)
                self.check_phone_file(out, 60, 3.0, 41)

    def test_the_graphics_card_failing_still_gives_the_phone_file_on_the_cpu(self):
        broken = {"h264_broken": lambda crf, fps: ["-c:v", "h264_does_not_exist", "-pix_fmt", "yuv420p"]}
        notes = []
        with mock.patch.dict(gpu.ENCODERS, broken):
            out = convert_video(FFMPEG, VideoJob(self.vfr, self.tmp / "fallback.mp4",
                                                 VideoEdit(aspect=None, frame_rate=60)),
                                encoder="h264_broken", on_note=notes.append)
        self.assertEqual(notes, [gpu.CPU_FALLBACK_NOTE])
        self.check_phone_file(out, 60, 3.0, 41)


if __name__ == "__main__":
    unittest.main()
