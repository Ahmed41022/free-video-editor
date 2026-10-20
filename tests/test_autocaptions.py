"""Automatic captions: cutting words into cues, the .srt file, time mapping through cuts and speed, and the
whole pipeline with a pretend speech model (no network, no model download)."""

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.test_speech_models import FILES, FakeServer, tiny_model
from video_converter import autocaptions as ac
from video_converter import speech_models as sm
from video_converter.captions import Cue, Subtitles, format_srt, parse_cues, read_text, retimed_subtitles, write_srt
from video_converter.core import ConversionCancelled, ConversionError, find_ffmpeg

FFMPEG = find_ffmpeg()


def words(text: str, start: float = 0.0, per_word: float = 0.3, gap: float = 0.03) -> list:
    """Pieces for the words of `text`, spoken one after the other."""
    pieces, t = [], start
    for word in text.split():
        pieces.append(ac.Piece(t, t + per_word, " " + word))
        t += per_word + gap
    return pieces


def lines_of(cue: Cue) -> list:
    return cue.text.split("\n")


def assert_well_formed(test: unittest.TestCase, cues: list) -> None:
    for i, cue in enumerate(cues):
        test.assertLessEqual(len(lines_of(cue)), ac.MAX_LINES, cue.text)
        for line in lines_of(cue):
            test.assertLessEqual(len(line), ac.LINE_LENGTH, cue.text)
            test.assertTrue(line.strip())
        test.assertLessEqual(cue.end - cue.start, ac.MAX_DURATION + 1e-6, cue.text)
        test.assertGreater(cue.end, cue.start)
        if i:
            test.assertLessEqual(cues[i - 1].end, cue.start + 1e-6, f"{cues[i - 1]} overlaps {cue}")


ENGLISH = ("Hello everyone and welcome back to the channel. Today we are going to talk about something really "
           "interesting, which is how to edit videos quickly and easily without any complicated software. "
           "Let's get started.")
ARABIC = ("مرحبا بكم في قناتنا اليوم سوف نتحدث عن موضوع مهم جدا وهو كيف نقوم بتحرير مقاطع الفيديو بسرعة "
          "وسهولة، ثم نبدأ بالشرح خطوة بخطوة. شكرا لكم على المتابعة.")


class WrapTests(unittest.TestCase):
    def test_short_text_stays_on_one_line(self):
        self.assertEqual(ac.wrap_lines("Hello there"), ["Hello there"])
        self.assertEqual(ac.wrap_lines("x" * 42), ["x" * 42])

    def test_longer_text_is_split_into_two_even_lines_of_at_most_42(self):
        lines = ac.wrap_lines("The quick brown fox jumps over the lazy dog and keeps on running far away")
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 42 for line in lines))
        self.assertLess(abs(len(lines[0]) - len(lines[1])), 14)
        self.assertEqual(" ".join(lines), "The quick brown fox jumps over the lazy dog and keeps on running far away")

    def test_a_break_after_a_comma_is_preferred_when_it_costs_little(self):
        lines = ac.wrap_lines("When we got home late at night, everybody was already sound asleep")
        self.assertEqual(lines[0], "When we got home late at night,")

    def test_text_that_cannot_fit_two_lines_gives_none(self):
        self.assertIsNone(ac.wrap_lines("word " * 30))
        self.assertIsNone(ac.wrap_lines("x" * 50 + " y"))

    def test_arabic_wraps_at_spaces_without_breaking_words(self):
        text = "مرحبا بكم في قناتنا اليوم سوف نتحدث عن موضوع مهم جدا"
        lines = ac.wrap_lines(text)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 42 for line in lines))
        self.assertEqual(" ".join(lines), text)

    def test_text_without_spaces_is_broken_between_letters(self):
        lines = ac.wrap_lines("你好" * 30)
        self.assertEqual(len(lines), 2)
        self.assertEqual("".join(lines), "你好" * 30)
        self.assertTrue(all(len(line) <= 42 for line in lines))

    def test_extra_spaces_and_line_breaks_are_tidied(self):
        self.assertEqual(ac.wrap_lines("  two   words \n here "), ["two words here"])


class BuildCuesTests(unittest.TestCase):
    def test_a_talk_becomes_short_readable_cues_that_end_at_sentences(self):
        cues = ac.build_cues(words(ENGLISH))
        assert_well_formed(self, cues)
        self.assertGreaterEqual(len(cues), 3)
        self.assertEqual(" ".join(c.text.replace("\n", " ") for c in cues), ENGLISH)
        self.assertTrue(cues[0].text.rstrip().endswith("channel."))
        self.assertEqual(cues[-1].text, "Let's get started.")

    def test_arabic_is_cut_the_same_way_and_the_text_is_kept(self):
        cues = ac.build_cues(words(ARABIC))
        assert_well_formed(self, cues)
        self.assertEqual(" ".join(c.text.replace("\n", " ") for c in cues), ARABIC)
        self.assertGreaterEqual(len(cues), 3)

    def test_a_pause_starts_a_new_cue_even_in_the_middle_of_a_sentence(self):
        first, second = words("I was thinking", 0.0), words("about the weather", 5.0)
        cues = ac.build_cues(first + second)
        self.assertEqual([c.text for c in cues], ["I was thinking", "about the weather"])

    def test_a_short_pause_does_not(self):
        cues = ac.build_cues(words("I was thinking", 0.0) + words("about the weather", 1.5))
        self.assertEqual(len(cues), 1)

    def test_speech_with_no_punctuation_still_obeys_the_limits(self):
        cues = ac.build_cues(words("and then we went on and on and on talking " * 12, per_word=0.4, gap=0.0))
        assert_well_formed(self, cues)
        self.assertGreater(len(cues), 5)

    def test_cues_are_never_longer_than_six_seconds(self):
        slow = [ac.Piece(i * 2.0, i * 2.0 + 1.9, f" word{i}") for i in range(12)]
        cues = ac.build_cues(slow)
        assert_well_formed(self, cues)
        self.assertTrue(all(c.end - c.start <= ac.MAX_DURATION for c in cues))

    def test_a_cue_is_kept_on_screen_for_a_second_when_nothing_follows_it(self):
        cues = ac.build_cues([ac.Piece(3.0, 3.2, " Hi.")])
        self.assertEqual((cues[0].start, cues[0].end), (3.0, 4.0))

    def test_a_short_cue_is_not_stretched_into_the_next_one(self):
        pieces = [ac.Piece(0.0, 0.5, " Okay that works for me."), ac.Piece(0.55, 1.0, " Great"),
                  ac.Piece(1.0, 1.4, " see you.")]
        cues = ac.build_cues(pieces)
        assert_well_formed(self, cues)
        self.assertEqual(len(cues), 2)
        self.assertLessEqual(cues[0].end, cues[1].start)
        self.assertLess(cues[0].end, 1.0)

    def test_overlapping_word_times_from_the_model_are_straightened_out(self):
        pieces = [ac.Piece(0.0, 2.0, " One."), ac.Piece(1.0, 3.0, " Two.")]
        cues = ac.build_cues(pieces)
        assert_well_formed(self, cues)
        self.assertEqual(len(cues), 2)

    def test_times_are_moved_by_the_offset_of_the_trim(self):
        plain = ac.build_cues(words("Hello there. General Kenobi."))
        moved = ac.build_cues(words("Hello there. General Kenobi."), offset=12.5)
        self.assertEqual([(c.start + 12.5, c.end + 12.5) for c in plain], [(c.start, c.end) for c in moved])

    def test_cues_past_the_end_of_the_listened_part_are_dropped_and_clipped(self):
        cues = ac.build_cues(words("Hello there. General Kenobi."), offset=0, limit=1.0)
        self.assertTrue(all(c.end <= 1.0 for c in cues))
        self.assertEqual(ac.build_cues(words("late"), offset=10.0, limit=5.0), [])

    def test_music_notes_and_dots_alone_are_not_captions(self):
        self.assertEqual(ac.build_cues([ac.Piece(0, 2, " ♪♪"), ac.Piece(3, 4, " ...")]), [])

    def test_nothing_in_nothing_out(self):
        self.assertEqual(ac.build_cues([]), [])

    def test_a_word_longer_than_two_lines_is_wrapped_without_losing_letters(self):
        cues = ac.build_cues([ac.Piece(0, 2, " " + "a" * 100)])
        self.assertEqual("".join(cues[0].text.split()), "a" * 100)
        self.assertTrue(all(len(line) <= 42 for line in lines_of(cues[0])))

    def test_unsorted_pieces_are_put_in_order(self):
        pieces = words("first second third")
        cues = ac.build_cues(list(reversed(pieces)))
        self.assertEqual(cues[0].text, "first second third")


class SegmentTests(unittest.TestCase):
    def segment(self, start, end, text, words=None):
        return SimpleNamespace(start=start, end=end, text=text, words=words)

    def word(self, start, end, text):
        return SimpleNamespace(start=start, end=end, word=text, probability=0.9)

    def test_words_with_their_times_are_used_when_there_are_any(self):
        seg = self.segment(1.0, 2.0, " Hi there", [self.word(1.0, 1.4, " Hi"), self.word(1.5, 2.0, " there")])
        pieces = ac.pieces_from_segments([seg])
        self.assertEqual([(p.start, p.end, p.text) for p in pieces], [(1.0, 1.4, " Hi"), (1.5, 2.0, " there")])

    def test_a_segment_without_word_times_is_spread_over_its_time(self):
        pieces = ac.pieces_from_segments([self.segment(10.0, 13.0, " one two three four")])
        self.assertEqual(len(pieces), 4)
        self.assertAlmostEqual(pieces[0].start, 10.0)
        self.assertAlmostEqual(pieces[-1].end, 13.0)
        self.assertTrue(all(a.end <= b.start + 1e-9 for a, b in zip(pieces, pieces[1:])))
        self.assertEqual(ac.build_cues(pieces)[0].text, "one two three four")

    def test_chinese_without_spaces_or_word_times_is_cut_between_letters(self):
        pieces = ac.pieces_from_segments([self.segment(0.0, 8.0, "你好" * 30)])
        cues = ac.build_cues(pieces)
        assert_well_formed(self, cues)
        self.assertEqual("".join(c.text.replace("\n", "") for c in cues), "你好" * 30)

    def test_words_that_are_only_spaces_are_ignored(self):
        seg = self.segment(0, 1, " a", [self.word(0, 0.1, " "), self.word(0.1, 0.5, " a")])
        self.assertEqual([p.text for p in ac.pieces_from_segments([seg])], [" a"])


class SrtTests(unittest.TestCase):
    def test_the_file_reads_back_the_same_with_the_existing_parser(self):
        cues = [Cue(0.5, 2.25, "Hello there"), Cue(3.0, 5.123, "Second line\nof two"),
                Cue(3661.5, 3665.0, "مرحبا بكم\nفي قناتنا")]
        back = parse_cues(format_srt(cues))
        self.assertEqual([(c.start, c.end, c.text) for c in back], [(c.start, c.end, c.text) for c in cues])

    def test_the_cues_are_numbered_from_one(self):
        text = format_srt([Cue(0, 1, "a"), Cue(1, 2, "b")])
        self.assertTrue(text.startswith("1\n00:00:00,000 --> 00:00:01,000\na\n\n2\n"))

    def test_a_blank_line_inside_a_cue_cannot_split_it(self):
        back = parse_cues(format_srt([Cue(0, 1, "one\n\ntwo")]))
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].text, "one\ntwo")

    def test_writing_makes_a_utf8_file_that_the_app_reads_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "talk.srt"
            write_srt([Cue(1.0, 2.0, "مرحبا")], path)
            self.assertEqual(path.read_bytes().decode("utf-8").splitlines()[2], "مرحبا")
            self.assertEqual(parse_cues(read_text(path))[0].text, "مرحبا")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["talk.srt"])

    def test_a_name_that_is_taken_gets_a_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "talk.mp4"
            first = ac.save_captions([Cue(0, 1, "a")], video)
            second = ac.save_captions([Cue(0, 1, "b")], video)
            self.assertEqual((first.name, second.name), ("talk.srt", "talk (1).srt"))
            self.assertEqual(ac.save_captions([Cue(0, 1, "c")], video, overwrite=True), first)
            self.assertEqual(parse_cues(first.read_text("utf-8"))[0].text, "c")

    def test_the_output_folder_is_made_if_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ac.save_captions([Cue(0, 1, "a")], Path(tmp) / "talk.mp4", Path(tmp) / "new" / "folder")
            self.assertEqual(path, Path(tmp) / "new" / "folder" / "talk.srt")
            self.assertTrue(path.is_file())

    def test_a_folder_that_cannot_be_written_gives_a_plain_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            with self.assertRaisesRegex(ConversionError, "Couldn't save the captions"):
                ac.save_captions([Cue(0, 1, "a")], Path(tmp) / "talk.mp4", blocker / "inside")


class TimeMappingTests(unittest.TestCase):
    """The cues are in the source video's time; the existing subtitle retiming moves them through the edit."""

    def retime(self, cues, segments, speed=1.0, reverse=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.srt"
            write_srt(cues, path)
            out, kind, _ = retimed_subtitles(Subtitles(file=str(path)), segments, speed, reverse)
            self.assertEqual(kind, "srt")
            return [(round(c.start, 3), round(c.end, 3), c.text.replace("\u202b", "").replace("\u202c", "")) for c in parse_cues(read_text(out))]

    def test_cues_made_from_a_trim_land_on_the_right_second_of_the_export(self):
        # The trim started at 10 s of the source; the model's times are counted from there.
        cues = ac.build_cues(words("Hello there. General Kenobi."), offset=10.0)
        out = self.retime(cues, [(10.0, 30.0)])
        self.assertAlmostEqual(out[0][0], 0.0, places=2)
        self.assertTrue(out[0][2].startswith("Hello there."))

    def test_a_cue_inside_a_cut_part_disappears_and_later_ones_move_up(self):
        cues = [Cue(1.0, 2.0, "before"), Cue(3.5, 4.5, "cut out"), Cue(6.0, 8.0, "after")]
        out = self.retime(cues, [(0.0, 3.0), (5.0, 10.0)])
        self.assertEqual([c[2] for c in out], ["before", "after"])
        self.assertEqual(out[1][:2], (4.0, 6.0))                 # 6 s of the source is 4 s into the export

    def test_a_cue_across_a_cut_shows_before_and_after_it(self):
        out = self.retime([Cue(2.0, 6.0, "long")], [(0.0, 3.0), (5.0, 10.0)])
        self.assertEqual(out, [(2.0, 4.0, "long")])         # the two pieces meet, so they are one cue

    def test_speed_shortens_the_times(self):
        out = self.retime([Cue(2.0, 4.0, "fast")], [(0.0, 10.0)], speed=2.0)
        self.assertEqual(out[0][:2], (1.0, 2.0))

    def test_cuts_and_speed_together(self):
        out = self.retime([Cue(6.0, 8.0, "x")], [(0.0, 3.0), (5.0, 10.0)], speed=2.0)
        self.assertEqual(out[0][:2], (2.0, 3.0))

    def test_reversed_playback_mirrors_the_times(self):
        out = self.retime([Cue(1.0, 2.0, "x")], [(0.0, 10.0)], reverse=True)
        self.assertEqual(out[0][:2], (8.0, 9.0))

    def test_arabic_cues_keep_their_text_through_retiming(self):
        out = self.retime([Cue(1.0, 3.0, "مرحبا بكم\nفي قناتنا")], [(0.0, 10.0)])
        self.assertEqual(out[0][2], "مرحبا بكم\nفي قناتنا")

    def test_the_marks_that_keep_arabic_lines_right_to_left_are_added_for_each_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.srt"
            write_srt([Cue(1.0, 3.0, "مرحبا بكم\nفي قناتنا")], path)
            out, _, _ = retimed_subtitles(Subtitles(file=str(path)), [(0.0, 10.0)])
            body = read_text(out).split("\n")[2:4]
        self.assertTrue(all(line.startswith("\u202b") and line.endswith("\u202c") for line in body), body)


@unittest.skipUnless(FFMPEG, "ffmpeg is needed")
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.talk = cls.make("talk.mp4", sound="sine=frequency=300:duration=10")
        cls.mute = cls.make("mute.mp4", sound=None)
        cls.silent = cls.make("silent.mp4", sound="anullsrc=r=16000:cl=mono")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def make(cls, name, sound):
        path = cls.tmp / name
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
               "color=c=0x203040:s=160x90:d=10:r=10"]
        if sound:
            cmd += ["-f", "lavfi", "-i", sound, "-t", "10", "-c:a", "aac"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
        subprocess.run(cmd, check=True)
        return path

    def setUp(self):
        self.models = Path(tempfile.mkdtemp(dir=self.tmp))
        self.server = FakeServer()
        self.model = tiny_model()
        patcher = mock.patch.dict(sm.MODELS, {"fast": self.model})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_model(self, segments=None, language="en"):
        if segments is None:
            segments = [SimpleNamespace(start=0.5, end=3.0, text=" Hello there.", words=[
                SimpleNamespace(start=0.5, end=1.0, word=" Hello", probability=1.0),
                SimpleNamespace(start=1.2, end=2.0, word=" there.", probability=1.0)])]
        model = mock.Mock()
        model.transcribe.side_effect = lambda audio, **kwargs: (iter(segments), SimpleNamespace(language=language))
        return model

    def run_captions(self, source=None, loader=None, model=None, **options):
        model = model or self.fake_model()
        self.loaded = []

        def load(folder):
            self.loaded.append(folder)
            return model

        progress = []
        result = ac.make_captions(FFMPEG, source or self.talk, ac.CaptionOptions(model="fast", **options), 10.0,
                                  root=self.models, on_progress=lambda *a: progress.append(a), cancel_event=None,
                                  loader=loader or load, opener=self.server)
        self.progress, self.model_object = progress, model
        return result

    def test_cues_come_back_in_source_time_with_the_language(self):
        result = self.run_captions()
        self.assertEqual(result.language, "en")
        self.assertEqual([(c.start, c.end, c.text) for c in result.cues], [(0.5, 2.0, "Hello there.")])

    def test_a_trim_moves_the_cues_to_where_the_speech_is_in_the_source(self):
        result = self.run_captions(start=4.0, end=9.0)
        self.assertAlmostEqual(result.cues[0].start, 4.5, places=3)
        audio = self.model_object.transcribe.call_args.args[0]
        self.assertAlmostEqual(len(audio) / 16000, 5.0, delta=0.1)       # only the trimmed part was listened to

    def test_the_model_is_asked_for_vad_word_times_and_the_chosen_language(self):
        self.run_captions(language="ar")
        kwargs = self.model_object.transcribe.call_args.kwargs
        self.assertEqual(kwargs["language"], "ar")
        self.assertEqual(kwargs["task"], "transcribe")
        self.assertTrue(kwargs["vad_filter"])
        self.assertTrue(kwargs["word_timestamps"])

    def test_automatic_language_passes_none_and_translate_asks_for_english(self):
        self.run_captions(translate=True)
        kwargs = self.model_object.transcribe.call_args.kwargs
        self.assertIsNone(kwargs["language"])
        self.assertEqual(kwargs["task"], "translate")

    def test_the_audio_is_16khz_mono_floats(self):
        self.run_captions()
        audio = self.model_object.transcribe.call_args.args[0]
        self.assertEqual(str(audio.dtype), "float32")
        self.assertEqual(audio.ndim, 1)
        self.assertAlmostEqual(len(audio) / 16000, 10.0, delta=0.2)
        self.assertGreater(float(abs(audio).max()), 0.1)

    def test_the_model_is_downloaded_first_when_it_is_missing_and_loaded_only_once_verified(self):
        self.run_captions()
        self.assertEqual(self.loaded, [sm.model_path(self.model, self.models)])
        sm.verify_model(self.model, self.models)
        phases = [p[0] for p in self.progress]
        self.assertEqual(phases[0], ac.PREPARING)
        self.assertLess(phases.index(ac.DOWNLOADING), phases.index(ac.LOADING))
        self.assertLess(phases.index(ac.LOADING), phases.index(ac.LISTENING))
        self.assertTrue(self.server.requests)

    def test_a_downloaded_model_is_not_downloaded_again(self):
        self.run_captions()
        self.server.requests.clear()
        self.run_captions()
        self.assertEqual(self.server.requests, [])
        self.assertNotIn(ac.DOWNLOADING, [p[0] for p in self.progress])

    def test_a_model_that_fails_its_check_is_never_loaded(self):
        folder = sm.download_model(self.model, self.models, open_=self.server)
        (folder / "model.bin").write_bytes(FILES["model.bin"][::-1])          # same size, other content
        self.server.tamper = {"model.bin": FILES["model.bin"][::-1]}           # and the server only has the bad one
        with self.assertRaises(ConversionError):
            self.run_captions()
        self.assertEqual(self.loaded, [])

    def test_progress_while_listening_follows_the_segment_times(self):
        segments = [SimpleNamespace(start=0.0, end=5.0, text=" a b", words=None),
                    SimpleNamespace(start=5.0, end=10.0, text=" c d", words=None)]
        self.run_captions(model=self.fake_model(segments))
        listening = [round(p[1], 2) for p in self.progress if p[0] == ac.LISTENING]
        self.assertEqual(listening, [0.0, 0.5, 1.0, 1.0])

    def test_no_speech_gives_no_cues_not_an_error(self):
        result = self.run_captions(model=self.fake_model([]))
        self.assertEqual(result.cues, [])

    def test_a_video_with_no_sound_track_says_so(self):
        with self.assertRaisesRegex(ConversionError, "no sound"):
            self.run_captions(source=self.mute)
        self.assertEqual(self.server.requests, [])         # nothing was downloaded for it

    def test_digital_silence_needs_neither_the_model_nor_a_download(self):
        result = self.run_captions(source=self.silent)
        self.assertEqual(result.cues, [])
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.loaded, [])

    def test_cancelling_while_listening_stops_with_a_cancel(self):
        cancel = threading.Event()

        def segments():
            yield SimpleNamespace(start=0, end=1, text=" a", words=None)
            cancel.set()
            yield SimpleNamespace(start=1, end=2, text=" b", words=None)

        model = mock.Mock()
        model.transcribe.side_effect = lambda audio, **kwargs: (segments(), SimpleNamespace(language="en"))
        with self.assertRaises(ConversionCancelled):
            ac.make_captions(FFMPEG, self.talk, ac.CaptionOptions(model="fast"), 10.0, root=self.models,
                             cancel_event=cancel, loader=lambda folder: model, opener=self.server)

    def test_cancelling_during_the_download_stops_it_and_keeps_the_part(self):
        cancel = threading.Event()
        with mock.patch.object(sm, "CHUNK", 1000):
            with self.assertRaises(ConversionCancelled):
                ac.make_captions(FFMPEG, self.talk, ac.CaptionOptions(model="fast"), 10.0, root=self.models,
                                 on_progress=lambda phase, f, t: cancel.set() if phase == ac.DOWNLOADING else None,
                                 cancel_event=cancel, loader=lambda folder: self.fake_model(), opener=self.server)
        self.assertTrue(any((self.models / "tiny.downloading").glob("*.part")))

    def test_a_problem_inside_the_speech_engine_becomes_a_plain_error(self):
        model = mock.Mock()
        model.transcribe.side_effect = RuntimeError("out of memory")
        with self.assertRaisesRegex(ConversionError, "Couldn't make captions: out of memory"):
            self.run_captions(model=model)

    def test_a_model_that_cannot_load_gives_a_plain_error(self):
        def broken(folder):
            raise ConversionError("Couldn't load the speech model: bad file")

        with self.assertRaisesRegex(ConversionError, "Couldn't load"):
            self.run_captions(loader=broken)

    def test_unknown_choices_are_refused(self):
        with self.assertRaisesRegex(ConversionError, "accuracy"):
            ac.make_captions(FFMPEG, self.talk, ac.CaptionOptions(model="huge"), 10.0)
        with self.assertRaisesRegex(ConversionError, "language"):
            ac.make_captions(FFMPEG, self.talk, ac.CaptionOptions(model="fast", language="xx"), 10.0)

    def test_an_empty_trim_is_refused(self):
        with self.assertRaisesRegex(ConversionError, "empty"):
            self.run_captions(start=5.0, end=5.0)

    def test_the_temporary_sound_file_is_cleaned_up(self):
        before = set(Path(tempfile.gettempdir()).glob("fve-speech-*"))
        self.run_captions()
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("fve-speech-*")) - before, set())

    def test_the_whole_chain_cuts_into_sensible_cues_and_survives_a_cut_in_the_edit(self):
        words_ = [SimpleNamespace(start=0.4 * i, end=0.4 * i + 0.3, word=f" w{i}" + ("." if i % 5 == 4 else ""),
                                  probability=1.0) for i in range(15)]
        segment = SimpleNamespace(start=0.0, end=6.0, text="", words=words_)
        result = self.run_captions(model=self.fake_model([segment]))
        assert_well_formed(self, result.cues)
        self.assertEqual(len(result.cues), 3)
        out = TimeMappingTests().retime(result.cues, [(0.0, 2.0), (4.0, 10.0)])
        self.assertTrue(all(c[1] <= 8.0 for c in out))
        self.assertLess(len(out), len(result.cues) + 1)


@unittest.skipUnless(FFMPEG, "ffmpeg is needed")
@unittest.skipUnless(sm.is_downloaded(sm.MODELS["fast"]), "the Fast speech model hasn't been downloaded on this PC")
class RealModelTests(unittest.TestCase):
    """Runs the real Fast model on made-up sound. There is no speech in it to check the words of, so this
    only shows that the whole chain runs and ends gracefully."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def make(self, name, sound):
        path = self.tmp / name
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", sound, "-t", "4",
                        str(path)], check=True)
        return path

    def test_a_tone_runs_through_the_real_model_without_a_crash(self):
        result = ac.make_captions(FFMPEG, self.make("tone.wav", "sine=frequency=440:duration=4"),
                                  ac.CaptionOptions(model="fast"), 4.0)
        self.assertIsInstance(result.cues, list)
        assert_well_formed(self, result.cues)

    def test_noise_runs_through_the_real_model_without_a_crash(self):
        result = ac.make_captions(FFMPEG, self.make("noise.wav", "anoisesrc=d=4:c=pink:a=0.2"),
                                  ac.CaptionOptions(model="fast", language="en"), 4.0)
        self.assertIsInstance(result.cues, list)
        assert_well_formed(self, result.cues)


class SelfTestTests(unittest.TestCase):
    def test_the_engine_and_its_voice_detector_load(self):
        self.assertTrue(ac.self_test().startswith("ok:"))

    def test_the_exe_flag_writes_its_result_to_a_file_and_returns_zero(self):
        try:
            from video_converter.ui import app
        except ImportError:
            self.skipTest("PySide6 is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "result.txt"
            self.assertEqual(app.main([app.CAPTIONS_SELF_TEST, str(report)]), 0)
            self.assertTrue(report.read_text(encoding="utf-8").startswith("ok:"))

    def test_a_broken_engine_is_reported_with_a_failure_code(self):
        try:
            from video_converter.ui import app
        except ImportError:
            self.skipTest("PySide6 is not installed")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ac, "self_test", side_effect=ImportError("nope")):
            report = Path(tmp) / "result.txt"
            self.assertEqual(app.captions_self_test(str(report)), 1)
            self.assertIn("FAILED", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
