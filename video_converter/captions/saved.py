"""Loading saved settings: whatever is wrong in a saved file is dropped or put back to the
default."""

from __future__ import annotations

import re
from typing import Any, Optional

from ..fonts import DEFAULT_FONT, safe_font_name
from ..timing import FADE_MAX
from .model import (
    ALIGNMENTS, BOX_OPACITY, LOGO_CORNERS, LOGO_MARGIN, LOGO_OPACITY, LOGO_SIZE, MAX_TEXTS,
    MAX_TEXT_LENGTH, OUTLINE_SIZE, SUBTITLE_POSITIONS, SUBTITLE_SIZE, TEXT_SIZE, Logo, Subtitles,
    TextItem,
)

_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")


def _is_number(value: Any) -> bool:
    return isinstance(
        value,
        (int, float)) and not isinstance(value, bool) and value == value and abs(value) < 1e9


def _in(low: float, high: float):
    return lambda value: _is_number(value) and low <= value <= high


def _text(limit: int):
    return lambda value: isinstance(value, str) and len(value) <= limit


def _one_of(choices):
    return lambda value: isinstance(value, str) and value in choices


_TEXT_RULES = {
    "text": _text(MAX_TEXT_LENGTH),
    "font": lambda v: isinstance(v, str) and 0 < len(v) <= 100,
    "size": _in(*TEXT_SIZE),
    "color": lambda v: isinstance(v, str) and bool(_COLOR.match(v)),
    "outline_color": lambda v: isinstance(v, str) and bool(_COLOR.match(v)),
    "outline": _in(*OUTLINE_SIZE),
    "box": lambda v: isinstance(v, bool),
    "box_color": lambda v: isinstance(v, str) and bool(_COLOR.match(v)),
    "box_opacity": _in(*BOX_OPACITY),
    "bold": lambda v: isinstance(v, bool),
    "align": _one_of(ALIGNMENTS),
    "x": _in(0, 1),
    "y": _in(0, 1),
    "start": _in(0, 1e7),
    "end": lambda v: v is None or _in(0, 1e7)(v),
    "fade_in": _in(0, FADE_MAX),
    "fade_out": _in(0, FADE_MAX),
}
_LOGO_RULES = {
    "file": _text(1024),
    "corner": _one_of(LOGO_CORNERS),
    "x": _in(0, 1),
    "y": _in(0, 1),
    "size": _in(*LOGO_SIZE),
    "opacity": _in(*LOGO_OPACITY),
    "margin": _in(*LOGO_MARGIN),
}
_SUBTITLE_RULES = {
    "file": _text(1024),
    "font": _TEXT_RULES["font"],
    "size": _in(*SUBTITLE_SIZE),
    "color": _TEXT_RULES["color"],
    "outline_color": _TEXT_RULES["outline_color"],
    "outline": _in(*OUTLINE_SIZE),
    "bold": _TEXT_RULES["bold"],
    "position": _one_of(SUBTITLE_POSITIONS),
    "own_style": lambda v: isinstance(v, bool),
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
    if isinstance(item, Subtitles):
        # The name goes into the subtitles filter's style list, where "," and "=" would start
        # another key.
        item.font = safe_font_name(item.font) or DEFAULT_FONT
    return item


def load_texts(value: Any) -> Optional[list[TextItem]]:
    """The texts of saved settings, or None if the saved value isn't a list."""
    if not isinstance(value, (list, tuple)):
        return None
    return [
        _load(TextItem, entry, _TEXT_RULES) for entry in value[:MAX_TEXTS]
        if isinstance(entry, dict)
    ]


def load_logo(value: Any) -> Logo:
    return _load(Logo, value, _LOGO_RULES)


def load_subtitles(value: Any) -> Subtitles:
    return _load(Subtitles, value, _SUBTITLE_RULES)
