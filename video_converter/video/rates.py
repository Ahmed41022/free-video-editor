"""Steady frame rates and the H.264/H.265 levels that phones can play."""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Optional

from ..core import MediaInfo
from ..i18n import tr
from ..timing import clamp_speed
from .edit import VideoEdit

FAST_FRAME_RATE = 60.5  # above this (59.94 and 60 are fine) some phones give up
# A source this much slower than the chosen steady rate keeps its own rate, not the chosen one.
SLOW_SOURCE = 0.9
# Rates with a fraction (NTSC) that a plain 29.97 would only approximate.
_NTSC_RATES = (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001))


def _source_rate(fps: float) -> Fraction:
    """A source's frame rate as an exact rate: 23.98 -> 24000/1001, 25 -> 25, 24.7 -> 25."""
    for rate in _NTSC_RATES:
        if abs(fps - float(rate)) <= 0.02:
            return rate
    return Fraction(max(round(fps), 1))


def steady_rate(chosen: Optional[int], source_fps: Optional[float]) -> Optional[Fraction]:
    """The constant frame rate to export at, or None when the export keeps the source's own.

    `chosen` is the user's Steady 60 / Steady 30 (a VideoEdit.frame_rate) and `source_fps` the
    picture's frame rate as it will arrive at the end of the edit (after any speed change). The
    result is never above `chosen`, and a source that is clearly slower than `chosen` (more than
    10% below it, so a 58 fps game recording still counts as 60) keeps its own rate instead of
    having frames repeated: a 25 fps video stays at 25 fps, steady."""
    if not chosen:
        return None
    if not source_fps or source_fps >= chosen * SLOW_SOURCE:
        return Fraction(chosen)
    return min(_source_rate(source_fps), Fraction(chosen))


def source_rate(info: MediaInfo, speed: float) -> Optional[float]:
    """The frame rate the picture of `info` has after a `speed` change, as far as the choice of a
    steady rate goes: a faster clip has the faster rate, but a slowed-down one is not "slower than
    the source". Slow motion is made by repeating frames, up to whatever steady rate was asked
    for."""
    return info.fps * max(clamp_speed(speed), 1.0) if info.fps else None


def edit_rate(edit: VideoEdit, info: MediaInfo) -> Optional[Fraction]:
    """The steady rate for exporting `info` with `edit`, or None (see `steady_rate`)."""
    return steady_rate(edit.frame_rate, source_rate(info, edit.speed))


def native_rate(fps: float) -> Fraction:
    """A picture's frame rate as the exact rate to write: 29.97 -> 30000/1001, 25 -> 25, 12.5 ->
    25/2."""
    for rate in _NTSC_RATES:
        if abs(fps - float(rate)) <= 0.02:
            return rate
    if abs(fps - round(fps)) <= 0.02:
        return Fraction(max(round(fps), 1))
    return Fraction(round(fps * 100), 100)


def output_rate(edit: VideoEdit, info: MediaInfo) -> Optional[Fraction]:
    """The frame rate to tell the encoder for exporting `info` with `edit`: the steady rate, or the
    source's own. None when the picture keeps a frame rate that changes (see
    MediaInfo.variable_frame_rate) or the rate is unknown. Without one an encoder works its rate
    out from the time base of the filtered picture, which can be 90000 or more frames a second, and
    writes the file as the highest H.264 level."""
    steady = edit_rate(edit, info)
    if steady:
        return steady
    if info.fps and (edit.speed != 1.0 or not info.variable_frame_rate):
        return native_rate(info.fps)
    return None


def _rate_text(rate: float | Fraction) -> str:
    """A frame rate as ffmpeg takes it: 30, 24000/1001, or (a plain number) 29.97."""
    if isinstance(rate, Fraction):
        return str(
            rate.numerator) if rate.denominator == 1 else f"{rate.numerator}/{rate.denominator}"
    return f"{rate:.6g}"


# H.264 levels from 4.1 up: (name, biggest picture in 16x16 blocks, most blocks a second). Phones
# that play 1080p60 want 4.2; 4.1 is 1080p30. Smaller pictures also say 4.1, which every phone
# plays.
_H264_LEVELS = (
    ("4.1", 8192, 245760), ("4.2", 8192, 522240), ("5.0", 22080, 589824), ("5.1", 36864, 983040),
    ("5.2", 36864, 2073600), ("6.0", 139264, 4177920), ("6.1", 139264,
                                                        8355840), ("6.2", 139264, 16711680),
)


def h264_level(width: int, height: int, fps: float) -> str:
    """The lowest H.264 level (from 4.1) that a `width` x `height` picture at `fps` fits, like
    "4.2"."""
    blocks = math.ceil(width / 16) * math.ceil(height / 16)
    for name, frame_limit, rate_limit in _H264_LEVELS:
        if blocks <= frame_limit and blocks * fps <= rate_limit:
            return name
    return _H264_LEVELS[-1][0]


# H.265 levels (main tier): (name, biggest picture in luma samples, most luma samples a second).
_H265_LEVELS = (
    ("2.0", 122880, 3686400), ("2.1", 245760, 7372800), ("3.0", 552960, 16588800),
    ("3.1", 983040, 33177600), ("4.0", 2228224, 66846720), ("4.1", 2228224, 133693440),
    ("5.0", 8912896, 267386880), ("5.1", 8912896, 534773760), ("5.2", 8912896, 1069547520),
    ("6.0", 35651584, 1069547520), ("6.1", 35651584, 2139095040), ("6.2", 35651584, 4278190080),
)


def h265_level(width: int, height: int, fps: float) -> str:
    """The lowest H.265 level that a `width` x `height` picture at `fps` fits, like "4.1"."""
    samples = width * height
    for name, frame_limit, rate_limit in _H265_LEVELS:
        if samples <= frame_limit and samples * fps <= rate_limit:
            return name
    return _H265_LEVELS[-1][0]


UNKNOWN_RATE_PEAK = 60.0  # the rate a level is worked out for when the picture doesn't say its own


def encoder_level(codec: str, width: int, height: int, rate: Optional[Fraction],
                  info: MediaInfo) -> Optional[str]:
    """The level an H.264 or H.265 file of this size and frame rate is written as. A picture whose
    rate changes is covered up to the highest rate it declares."""
    peak = float(rate) if rate else (info.max_fps or info.fps or UNKNOWN_RATE_PEAK)
    if codec == "h264":
        return h264_level(width, height, peak)
    if codec == "hevc":
        return h265_level(width, height, peak)
    return None


def frame_rate_hint(info: MediaInfo) -> str:
    """What to tell the user about a source whose frame rate may not play on a phone, or "" when
    there is nothing to say. Shown while the frame rate is left at "Same as source"."""
    if info.variable_frame_rate and info.max_fps:
        return tr(
            "This video's frame rate changes (up to {fps:g} fps), which some phones can't play. "
            "Choose Steady 60 fps.", fps=info.max_fps)
    if info.fps and info.fps > FAST_FRAME_RATE:
        return tr(
            "This video runs at {fps:g} fps, which some phones can't play. Choose Steady 60 fps.",
            fps=info.fps)
    return ""
