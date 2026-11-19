"""Burning subtitles in."""

from __future__ import annotations

from ..fonts import subtitle_font, windows_fonts_dir
from ..timing import Segment
from .escaping import _n, _path_value, filter_value
from .model import Subtitles
from .retime import retimed_subtitles


def _ass_color(color: str) -> str:
    """"#RRGGBB" as the &HAABBGGRR& that ASS styles use (no transparency)."""
    return f"&H00{color[5:7]}{color[3:5]}{color[1:3]}&".upper()


def subtitle_filter(
        subtitles: Subtitles, segments: list[Segment], speed: float = 1.0,
        reverse: bool = False) -> str:
    """The subtitles filter, drawing the file on the export's timeline in the chosen style. An
    .ass/.ssa file keeps its own design (unless the style is asked for); the font is one that can
    draw the words."""
    path, kind, script_height = retimed_subtitles(subtitles, segments, speed, reverse)
    options = [f"filename={_path_value(path)}"]
    fonts = windows_fonts_dir()
    if fonts:
        options.append(
            f"fontsdir={_path_value(fonts)}")  # so the fonts are found without any font setup
    if kind == "ass" and subtitles.own_style:
        return "subtitles=" + ":".join(options)
    try:
        words = path.read_text(encoding="utf-8")
    except OSError:
        words = ""
    size = subtitles.size * script_height
    style = ",".join(
        [
            f"FontName={subtitle_font(subtitles.font, subtitles.bold, words)}",
            f"FontSize={_n(size, 2)}",
            f"PrimaryColour={_ass_color(subtitles.color)}",
            f"OutlineColour={_ass_color(subtitles.outline_color)}",
            "BorderStyle=1",
            f"Outline={_n(subtitles.outline * size, 2)}",
            "Shadow=0",
            f"Bold={-1 if subtitles.bold else 0}",
            # force_style numbers them the SSA way
            f"Alignment={ {'bottom': 2, 'middle': 10, 'top': 6}[subtitles.position] }",
        ])
    options.append(f"force_style={filter_value(style)}")
    return "subtitles=" + ":".join(options)
