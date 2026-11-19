"""The settings of a text, a logo and subtitles, and the limits they are kept within."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Optional

from ..fonts import DEFAULT_FONT
from ..i18n import N_

TEXT_SIZE = (0.02, 0.4)  # of the frame's height
OUTLINE_SIZE = (0.0, 0.3)  # of the text size
BOX_OPACITY = (0.0, 1.0)
LOGO_SIZE = (0.03, 0.8)  # of the frame's width
LOGO_OPACITY = (0.05, 1.0)
LOGO_MARGIN = (0.0, 0.2)  # of the frame's width
SUBTITLE_SIZE = (0.02, 0.15)  # of the frame's height
MAX_TEXTS = 50
MAX_TEXT_LENGTH = 2000
WRAP_WIDTH = 0.92  # a line of text is wrapped before it is wider than this much of the frame
BOX_PADDING = 0.3  # of the text size, around the text inside its background box

ALIGNMENTS = {"left": N_("Left"), "center": N_("Centered"), "right": N_("Right")}
LOGO_CORNERS = {
    "top-left": N_("Top left"),
    "top-right": N_("Top right"),
    "bottom-left": N_("Bottom left"),
    "bottom-right": N_("Bottom right"),
    "center": N_("Center"),
    "custom": N_("Where I put it"),
}
SUBTITLE_POSITIONS = {"bottom": N_("Bottom"), "middle": N_("Middle"), "top": N_("Top")}
SUBTITLE_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa")
IMAGE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".webp", ".bmp"
)  # what the logo picker offers; a .gif given another way is looped


@dataclass
class TextItem:
    """One piece of text on the video."""
    text: str = "Your text"
    font: str = DEFAULT_FONT
    size: float = 0.06  # the text's height, as a share of the video's height
    color: str = "#ffffff"
    outline_color: str = "#000000"
    outline: float = 0.08  # the outline's width, as a share of the text size; 0 = none
    box: bool = False  # a background box behind the text
    box_color: str = "#000000"
    box_opacity: float = 0.6
    bold: bool = False
    align: str = "center"  # how the lines of a longer text line up with each other
    x: float = 0.5  # where the middle of the text sits, as a share of the frame
    y: float = 0.85
    start: float = 0.0  # seconds on the exported video
    end: Optional[float] = None  # None = to the end of the video
    fade_in: float = 0.0
    fade_out: float = 0.0


@dataclass
class Logo:
    """A picture (a logo or watermark) shown over the whole video."""
    file: str = ""  # "" = none
    corner: str = "bottom-right"  # a LOGO_CORNERS key; "custom" uses x and y
    x: float = 0.9  # where the middle of the logo sits, when the corner is "custom"
    y: float = 0.9
    size: float = 0.15  # its width, as a share of the video's width
    opacity: float = 1.0
    margin: float = 0.03  # distance from the edge in a corner, as a share of the video's width


@dataclass
class Subtitles:
    """A subtitle file burned into the picture, and how it looks."""
    file: str = ""  # "" = none
    font: str = DEFAULT_FONT
    size: float = 0.05  # as a share of the video's height
    color: str = "#ffffff"
    outline_color: str = "#000000"
    outline: float = 0.08  # as a share of the text size
    bold: bool = False
    position: str = "bottom"  # a SUBTITLE_POSITIONS key
    # an .ass/.ssa file keeps its own design (fonts, colors, places) and ignores the above
    own_style: bool = True


# Settings that belong to one video and aren't copied by "Apply to all files" or a preset: the
# texts and the subtitle FILE (a subtitle file was made for one video). The logo and the subtitle
# style are shared.
def keep_own_captions(result_edit: Any, target_edit: Any) -> None:
    """Put `target_edit`'s own texts and subtitle file into `result_edit`."""
    result_edit.texts = copy.deepcopy(target_edit.texts)
    result_edit.subtitles.file = target_edit.subtitles.file
