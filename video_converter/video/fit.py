"""Fitting an export into a file size, and what to say about the format."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from ..core import ConversionError, MediaInfo, no_segments_error
from ..i18n import tr
from ..output import LADDER, SizeFit, fit_note, fit_plan, gif_rate, gif_warning, target_bytes
from ..timing import clamp_speed, output_duration
from .commands import music_path
from .edit import VideoEdit
from .geometry import plan_geometry
from .memory import DEFAULT_FPS
from .rates import output_rate


def plan_fit(
        edit: VideoEdit, info: MediaInfo, seconds: Optional[float], sound: bool,
        fps: Optional[float] = None) -> tuple[VideoEdit, Optional[SizeFit]]:
    """Work out how to fit an export of `edit` (a result `seconds` long, `sound` whether it has any,
    running at `fps`) into `edit.target_mb`. Returns the edit to export, which has a smaller size when the
    picture had to be lowered (720p, then 480p, 360p), and the SizeFit, or (edit, None) when there is
    no file size to fit. Raises ConversionError when not even the smallest size leaves the picture enough."""
    fmt = edit.output_format
    if edit.target_mb <= 0 or fmt.codec == "gif":
        return edit, None
    if seconds is None:
        raise ConversionError(tr("Can't fit this into a file size: its length is unknown."))
    if seconds <= 0:
        raise no_segments_error()
    geometry = plan_geometry(info.width, info.height, edit)
    rungs: list[tuple[Optional[int], int, int]] = [(None, geometry.out_width, geometry.out_height)]
    for short in LADDER:
        if short < min(geometry.out_width, geometry.out_height):
            lower = plan_geometry(info.width, info.height, replace(edit, resolution=short))
            rungs.append((short, lower.out_width, lower.out_height))
    fit = fit_plan(
        target_bytes(edit.target_mb), seconds, fmt.codec, fmt.audio if sound else "", fps
        or info.fps or DEFAULT_FPS, rungs)
    return (replace(edit, resolution=fit.short) if fit.short else edit), fit


def edit_fit(edit: VideoEdit, info: MediaInfo) -> tuple[VideoEdit, Optional[SizeFit]]:
    """`plan_fit` for exporting `info` with `edit`: the length, sound and frame rate come from them."""
    segments = edit.segments(info.duration, info.fps)
    rate = output_rate(edit, info)
    sound = (info.has_audio and not edit.mute) or bool(music_path(edit))
    return plan_fit(
        edit, info, output_duration(segments, edit.speed), sound,
        float(rate) if rate else info.fps)


def fit_summary(edit: VideoEdit, info: MediaInfo) -> tuple[str, bool]:
    """What to tell the user about fitting `info` into `edit.target_mb`, and whether it can't be done:
    ("", False) when no file size is asked for."""
    if edit.target_mb <= 0 or edit.output_format.codec == "gif" or not info.has_video:
        return "", False
    try:
        return fit_note(edit_fit(edit, info)[1]), False
    except ConversionError as exc:
        return str(exc), True


def format_warning(edit: VideoEdit, info: MediaInfo) -> str:
    """A warning about the format for exporting `info` with `edit`: a GIF that will get big."""
    if edit.output_format.codec != "gif":
        return ""
    seconds = output_duration(edit.segments(info.duration, info.fps), edit.speed)
    if not info.has_video:
        return gif_warning(seconds)
    # The size follows from the picture's size and frame rate too: a short clip can be as heavy as a long one.
    geometry = plan_geometry(info.width, info.height, edit)
    width = min(edit.gif_width, geometry.out_width)
    height = max(round(geometry.out_height * width / geometry.out_width), 1)
    fps = gif_rate(
        edit.gif_fps,
        info.fps * clamp_speed(edit.speed) if info.fps else None)  # as in gif_command
    return gif_warning(seconds, width, height, fps)


def wants_loudness_pass(edit: VideoEdit, info: MediaInfo) -> bool:
    """Whether exporting `info` with `edit` evens out the loudness, which takes a first pass.
    There is nothing to even out in a file without sound, and nothing at all with the sound removed
    (or in a GIF, which has none)."""
    if edit.output_format.codec == "gif":
        return False
    return edit.normalize and ((info.has_audio and not edit.mute) or bool(music_path(edit)))
