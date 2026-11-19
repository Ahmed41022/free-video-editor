"""How much memory reversing a clip needs."""

from __future__ import annotations

from typing import Optional

from ..core import MediaInfo
from ..i18n import tr
from ..timing import output_duration, segments_length
from .edit import RGB_LOOKS, VideoEdit
from .geometry import plan_geometry
from .rates import edit_rate

DEFAULT_FPS = 30.0                     # assumed when the video doesn't say
REVERSE_WARN_BYTES = 2 * 1024 ** 3     # more than this is worth a warning
REVERSE_BASE_BYTES = 256 * 1024 ** 2   # what ffmpeg needs besides the frames it holds
_SOUND_BYTES_PER_SECOND = 48000 * 2 * 4    # stereo floats, which is what the sound is reversed as


def reverse_memory(edit: VideoEdit, info: MediaInfo, fps: Optional[float] = None, sound_only: bool = False) -> int:
    """About how many bytes ffmpeg needs to play this clip backwards, or 0 when it isn't reversed
    (or its length is unknown).

    Reversing holds every frame of the result in memory. Measured on real exports, ffmpeg needs about
    3 bytes a pixel for each frame (the raw yuv420p frame is 1.5, and its buffers and the encoder's own
    frames about as much again), or 4.5 after a color look that works on RGB pictures (see `RGB_LOOKS`),
    plus REVERSE_BASE_BYTES for the rest of ffmpeg. The size is
    the exported one (see `plan_geometry`), the number of frames comes from
    the kept, sped-up length at `fps` (a join's frame rate; the clip's own by default, or 30 when
    unknown). A `sound_only` export holds just the sound instead."""
    if not edit.reverse:
        return 0
    segments = edit.segments(info.duration)
    length, seconds = segments_length(segments), output_duration(segments, edit.speed)
    if length is None or seconds is None:
        return 0
    if sound_only:
        return int(seconds * _SOUND_BYTES_PER_SECOND)
    if not info.has_video:
        return 0
    geometry = plan_geometry(info.width, info.height, edit)
    steady = edit_rate(edit, info)
    rate = fps or (float(steady) if steady else info.fps)
    frames = seconds * rate if rate else length * DEFAULT_FPS
    per_pixel = 4.5 if edit.look in RGB_LOOKS else 3.0
    return int(frames * geometry.out_width * geometry.out_height * per_pixel) + REVERSE_BASE_BYTES


def format_memory(nbytes: float) -> str:
    gigabytes = nbytes / 1024 ** 3
    return f"{gigabytes:.1f} GB" if gigabytes >= 1 else f"{max(round(nbytes / 1024 ** 2), 1)} MB"


def reverse_warning(nbytes: int) -> str:
    """The warning to show next to a reversed clip that needs `nbytes`, or "" when that is fine."""
    if nbytes <= REVERSE_WARN_BYTES:
        return ""
    return tr("Reversing this needs about {memory} of memory. Shorten the clip or lower the size if your PC "
              "has less.", memory=format_memory(nbytes))
