import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from video_converter.core import DEFAULT_QUALITY, QUALITY_PRESETS
from video_converter.project import (
    FRAMING_FIELDS,
    OUTPUT_AUDIO,
    OUTPUT_VIDEO,
    PRESETS,
    ClipSettings,
    apply_preset,
    apply_to,
    describe,
    output_file_for,
)
from video_converter.video import VideoEdit


def custom_settings() -> ClipSettings:
    """Settings where every field differs from its default."""
    return ClipSettings(
        output=OUTPUT_AUDIO,
        edit=VideoEdit(aspect=(4, 5), fit="bars", pos_x=0.25, pos_y=0.75, zoom=1.75, rotate=270, flip=True,
                       resolution=720, crf=18, mute=True, start=1.5, end=9.25),
        audio_quality="320 kbps",
    )


class RoundTripTests(unittest.TestCase):
    def test_custom_settings_cover_every_field(self):
        # Guards the round-trip tests below: a new VideoEdit field must be added to custom_settings().
        defaults, custom = VideoEdit(), custom_settings().edit
        for f in fields(VideoEdit):
            with self.subTest(field=f.name):
                self.assertNotEqual(getattr(defaults, f.name), getattr(custom, f.name))
        self.assertNotEqual(ClipSettings().output, custom_settings().output)
        self.assertNotEqual(ClipSettings().audio_quality, custom_settings().audio_quality)

    def test_default_settings_round_trip(self):
        settings = ClipSettings()
        self.assertEqual(ClipSettings.from_dict(json.loads(json.dumps(settings.to_dict()))), settings)

    def test_every_field_round_trips_through_json(self):
        settings = custom_settings()
        loaded = ClipSettings.from_dict(json.loads(json.dumps(settings.to_dict())))
        self.assertEqual(loaded, settings)
        self.assertIsInstance(loaded.edit.aspect, tuple)

    def test_original_shape_aspect_none_round_trips(self):
        settings = ClipSettings(edit=VideoEdit(aspect=None))
        data = json.loads(json.dumps(settings.to_dict()))
        self.assertIsNone(data["edit"]["aspect"])
        self.assertIsNone(ClipSettings.from_dict(data).edit.aspect)

    def test_to_dict_does_not_change_the_settings(self):
        settings = custom_settings()
        settings.to_dict()
        self.assertEqual(settings.edit.aspect, (4, 5))
        self.assertIsInstance(settings.edit.aspect, tuple)

    def test_to_dict_is_independent_of_the_settings(self):
        settings = custom_settings()
        data = settings.to_dict()
        data["edit"]["zoom"] = 9.0
        self.assertEqual(settings.edit.zoom, 1.75)

    def test_copy_is_deep(self):
        settings = custom_settings()
        clone = settings.copy()
        self.assertEqual(clone, settings)
        clone.edit.zoom = 3.0
        self.assertEqual(settings.edit.zoom, 1.75)

    def test_trim_values_may_be_none_or_numbers(self):
        for start, end in ((None, None), (None, 12.5), (3.0, None), (0.0, 7), (2, 5)):
            with self.subTest(start=start, end=end):
                loaded = ClipSettings.from_dict({"edit": {"start": start, "end": end}})
                self.assertEqual((loaded.edit.start, loaded.edit.end), (start, end))


class DefensiveLoadingTests(unittest.TestCase):
    def load(self, **edit):
        return ClipSettings.from_dict({"edit": edit})

    def test_empty_and_missing_sections_give_defaults(self):
        self.assertEqual(ClipSettings.from_dict({}), ClipSettings())
        self.assertEqual(ClipSettings.from_dict({"edit": None}), ClipSettings())
        self.assertEqual(ClipSettings.from_dict({"edit": {}}), ClipSettings())

    def test_unknown_keys_are_ignored(self):
        data = custom_settings().to_dict()
        data["future_option"] = 1
        data["edit"]["sharpen"] = 0.5
        self.assertEqual(ClipSettings.from_dict(data), custom_settings())

    def test_wrong_types_keep_the_default(self):
        defaults = VideoEdit()
        bad_values = {
            "crf": [True, "23", 23.5, None],
            "zoom": ["big", True, None, [1]],
            "pos_x": ["left", True],
            "pos_y": [{}, False],
            "rotate": [45, "90", True, 90.0, -90, 360],
            "fit": ["stretch", 1, None, ""],
            "aspect": [[0, 16], [9, 0], [-9, 16], [9], [9, 16, 1], "9:16", 1.5, {"w": 9, "h": 16}, [9.0, 16.0],
                       ["9", "16"]],
            "flip": [1, "yes", None],
            "mute": [0, "no", None],
            "resolution": ["720p", True, [720]],
            "start": ["5", True, [1]],
            "end": ["5", False, {}],
        }
        for name, values in bad_values.items():
            for value in values:
                with self.subTest(field=name, value=value):
                    loaded = self.load(**{name: value})
                    self.assertEqual(getattr(loaded.edit, name), getattr(defaults, name))

    def test_wrong_top_level_values_keep_the_default(self):
        for output in ("gif", None, 1, "VIDEO"):
            with self.subTest(output=output):
                self.assertEqual(ClipSettings.from_dict({"output": output}).output, OUTPUT_VIDEO)
        for quality in ("999 kbps", None, 192, ""):
            with self.subTest(audio_quality=quality):
                self.assertEqual(ClipSettings.from_dict({"audio_quality": quality}).audio_quality, DEFAULT_QUALITY)

    def test_a_bad_value_does_not_discard_the_good_ones(self):
        loaded = self.load(zoom="big", rotate=90, crf=True, mute=True, aspect=[1, 1])
        self.assertEqual(loaded.edit.zoom, 1.0)
        self.assertEqual(loaded.edit.crf, VideoEdit().crf)
        self.assertEqual((loaded.edit.rotate, loaded.edit.mute, loaded.edit.aspect), (90, True, (1, 1)))

    def test_ints_are_accepted_for_float_fields(self):
        loaded = self.load(zoom=2, pos_x=1, pos_y=0)
        self.assertEqual((loaded.edit.zoom, loaded.edit.pos_x, loaded.edit.pos_y), (2.0, 1.0, 0.0))
        self.assertIsInstance(loaded.edit.zoom, float)
        self.assertIsInstance(loaded.edit.pos_x, float)
        self.assertIsInstance(loaded.edit.pos_y, float)

    def test_valid_choices_are_accepted(self):
        for rotate in (0, 90, 180, 270):
            self.assertEqual(self.load(rotate=rotate).edit.rotate, rotate)
        for fit in ("crop", "blur", "bars"):
            self.assertEqual(self.load(fit=fit).edit.fit, fit)
        for quality in QUALITY_PRESETS:
            self.assertEqual(ClipSettings.from_dict({"audio_quality": quality}).audio_quality, quality)
        self.assertEqual(self.load(aspect=(16, 9)).edit.aspect, (16, 9))
        self.assertEqual(self.load(aspect=[16, 9]).edit.aspect, (16, 9))
        self.assertEqual(self.load(resolution=None).edit.resolution, None)
        self.assertEqual(self.load(resolution=480).edit.resolution, 480)

    # The next tests describe the documented contract ("ignoring unknown keys and bad values so
    # presets saved by other versions still load"). Values that are lists or dicts reach a
    # `value in <dict>` test and raise TypeError instead of being ignored.

    def test_unhashable_audio_quality_is_ignored(self):
        # Bug: project.py ClipSettings.from_dict does `data.get("audio_quality") in QUALITY_PRESETS`.
        # Input {"audio_quality": ["128 kbps"]} raises TypeError (unhashable list); expected: default kept.
        loaded = ClipSettings.from_dict({"audio_quality": ["128 kbps"]})
        self.assertEqual(loaded.audio_quality, DEFAULT_QUALITY)

    def test_unhashable_fit_is_ignored(self):
        # Bug: project.py ClipSettings.from_dict does `value not in FIT_MODES` for "fit".
        # Input {"edit": {"fit": ["crop"]}} raises TypeError (unhashable list); expected: default kept.
        loaded = self.load(fit=["crop"])
        self.assertEqual(loaded.edit.fit, VideoEdit().fit)

    def test_non_mapping_edit_section_is_ignored(self):
        # Bug: project.py ClipSettings.from_dict only guards `edit` with `or {}`.
        # Input {"edit": 5} raises TypeError ("argument of type 'int' is not iterable"); {"edit": "fit"}
        # raises TypeError too. Expected: defaults.
        for edit in (5, "fit", 1.5):
            with self.subTest(edit=edit):
                self.assertEqual(ClipSettings.from_dict({"edit": edit}), ClipSettings())


class ApplyToTests(unittest.TestCase):
    def setUp(self):
        self.source = custom_settings()
        self.target = ClipSettings(
            edit=VideoEdit(aspect=(1, 1), fit="blur", pos_x=0.1, pos_y=0.9, zoom=2.5, start=10.0, end=20.0,
                           crf=28, mute=False, rotate=90),
            audio_quality="128 kbps",
        )

    def test_framing_fields_are_exactly_the_per_file_ones(self):
        self.assertEqual(set(FRAMING_FIELDS), {"pos_x", "pos_y", "zoom", "start", "end"})
        names = {f.name for f in fields(VideoEdit)}
        self.assertTrue(set(FRAMING_FIELDS) <= names)

    def test_copies_export_settings_but_keeps_target_framing(self):
        result = apply_to(self.target, self.source)
        for name in FRAMING_FIELDS:
            with self.subTest(field=name):
                self.assertEqual(getattr(result.edit, name), getattr(self.target.edit, name))
        for f in fields(VideoEdit):
            if f.name not in FRAMING_FIELDS:
                with self.subTest(field=f.name):
                    self.assertEqual(getattr(result.edit, f.name), getattr(self.source.edit, f.name))
        self.assertEqual(result.output, self.source.output)
        self.assertEqual(result.audio_quality, self.source.audio_quality)

    def test_include_framing_takes_everything_from_the_source(self):
        self.assertEqual(apply_to(self.target, self.source, include_framing=True), self.source)

    def test_target_trim_of_none_is_kept(self):
        target = ClipSettings(edit=VideoEdit(start=None, end=None))
        result = apply_to(target, self.source)
        self.assertEqual((result.edit.start, result.edit.end), (None, None))

    def test_does_not_mutate_either_input(self):
        source_before, target_before = self.source.copy(), self.target.copy()
        for include in (False, True):
            result = apply_to(self.target, self.source, include_framing=include)
            result.edit.zoom = 99.0
            result.audio_quality = "256 kbps"
            self.assertEqual(self.source, source_before)
            self.assertEqual(self.target, target_before)
            self.assertIsNot(result, self.source)
            self.assertIsNot(result, self.target)
            self.assertIsNot(result.edit, self.source.edit)


class PresetTests(unittest.TestCase):
    def test_only_the_presets_keys_change(self):
        base = custom_settings()
        base.edit.rotate, base.edit.flip, base.edit.mute = 90, True, True
        preset = PRESETS["Square post (1:1)"]
        result = apply_preset(base, preset)

        self.assertEqual(result.output, OUTPUT_VIDEO)
        for key, value in preset.items():
            if key != "output":
                self.assertEqual(getattr(result.edit, key), value)
        for f in fields(VideoEdit):
            if f.name not in preset:
                self.assertEqual(getattr(result.edit, f.name), getattr(base.edit, f.name), f.name)
        self.assertEqual(result.audio_quality, base.audio_quality)

    def test_returns_a_copy_and_leaves_the_input_alone(self):
        base = custom_settings()
        before = base.copy()
        result = apply_preset(base, PRESETS["TikTok, Reels, Shorts"])
        self.assertIsNot(result, base)
        self.assertEqual(base, before)
        result.edit.zoom = 5.0
        self.assertEqual(base, before)

    def test_audio_preset_leaves_video_edit_alone(self):
        base = ClipSettings(edit=VideoEdit(aspect=(1, 1), fit="bars", zoom=2.0))
        result = apply_preset(base, PRESETS["MP3 audio"])
        self.assertEqual(result.output, OUTPUT_AUDIO)
        self.assertEqual(result.edit, base.edit)

    def test_preset_with_audio_quality(self):
        result = apply_preset(ClipSettings(), {"audio_quality": "320 kbps"})
        self.assertEqual(result.audio_quality, "320 kbps")
        self.assertEqual(result.edit, VideoEdit())

    def test_every_preset_applies_and_survives_a_round_trip(self):
        self.assertTrue(PRESETS)
        for name, values in PRESETS.items():
            for base in (ClipSettings(), custom_settings()):
                with self.subTest(preset=name):
                    result = apply_preset(base, values)
                    self.assertEqual(ClipSettings.from_dict(json.loads(json.dumps(result.to_dict()))), result)
                    # Applying twice changes nothing more.
                    self.assertEqual(apply_preset(result, values), result)

    def test_preset_values_are_valid_settings(self):
        # A preset that holds an invalid value would be dropped silently when saved and reloaded.
        for name, values in PRESETS.items():
            with self.subTest(preset=name):
                result = apply_preset(ClipSettings(), values)
                self.assertIn(result.output, (OUTPUT_VIDEO, OUTPUT_AUDIO))
                loaded = ClipSettings.from_dict(result.to_dict())
                self.assertEqual(loaded.edit.fit, result.edit.fit)
                self.assertEqual(loaded.edit.aspect, result.edit.aspect)
                self.assertEqual(loaded.edit.crf, result.edit.crf)
                self.assertEqual(loaded.edit.resolution, result.edit.resolution)

    def test_presets_do_not_touch_framing_or_trim(self):
        base = custom_settings()
        for name, values in PRESETS.items():
            with self.subTest(preset=name):
                result = apply_preset(base, values)
                for field_name in FRAMING_FIELDS:
                    self.assertEqual(getattr(result.edit, field_name), getattr(base.edit, field_name))

    def test_preset_dicts_are_not_modified_by_applying_them(self):
        snapshot = {name: dict(values) for name, values in PRESETS.items()}
        for values in PRESETS.values():
            apply_preset(custom_settings(), values).edit.zoom = 7.0
        self.assertEqual(PRESETS, snapshot)


class OutputFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "holiday.mov"

    def video(self, **edit):
        return ClipSettings(edit=VideoEdit(**edit))

    def test_audio_output_is_an_mp3_named_after_the_source(self):
        self.assertEqual(output_file_for(self.source, ClipSettings(output=OUTPUT_AUDIO)), self.tmp / "holiday.mp3")

    def test_video_names_carry_the_shape(self):
        cases = {
            (9, 16): "holiday (phone).mp4",
            (1, 1): "holiday (square).mp4",
            (4, 5): "holiday (portrait).mp4",
            (16, 9): "holiday (widescreen).mp4",
            None: "holiday (edited).mp4",
        }
        for aspect, name in cases.items():
            with self.subTest(aspect=aspect):
                self.assertEqual(output_file_for(self.source, self.video(aspect=aspect)), self.tmp / name)

    def test_unknown_aspect_falls_back_to_edited(self):
        self.assertEqual(output_file_for(self.source, self.video(aspect=(3, 2))), self.tmp / "holiday (edited).mp4")

    def test_video_output_never_has_the_source_name(self):
        # An export must not be able to replace the original video, whatever the shape.
        source = self.tmp / "clip.mp4"
        for aspect in (None, (9, 16), (1, 1), (4, 5), (16, 9)):
            self.assertNotEqual(output_file_for(source, self.video(aspect=aspect)), source)

    def test_custom_output_directory(self):
        out = self.tmp / "exports"
        self.assertEqual(output_file_for(self.source, ClipSettings(output=OUTPUT_AUDIO), out), out / "holiday.mp3")
        self.assertEqual(output_file_for(self.source, ClipSettings(), out), out / "holiday (phone).mp4")

    def test_does_not_create_anything(self):
        out = self.tmp / "not-yet"
        output_file_for(self.source, ClipSettings(), out)
        self.assertFalse(out.exists())

    def test_existing_files_are_never_returned(self):
        settings = ClipSettings()
        taken = []
        for expected in ("holiday (phone).mp4", "holiday (phone) (1).mp4", "holiday (phone) (2).mp4"):
            path = output_file_for(self.source, settings)
            self.assertEqual(path, self.tmp / expected)
            self.assertNotIn(path, taken)
            path.write_bytes(b"x")
            taken.append(path)

    def test_audio_collision_adds_a_suffix(self):
        settings = ClipSettings(output=OUTPUT_AUDIO)
        (self.tmp / "holiday.mp3").write_bytes(b"x")
        self.assertEqual(output_file_for(self.source, settings), self.tmp / "holiday (1).mp3")

    def test_gap_in_numbering_is_filled_first(self):
        (self.tmp / "holiday.mp3").write_bytes(b"x")
        (self.tmp / "holiday (2).mp3").write_bytes(b"x")
        self.assertEqual(output_file_for(self.source, ClipSettings(output=OUTPUT_AUDIO)), self.tmp / "holiday (1).mp3")

    def test_dots_in_the_source_name_are_kept(self):
        source = self.tmp / "my.trip.2024.mkv"
        self.assertEqual(output_file_for(source, ClipSettings(output=OUTPUT_AUDIO)), self.tmp / "my.trip.2024.mp3")
        self.assertEqual(output_file_for(source, ClipSettings()), self.tmp / "my.trip.2024 (phone).mp4")


class DescribeTests(unittest.TestCase):
    def test_audio(self):
        text = describe(ClipSettings(output=OUTPUT_AUDIO, audio_quality="320 kbps"))
        self.assertIn("MP3", text)
        self.assertIn("320 kbps", text)
        self.assertNotIn("MP4", text)

    def test_audio_ignores_video_only_options(self):
        text = describe(ClipSettings(output=OUTPUT_AUDIO, edit=VideoEdit(rotate=90, mute=True, aspect=(1, 1))))
        self.assertNotIn("rotated", text)
        self.assertNotIn("no sound", text)
        self.assertNotIn("1:1", text)

    def test_video_with_aspect_and_fit(self):
        for fit, label in (("crop", "crop"), ("blur", "blurred"), ("bars", "bars")):
            with self.subTest(fit=fit):
                text = describe(ClipSettings(edit=VideoEdit(aspect=(9, 16), fit=fit)))
                self.assertIn("MP4", text)
                self.assertIn("9:16", text)
                self.assertIn(label, text)

    def test_original_shape_does_not_mention_fit(self):
        text = describe(ClipSettings(edit=VideoEdit(aspect=None, fit="blur")))
        self.assertIn("original shape", text)
        self.assertNotIn("blurred", text)
        self.assertNotIn("None", text)

    def test_rotation_and_mute(self):
        text = describe(ClipSettings(edit=VideoEdit(rotate=270, mute=True)))
        self.assertIn("rotated 270", text)
        self.assertIn("no sound", text)
        plain = describe(ClipSettings())
        self.assertNotIn("rotated", plain)
        self.assertNotIn("no sound", plain)

    def test_trims(self):
        both = describe(ClipSettings(edit=VideoEdit(start=5.0, end=20.0)))
        self.assertIn("0:05.0", both)
        self.assertIn("0:20.0", both)
        self.assertNotIn("from", both)

        only_start = describe(ClipSettings(edit=VideoEdit(start=65.0)))
        self.assertIn("from 1:05.0", only_start)

        only_end = describe(ClipSettings(edit=VideoEdit(end=20.0)))
        self.assertIn("0:00.0", only_end)
        self.assertIn("0:20.0", only_end)

        zero_start = describe(ClipSettings(edit=VideoEdit(start=0.0, end=3.0)))
        self.assertIn("0:03.0", zero_start)

        self.assertNotIn("0:0", describe(ClipSettings()))

    def test_trim_is_shown_for_audio_too(self):
        text = describe(ClipSettings(output=OUTPUT_AUDIO, edit=VideoEdit(start=2.0, end=4.0)))
        self.assertIn("0:02.0", text)
        self.assertIn("0:04.0", text)

    def test_every_preset_can_be_described(self):
        for name, values in PRESETS.items():
            with self.subTest(preset=name):
                self.assertTrue(describe(apply_preset(ClipSettings(), values)))


if __name__ == "__main__":
    unittest.main()
