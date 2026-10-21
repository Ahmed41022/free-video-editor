import re
import subprocess
import sys
import tempfile
import unittest
from array import array
from pathlib import Path

from video_converter.core import (
    ConversionError,
    ConversionJob,
    MediaInfo,
    convert,
    find_ffmpeg,
    format_time,
    parse_media_info,
    parse_time,
    probe_media,
)
from video_converter.video import (
    JoinJob,
    JoinPart,
    VideoEdit,
    VideoJob,
    convert_video,
    join_clips,
    join_command,
    format_memory,
    plan_geometry,
    render_frame,
    render_source_frames,
    REVERSE_BASE_BYTES,
    reverse_memory,
    reverse_warning,
    video_command,
)

FFMPEG = find_ffmpeg()
ROOT = Path(__file__).resolve().parent.parent
FRAME_BYTES = 16 * 9


def make_video(ffmpeg, path, width=640, height=360, seconds=4, audio=True, rotation=None, rate=15):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={width}x{height}:rate={rate}"]
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

    def test_cover_art_is_not_a_video_track(self):
        info = parse_media_info(
            "  Duration: 00:00:08.05\n  Stream #0:0: Audio: mp3 (mp3float), 44100 Hz, mono\n"
            "  Stream #0:1: Video: mjpeg (Baseline), yuvj420p, 300x300 [SAR 1:1 DAR 1:1], 90k tbr (attached pic)\n")
        self.assertFalse(info.has_video)
        self.assertTrue(info.has_audio)
        # A real video stream after the cover art still counts.
        info = parse_media_info(
            "  Stream #0:0: Video: mjpeg, yuvj420p, 300x300, 90k tbr (attached pic)\n"
            "  Stream #0:1: Video: h264 (High), yuv420p, 1280x720, 30 fps\n")
        self.assertEqual((info.width, info.height, info.fps), (1280, 720, 30.0))


class GeometryTests(unittest.TestCase):
    def test_landscape_to_phone_crops_center(self):
        g = plan_geometry(1920, 1080, VideoEdit(aspect=(9, 16)))
        cw, ch, x, y = g.crop
        self.assertEqual(ch, 1080)
        # "Same as source" follows the source's short side: 1080p makes the standard 1080x1920.
        self.assertEqual((g.out_width, g.out_height), (1080, 1920))
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


class OutputSizeTests(unittest.TestCase):
    """"Same as source" never enlarges what is exported, and the shape stays exact."""

    def size(self, width, height, **edit):
        g = plan_geometry(width, height, VideoEdit(**edit))
        return g.out_width, g.out_height

    def test_same_as_source_follows_the_source_up_to_1080(self):
        # The short side matches the source, so 1080p gives the 1080x1920 that phone apps expect.
        self.assertEqual(self.size(1920, 1080, aspect=(9, 16)), (1080, 1920))
        self.assertEqual(self.size(1920, 1080, aspect=(4, 5)), (1080, 1350))
        self.assertEqual(self.size(1920, 1080, aspect=(1, 1)), (1080, 1080))
        self.assertEqual(self.size(1920, 1080, aspect=(16, 9)), (1920, 1080))

    def test_phone_portrait_and_square_stop_at_1080_but_widescreen_does_not(self):
        # 4K cropped to 9:16 used to come out 2160x3840.
        self.assertEqual(self.size(3840, 2160, aspect=(9, 16)), (1080, 1920))
        self.assertEqual(self.size(3840, 2160, aspect=(4, 5)), (1080, 1350))
        self.assertEqual(self.size(3840, 2160, aspect=(1, 1)), (1080, 1080))
        self.assertEqual(self.size(3840, 2160, aspect=(16, 9)), (3840, 2160))

    def test_a_zoomed_crop_keeps_the_output_size(self):
        g = plan_geometry(1920, 1080, VideoEdit(aspect=(1, 1), zoom=2.0))
        self.assertEqual(g.crop[:2], (540, 540))
        self.assertEqual((g.out_width, g.out_height), (1080, 1080))

    def test_a_fit_uses_the_sources_short_side_up_to_1080(self):
        for fit in ("blur", "bars"):
            self.assertEqual(self.size(1920, 1080, aspect=(9, 16), fit=fit), (1080, 1920))
            self.assertEqual(self.size(640, 360, aspect=(9, 16), fit=fit), (360, 640))
            self.assertEqual(self.size(3840, 2160, aspect=(9, 16), fit=fit), (1080, 1920))
            # Widescreen isn't capped: a 4K widescreen video stays 4K.
            self.assertEqual(self.size(3840, 2160, aspect=(16, 9), fit=fit), (3840, 2160))

    def test_the_original_shape_keeps_the_exact_source_size(self):
        for size in ((1920, 1080), (3840, 2160), (1280, 720), (1080, 1920), (1918, 1080), (854, 480)):
            self.assertEqual(self.size(*size, aspect=None), size)
        self.assertEqual(self.size(1920, 1080, aspect=None, rotate=90), (1080, 1920))
        self.assertEqual(self.size(641, 359, aspect=None), (640, 358))      # odd sizes can't be kept

    def test_zooming_the_original_shape_keeps_the_full_size(self):
        # Zooming in keeps the full output size, as it did before 2.3.1.
        self.assertEqual(self.size(1920, 1080, aspect=None, zoom=2.0), (1920, 1080))

    def test_a_chosen_size_is_always_used(self):
        self.assertEqual(self.size(640, 360, aspect=(9, 16), resolution=1080), (1080, 1920))
        self.assertEqual(self.size(3840, 2160, aspect=(9, 16), resolution=720), (720, 1280))
        self.assertEqual(self.size(1920, 1080, aspect=None, resolution=480), (854, 480))

    def test_odd_source_sizes_keep_the_exact_shape(self):
        # 641x359 as 9:16 used to give 360x638 (the odd 359 was rounded after the ratio was applied).
        self.assertEqual(self.size(641, 359, aspect=(9, 16), fit="blur"), (360, 640))
        self.assertEqual(self.size(641, 359, aspect=(9, 16), fit="bars"), (360, 640))
        self.assertEqual(self.size(359, 641, aspect=(16, 9), fit="blur"), (640, 360))
        self.assertEqual(self.size(641, 359, aspect=(4, 5), fit="blur"), (360, 450))

    def test_the_shape_is_as_exact_as_even_sizes_allow(self):
        for size in ((1920, 1080), (641, 359), (1279, 719), (3840, 2160), (1080, 1920), (721, 1281)):
            for aspect in ((9, 16), (1, 1), (4, 5), (16, 9)):
                for fit in ("crop", "blur"):
                    for zoom in (1.0, 1.7):
                        with self.subTest(size=size, aspect=aspect, fit=fit, zoom=zoom):
                            out_w, out_h = self.size(*size, aspect=aspect, fit=fit, zoom=zoom)
                            self.assertEqual((out_w % 2, out_h % 2), (0, 0))
                            # Off by less than one pixel on the long side, the best even sizes can do.
                            long_side = max(out_w, out_h)
                            exact = long_side * min(aspect) / max(aspect)
                            self.assertLessEqual(abs(min(out_w, out_h) - exact), 1.0, (out_w, out_h))


def held(edit, info, **kwargs):
    """What reverse_memory counts for the frames alone, without what ffmpeg needs besides them."""
    return reverse_memory(edit, info, **kwargs) - REVERSE_BASE_BYTES


class ReverseMemoryTests(unittest.TestCase):
    HD = MediaInfo(duration=600.0, width=1920, height=1080, has_audio=True, fps=30.0)
    GB = 1024 ** 3

    def test_nothing_is_needed_unless_the_clip_is_reversed(self):
        self.assertEqual(reverse_memory(VideoEdit(aspect=None), self.HD), 0)

    def test_every_frame_is_held_at_the_exported_size(self):
        # 10 s at 30 fps of 1920x1080, about 3 bytes a pixel (the yuv420p frame and ffmpeg's buffers around it).
        edit = VideoEdit(aspect=None, reverse=True, start=0.0, end=10.0)
        self.assertEqual(reverse_memory(edit, self.HD), 300 * 1920 * 1080 * 3 + REVERSE_BASE_BYTES)

    def test_the_output_size_counts_not_the_source_size(self):
        full = held(VideoEdit(aspect=None, reverse=True), self.HD)
        small = held(VideoEdit(aspect=None, reverse=True, resolution=480), self.HD)
        self.assertAlmostEqual(small / full, (854 * 480) / (1920 * 1080), places=4)
        phone = held(VideoEdit(aspect=(9, 16), reverse=True), self.HD)     # exported at 1080x1920
        self.assertEqual(phone, 18000 * 1080 * 1920 * 3)

    def test_only_the_kept_and_sped_up_length_counts(self):
        whole = held(VideoEdit(aspect=None, reverse=True), self.HD)
        cut = held(VideoEdit(aspect=None, reverse=True, keep=[(0.0, 100.0), (200.0, 300.0)]), self.HD)
        self.assertEqual(cut * 3, whole)
        fast = held(VideoEdit(aspect=None, reverse=True, speed=2.0), self.HD)
        self.assertEqual(fast * 2, whole)        # the frame rate stays 30, so twice as fast means half the frames

    def test_a_missing_frame_rate_is_taken_as_30_and_a_missing_length_as_unknown(self):
        guessed = MediaInfo(duration=10.0, width=1920, height=1080, has_audio=True, fps=None)
        edit = VideoEdit(aspect=None, reverse=True)
        self.assertEqual(held(edit, guessed), 300 * 1920 * 1080 * 3)
        self.assertEqual(reverse_memory(edit, MediaInfo(duration=None, width=1920, height=1080, fps=30.0)), 0)

    def test_a_joins_frame_rate_decides_the_frames_of_each_clip(self):
        edit = VideoEdit(aspect=None, reverse=True)
        info = MediaInfo(duration=10.0, width=1920, height=1080, fps=30.0)
        self.assertEqual(held(edit, info, fps=60.0), 2 * held(edit, info))

    def test_sound_alone_is_far_smaller(self):
        edit = VideoEdit(reverse=True)
        sound = reverse_memory(edit, self.HD, sound_only=True)
        self.assertEqual(sound, 600 * 48000 * 2 * 4)
        self.assertLess(sound, reverse_memory(edit, self.HD) / 50)
        self.assertEqual(reverse_memory(edit, MediaInfo(duration=600.0, has_audio=True), sound_only=False), 0)

    def test_the_warning_appears_only_above_2_gb(self):
        self.assertEqual(reverse_warning(2 * self.GB), "")
        self.assertEqual(reverse_warning(0), "")
        text = reverse_warning(int(6.4 * self.GB))
        self.assertEqual(text, "Reversing this needs about 6.4 GB of memory. "
                               "Shorten the clip or lower the size if your PC has less.")
        self.assertIn("2.5 GB", reverse_warning(int(2.5 * self.GB)))

    def test_amounts_are_written_in_gb_or_mb(self):
        self.assertEqual(format_memory(int(1.25 * self.GB)), "1.2 GB")
        self.assertEqual(format_memory(300 * 1024 ** 2), "300 MB")


SOURCE = MediaInfo(duration=6.0, width=640, height=360, has_audio=True, fps=15.0)


def command_for(edit, info=SOURCE):
    return video_command("ffmpeg", VideoJob(Path("in.mp4"), Path("out.mp4"), edit), info)


def graph_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


class VideoCommandTests(unittest.TestCase):
    def test_cuts_seek_to_the_first_kept_part_and_stop_after_the_last(self):
        cmd = command_for(VideoEdit(keep=[(1.0, 2.0), (3.0, 5.0)]))
        before_input = cmd[:cmd.index("-i")]
        self.assertEqual(before_input[-4:], ["-ss", "0.999500", "-to", "4.999500"])

    def test_cut_times_are_relative_to_the_seek_point(self):
        graph = graph_of(command_for(VideoEdit(keep=[(1.0, 2.0), (3.0, 5.0)])))
        # The sound is cut exactly; the picture half a frame (1/30 s at 15 fps) early, between two frames.
        self.assertIn("[vin0_0]trim=start=0:end=0.966667,", graph)
        self.assertIn("[vin0_1]trim=start=1.966667:end=3.966667,", graph)
        self.assertIn("atrim=start=0:end=1,", graph)
        self.assertIn("atrim=start=2:end=4,", graph)
        self.assertIn("concat=n=2:v=1:a=0[", graph)
        self.assertIn("concat=n=2:v=0:a=1[", graph)

    def test_no_cuts_and_no_trim_read_the_whole_input(self):
        cmd = command_for(VideoEdit())
        self.assertNotIn("-ss", cmd)
        self.assertNotIn("split=", graph_of(cmd))

    def test_the_trim_and_the_cuts_combine(self):
        cmd = command_for(VideoEdit(keep=[(1.0, 3.0), (4.0, 6.0)], start=2.0, end=5.0))
        self.assertEqual(cmd[:cmd.index("-i")][-4:], ["-ss", "1.999500", "-to", "4.999500"])
        graph = graph_of(cmd)
        self.assertIn("[vin0_0]trim=start=0:end=0.966667,", graph)          # 2..3, half a frame early
        self.assertIn("[vin0_1]trim=start=1.966667:end=2.966667,", graph)   # 4..5
        self.assertIn("atrim=start=0:end=1,", graph)
        self.assertIn("atrim=start=2:end=3,", graph)

    def test_speed_changes_the_timestamps_and_keeps_the_source_frame_rate(self):
        graph = graph_of(command_for(VideoEdit(speed=2.0)))
        # a quarter of an output frame later (1/60 s), so no frame is half way between two slots
        self.assertIn("[t0]setpts=PTS/2+0.016667/TB,fps=15[tempo0]", graph)
        self.assertIn("atempo=2", graph)

    def test_normal_speed_adds_no_retiming_filters(self):
        graph = graph_of(command_for(VideoEdit()))
        self.assertNotIn("PTS/", graph)
        self.assertNotIn("fps=", graph)
        self.assertNotIn("atempo", graph)

    def test_slow_motion_uses_an_atempo_chain(self):
        graph = graph_of(command_for(VideoEdit(speed=0.25)))
        self.assertIn("setpts=PTS/0.25", graph)
        self.assertIn("atempo=0.5,atempo=0.5", graph)

    def test_reverse_comes_after_the_reframing_and_before_the_fades(self):
        graph = graph_of(command_for(VideoEdit(reverse=True, fade_in=0.5, fade_out=0.5)))
        crop, scale = graph.index("crop="), graph.index("scale=")
        reverse, fade = graph.index("reverse"), graph.index("fade=")
        self.assertLess(max(crop, scale), reverse)
        self.assertLess(reverse, fade)
        self.assertIn("[f0]reverse,fade=t=in:st=0:d=0.5,fade=t=out:st=5.5:d=0.5[v]", graph)
        self.assertIn("areverse", graph)

    def test_fade_out_starts_relative_to_the_length_of_the_export(self):
        # 4 s of the source (after the trim) at 2x is 2 s of export.
        edit = VideoEdit(start=1.0, end=5.0, speed=2.0, fade_out=0.5, audio_fade_out=0.5)
        graph = graph_of(command_for(edit))
        self.assertIn("fade=t=out:st=1.5:d=0.5", graph)
        self.assertIn("afade=t=out:st=1.5:d=0.5", graph)

    def test_picture_and_sound_fades_are_independent(self):
        graph = graph_of(command_for(VideoEdit(fade_in=1.0)))
        self.assertIn("fade=t=in:st=0:d=1", graph)
        self.assertNotIn("afade", graph)
        graph = graph_of(command_for(VideoEdit(audio_fade_in=1.0)))
        self.assertIn("afade=t=in:st=0:d=1", graph)
        self.assertNotIn("[f0]fade", graph)

    def test_mute_drops_the_audio(self):
        cmd = command_for(VideoEdit(mute=True, speed=2.0, audio_fade_in=1.0))
        self.assertIn("-an", cmd)
        self.assertNotIn("[a]", cmd)
        self.assertNotIn("[0:a:0]", graph_of(cmd))
        self.assertNotIn("atempo", graph_of(cmd))

    def test_audio_is_mapped_when_the_source_has_it(self):
        cmd = command_for(VideoEdit())
        self.assertEqual(cmd[cmd.index("[a]") - 1], "-map")
        self.assertNotIn("-an", cmd)
        silent = command_for(VideoEdit(), MediaInfo(duration=6.0, width=640, height=360, has_audio=False, fps=15.0))
        self.assertIn("-an", silent)
        self.assertNotIn("[a]", silent)

    def test_nothing_left_after_trim_and_cuts_is_an_error(self):
        with self.assertRaisesRegex(ConversionError, "Nothing is left"):
            command_for(VideoEdit(keep=[(0.0, 1.0)], start=2.0))

    def test_the_quality_setting_is_passed_on(self):
        cmd = command_for(VideoEdit(crf=28))
        self.assertEqual(cmd[cmd.index("-crf") + 1], "28")


def part(name, width, height, fps, audio, duration=4.0, **edit):
    info = MediaInfo(duration=duration, width=width, height=height, has_audio=audio, fps=fps)
    return JoinPart(Path(name), VideoEdit(aspect=None, **edit), info)


def output_labels(graph):
    """The labels each filter in `graph` writes to."""
    labels = []
    for chain in graph.split(";"):
        found = re.search(r"((?:\[[^\]]+\])+)$", chain)
        labels += re.findall(r"\[[^\]]+\]", found.group(1)) if found else []
    return labels


class JoinCommandTests(unittest.TestCase):
    def build(self, parts, **kwargs):
        return join_command("ffmpeg", JoinJob(parts, Path("out.mp4"), **kwargs))

    def test_every_clip_is_fitted_into_the_first_clips_frame_and_frame_rate(self):
        cmd, _ = self.build([part("a.mp4", 640, 360, 15.0, True), part("b.mp4", 320, 240, 25.0, True)])
        graph = graph_of(cmd)
        self.assertEqual(graph.count("scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                                     "pad=640:360:(ow-iw)/2:(oh-ih)/2:color=black"), 2)
        self.assertIn("[t0]fps=15[tempo0]", graph)
        self.assertIn("[t1]fps=15[tempo1]", graph)
        self.assertNotIn("fps=25", graph)

    def test_the_first_clips_reframing_sets_the_size(self):
        first = part("a.mp4", 640, 360, 15.0, True)
        first.edit.aspect = (9, 16)
        cmd, _ = self.build([first, part("b.mp4", 640, 360, 15.0, True)])
        # 9:16 out of a 640x360 source exports at 360x640 (the source's short side).
        self.assertEqual(graph_of(cmd).count("pad=360:640:"), 2)

    def test_the_first_clips_quality_is_used(self):
        first = part("a.mp4", 640, 360, 15.0, True, crf=28)
        cmd, _ = self.build([first, part("b.mp4", 640, 360, 15.0, True, crf=18)])
        self.assertEqual(cmd[cmd.index("-crf") + 1], "28")

    def test_a_missing_frame_rate_falls_back_to_30(self):
        cmd, _ = self.build([part("a.mp4", 640, 360, None, True), part("b.mp4", 640, 360, 25.0, True)])
        self.assertIn("[t0]fps=30[tempo0]", graph_of(cmd))

    def test_the_result_is_as_long_as_the_clips_added_up(self):
        _, total = self.build([part("a.mp4", 640, 360, 15.0, True, duration=4.0),
                               part("b.mp4", 640, 360, 15.0, True, duration=2.5)])
        self.assertEqual(total, 6.5)

    def test_cuts_and_speed_change_each_clips_length(self):
        _, total = self.build([part("a.mp4", 640, 360, 15.0, True, duration=10.0, keep=[(0.0, 2.0), (6.0, 10.0)],
                                    speed=2.0),
                               part("b.mp4", 640, 360, 15.0, True, duration=4.0, start=1.0, end=3.0, speed=0.5)])
        self.assertEqual(total, 3.0 + 4.0)

    def test_each_clip_is_cut_from_its_own_input(self):
        cmd, _ = self.build([part("a.mp4", 640, 360, 15.0, True, duration=10.0, keep=[(1.0, 2.0), (3.0, 5.0)]),
                             part("b.mp4", 640, 360, 15.0, True, duration=2.0)])
        inputs = cmd[cmd.index("-y") + 1:cmd.index("-filter_complex")]
        # a hair before the times, so the frame that sits on a time is kept (see core.SEEK_SLACK)
        self.assertEqual(inputs, ["-ss", "0.999500", "-to", "4.999500", "-i", "a.mp4", "-to", "1.999500", "-i", "b.mp4"])
        graph = graph_of(cmd)
        self.assertIn("[0:v:0]split=2", graph)
        self.assertIn("[1:v:0]setpts=PTS-STARTPTS[t1]", graph)

    def test_labels_do_not_collide_when_both_clips_use_a_blurred_fit(self):
        parts = [part("a.mp4", 640, 360, 15.0, True), part("b.mp4", 320, 240, 25.0, True)]
        for p in parts:
            p.edit.aspect, p.edit.fit = (9, 16), "blur"
        cmd, _ = self.build(parts)
        labels = output_labels(graph_of(cmd))
        self.assertTrue(labels)
        self.assertEqual(len(labels), len(set(labels)), labels)
        self.assertIn("[bg0]", labels)
        self.assertIn("[bg1]", labels)

    def test_labels_do_not_collide_with_cuts_and_sound(self):
        parts = [part("a.mp4", 640, 360, 15.0, True, duration=10.0, keep=[(0.0, 3.0), (3.0, 5.0), (7.0, 10.0)]),
                 part("b.mp4", 640, 360, 15.0, True, duration=10.0, keep=[(0.0, 3.0), (3.0, 5.0), (7.0, 10.0)])]
        labels = output_labels(graph_of(self.build(parts)[0]))
        self.assertEqual(len(labels), len(set(labels)), labels)

    def test_a_silent_clip_gets_generated_silence_of_its_length(self):
        cmd, _ = self.build([part("a.mp4", 640, 360, 15.0, True, duration=4.0),
                             part("b.mp4", 320, 240, 25.0, False, duration=2.0)])
        graph = graph_of(cmd)
        self.assertEqual(graph.count("anullsrc"), 1)
        self.assertIn("anullsrc=r=48000:cl=stereo,atrim=duration=2.000,", graph)
        self.assertIn("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]", graph)
        self.assertIn("[0:a:0]", graph)
        self.assertNotIn("[1:a:0]", graph)

    def test_silence_matches_the_sped_up_length(self):
        cmd, _ = self.build([part("a.mp4", 640, 360, 15.0, True, duration=4.0),
                             part("b.mp4", 320, 240, 25.0, False, duration=4.0, speed=2.0)])
        self.assertIn("atrim=duration=2.000", graph_of(cmd))

    def test_a_muted_clip_is_replaced_by_silence(self):
        cmd, _ = self.build([part("a.mp4", 640, 360, 15.0, True, mute=True),
                             part("b.mp4", 640, 360, 15.0, True)])
        graph = graph_of(cmd)
        self.assertEqual(graph.count("anullsrc"), 1)
        self.assertNotIn("[0:a:0]", graph)
        self.assertIn("[1:a:0]", graph)

    def test_all_silent_clips_make_a_silent_video(self):
        cmd, _ = self.build([part("a.mp4", 640, 360, 15.0, False), part("b.mp4", 640, 360, 15.0, False)])
        graph = graph_of(cmd)
        self.assertNotIn("anullsrc", graph)
        self.assertIn("concat=n=2:v=1:a=0[v]", graph)
        self.assertIn("-an", cmd)

    def test_an_mp3_join_has_no_picture(self):
        parts = [part("a.mp4", 640, 360, 15.0, True), part("b.mp4", 640, 360, 15.0, False, duration=2.0)]
        cmd, total = self.build(parts, audio_only=True)
        graph = graph_of(cmd)
        self.assertNotIn("[0:v", graph)
        self.assertIn("concat=n=2:v=0:a=1[a]", graph)
        self.assertEqual(graph.count("anullsrc"), 1)
        self.assertIn("-vn", cmd)
        self.assertIn("libmp3lame", cmd)
        self.assertEqual(total, 6.0)

    def test_an_mp3_join_ignores_the_leftover_mute_setting(self):
        cmd, _ = self.build([part("a.mp4", 640, 360, 15.0, True, mute=True), part("b.mp4", 640, 360, 15.0, True)],
                            audio_only=True)
        graph = graph_of(cmd)
        self.assertIn("[0:a:0]", graph)
        self.assertNotIn("anullsrc", graph)

    def test_an_mp3_join_of_silent_clips_is_an_error(self):
        with self.assertRaisesRegex(ConversionError, "have sound"):
            self.build([part("a.mp4", 640, 360, 15.0, False), part("b.mp4", 640, 360, 15.0, False)], audio_only=True)

    def test_a_clip_with_nothing_left_is_an_error(self):
        with self.assertRaisesRegex(ConversionError, "Nothing is left of b.mp4"):
            self.build([part("a.mp4", 640, 360, 15.0, True),
                        part("b.mp4", 640, 360, 15.0, True, keep=[(0.0, 1.0)], start=2.0)])

    def test_an_unknown_length_is_an_error(self):
        with self.assertRaisesRegex(ConversionError, "length of b.mp4"):
            self.build([part("a.mp4", 640, 360, 15.0, True), part("b.mp4", 640, 360, 15.0, True, duration=None)])

    def test_joining_needs_two_clips(self):
        with self.assertRaises(ConversionError):
            join_clips("ffmpeg", JoinJob([part("a.mp4", 640, 360, 15.0, True)], Path("out.mp4")))


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
        # Every fit uses the source's short side: 640x360 becomes 360x640.
        for fit, size in (("crop", (360, 640)), ("blur", (360, 640)), ("bars", (360, 640))):
            with self.subTest(fit=fit):
                info = self.export(f"phone-{fit}.mp4", VideoEdit(aspect=(9, 16), fit=fit))
                self.assertEqual((info.width, info.height), size)
                self.assertTrue(info.has_audio)

    def stream_lengths(self, path, rate):
        """(seconds of picture, seconds of sound) of an exported file: the picture counted in frames,
        the sound decoded to the sample."""
        shown = subprocess.run([FFMPEG, "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
                               capture_output=True, text=True)
        frames = int(re.findall(r"frame=\s*(\d+)", shown.stderr)[-1])
        sound = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "s16le", "-ac", "1",
                                "-ar", "48000", "-"], capture_output=True)
        return frames / rate, len(sound.stdout) / 2 / 48000

    def test_many_cuts_off_the_frame_grid_keep_picture_and_sound_together(self):
        # Thirty parts of 0.21 s (5.25 frames at 25 fps) that start 0.8 of a frame past a frame. The sound
        # is cut to the sample but the picture in whole frames, so unsnapped they drifted ~0.3 s apart.
        source = self.tmp / "grid.mp4"
        make_video(FFMPEG, source, width=160, height=90, seconds=12, rate=25)
        keep = [(0.4 * i + 0.032, 0.4 * i + 0.242) for i in range(30)]
        output = self.tmp / "grid-cut.mp4"
        convert_video(FFMPEG, VideoJob(source, output, VideoEdit(aspect=None, keep=keep)))
        picture, sound = self.stream_lengths(output, 25)
        self.assertLess(abs(picture - sound), 1 / 25)
        self.assertAlmostEqual(picture, 6.0, delta=0.05)         # 30 x 5 frames

    def test_the_same_holds_when_joining_clips(self):
        source = self.tmp / "grid.mp4"
        make_video(FFMPEG, source, width=160, height=90, seconds=12, rate=25)
        keep = [(0.4 * i + 0.032, 0.4 * i + 0.242) for i in range(15)]
        parts = [JoinPart(source, VideoEdit(aspect=None, keep=keep)) for _ in range(2)]
        output = join_clips(FFMPEG, JoinJob(parts, self.tmp / "grid-joined.mp4"))
        picture, sound = self.stream_lengths(output, 25)
        self.assertLess(abs(picture - sound), 1 / 25)
        self.assertAlmostEqual(picture, 6.0, delta=0.1)

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


def raw_gray(path, at=None):
    """Frames of `path` shrunk to 16x9 grayscale, as bytes (FRAME_BYTES each). With `at`, only the frame there."""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    if at is not None:
        cmd += ["-ss", str(at)]
    cmd += ["-i", str(path)]
    if at is not None:
        cmd += ["-frames:v", "1"]
    cmd += ["-vf", "scale=16:9,format=gray", "-f", "rawvideo", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def brightness(frame):
    return sum(frame) / len(frame)


def difference(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def mono_samples(path):
    """The sound of `path` as 8 kHz mono samples (8000 per second)."""
    data = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                           "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    samples = array("h")
    samples.frombytes(data)
    return samples


def audio_end(path):
    """When the last sound packet of `path` ends, by its timestamps (not by how much sound there is)."""
    out = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-c", "copy",
                          "-f", "null", "-progress", "pipe:1", "-"], capture_output=True, text=True,
                         check=True).stdout
    times = [int(line.partition("=")[2]) for line in out.splitlines() if line.startswith("out_time_us=")]
    return times[-1] / 1_000_000


def peak(samples):
    return max((abs(s) for s in samples), default=0)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class TimingExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "clip.mp4"                    # 4 s, 320x180, 15 fps, with sound
        make_video(FFMPEG, cls.clip, 320, 180, 4)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def export(self, name, edit, source=None, **kwargs):
        output = convert_video(FFMPEG, VideoJob(source or self.clip, self.tmp / name, edit), **kwargs)
        return output, probe_media(FFMPEG, output)

    def test_cutting_out_a_middle_part(self):
        _, info = self.export("cut.mp4", VideoEdit(aspect=None, keep=[(0.0, 1.0), (2.0, 4.0)]))
        self.assertAlmostEqual(info.duration, 3.0, delta=0.15)
        self.assertTrue(info.has_audio)

    def test_the_kept_parts_are_the_right_ones(self):
        # Keeping 2..4 only: the export starts on the picture that was at 2 s.
        output, _ = self.export("late.mp4", VideoEdit(aspect=None, keep=[(2.0, 4.0)]))
        self.assertLess(difference(raw_gray(output, 0), raw_gray(self.clip, 2.0)),
                        difference(raw_gray(output, 0), raw_gray(self.clip, 0.0)))
        self.assertLess(difference(raw_gray(output, 0), raw_gray(self.clip, 2.0)), 3)

    def test_cuts_and_trim_work_together(self):
        _, info = self.export("both.mp4", VideoEdit(aspect=None, keep=[(0.0, 1.0), (2.0, 4.0)], start=0.5, end=3.0))
        self.assertAlmostEqual(info.duration, 1.5, delta=0.15)             # 0.5..1 and 2..3

    def test_splits_leave_no_visible_seam(self):
        _, info = self.export("split.mp4", VideoEdit(aspect=None, keep=[(0.0, 2.0), (2.0, 4.0)]))
        self.assertAlmostEqual(info.duration, 4.0, delta=0.15)

    def test_double_speed_halves_the_length_and_keeps_the_frame_rate(self):
        _, info = self.export("fast.mp4", VideoEdit(aspect=None, speed=2.0))
        self.assertAlmostEqual(info.duration, 2.0, delta=0.15)
        self.assertTrue(info.has_audio)
        self.assertAlmostEqual(info.fps, 15.0, delta=0.5)

    def test_slow_motion_on_a_trimmed_second(self):
        _, info = self.export("slow.mp4", VideoEdit(aspect=None, speed=0.5, start=1.0, end=2.0))
        self.assertAlmostEqual(info.duration, 2.0, delta=0.15)

    def test_quarter_speed_uses_a_two_step_audio_chain(self):
        _, info = self.export("quarter.mp4", VideoEdit(aspect=None, speed=0.25, start=0.0, end=1.0))
        self.assertAlmostEqual(info.duration, 4.0, delta=0.2)

    def test_cuts_and_speed_combine(self):
        _, info = self.export("cut-fast.mp4", VideoEdit(aspect=None, keep=[(0.0, 1.0), (2.0, 4.0)], speed=2.0))
        self.assertAlmostEqual(info.duration, 1.5, delta=0.15)

    def test_reverse_starts_on_the_last_picture(self):
        forward, _ = self.export("forward.mp4", VideoEdit(aspect=None))
        backward, info = self.export("backward.mp4", VideoEdit(aspect=None, reverse=True))
        self.assertAlmostEqual(info.duration, 4.0, delta=0.15)
        forward_frames, backward_frames = raw_gray(forward), raw_gray(backward)
        first, last = slice(0, FRAME_BYTES), slice(-FRAME_BYTES, None)
        self.assertLess(difference(backward_frames[first], forward_frames[last]), 2)
        self.assertGreater(difference(backward_frames[first], forward_frames[first]), 4)
        self.assertLess(difference(backward_frames[last], forward_frames[first]), 2)

    def test_reverse_with_cuts_keeps_all_the_sound(self):
        # Reversing sound that was cut into parts used to leave under a second of it.
        output, _ = self.export("cut-backward.mp4", VideoEdit(aspect=None, keep=[(0.0, 1.0), (2.0, 4.0)],
                                                              reverse=True))
        self.assertAlmostEqual(audio_end(output), 3.0, delta=0.2)
        job = ConversionJob(self.clip, self.tmp / "cut-backward.mp3", keep=[(0.0, 1.0), (2.0, 4.0)], reverse=True)
        self.assertAlmostEqual(audio_end(convert(FFMPEG, job)), 3.0, delta=0.2)

    def test_picture_fade_in_starts_dark(self):
        output, _ = self.export("fade-in.mp4", VideoEdit(aspect=None, fade_in=1.0))
        self.assertLess(brightness(raw_gray(output, 0)), 30)
        self.assertGreater(brightness(raw_gray(output, 2.0)), 80)

    def test_picture_fade_out_ends_dark(self):
        output, _ = self.export("fade-out.mp4", VideoEdit(aspect=None, fade_out=1.0))
        frames = raw_gray(output)
        self.assertLess(brightness(frames[-FRAME_BYTES:]), 30)
        self.assertGreater(brightness(raw_gray(output, 1.0)), 80)

    def test_fade_out_lands_at_the_end_of_a_sped_up_cut_export(self):
        output, _ = self.export("fade-fast.mp4", VideoEdit(aspect=None, keep=[(0.0, 1.0), (2.0, 4.0)], speed=2.0,
                                                           fade_out=0.5))
        frames = raw_gray(output)
        self.assertLess(brightness(frames[-FRAME_BYTES:]), 30)
        self.assertGreater(brightness(raw_gray(output, 0.5)), 80)

    def test_sound_fade_in_starts_quiet(self):
        output, _ = self.export("sound-fade.mp4", VideoEdit(aspect=None, audio_fade_in=1.0))
        samples = mono_samples(output)
        self.assertGreater(peak(samples[8000:16000]), 1000)
        self.assertLess(peak(samples[:800]), peak(samples[8000:16000]) / 4)

    def test_sound_fade_out_ends_quiet(self):
        output, _ = self.export("sound-fade-out.mp4", VideoEdit(aspect=None, audio_fade_out=1.0))
        samples = mono_samples(output)
        self.assertLess(peak(samples[-800:]), peak(samples[8000:16000]) / 4)

    def test_progress_runs_forward_and_ends_at_one(self):
        progress = []
        self.export("progress.mp4", VideoEdit(aspect=None, speed=2.0), on_progress=progress.append)
        self.assertEqual(progress[-1], 1.0)
        self.assertTrue(all(0.0 <= p <= 1.0 for p in progress))
        self.assertEqual(progress, sorted(progress))

    def test_preview_frame_shows_the_fade(self):
        info = probe_media(FFMPEG, self.clip)
        edit = VideoEdit(aspect=None, fade_in=1.0)
        early = render_frame(FFMPEG, self.clip, self.tmp / "early.png", 0.05, (160, 90), edit, info)
        late = render_frame(FFMPEG, self.clip, self.tmp / "late.png", 2.0, (160, 90), edit, info)
        self.assertLess(brightness(raw_gray(early)[:FRAME_BYTES]), 30)
        self.assertGreater(brightness(raw_gray(late)[:FRAME_BYTES]), 80)

    def test_preview_frame_of_a_cut_out_moment_still_renders(self):
        info = probe_media(FFMPEG, self.clip)
        edit = VideoEdit(aspect=None, keep=[(0.0, 1.0), (3.0, 4.0)])
        frame = render_frame(FFMPEG, self.clip, self.tmp / "cut.png", 2.0, (160, 90), edit, info)
        self.assertGreater(frame.stat().st_size, 0)

    def test_nothing_left_is_an_error_and_leaves_no_file(self):
        edit = VideoEdit(aspect=None, keep=[(0.0, 1.0)], start=2.0)
        output = self.tmp / "empty.mp4"
        with self.assertRaisesRegex(ConversionError, "Nothing is left"):
            convert_video(FFMPEG, VideoJob(self.clip, output, edit))
        self.assertFalse(output.exists())
        mp3 = self.tmp / "empty.mp3"
        with self.assertRaisesRegex(ConversionError, "Nothing is left"):
            convert(FFMPEG, ConversionJob(self.clip, mp3, start=2.0, keep=[(0.0, 1.0)]))
        self.assertFalse(mp3.exists())

    def test_mp3_with_cuts_and_speed(self):
        job = ConversionJob(self.clip, self.tmp / "fast.mp3", keep=[(0.0, 1.0), (2.0, 4.0)], speed=2.0)
        self.assertAlmostEqual(probe_media(FFMPEG, convert(FFMPEG, job)).duration, 1.5, delta=0.15)

    def test_mp3_fade_and_reverse(self):
        job = ConversionJob(self.clip, self.tmp / "faded.mp3", fade_in=1.0, reverse=True)
        samples = mono_samples(convert(FFMPEG, job))
        # Reversed, then faded in: quiet at the start, full later, and still 4 s long.
        self.assertAlmostEqual(len(samples) / 8000, 4.0, delta=0.2)
        self.assertLess(peak(samples[:800]), peak(samples[8000:16000]) / 4)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class JoinExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.wide = cls.tmp / "wide.mp4"                    # 2 s, 640x360, 15 fps, with sound
        cls.boxy = cls.tmp / "boxy.mp4"                    # 2 s, 320x240, 25 fps, no sound
        cls.short = cls.tmp / "short.mp4"                  # 1.5 s, 160x90, 15 fps, with sound
        make_video(FFMPEG, cls.wide, 640, 360, 2)
        make_video(FFMPEG, cls.boxy, 320, 240, 2, audio=False, rate=25)
        make_video(FFMPEG, cls.short, 160, 90, 1.5)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def join(self, name, sources, edits=None, **kwargs):
        edits = edits or [VideoEdit(aspect=None) for _ in sources]
        job = JoinJob([JoinPart(s, e) for s, e in zip(sources, edits)], self.tmp / name, **kwargs)
        return job.output if join_clips(FFMPEG, job) else None

    def test_clips_of_different_shapes_and_rates_join_into_the_first_clips_frame(self):
        output = self.join("mixed.mp4", [self.wide, self.boxy])
        info = probe_media(FFMPEG, output)
        self.assertEqual((info.width, info.height), (640, 360))
        self.assertAlmostEqual(info.fps, 15.0, delta=0.5)
        self.assertAlmostEqual(info.duration, 4.0, delta=0.2)
        self.assertTrue(info.has_audio)

    def test_the_second_clip_is_fitted_with_black_bars(self):
        output = self.join("bars.mp4", [self.wide, self.boxy])
        # A 4:3 clip in a 16:9 frame leaves black bars left and right; the first clip fills the frame.
        first = raw_gray(output, 0.5)
        second = raw_gray(output, 3.0)
        columns = lambda frame, x: [frame[y * 16 + x] for y in range(9)]
        self.assertLess(brightness(columns(second, 0)), 25)
        self.assertLess(brightness(columns(second, 15)), 25)
        self.assertGreater(brightness(columns(first, 0)) + brightness(columns(first, 15)), 40)

    def test_a_silent_clip_gets_silence_in_step_with_the_pictures(self):
        output = self.join("silence.mp4", [self.wide, self.boxy])
        samples = mono_samples(output)
        self.assertAlmostEqual(len(samples) / 8000, 4.0, delta=0.2)
        self.assertGreater(peak(samples[2000:14000]), 1000)
        self.assertLess(peak(samples[8000 * 2 + 1600:]), 100)

    def test_each_clip_keeps_its_own_edits(self):
        edits = [VideoEdit(aspect=None, start=0.5, end=1.5), VideoEdit(aspect=None, speed=2.0)]
        output = self.join("edited.mp4", [self.wide, self.boxy], edits)
        self.assertAlmostEqual(probe_media(FFMPEG, output).duration, 2.0, delta=0.2)    # 1 s + 2 s at 2x

    def test_three_clips_to_one_mp3(self):
        edits = [VideoEdit(start=0.0, end=1.0), VideoEdit(), VideoEdit(keep=[(0.0, 0.5), (1.0, 2.0)])]
        output = self.join("three.mp3", [self.wide, self.short, self.wide], edits, audio_only=True)
        info = probe_media(FFMPEG, output)
        self.assertFalse(info.has_video)
        self.assertAlmostEqual(info.duration, 1.0 + 1.5 + 1.5, delta=0.2)

    def test_an_mp3_join_pads_a_silent_clip(self):
        output = self.join("padded.mp3", [self.wide, self.boxy], audio_only=True)
        self.assertAlmostEqual(probe_media(FFMPEG, output).duration, 4.0, delta=0.2)

    def test_joining_progress_ends_at_one(self):
        progress = []
        job = JoinJob([JoinPart(self.wide, VideoEdit(aspect=None)), JoinPart(self.short, VideoEdit(aspect=None))],
                      self.tmp / "progress.mp4")
        join_clips(FFMPEG, job, on_progress=progress.append)
        self.assertEqual(progress[-1], 1.0)
        self.assertEqual(progress, sorted(progress))

    def test_a_missing_file_stops_the_join(self):
        job = JoinJob([JoinPart(self.wide, VideoEdit()), JoinPart(self.tmp / "nope.mp4", VideoEdit())],
                      self.tmp / "nope-out.mp4")
        with self.assertRaisesRegex(ConversionError, "not found"):
            join_clips(FFMPEG, job)
        self.assertFalse(job.output.exists())

    def test_mp4_join_of_a_sound_only_file_is_refused(self):
        sound = self.tmp / "sound.mp3"
        convert(FFMPEG, ConversionJob(self.wide, sound))
        job = JoinJob([JoinPart(self.wide, VideoEdit()), JoinPart(sound, VideoEdit())], self.tmp / "refused.mp4")
        with self.assertRaisesRegex(ConversionError, "no video track"):
            join_clips(FFMPEG, job)


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "video_converter", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True)

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_cut_and_speed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            make_video(FFMPEG, source, 320, 180, 4)
            result = self.run_cli(source, "--video", "square", "--cut", "1-2", "--speed", "2", "-o", Path(tmp) / "out")
            self.assertEqual(result.returncode, 0, result.stderr)
            output = Path(tmp) / "out" / "clip (square).mp4"
            info = probe_media(FFMPEG, output)
            self.assertAlmostEqual(info.duration, 1.5, delta=0.15)           # 3 s kept, at 2x
            self.assertEqual((info.width, info.height), (180, 180))

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_cut_mp3_and_fades(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            make_video(FFMPEG, source, 320, 180, 4)
            result = self.run_cli(source, "--cut", "0:01-0:02", "--cut", "3-4", "--fade-out", "0.5", "-o", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertAlmostEqual(probe_media(FFMPEG, Path(tmp) / "clip.mp3").duration, 2.0, delta=0.15)

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_join_makes_one_file_named_after_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "first.mp4", Path(tmp) / "second.mp4"
            make_video(FFMPEG, first, 320, 180, 2)
            make_video(FFMPEG, second, 160, 120, 1.5, audio=False)
            result = self.run_cli(first, second, "--video", "original", "--join", "-o", Path(tmp) / "out")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([p.name for p in (Path(tmp) / "out").iterdir()], ["first (joined).mp4"])
            info = probe_media(FFMPEG, Path(tmp) / "out" / "first (joined).mp4")
            self.assertAlmostEqual(info.duration, 3.5, delta=0.2)
            self.assertEqual((info.width, info.height), (320, 180))

    def test_bad_timing_options_are_refused_with_exit_code_2(self):
        cases = {
            "join of one file": ["clip.mp4", "--join"],
            "speed too high": ["clip.mp4", "--speed", "9"],
            "speed too low": ["clip.mp4", "--speed", "0.1"],
            "speed not a number": ["clip.mp4", "--speed", "fast"],
            "cut without an end": ["clip.mp4", "--cut", "5"],
            "cut backwards": ["clip.mp4", "--cut", "3-1"],
            "fade too long": ["clip.mp4", "--fade-in", "99"],
        }
        for why, args in cases.items():
            with self.subTest(why):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
