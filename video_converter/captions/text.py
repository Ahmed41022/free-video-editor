"""Text on the picture: cleaning, wrapping and the drawtext filter."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Optional

from ..fonts import load_font, pick_font, text_size, wrap_text
from .escaping import _hex, _n, _path_value
from .model import BOX_PADDING, WRAP_WIDTH, Logo, Subtitles, TextItem
from .scratch import _scratch_file


def has_overlays(texts: list[TextItem], logo: Logo, subtitles: Subtitles) -> bool:
    return bool(logo.file or subtitles.file or any(drawable(item) for item in texts))


def drawable(item: TextItem) -> bool:
    """Whether a text item draws anything: it has text and a time to be seen."""
    return bool(clean_text(item.text).strip()) and (item.end is None or item.end > item.start)


def text_pixels(item: TextItem, frame: tuple[int, int]) -> int:
    return max(int(round(item.size * frame[1])), 6)


def clean_text(text: str) -> str:
    """`text` without the characters that only draw an empty box: control characters (C0 and C1)
    are dropped, a tab becomes a space and the odd line separators become a line break."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028",
                                                                  "\n").replace("\u2029", "\n")
    text = text.replace("\t", " ")
    return "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) != "Cc")


def drawable_text(text: str) -> str:
    """`text` as it is drawn: clean, with no blank lines at the start or the end (drawtext draws
    nothing at all for a text that starts with a line break) and a single space on each blank line
    in the middle, so that the blank line keeps its height."""
    lines = clean_text(text).split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line if line.strip() else " " for line in lines)


def laid_out(item: TextItem, frame: tuple[int, int]) -> tuple[str, Optional[str], float]:
    """(the text with its line breaks, the font file, the text size in pixels) for drawing `item`
    in a `frame` (width, height). The preview and the export both draw exactly this text."""
    pixels = text_pixels(item, frame)
    text = drawable_text(item.text)
    path = pick_font(item.font, item.bold, text)
    font = load_font(path) if path else None
    padding = 2 * (BOX_PADDING if item.box else item.outline) * pixels
    return wrap_text(text, font, pixels, frame[0] * WRAP_WIDTH - padding), path, float(pixels)


def text_box(item: TextItem, frame: tuple[int, int]) -> tuple[float, float]:
    """About how big the drawn text is in a `frame` (width, height), padding and outline included:
    what the editor draws its handle around. ffmpeg does the exact drawing, so this is close, not
    exact."""
    wrapped, path, pixels = laid_out(item, frame)
    width, height = text_size(wrapped, load_font(path) if path else None, pixels)
    pad = (BOX_PADDING if item.box else item.outline) * pixels
    return width + 2 * pad, height + 2 * pad


_TAGS = re.compile(r"<[^>]*>|\{[^}]*\}")
RTL_START, RTL_END = "‫", "‬"  # the embedding marks that start and end a right-to-left line


def mark_right_to_left(text: str) -> str:
    """`text` with each line that starts in a right-to-left script (Arabic, Hebrew...) wrapped in
    an embedding mark. drawtext lays every line out as left to right unless told otherwise, which
    would put the Latin words and numbers of an Arabic sentence on the wrong side of it; with the
    mark the line is ordered right to left, like any other program shows it. The marks themselves
    are not drawn. The subtitles filter (libass) has the same habit, so subtitle lines get the
    marks too."""
    lines = []
    for line in text.split("\n"):
        words = _TAGS.sub("", line)  # <i> and {\i1} are markup, not letters
        first = next(
            (kind for kind in map(unicodedata.bidirectional, words) if kind in ("L", "R", "AL")),
            None)
        lines.append(RTL_START + line + RTL_END if first in ("R", "AL") else line)
    return "\n".join(lines)


def text_filter(item: TextItem, frame: tuple[int, int], total: Optional[float]) -> str:
    """The drawtext filter for one text item on a `frame` (width, height) video `total` seconds
    long (None if unknown), or "" when it draws nothing. The text goes through a file, so no
    character in it can upset the filter syntax; `expansion=none` stops drawtext reading "%{...}"
    as a command."""
    if not drawable(item):
        return ""
    wrapped, path, pixels = laid_out(item, frame)
    data = mark_right_to_left(wrapped).encode("utf-8")
    options = [
        f"fontfile={_path_value(Path(path))}" if path else "font=Sans",
        f"textfile={_path_value(_scratch_file(data, '.txt'))}", "expansion=none", "text_shaping=1",
        f"fontsize={_n(pixels)}", f"fontcolor={_hex(item.color)}",
    ]
    pad = 0.0
    if item.outline > 0:
        border = max(int(round(item.outline * pixels)), 1)
        options += [f"borderw={border}", f"bordercolor={_hex(item.outline_color)}"]
        pad = border
    if item.box:
        spread = int(round(BOX_PADDING * pixels))
        options += [
            "box=1", f"boxcolor={_hex(item.box_color)}@{_n(item.box_opacity, 3)}",
            f"boxborderw={spread}"
        ]
        pad = max(pad, spread)
    if "\n" in wrapped and item.align != "left":
        options.append(f"text_align={item.align}")
    # The middle of the text sits at (x, y), but the text (with its box and outline) stays inside
    # the frame.
    options.append(f"x='max({_n(pad)},min(w-text_w-{_n(pad)},w*{_n(item.x)}-text_w/2))'")
    options.append(f"y='max({_n(pad)},min(h-text_h-{_n(pad)},h*{_n(item.y)}-text_h/2))'")
    end = item.end if item.end is not None else total
    if item.end is not None:
        options.append(f"enable='between(t,{_n(item.start, 3)},{_n(item.end, 3)})'")
    elif item.start > 0:
        options.append(f"enable='gte(t,{_n(item.start, 3)})'")
    fade = _fade_alpha(item, end)
    if fade:
        options.append(f"alpha='{fade}'")
    return "drawtext=" + ":".join(options)


def _fade_alpha(item: TextItem, end: Optional[float]) -> str:
    """The expression that fades a text in and out over the time it is shown, or "" for no fade."""
    shown = (end - item.start) if end is not None else None
    fade_in, fade_out = item.fade_in, item.fade_out if end is not None else 0.0
    # two fades can't be longer than the text
    if shown is not None and fade_in + fade_out > shown and shown > 0:
        fade_in, fade_out = fade_in * shown / (fade_in +
                                               fade_out), fade_out * shown / (fade_in + fade_out)
    parts = []
    if fade_in > 0:
        parts.append(f"(t-{_n(item.start, 3)})/{_n(fade_in, 3)}")
    if fade_out > 0:
        parts.append(f"({_n(end, 3)}-t)/{_n(fade_out, 3)}")
    if not parts:
        return ""
    expression = "1"
    for part in parts:
        expression = f"min({expression},{part})"
    return expression
