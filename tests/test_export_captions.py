"""Text, logo and subtitles (2.8) together with the export options (2.9): Save frame, GIFs, a file size to
fit and joins in another format must all carry the same overlays as a plain MP4 export."""

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_captions import H, W, bright, make_black, write_png
from video_converter import __main__ as cli
from video_converter.captions import Logo, Subtitles, TextItem
from video_converter.core import find_ffmpeg, probe_media
from video_converter.stills import save_frame
from video_converter.video import JoinJob, JoinPart, VideoEdit, VideoJob, convert_video, join_clips, plan_geometry, \
    render_frame

FFMPEG = find_ffmpeg()
TEXT = TextItem(text="WWWW", size=0.3, outline=0)       # white on black: bright pixels are the text


def frame_pixels(path: Path, at: float = 0.5, pix: str = "gray") -> bytes:
    """The raw pixels of a picture or of a video at `at` seconds (a GIF is read from its start)."""
    seek = ["-ss", f"{at}"] if path.suffix.lower() not in (".png", ".jpg", ".gif") else []
    return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", *seek, "-i", str(path), "-frames:v", "1",
                           "-f", "rawvideo", "-pix_fmt", pix, "-"], capture_output=True, check=True).stdout


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class CaptionsInEveryExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.src = cls.tmp / "black.mp4"
        make_black(cls.src, 3)
        cls.info = probe_media(FFMPEG, cls.src)
        cls.logo = cls.tmp / "logo.png"
        write_png(cls.logo, 30, 30, lambda x, y: (255, 255, 255, 255))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def export(self, name, **edit):
        edit = VideoEdit(**{"aspect": None, "use_gpu": False, **edit})
        out = self.tmp / name
        convert_video(FFMPEG, VideoJob(self.src, out, edit), info=self.info)
        return out

    # ----- Save frame

    def test_a_saved_frame_is_the_preview_frame_with_the_text(self):
        edit = VideoEdit(aspect=None, texts=[TEXT], logo=Logo(file=str(self.logo), corner="top-left", size=0.2))
        saved = save_frame(FFMPEG, self.src, self.tmp / "saved.png", 1.0, edit, self.info)
        geometry = plan_geometry(self.info.width, self.info.height, edit)
        shown = render_frame(FFMPEG, self.src, self.tmp / "shown.png", 1.0,
                             (geometry.out_width, geometry.out_height), edit, self.info)
        self.assertEqual(frame_pixels(saved, pix="rgb24"), frame_pixels(shown, pix="rgb24"))
        self.assertGreater(bright(frame_pixels(saved), box=(0, H // 3, W, H)), 300)     # the text, below the logo

    def test_a_saved_frame_matches_the_exported_video(self):
        edit = VideoEdit(aspect=None, use_gpu=False, crf=12, texts=[TEXT])
        out = self.export("match.mp4", crf=12, texts=[TEXT])
        saved = save_frame(FFMPEG, self.src, self.tmp / "match.png", 1.0, edit, self.info)
        pic, vid = frame_pixels(saved, pix="rgb24"), frame_pixels(out, 1.0, "rgb24")
        self.assertEqual(len(pic), len(vid))
        self.assertLess(sum(abs(a - b) for a, b in zip(pic, vid)) / len(pic), 3.0)

    def test_a_saved_jpg_has_the_text_too(self):
        edit = VideoEdit(aspect=None, texts=[TEXT])
        saved = save_frame(FFMPEG, self.src, self.tmp / "saved.jpg", 1.0, edit, self.info)
        self.assertGreater(bright(frame_pixels(saved)), 300)

    def test_a_saved_frame_follows_the_text_window_and_the_cuts(self):
        edit = VideoEdit(aspect=None, keep=[(0.0, 1.0), (2.0, 3.0)],
                         texts=[TextItem(text="WWWW", size=0.3, outline=0, start=0.0, end=0.5)])
        early = save_frame(FFMPEG, self.src, self.tmp / "early.png", 0.2, edit, self.info)
        late = save_frame(FFMPEG, self.src, self.tmp / "late.png", 2.5, edit, self.info)      # 1.5 s into the result
        self.assertGreater(bright(frame_pixels(early)), 300)
        self.assertEqual(bright(frame_pixels(late)), 0)

    # ----- a GIF

    def test_a_gif_shows_the_text(self):
        out = self.export("text.gif", video_format="gif", texts=[TEXT])
        self.assertGreater(bright(frame_pixels(out), width=out_width(out)), 300)

    def test_a_gif_follows_the_text_window(self):
        out = self.export("window.gif", video_format="gif",
                          texts=[TextItem(text="WWWW", size=0.3, outline=0, start=1.0, end=2.0)])
        first = frame_pixels(out)
        later = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", "1.4", "-i", str(out),
                                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                               capture_output=True, check=True).stdout
        self.assertEqual(bright(first, width=out_width(out)), 0)
        self.assertGreater(bright(later, width=out_width(out)), 300)

    def test_a_gif_has_the_logo_and_subtitles(self):
        srt = self.tmp / "g.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:09,000\nWWWWWW\n", encoding="utf-8")
        out = self.export("both.gif", video_format="gif", logo=Logo(file=str(self.logo), corner="top-left", size=0.3),
                          subtitles=Subtitles(file=str(srt), size=0.15, outline=0))
        pixels, width = frame_pixels(out), out_width(out)
        self.assertGreater(bright(pixels, width, box=(0, 0, width // 4, len(pixels) // width // 4)), 20)    # logo
        self.assertGreater(bright(pixels, width, box=(0, len(pixels) // width * 2 // 3, width, len(pixels) // width)), 50)

    # ----- a file size to fit

    def test_a_video_made_to_fit_a_size_keeps_the_text_in_both_passes(self):
        out = self.export("fit.mp4", target_mb=1.0, texts=[TEXT])
        self.assertLessEqual(out.stat().st_size, 1_000_000)
        self.assertGreater(bright(frame_pixels(out, 1.0)), 300)

    def test_a_webm_made_to_fit_a_size_keeps_the_text(self):
        out = self.export("fit.webm", video_format="webm", target_mb=1.0, texts=[TEXT])
        self.assertGreater(bright(frame_pixels(out, 1.0)), 300)

    def test_h265_keeps_the_text(self):
        out = self.export("text.mp4", video_format="hevc", texts=[TEXT])
        self.assertGreater(bright(frame_pixels(out, 1.0)), 300)

    # ----- joins

    def test_a_join_in_another_format_keeps_each_clips_text(self):
        other = self.tmp / "other.mp4"
        make_black(other, 2)
        parts = [JoinPart(self.src, VideoEdit(aspect=None, use_gpu=False, video_format="webm", target_mb=1.0,
                                              texts=[TextItem(text="WWWW", size=0.3, outline=0, end=1.0)])),
                 JoinPart(other, VideoEdit(aspect=None, use_gpu=False, texts=[TEXT]))]
        out = self.tmp / "joined.webm"
        join_clips(FFMPEG, JoinJob(parts, out))
        info = probe_media(FFMPEG, out)
        self.assertAlmostEqual(info.duration, 5.0, delta=0.4)
        self.assertGreater(bright(frame_pixels(out, 0.5)), 300)      # the first clip, while its text is on
        self.assertEqual(bright(frame_pixels(out, 2.0)), 0)          # the first clip, after its text
        self.assertGreater(bright(frame_pixels(out, 4.0)), 300)      # the second clip

    # ----- the command line

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run_cli([str(a) for a in args])
        self.assertEqual(code, 0, err.getvalue())

    def test_the_command_line_saves_a_frame_with_the_text(self):
        target = self.tmp / "cli-frame.png"
        self.run_cli(self.src, "--frame-at", "1", "--frame-out", target, "--text", "WWWW", "--text-size", "0.3")
        self.assertGreater(bright(frame_pixels(target)), 300)

    def test_the_command_line_makes_a_gif_with_the_text(self):
        folder = self.tmp / "cli-gif"
        self.run_cli(self.src, "--format", "gif", "--text", "WWWW", "--text-size", "0.3",
                     "-o", folder)
        gif = next(folder.glob("*.gif"))
        self.assertGreater(bright(frame_pixels(gif), width=out_width(gif)), 300)


def out_width(path: Path) -> int:
    return probe_media(FFMPEG, path).width


if __name__ == "__main__":
    unittest.main()
