"""Sound tools (2.7): volume, even loudness, noise reduction and background music."""

import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from video_converter import core, video
from video_converter.core import (
    LOUDNESS_SHARE,
    LOUDNESS_SHARE_AUDIO,
    ConversionCancelled,
    ConversionError,
    ConversionJob,
    MediaInfo,
    check_music,
    convert,
    find_ffmpeg,
    mp3_command,
    probe_media,
)
from video_converter.jobs import Job, run_job
from video_converter.project import (
    OUTPUT_AUDIO,
    PRESETS,
    ClipSettings,
    apply_preset,
    apply_to,
    describe,
)
from video_converter.sound import (
    AUDIO_DENOISE,
    DEFAULT_MUSIC_VOLUME,
    Loudness,
    Sound,
    clip_filters,
    loudnorm,
    master_filters,
    parse_loudness,
)
from video_converter.video import (
    JoinJob,
    JoinPart,
    VideoEdit,
    VideoJob,
    convert_video,
    join_clips,
    join_command,
    measure_command,
    video_command,
    wants_loudness_pass,
)

FFMPEG = find_ffmpeg()
ROOT = Path(__file__).resolve().parent.parent
SOURCE = MediaInfo(duration=6.0, width=640, height=360, has_audio=True, fps=15.0)
NO_SOUND = MediaInfo(duration=6.0, width=640, height=360, has_audio=False, fps=15.0)
MEASURED = Loudness(integrated=-27.3, true_peak=-12.1, range=0.4, threshold=-37.3, offset=0.1)


def graph_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def command_for(edit, info=SOURCE, **kwargs):
    return video_command("ffmpeg", VideoJob(Path("clip.mp4"), Path("out.mp4"), edit), info, **kwargs)


# ----- making and measuring sound ---------------------------------------------------

def make_media(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True)


def sine(frequency, seconds, amplitude):
    """A sine wave source with a true peak of `amplitude` (1 = full scale; ffmpeg's own sine peaks at 0.125)."""
    return f"sine=frequency={frequency}:duration={seconds},volume={amplitude * 8}"


def make_clip(path, audio="tone", seconds=6, amplitude=0.25):
    """A tiny video. `audio`: "tone" (440 Hz all the way), "gated" (silent for 3 s, then the tone),
    "silent" (an audio track with nothing in it), or None for no audio track at all."""
    sources = {
        "tone": sine(440, seconds, amplitude),
        "gated": f"{sine(440, seconds, amplitude)},volume='if(gte(t,3),1,0)':eval=frame",
        "silent": f"anullsrc=r=44100:cl=stereo,atrim=duration={seconds}",
    }
    cmd = ["-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=160x90:rate=10"]
    if audio:
        cmd += ["-f", "lavfi", "-i", sources[audio], "-c:a", "aac"]
    make_media(*cmd, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", path)


def make_tone(path, seconds, frequency=880, amplitude=0.5):
    make_media("-f", "lavfi", "-i", sine(frequency, seconds, amplitude), "-ac", "2", path)     # stereo, like a song


def analyse(path, audio_filter="anull", start=None, length=None):
    """ffmpeg's report (stderr) of decoding only the sound of `path` through `audio_filter`."""
    window = (["-ss", str(start)] if start else []) + (["-t", str(length)] if length else [])
    result = subprocess.run([FFMPEG, "-hide_banner", "-nostats", *window, "-i", str(path), "-vn", "-af", audio_filter,
                             "-f", "null", "-"], capture_output=True, text=True)
    return result.stderr


def level(path, band=None, start=None, length=None, key="max_volume", before=""):
    """The loudest (or mean) level in dB of a part of the sound, optionally only around `band` Hz
    (or after the filters in `before`, which end in a comma)."""
    audio_filter = (f"bandpass=f={band}:width_type=h:w=100," if band else "") + before
    report = analyse(path, audio_filter + "volumedetect", start, length)
    return float(re.search(rf"{key}: (-?[\d.]+) dB", report).group(1))


HIGH_PASS = "highpass=f=3000," * 3        # everything above 3 kHz, where the voice (440 Hz) leaks nothing


def lufs(path):
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", analyse(path, "ebur128"))[-1])


def sound_seconds(path):
    """How long the sound of `path` really is."""
    h, m, s = re.findall(r"time=(\d+):(\d+):([\d.]+)", analyse(path))[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


# ----- the filters --------------------------------------------------------------------

class VolumeFilterTests(unittest.TestCase):
    def test_nothing_is_added_by_default(self):
        graph = graph_of(command_for(VideoEdit()))
        for word in ("volume", "afftdn", "loudnorm", "sidechaincompress", "amix"):
            self.assertNotIn(word, graph)
        self.assertEqual(clip_filters(Sound()), [])

    def test_the_volume_is_in_decibels(self):
        self.assertIn("[snd0]volume=6dB[a]", graph_of(command_for(VideoEdit(volume=6.0))))
        self.assertIn("volume=-12.5dB", graph_of(command_for(VideoEdit(volume=-12.5))))

    def test_the_volume_is_kept_within_20_decibels(self):
        self.assertEqual(clip_filters(Sound(volume=99)), ["volume=20dB"])
        self.assertEqual(clip_filters(Sound(volume=-99)), ["volume=-20dB"])

    def test_it_comes_after_the_cuts_speed_reverse_and_fades(self):
        edit = VideoEdit(volume=3.0, speed=2.0, reverse=True, audio_fade_out=1.0, keep=[(0.0, 1.0), (2.0, 4.0)])
        graph = graph_of(command_for(edit))
        positions = [graph.index(word) for word in ("atrim", "atempo", "areverse", "afade", "volume=3dB")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("[snd0]volume=3dB[a]", graph)

    def test_a_muted_video_has_no_sound_to_change(self):
        cmd = command_for(VideoEdit(volume=6.0, audio_denoise="strong", normalize=True, mute=True))
        self.assertIn("-an", cmd)
        self.assertNotIn("volume", graph_of(cmd))
        self.assertNotIn("afftdn", graph_of(cmd))

    def test_mp3_commands_get_it_too(self):
        job = ConversionJob(Path("clip.mp4"), Path("clip.mp3"), sound=Sound(volume=4.0, denoise="light"))
        cmd = mp3_command("ffmpeg", job, [(0.0, 6.0)], True, 6.0)
        self.assertIn("afftdn", graph_of(cmd))
        self.assertIn("volume=4dB", graph_of(cmd))
        self.assertEqual(cmd[cmd.index("-map") + 1], "[voice]")

    def test_an_mp3_without_changes_still_maps_the_stream_directly(self):
        job = ConversionJob(Path("clip.mp4"), Path("clip.mp3"))
        cmd = mp3_command("ffmpeg", job, [(0.0, 6.0)], True, 6.0)
        self.assertNotIn("-filter_complex", cmd)
        self.assertEqual(cmd[cmd.index("-map") + 1], "0:a:0")


class NoiseReductionFilterTests(unittest.TestCase):
    def test_off_light_and_strong(self):
        self.assertEqual(set(AUDIO_DENOISE), {"off", "light", "strong"})
        self.assertEqual(clip_filters(Sound(denoise="off")), [])
        light, strong = clip_filters(Sound(denoise="light")), clip_filters(Sound(denoise="strong"))
        self.assertTrue(light[0].startswith("afftdn=") and strong[0].startswith("afftdn="))
        self.assertNotEqual(light, strong)

    def test_light_takes_out_less_noise_than_strong(self):
        def reduction(key):
            return float(re.search(r"nr=([\d.]+)", AUDIO_DENOISE[key]).group(1))
        self.assertLess(reduction("light"), reduction("strong"))
        self.assertLessEqual(reduction("light"), 12)       # a gentle clean-up that keeps voices natural

    def test_the_noise_follows_the_sound(self):
        for key in ("light", "strong"):
            self.assertIn("tn=1", AUDIO_DENOISE[key])

    def test_noise_reduction_comes_before_the_volume(self):
        self.assertEqual([f.split("=")[0] for f in clip_filters(Sound(denoise="light", volume=3.0))],
                         ["afftdn", "volume"])

    def test_an_unknown_name_does_nothing(self):
        self.assertEqual(clip_filters(Sound(denoise="medium")), [])


class LoudnessFilterTests(unittest.TestCase):
    def test_the_first_pass_only_measures(self):
        text = loudnorm()
        self.assertIn("I=-14", text)
        self.assertIn("TP=-1", text)
        self.assertIn("print_format=json", text)
        self.assertNotIn("measured_I", text)

    def test_the_second_pass_applies_the_measurement_with_one_steady_gain(self):
        text = loudnorm(MEASURED)
        for part in ("I=-14", "TP=-1", "measured_I=-27.30", "measured_TP=-12.10", "measured_LRA=0.40",
                     "measured_thresh=-37.30", "offset=0.10", "linear=true", "aresample=48000"):
            self.assertIn(part, text)
        self.assertNotIn("print_format", text)

    def test_parsing_what_loudnorm_prints(self):
        report = ('[Parsed_loudnorm_0 @ 000001] \n{\n\t"input_i" : "-27.31",\n\t"input_tp" : "-12.10",\n'
                  '\t"input_lra" : "0.40",\n\t"input_thresh" : "-37.31",\n\t"output_i" : "-14.5",\n'
                  '\t"output_tp" : "-1.1",\n\t"output_lra" : "0.3",\n\t"output_thresh" : "-24.2",\n'
                  '\t"normalization_type" : "dynamic",\n\t"target_offset" : "0.12"\n}\n')
        measured = parse_loudness("some log line\n" + report + "video:0KiB audio:5KiB")
        self.assertEqual((measured.integrated, measured.true_peak, measured.range, measured.threshold,
                          measured.offset), (-27.31, -12.10, 0.40, -37.31, 0.12))
        self.assertTrue(measured.usable)

    def test_a_silent_sound_is_not_usable(self):
        report = json.dumps({"input_i": "-inf", "input_tp": "-inf", "input_lra": "0.00", "input_thresh": "-70.00",
                             "target_offset": "inf"})
        measured = parse_loudness(report)
        self.assertIsNotNone(measured)
        self.assertFalse(measured.usable)

    def test_unreadable_reports_give_nothing(self):
        for report in ("", "no json here", "{not json}", '{"input_i": "x"}', '{"other": 1}'):
            with self.subTest(report=report):
                self.assertIsNone(parse_loudness(report))

    def test_the_export_applies_the_measured_loudness_to_the_whole_mix(self):
        cmd = command_for(VideoEdit(normalize=True), loudness=MEASURED)
        graph = graph_of(cmd)
        self.assertIn("[voice]loudnorm=I=-14:TP=-1", graph)
        self.assertIn("measured_I=-27.30", graph)
        self.assertTrue(graph.endswith("aresample=48000[a]"))
        self.assertEqual(cmd[cmd.index("[a]") - 1], "-map")

    def test_without_a_measurement_nothing_is_normalized(self):
        # The command that is built first, to check the settings, and the one for a silent sound.
        self.assertNotIn("loudnorm", graph_of(command_for(VideoEdit(normalize=True))))

    def test_the_measuring_command_has_the_same_sound_and_no_picture(self):
        edit = VideoEdit(normalize=True, volume=3.0, speed=2.0, keep=[(0.0, 1.0), (2.0, 4.0)])
        job = VideoJob(Path("clip.mp4"), Path("out.mp4"), edit)
        cmd = measure_command("ffmpeg", job, SOURCE)
        graph = graph_of(cmd)
        self.assertEqual(cmd[cmd.index("-loglevel") + 1], "info")      # loudnorm prints its result at this level
        self.assertIn("print_format=json", graph)
        self.assertIn("atempo=2", graph)
        self.assertIn("volume=3dB", graph)
        self.assertNotIn("[0:v", graph)
        self.assertNotIn("libx264", cmd)
        self.assertEqual(cmd[cmd.index("-f") + 1], "null")
        self.assertEqual(cmd[-1], "-")
        # The export applies loudnorm to exactly this sound, so what was measured is what gets normalized.
        export = graph_of(command_for(edit, loudness=MEASURED))
        sound_of = lambda text: text[text.index("[0:a:0]"):text.index("loudnorm")]       # noqa: E731
        self.assertEqual(sound_of(graph), sound_of(export))

    def test_the_pass_is_only_run_when_there_is_something_to_even_out(self):
        self.assertTrue(wants_loudness_pass(VideoEdit(normalize=True), SOURCE))
        self.assertFalse(wants_loudness_pass(VideoEdit(), SOURCE))
        self.assertFalse(wants_loudness_pass(VideoEdit(normalize=True), NO_SOUND))             # nothing to measure
        self.assertFalse(wants_loudness_pass(VideoEdit(normalize=True, mute=True), SOURCE))   # Mute wins
        self.assertTrue(wants_loudness_pass(VideoEdit(normalize=True, music="song.mp3"), NO_SOUND))


class MusicFilterTests(unittest.TestCase):
    MUSIC = str(Path("song.mp3").resolve())

    def graph(self, info=SOURCE, **edit):
        return graph_of(command_for(VideoEdit(music="song.mp3", **edit), info))

    def test_the_music_is_a_second_input_that_repeats(self):
        cmd = command_for(VideoEdit(music="song.mp3"))
        at = cmd.index("-stream_loop")
        self.assertEqual(cmd[at:at + 4], ["-stream_loop", "-1", "-i", self.MUSIC])
        self.assertEqual(cmd.count("-i"), 2)
        self.assertLess(cmd.index("-i"), at)           # the video is input 0

    def test_it_is_18_decibels_down_by_default(self):
        self.assertEqual(DEFAULT_MUSIC_VOLUME, -18.0)
        self.assertIn("[1:a:0]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,volume=-18dB",
                      self.graph())
        self.assertIn("volume=-9.5dB", self.graph(music_volume=-9.5))

    def test_the_music_volume_is_kept_in_range(self):
        self.assertIn("volume=-40dB", self.graph(music_volume=-99.0))
        self.assertIn("volume=0dB", self.graph(music_volume=12.0))

    def test_it_is_cut_to_the_length_of_the_result_and_fades_out_over_2_seconds(self):
        graph = self.graph()
        self.assertIn("atrim=duration=6.000", graph)
        self.assertIn("afade=t=out:st=4:d=2", graph)

    def test_a_short_result_fades_out_over_all_of_it(self):
        graph = self.graph(speed=4.0)           # 6 s at 4x is 1.5 s
        self.assertIn("atrim=duration=1.500", graph)
        self.assertIn("afade=t=out:st=0:d=1.5", graph)

    def test_cuts_and_speed_decide_the_length(self):
        graph = self.graph(keep=[(0.0, 1.0), (2.0, 4.0)], speed=2.0)      # 3 s kept, at 2x
        self.assertIn("atrim=duration=1.500", graph)

    def test_it_is_laid_over_the_final_sound_not_each_cut(self):
        graph = self.graph(keep=[(0.0, 1.0), (2.0, 4.0)])
        self.assertEqual(graph.count("[1:a:0]"), 1)
        self.assertEqual(graph.count("amix"), 1)
        self.assertLess(graph.index("concat"), graph.index("amix"))      # the cuts are joined first

    def test_it_is_mixed_without_the_mixer_lowering_the_levels(self):
        self.assertIn("amix=inputs=2:duration=first:dropout_transition=0:normalize=0", self.graph())

    def test_ducking_is_keyed_by_the_videos_own_sound(self):
        plain, ducked = self.graph(), self.graph(music_duck=True)
        self.assertNotIn("sidechaincompress", plain)
        self.assertIn("asplit=2", ducked)
        key = re.search(r"\[song\]\[key\]sidechaincompress=[^\[]*\[ducked\]", ducked)
        self.assertIsNotNone(key)
        self.assertIn("[voice]", ducked.split("asplit")[0].split(";")[-1])    # the split sound is the video's

    def test_a_video_without_sound_gets_the_music_as_its_soundtrack(self):
        cmd = command_for(VideoEdit(music="song.mp3", music_duck=True, volume=3.0), NO_SOUND)
        graph = graph_of(cmd)
        self.assertNotIn("[0:a", graph)
        self.assertNotIn("sidechaincompress", graph)      # nothing to duck for
        self.assertNotIn("volume=-18dB", graph)           # no sound of its own to sit next to
        self.assertIn("atrim=duration=6.000", graph)
        self.assertNotIn("-an", cmd)
        self.assertEqual(cmd[cmd.index("[a]") - 1], "-map")

    def test_mute_removes_the_music_too(self):
        cmd = command_for(VideoEdit(music="song.mp3", mute=True))
        self.assertIn("-an", cmd)
        self.assertNotIn("-stream_loop", cmd)
        self.assertNotIn("amix", graph_of(cmd))

    def test_without_a_known_length_the_music_cant_be_cut_to_it(self):
        with self.assertRaisesRegex(ConversionError, "length"):
            command_for(VideoEdit(music="song.mp3"), MediaInfo(duration=None, width=640, height=360, has_audio=True))

    def test_normalizing_comes_after_the_mix(self):
        graph = graph_of(command_for(VideoEdit(music="song.mp3", normalize=True), loudness=MEASURED))
        self.assertLess(graph.index("amix"), graph.index("loudnorm"))
        self.assertTrue(graph.endswith("[a]"))

    def test_master_filters_alone(self):
        sound = Sound(music="song.mp3", duck=True, normalize=True)
        steps = master_filters(sound, "[voice]", 1, 10.0, "[out]", MEASURED)
        self.assertEqual(steps[-1].split("]", 1)[0], "[mixed")
        self.assertTrue(steps[-1].endswith("[out]"))
        self.assertEqual(master_filters(Sound(), "[voice]", None, 10.0, "[out]"), ["[voice]anull[out]"])


class JoinFilterTests(unittest.TestCase):
    def parts(self, first, second=None):
        info = MediaInfo(duration=4.0, width=320, height=180, has_audio=True, fps=15.0)
        return [JoinPart(Path("a.mp4"), first, info), JoinPart(Path("b.mp4"), second or VideoEdit(aspect=None), info)]

    def command(self, first, second=None, **kwargs):
        return join_command("ffmpeg", JoinJob(self.parts(first, second), Path("out.mp4")), **kwargs)

    def test_each_clip_keeps_its_own_volume_and_noise_reduction(self):
        cmd, _ = self.command(VideoEdit(aspect=None, volume=6.0), VideoEdit(aspect=None, audio_denoise="strong"))
        graph = graph_of(cmd)
        self.assertEqual(graph.count("volume=6dB"), 1)
        self.assertEqual(graph.count("afftdn"), 1)
        self.assertLess(graph.index("volume=6dB"), graph.index("afftdn"))     # the first clip's comes first

    def test_the_music_is_laid_over_the_joined_sound(self):
        cmd, total = self.command(VideoEdit(aspect=None, music="song.mp3"))
        graph = graph_of(cmd)
        self.assertEqual(total, 8.0)
        self.assertEqual(cmd[cmd.index("-stream_loop") + 3], str(Path("song.mp3").resolve()))
        self.assertIn("[2:a:0]", graph)                      # after the two clips
        self.assertEqual(graph.count("amix"), 1)
        self.assertLess(graph.index("concat"), graph.index("amix"))
        self.assertIn("atrim=duration=8.000", graph)
        self.assertIn("afade=t=out:st=6:d=2", graph)

    def test_the_music_and_the_loudness_come_from_the_first_clip(self):
        cmd, _ = self.command(VideoEdit(aspect=None), VideoEdit(aspect=None, music="song.mp3", normalize=True),
                              loudness=MEASURED)
        self.assertNotIn("amix", graph_of(cmd))
        self.assertNotIn("loudnorm", graph_of(cmd))
        self.assertNotIn("-stream_loop", cmd)

    def test_the_first_clip_without_sound_has_no_music(self):
        cmd, _ = self.command(VideoEdit(aspect=None, music="song.mp3", mute=True))
        self.assertNotIn("-stream_loop", cmd)

    def test_the_loudness_pass_is_the_joined_sound_without_the_picture(self):
        cmd, total = self.command(VideoEdit(aspect=None, normalize=True, music="song.mp3"), measure=True)
        graph = graph_of(cmd)
        self.assertEqual(total, 8.0)
        self.assertNotIn("[0:v", graph)
        self.assertIn("print_format=json", graph)
        self.assertIn("concat=n=2:v=0:a=1", graph)
        self.assertEqual(cmd[cmd.index("-f") + 1], "null")

    def test_the_loudness_is_applied_after_the_clips_are_joined(self):
        cmd, _ = self.command(VideoEdit(aspect=None, normalize=True), loudness=MEASURED)
        graph = graph_of(cmd)
        self.assertLess(graph.index("concat"), graph.index("loudnorm"))
        self.assertEqual(graph.count("loudnorm"), 1)

    def test_an_mp3_join_gets_the_same(self):
        parts = self.parts(VideoEdit(volume=3.0, normalize=True, music="song.mp3"))
        cmd, _ = join_command("ffmpeg", JoinJob(parts, Path("out.mp3"), audio_only=True), loudness=MEASURED)
        graph = graph_of(cmd)
        self.assertIn("volume=3dB", graph)
        self.assertIn("amix", graph)
        self.assertIn("loudnorm", graph)
        self.assertIn("libmp3lame", cmd)
        self.assertNotIn("[v]", graph)

    def test_clips_without_sound_can_still_have_music(self):
        info = MediaInfo(duration=4.0, width=320, height=180, has_audio=False, fps=15.0)
        parts = [JoinPart(Path("a.mp4"), VideoEdit(aspect=None, music="song.mp3"), info),
                 JoinPart(Path("b.mp4"), VideoEdit(aspect=None), info)]
        cmd, _ = join_command("ffmpeg", JoinJob(parts, Path("out.mp4")))
        graph = graph_of(cmd)
        self.assertNotIn("[0:a", graph)
        self.assertNotIn("-an", cmd)
        self.assertIn("atrim=duration=8.000", graph)


# ----- the settings ---------------------------------------------------------------------

class SoundSettingsTests(unittest.TestCase):
    def load(self, **edit):
        return ClipSettings.from_dict({"edit": edit}).edit

    def test_defaults(self):
        edit = VideoEdit()
        self.assertEqual((edit.volume, edit.normalize, edit.audio_denoise, edit.music, edit.music_volume,
                          edit.music_duck), (0.0, False, "off", "", -18.0, False))

    def test_the_settings_round_trip_through_json(self):
        settings = ClipSettings(edit=VideoEdit(volume=-20.0, normalize=True, audio_denoise="light",
                                               music="/music/a song.mp3", music_volume=-40.0, music_duck=True))
        self.assertEqual(ClipSettings.from_dict(json.loads(json.dumps(settings.to_dict()))), settings)

    def test_edge_values_and_whole_numbers_are_accepted(self):
        edit = self.load(volume=20, music_volume=0)
        self.assertEqual((edit.volume, edit.music_volume), (20.0, 0.0))
        self.assertIsInstance(edit.volume, float)
        edit = self.load(volume=-20, music_volume=-40)
        self.assertEqual((edit.volume, edit.music_volume), (-20.0, -40.0))

    def test_junk_keeps_the_defaults(self):
        defaults = VideoEdit()
        bad = {
            "volume": [20.5, -21, "3", True, None, [1], float("nan"), float("inf")],
            "music_volume": [0.5, -40.5, "-10", False, None, float("nan"), {}],
            "audio_denoise": ["medium", "", None, 1, ["light"], "LIGHT", True],
            "normalize": [1, "yes", None, 0, "true"],
            "music_duck": [1, "yes", None, [True]],
            "music": [5, None, ["a.mp3"], True, "x" * 5000],
        }
        for name, values in bad.items():
            for value in values:
                with self.subTest(field=name, value=value):
                    self.assertEqual(getattr(self.load(**{name: value}), name), getattr(defaults, name))

    def test_other_settings_survive_a_bad_sound_value(self):
        edit = self.load(volume="loud", speed=2.0, music="a.mp3")
        self.assertEqual((edit.volume, edit.speed, edit.music), (0.0, 2.0, "a.mp3"))

    def test_settings_from_before_the_sound_tools_still_load(self):
        edit = ClipSettings.from_dict({"edit": {"speed": 1.5, "mute": True}}).edit
        self.assertEqual((edit.speed, edit.volume, edit.music), (1.5, 0.0, ""))

    def test_apply_to_all_copies_the_sound_settings_and_presets_leave_them_alone(self):
        source = ClipSettings(edit=VideoEdit(volume=3.0, normalize=True, music="a.mp3", music_duck=True))
        result = apply_to(ClipSettings(), source)
        self.assertEqual((result.edit.volume, result.edit.normalize, result.edit.music, result.edit.music_duck),
                         (3.0, True, "a.mp3", True))
        for name, values in PRESETS.items():
            with self.subTest(preset=name):
                edit = apply_preset(source, values).edit
                self.assertEqual((edit.volume, edit.normalize, edit.music), (3.0, True, "a.mp3"))

    def test_the_summary_mentions_what_is_done_to_the_sound(self):
        text = describe(ClipSettings(edit=VideoEdit(volume=3.0, normalize=True, audio_denoise="light", music="a.mp3")))
        for word in ("+3 dB", "even loudness", "less noise", "music"):
            self.assertIn(word, text)
        plain = describe(ClipSettings())
        for word in ("dB", "loudness", "noise", "music"):
            self.assertNotIn(word, plain)

    def test_the_summary_says_nothing_when_the_sound_is_removed(self):
        text = describe(ClipSettings(edit=VideoEdit(volume=3.0, music="a.mp3", mute=True)))
        self.assertIn("no sound", text)
        self.assertNotIn("music", text)
        self.assertIn("music", describe(ClipSettings(output=OUTPUT_AUDIO, edit=VideoEdit(music="a.mp3", mute=True))))


# ----- real exports ---------------------------------------------------------------------

@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class SoundExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.tone = cls.tmp / "tone.mp4"                  # 6 s, a quiet 440 Hz tone all the way
        cls.quiet = cls.tmp / "quiet.mp4"                # the same, much quieter
        cls.loud = cls.tmp / "loud.mp4"                  # and much louder
        cls.gated = cls.tmp / "gated.mp4"                # silence for 3 s, then the tone
        cls.silent = cls.tmp / "silent.mp4"              # a sound track with nothing in it
        cls.mute_video = cls.tmp / "mute-video.mp4"      # no sound track at all
        cls.short_music = cls.tmp / "short.wav"          # 2 s of 880 Hz
        cls.long_music = cls.tmp / "long.wav"            # 30 s of 880 Hz
        make_clip(cls.tone, "tone", amplitude=0.1)
        make_clip(cls.quiet, "tone", amplitude=0.02)
        make_clip(cls.loud, "tone", amplitude=0.9)
        make_clip(cls.gated, "gated", amplitude=0.4)
        make_clip(cls.silent, "silent")
        make_clip(cls.mute_video, None)
        make_tone(cls.short_music, 2)
        make_tone(cls.long_music, 30)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def export(self, name, source, **edit):
        edit.setdefault("aspect", None)
        output = convert_video(FFMPEG, VideoJob(source, self.tmp / name, VideoEdit(**edit)))
        return output

    def test_plus_6_decibels_doubles_the_amplitude_and_minus_6_halves_it(self):
        base = level(self.export("v0.mp4", self.tone))
        for volume, ratio in ((6.0, 2.0), (-6.0, 0.5), (12.0, 4.0)):
            with self.subTest(volume=volume):
                louder = level(self.export(f"v{volume}.mp4", self.tone, volume=volume))
                self.assertAlmostEqual(10 ** ((louder - base) / 20), ratio, delta=ratio * 0.07)

    def test_the_volume_works_in_an_mp3_too(self):
        base = level(convert(FFMPEG, ConversionJob(self.tone, self.tmp / "plain.mp3")))
        louder = level(convert(FFMPEG, ConversionJob(self.tone, self.tmp / "louder.mp3", sound=Sound(volume=6.0))))
        self.assertAlmostEqual(louder - base, 6.0, delta=0.7)

    def test_the_volume_is_applied_after_the_cuts_and_speed_without_changing_the_length(self):
        output = self.export("cut-volume.mp4", self.tone, volume=6.0, keep=[(0.0, 1.0), (2.0, 4.0)], speed=2.0)
        self.assertAlmostEqual(probe_media(FFMPEG, output).duration, 1.5, delta=0.15)

    def test_the_loudness_lands_on_minus_14_whether_the_source_is_quiet_or_loud(self):
        for source in (self.quiet, self.tone, self.loud):
            with self.subTest(source=source.name):
                before = lufs(source)
                after = lufs(self.export(f"even-{source.name}", source, normalize=True))
                self.assertGreater(abs(before + 14), 3, "the test source must be far from the target")
                self.assertAlmostEqual(after, -14.0, delta=0.8)

    def test_the_loudness_is_evened_out_in_an_mp3_too(self):
        output = convert(FFMPEG, ConversionJob(self.quiet, self.tmp / "even.mp3", sound=Sound(normalize=True)))
        self.assertAlmostEqual(lufs(output), -14.0, delta=1.0)

    def test_the_peaks_stay_below_the_limit(self):
        report = analyse(self.export("even-peak.mp4", self.loud, normalize=True), "ebur128=peak=true")
        peak = float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", report)[-1])
        self.assertLessEqual(peak, -0.5)

    def test_normalizing_a_silent_video_still_exports(self):
        output = self.export("even-silent.mp4", self.silent, normalize=True)
        self.assertTrue(probe_media(FFMPEG, output).has_audio)
        self.assertLess(level(output), -60)

    def test_normalizing_a_video_without_sound_still_exports(self):
        output = self.export("even-none.mp4", self.mute_video, normalize=True)
        self.assertFalse(probe_media(FFMPEG, output).has_audio)

    def test_music_is_heard_where_the_video_is_silent(self):
        output = self.export("music-silent.mp4", self.gated, music=str(self.long_music))
        self.assertGreater(level(output, band=880, start=0.5, length=2.0), -40)
        # ... and the video's own sound is still there afterwards, next to it.
        self.assertGreater(level(output, band=440, start=3.5, length=1.0), -20)

    def test_the_music_is_18_decibels_below_the_video_by_default(self):
        steady = dict(band=880, start=1.0, length=1.5, key="mean_volume")
        reference = level(self.long_music, **steady)
        output = self.export("music-level.mp4", self.gated, music=str(self.long_music))
        self.assertAlmostEqual(level(output, **steady) - reference, -18.0, delta=1.0)
        louder = self.export("music-level2.mp4", self.gated, music=str(self.long_music), music_volume=-6.0)
        self.assertAlmostEqual(level(louder, **steady) - reference, -6.0, delta=1.0)

    def test_ducking_lowers_the_music_while_the_video_has_sound(self):
        plain = self.export("no-duck.mp4", self.gated, music=str(self.long_music), music_volume=-12.0)
        ducked = self.export("duck.mp4", self.gated, music=str(self.long_music), music_volume=-12.0, music_duck=True)
        # Where people talk (3 s on) the music sits much lower ...
        talking = dict(band=880, start=3.4, length=0.5)
        self.assertLess(level(ducked, **talking), level(plain, **talking) - 6)
        # ... and where they don't it is just as loud as without ducking.
        quiet = dict(band=880, start=0.5, length=2.0)
        self.assertAlmostEqual(level(ducked, **quiet), level(plain, **quiet), delta=1.5)

    def test_short_music_starts_again_to_fill_the_video(self):
        output = self.export("music-loop.mp4", self.silent, music=str(self.short_music))
        self.assertGreater(level(output, band=880, start=2.3, length=1.4), -40)      # 2 s music, heard after 2 s
        self.assertGreater(level(output, band=880, start=0.5, length=1.0), -40)

    def test_the_music_fades_out_over_the_last_2_seconds(self):
        output = self.export("music-fade.mp4", self.silent, music=str(self.long_music))
        steady = level(output, band=880, start=1.0, length=1.0)
        self.assertLess(level(output, band=880, start=5.7, length=0.3), steady - 15)
        self.assertGreater(level(output, band=880, start=3.0, length=0.8), steady - 1)    # untouched before the fade

    def test_the_music_is_cut_to_the_length_of_the_result(self):
        for name, music in (("short", self.short_music), ("long", self.long_music)):
            with self.subTest(music=name):
                output = self.export(f"music-length-{name}.mp4", self.tone, music=str(music))
                self.assertAlmostEqual(sound_seconds(output), 6.0, delta=0.1)
                self.assertAlmostEqual(probe_media(FFMPEG, output).duration, 6.0, delta=0.15)

    def test_the_music_follows_the_cuts_and_the_speed(self):
        output = self.export("music-cut.mp4", self.tone, music=str(self.long_music), speed=2.0,
                             keep=[(0.0, 1.0), (2.0, 4.0)])
        self.assertAlmostEqual(sound_seconds(output), 1.5, delta=0.1)
        self.assertAlmostEqual(probe_media(FFMPEG, output).duration, 1.5, delta=0.15)
        trimmed = self.export("music-trim.mp4", self.tone, music=str(self.long_music), start=1.0, end=4.0)
        self.assertAlmostEqual(sound_seconds(trimmed), 3.0, delta=0.1)

    def test_a_video_without_sound_gets_the_music_as_its_soundtrack(self):
        output = self.export("music-only.mp4", self.mute_video, music=str(self.long_music), music_duck=True)
        info = probe_media(FFMPEG, output)
        self.assertTrue(info.has_audio)
        self.assertAlmostEqual(sound_seconds(output), 6.0, delta=0.1)
        steady = dict(band=880, start=1.0, length=1.5, key="mean_volume")
        self.assertAlmostEqual(level(output, **steady) - level(self.long_music, **steady), 0.0, delta=1.0)

    def test_music_together_with_normalizing_lands_on_minus_14(self):
        output = self.export("music-even.mp4", self.mute_video, music=str(self.long_music), normalize=True)
        self.assertAlmostEqual(lufs(output), -14.0, delta=1.0)
        mixed = self.export("music-even2.mp4", self.tone, music=str(self.long_music), normalize=True,
                            music_duck=True, volume=-6.0)
        self.assertAlmostEqual(lufs(mixed), -14.0, delta=1.0)

    def test_mute_wins_over_everything(self):
        missing = self.tmp / "gone.mp3"       # not even looked at
        output = self.export("muted.mp4", self.tone, mute=True, volume=6.0, normalize=True, audio_denoise="strong",
                             music=str(missing))
        self.assertFalse(probe_media(FFMPEG, output).has_audio)

    def test_noise_reduction_takes_out_noise_and_keeps_the_voice(self):
        noisy_sound = self.tmp / "noisy.wav"
        make_media("-f", "lavfi", "-i", sine(440, 6, 0.3), "-f", "lavfi",
                   "-i", "anoisesrc=color=white:amplitude=0.02:duration=6:seed=1",
                   "-filter_complex", "amix=inputs=2:normalize=0", noisy_sound)
        noisy = self.tmp / "noisy.mp4"
        make_media("-i", self.mute_video, "-i", noisy_sound, "-c:v", "copy", "-c:a", "aac", "-shortest", noisy)

        def measure(output):
            """The level of the voice (around 440 Hz) and of the hiss (everything above 3 kHz)."""
            return (level(output, band=440, start=2.0, key="mean_volume"),
                    level(output, start=2.0, key="mean_volume", before=HIGH_PASS))
        voice_before, hiss_before = measure(noisy)
        results = {key: measure(self.export(f"denoise-{key}.mp4", noisy, audio_denoise=key))
                   for key in ("light", "strong")}
        for key, (voice, hiss) in results.items():
            with self.subTest(setting=key):
                self.assertAlmostEqual(voice, voice_before, delta=1.0)      # the voice isn't touched
                self.assertLess(hiss, hiss_before - 3)
        self.assertLess(results["strong"][1], results["light"][1] - 3)

    def test_an_mp3_with_music_and_volume(self):
        output = convert(FFMPEG, ConversionJob(self.gated, self.tmp / "music.mp3",
                                               sound=Sound(music=str(self.short_music), duck=True)))
        self.assertAlmostEqual(sound_seconds(output), 6.0, delta=0.2)
        self.assertGreater(level(output, band=880, start=0.5, length=1.5), -40)

    def test_an_mp3_of_a_file_without_sound_can_be_all_music(self):
        output = convert(FFMPEG, ConversionJob(self.mute_video, self.tmp / "only-music.mp3",
                                               sound=Sound(music=str(self.long_music), normalize=True)))
        self.assertAlmostEqual(sound_seconds(output), 6.0, delta=0.2)
        self.assertAlmostEqual(lufs(output), -14.0, delta=1.0)

    def test_the_export_queue_hands_the_sound_settings_on_to_an_mp3(self):
        settings = ClipSettings(output=OUTPUT_AUDIO, edit=VideoEdit(volume=6.0))
        base = level(convert(FFMPEG, ConversionJob(self.tone, self.tmp / "queue-plain.mp3")))
        job = Job(self.tone, settings, self.tmp / "queue")
        louder = level(run_job(FFMPEG, job, lambda fraction: None))
        self.assertAlmostEqual(louder - base, 6.0, delta=0.7)

    def test_joined_clips_have_their_own_volume_and_share_the_music_and_loudness(self):
        quiet_edit = VideoEdit(aspect=None, volume=-12.0)
        parts = [JoinPart(self.tone, quiet_edit), JoinPart(self.tone, VideoEdit(aspect=None))]
        output = join_clips(FFMPEG, JoinJob(parts, self.tmp / "joined-volume.mp4"))
        self.assertAlmostEqual(level(output, start=1.0, length=2.0) + 12, level(output, start=7.0, length=2.0), delta=1.0)

        first = VideoEdit(aspect=None, normalize=True, music=str(self.long_music), music_duck=True)
        joined = join_clips(FFMPEG, JoinJob([JoinPart(self.gated, first), JoinPart(self.quiet, VideoEdit(aspect=None))],
                                            self.tmp / "joined-music.mp4"))
        self.assertAlmostEqual(sound_seconds(joined), 12.0, delta=0.15)
        self.assertAlmostEqual(lufs(joined), -14.0, delta=1.0)
        self.assertGreater(level(joined, band=880, start=7.0, length=2.0), -45)      # over the second clip as well

    def test_an_mp3_join_gets_the_music_and_the_loudness(self):
        first = VideoEdit(normalize=True, music=str(self.long_music))
        job = JoinJob([JoinPart(self.tone, first), JoinPart(self.quiet, VideoEdit())], self.tmp / "joined.mp3",
                      audio_only=True)
        output = join_clips(FFMPEG, job)
        self.assertAlmostEqual(sound_seconds(output), 12.0, delta=0.2)
        self.assertAlmostEqual(lufs(output), -14.0, delta=1.0)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class MusicFileErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "clip.mp4"
        make_clip(cls.clip, "tone", seconds=2)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_missing_music_file_is_a_clear_error_and_nothing_is_made(self):
        gone = self.tmp / "gone.mp3"
        output = self.tmp / "never.mp4"
        with self.assertRaisesRegex(ConversionError, r"background music file can't be found.*gone\.mp3"):
            convert_video(FFMPEG, VideoJob(self.clip, output, VideoEdit(aspect=None, music=str(gone))))
        self.assertFalse(output.exists())
        with self.assertRaisesRegex(ConversionError, r"gone\.mp3"):
            convert(FFMPEG, ConversionJob(self.clip, self.tmp / "never.mp3", sound=Sound(music=str(gone))))
        with self.assertRaisesRegex(ConversionError, r"gone\.mp3"):
            join_clips(FFMPEG, JoinJob([JoinPart(self.clip, VideoEdit(aspect=None, music=str(gone))),
                                        JoinPart(self.clip, VideoEdit(aspect=None))], self.tmp / "never-joined.mp4"))
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith("never")], [])

    def test_the_error_comes_through_the_export_queue(self):
        job = Job(self.clip, ClipSettings(edit=VideoEdit(aspect=None, music=str(self.tmp / "gone.mp3"))), self.tmp)
        with self.assertRaisesRegex(ConversionError, "can't be found"):
            run_job(FFMPEG, job, lambda fraction: None)

    def test_a_file_without_sound_is_not_music(self):
        notes = self.tmp / "notes.txt"
        notes.write_text("not music")
        with self.assertRaisesRegex(ConversionError, r"notes\.txt has no sound"):
            check_music(FFMPEG, Sound(music=str(notes)))
        with self.assertRaisesRegex(ConversionError, r"notes\.txt has no sound"):
            convert_video(FFMPEG, VideoJob(self.clip, self.tmp / "never.mp4",
                                           VideoEdit(aspect=None, music=str(notes))))

    def test_nothing_is_checked_without_music_or_with_the_sound_removed(self):
        check_music(FFMPEG, Sound())
        check_music(FFMPEG, Sound(music=str(self.tmp / "gone.mp3")), muted=True)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class TwoPassTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clip = cls.tmp / "clip.mp4"
        make_clip(cls.clip, "tone", seconds=4, amplitude=0.1)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def recorded(self, action, *, spawned=None):
        """Run `action()` while recording every ffmpeg run, in order: (command, working folder, output)."""
        calls, real_core, real_video = [], core.run_ffmpeg, video.run_ffmpeg

        def recorder(real):
            def record(cmd, output, duration, on_progress=None, cancel_event=None, cwd=None, report=None):
                calls.append((cmd, cwd, output))
                return real(cmd, output, duration, on_progress, cancel_event, cwd,
                            **({"report": report} if report is not None else {}))
            return record

        real_popen = subprocess.Popen

        def spy(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            if spawned is not None:
                spawned.append(process)
            return process

        with mock.patch.object(core, "run_ffmpeg", recorder(real_core)), \
                mock.patch.object(video, "run_ffmpeg", recorder(real_video)), \
                mock.patch.object(subprocess, "Popen", spy):
            result = action()
        return result, calls

    def test_the_loudness_is_measured_first_and_then_applied(self):
        job = VideoJob(self.clip, self.tmp / "even.mp4", VideoEdit(aspect=None, normalize=True))
        output, calls = self.recorded(lambda: convert_video(FFMPEG, job))
        self.assertEqual(len(calls), 2)
        measure, export = calls[0][0], calls[1][0]
        self.assertIn("print_format=json", graph_of(measure))
        self.assertNotIn("measured_I", graph_of(measure))
        self.assertIn("measured_I=", graph_of(export))
        self.assertIn("linear=true", graph_of(export))
        self.assertEqual(calls[0][1], calls[1][1])          # one temporary folder for both
        self.assertFalse(calls[0][1].exists())              # ... which is gone
        self.assertTrue(output.is_file())

    def test_the_applied_numbers_are_the_measured_ones(self):
        output, calls = self.recorded(lambda: convert_video(
            FFMPEG, VideoJob(self.clip, self.tmp / "even2.mp4", VideoEdit(aspect=None, normalize=True))))
        report = analyse(self.clip, "loudnorm=I=-14:TP=-1:LRA=11:print_format=json")
        measured = parse_loudness(report)
        applied = re.search(r"measured_I=(-?[\d.]+)", graph_of(calls[1][0])).group(1)
        self.assertAlmostEqual(float(applied), measured.integrated, delta=0.15)

    def test_no_loudness_pass_without_normalizing_or_when_muted(self):
        for name, edit in (("plain", VideoEdit(aspect=None)), ("muted", VideoEdit(aspect=None, normalize=True,
                                                                                 mute=True))):
            with self.subTest(name):
                _, calls = self.recorded(lambda: convert_video(FFMPEG, VideoJob(self.clip, self.tmp / f"{name}.mp4",
                                                                                edit)))
                self.assertEqual(len(calls), 1)

    def test_stabilizing_and_normalizing_run_three_passes_in_one_folder(self):
        if "vidstabdetect" not in subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True,
                                                 text=True).stdout:
            self.skipTest("this ffmpeg can't stabilize")
        job = VideoJob(self.clip, self.tmp / "both.mp4", VideoEdit(aspect=None, normalize=True, stabilize="light"))
        _, calls = self.recorded(lambda: convert_video(FFMPEG, job))
        self.assertEqual(len(calls), 3)
        self.assertIn("vidstabdetect", graph_of(calls[0][0]))
        self.assertIn("print_format=json", graph_of(calls[1][0]))
        self.assertIn("vidstabtransform", graph_of(calls[2][0]))
        self.assertEqual({str(c[1]) for c in calls}, {str(calls[0][1])})

    def fake_runs(self, edit, mp3=False):
        """The progress an export reports when each pass says 50% and then 100%."""
        progress = []

        def fake(cmd, output, duration, on_progress=None, cancel_event=None, cwd=None, report=None):
            on_progress(0.5)
            on_progress(1.0)
            return output

        with mock.patch.object(core, "run_ffmpeg", fake), mock.patch.object(video, "run_ffmpeg", fake):
            if mp3:
                convert(FFMPEG, ConversionJob(self.clip, self.tmp / "unused.mp3", sound=edit.sound), progress.append)
            else:
                convert_video(FFMPEG, VideoJob(self.clip, self.tmp / "unused.mp4", edit), on_progress=progress.append)
        return progress

    def test_both_passes_report_progress_in_their_share_of_the_bar(self):
        self.assertEqual((LOUDNESS_SHARE, LOUDNESS_SHARE_AUDIO), (0.1, 0.4))
        for got, expected in zip(self.fake_runs(VideoEdit(aspect=None, normalize=True)), (0.05, 0.1, 0.55, 1.0)):
            self.assertAlmostEqual(got, expected)
        for got, expected in zip(self.fake_runs(VideoEdit(normalize=True), mp3=True), (0.2, 0.4, 0.7, 1.0)):
            self.assertAlmostEqual(got, expected)
        self.assertEqual(len(self.fake_runs(VideoEdit(aspect=None, normalize=True))), 4)

    def test_the_progress_of_a_real_export_never_goes_back_and_ends_at_one(self):
        progress = []
        convert_video(FFMPEG, VideoJob(self.clip, self.tmp / "steady-progress.mp4",
                                       VideoEdit(aspect=None, normalize=True)), on_progress=progress.append)
        self.assertEqual(progress, sorted(progress))
        self.assertTrue(all(0.0 <= p <= 1.0 for p in progress))
        self.assertEqual(progress[-1], 1.0)
        self.assertTrue(any(0 < p <= LOUDNESS_SHARE for p in progress))
        self.assertTrue(any(LOUDNESS_SHARE < p < 1 for p in progress))

    def cancel_during(self, make_job, run, condition, passes_started, name):
        """Cancel a real export as soon as `condition(progress)`; nothing may be left behind."""
        cancel, spawned = threading.Event(), []

        def progress(fraction):
            if condition(fraction):
                cancel.set()

        def action():
            with self.assertRaises(ConversionCancelled):
                run(make_job(name), progress, cancel)
        _, calls = self.recorded(action, spawned=spawned)
        self.assertEqual(len(calls), passes_started)
        self.assertTrue(spawned and all(p.poll() is not None for p in spawned), "an ffmpeg is still running")
        self.assertFalse(calls[0][1].exists(), "the temporary folder is still there")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith(name)), [])

    def test_cancelling_during_either_pass_of_a_video_export(self):
        def make(name):
            return VideoJob(self.clip, self.tmp / f"{name}.mp4", VideoEdit(aspect=None, normalize=True))

        def run(job, progress, cancel):
            convert_video(FFMPEG, job, on_progress=progress, cancel_event=cancel)
        self.cancel_during(make, run, lambda f: f > 0, 1, "cancel-measure")             # in the measuring pass
        self.cancel_during(make, run, lambda f: f > LOUDNESS_SHARE, 2, "cancel-export")  # in the export

    def test_cancelling_during_either_pass_of_an_mp3(self):
        def make(name):
            return ConversionJob(self.clip, self.tmp / f"{name}.mp3", sound=Sound(normalize=True))

        def run(job, progress, cancel):
            convert(FFMPEG, job, progress, cancel)
        self.cancel_during(make, run, lambda f: f > 0, 1, "cancel-mp3-measure")
        self.cancel_during(make, run, lambda f: f > LOUDNESS_SHARE_AUDIO, 2, "cancel-mp3-export")

    def test_cancelling_during_either_pass_of_a_join(self):
        def make(name):
            parts = [JoinPart(self.clip, VideoEdit(aspect=None, normalize=True)), JoinPart(self.clip, VideoEdit(aspect=None))]
            return JoinJob(parts, self.tmp / f"{name}.mp4")

        def run(job, progress, cancel):
            join_clips(FFMPEG, job, on_progress=progress, cancel_event=cancel)
        self.cancel_during(make, run, lambda f: f > 0, 1, "cancel-join-measure")
        self.cancel_during(make, run, lambda f: f > LOUDNESS_SHARE, 2, "cancel-join-export")


# ----- the command line -----------------------------------------------------------------

class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "video_converter", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True)

    def test_bad_sound_options_are_refused_with_exit_code_2(self):
        cases = {
            "volume too high": ["clip.mp4", "--volume", "21"],
            "volume too low": ["clip.mp4", "--volume", "-21"],
            "volume not a number": ["clip.mp4", "--volume", "loud"],
            "music volume above 0": ["clip.mp4", "--music", "a.mp3", "--music-volume", "3"],
            "music volume too low": ["clip.mp4", "--music", "a.mp3", "--music-volume", "-41"],
            "unknown noise setting": ["clip.mp4", "--denoise-audio", "medium"],
            "duck without music": ["clip.mp4", "--duck"],
            "music volume without music": ["clip.mp4", "--music-volume", "-10"],
        }
        for why, args in cases.items():
            with self.subTest(why):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_the_options_are_in_the_help(self):
        text = self.run_cli("--help").stdout
        for option in ("--volume", "--normalize", "--denoise-audio", "--music", "--music-volume", "--duck"):
            self.assertIn(option, text)

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_volume_and_normalize_for_an_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "clip.mp4"
            make_clip(source, "tone", seconds=4, amplitude=0.05)
            result = self.run_cli(source, "--video", "original", "--volume", "6", "-o", tmp / "louder")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertAlmostEqual(level(tmp / "louder" / "clip (edited).mp4") - level(source), 6.0, delta=0.7)
            result = self.run_cli(source, "--video", "original", "--normalize", "-o", tmp / "even")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertAlmostEqual(lufs(tmp / "even" / "clip (edited).mp4"), -14.0, delta=1.0)

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_music_noise_reduction_and_ducking_for_an_mp4_and_an_mp3(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source, music = tmp / "clip.mp4", tmp / "song.wav"
            make_clip(source, "gated", seconds=4, amplitude=0.4)
            make_tone(music, 1)
            args = ["--music", music, "--music-volume", "-10", "--duck", "--denoise-audio", "light"]
            result = self.run_cli(source, "--video", "original", *args, "-o", tmp / "out")
            self.assertEqual(result.returncode, 0, result.stderr)
            output = tmp / "out" / "clip (edited).mp4"
            self.assertAlmostEqual(probe_media(FFMPEG, output).duration, 4.0, delta=0.2)
            self.assertGreater(level(output, band=880, start=0.2, length=1.0), -40)      # the music, looped
            result = self.run_cli(source, *args, "-o", tmp / "out")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertAlmostEqual(sound_seconds(tmp / "out" / "clip.mp3"), 4.0, delta=0.2)
            self.assertGreater(level(tmp / "out" / "clip.mp3", band=880, start=0.2, length=1.0), -40)

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_a_missing_music_file_fails_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            make_clip(source, "tone", seconds=2)
            for extra in (["--video", "original"], []):
                result = self.run_cli(source, *extra, "--music", Path(tmp) / "nope.mp3", "-o", Path(tmp) / "out")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("nope.mp3", result.stderr)
                self.assertIn("can't be found", result.stderr)
            self.assertFalse((Path(tmp) / "out").exists())

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_joining_with_the_sound_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            first, second, music = tmp / "first.mp4", tmp / "second.mp4", tmp / "song.wav"
            make_clip(first, "tone", seconds=2, amplitude=0.05)
            make_clip(second, "tone", seconds=2, amplitude=0.05)
            make_tone(music, 10)
            result = self.run_cli(first, second, "--video", "original", "--join", "--normalize", "--music", music,
                                  "-o", tmp / "out")
            self.assertEqual(result.returncode, 0, result.stderr)
            output = tmp / "out" / "first (joined).mp4"
            self.assertAlmostEqual(sound_seconds(output), 4.0, delta=0.15)
            self.assertAlmostEqual(lufs(output), -14.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
