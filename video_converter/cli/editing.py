"""Warnings, cuts and silence removal that the command line adds to an edit."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

from ..core import ConversionError
from ..silence import detect_silences, plan_silence_removal
from ..timing import cut_range
from ..video import VideoEdit, format_warning, reverse_memory, reverse_warning


def _warn_gif(name: str, edit: VideoEdit, info) -> None:
    """Say so on stderr when a GIF will run long enough to get big. The export still runs."""
    warning = format_warning(edit, info)
    if warning:
        print(f"\r{name}: warning - {warning}", file=sys.stderr)


def frame_mode_requested(args) -> bool:
    return args.frame_at is not None or args.frame_out is not None


def _warn_reverse(
        name: str, edit: VideoEdit, info, sound_only: bool, fps: float | None = None) -> None:
    """Say so on stderr when playing `edit` backwards will need a lot of memory. The export still
    runs."""
    warning = reverse_warning(reverse_memory(edit, info, fps, sound_only))
    if warning:
        print(f"\r{name}: warning - {warning}", file=sys.stderr)


def _with_cuts(
        edit: VideoEdit, cuts: list[tuple[float, float]], duration: float | None) -> VideoEdit:
    """A copy of `edit` with `cuts` removed from a source `duration` seconds long."""
    edit = copy.deepcopy(edit)
    if cuts:
        if not duration:
            raise ConversionError("Can't cut this file: its length is unknown.")
        for a, b in cuts:
            edit.keep = cut_range(edit.keep, a, b, duration)
    return edit


def _without_silences(edit: VideoEdit, ffmpeg: str, source: Path, info, args, note) -> VideoEdit:
    """A copy of `edit` with the silences of `source` cut out (inside its trim, next to any cuts it
    has)."""
    if not info.duration:
        raise ConversionError("Can't remove silences: this file's length is unknown.")
    if not info.has_audio:
        raise ConversionError("This file has no sound, so there are no silences to remove.")
    silences = detect_silences(ffmpeg, source, args.silence_db, args.silence_min, info.duration)
    plan = plan_silence_removal(
        edit.keep, silences, edit.start, edit.end, info.duration, args.silence_pad)
    note(
        f"{source.name}: cut out {len(plan.cuts)} {'pause' if len(plan.cuts) == 1 else 'pauses'}, "
        f"{plan.removed:.1f} s in total")
    edit = copy.deepcopy(edit)
    edit.keep = plan.keep
    return edit
