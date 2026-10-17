"""Per-file edit settings, presets, and turning settings into export jobs."""

from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .core import DEFAULT_QUALITY, QUALITY_PRESETS, output_path_for
from .video import FIT_MODES, RESOLUTIONS, VIDEO_QUALITY, VideoEdit, output_tag

OUTPUT_VIDEO = "video"
OUTPUT_AUDIO = "audio"


@dataclass
class ClipSettings:
    """Everything the user can change for one file."""
    output: str = OUTPUT_VIDEO                 # OUTPUT_VIDEO (MP4) or OUTPUT_AUDIO (MP3)
    edit: VideoEdit = field(default_factory=VideoEdit)
    audio_quality: str = DEFAULT_QUALITY       # a QUALITY_PRESETS key, used for MP3 output

    def copy(self) -> "ClipSettings":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        aspect = data["edit"]["aspect"]
        data["edit"]["aspect"] = list(aspect) if aspect else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClipSettings":
        """Build settings from `to_dict()` output, ignoring unknown keys and bad values
        so presets saved by other versions still load."""
        settings = cls()
        if not isinstance(data, dict):
            return settings
        if _is_str(data.get("output")) and data["output"] in (OUTPUT_VIDEO, OUTPUT_AUDIO):
            settings.output = data["output"]
        if _is_str(data.get("audio_quality")) and data["audio_quality"] in QUALITY_PRESETS:
            settings.audio_quality = data["audio_quality"]

        edit_data = data.get("edit")
        if not isinstance(edit_data, dict):
            edit_data = {}
        defaults = VideoEdit()
        for name in VideoEdit.__dataclass_fields__:
            if name not in edit_data:
                continue
            value = edit_data[name]
            if name == "aspect":
                if value is None:
                    settings.edit.aspect = None
                elif isinstance(value, (list, tuple)) and len(value) == 2 and all(
                        isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in value):
                    settings.edit.aspect = (value[0], value[1])
                continue
            if name == "fit" and not (_is_str(value) and value in FIT_MODES):
                continue
            if name == "rotate" and (isinstance(value, bool) or value not in (0, 90, 180, 270)):
                continue
            if name == "resolution" and (isinstance(value, bool) or value not in RESOLUTIONS.values()):
                continue
            if name in ("start", "end") and value is not None and not (
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0):
                continue
            default = getattr(defaults, name)
            if _compatible(default, value):
                setattr(settings.edit, name, float(value) if isinstance(default, float) else value)
        return settings


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _compatible(default: Any, value: Any) -> bool:
    """Whether a loaded `value` has the right type for a field whose default is `default`."""
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if default is None:            # optional numbers: start, end, resolution
        return value is None or number
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, float):
        return number
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, type(default))


# Settings that describe how a particular file is framed and cut, as opposed to
# how it should be exported. "Apply to all" and presets leave these alone.
FRAMING_FIELDS = ("pos_x", "pos_y", "zoom", "start", "end")


def apply_to(target: ClipSettings, source: ClipSettings, include_framing: bool = False) -> ClipSettings:
    """Return a copy of `target` with `source`'s settings, keeping target's own
    crop position, zoom and trim unless `include_framing` is set."""
    result = source.copy()
    if not include_framing:
        for name in FRAMING_FIELDS:
            setattr(result.edit, name, getattr(target.edit, name))
    return result


# Preset name -> settings it changes. Presets only touch these fields.
PRESETS: dict[str, dict[str, Any]] = {
    "TikTok, Reels, Shorts": {"output": OUTPUT_VIDEO, "aspect": (9, 16), "fit": "crop", "resolution": 1080,
                              "crf": VIDEO_QUALITY["High"]},
    "Instagram post (4:5)": {"output": OUTPUT_VIDEO, "aspect": (4, 5), "fit": "crop", "resolution": 1080,
                             "crf": VIDEO_QUALITY["High"]},
    "Square post (1:1)": {"output": OUTPUT_VIDEO, "aspect": (1, 1), "fit": "crop", "resolution": 1080,
                          "crf": VIDEO_QUALITY["High"]},
    "YouTube (16:9)": {"output": OUTPUT_VIDEO, "aspect": (16, 9), "fit": "blur", "resolution": 1080,
                       "crf": VIDEO_QUALITY["High"]},
    "WhatsApp status": {"output": OUTPUT_VIDEO, "aspect": (9, 16), "fit": "blur", "resolution": 720,
                        "crf": VIDEO_QUALITY["Smaller file"]},
    "MP3 audio": {"output": OUTPUT_AUDIO},
}


def apply_preset(settings: ClipSettings, values: dict[str, Any]) -> ClipSettings:
    """Return a copy of `settings` with a preset's values applied."""
    result = settings.copy()
    for key, value in values.items():
        if key == "output":
            result.output = value
        elif key == "audio_quality":
            result.audio_quality = value
        elif key in VideoEdit.__dataclass_fields__:
            setattr(result.edit, key, value)
        else:
            raise KeyError(f"Unknown preset setting: {key}")
    return result


def output_file_for(source: Path, settings: ClipSettings, output_dir: Optional[Path] = None) -> Path:
    """Where an export of `source` with `settings` should go (never an existing file)."""
    if settings.output == OUTPUT_AUDIO:
        return output_path_for(source, output_dir)
    return output_path_for(source, output_dir, suffix=".mp4", name_tag=f" ({output_tag(settings.edit)})")


def describe(settings: ClipSettings) -> str:
    """A short human summary, e.g. "MP4 · 9:16 · crop · 0:05–0:20"."""
    from .core import format_time

    if settings.output == OUTPUT_AUDIO:
        parts = ["MP3", settings.audio_quality]
    else:
        edit = settings.edit
        parts = ["MP4", f"{edit.aspect[0]}:{edit.aspect[1]}" if edit.aspect else "original shape"]
        if edit.aspect:
            parts.append({"crop": "crop", "blur": "blurred fit", "bars": "black bars"}[edit.fit])
        if edit.rotate:
            parts.append(f"rotated {edit.rotate}°")
        if edit.mute:
            parts.append("no sound")
    edit = settings.edit
    if edit.start is not None or edit.end is not None:
        start = format_time(edit.start or 0)
        parts.append(f"{start}–{format_time(edit.end)}" if edit.end is not None else f"from {start}")
    return " · ".join(parts)
