"""Checks an export makes before it starts."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..i18n import tr
from ..sound import Sound
from .errors import ConversionError
from .probe import probe_media


def check_trim(start: Optional[float], end: Optional[float]) -> None:
    if start is not None and end is not None and end <= start:
        raise ConversionError(tr("The trim end time must be after the start time."))


def no_segments_error() -> ConversionError:
    return ConversionError(
        tr("Nothing is left to export: the trim and cuts remove the whole video."))


def check_music(ffmpeg: str, sound: Sound, muted: bool = False) -> None:
    """Fail with a clear message when the background music can't be used. A `muted` export has no
    music (Mute wins), so then there is nothing to check."""
    if not sound.music or muted:
        return
    path = Path(sound.music)
    if not path.is_file():
        raise ConversionError(
            tr(
                "The background music file can't be found: {path}. "
                "Choose it again under Sound, or remove it.", path=path))
    if not probe_media(ffmpeg, path).has_audio:
        raise ConversionError(
            tr("{name} has no sound, so it can't be used as background music.", name=path.name))


def music_length_error() -> ConversionError:
    return ConversionError(tr("Can't add background music: the length of this file is unknown."))
