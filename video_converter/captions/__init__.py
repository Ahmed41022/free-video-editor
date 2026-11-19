"""Text, a logo and subtitles drawn into the picture.

Everything here builds ffmpeg filter graphs (and the small temporary files they read); running them
is up to the `video` package. The overlays are the last step of the picture, after the reframing,
the adjustments and the fades, so a look doesn't tint them and a fade to black doesn't hide them.

Times: a text item's start and end are measured on the exported video's own timeline (after cuts
and speed), like the picture fades. Subtitle files were made for the original video, so their times
are source times; `retimed_subtitles` rewrites them onto the export's timeline, dropping what was
cut out.
"""

from __future__ import annotations

from .cues import Cue, encode_srt, format_srt, parse_cues, subtitle_kind, write_srt
from .encoding import (
    LEGACY_CODEPAGES, code_page_score, decode_text, read_text, read_text_with_encoding,
)
from .escaping import filter_value
from .logo import logo_graph, logo_position, logo_source
from .model import (
    ALIGNMENTS, BOX_OPACITY, BOX_PADDING, IMAGE_EXTENSIONS, LOGO_CORNERS, LOGO_MARGIN,
    LOGO_OPACITY, LOGO_SIZE, MAX_TEXTS, MAX_TEXT_LENGTH, OUTLINE_SIZE, SUBTITLE_EXTENSIONS,
    SUBTITLE_POSITIONS, SUBTITLE_SIZE, TEXT_SIZE, WRAP_WIDTH, Logo, Subtitles, TextItem,
    keep_own_captions,
)
from .overlay import overlay_graph
from .retime import SpanMapper, map_span, retime_ass, retimed_subtitles, srt_cue_text
from .saved import load_logo, load_subtitles, load_texts
from .scratch import scratch_dir
from .subtitles import subtitle_filter
from .text import (
    RTL_END, RTL_START, clean_text, drawable, drawable_text, has_overlays, laid_out,
    mark_right_to_left, text_box, text_filter, text_pixels,
)

__all__ = [
    "ALIGNMENTS", "BOX_OPACITY", "BOX_PADDING", "IMAGE_EXTENSIONS", "LEGACY_CODEPAGES",
    "LOGO_CORNERS", "LOGO_MARGIN", "LOGO_OPACITY", "LOGO_SIZE", "MAX_TEXTS", "MAX_TEXT_LENGTH",
    "OUTLINE_SIZE", "RTL_END", "RTL_START", "SUBTITLE_EXTENSIONS", "SUBTITLE_POSITIONS",
    "SUBTITLE_SIZE", "TEXT_SIZE", "WRAP_WIDTH", "Cue", "Logo", "SpanMapper", "Subtitles",
    "TextItem", "clean_text", "code_page_score", "decode_text", "drawable", "drawable_text",
    "encode_srt", "filter_value", "format_srt", "has_overlays", "keep_own_captions", "laid_out",
    "load_logo", "load_subtitles", "load_texts", "logo_graph", "logo_position", "logo_source",
    "map_span", "mark_right_to_left", "overlay_graph", "parse_cues", "read_text",
    "read_text_with_encoding", "retime_ass", "retimed_subtitles", "scratch_dir", "srt_cue_text",
    "subtitle_filter", "subtitle_kind", "text_box", "text_filter", "text_pixels", "write_srt",
]
