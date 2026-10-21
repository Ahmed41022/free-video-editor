"""Text, logo and subtitles (2.8): filter strings, fonts, subtitle timing, and real exports whose pixels are checked."""

import contextlib
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from video_converter import captions, fonts
from video_converter.captions import (
    Logo, Subtitles, TextItem, filter_value, has_overlays, load_logo, load_subtitles, load_texts, map_span,
    overlay_graph, parse_cues, read_text, retime_ass, retimed_subtitles, subtitle_kind, text_box, text_filter,
)
from video_converter.core import ConversionError, MediaInfo, find_ffmpeg, probe_media
from video_converter.project import ClipSettings, apply_to, apply_preset, describe
from video_converter.timing import output_spans_to_source, output_to_source, source_to_output
from video_converter.video import (
    JoinJob, JoinPart, VideoEdit, VideoJob, clip_graph, convert_video, join_clips, plan_geometry, render_frame,
    video_command,
)

FFMPEG = find_ffmpeg()
W, H = 320, 180
FRAME = (W, H)
WINDOWS = sys.platform == "win32" and fonts.font_file("Arial") is not None


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def text_file_of(filter_text: str) -> str:
    """The text a drawtext filter reads from its file."""
    name = filter_text.split("textfile=")[1].split(":expansion")[0]
    return read_file(name.replace("\\\\:", ":").replace("\\\\", "\\"))


def run_ffmpeg(*args: str, binary: bool = False):
    result = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True)
    return result if binary else (result.returncode, result.stderr.decode("utf-8", "replace"))


def render(vf: str, size=(W, H), color="black", pix="gray", seconds=1) -> bytes:
    """One frame of a plain `color` picture run through the filter `vf`, as raw pixels."""
    result = run_ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s={size[0]}x{size[1]}:d={seconds}:r=15", "-vf", vf,
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", pix, "-", binary=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout


def grab(path: Path, t: float, pix: str = "gray") -> bytes:
    result = run_ffmpeg("-ss", f"{t}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", pix, "-",
                        binary=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout


def bright(gray: bytes, width: int = W, box=None, level: int = 128) -> int:
    """How many pixels of a gray frame are above `level`, in `box` = (left, top, right, bottom) if given."""
    height = len(gray) // width
    left, top, right, bottom = box or (0, 0, width, height)
    return sum(1 for y in range(top, bottom) for x in range(left, right) if gray[y * width + x] > level)


def centroid(gray: bytes, width: int = W, level: int = 128):
    points = [(i % width, i // width) for i, v in enumerate(gray) if v > level]
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)) if points else None


def rgb_at(frame: bytes, x: int, y: int, width: int = W) -> tuple[int, int, int]:
    i = (y * width + x) * 3
    return frame[i], frame[i + 1], frame[i + 2]


def write_png(path: Path, width: int, height: int, pixel) -> None:
    """A PNG with RGBA pixels from `pixel(x, y)`, written by hand so the tests need no image library."""
    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def make_black(path: Path, seconds: int = 4, width: int = W, height: int = H) -> None:
    code, error = run_ffmpeg("-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d={seconds}:r=15",
                             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
    assert code == 0, error


def info_for(seconds=4.0) -> MediaInfo:
    return MediaInfo(duration=seconds, width=W, height=H, has_audio=False, fps=15.0)


class FilterEscapingTests(unittest.TestCase):
    def test_both_levels_of_escaping(self):
        self.assertEqual(filter_value("a:b'c,d;e[f]g\\h"), "a\\\\:b\\\\\\'c\\,d\\;e\\[f\\]g\\\\\\\\h")

    def test_plain_text_is_left_alone(self):
        self.assertEqual(filter_value("C/Users/me/logo.png"), "C/Users/me/logo.png")

    def test_a_path_with_every_odd_character_works_in_a_real_filter(self):
        folder = Path(tempfile.mkdtemp(prefix="odd ' [x], y;z=1 "))
        try:
            with mock.patch.object(captions, "_scratch", folder):
                item = TextItem(text="hi", size=0.3)
                vf = text_filter(item, FRAME, None)
                self.assertGreater(bright(render(vf)), 50)
                self.assertIn("textfile=", vf)
                self.assertTrue(any(folder.glob("*.txt")))
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_font_file_with_odd_characters_in_its_path_works(self):
        if not WINDOWS:
            self.skipTest("needs the Windows fonts")
        folder = Path(tempfile.mkdtemp(prefix="odd ' [x], y;z=1 "))
        try:
            copy = folder / "my: font's [file].ttf"
            shutil.copyfile(fonts.font_file("Arial"), copy)
            vf = f"drawtext=fontfile={captions._path_value(copy)}:text=Hi:fontsize=60:fontcolor=white"
            self.assertGreater(bright(render(vf)), 50)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


TRICKY = [
    "plain",
    "it's \"quoted\": 100% sure",
    "back\\slash and C:\\path\\file.txt",
    "%{localtime} %{pts} %d \\%",
    "semi; colon: comma, [brackets] = equals",
    "line one\nline two\nline three",
    "windows\r\nnewlines\rand mac",
    "emoji \U0001F600 and \u00e9\u00e8 \u4e2d\u6587",
    "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645",
    "\u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd",
    "' \" \\ ; : , [ ]",
]


class TextFilterTests(unittest.TestCase):
    def test_the_text_never_appears_in_the_filter_only_in_its_file(self):
        for text in TRICKY:
            with self.subTest(text=text):
                vf = text_filter(TextItem(text=text, size=0.05), FRAME, None)
                self.assertTrue(vf.startswith("drawtext="))
                self.assertIn("expansion=none", vf)          # %{...} is shown, not run
                self.assertIn("text_shaping=1", vf)
                self.assertNotIn(":text=", vf)
                for word in ("quoted", "localtime", "brackets", "newlines"):
                    self.assertNotIn(word, vf)
                drawn = text_file_of(vf).replace(captions.RTL_START, "").replace(captions.RTL_END, "")
                self.assertEqual(drawn.replace("\n", " ").split(), text.replace("\r\n", "\n")
                                 .replace("\r", "\n").replace("\n", " ").split())

    def test_every_tricky_text_runs_through_real_ffmpeg(self):
        for text in TRICKY:
            with self.subTest(text=text):
                frame = render(text_filter(TextItem(text=text, size=0.1), FRAME, None))
                self.assertGreater(bright(frame), 20, "nothing was drawn")

    def test_percent_commands_are_drawn_as_they_are_typed(self):
        literal = render(text_filter(TextItem(text="%{pts}", size=0.2), FRAME, None))
        expanded = render("drawtext=fontfile=%s:text='%%{pts}':fontsize=36:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2"
                          % captions._path_value(Path(fonts.pick_font("Arial", False, "") or "")))
        self.assertNotEqual(literal, expanded)

    def test_empty_text_and_empty_time_draw_nothing(self):
        self.assertEqual(text_filter(TextItem(text=""), FRAME, None), "")
        self.assertEqual(text_filter(TextItem(text="  \n "), FRAME, None), "")
        self.assertEqual(text_filter(TextItem(text="x", start=3, end=3), FRAME, None), "")
        self.assertEqual(text_filter(TextItem(text="x", start=3, end=2), FRAME, None), "")

    def test_the_size_follows_the_height_of_the_video(self):
        small = text_filter(TextItem(text="Hi", size=0.1), (640, 360), None)
        large = text_filter(TextItem(text="Hi", size=0.1), (1280, 720), None)
        self.assertIn("fontsize=36:", small)
        self.assertIn("fontsize=72:", large)

    def test_the_position_is_the_middle_of_the_text_kept_inside_the_frame(self):
        vf = text_filter(TextItem(text="Hi", x=0.25, y=0.75, outline=0), FRAME, None)
        self.assertIn("x='max(0,min(w-text_w-0,w*0.25-text_w/2))'", vf)
        self.assertIn("y='max(0,min(h-text_h-0,h*0.75-text_h/2))'", vf)

    def test_outline_and_box_settings(self):
        vf = text_filter(TextItem(text="Hi", size=0.1, outline=0.1, outline_color="#ff0000", box=True,
                                  box_color="#00ff00", box_opacity=0.5), FRAME, None)
        self.assertIn("borderw=2:bordercolor=0xFF0000", vf)
        self.assertIn("box=1:boxcolor=0x00FF00@0.5:boxborderw=5", vf)
        self.assertNotIn("borderw", text_filter(TextItem(text="Hi", outline=0), FRAME, None))
        self.assertNotIn("box=1", text_filter(TextItem(text="Hi", box=False), FRAME, None))

    def test_alignment_only_matters_for_more_than_one_line(self):
        self.assertNotIn("text_align", text_filter(TextItem(text="one line", align="right"), FRAME, None))
        self.assertIn("text_align=right", text_filter(TextItem(text="a\nb", align="right"), FRAME, None))
        self.assertNotIn("text_align", text_filter(TextItem(text="a\nb", align="left"), FRAME, None))

    def test_time_window_is_on_the_exported_timeline(self):
        self.assertIn("enable='between(t,1.5,3)'", text_filter(TextItem(text="x", start=1.5, end=3), FRAME, 10))
        self.assertIn("enable='gte(t,2)'", text_filter(TextItem(text="x", start=2), FRAME, 10))
        self.assertNotIn("enable", text_filter(TextItem(text="x"), FRAME, 10))

    def test_fades(self):
        both = text_filter(TextItem(text="x", start=1, end=5, fade_in=0.5, fade_out=1), FRAME, None)
        self.assertIn("alpha='min(min(1,(t-1)/0.5),(5-t)/1)'", both)
        self.assertIn("alpha='min(1,(t-1)/0.5)'", text_filter(TextItem(text="x", start=1, end=5, fade_in=0.5), FRAME, None))
        self.assertNotIn("alpha", text_filter(TextItem(text="x", start=1, end=5), FRAME, None))

    def test_fades_are_never_longer_than_the_text_is_shown(self):
        vf = text_filter(TextItem(text="x", start=0, end=2, fade_in=3, fade_out=1), FRAME, None)
        self.assertIn("(t-0)/1.5", vf)
        self.assertIn("(2-t)/0.5", vf)

    def test_a_fade_out_without_an_end_uses_the_length_of_the_video(self):
        vf = text_filter(TextItem(text="x", start=1, fade_out=2), FRAME, 6)
        self.assertIn("(6-t)/2", vf)
        self.assertNotIn("alpha", text_filter(TextItem(text="x", start=1, fade_out=2), FRAME, None))

    def test_long_text_is_wrapped_to_fit_the_frame(self):
        if not WINDOWS:
            self.skipTest("needs the Windows fonts")
        long = "the quick brown fox jumps over the lazy dog " * 3
        vf = text_filter(TextItem(text=long, size=0.08), FRAME, None)
        lines = text_file_of(vf).split("\n")
        self.assertGreater(len(lines), 1)
        font = fonts.load_font(fonts.pick_font("Arial", False, long))
        for line in lines:
            self.assertLessEqual(font.width(line, 0.08 * H), W * captions.WRAP_WIDTH + 1)
        self.assertEqual(" ".join(lines).split(), long.split())

    def test_lines_the_person_broke_are_kept(self):
        # the blank line stays (as a line holding one space, so it keeps its height)
        self.assertEqual(text_file_of(text_filter(TextItem(text="a\n\nb"), FRAME, None)), "a\n \nb")

    def test_the_box_the_editor_draws_is_close_to_the_real_text(self):
        if not WINDOWS:
            self.skipTest("needs the Windows fonts")
        item = TextItem(text="WWWW WWWW", size=0.2, outline=0, y=0.5)
        frame = render(text_filter(item, FRAME, None))
        xs = [i % W for i, v in enumerate(frame) if v > 128]
        ys = [i // W for i, v in enumerate(frame) if v > 128]
        width, height = max(xs) - min(xs), max(ys) - min(ys)
        box_w, box_h = text_box(item, FRAME)
        self.assertLess(abs(box_w - width) / width, 0.12)
        self.assertGreater(box_h, height * 0.9)
        self.assertLess(box_h, height * 2.0)


class OverlayGraphTests(unittest.TestCase):
    def test_nothing_to_draw(self):
        self.assertEqual(overlay_graph("[a]", "[b]", [], Logo(), Subtitles(), FRAME, [(0, 4.0)]), [])
        self.assertFalse(has_overlays([], Logo(), Subtitles()))
        self.assertFalse(has_overlays([TextItem(text="")], Logo(), Subtitles()))
        self.assertTrue(has_overlays([TextItem()], Logo(), Subtitles()))
        self.assertTrue(has_overlays([], Logo(file="x.png"), Subtitles()))
        self.assertTrue(has_overlays([], Logo(), Subtitles(file="x.srt")))

    def test_texts_are_one_chain_from_the_source_label_to_the_result(self):
        steps = overlay_graph("[a]", "[b]", [TextItem(text="one"), TextItem(text="two")], Logo(), Subtitles(), FRAME,
                              [(0, 4.0)])
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0].startswith("[a]drawtext=") and steps[0].endswith("[b]"))
        self.assertEqual(steps[0].count("drawtext="), 2)

    def test_the_logo_goes_first_then_subtitles_then_texts(self):
        folder = Path(tempfile.mkdtemp())
        try:
            logo, srt = folder / "logo.png", folder / "a.srt"
            write_png(logo, 8, 8, lambda x, y: (255, 0, 0, 255))
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
            steps = overlay_graph("[a]", "[b]", [TextItem(text="t")], Logo(file=str(logo)), Subtitles(file=str(srt)),
                                  FRAME, [(0, 4.0)], tag="7")
            self.assertEqual(len(steps), 3)
            self.assertTrue(steps[0].startswith("movie=") and steps[0].endswith("[logo7]"))
            self.assertEqual(steps[1].split("]")[0] + "]", "[a]")
            self.assertIn("[logo7]overlay=", steps[1])
            self.assertTrue(steps[1].endswith("[lg7]"))
            self.assertTrue(steps[2].startswith("[lg7]subtitles=") and steps[2].endswith("[b]"))
            self.assertLess(steps[2].index("subtitles="), steps[2].index("drawtext="))
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_logo_alone_ends_in_the_result_label(self):
        folder = Path(tempfile.mkdtemp())
        try:
            logo = folder / "logo.png"
            write_png(logo, 8, 8, lambda x, y: (255, 0, 0, 255))
            steps = overlay_graph("[a]", "[b]", [], Logo(file=str(logo)), Subtitles(), FRAME, [(0, 4.0)])
            self.assertEqual(len(steps), 2)
            self.assertTrue(steps[1].endswith("[b]"))
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_missing_logo_or_subtitle_file_is_an_error_that_names_it(self):
        with self.assertRaisesRegex(ConversionError, "nothing.png"):
            overlay_graph("[a]", "[b]", [], Logo(file="/no/such/nothing.png"), Subtitles(), FRAME, [(0, 4.0)])
        with self.assertRaisesRegex(ConversionError, "nothing.srt"):
            overlay_graph("[a]", "[b]", [], Logo(), Subtitles(file="/no/such/nothing.srt"), FRAME, [(0, 4.0)])

    def test_logo_positions(self):
        corners = {"top-left": ("20", "20"), "top-right": ("W-w-20", "20"), "bottom-left": ("20", "H-h-20"),
                   "bottom-right": ("W-w-20", "H-h-20"), "center": ("(W-w)/2", "(H-h)/2")}
        for corner, expected in corners.items():
            with self.subTest(corner=corner):
                self.assertEqual(captions.logo_position(Logo(corner=corner, margin=20 / W), FRAME), expected)
        x, y = captions.logo_position(Logo(corner="custom", x=0.25, y=0.75), FRAME)
        self.assertEqual((x, y), ("max(0,min(W-w,W*0.25-w/2))", "max(0,min(H-h,H*0.75-h/2))"))


class ClipGraphTests(unittest.TestCase):
    """The texts' times are on the finished video, so cuts and speed must not move them."""

    def graph(self, **edit_args):
        edit = VideoEdit(aspect=None, **edit_args)
        info = info_for(10.0)
        segments = edit.segments(info.duration, info.fps)
        return ";".join(clip_graph(0, edit, info, segments, audio=False).filters), edit

    def test_text_times_are_not_moved_by_cuts_or_speed(self):
        text = TextItem(text="x", start=1.0, end=2.0, fade_out=0.5)
        plain, _ = self.graph(texts=[text])
        cut, _ = self.graph(texts=[text], keep=[(0.0, 2.0), (5.0, 10.0)], speed=2.0)
        for graph in (plain, cut):
            self.assertIn("enable='between(t,1,2)'", graph)
            self.assertIn("(2-t)/0.5", graph)

    def test_a_fade_out_to_the_end_uses_the_length_after_cuts_and_speed(self):
        text = TextItem(text="x", start=0.5, fade_out=1.0)
        graph, edit = self.graph(texts=[text], keep=[(0.0, 2.0), (5.0, 9.0)], speed=2.0)     # 6 s kept, at 2x: 3 s
        self.assertIn("(3-t)/1", graph)

    def test_the_overlays_come_after_the_fades_and_the_look(self):
        graph, _ = self.graph(texts=[TextItem(text="x")], fade_in=1.0, look="bw")
        self.assertLess(graph.index("hue=s=0"), graph.index("drawtext="))
        self.assertLess(graph.index("fade=t=in"), graph.index("drawtext="))
        self.assertTrue(graph.endswith("[v]") or "[v]" in graph)

    def test_nothing_changes_without_overlays(self):
        graph, _ = self.graph()
        self.assertNotIn("drawtext", graph)
        self.assertNotIn("[ov0]", graph)
        self.assertTrue(graph.endswith("[v]"))

    def test_the_size_is_the_size_of_the_export_not_of_the_source(self):
        edit = VideoEdit(aspect=(9, 16), texts=[TextItem(text="x", size=0.1)], resolution=720)
        info = MediaInfo(duration=10.0, width=1920, height=1080, fps=30.0)
        graph = ";".join(clip_graph(0, edit, info, edit.segments(10.0, 30.0), audio=False).filters)
        self.assertIn("fontsize=128:", graph)         # 1280 high at 10 %

    def test_a_joined_clip_sizes_text_for_the_joined_frame(self):
        edit = VideoEdit(aspect=None, texts=[TextItem(text="x", size=0.1)])
        info = info_for(4.0)
        graph = ";".join(clip_graph(0, edit, info, edit.segments(4.0, 15.0), audio=False, frame=(640, 360)).filters)
        self.assertIn("fontsize=36:", graph)

    def test_audio_only_graphs_have_no_overlays(self):
        edit = VideoEdit(aspect=None, texts=[TextItem(text="x")])
        graph = ";".join(clip_graph(0, edit, MediaInfo(duration=4.0, has_audio=True), [(0.0, 4.0)], video=False).filters)
        self.assertNotIn("drawtext", graph)


class SettingsTests(unittest.TestCase):
    def round_trip(self, edit: VideoEdit) -> VideoEdit:
        return ClipSettings.from_dict(json.loads(json.dumps(ClipSettings(edit=edit).to_dict()))).edit

    def test_texts_logo_and_subtitles_round_trip(self):
        edit = VideoEdit(texts=[TextItem(text="a", end=3.0), TextItem(text="\u0645\u0631\u062d\u0628\u0627", x=0.1)],
                         logo=Logo(file="x.png", corner="custom", x=0.2), subtitles=Subtitles(file="s.srt"))
        self.assertEqual(self.round_trip(edit), edit)

    def test_junk_texts_are_ignored(self):
        for junk in ("text", 5, None, {"a": 1}, [None, 5, "x"], [[]]):
            with self.subTest(junk=junk):
                self.assertEqual(ClipSettings.from_dict({"edit": {"texts": junk}}).edit.texts, [])
        self.assertEqual(ClipSettings.from_dict({"edit": {"texts": [{}, 5]}}).edit.texts, [TextItem()])

    def test_bad_values_inside_a_text_fall_back_to_the_defaults(self):
        bad = {"text": 5, "font": "", "size": 99, "color": "red", "outline_color": "#12", "outline": -1, "box": "yes",
               "box_color": None, "box_opacity": 3, "bold": 1, "align": "diagonal", "x": 2, "y": "a", "start": -1,
               "end": "never", "fade_in": 1e6, "fade_out": float("nan"), "surprise": True}
        (loaded,) = load_texts([bad])
        self.assertEqual(loaded, TextItem())

    def test_an_end_before_the_start_means_to_the_end(self):
        (loaded,) = load_texts([{"start": 5, "end": 2}])
        self.assertEqual((loaded.start, loaded.end), (5.0, None))

    def test_numbers_are_floats_and_good_values_are_kept(self):
        (loaded,) = load_texts([{"size": 1 / 10, "x": 0, "y": 1, "start": 2, "end": 9, "text": "hi", "bold": True}])
        self.assertEqual((loaded.size, loaded.x, loaded.y, loaded.start, loaded.end, loaded.text, loaded.bold),
                         (0.1, 0.0, 1.0, 2.0, 9.0, "hi", True))
        self.assertIsInstance(loaded.x, float)
        self.assertIsInstance(loaded.end, float)

    def test_there_is_a_limit_on_how_many_texts_and_how_long(self):
        self.assertEqual(len(load_texts([{"text": "x"}] * 500)), captions.MAX_TEXTS)
        self.assertEqual(load_texts([{"text": "x" * (captions.MAX_TEXT_LENGTH + 1)}])[0].text, TextItem().text)

    def test_junk_logo_and_subtitles(self):
        for junk in ("x", 5, None, [1], {"file": 5, "size": "big", "opacity": 9, "corner": "up"}):
            with self.subTest(junk=junk):
                self.assertEqual(load_logo(junk), Logo())
                self.assertEqual(load_subtitles(junk), Subtitles())
        self.assertEqual(load_subtitles({"position": "middle", "size": 0.3}), Subtitles(position="middle"))
        self.assertEqual(load_subtitles({"position": "middle", "size": 0.1}).position, "middle")

    def test_junk_in_a_whole_settings_dict(self):
        loaded = ClipSettings.from_dict({"edit": {"texts": "x", "logo": [1], "subtitles": 5, "zoom": 2.0}}).edit
        self.assertEqual((loaded.texts, loaded.logo, loaded.subtitles, loaded.zoom), ([], Logo(), Subtitles(), 2.0))

    def test_old_settings_without_these_fields_still_load(self):
        loaded = ClipSettings.from_dict({"edit": {"zoom": 1.5}}).edit
        self.assertEqual((loaded.texts, loaded.logo, loaded.subtitles), ([], Logo(), Subtitles()))

    def test_loading_gives_each_settings_its_own_lists(self):
        a, b = ClipSettings(), ClipSettings()
        a.edit.texts.append(TextItem())
        self.assertEqual(b.edit.texts, [])
        self.assertIsNot(a.edit.logo, b.edit.logo)

    def test_the_summary_mentions_them(self):
        edit = VideoEdit(texts=[TextItem(text="hi")], logo=Logo(file="x.png"), subtitles=Subtitles(file="s.srt"))
        summary = describe(ClipSettings(edit=edit))
        for word in ("text", "logo", "subtitles"):
            self.assertIn(word, summary)
        self.assertNotIn("text", describe(ClipSettings(edit=VideoEdit(texts=[TextItem(text=" ")]))))


class ApplyToTests(unittest.TestCase):
    def setUp(self):
        self.source = ClipSettings(edit=VideoEdit(
            texts=[TextItem(text="source text")], logo=Logo(file="logo.png", size=0.3),
            subtitles=Subtitles(file="source.srt", size=0.08, position="top")))
        self.target = ClipSettings(edit=VideoEdit(texts=[TextItem(text="target text")],
                                                  subtitles=Subtitles(file="target.srt")))

    def test_the_logo_and_the_subtitle_style_are_copied_but_not_the_texts_or_the_subtitle_file(self):
        result = apply_to(self.target, self.source)
        self.assertEqual(result.edit.logo, Logo(file="logo.png", size=0.3))
        self.assertEqual(result.edit.subtitles, Subtitles(file="target.srt", size=0.08, position="top"))
        self.assertEqual([t.text for t in result.edit.texts], ["target text"])

    def test_the_result_shares_nothing_with_the_target(self):
        result = apply_to(self.target, self.source)
        result.edit.texts[0].text = "changed"
        self.assertEqual(self.target.edit.texts[0].text, "target text")
        self.assertEqual(self.source.edit.texts[0].text, "source text")

    def test_a_preset_leaves_the_texts_logo_and_subtitles_alone(self):
        from video_converter.project import PRESETS
        for values in PRESETS.values():
            result = apply_preset(self.source, values)
            self.assertEqual(result.edit.texts, self.source.edit.texts)
            self.assertEqual(result.edit.logo, self.source.edit.logo)
            self.assertEqual(result.edit.subtitles, self.source.edit.subtitles)

    def test_a_saved_preset_does_not_bring_its_texts(self):
        saved = ClipSettings.from_dict(self.source.to_dict())
        result = apply_to(self.target, saved)
        self.assertEqual([t.text for t in result.edit.texts], ["target text"])
        self.assertEqual(result.edit.logo.file, "logo.png")


class FontTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        for name in ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf", "foo.ttf", "bar.ttc", "old.fon",
                     "segoeui.ttf"):
            (self.folder / name).write_bytes(b"not a real font")
        entries = [("Arial (TrueType)", "arial.ttf"), ("Arial Bold (TrueType)", "arialbd.ttf"),
                   ("Arial Italic (TrueType)", "ariali.ttf"), ("Arial Bold Italic (TrueType)", "arialbi.ttf"),
                   ("Foo Display (TrueType)", "foo.ttf"), ("Bar Sans & Bar Serif (TrueType)", "bar.ttc"),
                   ("Old (Raster)", "old.fon"), ("Gone (TrueType)", "gone.ttf"), ("Segoe UI (TrueType)", "segoeui.ttf"),
                   ("Elsewhere (TrueType)", str(self.folder / "foo.ttf"))]
        self.patches = [mock.patch.object(fonts, "_registry_entries", return_value=entries),
                        mock.patch.object(fonts, "windows_fonts_dir", return_value=self.folder)]
        for patch in self.patches:
            patch.start()
        fonts.forget_installed_fonts()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        fonts.forget_installed_fonts()
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_a_family_finds_its_file(self):
        self.assertEqual(fonts.font_file("Arial"), str(self.folder / "arial.ttf"))
        self.assertEqual(fonts.font_file("arial"), str(self.folder / "arial.ttf"))
        self.assertEqual(fonts.font_file("Foo Display"), str(self.folder / "foo.ttf"))

    def test_styles(self):
        self.assertEqual(fonts.font_file("Arial", bold=True), str(self.folder / "arialbd.ttf"))
        self.assertEqual(fonts.font_file("Arial", italic=True), str(self.folder / "ariali.ttf"))
        self.assertEqual(fonts.font_file("Arial", bold=True, italic=True), str(self.folder / "arialbi.ttf"))
        self.assertEqual(fonts.font_file("Foo Display", bold=True), str(self.folder / "foo.ttf"))   # no bold: regular

    def test_unknown_and_unusable_fonts(self):
        for family in ("Nope", "", "Old", "Gone", "Bar Serif"):
            with self.subTest(family=family):
                self.assertIsNone(fonts.font_file(family))
        self.assertEqual(fonts.font_file("Bar Sans"), str(self.folder / "bar.ttc"))

    def test_a_font_in_another_folder_is_found_by_its_full_path(self):
        self.assertEqual(fonts.font_file("Elsewhere"), str(self.folder / "foo.ttf"))

    def test_an_unknown_family_falls_back_to_arial_then_segoe(self):
        self.assertEqual(fonts.pick_font("Nope", False, "hello"), str(self.folder / "arial.ttf"))
        self.assertEqual(fonts.pick_font("Nope", True, "hello"), str(self.folder / "arialbd.ttf"))

    def test_the_chosen_family_is_used_when_it_can_show_the_text(self):
        self.assertEqual(fonts.pick_font("Foo Display", False, "hello"), str(self.folder / "foo.ttf"))

    def test_a_font_that_lacks_a_glyph_gives_way_to_one_that_has_it(self):
        covers_arabic = {str(self.folder / "segoeui.ttf")}

        def load(path):
            font = fonts.Font(path, 1000, 800, -200, 0, [], [500], 1)
            # Every font has the Latin letters; only Segoe UI (here) has the Arabic ones; nothing has Chinese.
            font.has_char = lambda code, ok=path in covers_arabic: code < 0x600 or (ok and code < 0x700)  # type: ignore
            return font

        with mock.patch.object(fonts, "load_font", side_effect=load):
            self.assertEqual(fonts.pick_font("Arial", False, "abc \u0645\u0631"), str(self.folder / "segoeui.ttf"))
            self.assertEqual(fonts.pick_font("Arial", False, "abc"), str(self.folder / "arial.ttf"))
            # nothing has every glyph: the font with the most
            self.assertEqual(fonts.pick_font("Arial", False, "abc \u4e2d"), str(self.folder / "arial.ttf"))

    def test_no_installed_fonts_means_no_file(self):
        with mock.patch.object(fonts, "_registry_entries", return_value=[]), \
                mock.patch.object(fonts, "windows_fonts_dir", return_value=None):
            fonts.forget_installed_fonts()
            self.assertIsNone(fonts.pick_font("Arial", False, "x"))
            self.assertIn("font=Sans", text_filter(TextItem(text="x"), FRAME, None))

    def test_a_file_that_is_not_a_font_does_not_stop_anything(self):
        self.assertIsNone(fonts.load_font(str(self.folder / "arial.ttf")))
        self.assertIsNone(fonts.load_font(str(self.folder / "missing.ttf")))
        self.assertEqual(fonts.wrap_text("a b c", None, 20, 30), "a b c")
        w, h = fonts.text_size("ab\ncd", None, 10)
        self.assertGreater(w, 0)
        self.assertGreater(h, 10)


@unittest.skipUnless(WINDOWS, "needs the fonts that come with Windows")
class SystemFontTests(unittest.TestCase):
    def test_the_registry_lists_the_common_fonts(self):
        fonts.forget_installed_fonts()
        for family in ("Arial", "Segoe UI", "Tahoma"):
            with self.subTest(family=family):
                self.assertTrue(Path(fonts.font_file(family)).is_file())
        self.assertNotEqual(fonts.font_file("Arial", bold=True), fonts.font_file("Arial"))

    def test_measuring_a_real_font(self):
        font = fonts.load_font(fonts.font_file("Arial"))
        self.assertEqual(font.units_per_em, 2048)
        self.assertTrue(font.has_char(ord("A")))
        self.assertTrue(font.has_char(0x0645))                  # Arabic meem
        self.assertFalse(font.has_char(0xE000))                 # nothing lives in the private use area
        self.assertFalse(font.has_char(0x1F600))
        self.assertAlmostEqual(font.advance(ord("A")), 0.667, places=2)
        self.assertAlmostEqual(font.advance(ord(" ")), 0.278, places=2)
        self.assertAlmostEqual(font.line_height, 1.15, places=2)
        self.assertGreater(font.width("WWW", 10), font.width("iii", 10))

    def test_arabic_in_a_font_without_arabic_letters_uses_one_that_has_them(self):
        impact = fonts.font_file("Impact")
        if not impact:
            self.skipTest("Impact isn't installed")
        self.assertFalse(fonts.load_font(impact).has_char(0x0645))
        chosen = fonts.pick_font("Impact", False, "\u0645\u0631\u062d\u0628\u0627")
        self.assertNotEqual(chosen, impact)
        self.assertTrue(fonts.load_font(chosen).has_char(0x0645))
        self.assertEqual(fonts.pick_font("Impact", False, "Hello"), impact)

    def test_a_collection_file_is_read_too(self):
        path = fonts.font_file("Microsoft YaHei")
        if not path:
            self.skipTest("Microsoft YaHei isn't installed")
        self.assertTrue(path.lower().endswith(".ttc"))
        self.assertTrue(fonts.load_font(path).has_char(0x4E2D))
        self.assertEqual(fonts.pick_font("Arial", False, "\u4e2d\u6587"), path)


class SubtitleParsingTests(unittest.TestCase):
    SRT = ("\ufeff1\r\n00:00:01,000 --> 00:00:03,500\r\nHello\r\nthere\r\n\r\n"
           "2\r\n00:01:00,250 --> 00:01:02,000\r\n<i>Second</i>\r\n\r\n"
           "garbage block without a time\r\n\r\n"
           "3\r\n01:00:00.5 --> 01:00:01.75\r\nDotted times\r\n\r\n"
           "4\r\n00:00:05,000 --> 00:00:06,000\r\n\r\n")

    def test_srt(self):
        cues = parse_cues(self.SRT)
        # A blank line only ends a cue when a new cue (its number or its time) comes next, so the stray block
        # that follows the second cue is read as more of its text rather than dropped.
        self.assertEqual([(c.start, c.end, c.text) for c in cues],
                         [(1.0, 3.5, "Hello\nthere"), (60.25, 62.0, "<i>Second</i>\n\ngarbage block without a time"),
                          (3600.5, 3601.75, "Dotted times")])

    def test_webvtt(self):
        text = ("WEBVTT\n\nNOTE a comment\n\nintro\n00:01.000 --> 00:02.500 align:start position:0%\n"
                "<v Roger>Hello <c.yellow>there</c></v> <b>bold</b>\n\n00:03.000 --> 00:04.000\n<00:03.500>Late\n")
        cues = parse_cues(text, "vtt")
        self.assertEqual([(c.start, c.end) for c in cues], [(1.0, 2.5), (3.0, 4.0)])
        self.assertEqual(cues[0].text, "Hello there <b>bold</b>")
        self.assertEqual(cues[1].text, "Late")

    def test_kinds(self):
        self.assertEqual(subtitle_kind(Path("a.srt"), "1\n"), "srt")
        self.assertEqual(subtitle_kind(Path("a.vtt"), ""), "vtt")
        self.assertEqual(subtitle_kind(Path("a.txt"), "WEBVTT\n"), "vtt")
        self.assertEqual(subtitle_kind(Path("a.ass"), ""), "ass")
        self.assertEqual(subtitle_kind(Path("a.txt"), "[Script Info]\nTitle: x"), "ass")

    def test_reading_files_in_other_encodings(self):
        folder = Path(tempfile.mkdtemp())
        try:
            (folder / "u8.srt").write_bytes("\ufeffcaf\u00e9 \u0645".encode("utf-8"))
            (folder / "u16.srt").write_bytes("caf\u00e9 \u0645".encode("utf-16"))
            (folder / "old.srt").write_bytes("caf\u00e9".encode("cp1252"))
            self.assertEqual(read_text(folder / "u8.srt"), "caf\u00e9 \u0645")
            self.assertEqual(read_text(folder / "u16.srt"), "caf\u00e9 \u0645")
            self.assertEqual(read_text(folder / "old.srt"), "caf\u00e9")
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class SubtitleTimingTests(unittest.TestCase):
    CUTS = [(0.0, 2.0), (4.0, 6.0)]            # 2 s to 4 s is cut out

    def test_a_span_inside_one_part(self):
        self.assertEqual(map_span(0.5, 1.5, self.CUTS), [(0.5, 1.5)])
        self.assertEqual(map_span(4.5, 5.5, self.CUTS), [(2.5, 3.5)])

    def test_a_span_that_was_cut_out_is_gone(self):
        self.assertEqual(map_span(2.2, 3.8, self.CUTS), [])

    def test_a_span_across_a_cut_shows_before_and_after_it(self):
        self.assertEqual(map_span(1.0, 5.0, self.CUTS), [(1.0, 3.0)])         # the two pieces meet on the export
        self.assertEqual(map_span(1.0, 5.0, [(0.0, 2.0), (4.0, 6.0), (8.0, 9.0)]), [(1.0, 3.0)])
        gap = [(0.0, 2.0), (3.0, 4.0)]
        self.assertEqual(map_span(1.0, 3.5, gap), [(1.0, 2.5)])

    def test_speed_shortens_the_times(self):
        self.assertEqual(map_span(0.0, 2.0, self.CUTS, speed=2.0), [(0.0, 1.0)])
        self.assertEqual(map_span(4.0, 6.0, self.CUTS, speed=2.0), [(1.0, 2.0)])
        self.assertEqual(map_span(0.0, 1.0, self.CUTS, speed=0.5), [(0.0, 2.0)])

    def test_reverse_mirrors_the_times(self):
        self.assertEqual(map_span(0.0, 1.0, self.CUTS, reverse=True), [(3.0, 4.0)])
        self.assertEqual(map_span(5.0, 6.0, self.CUTS, reverse=True), [(0.0, 1.0)])

    def test_speed_and_reverse_together(self):
        self.assertEqual(map_span(0.0, 1.0, self.CUTS, speed=2.0, reverse=True), [(1.5, 2.0)])

    def test_the_trim_is_just_another_segment(self):
        self.assertEqual(map_span(0.0, 10.0, [(3.0, 5.0)]), [(0.0, 2.0)])
        self.assertEqual(map_span(0.0, 2.0, [(3.0, 5.0)]), [])

    def test_an_open_ended_part(self):
        self.assertEqual(map_span(1.0, 2.0, [(0.5, None)]), [(0.5, 1.5)])

    def test_tiny_pieces_are_dropped(self):
        self.assertEqual(map_span(1.99, 4.001, self.CUTS), [])

    def test_inverse_mapping_of_a_time_on_the_export(self):
        segments = [(1.0, 3.0), (5.0, 9.0)]
        for speed in (1.0, 2.0, 0.5):
            for reverse in (False, True):
                for source in (1.2, 2.9, 5.1, 8.8):
                    with self.subTest(speed=speed, reverse=reverse, source=source):
                        out = source_to_output(source, segments, speed, reverse)
                        self.assertAlmostEqual(output_to_source(out, segments, speed, reverse), source, places=4)

    def test_where_a_text_falls_on_the_source_timeline(self):
        segments = [(0.0, 2.0), (4.0, 6.0)]
        self.assertEqual(output_spans_to_source(0.5, 1.5, segments), [(0.5, 1.5)])
        self.assertEqual(output_spans_to_source(1.0, 3.0, segments), [(1.0, 2.0), (4.0, 5.0)])
        self.assertEqual(output_spans_to_source(1.0, None, segments), [(1.0, 2.0), (4.0, 6.0)])
        self.assertEqual(output_spans_to_source(0.5, 1.5, segments, speed=2.0), [(1.0, 2.0), (4.0, 5.0)])
        self.assertEqual(output_spans_to_source(0.0, 1.0, segments, reverse=True), [(5.0, 6.0)])
        self.assertEqual(output_spans_to_source(9.0, 10.0, segments), [])

    def test_a_text_across_the_cut_point_is_two_parts_of_the_source(self):
        spans = output_spans_to_source(1.5, 2.5, [(0.0, 2.0), (5.0, 8.0)])
        self.assertEqual(spans, [(1.5, 2.0), (5.0, 5.5)])


class SubtitleFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)

    def write(self, name, text, encoding="utf-8"):
        path = self.folder / name
        path.write_text(text, encoding=encoding)
        return path

    def test_an_srt_is_rewritten_for_the_export(self):
        path = self.write("a.srt", "1\n00:00:01,000 --> 00:00:02,000\ncut off\n\n2\n00:00:05,000 --> 00:00:06,000\n"
                                   "stays \u0645\u0631\u062d\u0628\u0627\n")
        new, kind, height = retimed_subtitles(Subtitles(file=str(path)), [(0.0, 0.5), (3.0, 10.0)])
        self.assertEqual((kind, height), ("srt", 288))
        self.assertEqual(new.read_text(encoding="utf-8"),
                         "1\n00:00:02,500 --> 00:00:03,500\nstays \u0645\u0631\u062d\u0628\u0627\n")

    def test_every_cue_cut_out_leaves_a_harmless_placeholder(self):
        path = self.write("a.srt", "1\n00:00:01,000 --> 00:00:02,000\nx\n")
        new, _, _ = retimed_subtitles(Subtitles(file=str(path)), [(5.0, 9.0)])
        self.assertIn("-->", new.read_text(encoding="utf-8"))

    def test_a_cue_across_a_cut_keeps_its_text_for_both_pieces_when_they_do_not_meet(self):
        path = self.write("a.srt", "1\n00:00:01,000 --> 00:00:05,000\nlong\n")
        new, _, _ = retimed_subtitles(Subtitles(file=str(path)), [(0.0, 2.0), (4.0, 6.0)], speed=1.0)
        self.assertIn("00:00:01,000 --> 00:00:03,000", new.read_text(encoding="utf-8"))

    def test_the_same_content_is_written_once(self):
        path = self.write("a.srt", "1\n00:00:01,000 --> 00:00:02,000\nx\n")
        a, _, _ = retimed_subtitles(Subtitles(file=str(path)), [(0.0, 9.0)])
        b, _, _ = retimed_subtitles(Subtitles(file=str(path)), [(0.0, 9.0)])
        self.assertEqual(a, b)

    def test_a_missing_file_is_a_clear_error(self):
        with self.assertRaisesRegex(ConversionError, "gone.srt"):
            retimed_subtitles(Subtitles(file=str(self.folder / "gone.srt")), [(0.0, 4.0)])

    ASS = ("[Script Info]\nTitle: t\nPlayResY: 480\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
           "Style: Default,Arial,20\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
           "Effect, Text\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,cut, with comma\n"
           "Dialogue: 0,0:00:05.00,0:00:06.50,Default,,0,0,0,,kept {\\i1}text{\\i0}\nComment: 0,0:00:05.00,0:00:06.00,"
           "Default,,0,0,0,,not shown\n")

    def test_ass_keeps_its_styles_and_moves_the_dialogue(self):
        path = self.write("a.ass", self.ASS)
        new, kind, height = retimed_subtitles(Subtitles(file=str(path)), [(0.0, 0.5), (3.0, 10.0)])
        self.assertEqual((kind, height), ("ass", 480))
        text = new.read_text(encoding="utf-8")
        self.assertIn("Style: Default,Arial,20", text)
        self.assertIn("PlayResY: 480", text)
        self.assertIn("Dialogue: 0,0:00:02.50,0:00:04.00,Default,,0,0,0,,kept {\\i1}text{\\i0}", text)
        self.assertNotIn("cut, with comma", text)
        self.assertIn("Comment:", text)

    def test_ass_lines_split_at_a_cut(self):
        text, left = retime_ass(self.ASS, lambda a, b: [(a / 2, b / 2)])
        self.assertEqual(left, 2)
        self.assertIn("0:00:00.50,0:00:01.00", text)

    def test_force_style_uses_the_script_size_and_colors(self):
        path = self.write("a.srt", "1\n00:00:01,000 --> 00:00:02,000\nx\n")
        vf = captions.subtitle_filter(Subtitles(file=str(path), size=0.1, color="#ff8000", outline_color="#0000ff",
                                                outline=0.1, bold=True, position="top", font="Tahoma"),
                                      [(0.0, 9.0)])
        for part in ("FontName=Tahoma", "FontSize=28.8", "PrimaryColour=&H000080FF&", "OutlineColour=&H00FF0000&",
                     "Outline=2.88", "Bold=-1", "Alignment=6"):
            self.assertIn(part, vf.replace("\\,", ","))
        self.assertIn("Alignment=2", captions.subtitle_filter(Subtitles(file=str(path)), [(0.0, 9.0)]).replace("\\,", ","))
        self.assertIn("Alignment=10", captions.subtitle_filter(Subtitles(file=str(path), position="middle"),
                                                              [(0.0, 9.0)]).replace("\\,", ","))


class RealExportTests(unittest.TestCase):
    """Real ffmpeg on a tiny black video: text is white, so where the bright pixels are says where the text is."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.src = cls.tmp / "black.mp4"
        make_black(cls.src)
        cls.info = probe_media(FFMPEG, cls.src)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def export(self, name: str, **edit_args) -> Path:
        edit_args.setdefault("crf", 18)
        edit = VideoEdit(aspect=None, use_gpu=False, **edit_args)
        out = self.tmp / name
        convert_video(FFMPEG, VideoJob(self.src, out, edit), info=self.info)
        return out

    def test_text_shows_only_in_its_time_window(self):
        out = self.export("window.mp4", texts=[TextItem(text="WWWW", size=0.3, outline=0, start=1.0, end=2.0)])
        self.assertEqual(bright(grab(out, 0.4)), 0)
        self.assertGreater(bright(grab(out, 1.4)), 300)
        self.assertEqual(bright(grab(out, 2.5)), 0)

    def test_the_window_is_on_the_finished_video_after_cuts_and_speed(self):
        # 4 s, 1..2 cut out = 3 s, at 2x = 1.5 s. The text is on from 0.5 to 1.0 of the result.
        out = self.export("cuts.mp4", keep=[(0.0, 1.0), (2.0, 4.0)], speed=2.0,
                          texts=[TextItem(text="WWWW", size=0.3, outline=0, start=0.5, end=1.0)])
        self.assertEqual(bright(grab(out, 0.2)), 0)
        self.assertGreater(bright(grab(out, 0.7)), 300)
        self.assertEqual(bright(grab(out, 1.2)), 0)

    def test_the_position_is_where_the_middle_of_the_text_sits(self):
        out = self.export("position.mp4", texts=[TextItem(text="WW", size=0.12, outline=0, x=0.25, y=0.25)])
        cx, cy = centroid(grab(out, 0.5))
        self.assertAlmostEqual(cx, 0.25 * W, delta=8)
        self.assertAlmostEqual(cy, 0.25 * H, delta=8)
        out = self.export("position2.mp4", texts=[TextItem(text="WW", size=0.12, outline=0, x=0.8, y=0.9)])
        cx, cy = centroid(grab(out, 0.5))
        self.assertAlmostEqual(cx, 0.8 * W, delta=8)
        self.assertAlmostEqual(cy, 0.9 * H, delta=10)

    def test_text_dragged_past_the_edge_stays_inside_the_frame(self):
        out = self.export("edge.mp4", texts=[TextItem(text="WWWW", size=0.2, outline=0, x=1.0, y=1.0)])
        frame = grab(out, 0.5)
        self.assertGreater(bright(frame), 100)
        self.assertEqual(bright(frame, box=(0, 0, W // 2, H // 2)), 0)
        self.assertGreater(bright(frame, box=(W - 60, H - 40, W, H)), 100)

    def test_the_size_is_a_share_of_the_height(self):
        small = bright(grab(self.export("small.mp4", texts=[TextItem(text="W", size=0.1, outline=0)]), 0.5))
        large = bright(grab(self.export("large.mp4", texts=[TextItem(text="W", size=0.2, outline=0)]), 0.5))
        self.assertGreater(large, small * 3)       # twice as tall and wide is about four times the pixels

    def test_colors_outline_and_box(self):
        out = self.export("colors.mp4", texts=[TextItem(text="WWW", size=0.3, color="#ff0000", outline=0.15,
                                                        outline_color="#00ff00", y=0.5)])
        frame = grab(out, 0.5, "rgb24")
        reds = sum(1 for i in range(0, len(frame), 3) if frame[i] > 180 and frame[i + 1] < 80 and frame[i + 2] < 80)
        greens = sum(1 for i in range(0, len(frame), 3) if frame[i + 1] > 150 and frame[i] < 80 and frame[i + 2] < 80)
        self.assertGreater(reds, 200)
        self.assertGreater(greens, 100)
        boxed = self.export("box.mp4", texts=[TextItem(text="W", size=0.3, color="#000000", outline=0, box=True,
                                                       box_color="#ffffff", box_opacity=1.0)])
        self.assertGreater(bright(grab(boxed, 0.5)), 1500)           # the box is bright though the text is black

    def test_fades_in_and_out(self):
        out = self.export("fade.mp4", texts=[TextItem(text="WWWW", size=0.3, outline=0, start=0.0, end=3.0,
                                                      fade_in=1.0, fade_out=1.0)])
        def level(t):
            return max(grab(out, t))
        self.assertLess(level(0.1), 90)
        self.assertGreater(level(0.5), 90)
        self.assertLess(level(0.5), 200)
        self.assertGreater(level(1.5), 230)
        self.assertLess(level(2.9), 110)

    def test_a_text_without_an_end_fades_out_at_the_end_of_the_video(self):
        out = self.export("fadeend.mp4", texts=[TextItem(text="WWWW", size=0.3, outline=0, fade_out=1.0)])
        self.assertGreater(max(grab(out, 1.5)), 230)
        self.assertLess(max(grab(out, 3.8)), 120)

    def test_several_texts_at_once(self):
        out = self.export("several.mp4", texts=[TextItem(text="WW", size=0.1, outline=0, y=0.2),
                                                TextItem(text="WW", size=0.1, outline=0, y=0.8)])
        frame = grab(out, 0.5)
        self.assertGreater(bright(frame, box=(0, 0, W, H // 2)), 100)
        self.assertGreater(bright(frame, box=(0, H // 2, W, H)), 100)

    def test_text_goes_over_the_fade_to_black_and_is_not_tinted_by_a_look(self):
        out = self.export("over.mp4", texts=[TextItem(text="WWWW", size=0.3, outline=0, color="#ff0000")],
                          fade_in=2.0, look="bw")
        frame = grab(out, 0.1, "rgb24")
        reds = sum(1 for i in range(0, len(frame), 3) if frame[i] > 180 and frame[i + 1] < 80)
        self.assertGreater(reds, 300)          # a black and white look would have made it grey

    def test_a_logo_sits_in_its_corner_at_the_chosen_size_and_opacity(self):
        logo = self.tmp / "red.png"
        write_png(logo, 40, 40, lambda x, y: (255, 0, 0, 255))
        out = self.export("logo.mp4", logo=Logo(file=str(logo), corner="top-left", size=0.25, margin=0.05, opacity=0.5))
        frame = grab(out, 0.5, "rgb24")
        left, size = round(0.05 * W), round(0.25 * W)               # 16 px in, 80 px wide
        r, g, b = rgb_at(frame, left + size // 2, left + size // 2)
        self.assertTrue(110 <= r <= 150, r)                         # half of full red over black
        self.assertLess(g, 30)
        self.assertLess(rgb_at(frame, left - 6, left + 10)[0], 20)
        self.assertLess(rgb_at(frame, left + size + 6, left + 10)[0], 20)
        self.assertLess(rgb_at(frame, left + 10, left + size + 6)[0], 20)

    def test_every_corner_and_the_center(self):
        logo = self.tmp / "red2.png"
        write_png(logo, 20, 20, lambda x, y: (255, 0, 0, 255))
        size, margin = 40, 10                               # 0.125 and 0.03125 of the width
        spots = {"top-left": (margin, margin), "top-right": (W - size - margin, margin),
                 "bottom-left": (margin, H - size - margin), "bottom-right": (W - size - margin, H - size - margin),
                 "center": ((W - size) // 2, (H - size) // 2)}
        for corner, (x, y) in spots.items():
            with self.subTest(corner=corner):
                out = self.export(f"corner-{corner}.mp4", logo=Logo(file=str(logo), corner=corner, size=size / W,
                                                                    margin=margin / W))
                frame = grab(out, 0.5, "rgb24")
                self.assertGreater(rgb_at(frame, x + size // 2, y + size // 2)[0], 220)
                self.assertLess(rgb_at(frame, x - 4 if x > 5 else x + size + 4, y + size // 2)[0], 30)

    def test_a_logo_dragged_to_a_custom_place(self):
        logo = self.tmp / "red3.png"
        write_png(logo, 20, 20, lambda x, y: (255, 0, 0, 255))
        out = self.export("custom.mp4", logo=Logo(file=str(logo), corner="custom", x=0.25, y=0.75, size=40 / W))
        frame = grab(out, 0.5, "rgb24")
        self.assertGreater(rgb_at(frame, round(0.25 * W), round(0.75 * H))[0], 220)
        self.assertLess(rgb_at(frame, round(0.75 * W), round(0.25 * H))[0], 30)

    def test_a_logos_see_through_parts_stay_see_through(self):
        logo = self.tmp / "half.png"
        write_png(logo, 40, 40, lambda x, y: (0, 255, 0, 255) if x < 20 else (0, 255, 0, 0))
        out = self.export("half.mp4", logo=Logo(file=str(logo), corner="top-left", size=0.25, margin=0.0))
        frame = grab(out, 0.5, "rgb24")
        self.assertGreater(rgb_at(frame, 20, 40)[1], 200)           # the left half is green
        self.assertLess(rgb_at(frame, 60, 40)[1], 30)               # the right half lets the black through

    def test_the_logo_shows_for_the_whole_video(self):
        logo = self.tmp / "red4.png"
        write_png(logo, 20, 20, lambda x, y: (255, 0, 0, 255))
        out = self.export("whole.mp4", logo=Logo(file=str(logo), corner="center", size=0.2))
        for t in (0.1, 2.0, 3.8):
            with self.subTest(t=t):
                self.assertGreater(rgb_at(grab(out, t, "rgb24"), W // 2, H // 2)[0], 200)

    def test_subtitles_appear_at_the_right_times_after_a_cut(self):
        srt = self.tmp / "talk.srt"
        # Source times: 1-2 s is cut out below; the cue at 3-3.5 s lands on 2-2.5 s of the result.
        srt.write_text("1\n00:00:00,100 --> 00:00:00,500\nWWWWWW\n\n2\n00:00:01,200 --> 00:00:01,800\nWWWWWW\n\n"
                       "3\n00:00:03,000 --> 00:00:03,500\nWWWWWW\n", encoding="utf-8")
        out = self.export("subs.mp4", keep=[(0.0, 1.0), (2.0, 4.0)], subtitles=Subtitles(file=str(srt), size=0.12))
        area = (0, int(H * 0.6), W, H)
        self.assertGreater(bright(grab(out, 0.3), box=area), 100)          # the first cue
        self.assertEqual(bright(grab(out, 0.8), box=area), 0)
        self.assertEqual(bright(grab(out, 1.2), box=area), 0)              # the second cue was cut out
        self.assertEqual(bright(grab(out, 1.7), box=area), 0)
        self.assertGreater(bright(grab(out, 2.2), box=area), 100)          # the third, moved up by the cut
        self.assertEqual(bright(grab(out, 2.7), box=area), 0)
        self.assertEqual(bright(grab(out, 2.2), box=(0, 0, W, int(H * 0.5))), 0)

    def test_subtitles_follow_a_speed_change(self):
        srt = self.tmp / "fast.srt"
        srt.write_text("1\n00:00:02,000 --> 00:00:03,000\nWWWWWW\n", encoding="utf-8")
        out = self.export("subsfast.mp4", speed=2.0, subtitles=Subtitles(file=str(srt), size=0.12))
        area = (0, int(H * 0.6), W, H)
        self.assertEqual(bright(grab(out, 0.5), box=area), 0)
        self.assertGreater(bright(grab(out, 1.2), box=area), 100)      # 2-3 s of the source is 1-1.5 s at double speed
        self.assertEqual(bright(grab(out, 1.8), box=area), 0)

    def test_subtitle_position_and_style(self):
        srt = self.tmp / "pos.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:09,000\nWWWWWW\n", encoding="utf-8")
        top = grab(self.export("subtop.mp4", subtitles=Subtitles(file=str(srt), size=0.12, position="top")), 0.5)
        self.assertGreater(bright(top, box=(0, 0, W, H // 3)), 100)
        self.assertEqual(bright(top, box=(0, H // 2, W, H)), 0)
        middle = grab(self.export("submid.mp4", subtitles=Subtitles(file=str(srt), size=0.12, position="middle")), 0.5)
        self.assertGreater(bright(middle, box=(0, H // 3, W, 2 * H // 3)), 100)
        yellow = grab(self.export("subcolor.mp4", subtitles=Subtitles(file=str(srt), size=0.12, color="#ffff00")),
                      0.5, "rgb24")
        hits = sum(1 for i in range(0, len(yellow), 3) if yellow[i] > 200 and yellow[i + 1] > 200 and yellow[i + 2] < 80)
        self.assertGreater(hits, 100)

    def test_arabic_subtitles_render_in_the_export(self):
        srt = self.tmp / "ar.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:09,000\n\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645\n",
                       encoding="utf-8")
        frame = grab(self.export("subar.mp4", subtitles=Subtitles(file=str(srt), size=0.12)), 0.5)
        self.assertGreater(bright(frame, box=(0, int(H * 0.6), W, H)), 100)

    def test_vtt_and_ass_files_work_too(self):
        vtt = self.tmp / "a.vtt"
        vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nWWWWWW\n", encoding="utf-8")
        ass = self.tmp / "a.ass"
        ass.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 384\nPlayResY: 288\n\n[V4+ Styles]\n"
                       "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, BorderStyle, Outline, "
                       "Shadow, Alignment, MarginL, MarginR, MarginV\nStyle: Default,Arial,30,&H00FFFFFF,&H00000000,"
                       "0,1,1,0,2,10,10,10\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
                       "MarginV, Effect, Text\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,WWWWWW\n", encoding="utf-8")
        area = (0, int(H * 0.6), W, H)
        for path in (vtt, ass):
            with self.subTest(file=path.name):
                out = self.export(f"sub-{path.name}.mp4", subtitles=Subtitles(file=str(path), size=0.12))
                self.assertEqual(bright(grab(out, 0.5), box=area), 0)
                self.assertGreater(bright(grab(out, 1.5), box=area), 100)

    def test_a_missing_subtitle_or_logo_file_fails_before_ffmpeg_starts(self):
        for edit in (VideoEdit(aspect=None, subtitles=Subtitles(file=str(self.tmp / "none.srt"))),
                     VideoEdit(aspect=None, logo=Logo(file=str(self.tmp / "none.png")))):
            with self.subTest(edit=edit):
                with self.assertRaises(ConversionError):
                    convert_video(FFMPEG, VideoJob(self.src, self.tmp / "never.mp4", edit), info=self.info)
        self.assertFalse((self.tmp / "never.mp4").exists())

    def test_the_command_for_the_hardware_encoder_has_the_same_picture_filters(self):
        edit = VideoEdit(aspect=None, texts=[TextItem(text="hi")])
        job = VideoJob(self.src, self.tmp / "x.mp4", edit)
        plain = video_command(FFMPEG, job, self.info)
        gpu = video_command(FFMPEG, job, self.info, encoder="h264_nvenc")
        self.assertEqual(plain[plain.index("-filter_complex") + 1], gpu[gpu.index("-filter_complex") + 1])


class ArabicTests(unittest.TestCase):
    AR = "\u0645\u0631\u062d\u0628\u0627"

    def item(self, text, **kw):
        return TextItem(text=text, size=0.3, outline=0, **kw)

    @unittest.skipUnless(WINDOWS, "needs Arial")
    def test_arabic_is_drawn_joined_not_as_boxes_or_separate_letters(self):
        vf = text_filter(self.item(self.AR), FRAME, None)
        shaped = render(vf)
        unshaped = render(vf.replace("text_shaping=1", "text_shaping=0"))
        tofu = render(text_filter(self.item("\ue000" * 5), FRAME, None))
        self.assertGreater(bright(shaped), 300)
        self.assertNotEqual(shaped, unshaped, "the letters were not joined")
        self.assertNotEqual(shaped, tofu, "the letters came out as boxes")
        self.assertNotEqual(unshaped, tofu)
        # Joined letters flow into each other, so the shaped word is narrower than the loose letters.
        def width(frame):
            xs = [i % W for i, v in enumerate(frame) if v > 128]
            return max(xs) - min(xs)
        self.assertLess(width(shaped), width(unshaped))

    @unittest.skipUnless(WINDOWS, "needs Arial")
    def test_arabic_runs_right_to_left(self):
        # With a right-to-left start, "Arabic ABC" is shown as ABC on the left of the Arabic. So it looks
        # the same as "ABC Arabic" written the other way round, which only works if the order is right.
        a = render(text_filter(self.item(self.AR + " ABC"), FRAME, None))
        b = render(text_filter(self.item("ABC " + self.AR), FRAME, None))
        different = sum(1 for p, q in zip(a, b) if abs(p - q) > 60)
        self.assertLess(different, 0.01 * len(a))
        vf = text_filter(self.item(self.AR + " ABC"), FRAME, None)
        loose = render(vf.replace("text_shaping=1", "text_shaping=0"))
        other = render(text_filter(self.item("ABC " + self.AR), FRAME, None).replace("text_shaping=1", "text_shaping=0"))
        self.assertGreater(sum(1 for p, q in zip(loose, other) if abs(p - q) > 60), 0.01 * len(a))

    def test_lines_that_start_in_a_right_to_left_script_are_marked(self):
        mark = captions.mark_right_to_left
        start, end = captions.RTL_START, captions.RTL_END
        self.assertEqual(mark("Hello"), "Hello")
        self.assertEqual(mark(self.AR), start + self.AR + end)
        self.assertEqual(mark("12 " + self.AR + " abc"), start + "12 " + self.AR + " abc" + end)     # digits are not strong
        self.assertEqual(mark("abc " + self.AR), "abc " + self.AR)
        self.assertEqual(mark("שלום"), start + "שלום" + end)
        self.assertEqual(mark(self.AR + "\nabc\n" + self.AR),
                         f"{start}{self.AR}{end}\nabc\n{start}{self.AR}{end}")
        self.assertEqual(mark("123 !?"), "123 !?")
        self.assertEqual(mark("<i>" + self.AR + "</i>"), start + "<i>" + self.AR + "</i>" + end)    # tags are not letters

    def test_the_marks_reach_the_rewritten_subtitle_file(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "a.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\n" + self.AR + " ABC\nplain\n", encoding="utf-8")
        new, _, _ = retimed_subtitles(Subtitles(file=str(path)), [(0.0, 4.0)])
        body = new.read_text(encoding="utf-8").split("\n")[2:4]
        self.assertEqual(body, [captions.RTL_START + self.AR + " ABC" + captions.RTL_END, "plain"])

    @unittest.skipUnless(WINDOWS, "needs Arial")
    def test_arabic_subtitles_also_run_right_to_left(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)

        def subtitle_frame(text):
            path = folder / f"{abs(hash(text))}.srt"
            path.write_text(f"1\n00:00:00,000 --> 00:00:09,000\n{text}\n", encoding="utf-8")
            return render(captions.subtitle_filter(Subtitles(file=str(path), size=0.2), [(0.0, 9.0)]))

        a, b = subtitle_frame(self.AR + " ABC"), subtitle_frame("ABC " + self.AR)
        self.assertGreater(bright(a), 200)
        self.assertLess(sum(1 for p, q in zip(a, b) if abs(p - q) > 60), 0.01 * len(a))

    @unittest.skipUnless(WINDOWS, "needs Arial")
    def test_a_font_without_arabic_still_shows_arabic(self):
        if not fonts.font_file("Impact"):
            self.skipTest("Impact isn't installed")
        with_impact = render(text_filter(self.item(self.AR, font="Impact"), FRAME, None))
        with_arial = render(text_filter(self.item(self.AR, font="Arial"), FRAME, None))
        self.assertEqual(with_impact, with_arial)
        self.assertGreater(bright(with_impact), 300)

    @unittest.skipUnless(WINDOWS, "needs Arial")
    def test_hebrew_and_other_scripts_draw_something(self):
        for text in ("\u05e9\u05dc\u05d5\u05dd", "\u4e2d\u6587", "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35", "\u0939\u093f\u0928\u094d\u0926\u0940"):
            with self.subTest(text=text):
                self.assertGreater(bright(render(text_filter(self.item(text), FRAME, None))), 100)

    @unittest.skipUnless(WINDOWS, "needs Arial")
    def test_bold_is_a_different_picture(self):
        regular = render(text_filter(self.item("WWW"), FRAME, None))
        bold = render(text_filter(self.item("WWW", bold=True), FRAME, None))
        self.assertGreater(bright(bold), bright(regular))


class PreviewMatchesExportTests(unittest.TestCase):
    """The Before / after picture goes through the same filters as the export."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.src = cls.tmp / "gray.mp4"
        code, error = run_ffmpeg("-f", "lavfi", "-i", f"color=c=0x303030:s={W}x{H}:d=4:r=15", "-c:v", "libx264",
                                 "-pix_fmt", "yuv420p", str(cls.src))
        assert code == 0, error
        cls.info = probe_media(FFMPEG, cls.src)
        cls.logo = cls.tmp / "logo.png"
        write_png(cls.logo, 30, 30, lambda x, y: (255, 80, 0, 255) if (x // 5 + y // 5) % 2 else (0, 120, 255, 255))
        cls.srt = cls.tmp / "p.srt"
        cls.srt.write_text("1\n00:00:00,000 --> 00:00:09,000\nPreview subtitle\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def mean_difference(self, edit: VideoEdit, source_time: float, output_time: float) -> float:
        out = self.tmp / "export.mp4"
        convert_video(FFMPEG, VideoJob(self.src, out, edit), info=self.info)
        png = self.tmp / "preview.png"
        geometry = plan_geometry(self.info.width, self.info.height, edit)
        render_frame(FFMPEG, self.src, png, source_time, (geometry.out_width, geometry.out_height), edit, self.info)
        shown = run_ffmpeg("-i", str(png), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-", binary=True).stdout
        exported = grab(out, output_time, "rgb24")
        self.assertEqual(len(shown), len(exported))
        return sum(abs(a - b) for a, b in zip(shown, exported)) / len(shown)

    def test_text_logo_and_subtitles_match_within_compression_noise(self):
        edit = VideoEdit(aspect=None, use_gpu=False, crf=12,
                         texts=[TextItem(text="Hello preview", size=0.12, start=0.5, end=3.0, fade_in=0.4, y=0.3),
                                TextItem(text="\u0645\u0631\u062d\u0628\u0627", size=0.12, y=0.55, box=True)],
                         logo=Logo(file=str(self.logo), corner="top-right", opacity=0.8),
                         subtitles=Subtitles(file=str(self.srt), size=0.07))
        self.assertLess(self.mean_difference(edit, 1.5, 1.5), 3.0)

    def test_the_match_holds_after_cuts_and_speed(self):
        edit = VideoEdit(aspect=None, use_gpu=False, crf=12, keep=[(0.0, 1.0), (2.0, 4.0)], speed=2.0,
                         texts=[TextItem(text="After the cut", size=0.12, start=0.5, end=1.4, y=0.4)],
                         subtitles=Subtitles(file=str(self.srt)))
        # source 3.0 s is the 2.0 s + 1.0 s into the second part: 1 s of the result + 0.5 s = 1.0 s
        self.assertEqual(round(source_to_output(3.0, edit.segments(4.0), 2.0), 3), 1.0)
        self.assertLess(self.mean_difference(edit, 3.0, 1.0), 3.0)

    def test_a_cut_out_frame_gets_only_the_logo(self):
        edit = VideoEdit(aspect=None, keep=[(0.0, 1.0), (2.0, 4.0)],
                         texts=[TextItem(text="WWWWWW", size=0.3, outline=0)],
                         logo=Logo(file=str(self.logo), corner="center", size=0.2))
        png = self.tmp / "cut.png"
        render_frame(FFMPEG, self.src, png, 1.5, (W, H), edit, self.info)
        frame = run_ffmpeg("-i", str(png), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-", binary=True).stdout
        self.assertEqual(bright(frame, box=(0, 0, W, H // 4)), 0)         # no text
        self.assertGreater(max(frame[(H // 2) * W + W // 2 - 20:(H // 2) * W + W // 2 + 20]), 100)     # the logo is there

    def test_the_preview_is_shrunk_after_drawing(self):
        edit = VideoEdit(aspect=None, texts=[TextItem(text="WWWW", size=0.3, outline=0)])
        png = self.tmp / "small.png"
        render_frame(FFMPEG, self.src, png, 1.0, (160, 90), edit, self.info)
        frame = run_ffmpeg("-i", str(png), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-", binary=True).stdout
        self.assertEqual(len(frame), 160 * 90)
        self.assertGreater(bright(frame, 160, level=150), 100)


class JoinTests(unittest.TestCase):
    def test_each_clip_of_a_join_gets_its_own_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            a, b = tmp / "a.mp4", tmp / "b.mp4"
            make_black(a, 2)
            make_black(b, 2)
            parts = [JoinPart(a, VideoEdit(aspect=None, use_gpu=False, texts=[TextItem(text="WWWW", size=0.3, outline=0, start=0.0, end=1.0)])),
                     JoinPart(b, VideoEdit(aspect=None, use_gpu=False, texts=[TextItem(text="WWWW", size=0.3, outline=0, start=1.0)]))]
            out = tmp / "joined.mp4"
            join_clips(FFMPEG, JoinJob(parts, out))
            self.assertGreater(bright(grab(out, 0.5)), 300)         # clip A, 0-1 s
            self.assertEqual(bright(grab(out, 1.5)), 0)             # clip A after its text, 1-2 s
            self.assertEqual(bright(grab(out, 2.5)), 0)             # clip B before its text (its own 0-1 s)
            self.assertGreater(bright(grab(out, 3.5)), 300)         # clip B at its own 1.5 s


class CommandLineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.src = cls.tmp / "black.mp4"
        make_black(cls.src)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_cli(self, *args):
        from video_converter.__main__ import run_cli
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                code = run_cli([str(self.src), "-o", str(self.tmp / "out"), "--overwrite", "--gpu", "off", *args])
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue()

    def result(self, tag="edited"):
        return self.tmp / "out" / f"black ({tag}).mp4"

    def test_text_options(self):
        code, err = self.run_cli("--video", "original", "--text", "WWWW", "--text-start", "1", "--text-end", "2",
                                 "--text-pos", "0.25,0.25", "--text-size", "0.2", "--text-color", "white")
        self.assertEqual(code, 0, err)
        out = self.result()
        self.assertEqual(bright(grab(out, 0.4)), 0)
        frame = grab(out, 1.5)
        self.assertGreater(bright(frame), 200)
        cx, cy = centroid(frame)
        self.assertLess(cx, W / 2)
        self.assertLess(cy, H / 2)
        self.assertEqual(bright(grab(out, 2.5)), 0)

    def test_text_color_names_and_codes(self):
        code, err = self.run_cli("--video", "original", "--text", "WWW", "--text-size", "0.3", "--text-color", "#ff0000")
        self.assertEqual(code, 0, err)
        frame = grab(self.result(), 0.5, "rgb24")
        reds = sum(1 for i in range(0, len(frame), 3) if frame[i] > 180 and frame[i + 1] < 90)
        self.assertGreater(reds, 200)

    def test_a_new_line_can_be_typed_as_backslash_n(self):
        code, err = self.run_cli("--video", "original", "--text", "WW\\nWW", "--text-size", "0.2",
                                 "--text-pos", "0.5,0.5")
        self.assertEqual(code, 0, err)
        frame = grab(self.result(), 0.5)
        self.assertGreater(bright(frame, box=(0, 0, W, H // 2)), 50)
        self.assertGreater(bright(frame, box=(0, H // 2, W, H)), 50)

    def test_logo_options(self):
        logo = self.tmp / "red.png"
        write_png(logo, 20, 20, lambda x, y: (255, 0, 0, 255))
        code, err = self.run_cli("--video", "original", "--logo", str(logo), "--logo-pos", "top-left", "--logo-size",
                                 "0.2", "--logo-opacity", "1")
        self.assertEqual(code, 0, err)
        frame = grab(self.result(), 0.5, "rgb24")
        self.assertGreater(rgb_at(frame, 10 + 20, 10 + 20)[0], 200)
        self.assertLess(rgb_at(frame, W - 20, H - 20)[0], 20)
        code, err = self.run_cli("--video", "original", "--logo", str(logo), "--logo-pos", "0.5,0.5", "--logo-size", "0.2")
        self.assertEqual(code, 0, err)
        self.assertGreater(rgb_at(grab(self.result(), 0.5, "rgb24"), W // 2, H // 2)[0], 200)

    def test_subtitles_option(self):
        srt = self.tmp / "s.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nWWWWWW\n", encoding="utf-8")
        code, err = self.run_cli("--video", "original", "--subtitles", str(srt), "--subtitle-size", "0.12",
                                 "--cut", "0:00-0:00.5")
        self.assertEqual(code, 0, err)
        area = (0, int(H * 0.6), W, H)
        self.assertEqual(bright(grab(self.result(), 0.2), box=area), 0)
        self.assertGreater(bright(grab(self.result(), 0.7), box=area), 100)       # 1 s of the source, 0.5 s cut off

    def test_a_missing_logo_fails_with_a_message(self):
        code, err = self.run_cli("--video", "original", "--logo", str(self.tmp / "nope.png"))
        self.assertEqual(code, 1)
        self.assertIn("nope.png", err)

    def test_options_that_make_no_sense_together_are_refused(self):
        logo = self.tmp / "red.png"
        srt = self.tmp / "s.srt"
        for args in (("--text", "x"),                                        # no --video
                     ("--video", "original", "--text-end", "3"),             # no --text
                     ("--video", "original", "--text", "x", "--text-start", "3", "--text-end", "2"),
                     ("--video", "original", "--text", "x", "--text-pos", "2,3"),
                     ("--video", "original", "--text", "x", "--text-pos", "0.5"),
                     ("--video", "original", "--text", "x", "--text-color", "bluish"),
                     ("--video", "original", "--logo-size", "0.3"),
                     ("--video", "original", "--logo", str(logo), "--logo-pos", "middle"),
                     ("--video", "original", "--logo", str(logo), "--logo-size", "9"),
                     ("--video", "original", "--subtitle-size", "0.1"),
                     ("--subtitles", str(srt))):
            with self.subTest(args=args):
                code, err = self.run_cli(*args)
                self.assertEqual(code, 2)

    def test_subtitles_cannot_be_joined(self):
        other = self.tmp / "other.mp4"
        make_black(other, 1)
        from video_converter.__main__ import run_cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            run_cli([str(self.src), str(other), "--join", "--video", "original", "--subtitles", "x.srt"])
        self.assertIn("--join", err.getvalue())

    def test_the_help_lists_the_options(self):
        from video_converter.__main__ import run_cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            run_cli(["--help"])
        for option in ("--text ", "--text-start", "--text-end", "--text-pos", "--text-size", "--text-color", "--logo ",
                       "--logo-pos", "--logo-size", "--logo-opacity", "--subtitles "):
            self.assertIn(option, out.getvalue())


# ----- fixes: fonts, encodings, sloppy files, races, speed --------------------------------------------------------

LAM_ALEF = "لا لأن الإطار مرحبا"     # لا لأن الإطار مرحبا


def hollow_boxes(gray: bytes, width: int, height: int) -> int:
    """How many shapes in a gray picture are the empty box a font draws for a character it has no glyph for:
    an outline of a rectangle, taller than wide, with hardly any pixels inside."""
    seen = bytearray(len(gray))
    boxes = 0
    for first in range(len(gray)):
        if gray[first] <= 128 or seen[first]:
            continue
        stack, seen[first] = [first], 1
        count, left, right = 0, width, 0
        top, bottom = height, 0
        while stack:
            i = stack.pop()
            x, y = i % width, i // width
            count += 1
            left, right, top, bottom = min(left, x), max(right, x), min(top, y), max(bottom, y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    j = ny * width + nx
                    if 0 <= nx < width and 0 <= ny < height and gray[j] > 128 and not seen[j]:
                        seen[j] = 1
                        stack.append(j)
        w, h = right - left + 1, bottom - top + 1
        if min(w, h) >= 15 and 0.5 < w / h < 1.1 and count < 0.14 * w * h:
            boxes += 1
    return boxes


class FakeFonts:
    """A pretend set of installed fonts: Arial and Impact lack what libass needs for Arabic, Tahoma has it all."""

    GLYPHS = {
        "arial": lambda code: code < 0x700 or code == 0x200D or 0xFEF5 <= code <= 0xFEFC,     # Arabic and lam-alef, but no U+FEFF
        "impact": lambda code: code < 0x250,                                    # no Arabic at all
        "tahoma": lambda code: True,
        "segoe ui": lambda code: True,
    }

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        entries = []
        for name in ("arial", "impact", "tahoma", "segoeui"):
            (self.folder / f"{name}.ttf").write_bytes(b"x")
        entries = [("Arial (TrueType)", "arial.ttf"), ("Impact (TrueType)", "impact.ttf"),
                   ("Tahoma (TrueType)", "tahoma.ttf"), ("Segoe UI (TrueType)", "segoeui.ttf")]

        def load(path):
            has = self.GLYPHS[{"arial": "arial", "impact": "impact", "tahoma": "tahoma", "segoeui": "segoe ui"}[Path(path).stem]]
            font = fonts.Font(path, 1000, 800, -200, 0, [], [500], 1)
            font.has_char = has                      # type: ignore
            return font

        self.patches = [mock.patch.object(fonts, "_registry_entries", return_value=entries),
                        mock.patch.object(fonts, "windows_fonts_dir", return_value=self.folder),
                        mock.patch.object(fonts, "load_font", side_effect=load)]
        for patch in self.patches:
            patch.start()
        fonts.forget_installed_fonts()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        fonts.forget_installed_fonts()
        shutil.rmtree(self.folder, ignore_errors=True)


class SubtitleFontChoiceTests(FakeFonts, unittest.TestCase):
    def test_arabic_needs_the_lam_alef_forms_and_the_joiner_not_only_the_letters(self):
        needed = fonts.needed_glyphs(LAM_ALEF)
        for code in (0xFEFB, 0xFEF5, 0xFEF7, 0xFEF9, 0xFEFF, 0x200D):
            self.assertIn(code, needed)
        self.assertNotIn(0xFEFF, fonts.needed_glyphs("hello"))
        self.assertEqual(fonts.needed_glyphs("a b‫"), {ord("a"), ord("b")})        # spaces and marks need no glyph

    def test_a_font_that_draws_arabic_letters_but_leaves_a_box_after_lam_alef_is_not_used(self):
        # Arial here has every Arabic letter, so the plain glyph check would let it through.
        self.assertTrue(all(fonts.load_font(fonts.font_file("Arial")).has_char(ord(c)) for c in LAM_ALEF if c != " "))
        self.assertEqual(fonts.subtitle_font("Arial", False, LAM_ALEF), "Segoe UI")
        self.assertEqual(fonts.subtitle_font("Impact", True, LAM_ALEF), "Segoe UI")

    def test_the_users_font_stays_when_it_can_draw_everything(self):
        self.assertEqual(fonts.subtitle_font("Tahoma", False, LAM_ALEF), "Tahoma")
        self.assertEqual(fonts.subtitle_font("Arial", False, "Hello there"), "Arial")
        self.assertEqual(fonts.subtitle_font("Impact", False, "Hello there"), "Impact")
        self.assertEqual(fonts.subtitle_font("Arial", False, ""), "Arial")

    def test_hebrew_in_a_font_without_hebrew_changes_font_too(self):
        hebrew = "שלום"
        self.assertEqual(fonts.subtitle_font("Impact", False, hebrew), "Segoe UI")

    def test_an_unknown_font_is_left_to_libass_unless_the_text_is_right_to_left(self):
        self.assertEqual(fonts.subtitle_font("Nope Sans", False, "Hello"), "Nope Sans")
        self.assertEqual(fonts.subtitle_font("Nope Sans", False, LAM_ALEF), "Segoe UI")

    def test_the_subtitles_filter_names_the_font_that_was_chosen(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        arabic, latin = folder / "ar.srt", folder / "en.srt"
        arabic.write_text("1\n00:00:00,000 --> 00:00:01,000\n" + LAM_ALEF + "\n", encoding="utf-8")
        latin.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        vf = captions.subtitle_filter(Subtitles(file=str(arabic), font="Arial"), [(0.0, 9.0)]).replace("\\,", ",")
        self.assertIn("FontName=Segoe UI,", vf)
        vf = captions.subtitle_filter(Subtitles(file=str(latin), font="Arial"), [(0.0, 9.0)]).replace("\\,", ",")
        self.assertIn("FontName=Arial,", vf)

    def test_an_ass_files_own_fonts_are_swapped_only_when_they_cannot_draw_its_words(self):
        script = ("[V4+ Styles]\nFormat: Name, Fontname, Fontsize, Bold\nStyle: Default,Arial,20,0\nStyle: Fancy,Impact,20,-1\n\n"
                  "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                  "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}" + LAM_ALEF + "{\\i0}\n")
        fixed = captions._fix_ass_fonts(script)
        self.assertIn("Style: Default,Segoe UI,20,0", fixed)
        self.assertIn("Style: Fancy,Segoe UI,20,-1", fixed)
        self.assertIn("{\\i1}" + LAM_ALEF, fixed)                  # the dialogue is untouched
        english = script.replace(LAM_ALEF, "Hello")
        self.assertEqual(captions._fix_ass_fonts(english), english)

    def test_font_names_cannot_inject_style_keys(self):
        self.assertEqual(fonts.safe_font_name("Arial,Outline=50:x='y'\\"), "ArialOutline50xy")
        self.assertEqual(fonts.safe_font_name("  Segoe   UI "), "Segoe UI")
        self.assertEqual(fonts.safe_font_name(None), "")
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "a.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
        vf = captions.subtitle_filter(Subtitles(file=str(path), font="Arial,Outline=50,Fontsize=1"), [(0.0, 9.0)])
        style = vf.split("force_style=")[1].replace("\\\\", "\\").replace("\\,", ",").replace("\\:", ":")
        keys = [part.split("=")[0] for part in style.split(",")]
        self.assertEqual(keys, ["FontName", "FontSize", "PrimaryColour", "OutlineColour", "BorderStyle", "Outline",
                                "Shadow", "Bold", "Alignment"])
        self.assertIn("FontName=ArialOutline50Fontsize1,", style)

    def test_a_saved_font_name_is_cleaned_when_it_is_loaded(self):
        self.assertEqual(load_subtitles({"font": "Arial,Bold=-1"}).font, "ArialBold-1")
        self.assertEqual(load_subtitles({"font": ",=:"}).font, fonts.DEFAULT_FONT)
        self.assertEqual(load_subtitles({"font": "Tahoma"}).font, "Tahoma")


@unittest.skipUnless(WINDOWS, "needs the fonts that come with Windows")
class ArabicSubtitleBoxTests(unittest.TestCase):
    """Arial, Times New Roman and Impact used to leave an empty box after every lam-alef in burned-in subtitles."""

    SIZE = (640, 180)

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.srt = self.folder / "ar.srt"
        self.srt.write_text("1\n00:00:00,000 --> 00:00:09,000\n" + LAM_ALEF + "\n", encoding="utf-8")

    def frame(self, font="Arial", bold=False, patch=None):
        vf = captions.subtitle_filter(Subtitles(file=str(self.srt), size=0.3, font=font, bold=bold), [(0.0, 9.0)])
        if patch:
            vf = vf.replace(*patch)
        return render(vf, self.SIZE)

    def test_no_font_leaves_boxes_after_the_ligatures(self):
        for font in ("Arial", "Times New Roman", "Impact", "Tahoma", "Segoe UI", "Calibri"):
            for bold in (False, True):
                with self.subTest(font=font, bold=bold):
                    if not fonts.font_file(font):
                        self.skipTest(f"{font} isn't installed")
                    gray = self.frame(font, bold)
                    self.assertGreater(bright(gray, self.SIZE[0]), 500)
                    self.assertEqual(hollow_boxes(gray, *self.SIZE), 0)

    def test_the_boxes_were_real_with_the_font_the_old_code_gave_libass(self):
        tahoma = self.frame("Tahoma")
        # Force Arial into the very same filter, as the old code did.
        old = self.frame("Tahoma", patch=("FontName=Tahoma", "FontName=Arial"))
        if hollow_boxes(old, *self.SIZE) == 0:
            self.skipTest("this version of Arial draws lam-alef without a box")
        self.assertGreaterEqual(hollow_boxes(old, *self.SIZE), 3)               # one after each of the three ligatures
        self.assertEqual(hollow_boxes(tahoma, *self.SIZE), 0)

    def test_an_ass_file_with_arial_is_fixed_too(self):
        ass = self.folder / "ar.ass"
        ass.write_text("[Script Info]\nPlayResX: 640\nPlayResY: 180\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, "
                       "PrimaryColour, OutlineColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
                       "MarginV\nStyle: Default,Arial,70,&H00FFFFFF,&H00000000,0,1,1,0,2,10,10,10\n\n[Events]\n"
                       "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                       "Dialogue: 0,0:00:00.00,0:00:09.00,Default,,0,0,0,," + LAM_ALEF + "\n", encoding="utf-8")
        vf = captions.subtitle_filter(Subtitles(file=str(ass)), [(0.0, 9.0)])
        gray = render(vf, self.SIZE)
        self.assertGreater(bright(gray, self.SIZE[0]), 500)
        self.assertEqual(hollow_boxes(gray, *self.SIZE), 0)


class SubtitleEncodingTests(unittest.TestCase):
    SAMPLES = {
        "cp1256": ["مرحبا بكم في قناتنا اليوم سوف نتحدث",
                   LAM_ALEF, "أين كنت بالأمس؟ لقد انتظرتك"],
        "cp1255": ["שלום לכולם ברוכים הבאים לערוץ שלנו",
                   "תודה רבה על הצפייה"],
        "cp1251": ["Привет всем добро пожаловать на наш канал",
                   "Спасибо за просмотр"],
        "cp1253": ["Γεια σας καλώς ήρθατε στο κανάλι μας"],
        "cp1254": ["Merhaba herkese kanalımıza hoş geldiniz bugün önemli bir konudan bahsedeceğiz",
                   "Teşekkürler, ışık"],
        "cp1252": ["Bonjour à tous, bienvenue sur la chaîne. Aujourd'hui nous parlons d'un sujet très important",
                   "Hello café", "Ich möchte über Straßenbau sprechen, größer und schöner",
                   "¿Cómo estás? Muy bien, gracias. El niño está allí."],
    }

    @staticmethod
    def srt(text: str) -> str:
        return "1\r\n00:00:01,000 --> 00:00:03,000\r\n" + text + "\r\n\r\n2\r\n00:00:04,000 --> 00:00:05,000\r\nOK\r\n"

    def test_each_old_code_page_is_recognised(self):
        for encoding, texts in self.SAMPLES.items():
            for text in texts:
                with self.subTest(encoding=encoding, text=text[:20]):
                    got, name = captions.decode_text(self.srt(text).encode(encoding))
                    self.assertEqual(got, self.srt(text))
                    self.assertEqual(name, encoding if encoding != "cp1254" or "ı" in text else name)

    def test_arabic_in_windows_1256_is_no_longer_mojibake(self):
        data = self.srt(self.SAMPLES["cp1256"][0]).encode("cp1256")
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "a.srt").write_bytes(data)
        text, encoding = captions.read_text_with_encoding(folder / "a.srt")
        self.assertEqual(encoding, "cp1256")
        self.assertEqual(read_text(folder / "a.srt"), text)
        self.assertEqual(parse_cues(text)[0].text, self.SAMPLES["cp1256"][0])

    def test_utf16_without_a_byte_order_mark(self):
        for encoding in ("utf-16-le", "utf-16-be"):
            for text in (self.SAMPLES["cp1256"][0], self.SAMPLES["cp1255"][0], "plain english words here"):
                with self.subTest(encoding=encoding, text=text[:12]):
                    got, name = captions.decode_text(self.srt(text).encode(encoding))
                    self.assertEqual((got, name), (self.srt(text), encoding))

    def test_byte_order_marks_say_what_the_file_is(self):
        text = self.srt(self.SAMPLES["cp1256"][0])
        self.assertEqual(captions.decode_text(b"\xef\xbb\xbf" + text.encode("utf-8")), (text, "utf-8-sig"))
        self.assertEqual(captions.decode_text(text.encode("utf-16"))[0], text)
        self.assertEqual(captions.decode_text(b"\xfe\xff" + text.encode("utf-16-be")), (text, "utf-16-be"))
        self.assertEqual(captions.decode_text(text.encode("utf-32"))[0], text)

    def test_valid_utf8_and_plain_ascii_are_utf8(self):
        self.assertEqual(captions.decode_text("café م".encode("utf-8")), ("café م", "utf-8"))
        self.assertEqual(captions.decode_text(b"plain ascii 123"), ("plain ascii 123", "utf-8"))
        self.assertEqual(captions.decode_text(b""), ("", "utf-8"))

    def test_bytes_that_are_nothing_in_particular_still_give_text(self):
        text, name = captions.decode_text(bytes(range(128, 256)) * 3)
        self.assertIsInstance(text, str)
        self.assertTrue(name)

    def test_smart_quotes_alone_stay_windows_1252(self):
        self.assertEqual(captions.decode_text("“quoted” — text".encode("cp1252")),
                         ("“quoted” — text", "cp1252"))


class TextCleaningTests(unittest.TestCase):
    def test_a_leading_blank_line_no_longer_hides_the_text(self):
        for raw, wanted in (("\nHello", "Hello"), ("\n\n  \nHello\n\n", "Hello"), ("Hello\n", "Hello"),
                            ("A\n\nB", "A\n \nB"), ("A\n\n\nB", "A\n \n \nB"), ("\r\nA\r\n\r\nB\r\n", "A\n \nB")):
            with self.subTest(raw=raw):
                self.assertEqual(text_file_of(text_filter(TextItem(text=raw), FRAME, None)), wanted)

    def test_the_preview_draws_the_same_text_the_export_does(self):
        wrapped, _, _ = captions.laid_out(TextItem(text="\n\nHello\n\nthere\n"), FRAME)
        self.assertEqual(wrapped, "Hello\n \nthere")
        self.assertEqual(text_box(TextItem(text="\nHello"), FRAME), text_box(TextItem(text="Hello"), FRAME))

    def test_a_text_of_only_blank_lines_draws_nothing(self):
        self.assertEqual(text_filter(TextItem(text="\n \n\t\n"), FRAME, None), "")

    @unittest.skipUnless(WINDOWS, "needs Arial")
    def test_the_text_really_shows_with_a_leading_line_break_and_a_blank_line_keeps_its_height(self):
        def item(text):
            return TextItem(text=text, size=0.2, outline=0, y=0.5)

        plain = render(text_filter(item("WWW"), FRAME, None))
        led = render(text_filter(item("\nWWW"), FRAME, None))
        self.assertGreater(bright(led), 300)
        self.assertEqual(led, plain)
        tight = render(text_filter(item("WWW\nWWW"), FRAME, None))
        gap = render(text_filter(item("WWW\n\nWWW"), FRAME, None))

        def height(frame):
            ys = [i // W for i, v in enumerate(frame) if v > 128]
            return max(ys) - min(ys)
        self.assertGreater(height(gap), height(tight) + 10)

    def test_control_characters_are_dropped(self):
        raw = "he\x00l\x07lo\x85 wor\x1bld\ttab next"
        self.assertEqual(text_file_of(text_filter(TextItem(text=raw), FRAME, None)), "hello world tab\nnext")
        self.assertFalse(captions.drawable(TextItem(text="\x01\x02")))
        self.assertTrue(captions.drawable(TextItem(text="\x01a")))
        self.assertEqual(captions.clean_text("a\x80b\x9fc\r\nd"), "abc\nd")

    def test_control_characters_in_subtitles_are_dropped_too(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "a.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhe\x07llo\x85 there\n", encoding="utf-8")
        new, _, _ = retimed_subtitles(Subtitles(file=str(path)), [(0.0, 4.0)])
        self.assertEqual(new.read_text(encoding="utf-8").split("\n")[2], "hello there")


class SloppySubtitleTests(unittest.TestCase):
    def test_times_without_milliseconds_and_with_odd_fractions(self):
        text = ("1\n00:00:01 --> 00:00:02\nno ms\n\n2\n00:00:03,5 --> 00:00:04,25\nshort fractions\n\n"
                "3\n00:00:05,1234 --> 00:00:06.98765\nlong fractions\n\n4\n01:02 --> 01:03\nminutes only\n")
        cues = parse_cues(text)
        self.assertEqual([c.text for c in cues], ["no ms", "short fractions", "long fractions", "minutes only"])
        self.assertEqual([(c.start, c.end) for c in cues][:2], [(1.0, 2.0), (3.5, 4.25)])
        self.assertAlmostEqual(cues[2].start, 5.1234, places=4)
        self.assertAlmostEqual(cues[2].end, 6.98765, places=4)
        self.assertEqual((cues[3].start, cues[3].end), (62.0, 63.0))

    def test_webvtt_times_without_milliseconds(self):
        cues = parse_cues("WEBVTT\n\n00:01 --> 00:02\nhi\n\n00:00:03 --> 00:00:04.5\nthere\n", "vtt")
        self.assertEqual([(c.start, c.end, c.text) for c in cues], [(1.0, 2.0, "hi"), (3.0, 4.5, "there")])

    def test_a_blank_line_inside_an_srt_cue_does_not_cut_it_off(self):
        text = ("1\n00:00:01,000 --> 00:00:02,000\nfirst part\n\nsecond part\n\n2\n00:00:03,000 --> 00:00:04,000\nnext\n\n"
                "00:00:05,000 --> 00:00:06,000\nno number\n\nstill the same cue\n")
        cues = parse_cues(text)
        self.assertEqual([c.text for c in cues], ["first part\n\nsecond part", "next", "no number\n\nstill the same cue"])
        self.assertEqual(len(cues), 3)

    def test_a_blank_line_still_ends_a_webvtt_cue(self):
        cues = parse_cues("WEBVTT\n\n00:01.000 --> 00:02.000\none\n\nNOTE not a cue\n\n00:03.000 --> 00:04.000\ntwo\n", "vtt")
        self.assertEqual([c.text for c in cues], ["one", "two"])

    def test_an_srt_with_no_blank_lines_between_cues_is_read(self):
        cues = parse_cues("1\n00:00:01,000 --> 00:00:02,000\none\n2\n00:00:03,000 --> 00:00:04,000\ntwo\n")
        self.assertEqual([c.text for c in cues], ["one", "two"])

    def test_a_blank_line_inside_a_cue_survives_the_rewrite_without_splitting_it(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "a.srt"
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\nfirst\n\nsecond\n", encoding="utf-8")
        new, _, _ = retimed_subtitles(Subtitles(file=str(path)), [(0.0, 4.0)])
        again = parse_cues(new.read_text(encoding="utf-8"))
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].text.replace(" ", ""), "first\n\nsecond")

    def test_literal_braces_are_escaped_but_real_overrides_are_not(self):
        self.assertEqual(captions.srt_cue_text("{music} la {x}"), "\\{music\\} la \\{x\\}")
        self.assertEqual(captions.srt_cue_text("{\\an8}top"), "{\\an8}top")
        self.assertEqual(captions.srt_cue_text("a { b"), "a \\{ b")
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "a.srt"
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\n[laughs] {laughs}\n", encoding="utf-8")
        new, _, _ = retimed_subtitles(Subtitles(file=str(path)), [(0.0, 4.0)])
        self.assertIn("[laughs] \\{laughs\\}", new.read_text(encoding="utf-8"))

    def test_text_in_braces_is_shown_not_swallowed(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)

        def shown(text):
            path = folder / f"{abs(hash(text))}.srt"
            path.write_text(f"1\n00:00:00,000 --> 00:00:09,000\n{text}\n", encoding="utf-8")
            vf = captions.subtitle_filter(Subtitles(file=str(path), size=0.2, font="Tahoma" if WINDOWS else "Sans"),
                                          [(0.0, 9.0)])
            return bright(render(vf))

        self.assertGreater(shown("WWWW"), 100)
        self.assertGreater(shown("{WWWW}"), shown("WWWW"))                  # the braces are drawn as well


class ScratchRaceTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        patcher = mock.patch.object(captions, "_scratch", self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def hammer(self, make, remove, rounds=60, workers=8):
        """`workers` threads make the same file at the same moment, `rounds` times over; returns the failures."""
        failures = []
        for _ in range(rounds):
            barrier = threading.Barrier(workers)

            def work():
                try:
                    barrier.wait(5)
                    make()
                except Exception as exc:                 # noqa: BLE001 - every kind of failure counts
                    failures.append(repr(exc))

            threads = [threading.Thread(target=work) for _ in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            remove()
        return failures

    def test_threads_saving_the_same_content_at_once_never_collide(self):
        data = b"1\n00:00:00,000 --> 00:00:01,000\nhello\n" * 200
        path = captions._scratch_file(data, ".srt")

        def remove():                          # once the threads are done, the file must be whole
            self.assertEqual(path.read_bytes(), data)
            path.unlink()

        failures = self.hammer(lambda: captions._scratch_file(data, ".srt"), remove)
        self.assertEqual(failures, [])
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.endswith(".part")], [])

    def test_different_content_in_different_threads(self):
        failures = []

        def work(n):
            for i in range(40):
                data = f"{n}-{i}".encode() * 100
                try:
                    assert captions._scratch_file(data, ".txt").read_bytes() == data
                except Exception as exc:                 # noqa: BLE001
                    failures.append(repr(exc))

        threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(failures, [])

    def test_a_refused_replace_is_fine_when_someone_else_already_put_the_same_file_there(self):
        data = b"same content"
        real = captions.os.replace
        calls = []

        def refuse(src, dst):
            calls.append(src)
            captions.Path(dst).write_bytes(data)         # the other thread's file appears just before
            raise PermissionError(13, "in use")

        with mock.patch.object(captions.os, "replace", refuse):
            path = captions._scratch_file(data, ".txt")
        self.assertEqual(path.read_bytes(), data)
        self.assertTrue(calls)
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.endswith(".part")], [])
        self.assertTrue(real)

    def test_a_replace_that_keeps_failing_with_other_content_is_an_error(self):
        with mock.patch.object(captions.os, "replace", side_effect=PermissionError(13, "in use")), \
                mock.patch.object(captions.time, "sleep"):
            with self.assertRaises(PermissionError):
                captions._scratch_file(b"abc", ".txt")
        self.assertEqual([p.name for p in self.folder.iterdir()], [])

    def test_threads_copying_the_same_logo_at_once_never_collide(self):
        logo_file = self.folder.parent / f"logo-{uuid_hex()}.png"
        write_png(logo_file, 8, 8, lambda x, y: (255, 0, 0, 255))
        self.addCleanup(logo_file.unlink, True)
        logo = Logo(file=str(logo_file))
        target = captions.logo_source(logo)

        def remove():
            self.assertEqual(target.read_bytes(), logo_file.read_bytes())
            target.unlink()

        failures = self.hammer(lambda: captions.logo_source(logo), remove, rounds=40)
        self.assertEqual(failures, [])


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


class LogoFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_graph(self, logo: Logo, scratch: Path, seconds: float = 3.0, at: float = 0.2):
        """The overlay graph run for real over a black video: (the picture at `at` seconds as RGB, ffmpeg's complaint)."""
        with mock.patch.object(captions, "_scratch", scratch):
            statements = overlay_graph("[0:v]", "[out]", [], logo, Subtitles(), FRAME, [(0.0, None)])
        out = self.tmp / f"{uuid_hex()}.mp4"
        code, error = run_ffmpeg("-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:d={seconds}:r=10", "-filter_complex",
                                 ";".join(statements), "-map", "[out]", "-t", f"{seconds}", "-pix_fmt", "yuv420p",
                                 "-c:v", "libx264", str(out))
        if code:
            return None, error
        return grab(out, at, "rgb24"), ""

    @staticmethod
    def reds(frame: bytes) -> int:
        return sum(1 for i in range(0, len(frame), 3) if frame[i] > 180 and frame[i + 1] < 90 and frame[i + 2] < 90)

    def test_a_percent_sign_in_the_temp_folder_does_not_break_the_logo(self):
        scratch = self.tmp / "fve 100%d and %03d"
        scratch.mkdir()
        picture = self.tmp / "red.png"
        write_png(picture, 40, 40, lambda x, y: (255, 0, 0, 255))
        frame, error = self.run_graph(Logo(file=str(picture), size=0.3, corner="center"), scratch)
        self.assertIsNotNone(frame, error)
        self.assertGreater(self.reds(frame), 500)

    def test_the_graph_says_the_logo_is_a_plain_file(self):
        picture = self.tmp / "red2.png"
        write_png(picture, 8, 8, lambda x, y: (255, 0, 0, 255))
        statements = overlay_graph("[0:v]", "[out]", [], Logo(file=str(picture)), Subtitles(), FRAME, [(0.0, None)])
        self.assertIn("format_opts=pattern_type\\=none", statements[0])

    def test_every_picture_type_the_dialog_offers_works(self):
        base = self.tmp / "base.png"
        write_png(base, 24, 24, lambda x, y: (255, 0, 0, 255))
        for extension in captions.IMAGE_EXTENSIONS:
            with self.subTest(extension=extension):
                picture = self.tmp / f"pic{extension}"
                code, error = run_ffmpeg("-i", str(base), "-frames:v", "1", "-update", "1", str(picture))
                if code:
                    self.skipTest(f"this ffmpeg can't make {extension}: {error[:80]}")
                scratch = self.tmp / f"scratch{extension[1:]}%d"
                scratch.mkdir()
                frame, error = self.run_graph(Logo(file=str(picture), size=0.3, corner="center"), scratch)
                self.assertIsNotNone(frame, error)
                self.assertGreater(self.reds(frame), 200)

    def test_the_logo_picker_offers_pictures_that_stay_still_not_gifs_or_videos(self):
        self.assertEqual(captions.IMAGE_EXTENSIONS, (".png", ".jpg", ".jpeg", ".webp", ".bmp"))

    def test_a_gif_still_works_as_a_logo_and_gets_no_image_pattern_option(self):
        gif = self.tmp / "white.gif"
        code, error = run_ffmpeg("-f", "lavfi", "-i", "color=c=white:s=40x40:d=1:r=10", str(gif))
        self.assertEqual(code, 0, error)
        scratch = self.tmp / "gifs 100%d"
        scratch.mkdir(exist_ok=True)
        logo = Logo(file=str(gif), size=0.4, corner="center")
        with mock.patch.object(captions, "_scratch", scratch):
            graph = overlay_graph("[0:v]", "[out]", [], logo, Subtitles(), FRAME, [(0.0, None)])[0]
        self.assertNotIn("format_opts", graph)
        frame, error = self.run_graph(logo, scratch, seconds=1.0, at=0.2)
        self.assertIsNotNone(frame, error)
        self.assertGreater(sum(1 for i in range(0, len(frame), 3) if frame[i] > 200), 300)


class SpanMapperTests(unittest.TestCase):
    @staticmethod
    def reference(start, end, segments, speed=1.0, reverse=False):
        """The mapping as it was written before it used bisection: every segment looked at for every span."""
        from video_converter.timing import clamp_speed, output_duration
        speed = clamp_speed(speed)
        total = output_duration(segments, speed)
        pieces, done = [], 0.0
        for a, b in segments:
            high = end if b is None else min(end, b)
            low = max(start, a)
            if high - low > 1e-6:
                lo, hi = (done + low - a) / speed, (done + high - a) / speed
                if reverse and total is not None:
                    lo, hi = total - hi, total - lo
                pieces.append((lo, hi))
            if b is None:
                break
            done += b - a
        pieces.sort()
        joined = []
        for lo, hi in pieces:
            if joined and lo - joined[-1][1] < 0.001:
                joined[-1] = (joined[-1][0], max(joined[-1][1], hi))
            else:
                joined.append((lo, hi))
        return [(lo, hi) for lo, hi in joined if hi - lo >= 0.02]

    def test_it_gives_the_same_answers_as_the_plain_loop(self):
        import random
        rng = random.Random(7)
        for case in range(60):
            cuts = sorted(rng.uniform(0, 100) for _ in range(rng.randint(0, 12)))
            segments, at = [], 0.0
            for i in range(0, len(cuts) - 1, 2):
                segments.append((cuts[i], cuts[i + 1]))
            if case % 3 == 0:
                segments.append((cuts[-1] if cuts else 0.0, None))
            if not segments:
                segments = [(0.0, None)]
            for speed in (1.0, 0.5, 2.0):
                for reverse in (False, True):
                    mapper = captions.SpanMapper(segments, speed, reverse)
                    for _ in range(25):
                        a = rng.uniform(0, 110)
                        b = a + rng.uniform(0, 15)
                        self.assertEqual(mapper(a, b), self.reference(a, b, segments, speed, reverse),
                                         (segments, a, b, speed, reverse))
                        self.assertEqual(map_span(a, b, segments, speed, reverse), mapper(a, b))

    def test_overlapping_or_unordered_parts_still_work(self):
        segments = [(5.0, 9.0), (0.0, 6.0)]
        for a, b in ((1.0, 2.0), (4.0, 8.0), (5.5, 5.6)):
            self.assertEqual(captions.SpanMapper(segments)(a, b), self.reference(a, b, segments))

    def test_1500_cues_through_600_cuts_is_quick(self):
        import time as clock
        segments = [(i * 2.0, i * 2.0 + 1.5) for i in range(600)]
        spans = [(i * 0.8, i * 0.8 + 1.2) for i in range(1500)]
        started = clock.perf_counter()
        slow = [self.reference(a, b, segments) for a, b in spans]
        slow_time = clock.perf_counter() - started
        started = clock.perf_counter()
        mapper = captions.SpanMapper(segments)
        fast = [mapper(a, b) for a, b in spans]
        fast_time = clock.perf_counter() - started
        self.assertEqual(fast, slow)
        self.assertLess(fast_time, slow_time / 5)
        self.assertLess(fast_time, 0.25)

    def test_the_rewrite_of_a_big_file_uses_it(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "big.srt"
        path.write_text("".join(f"{i + 1}\n{captions._srt_time(i * 0.8)} --> {captions._srt_time(i * 0.8 + 0.6)}\nline {i}\n\n"
                                for i in range(1500)), encoding="utf-8")
        segments = [(i * 2.0, i * 2.0 + 1.5) for i in range(600)]
        import time as clock
        started = clock.perf_counter()
        new, _, _ = retimed_subtitles(Subtitles(file=str(path)), segments)
        self.assertLess(clock.perf_counter() - started, 1.0)
        self.assertGreater(len(parse_cues(new.read_text(encoding="utf-8"))), 500)


class AssStyleTests(unittest.TestCase):
    ASS = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n\n[V4+ Styles]\n"
           "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, BorderStyle, Outline, Shadow, "
           "Alignment, MarginL, MarginR, MarginV\nStyle: Default,Arial,40,&H000000FF,&H00000000,0,1,0,0,8,10,10,10\n\n"
           "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
           "Dialogue: 0,0:00:00.00,0:00:09.00,Default,,0,0,0,,WWWW\n")

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = self.folder / "a.ass"
        self.path.write_text(self.ASS, encoding="utf-8")

    def test_the_default_keeps_the_file_s_own_style(self):
        self.assertTrue(Subtitles().own_style)
        vf = captions.subtitle_filter(Subtitles(file=str(self.path)), [(0.0, 9.0)])
        self.assertNotIn("force_style", vf)
        self.assertNotIn("FontName", vf)

    def test_turning_it_off_applies_the_chosen_style_as_before(self):
        vf = captions.subtitle_filter(Subtitles(file=str(self.path), own_style=False, color="#00ff00", position="bottom"),
                                      [(0.0, 9.0)])
        self.assertIn("force_style", vf)
        self.assertIn("Alignment=2", vf.replace("\\,", ","))

    def test_an_srt_always_gets_the_chosen_style(self):
        srt = self.folder / "a.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:09,000\nWWWW\n", encoding="utf-8")
        self.assertIn("force_style", captions.subtitle_filter(Subtitles(file=str(srt)), [(0.0, 9.0)]))

    def test_the_colour_and_the_alignment_of_the_file_are_what_is_drawn(self):
        def reds(vf):
            frame = render(vf, pix="rgb24")
            top = [(frame[(y * W + x) * 3], frame[(y * W + x) * 3 + 1], frame[(y * W + x) * 3 + 2])
                   for y in range(H // 3) for x in range(W)]
            bottom = [(frame[(y * W + x) * 3], frame[(y * W + x) * 3 + 1], frame[(y * W + x) * 3 + 2])
                      for y in range(2 * H // 3, H) for x in range(W)]
            red = lambda p: p[0] > 200 and p[1] < 80 and p[2] < 80          # noqa: E731
            white = lambda p: p[0] > 200 and p[1] > 200 and p[2] > 200      # noqa: E731
            return sum(map(red, top)), sum(map(white, bottom))

        red_top, white_bottom = reds(captions.subtitle_filter(Subtitles(file=str(self.path)), [(0.0, 9.0)]))
        self.assertGreater(red_top, 100)                 # the file says red, at the top
        self.assertEqual(white_bottom, 0)
        red_top, white_bottom = reds(captions.subtitle_filter(Subtitles(file=str(self.path), own_style=False, size=0.2),
                                                              [(0.0, 9.0)]))
        self.assertEqual(red_top, 0)                     # force_style: white, at the bottom
        self.assertGreater(white_bottom, 100)

    def test_the_setting_is_saved_and_loaded(self):
        self.assertFalse(load_subtitles({"own_style": False}).own_style)
        self.assertTrue(load_subtitles({"own_style": "no"}).own_style)           # junk means the default
        self.assertTrue(load_subtitles({}).own_style)
        self.assertFalse(load_subtitles(asdict(Subtitles(own_style=False))).own_style)


if __name__ == "__main__":
    unittest.main()
