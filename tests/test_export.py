"""The 2.9 export options: more formats (H.265, WebM, MOV, GIF), fitting a file size, and saving a frame."""

import io
import os
import re
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path
from unittest import mock

from video_converter import __main__ as cli
from video_converter import gpu, output, video
from video_converter.core import (
    ConversionCancelled, ConversionError, MediaInfo, _ffmpeg_info_text, find_ffmpeg, probe_media,
)
from video_converter.jobs import Job, JobState, run_job
from video_converter.output import MB, OUTPUT_FORMATS, SizeFit
from video_converter.project import OUTPUT_AUDIO, PRESETS, ClipSettings, apply_preset, describe, output_file_for
from video_converter.stills import save_frame
from video_converter.video import (
    JoinJob, JoinPart, VideoEdit, VideoJob, convert_video, edit_fit, fit_summary, format_warning, join_clips,
    output_tag, plan_geometry, render_frame, video_command, video_codec_args,
)

FFMPEG = find_ffmpeg()
INFO = MediaInfo(duration=4.0, width=640, height=360, has_audio=True, fps=15.0)
BIG = MediaInfo(duration=600.0, width=1920, height=1080, has_audio=True, fps=30.0)


def after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def make_video(path, width=320, height=180, seconds=2, rate=15, audio=True, hard=False):
    """A test clip. A `hard` one (a zooming fractal, saved at high quality) is a picture that a file size
    really limits: it takes about 2 MB for six seconds at 640x360, far more than a test card."""
    source = (f"mandelbrot=size={width}x{height}:rate={rate}" if hard
              else f"testsrc2=duration={seconds}:size={width}x{height}:rate={rate}")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-t", str(seconds), "-i", source]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", *(["-crf", "12"] if hard else []), "-shortest", str(path)]
    subprocess.run(cmd, check=True)


def info_text(path):
    return _ffmpeg_info_text(FFMPEG, path)


def frame_count(path):
    result = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-i", str(path), "-f", "null", "-"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    return int(re.findall(r"frame=\s*(\d+)", result.stderr)[-1])


def raw_frame(path):
    """The pixels of a picture file, as bytes."""
    return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "rawvideo",
                           "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout


# ----- the arithmetic ---------------------------------------------------------------------------

class BitrateMathTests(unittest.TestCase):
    def plan(self, seconds, mb=10, rungs=None, codec="h264", audio="aac", fps=30.0):
        rungs = rungs or [(None, 1920, 1080), (720, 1280, 720), (480, 854, 480), (360, 640, 360)]
        return output.fit_plan(output.target_bytes(mb), seconds, codec, audio, fps, rungs)

    def test_megabytes_are_a_million_bytes(self):
        self.assertEqual(output.target_bytes(10), 10_000_000)
        self.assertEqual(output.target_bytes(0.5), 500_000)

    def test_everything_is_planned_a_little_under_the_limit(self):
        plan = self.plan(60, rungs=[(None, 640, 360)])
        everything = (plan.kbps + plan.audio_kbps) * 1000 / 8 * 60
        self.assertLess(everything, 10_000_000 * (1 - output.SAFETY) + 100)
        self.assertGreater(everything, 10_000_000 * (1 - output.SAFETY) - 100 * 1000 / 8 * 60 / 1000)   # within 1 kbps

    def test_the_sound_comes_off_first(self):
        with_sound = self.plan(60, rungs=[(None, 640, 360)])
        without = self.plan(60, rungs=[(None, 640, 360)], audio="")
        self.assertEqual(with_sound.audio_kbps, 128)
        self.assertEqual(without.audio_kbps, 0)
        self.assertAlmostEqual(without.kbps - with_sound.kbps, 128, delta=1)

    def test_opus_sound_is_smaller_and_a_tight_budget_squeezes_the_sound_too(self):
        self.assertEqual(output.audio_kbps("opus", 5000), 96)
        self.assertEqual(output.audio_kbps("aac", 5000), 128)
        self.assertEqual(output.audio_kbps("aac", 450), 75)        # a sixth of everything, no more
        self.assertEqual(output.audio_kbps("aac", 100), output.MIN_STEREO_KBPS)
        self.assertEqual(output.audio_kbps("", 5000), 0)

    def test_a_longer_video_gets_a_lower_bitrate(self):
        short, long = self.plan(30, rungs=[(None, 640, 360)]), self.plan(120, rungs=[(None, 640, 360)])
        self.assertAlmostEqual(short.kbps + short.audio_kbps, 4 * (long.kbps + long.audio_kbps), delta=5)

    def test_a_picture_that_fits_keeps_its_size_and_says_nothing(self):
        plan = self.plan(20, mb=25)
        self.assertIsNone(plan.short)
        self.assertEqual(plan.note, "")

    def test_a_picture_with_too_few_bits_is_made_smaller_and_the_note_says_so(self):
        plan = self.plan(60)              # 1.3 Mbps in all: too little for 1080p30, plenty for 720p
        self.assertEqual(plan.short, 720)
        self.assertIn("720p", plan.note)
        self.assertIn("10 MB", plan.note)

    def test_it_goes_down_the_ladder_until_the_picture_fits(self):
        self.assertEqual(self.plan(100).short, 480)
        self.assertEqual(self.plan(140).short, 360)

    def test_h265_and_vp9_need_fewer_bits_for_the_same_picture(self):
        h264 = self.plan(90, mb=25)
        hevc = self.plan(90, mb=25, codec="hevc")
        self.assertEqual(h264.short, 720)
        self.assertIsNone(hevc.short)

    def test_a_higher_frame_rate_needs_more_bits(self):
        self.assertIsNone(self.plan(30, fps=24.0).short)
        self.assertEqual(self.plan(30, fps=60.0).short, 720)

    def test_a_size_nothing_can_fit_is_refused_with_how_much_would(self):
        with self.assertRaises(ConversionError) as caught:
            self.plan(1500)
        message = str(caught.exception)
        self.assertIn("too long to fit in 10 MB", message)
        self.assertIn("Shorten the video", message)
        self.assertRegex(message, r"about \d+:\d+\.\d of it fits")

    def test_a_video_without_length_cannot_be_planned(self):
        with self.assertRaises(ConversionError):
            self.plan(0)

    FIT = SizeFit(10 * MB, 8000, 128, 10.0)
    AIM = 10 * MB * (1 - output.SAFETY / 2)          # the middle of the margin under the limit

    def test_after_a_file_comes_out_too_big_the_bitrate_goes_down_in_proportion(self):
        again = output.next_kbps(self.FIT, [(8000, 11 * MB)])
        self.assertAlmostEqual(again, 8000 * self.AIM / (11 * MB), delta=2)

    def test_after_a_file_comes_out_well_under_the_limit_it_goes_up(self):
        again = output.next_kbps(self.FIT, [(8000, 8 * MB)])
        self.assertAlmostEqual(again, 8000 * self.AIM / (8 * MB), delta=2)

    def test_two_tries_show_how_far_the_bits_reach_and_the_next_one_uses_it(self):
        # A rate control that gives back only the square root of what is asked: size = c * kbps ** 0.5.
        def size(kbps):
            return int(11 * MB * (kbps / 8000) ** 0.5)
        exact = 8000 * (self.AIM / (11 * MB)) ** 2
        tries = [(8000, size(8000)), (4000, size(4000))]
        again = output.next_kbps(self.FIT, tries)
        self.assertAlmostEqual(again, exact, delta=exact * 0.01)
        # One try alone would have guessed too high: it expects the file to shrink as fast as the bitrate.
        self.assertGreater(output.next_kbps(self.FIT, tries[:1]), exact * 1.05)

    def test_a_simple_picture_that_does_not_use_the_bits_is_left_alone(self):
        self.assertIsNone(output.next_kbps(self.FIT, [(8000, 3 * MB)]))
        # ... unless an earlier try was too big: then this one only overshot on the way down.
        self.assertIsNotNone(output.next_kbps(self.FIT, [(8000, 11 * MB), (4000, 3 * MB)]))

    def test_one_try_never_lowers_the_bitrate_below_a_third(self):
        self.assertEqual(output.next_kbps(self.FIT, [(8000, 1000 * MB)]), int(8000 * 0.3))

    def test_it_gives_up_when_the_bitrate_would_be_tiny_or_would_not_change(self):
        self.assertIsNone(output.next_kbps(SizeFit(10 * MB, 100, 128, 10.0), [(100, 50 * MB)]))
        self.assertIsNone(output.next_kbps(self.FIT, [(8000, int(self.AIM))]))

    def test_gif_helpers(self):
        self.assertEqual(output.gif_rate(15, 30.0), 15.0)
        self.assertEqual(output.gif_rate(15, 10.0), 10.0)               # never repeats frames
        self.assertEqual(output.gif_rate(15, None), 15.0)
        self.assertEqual(output.gif_warning(None), "")
        self.assertEqual(output.gif_warning(10), "")
        self.assertIn("big", output.gif_warning(60))
        self.assertIn("1:00.0", output.gif_warning(60))


class PlanForAnEditTests(unittest.TestCase):
    """The length that matters is the one after the cuts and the speed change."""

    def fit(self, **changes):
        return edit_fit(VideoEdit(aspect=None, target_mb=10.0, **changes), BIG)

    def test_cuts_and_speed_decide_the_length(self):
        edit, fit = self.fit(keep=[(0.0, 100.0)], speed=2.0)
        self.assertAlmostEqual(fit.seconds, 50.0)
        self.assertEqual(edit.resolution, 720)

    def test_the_whole_video_is_too_long_for_the_size_but_a_part_of_it_fits(self):
        with self.assertRaises(ConversionError):
            self.fit()
        _, fit = self.fit(keep=[(0.0, 60.0)])
        self.assertAlmostEqual(fit.seconds, 60.0)

    def test_a_trim_counts_too(self):
        _, fit = self.fit(start=10.0, end=70.0)
        self.assertAlmostEqual(fit.seconds, 60.0, delta=0.1)

    def test_no_sound_leaves_more_for_the_picture(self):
        _, with_sound = self.fit(keep=[(0.0, 60.0)])
        _, muted = self.fit(keep=[(0.0, 60.0)], mute=True)
        self.assertEqual(muted.audio_kbps, 0)
        self.assertGreater(muted.kbps, with_sound.kbps)

    def test_the_music_is_a_sound_too(self):
        info = MediaInfo(duration=60.0, width=1280, height=720, has_audio=False, fps=30.0)
        _, silent = edit_fit(VideoEdit(aspect=None, target_mb=10.0), info)
        _, scored = edit_fit(VideoEdit(aspect=None, target_mb=10.0, music="song.mp3"), info)
        self.assertEqual(silent.audio_kbps, 0)
        self.assertEqual(scored.audio_kbps, 128)

    def test_h265_keeps_a_size_that_h264_would_lower(self):
        info = MediaInfo(duration=90.0, width=1920, height=1080, has_audio=True, fps=30.0)
        h264, _ = edit_fit(VideoEdit(aspect=None, target_mb=25.0), info)
        hevc, _ = edit_fit(VideoEdit(aspect=None, target_mb=25.0, video_format="hevc"), info)
        self.assertEqual(h264.resolution, 720)
        self.assertIsNone(hevc.resolution)

    def test_the_edit_given_is_not_changed(self):
        edit = VideoEdit(aspect=None, target_mb=10.0, keep=[(0.0, 60.0)])
        lowered, _ = edit_fit(edit, BIG)
        self.assertIsNone(edit.resolution)
        self.assertEqual(lowered.resolution, 720)

    def test_a_steady_frame_rate_is_what_the_picture_is_planned_at(self):
        # A 60 fps source needs the picture lowered; the same video at a steady 30 fps does not.
        info = MediaInfo(duration=28.0, width=1920, height=1080, has_audio=True, fps=60.0)
        as_it_is, _ = edit_fit(VideoEdit(aspect=None, target_mb=10.0), info)
        steady, _ = edit_fit(VideoEdit(aspect=None, target_mb=10.0, frame_rate=30), info)
        self.assertEqual(as_it_is.resolution, 720)
        self.assertIsNone(steady.resolution)

    def test_without_a_file_size_or_for_a_gif_nothing_is_planned(self):
        edit = VideoEdit(aspect=None)
        self.assertEqual(edit_fit(edit, BIG), (edit, None))
        gif = VideoEdit(aspect=None, target_mb=10.0, video_format="gif")
        self.assertEqual(edit_fit(gif, BIG), (gif, None))

    def test_a_video_without_a_length_cannot_be_fitted(self):
        with self.assertRaises(ConversionError) as caught:
            edit_fit(VideoEdit(aspect=None, target_mb=10.0), MediaInfo(duration=None, width=640, height=360))
        self.assertIn("length is unknown", str(caught.exception))

    def test_everything_cut_out_says_so(self):
        with self.assertRaises(ConversionError) as caught:
            edit_fit(VideoEdit(aspect=None, target_mb=10.0, start=5.0, end=5.5, keep=[(0.0, 0.2)]), INFO)
        self.assertIn("Nothing is left", str(caught.exception))

    def test_the_panel_summary(self):
        text, failed = fit_summary(VideoEdit(aspect=None, target_mb=25.0), INFO)
        self.assertFalse(failed)
        self.assertIn("Mbps", text)
        text, failed = fit_summary(VideoEdit(aspect=None, target_mb=10.0), BIG)
        self.assertTrue(failed)
        self.assertIn("too long to fit", text)
        self.assertEqual(fit_summary(VideoEdit(aspect=None), BIG), ("", False))
        self.assertEqual(fit_summary(VideoEdit(aspect=None, target_mb=10.0, video_format="gif"), BIG), ("", False))
        lowered = fit_summary(VideoEdit(aspect=None, target_mb=10.0, keep=[(0.0, 60.0)]), BIG)[0]
        self.assertIn("720p", lowered)

    def test_only_a_long_gif_gets_a_warning(self):
        gif = VideoEdit(aspect=None, video_format="gif")
        self.assertIn("GIF", format_warning(gif, BIG))
        self.assertEqual(format_warning(VideoEdit(aspect=None, video_format="gif", keep=[(0.0, 5.0)]), BIG), "")
        self.assertEqual(format_warning(VideoEdit(aspect=None), BIG), "")


# ----- the settings -----------------------------------------------------------------------------

class SettingsTests(unittest.TestCase):
    def test_the_formats(self):
        self.assertEqual(list(OUTPUT_FORMATS), ["mp4", "hevc", "webm", "mov", "gif"])
        self.assertEqual({k: f.extension for k, f in OUTPUT_FORMATS.items()},
                         {"mp4": ".mp4", "hevc": ".mp4", "webm": ".webm", "mov": ".mov", "gif": ".gif"})
        self.assertEqual({k: (f.codec, f.audio) for k, f in OUTPUT_FORMATS.items()},
                         {"mp4": ("h264", "aac"), "hevc": ("hevc", "aac"), "webm": ("vp9", "opus"),
                          "mov": ("h264", "aac"), "gif": ("gif", "")})
        self.assertIn("older phones", OUTPUT_FORMATS["hevc"].hint)

    def test_the_defaults_are_the_old_behaviour(self):
        edit = VideoEdit()
        self.assertEqual((edit.video_format, edit.target_mb, edit.gif_fps, edit.gif_width), ("mp4", 0.0, 15, 480))

    def test_an_unknown_format_means_mp4(self):
        self.assertEqual(VideoEdit(video_format="avi").output_format, OUTPUT_FORMATS["mp4"])

    def test_old_presets_load_with_the_defaults(self):
        old = ClipSettings().to_dict()
        for name in ("video_format", "target_mb", "gif_fps", "gif_width"):
            del old["edit"][name]
        self.assertEqual(ClipSettings.from_dict(old), ClipSettings())

    def test_bad_values_from_a_damaged_file_are_ignored(self):
        bad = {"video_format": "avi", "target_mb": -3, "gif_fps": 0, "gif_width": 5}
        for name, value in (*bad.items(), ("video_format", 7), ("target_mb", "10"), ("target_mb", 1e9),
                            ("target_mb", float("nan")), ("target_mb", True), ("gif_fps", 120), ("gif_fps", 12.5),
                            ("gif_fps", True), ("gif_width", 99999), ("gif_width", "480")):
            with self.subTest(name=name, value=value):
                loaded = ClipSettings.from_dict({"edit": {name: value}})
                self.assertEqual(loaded, ClipSettings())

    def test_good_values_load(self):
        loaded = ClipSettings.from_dict({"edit": {"video_format": "gif", "target_mb": 12.5, "gif_fps": 10,
                                                  "gif_width": 320}}).edit
        self.assertEqual((loaded.video_format, loaded.target_mb, loaded.gif_fps, loaded.gif_width),
                         ("gif", 12.5, 10, 320))
        self.assertEqual(ClipSettings.from_dict({"edit": {"target_mb": 0}}).edit.target_mb, 0.0)
        self.assertEqual(ClipSettings.from_dict({"edit": {"target_mb": 16}}).edit.target_mb, 16.0)

    def test_the_phone_preset_is_always_h264_in_an_mp4_without_a_size_limit(self):
        settings = ClipSettings(edit=VideoEdit(video_format="hevc", target_mb=10.0))
        phone = apply_preset(settings, PRESETS["Play on phone (keeps the whole picture)"])
        self.assertEqual((phone.edit.video_format, phone.edit.target_mb), ("mp4", 0.0))
        other = apply_preset(settings, PRESETS["TikTok, Reels, Shorts"])
        self.assertEqual(other.edit.video_format, "hevc")           # the other presets leave the format alone

    def test_file_names_follow_the_format(self):
        source = Path("clip.mkv")
        names = {}
        for key in OUTPUT_FORMATS:
            names[key] = output_file_for(source, ClipSettings(edit=VideoEdit(video_format=key)), Path("out")).name
        self.assertEqual(names, {"mp4": "clip (phone).mp4", "hevc": "clip (phone H.265).mp4",
                                 "webm": "clip (phone).webm", "mov": "clip (phone).mov", "gif": "clip (phone).gif"})
        joined = output_file_for(source, ClipSettings(edit=VideoEdit(video_format="webm")), Path("out"), joined=True)
        self.assertEqual(joined.name, "clip (joined).webm")
        self.assertEqual(output_file_for(source, ClipSettings(output=OUTPUT_AUDIO, edit=VideoEdit(video_format="gif")),
                                         Path("out")).name, "clip.mp3")
        self.assertEqual(output_tag(VideoEdit(aspect=None, frame_rate=60, video_format="hevc")),
                         "phone-ready H.265")

    def test_an_h265_file_never_replaces_the_h264_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = output_file_for(Path("clip.mkv"), ClipSettings(), Path(tmp))
            first.write_bytes(b"x")
            second = output_file_for(Path("clip.mkv"), ClipSettings(edit=VideoEdit(video_format="hevc")), Path(tmp))
            self.assertNotEqual(first, second)

    def test_the_summary_names_the_format_and_the_size(self):
        self.assertTrue(describe(ClipSettings()).startswith("MP4 ·"))
        self.assertTrue(describe(ClipSettings(edit=VideoEdit(video_format="hevc"))).startswith("H.265 ·"))
        self.assertTrue(describe(ClipSettings(edit=VideoEdit(video_format="webm"))).startswith("WebM ·"))
        self.assertTrue(describe(ClipSettings(edit=VideoEdit(video_format="gif"))).startswith("GIF ·"))
        self.assertIn("fits 16 MB", describe(ClipSettings(edit=VideoEdit(target_mb=16.0))))
        self.assertNotIn("fits", describe(ClipSettings(edit=VideoEdit(target_mb=16.0, video_format="gif"))))


# ----- the ffmpeg options -----------------------------------------------------------------------

class CommandTests(unittest.TestCase):
    FAST = MediaInfo(duration=4.0, width=640, height=360, has_audio=True, fps=60.0)

    def command(self, encoder=None, fit=None, first_pass=False, info=INFO, **edit):
        job = VideoJob(Path("clip.mp4"), Path("out.bin"), VideoEdit(aspect=None, **edit))
        return video_command("ffmpeg", job, info, encoder=encoder, fit=fit, first_pass=first_pass)

    def test_the_default_is_exactly_what_it_always_was(self):
        cmd = self.command()
        self.assertEqual(cmd[cmd.index("-c:a"):cmd.index("-map_metadata")],
                         ["-c:a", "aac", "-b:a", "192k", "-c:v", "libx264", "-preset", "medium", "-pix_fmt",
                          "yuv420p", "-threads", str(video.ENCODER_THREADS), "-r", "15", "-crf", "23",
                          "-profile:v", "high", "-level", "4.1", "-movflags", "+faststart"])

    def test_h265(self):
        cmd = self.command(video_format="hevc")
        self.assertEqual(after(cmd, "-c:v"), "libx265")
        self.assertEqual(after(cmd, "-tag:v"), "hvc1")                 # what Apple's players want
        self.assertEqual(after(cmd, "-crf"), "28")                     # the same look as x264's 23
        self.assertIn("+faststart", cmd)
        self.assertEqual(after(cmd, "-c:a"), "aac")

    def test_h265_quality_follows_the_quality_setting(self):
        crfs = [after(self.command(video_format="hevc", crf=crf), "-crf") for crf in (18, 23, 28)]
        self.assertEqual(crfs, ["23", "28", "33"])

    def test_webm(self):
        cmd = self.command(video_format="webm")
        self.assertEqual(after(cmd, "-c:v"), "libvpx-vp9")
        self.assertEqual(after(cmd, "-crf"), "32")
        self.assertEqual(after(cmd, "-b:v"), "0")
        self.assertEqual(after(cmd, "-c:a"), "libopus")
        self.assertNotIn("-movflags", cmd)

    def test_mov(self):
        cmd = self.command(video_format="mov")
        self.assertEqual(after(cmd, "-c:v"), "libx264")
        self.assertEqual(after(cmd, "-c:a"), "aac")
        self.assertIn("+faststart", cmd)

    def test_a_gif_takes_two_commands_and_has_no_sound(self):
        palette = self.command(video_format="gif", first_pass=True)
        graph = after(palette, "-filter_complex")
        self.assertIn("palettegen", graph)
        self.assertIn("fps=15", graph)
        self.assertIn("scale=w='min(480,iw)'", graph)
        self.assertEqual(after(palette, "-map"), "[p]")
        self.assertIn("palette.png", palette)
        self.assertNotIn("-c:a", palette)
        gif = self.command(video_format="gif")
        graph = after(gif, "-filter_complex")
        self.assertIn("paletteuse", graph)
        self.assertEqual(after(gif, "-loop"), "0")                     # forever
        self.assertIn("-an", gif)
        self.assertNotIn("-c:a", gif)
        self.assertEqual(gif.count("-i"), 2)                           # the video and its palette
        self.assertTrue(gif[-1].endswith("out.bin"))

    def test_the_gif_limits_are_the_settings(self):
        graph = after(self.command(video_format="gif", gif_fps=10, gif_width=320, first_pass=True), "-filter_complex")
        self.assertIn("fps=10", graph)
        self.assertIn("min(320,iw)", graph)

    def test_a_gif_never_has_more_frames_than_its_video(self):
        slow = MediaInfo(duration=4.0, width=640, height=360, has_audio=False, fps=10.0)
        graph = after(self.command(video_format="gif", first_pass=True, info=slow), "-filter_complex")
        self.assertIn("fps=10", graph)
        self.assertNotIn("fps=15", graph)

    def test_a_faster_gif_is_capped_after_the_speed_change(self):
        graph = after(self.command(video_format="gif", first_pass=True, speed=2.0), "-filter_complex")
        self.assertIn("setpts=PTS/2", graph)
        self.assertIn("fps=15", graph)

    def test_a_gif_ignores_the_sound_settings(self):
        cmd = self.command(video_format="gif", normalize=True, music="song.mp3", volume=6.0)
        self.assertNotIn("loudnorm", " ".join(cmd))
        self.assertNotIn("song.mp3", " ".join(cmd))

    def test_two_passes_of_x264(self):
        fit = SizeFit(10 * MB, 1200, 128, 60.0)
        first = self.command(fit=fit, first_pass=True)
        self.assertEqual(after(first, "-pass"), "1")
        self.assertEqual(after(first, "-passlogfile"), video.PASSLOG)
        self.assertEqual(after(first, "-b:v"), "1200k")
        self.assertEqual(first[first.index("-f"):first.index("-f") + 2], ["-f", "null"])
        self.assertIn("-an", first)
        self.assertNotIn("-c:a", first)
        self.assertNotIn("-movflags", first)
        self.assertNotIn("[a]", after(first, "-filter_complex"))        # no sound in the first pass
        second = self.command(fit=fit)
        self.assertEqual(after(second, "-pass"), "2")
        self.assertEqual(after(second, "-passlogfile"), video.PASSLOG)
        self.assertEqual(after(second, "-b:v"), "1200k")
        self.assertEqual(after(second, "-b:a"), "128k")
        self.assertNotIn("-crf", second)
        self.assertIn("+faststart", second)
        self.assertEqual(after(first, "-r"), after(second, "-r"))      # the rate is said out loud, both times

    def test_the_other_formats_use_their_own_two_pass_options(self):
        fit = SizeFit(10 * MB, 1200, 96, 60.0)
        hevc = self.command(fit=fit, first_pass=True, video_format="hevc")
        self.assertEqual(after(hevc, "-x265-params"), f"pass=1:stats={video.PASSLOG}.log")
        self.assertEqual(after(self.command(fit=fit, video_format="hevc"), "-x265-params"),
                         f"pass=2:stats={video.PASSLOG}.log")
        webm = self.command(fit=fit, video_format="webm")
        self.assertEqual(after(webm, "-pass"), "2")
        self.assertEqual(after(webm, "-b:a"), "96k")
        self.assertEqual(after(webm, "-b:v"), "1200k")

    def test_the_graphics_card_gets_a_bitrate_and_a_maximum_not_two_passes(self):
        fit = SizeFit(10 * MB, 1200, 128, 60.0)
        for encoder in gpu.ENCODERS:
            with self.subTest(encoder=encoder):
                cmd = self.command(encoder=encoder, fit=fit)
                self.assertEqual(after(cmd, "-c:v"), encoder)
                self.assertEqual(after(cmd, "-b:v"), "1200k")
                self.assertEqual(after(cmd, "-maxrate"), "1200k")
                self.assertNotIn("-pass", cmd)
                for quality in ("-cq", "-global_quality", "-qp_i"):
                    self.assertNotIn(quality, cmd)
                self.assertIn("-r", cmd)

    def test_h265_on_the_graphics_card(self):
        for encoder in gpu.HEVC_ENCODERS:
            with self.subTest(encoder=encoder):
                cmd = self.command(encoder=encoder, video_format="hevc")
                self.assertEqual(after(cmd, "-c:v"), encoder)
                self.assertEqual(after(cmd, "-tag:v"), "hvc1")
        self.assertEqual(set(gpu.HEVC_ENCODERS), {"hevc_nvenc", "hevc_qsv", "hevc_amf"})

    def test_a_card_that_makes_the_wrong_codec_is_not_used(self):
        cmd = self.command(encoder="h264_nvenc", video_format="hevc")
        self.assertEqual(after(cmd, "-c:v"), "libx265")
        cmd = self.command(encoder="hevc_nvenc")
        self.assertEqual(after(cmd, "-c:v"), "libx264")
        cmd = self.command(encoder="h264_nvenc", video_format="webm")
        self.assertEqual(after(cmd, "-c:v"), "libvpx-vp9")

    def test_a_steady_frame_rate_makes_h264_phone_safe_but_not_the_other_formats(self):
        mp4 = self.command(frame_rate=60, info=self.FAST)
        self.assertEqual(after(mp4, "-level"), "4.1")
        self.assertEqual(after(mp4, "-profile:v"), "high")
        self.assertEqual(mp4[mp4.index("-c:a"):mp4.index("-c:a") + 8],
                         ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
        mov = self.command(frame_rate=60, video_format="mov", info=self.FAST)
        self.assertEqual(after(mov, "-level"), "4.1")
        for key in ("hevc", "webm"):
            cmd = self.command(frame_rate=60, video_format=key, info=self.FAST)
            self.assertNotIn("-level", cmd)
            self.assertNotIn("-profile:v", cmd)
            self.assertIn("fps=60", after(cmd, "-filter_complex"))           # still steady

    def test_a_steady_frame_rate_and_a_file_size_work_together(self):
        fit = SizeFit(10 * MB, 1200, 128, 60.0)
        cmd = self.command(frame_rate=30, fit=fit, info=self.FAST)
        self.assertEqual(after(cmd, "-level"), "4.1")
        self.assertEqual(after(cmd, "-r"), "30")
        self.assertIn("fps=30", after(cmd, "-filter_complex"))

    def test_a_join_uses_the_first_clips_format_and_size(self):
        job = JoinJob([JoinPart(Path("a.mp4"), VideoEdit(aspect=None, video_format="webm"), INFO),
                       JoinPart(Path("b.mp4"), VideoEdit(aspect=None, video_format="mov"), INFO)], Path("out.webm"))
        cmd, total = video.join_command("ffmpeg", job)
        self.assertEqual(after(cmd, "-c:v"), "libvpx-vp9")
        self.assertEqual(after(cmd, "-c:a"), "libopus")
        fit = SizeFit(10 * MB, 900, 96, total)
        first, _ = video.join_command("ffmpeg", job, fit=fit, first_pass=True)
        self.assertEqual(after(first, "-pass"), "1")
        self.assertIn("-an", first)
        self.assertEqual(after(first, "-filter_complex").count("concat=n=2:v=1:a=0"), 1)
        second, _ = video.join_command("ffmpeg", job, fit=fit)
        self.assertEqual((after(second, "-pass"), after(second, "-b:a")), ("2", "96k"))

    def test_a_join_cannot_be_a_gif(self):
        job = JoinJob([JoinPart(Path("a.mp4"), VideoEdit(video_format="gif"), INFO),
                       JoinPart(Path("b.mp4"), VideoEdit(), INFO)], Path("out.gif"))
        with self.assertRaises(ConversionError) as caught:
            video.join_command("ffmpeg", job)
        self.assertIn("can't be saved as a GIF", str(caught.exception))

    def test_a_joined_mp3_ignores_the_format(self):
        job = JoinJob([JoinPart(Path("a.mp4"), VideoEdit(video_format="gif"), INFO),
                       JoinPart(Path("b.mp4"), VideoEdit(), INFO)], Path("out.mp3"), audio_only=True)
        self.assertIn("libmp3lame", video.join_command("ffmpeg", job)[0])


class GraphicsCardH265Tests(unittest.TestCase):
    def test_the_options_use_the_same_quality_numbers_as_h264(self):
        for name, make in gpu.ENCODERS.items():
            hevc = gpu.HEVC_ENCODERS[name.replace("h264", "hevc")](23, 30.0)
            # the same options, with the quality number of their own (see gpu.QUALITY_ANCHORS)
            def without_numbers(args):
                skip = {"-cq", "-global_quality", "-qp_i", "-qp_p"}
                return [a for i, a in enumerate(args) if not (i and args[i - 1] in skip)]
            self.assertEqual(without_numbers([a.replace("hevc", "h264") for a in hevc if a not in ("-tag:v", "hvc1")]),
                             without_numbers(make(23, 30.0)))
            self.assertEqual(hevc[-2:], ["-tag:v", "hvc1"])

    def test_the_bitrate_replaces_the_quality(self):
        nvenc = gpu.encoder_args("hevc_nvenc", 23, 30.0, kbps=2000)
        self.assertEqual(nvenc[:2], ["-c:v", "hevc_nvenc"])
        self.assertEqual((nvenc[nvenc.index("-rc") + 1], nvenc[nvenc.index("-b:v") + 1]), ("vbr", "2000k"))
        self.assertEqual(nvenc[nvenc.index("-maxrate") + 1], "2000k")
        self.assertNotIn("-cq", nvenc)
        qsv = gpu.encoder_args("h264_qsv", 23, 30.0, kbps=2000)
        self.assertEqual(qsv[qsv.index("-b:v") + 1], "2000k")
        self.assertNotIn("-global_quality", qsv)
        amf = gpu.encoder_args("hevc_amf", 23, 30.0, kbps=2000)
        self.assertEqual(amf[amf.index("-rc") + 1], "vbr_peak")
        self.assertNotIn("-qp_i", amf)

    def test_without_a_bitrate_the_h264_options_are_unchanged(self):
        self.assertEqual(gpu.encoder_args("h264_nvenc", 23, 30.0), gpu.ENCODERS["h264_nvenc"](23, 30.0))
        self.assertEqual(gpu.encoder_args("h264_nvenc", 23, 30.0, "4.2")[-4:], ["-profile:v", "high", "-level", "42"])

    def test_the_codec_of_an_encoder(self):
        self.assertEqual(gpu.encoder_codec("h264_qsv"), "h264")
        self.assertEqual(gpu.encoder_codec("hevc_amf"), "hevc")

    def test_h265_encoders_are_looked_for_on_their_own(self):
        gpu.forget_detection()
        try:
            with mock.patch.object(gpu, "_encoder_works", side_effect=lambda ffmpeg, name: name == "hevc_qsv") as works:
                self.assertEqual(gpu.detect_encoders("fake-ffmpeg", "hevc"), ("hevc_qsv",))
                self.assertEqual({call.args[1] for call in works.call_args_list}, set(gpu.HEVC_ENCODERS))
                self.assertEqual(gpu.best_encoder("fake-ffmpeg", "hevc"), "hevc_qsv")
                self.assertEqual(gpu.choose_encoder("fake-ffmpeg", True, "hevc"), "hevc_qsv")
                self.assertEqual(gpu.detect_encoders("fake-ffmpeg"), ())      # the H.264 ones are a separate list
                self.assertEqual(works.call_count, len(gpu.HEVC_ENCODERS) + len(gpu.ENCODERS))
        finally:
            gpu.forget_detection()

    def test_no_card_for_webm_gif_or_when_the_user_says_no(self):
        with mock.patch.object(gpu, "detect_encoders", side_effect=AssertionError("looked")):
            self.assertIsNone(gpu.choose_encoder("fake-ffmpeg", True, "vp9"))
            self.assertIsNone(gpu.choose_encoder("fake-ffmpeg", True, "gif"))
            self.assertIsNone(gpu.choose_encoder("fake-ffmpeg", False, "hevc"))

    def test_a_card_is_named_by_its_maker(self):
        self.assertEqual(gpu.gpu_name("hevc_qsv"), "Intel graphics")
        self.assertEqual(gpu.gpu_name("hevc_amf"), "AMD graphics card")

    def test_a_job_asks_for_the_encoders_of_its_format(self):
        asked = []
        job = Job(Path("clip.mp4"), ClipSettings(edit=VideoEdit(video_format="hevc")))
        with mock.patch("video_converter.jobs.choose_encoder", lambda ffmpeg, use_gpu, codec: asked.append(codec)), \
                mock.patch("video_converter.jobs.convert_video", return_value=Path("out.mp4")):
            run_job("ffmpeg", job, lambda f: None)
            job.settings.edit.video_format = "webm"
            run_job("ffmpeg", job, lambda f: None)
            job.settings.edit.video_format = "mp4"
            run_job("ffmpeg", job, lambda f: None)
        self.assertEqual(asked, ["hevc", "vp9", "h264"])


# ----- the passes, without ffmpeg ---------------------------------------------------------------

class FittingLoopTests(unittest.TestCase):
    """What happens after a file comes out: kept, made again smaller, made again bigger, or refused."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.output = self.tmp / "out.mp4"
        self.runs = []

    def finish(self, sizes, encoder=None, fit=None, prepass=True, fail_encoder=False, notes=None):
        """Run `_finish` with an ffmpeg that writes a file of the next size each time. Returns the result path."""
        fit = fit or SizeFit(1_000_000, 1000, 128, 8.0)
        sizes = list(sizes)

        def fake(cmd, out, duration, on_progress=None, cancel_event=None, cwd=None):
            self.runs.append((cmd[0], fit.kbps, cwd))
            if cmd[0] == "encode:h264_bogus" and fail_encoder:
                raise ConversionError("no such encoder")
            if cmd[0].startswith("encode"):
                out.write_bytes(b"x" * sizes.pop(0))
            return out

        with mock.patch.object(video, "run_ffmpeg", fake):
            self.fit = fit
            return video._finish(lambda transforms, enc, loudness: [f"encode:{enc}"], {}, None, encoder, self.output,
                                 8.0, None, None, self.tmp, (notes.append if notes is not None else None), fit,
                                 (lambda transforms: ["prepass"]) if prepass else None)

    def names(self):
        return [name for name, _, _ in self.runs]

    def encodes(self):
        return [kbps for name, kbps, _ in self.runs if name.startswith("encode")]

    def size(self):
        return self.output.stat().st_size

    def test_a_file_that_fits_is_kept_after_one_try(self):
        self.finish([970_000])
        self.assertEqual(self.names(), ["prepass", "encode:None"])
        self.assertEqual(self.size(), 970_000)

    def test_the_first_pass_comes_before_the_encode_and_again_for_every_new_bitrate(self):
        self.finish([1_080_000, 975_000])
        self.assertEqual(self.names(), ["prepass", "encode:None", "prepass", "encode:None"])
        first, second = (kbps for name, kbps, _ in self.runs if name == "prepass")
        self.assertEqual(first, 1000)
        self.assertEqual(second, self.encodes()[1])          # the two passes are told the same bitrate

    def test_a_file_over_the_limit_is_made_again_at_a_lower_bitrate(self):
        self.finish([1_080_000, 975_000])
        first, second = self.encodes()
        self.assertEqual(first, 1000)
        self.assertLess(second, first * 0.93)
        self.assertEqual(self.size(), 975_000)

    def test_a_file_that_stays_over_the_limit_fails_and_leaves_nothing(self):
        with self.assertRaises(ConversionError) as caught:
            self.finish([1_200_000, 1_150_000, 1_100_000, 1_050_000, 1_040_000])
        self.assertIn("over the 1 MB limit", str(caught.exception))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.names().count("prepass"), output.TRIES)
        self.assertEqual(len(self.encodes()), output.TRIES)

    def test_a_file_that_cannot_be_squeezed_further_is_refused_at_once(self):
        with self.assertRaises(ConversionError):
            self.finish([5_000_000], fit=SizeFit(1_000_000, 100, 128, 8.0))
        self.assertEqual(len(self.encodes()), 1)
        self.assertFalse(self.output.exists())

    def test_a_file_well_under_the_limit_is_made_again_bigger(self):
        self.finish([800_000, 970_000])
        self.assertEqual(self.size(), 970_000)
        first, second = self.encodes()
        self.assertGreater(second, first)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["out.mp4"])      # no spare left behind

    def test_the_bigger_try_goes_over_and_the_next_is_found_between_the_two(self):
        self.finish([800_000, 1_100_000, 960_000])
        first, second, third = self.encodes()
        self.assertTrue(first < third < second)
        self.assertEqual(self.size(), 960_000)

    def test_the_biggest_file_that_fits_is_kept_when_no_try_lands_close(self):
        self.finish([800_000, 1_100_000, 850_000, 1_050_000])
        self.assertEqual(len(self.encodes()), 4)
        self.assertEqual(self.size(), 850_000)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["out.mp4"])

    def test_it_stops_after_the_most_tries_it_allows(self):
        self.finish([600_000, 650_000, 700_000, 750_000, 800_000])
        self.assertEqual(len(self.encodes()), output.TRIES)
        self.assertEqual(self.size(), 750_000)

    def test_a_simple_picture_that_does_not_use_the_bits_is_left_alone(self):
        self.finish([200_000])
        self.assertEqual(len(self.encodes()), 1)
        self.assertEqual(self.size(), 200_000)

    def test_the_graphics_card_is_tried_first_and_needs_no_first_pass(self):
        self.finish([950_000], encoder="h264_bogus")
        self.assertEqual(self.names(), ["encode:h264_bogus"])

    def test_if_the_card_fails_the_cpu_does_both_passes_and_the_note_says_so(self):
        notes = []
        self.finish([960_000], encoder="h264_bogus", fail_encoder=True, notes=notes)
        self.assertEqual(self.names(), ["encode:h264_bogus", "prepass", "encode:None"])
        self.assertEqual(notes, [gpu.CPU_FALLBACK_NOTE])

    def test_a_card_that_comes_out_too_big_is_asked_again_before_anything_else(self):
        self.finish([1_300_000, 980_000], encoder="h264_bogus")
        self.assertEqual(self.names(), ["encode:h264_bogus", "encode:h264_bogus"])


# ----- real exports -----------------------------------------------------------------------------

@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RealExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "clip.mp4"
        make_video(cls.clip, 640, 360, seconds=3)
        cls.hard = cls.tmp / "hard.mp4"                    # 1.9 MB for six seconds: a size limit means something
        make_video(cls.hard, 640, 360, seconds=6, hard=True)
        cls.big = cls.tmp / "big.mp4"                        # 1080p, to see the picture made smaller
        make_video(cls.big, 1920, 1080, seconds=2, rate=25, hard=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def export(self, name, source=None, encoder=None, notes=None, **edit):
        job = VideoJob(source or self.clip, self.tmp / name, VideoEdit(**{"aspect": None, **edit}))
        convert_video(FFMPEG, job, encoder=encoder, on_note=notes.append if notes is not None else None)
        return job.output

    def test_each_format_makes_the_right_codecs_in_the_right_container(self):
        expected = {
            "mp4": ("Video: h264", "Audio: aac", "mov,mp4,m4a", ".mp4"),
            "hevc": ("Video: hevc", "Audio: aac", "mov,mp4,m4a", ".mp4"),
            "webm": ("Video: vp9", "Audio: opus", "matroska,webm", ".webm"),
            "mov": ("Video: h264", "Audio: aac", "mov,mp4,m4a", ".mov"),
        }
        for key, (picture, sound, container, extension) in expected.items():
            with self.subTest(format=key):
                out = self.export(f"format-{key}{extension}", video_format=key)
                text = info_text(out)
                self.assertIn(picture, text)
                self.assertIn(sound, text)
                self.assertIn(container, text)
                info = probe_media(FFMPEG, out)
                self.assertEqual((info.width, info.height), (640, 360))
                self.assertAlmostEqual(info.duration, 3.0, delta=0.3)
                self.assertTrue(info.has_audio)

    def test_h265_is_tagged_hvc1_for_apple_players(self):
        text = info_text(self.export("tagged.mp4", video_format="hevc"))
        self.assertIn("hvc1", text)

    def test_a_mov_is_a_quicktime_file(self):
        self.assertIn("major_brand     : qt", info_text(self.export("quicktime.mov", video_format="mov")))

    def test_the_quality_setting_still_works_in_every_format(self):
        sizes = {}
        for crf in (18, 28):
            sizes[crf] = self.export(f"q{crf}.mp4", source=self.hard, video_format="hevc", crf=crf).stat().st_size
        self.assertGreater(sizes[18], sizes[28])

    def test_a_steady_frame_rate_in_h265_and_webm(self):
        for key in ("hevc", "webm"):
            with self.subTest(format=key):
                out = self.export(f"steady-{key}.mp4" if key == "hevc" else "steady-webm.webm",
                                  video_format=key, frame_rate=30)
                self.assertAlmostEqual(probe_media(FFMPEG, out).fps, 15.0, delta=0.5)    # slower: keeps its own

    def test_muting_leaves_no_sound_in_any_format(self):
        for key, name in (("hevc", "mute.mp4"), ("webm", "mute.webm"), ("mov", "mute.mov")):
            with self.subTest(format=key):
                self.assertFalse(probe_media(FFMPEG, self.export(name, video_format=key, mute=True)).has_audio)

    def test_cuts_and_speed_work_in_h265(self):
        out = self.export("cut.mp4", video_format="hevc", keep=[(0.0, 2.0)], speed=2.0)
        self.assertAlmostEqual(probe_media(FFMPEG, out).duration, 1.0, delta=0.3)

    # ----- GIF

    def gif(self, name="out.gif", source=None, **edit):
        return self.export(name, source=source, video_format="gif", **edit)

    def test_a_gif_is_a_silent_looping_picture_no_wider_than_480(self):
        out = self.gif(source=self.hard)
        text = info_text(out)
        self.assertIn("Video: gif", text)
        self.assertNotIn("Audio:", text)
        info = probe_media(FFMPEG, out)
        self.assertEqual((info.width, info.height), (480, 270))
        self.assertAlmostEqual(frame_count(out), 6 * 15, delta=1)                 # 15 frames a second, from a 15 fps video
        data = out.read_bytes()
        self.assertTrue(data.startswith(b"GIF89a"))
        at = data.index(b"NETSCAPE2.0")
        self.assertEqual(data[at + 11:at + 16], b"\x03\x01\x00\x00\x00")       # loop count 0: forever

    def test_the_width_is_a_cap_and_a_narrower_picture_is_not_stretched_to_it(self):
        self.assertEqual(probe_media(FFMPEG, self.gif("small.gif", gif_width=320)).width, 320)
        narrow = self.tmp / "narrow-source.mp4"
        make_video(narrow, 200, 120, seconds=1)
        self.assertEqual(probe_media(FFMPEG, self.gif("narrow.gif", source=narrow)).width, 200)

    def test_the_frame_rate_is_a_cap(self):
        self.assertAlmostEqual(frame_count(self.gif("fps10.gif", gif_fps=10)), 3 * 10, delta=1)
        slow = self.tmp / "slow.mp4"
        make_video(slow, 160, 90, seconds=2, rate=10)
        self.assertAlmostEqual(frame_count(self.gif("slow.gif", source=slow)), 2 * 10, delta=1)    # nothing repeated

    def test_cuts_and_speed_apply_to_a_gif(self):
        out = self.gif("fast.gif", keep=[(0.0, 2.0)], speed=2.0)
        self.assertAlmostEqual(frame_count(out), 15, delta=1)

    def test_the_gif_uses_the_edits_too(self):
        out = self.gif("bw.gif", look="bw", aspect=(1, 1))
        info = probe_media(FFMPEG, out)
        self.assertEqual((info.width, info.height), (360, 360))
        raw = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(out), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        pixels = [raw[i:i + 3] for i in range(0, len(raw), 3)]
        self.assertTrue(all(abs(p[0] - p[1]) < 24 and abs(p[1] - p[2]) < 24 for p in pixels[::50]))

    def test_a_gif_leaves_no_files_behind(self):
        before = set(self.tmp.iterdir())
        self.gif("tidy.gif")
        self.assertEqual(set(self.tmp.iterdir()) - before, {self.tmp / "tidy.gif"})

    def test_the_progress_of_a_gif_goes_up_to_one(self):
        progress = []
        convert_video(FFMPEG, VideoJob(self.clip, self.tmp / "progress.gif", VideoEdit(aspect=None, video_format="gif")),
                      on_progress=progress.append)
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], 1.0)
        self.assertTrue(any(0 < p < video.PREPASS_SHARE for p in progress))     # the colors pass counts too

    # ----- a file size

    def assertFits(self, path, megabytes, lowest=0.9):
        size = path.stat().st_size
        self.assertLessEqual(size, megabytes * MB, f"{size} bytes is over {megabytes} MB")
        self.assertGreaterEqual(size, megabytes * MB * lowest, f"{size} bytes is far under {megabytes} MB")

    def test_a_real_two_pass_export_lands_just_under_one_megabyte(self):
        out = self.export("one-mb.mp4", source=self.hard, target_mb=1.0)
        size = out.stat().st_size
        self.assertLessEqual(size, 1_000_000)
        self.assertGreaterEqual(size, 970_000 - 15_000)            # within about 3% under (and a little leeway)
        info = probe_media(FFMPEG, out)
        self.assertEqual((info.width, info.height), (640, 360))   # nothing had to be made smaller
        self.assertTrue(info.has_audio)

    def test_several_sizes_all_land_just_under(self):
        for megabytes in (0.6, 1.0, 1.5):
            with self.subTest(megabytes=megabytes):
                self.assertFits(self.export(f"size-{megabytes}.mp4", source=self.hard, target_mb=megabytes),
                                megabytes, lowest=0.92)

    def test_the_passes_run_in_one_temporary_folder_that_is_removed(self):
        calls, real = [], video.run_ffmpeg

        def record(cmd, out, duration, on_progress=None, cancel_event=None, cwd=None):
            calls.append((cmd, cwd))
            return real(cmd, out, duration, on_progress, cancel_event, cwd)

        with mock.patch.object(video, "run_ffmpeg", record):
            out = self.export("passes.mp4", source=self.hard, target_mb=1.0)
        self.assertGreaterEqual(len(calls), 2)
        (first, first_cwd), (second, second_cwd) = calls[0], calls[1]
        self.assertEqual(after(first, "-pass"), "1")
        self.assertEqual(after(second, "-pass"), "2")
        self.assertEqual(first_cwd, second_cwd)
        self.assertFalse(first_cwd.exists())                       # the passlog went with it
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if "passlog" in p.name), [])
        self.assertTrue(out.is_file())

    def test_the_progress_goes_up_to_one_through_both_passes(self):
        progress = []
        convert_video(FFMPEG, VideoJob(self.hard, self.tmp / "progress-fit.mp4", VideoEdit(aspect=None, target_mb=1.0)),
                      on_progress=progress.append)
        self.assertEqual(progress[-1], 1.0)
        self.assertTrue(all(0.0 <= p <= 1.0 for p in progress))
        self.assertTrue(any(0 < p < video.PREPASS_SHARE for p in progress))      # the first pass counts

    def test_every_format_fits(self):
        for key, name in (("hevc", "fit-hevc.mp4"), ("webm", "fit-webm.webm"), ("mov", "fit-mov.mov")):
            with self.subTest(format=key):
                out = self.export(name, source=self.hard, target_mb=1.0, video_format=key)
                self.assertFits(out, 1.0, lowest=0.8)

    def test_cuts_and_speed_make_room(self):
        # Half the video at twice the speed is a second and a half: the picture gets more than four times the bitrate.
        bitrates = {}
        for name, edit in (("whole", {}), ("quick", {"keep": [(0.0, 3.0)], "speed": 2.0})):
            calls, real = [], video.run_ffmpeg

            def record(cmd, out, duration, on_progress=None, cancel_event=None, cwd=None):
                calls.append(cmd)
                return real(cmd, out, duration, on_progress, cancel_event, cwd)

            with mock.patch.object(video, "run_ffmpeg", record):
                out = self.export(f"fit-{name}.mp4", source=self.hard, target_mb=1.0, **edit)
            bitrates[name] = int(after(calls[0], "-b:v").rstrip("k"))
            self.assertFits(out, 1.0, lowest=0.8)
        self.assertAlmostEqual(bitrates["quick"] / bitrates["whole"], 4.3, delta=0.5)
        self.assertAlmostEqual(probe_media(FFMPEG, out).duration, 1.5, delta=0.3)

    def test_a_picture_that_cannot_look_decent_is_made_smaller_and_the_note_says_so(self):
        notes = []
        out = self.export("fit-small.mp4", source=self.big, target_mb=0.3, notes=notes)
        info = probe_media(FFMPEG, out)
        self.assertEqual((info.width, info.height), (1280, 720))
        self.assertEqual(len(notes), 1)
        self.assertIn("720p", notes[0])
        self.assertLessEqual(out.stat().st_size, 300_000)

    def test_a_size_that_nothing_can_fit_fails_before_encoding_anything(self):
        target = self.tmp / "impossible.mp4"
        calls = []
        with mock.patch.object(video, "run_ffmpeg", lambda *a, **k: calls.append(a)):
            with self.assertRaises(ConversionError) as caught:
                convert_video(FFMPEG, VideoJob(self.hard, target, VideoEdit(aspect=None, target_mb=0.05)))
        self.assertIn("too long to fit in 0.05 MB", str(caught.exception))
        self.assertEqual(calls, [])
        self.assertFalse(target.exists())

    def test_a_join_fits_too(self):
        parts = [JoinPart(self.hard, VideoEdit(aspect=None, target_mb=1.5)), JoinPart(self.hard, VideoEdit(aspect=None))]
        out = join_clips(FFMPEG, JoinJob(parts, self.tmp / "joined-fit.mp4"))
        self.assertFits(out, 1.5, lowest=0.85)
        self.assertAlmostEqual(probe_media(FFMPEG, out).duration, 12.0, delta=0.5)

    def test_a_join_in_another_format(self):
        parts = [JoinPart(self.clip, VideoEdit(aspect=None, video_format="webm")), JoinPart(self.clip, VideoEdit(aspect=None))]
        out = join_clips(FFMPEG, JoinJob(parts, self.tmp / "joined.webm"))
        text = info_text(out)
        self.assertIn("Video: vp9", text)
        self.assertIn("Audio: opus", text)

    def test_a_job_reports_what_it_did_to_fit(self):
        job = Job(self.big, ClipSettings(edit=VideoEdit(aspect=None, target_mb=0.3, use_gpu=False)),
                  Path(self.tmp), info=None)
        out = run_job(FFMPEG, job, lambda f: None)
        self.assertIn("720p", job.note)
        self.assertEqual(out.suffix, ".mp4")
        self.assertLessEqual(out.stat().st_size, 300_000)

    def test_cancelling_during_either_pass_leaves_nothing(self):
        for name, condition, runs in (("cancel-first", lambda f: f > 0, 1),
                                      ("cancel-second", lambda f: f > video.PREPASS_SHARE, 2)):
            with self.subTest(name=name):
                cancel, calls, real = threading.Event(), [], video.run_ffmpeg

                def record(cmd, out, duration, on_progress=None, cancel_event=None, cwd=None):
                    calls.append(cwd)
                    return real(cmd, out, duration, on_progress, cancel_event, cwd)

                def progress(fraction):
                    if condition(fraction):
                        cancel.set()

                target = self.tmp / f"{name}.mp4"
                with mock.patch.object(video, "run_ffmpeg", record), self.assertRaises(ConversionCancelled):
                    convert_video(FFMPEG, VideoJob(self.hard, target, VideoEdit(aspect=None, target_mb=1.0)),
                                  on_progress=progress, cancel_event=cancel)
                self.assertEqual(len(calls), runs)
                self.assertFalse(target.exists())
                self.assertFalse(calls[0].exists())

    # ----- the graphics card

    def hevc_card(self):
        found = gpu.detect_encoders(FFMPEG, "hevc")
        if not found:
            self.skipTest("no graphics card that makes H.265 here")
        return found[0]

    def test_h265_on_the_graphics_card(self):
        encoder = self.hevc_card()
        out = self.export("card-hevc.mp4", source=self.hard, video_format="hevc", encoder=encoder)
        text = info_text(out)
        self.assertIn("Video: hevc", text)
        self.assertIn("hvc1", text)

    def test_a_file_size_on_the_graphics_card_is_checked_and_corrected(self):
        encoder = self.hevc_card()
        out = self.export("card-fit.mp4", source=self.hard, video_format="hevc", encoder=encoder, target_mb=1.0)
        self.assertFits(out, 1.0, lowest=0.7)
        self.assertIn("Video: hevc", info_text(out))

    def test_a_graphics_card_that_fails_h265_falls_back_to_the_cpu(self):
        bogus = {"hevc_bogus": lambda crf, fps: ["-c:v", "hevc_does_not_exist", "-pix_fmt", "yuv420p"]}
        notes = []
        with mock.patch.dict(gpu.HEVC_ENCODERS, bogus):
            out = self.export("fallback-hevc.mp4", video_format="hevc", encoder="hevc_bogus", notes=notes)
        self.assertIn("Video: hevc", info_text(out))
        self.assertEqual(notes, [gpu.CPU_FALLBACK_NOTE])

    def test_the_fallback_keeps_the_file_size(self):
        bogus = {"hevc_bogus": lambda crf, fps: ["-c:v", "hevc_does_not_exist", "-pix_fmt", "yuv420p"]}
        notes = []
        with mock.patch.dict(gpu.HEVC_ENCODERS, bogus):
            out = self.export("fallback-fit.mp4", source=self.hard, video_format="hevc", encoder="hevc_bogus",
                              target_mb=1.0, notes=notes)
        self.assertFits(out, 1.0, lowest=0.8)
        self.assertEqual(notes, [gpu.CPU_FALLBACK_NOTE])


# ----- saving a frame ---------------------------------------------------------------------------

@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class FrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "clip.mp4"
        make_video(cls.clip, 640, 360, seconds=3)
        cls.info = probe_media(FFMPEG, cls.clip)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_frame_is_what_render_frame_makes_at_the_full_output_size(self):
        edit = VideoEdit(aspect=(9, 16), resolution=720, look="vintage", brightness=0.1, zoom=1.5, flip=True)
        geometry = plan_geometry(self.info.width, self.info.height, edit)
        saved = save_frame(FFMPEG, self.clip, self.tmp / "frame.png", 1.2, edit, self.info)
        reference = render_frame(FFMPEG, self.clip, self.tmp / "reference.png", 1.2,
                                 (geometry.out_width, geometry.out_height), edit, self.info)
        self.assertEqual((geometry.out_width, geometry.out_height), (720, 1280))
        self.assertEqual(raw_frame(saved), raw_frame(reference))
        self.assertEqual(len(raw_frame(saved)), 720 * 1280 * 3)

    def test_it_is_bigger_than_the_preview(self):
        edit = VideoEdit(aspect=(9, 16), resolution=720)
        preview = render_frame(FFMPEG, self.clip, self.tmp / "preview.png", 1.0, (300, 300), edit, self.info)
        saved = save_frame(FFMPEG, self.clip, self.tmp / "full.png", 1.0, edit, self.info)
        self.assertLess(len(raw_frame(preview)), len(raw_frame(saved)))

    def test_the_original_shape_keeps_the_videos_own_size(self):
        saved = save_frame(FFMPEG, self.clip, self.tmp / "same.png", 0.5, VideoEdit(aspect=None), self.info)
        self.assertEqual(len(raw_frame(saved)), 640 * 360 * 3)

    def test_every_edit_is_applied(self):
        plain = raw_frame(save_frame(FFMPEG, self.clip, self.tmp / "plain.png", 1.0, VideoEdit(aspect=None), self.info))
        grey = raw_frame(save_frame(FFMPEG, self.clip, self.tmp / "grey.png", 1.0,
                                    VideoEdit(aspect=None, look="bw"), self.info))
        self.assertNotEqual(plain, grey)
        self.assertTrue(all(grey[i] == grey[i + 1] == grey[i + 2] for i in range(0, len(grey) - 2, 3 * 97)))
        rotated = save_frame(FFMPEG, self.clip, self.tmp / "turned.png", 1.0, VideoEdit(aspect=None, rotate=90),
                             self.info)
        self.assertEqual(len(raw_frame(rotated)), 360 * 640 * 3)

    def test_a_cut_moves_the_fade_to_where_the_frame_lands_in_the_export(self):
        faded = VideoEdit(aspect=None, fade_in=2.0)
        start = raw_frame(save_frame(FFMPEG, self.clip, self.tmp / "fade-start.png", 0.0, faded, self.info))
        later = raw_frame(save_frame(FFMPEG, self.clip, self.tmp / "fade-later.png", 2.5, faded, self.info))
        self.assertLess(sum(start), sum(later) / 4)                 # black at the start, the picture later

    def test_different_moments_give_different_frames(self):
        edit = VideoEdit(aspect=None)
        one = raw_frame(save_frame(FFMPEG, self.clip, self.tmp / "t1.png", 0.2, edit, self.info))
        two = raw_frame(save_frame(FFMPEG, self.clip, self.tmp / "t2.png", 2.2, edit, self.info))
        self.assertNotEqual(one, two)

    def test_a_jpg_is_a_jpeg_at_the_same_size(self):
        edit = VideoEdit(aspect=(1, 1), resolution=360)
        for name in ("frame.jpg", "frame.JPEG"):
            saved = save_frame(FFMPEG, self.clip, self.tmp / name, 1.0, edit, self.info)
            self.assertEqual(saved.read_bytes()[:2], b"\xff\xd8")
            text = info_text(saved)
            self.assertIn("Video: mjpeg", text)
            self.assertIn("360x360", text)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("fve-frame*")), [])

    def test_a_png_is_a_png(self):
        saved = save_frame(FFMPEG, self.clip, self.tmp / "really.png", 1.0, VideoEdit(aspect=None), self.info)
        self.assertEqual(saved.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_the_folder_is_made_if_needed(self):
        saved = save_frame(FFMPEG, self.clip, self.tmp / "new folder" / "frame.png", 1.0, VideoEdit(aspect=None),
                           self.info)
        self.assertTrue(saved.is_file())

    def test_other_extensions_and_files_without_a_picture_are_refused(self):
        with self.assertRaises(ConversionError) as caught:
            save_frame(FFMPEG, self.clip, self.tmp / "frame.bmp", 1.0, VideoEdit(aspect=None), self.info)
        self.assertIn("PNG or a JPG", str(caught.exception))
        with self.assertRaises(ConversionError):
            save_frame(FFMPEG, self.clip, self.tmp / "frame.png", 1.0, VideoEdit(aspect=None),
                       MediaInfo(duration=3.0, has_audio=True))

    def test_a_moment_after_the_end_saves_the_last_frame(self):
        target = self.tmp / "late.png"
        save_frame(FFMPEG, self.clip, target, 99.0, VideoEdit(aspect=None), self.info)
        self.assertTrue(target.is_file())
        self.assertGreater(target.stat().st_size, 0)


# ----- the command line -------------------------------------------------------------------------

def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.run_cli([str(a) for a in args])
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue(), err.getvalue()


class CommandLineOptionTests(unittest.TestCase):
    def test_bad_options_are_refused_with_exit_code_2(self):
        cases = {
            "unknown format": ["clip.mp4", "--format", "avi"],
            "size too small": ["clip.mp4", "--target-size", "0.1"],
            "size too big": ["clip.mp4", "--target-size", "99999"],
            "size not a number": ["clip.mp4", "--target-size", "big"],
            "gif and a size": ["clip.mp4", "--format", "gif", "--target-size", "10"],
            "gif fps without gif": ["clip.mp4", "--format", "mp4", "--gif-fps", "10"],
            "gif width without a format": ["clip.mp4", "--gif-width", "320"],
            "gif fps too high": ["clip.mp4", "--format", "gif", "--gif-fps", "99"],
            "gif width not whole": ["clip.mp4", "--format", "gif", "--gif-width", "3.5"],
            "frame without a file": ["clip.mp4", "--frame-at", "1"],
            "file without a frame": ["clip.mp4", "--frame-out", "f.png"],
            "frame in another format": ["clip.mp4", "--frame-at", "1", "--frame-out", "f.bmp"],
            "frame of two files": ["a.mp4", "b.mp4", "--frame-at", "1", "--frame-out", "f.png"],
            "frame with a joined export": ["a.mp4", "b.mp4", "--join", "--frame-at", "1", "--frame-out", "f.png"],
            "frame with a format": ["clip.mp4", "--frame-at", "1", "--frame-out", "f.png", "--format", "gif"],
            "frame with a size": ["clip.mp4", "--frame-at", "1", "--frame-out", "f.png", "--target-size", "10"],
            "frame at a bad time": ["clip.mp4", "--frame-at", "soon", "--frame-out", "f.png"],
        }
        for name, args in cases.items():
            with self.subTest(name):
                code, _, err = run(*args)
                self.assertEqual(code, 2, err)

    def test_the_messages_say_what_is_wrong(self):
        self.assertIn("--target-size must be between", run("clip.mp4", "--target-size", "0.1")[2])
        self.assertIn("need --format gif", run("clip.mp4", "--gif-fps", "10")[2])
        self.assertIn("go together", run("clip.mp4", "--frame-at", "1")[2])
        self.assertIn(".png or .jpg", run("clip.mp4", "--frame-at", "1", "--frame-out", "f.bmp")[2])
        self.assertIn("doesn't work for a GIF", run("clip.mp4", "--format", "gif", "--target-size", "5")[2])

    def test_the_new_options_are_in_the_help(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.run_cli(["--help"])
        for text in ("--format", "--target-size", "--frame-at", "--frame-out", "--gif-fps", "--gif-width", "hevc"):
            self.assertIn(text, out.getvalue())


class CommandLineSettingsTests(unittest.TestCase):
    """What the options turn into, with the export itself replaced."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.clip = self.tmp / "clip.mp4"
        self.clip.write_bytes(b"not really a video")

    def export(self, *args):
        jobs = []
        info = MediaInfo(duration=20.0, width=1280, height=720, has_audio=True, fps=30.0)
        with mock.patch.object(cli, "find_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(cli, "choose_encoder", return_value=None) as choose, \
                mock.patch.object(cli, "probe_media", return_value=info), \
                mock.patch.object(cli, "convert_video", side_effect=lambda ffmpeg, job, **k: jobs.append(job) or job.output):
            code, out, err = run(self.clip, *args)
        return code, jobs, err

    def test_a_format_alone_makes_a_video_of_the_original_shape(self):
        code, jobs, _ = self.export("--format", "webm")
        self.assertEqual(code, 0)
        self.assertEqual(jobs[0].edit.video_format, "webm")
        self.assertIsNone(jobs[0].edit.aspect)
        self.assertEqual(jobs[0].output.name, "clip (edited).webm")

    def test_each_format_names_its_file(self):
        names = {}
        for key in ("mp4", "hevc", "webm", "mov", "gif"):
            names[key] = self.export("--format", key, "--overwrite")[1][0].output.name
        self.assertEqual(names, {"mp4": "clip (edited).mp4", "hevc": "clip (edited H.265).mp4",
                                 "webm": "clip (edited).webm", "mov": "clip (edited).mov", "gif": "clip (edited).gif"})

    def test_without_the_options_nothing_changes(self):
        code, jobs, _ = self.export("--video", "phone")
        self.assertEqual((jobs[0].edit.video_format, jobs[0].edit.target_mb), ("mp4", 0.0))
        self.assertEqual(jobs[0].output.name, "clip (phone).mp4")

    def test_a_target_size(self):
        code, jobs, _ = self.export("--video", "phone", "--target-size", "16", "--format", "hevc")
        self.assertEqual((jobs[0].edit.target_mb, jobs[0].edit.video_format), (16.0, "hevc"))
        self.assertEqual(jobs[0].output.name, "clip (phone H.265).mp4")

    def test_a_size_alone_makes_a_video_too(self):
        code, jobs, _ = self.export("--target-size", "10")
        self.assertEqual((code, jobs[0].edit.target_mb, jobs[0].output.name), (0, 10.0, "clip (edited).mp4"))

    def test_the_gif_limits(self):
        _, jobs, _ = self.export("--format", "gif")
        self.assertEqual((jobs[0].edit.gif_fps, jobs[0].edit.gif_width), (15, 480))
        _, jobs, _ = self.export("--format", "gif", "--gif-fps", "10", "--gif-width", "320", "--overwrite")
        self.assertEqual((jobs[0].edit.gif_fps, jobs[0].edit.gif_width), (10, 320))

    def test_a_long_gif_prints_a_warning_but_still_exports(self):
        code, jobs, err = self.export("--format", "gif")
        self.assertEqual(code, 0)
        self.assertIn("warning", err)
        self.assertIn("GIF", err)
        self.assertEqual(len(jobs), 1)
        _, jobs, err = self.export("--format", "gif", "--end", "5", "--overwrite")
        self.assertNotIn("warning", err)

    def test_a_joined_export_takes_the_formats_extension(self):
        other = self.tmp / "other.mp4"
        other.write_bytes(b"x")
        info = MediaInfo(duration=5.0, width=640, height=360, has_audio=True, fps=30.0)
        jobs = []
        with mock.patch.object(cli, "find_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(cli, "choose_encoder", return_value=None), \
                mock.patch.object(cli, "probe_media", return_value=info), \
                mock.patch.object(cli, "join_clips", side_effect=lambda ffmpeg, job, **k: jobs.append(job) or job.output):
            code, out, err = run(self.clip, other, "--join", "--video", "original", "--format", "webm")
        self.assertEqual(code, 0, err)
        self.assertEqual(jobs[0].output.name, "clip (joined).webm")
        self.assertEqual(jobs[0].parts[0].edit.video_format, "webm")

    def test_a_failure_is_reported_and_the_exit_code_says_so(self):
        info = MediaInfo(duration=20.0, width=1280, height=720, has_audio=True, fps=30.0)
        with mock.patch.object(cli, "find_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(cli, "choose_encoder", return_value=None), \
                mock.patch.object(cli, "probe_media", return_value=info), \
                mock.patch.object(cli, "convert_video", side_effect=ConversionError("This video is too long to fit")):
            code, _, err = run(self.clip, "--target-size", "1")
        self.assertEqual(code, 1)
        self.assertIn("FAILED - This video is too long to fit", err)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RealCommandLineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "clip.mp4"
        make_video(cls.clip, 640, 360, seconds=3)
        cls.hard = cls.tmp / "hard.mp4"
        make_video(cls.hard, 640, 360, seconds=6, hard=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_frame(self):
        target = self.tmp / "frames" / "frame.png"
        target.parent.mkdir()
        code, out, err = run(self.clip, "--frame-at", "1.5", "--frame-out", target)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(raw_frame(target)), 640 * 360 * 3)
        self.assertIn("frame.png", out)

    def test_a_frame_with_edits_at_the_export_size(self):
        target = self.tmp / "phone.jpg"
        code, _, err = run(self.clip, "--video", "phone", "--size", "720", "--look", "bw", "--frame-at", "0:01",
                           "--frame-out", target)
        self.assertEqual(code, 0, err)
        self.assertIn("720x1280", info_text(target))

    def test_a_frame_never_replaces_a_file_unless_asked(self):
        target = self.tmp / "keep.png"
        target.write_bytes(b"precious")
        code, out, err = run(self.clip, "--frame-at", "1", "--frame-out", target)
        self.assertEqual(code, 0, err)
        self.assertEqual(target.read_bytes(), b"precious")
        self.assertTrue((self.tmp / "keep (1).png").is_file())
        code, _, err = run(self.clip, "--frame-at", "1", "--frame-out", target, "--overwrite")
        self.assertEqual(code, 0, err)
        self.assertNotEqual(target.read_bytes(), b"precious")

    def test_a_frame_after_the_end_fails_with_a_message(self):
        target = self.tmp / "late.png"
        code, _, err = run(self.clip, "--frame-at", "30", "--frame-out", target)
        self.assertEqual(code, 1)
        self.assertIn("only 0:03.0 long", err)
        self.assertFalse(target.exists())

    def test_a_frame_of_a_missing_file(self):
        code, _, err = run(self.tmp / "missing.mp4", "--frame-at", "1", "--frame-out", self.tmp / "m.png")
        self.assertEqual(code, 1)
        self.assertIn("File not found", err)

    def test_a_gif(self):
        code, out, err = run(self.clip, "--format", "gif", "--gif-width", "320", "-o", self.tmp / "gifs")
        self.assertEqual(code, 0, err)
        gif = self.tmp / "gifs" / "clip (edited).gif"
        self.assertEqual(probe_media(FFMPEG, gif).width, 320)

    def test_a_target_size(self):
        code, out, err = run(self.hard, "--target-size", "1", "-o", self.tmp / "sized", "--gpu", "off")
        self.assertEqual(code, 0, err)
        size = (self.tmp / "sized" / "hard (edited).mp4").stat().st_size
        self.assertLessEqual(size, 1_000_000)
        self.assertGreaterEqual(size, 940_000)

    def test_h265_and_webm_from_the_command_line(self):
        for key, name in (("hevc", "clip (edited H.265).mp4"), ("webm", "clip (edited).webm")):
            with self.subTest(format=key):
                code, _, err = run(self.clip, "--format", key, "-o", self.tmp / key, "--gpu", "off")
                self.assertEqual(code, 0, err)
                self.assertTrue((self.tmp / key / name).is_file())


if __name__ == "__main__":
    unittest.main()
