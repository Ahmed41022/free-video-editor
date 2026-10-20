"""Text, a logo and subtitles drawn into the picture.

Everything here builds ffmpeg filter graphs (and the small temporary files they read); running them
is up to `video.py`. The overlays are the last step of the picture, after the reframing, the
adjustments and the fades, so a look doesn't tint them and a fade to black doesn't hide them.

Times: a text item's start and end are measured on the exported video's own timeline (after cuts and
speed), like the picture fades. Subtitle files were made for the original video, so their times are
source times; `retimed_subtitles` rewrites them onto the export's timeline, dropping what was cut out.
"""

from __future__ import annotations

import atexit
import copy
import hashlib
import logging
import re
import shutil
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .core import ConversionError
from .i18n import N_, tr
from .fonts import DEFAULT_FONT, load_font, pick_font, text_size, windows_fonts_dir, wrap_text
from .timing import FADE_MAX, Segment, clamp_speed, output_duration

log = logging.getLogger(__name__)

TEXT_SIZE = (0.02, 0.4)         # of the frame's height
OUTLINE_SIZE = (0.0, 0.3)       # of the text size
BOX_OPACITY = (0.0, 1.0)
LOGO_SIZE = (0.03, 0.8)         # of the frame's width
LOGO_OPACITY = (0.05, 1.0)
LOGO_MARGIN = (0.0, 0.2)        # of the frame's width
SUBTITLE_SIZE = (0.02, 0.15)    # of the frame's height
MAX_TEXTS = 50
MAX_TEXT_LENGTH = 2000
WRAP_WIDTH = 0.92               # a line of text is wrapped before it is wider than this much of the frame
BOX_PADDING = 0.3               # of the text size, around the text inside its background box

ALIGNMENTS = {"left": N_("Left"), "center": N_("Centered"), "right": N_("Right")}
LOGO_CORNERS = {
    "top-left": N_("Top left"), "top-right": N_("Top right"), "bottom-left": N_("Bottom left"),
    "bottom-right": N_("Bottom right"), "center": N_("Center"), "custom": N_("Where I put it"),
}
SUBTITLE_POSITIONS = {"bottom": N_("Bottom"), "middle": N_("Middle"), "top": N_("Top")}
SUBTITLE_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


@dataclass
class TextItem:
    """One piece of text on the video."""
    text: str = "Your text"
    font: str = DEFAULT_FONT
    size: float = 0.06            # the text's height, as a share of the video's height
    color: str = "#ffffff"
    outline_color: str = "#000000"
    outline: float = 0.08         # the outline's width, as a share of the text size; 0 = none
    box: bool = False             # a background box behind the text
    box_color: str = "#000000"
    box_opacity: float = 0.6
    bold: bool = False
    align: str = "center"         # how the lines of a longer text line up with each other
    x: float = 0.5                # where the middle of the text sits, as a share of the frame
    y: float = 0.85
    start: float = 0.0            # seconds on the exported video
    end: Optional[float] = None   # None = to the end of the video
    fade_in: float = 0.0
    fade_out: float = 0.0


@dataclass
class Logo:
    """A picture (a logo or watermark) shown over the whole video."""
    file: str = ""                # "" = none
    corner: str = "bottom-right"  # a LOGO_CORNERS key; "custom" uses x and y
    x: float = 0.9                # where the middle of the logo sits, when the corner is "custom"
    y: float = 0.9
    size: float = 0.15            # its width, as a share of the video's width
    opacity: float = 1.0
    margin: float = 0.03          # distance from the edge in a corner, as a share of the video's width


@dataclass
class Subtitles:
    """A subtitle file burned into the picture, and how it looks."""
    file: str = ""                # "" = none
    font: str = DEFAULT_FONT
    size: float = 0.05            # as a share of the video's height
    color: str = "#ffffff"
    outline_color: str = "#000000"
    outline: float = 0.08         # as a share of the text size
    bold: bool = False
    position: str = "bottom"      # a SUBTITLE_POSITIONS key


# Settings that belong to one video and aren't copied by "Apply to all files" or a preset: the texts
# and the subtitle FILE (a subtitle file was made for one video). The logo and the subtitle style are shared.
def keep_own_captions(result_edit: Any, target_edit: Any) -> None:
    """Put `target_edit`'s own texts and subtitle file into `result_edit`."""
    result_edit.texts = copy.deepcopy(target_edit.texts)
    result_edit.subtitles.file = target_edit.subtitles.file


def has_overlays(texts: list[TextItem], logo: Logo, subtitles: Subtitles) -> bool:
    return bool(logo.file or subtitles.file or any(drawable(item) for item in texts))


def drawable(item: TextItem) -> bool:
    """Whether a text item draws anything: it has text and a time to be seen."""
    return bool(item.text.strip()) and (item.end is None or item.end > item.start)


# ----- loading saved settings --------------------------------------------------

_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) < 1e9


def _in(low: float, high: float):
    return lambda value: _is_number(value) and low <= value <= high


def _text(limit: int):
    return lambda value: isinstance(value, str) and len(value) <= limit


def _one_of(choices):
    return lambda value: isinstance(value, str) and value in choices


_TEXT_RULES = {
    "text": _text(MAX_TEXT_LENGTH), "font": lambda v: isinstance(v, str) and 0 < len(v) <= 100,
    "size": _in(*TEXT_SIZE), "color": lambda v: isinstance(v, str) and bool(_COLOR.match(v)),
    "outline_color": lambda v: isinstance(v, str) and bool(_COLOR.match(v)), "outline": _in(*OUTLINE_SIZE),
    "box": lambda v: isinstance(v, bool), "box_color": lambda v: isinstance(v, str) and bool(_COLOR.match(v)),
    "box_opacity": _in(*BOX_OPACITY), "bold": lambda v: isinstance(v, bool), "align": _one_of(ALIGNMENTS),
    "x": _in(0, 1), "y": _in(0, 1), "start": _in(0, 1e7), "end": lambda v: v is None or _in(0, 1e7)(v),
    "fade_in": _in(0, FADE_MAX), "fade_out": _in(0, FADE_MAX),
}
_LOGO_RULES = {
    "file": _text(1024), "corner": _one_of(LOGO_CORNERS), "x": _in(0, 1), "y": _in(0, 1),
    "size": _in(*LOGO_SIZE), "opacity": _in(*LOGO_OPACITY), "margin": _in(*LOGO_MARGIN),
}
_SUBTITLE_RULES = {
    "file": _text(1024), "font": _TEXT_RULES["font"], "size": _in(*SUBTITLE_SIZE), "color": _TEXT_RULES["color"],
    "outline_color": _TEXT_RULES["outline_color"], "outline": _in(*OUTLINE_SIZE), "bold": _TEXT_RULES["bold"],
    "position": _one_of(SUBTITLE_POSITIONS),
}


def _load(cls, data: Any, rules: dict):
    """An instance of dataclass `cls` from saved `data`: bad or unknown values are ignored."""
    item = cls()
    if not isinstance(data, dict):
        return item
    for name, check in rules.items():
        if name in data and check(data[name]):
            value = data[name]
            setattr(item, name, float(value) if _is_number(value) else value)
    if isinstance(item, TextItem) and item.end is not None and item.end <= item.start:
        item.end = None
    return item


def load_texts(value: Any) -> Optional[list[TextItem]]:
    """The texts of saved settings, or None if the saved value isn't a list."""
    if not isinstance(value, (list, tuple)):
        return None
    return [_load(TextItem, entry, _TEXT_RULES) for entry in value[:MAX_TEXTS] if isinstance(entry, dict)]


def load_logo(value: Any) -> Logo:
    return _load(Logo, value, _LOGO_RULES)


def load_subtitles(value: Any) -> Subtitles:
    return _load(Subtitles, value, _SUBTITLE_RULES)


# ----- temporary files and filter escaping ---------------------------------------

_scratch: Optional[Path] = None
_scratch_lock = threading.Lock()


def scratch_dir() -> Path:
    """A folder for the small files the filters read (text, rewritten subtitles, logo copies). It is
    made on first use and removed when the program ends."""
    global _scratch
    with _scratch_lock:
        if _scratch is None or not _scratch.is_dir():
            _scratch = Path(tempfile.mkdtemp(prefix="fve-captions-"))
            atexit.register(shutil.rmtree, _scratch, True)
        return _scratch


def _scratch_file(data: bytes, suffix: str) -> Path:
    """`data` saved under a name made from its content, so the same content is written only once."""
    path = scratch_dir() / (hashlib.sha1(data).hexdigest()[:20] + suffix)
    if not path.is_file():
        temp = path.with_name(path.name + ".part")
        temp.write_bytes(data)
        temp.replace(path)
    return path


def filter_value(text: str) -> str:
    """`text` as the value of a filter option, safe against every character ffmpeg treats specially. It
    goes through the two levels of escaping the filter syntax has: the option's and the graph's."""
    for chars in ("\\':", "\\',;[]"):
        text = "".join("\\" + ch if ch in chars else ch for ch in text)
    return text


def _path_value(path: Path) -> str:
    return filter_value(str(path).replace("\\", "/"))


def _n(value: float, places: int = 4) -> str:
    return f"{value:.{places}f}".rstrip("0").rstrip(".") or "0"


def _hex(color: str) -> str:
    return "0x" + color.lstrip("#").upper()


# ----- text -----------------------------------------------------------------------

def text_pixels(item: TextItem, frame: tuple[int, int]) -> int:
    return max(int(round(item.size * frame[1])), 6)


def laid_out(item: TextItem, frame: tuple[int, int]) -> tuple[str, Optional[str], float]:
    """(the text with its line breaks, the font file, the text size in pixels) for drawing `item` in a
    `frame` (width, height)."""
    pixels = text_pixels(item, frame)
    path = pick_font(item.font, item.bold, item.text)
    font = load_font(path) if path else None
    padding = 2 * (BOX_PADDING if item.box else item.outline) * pixels
    return wrap_text(item.text, font, pixels, frame[0] * WRAP_WIDTH - padding), path, float(pixels)


def text_box(item: TextItem, frame: tuple[int, int]) -> tuple[float, float]:
    """About how big the drawn text is in a `frame` (width, height), padding and outline included: what
    the editor draws its handle around. ffmpeg does the exact drawing, so this is close, not exact."""
    wrapped, path, pixels = laid_out(item, frame)
    width, height = text_size(wrapped, load_font(path) if path else None, pixels)
    pad = (BOX_PADDING if item.box else item.outline) * pixels
    return width + 2 * pad, height + 2 * pad


_TAGS = re.compile(r"<[^>]*>|\{[^}]*\}")
RTL_START, RTL_END = "‫", "‬"      # the embedding marks that start and end a right-to-left line


def mark_right_to_left(text: str) -> str:
    """`text` with each line that starts in a right-to-left script (Arabic, Hebrew...) wrapped in an
    embedding mark. drawtext lays every line out as left to right unless told otherwise, which would put
    the Latin words and numbers of an Arabic sentence on the wrong side of it; with the mark the line is
    ordered right to left, like any other program shows it. The marks themselves are not drawn. The
    subtitles filter (libass) has the same habit, so subtitle lines get the marks too."""
    lines = []
    for line in text.split("\n"):
        words = _TAGS.sub("", line)         # <i> and {\i1} are markup, not letters
        first = next((kind for kind in map(unicodedata.bidirectional, words) if kind in ("L", "R", "AL")), None)
        lines.append(RTL_START + line + RTL_END if first in ("R", "AL") else line)
    return "\n".join(lines)


def text_filter(item: TextItem, frame: tuple[int, int], total: Optional[float]) -> str:
    """The drawtext filter for one text item on a `frame` (width, height) video `total` seconds long
    (None if unknown), or "" when it draws nothing. The text goes through a file, so no character in
    it can upset the filter syntax; `expansion=none` stops drawtext reading "%{...}" as a command."""
    if not drawable(item):
        return ""
    wrapped, path, pixels = laid_out(item, frame)
    data = mark_right_to_left(wrapped).encode("utf-8")
    options = [
        f"fontfile={_path_value(Path(path))}" if path else "font=Sans",
        f"textfile={_path_value(_scratch_file(data, '.txt'))}",
        "expansion=none", "text_shaping=1",
        f"fontsize={_n(pixels)}", f"fontcolor={_hex(item.color)}",
    ]
    pad = 0.0
    if item.outline > 0:
        border = max(int(round(item.outline * pixels)), 1)
        options += [f"borderw={border}", f"bordercolor={_hex(item.outline_color)}"]
        pad = border
    if item.box:
        spread = int(round(BOX_PADDING * pixels))
        options += ["box=1", f"boxcolor={_hex(item.box_color)}@{_n(item.box_opacity, 3)}", f"boxborderw={spread}"]
        pad = max(pad, spread)
    if "\n" in wrapped and item.align != "left":
        options.append(f"text_align={item.align}")
    # The middle of the text sits at (x, y), but the text (with its box and outline) stays inside the frame.
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
    if shown is not None and fade_in + fade_out > shown and shown > 0:      # two fades can't be longer than the text
        fade_in, fade_out = fade_in * shown / (fade_in + fade_out), fade_out * shown / (fade_in + fade_out)
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


# ----- logo ------------------------------------------------------------------------

def logo_source(logo: Logo) -> Path:
    """A copy of the logo's image under a plain name (so odd characters in its path can't matter)."""
    path = Path(logo.file)
    if not path.is_file():
        raise ConversionError(tr("The logo picture can't be found: {name}", name=path.name or logo.file))
    stat = path.stat()
    key = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    target = scratch_dir() / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + (path.suffix.lower() or ".png"))
    if not target.is_file():
        shutil.copyfile(path, target)
    return target


def logo_position(logo: Logo, frame: tuple[int, int]) -> tuple[str, str]:
    """The overlay's x and y expressions (W, H = the video, w, h = the logo)."""
    margin = int(round(logo.margin * frame[0]))
    if logo.corner == "custom":
        return (f"max(0,min(W-w,W*{_n(logo.x)}-w/2))", f"max(0,min(H-h,H*{_n(logo.y)}-h/2))")
    vertical, _, horizontal = logo.corner.partition("-")
    if logo.corner == "center":
        return "(W-w)/2", "(H-h)/2"
    x = str(margin) if horizontal == "left" else f"W-w-{margin}"
    y = str(margin) if vertical == "top" else f"H-h-{margin}"
    return x, y


def logo_graph(src: str, out: str, logo: Logo, frame: tuple[int, int], tag: str) -> list[str]:
    """The filters that lay the logo over `src`: its picture is read from the file, scaled and made
    see-through by itself, then overlaid."""
    width = max(int(round(logo.size * frame[0])), 4)
    opacity = f",colorchannelmixer=aa={_n(logo.opacity, 3)}" if logo.opacity < 1 else ""
    x, y = logo_position(logo, frame)
    return [
        f"movie=filename={_path_value(logo_source(logo))},scale={width}:-1:flags=lanczos,format=rgba{opacity}[logo{tag}]",
        f"{src}[logo{tag}]overlay=x='{x}':y='{y}'{out}",
    ]


# ----- subtitles ---------------------------------------------------------------------

@dataclass
class Cue:
    start: float
    end: float
    text: str


_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})")
_ARROW = re.compile(r"([\d:.,]+)\s*-->\s*([\d:.,]+)")
_VTT_TAG = re.compile(r"<(?!/?(?:b|i|u|font)\b)[^>]*>", re.IGNORECASE)


def _seconds(match: re.Match) -> float:
    hours, minutes, seconds, fraction = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction.ljust(3, "0")) / 1000


def read_text(path: Path) -> str:
    """The text of a file, whatever Unicode form it is in; a file in an old code page is read as Windows-1252."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def subtitle_kind(path: Path, text: str) -> str:
    """"srt", "vtt" or "ass" for a subtitle file's contents."""
    suffix = path.suffix.lower()
    if suffix in (".ass", ".ssa") or "[Script Info]" in text[:2000]:
        return "ass"
    if suffix == ".vtt" or text.lstrip().startswith("WEBVTT"):
        return "vtt"
    return "srt"


def parse_cues(text: str, kind: str = "srt") -> list[Cue]:
    """The cues of an SRT or WebVTT file, in file order. A block with no usable time is skipped."""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip("﻿\n")):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            found = _ARROW.search(line)
            if found:
                start, end = _TIME.fullmatch(found.group(1)), _TIME.fullmatch(found.group(2))
                break
        else:
            continue
        if not (start and end):
            continue
        body = "\n".join(lines[i + 1:]).strip("\n")
        if kind == "vtt":
            body = _VTT_TAG.sub("", body)
        if body.strip():
            cues.append(Cue(_seconds(start), _seconds(end), body))
    return cues


def _srt_time(seconds: float) -> str:
    ms = int(round(max(seconds, 0.0) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def format_srt(cues: list[Cue]) -> str:
    """The text of an .srt file for `cues`, numbered in order. A blank line inside a cue would end it
    early in the file, so those are dropped."""
    blocks = []
    for number, cue in enumerate(cues, 1):
        body = "\n".join(line.strip() for line in cue.text.replace("\r", "").split("\n") if line.strip())
        blocks.append(f"{number}\n{_srt_time(cue.start)} --> {_srt_time(cue.end)}\n{body}\n")
    return "\n".join(blocks)


def write_srt(cues: list[Cue], path: Path) -> None:
    """Save `cues` as a UTF-8 .srt file at `path` (written aside first, so a failure can't leave half a file)."""
    temp = path.with_name(path.name + ".part")
    try:
        temp.write_bytes(format_srt(cues).encode("utf-8"))
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def map_span(start: float, end: float, segments: list[Segment], speed: float = 1.0, reverse: bool = False,
             ) -> list[tuple[float, float]]:
    """Where the source time span start..end lands on the export's timeline: one piece for each kept
    part it overlaps (so a cue that runs across a cut shows before and after it), nothing if it was cut
    out. Pieces that meet are joined."""
    speed = clamp_speed(speed)
    total = output_duration(segments, speed)
    pieces: list[tuple[float, float]] = []
    done = 0.0
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
    joined: list[tuple[float, float]] = []
    for lo, hi in pieces:
        if joined and lo - joined[-1][1] < 0.001:
            joined[-1] = (joined[-1][0], max(joined[-1][1], hi))
        else:
            joined.append((lo, hi))
    return [(lo, hi) for lo, hi in joined if hi - lo >= 0.02]


def _ass_seconds(text: str) -> Optional[float]:
    found = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2})[.:](\d{1,3})\s*", text)
    if not found:
        return None
    hours, minutes, seconds, fraction = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction) / 10 ** len(fraction)


def _ass_time(seconds: float) -> str:
    cs = int(round(max(seconds, 0.0) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def retime_ass(text: str, mapper) -> tuple[str, int]:
    """An ASS script with every Dialogue line moved by `mapper(start, end) -> [(start, end), ...]`
    (the same text on each piece), and how many lines are left. Styles and everything else stay as they were."""
    out, left = [], 0
    columns: list[str] = []
    in_events = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("["):
            in_events = stripped.lower() == "[events]"
        if in_events and stripped.lower().startswith("format:"):
            columns = [c.strip().lower() for c in stripped.split(":", 1)[1].split(",")]
        if not (in_events and stripped.lower().startswith("dialogue:") and "start" in columns and "end" in columns):
            out.append(line)
            continue
        fields = line.split(":", 1)[1].lstrip().split(",", len(columns) - 1)
        if len(fields) < len(columns):
            continue
        a, b = columns.index("start"), columns.index("end")
        start, end = _ass_seconds(fields[a]), _ass_seconds(fields[b])
        if start is None or end is None:
            continue
        for lo, hi in mapper(start, end):
            moved = list(fields)
            moved[a], moved[b] = _ass_time(lo), _ass_time(hi)
            out.append("Dialogue: " + ",".join(moved))
            left += 1
    return "\n".join(out), left


def _play_res_y(text: str) -> int:
    found = re.search(r"^\s*PlayResY:\s*(\d+)", text, re.MULTILINE | re.IGNORECASE)
    return int(found.group(1)) if found else 288


def retimed_subtitles(subtitles: Subtitles, segments: list[Segment], speed: float = 1.0,
                      reverse: bool = False) -> tuple[Path, str, int]:
    """The subtitle file rewritten for the export: (the new file, "srt" or "ass", the height of
    its script, which the font size is relative to). `segments` are the kept parts of the source."""
    path = Path(subtitles.file)
    try:
        text = read_text(path)
    except OSError:
        raise ConversionError(tr("The subtitle file can't be read: {name}", name=path.name or subtitles.file)) from None
    kind = subtitle_kind(path, text)

    def mapper(start: float, end: float) -> list[tuple[float, float]]:
        return map_span(start, end, segments, speed, reverse)

    if kind == "ass":
        rewritten, _ = retime_ass(text, mapper)
        return _scratch_file(rewritten.encode("utf-8"), ".ass"), "ass", _play_res_y(text)
    lines, number = [], 0
    moved = []
    for cue in parse_cues(text, kind):
        moved += [(lo, hi, mark_right_to_left(cue.text)) for lo, hi in mapper(cue.start, cue.end)]
    for lo, hi, body in sorted(moved, key=lambda m: (m[0], m[1])):
        number += 1
        lines += [str(number), f"{_srt_time(lo)} --> {_srt_time(hi)}", body, ""]
    if not number:
        # An empty file would make the filter fail; a cue that is never on screen does nothing.
        lines = ["1", "00:00:00,000 --> 00:00:00,020", " ", ""]
    return _scratch_file("\n".join(lines).encode("utf-8"), ".srt"), "srt", 288


def _ass_color(color: str) -> str:
    """"#RRGGBB" as the &HAABBGGRR& that ASS styles use (no transparency)."""
    return f"&H00{color[5:7]}{color[3:5]}{color[1:3]}&".upper()


def subtitle_filter(subtitles: Subtitles, segments: list[Segment], speed: float = 1.0, reverse: bool = False) -> str:
    """The subtitles filter, drawing the file on the export's timeline in the chosen style."""
    path, kind, script_height = retimed_subtitles(subtitles, segments, speed, reverse)
    size = subtitles.size * script_height
    style = ",".join([
        f"FontName={subtitles.font}", f"FontSize={_n(size, 2)}", f"PrimaryColour={_ass_color(subtitles.color)}",
        f"OutlineColour={_ass_color(subtitles.outline_color)}", "BorderStyle=1",
        f"Outline={_n(subtitles.outline * size, 2)}", "Shadow=0", f"Bold={-1 if subtitles.bold else 0}",
        f"Alignment={ {'bottom': 2, 'middle': 10, 'top': 6}[subtitles.position] }",      # force_style numbers them the SSA way
    ])
    options = [f"filename={_path_value(path)}"]
    fonts = windows_fonts_dir()
    if fonts:
        options.append(f"fontsdir={_path_value(fonts)}")      # so the fonts are found without any font setup
    options.append(f"force_style={filter_value(style)}")
    return "subtitles=" + ":".join(options)


# ----- everything together -----------------------------------------------------------

def overlay_graph(src: str, out: str, texts: list[TextItem], logo: Logo, subtitles: Subtitles,
                  frame: tuple[int, int], segments: list[Segment], speed: float = 1.0, reverse: bool = False,
                  tag: str = "") -> list[str]:
    """Graph statements that draw the logo, the subtitles and the texts (in that order, so text is on
    top) over stream `src`, ending in `out`. [] when there is nothing to draw.

    `frame` is the video's width and height at this point, `segments` the kept parts of the source
    (see `VideoEdit.segments`), which the subtitles' source times are moved through. The texts' times
    are already on the export's timeline."""
    chain = []
    if subtitles.file:
        chain.append(subtitle_filter(subtitles, segments, speed, reverse))
    total = output_duration(segments, speed) if segments else None
    chain += [f for f in (text_filter(item, frame, total) for item in texts) if f]
    statements: list[str] = []
    current = src
    if logo.file:
        mid = f"[lg{tag}]" if chain else out
        statements += logo_graph(current, mid, logo, frame, tag)
        current = mid
    if chain:
        statements.append(f"{current}{','.join(chain)}{out}")
    return statements
