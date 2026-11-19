"""Moving subtitle times from the source video onto the export's timeline."""

from __future__ import annotations

import bisect
import math
import re
from pathlib import Path
from typing import Optional

from ..core import ConversionError
from ..fonts import subtitle_font
from ..i18n import tr
from ..timing import Segment, clamp_speed, output_duration
from .cues import _srt_time, parse_cues, subtitle_kind
from .encoding import read_text
from .model import Subtitles
from .scratch import _scratch_file
from .text import clean_text, mark_right_to_left


class SpanMapper:
    """Moves spans of source time onto the export's timeline, for a fixed set of kept `segments`,
    `speed` and `reverse`. Built once, it finds the segments a span touches by bisection, so
    mapping thousands of cues through hundreds of cuts doesn't compare every cue with every
    segment."""

    def __init__(self, segments: list[Segment], speed: float = 1.0, reverse: bool = False):
        self.speed = clamp_speed(speed)
        self.reverse = reverse
        self.total = output_duration(segments, self.speed)
        kept = []
        for a, b in segments:
            kept.append((a, b))
            if b is None:  # an open end is the last part
                break
        self.segments = kept
        self.starts = [a for a, _ in kept]
        self.ends = [math.inf if b is None else b for _, b in kept]
        self.before: list[float] = []  # how much kept time lies before each segment
        done = 0.0
        for a, b in kept:
            self.before.append(done)
            done += (b - a) if b is not None else 0.0
        # Bisection needs the parts in order and apart (which a real edit always has).
        self.ordered = all(self.ends[i] <= self.starts[i + 1]
                           for i in range(len(kept) - 1)) and all(
                               self.starts[i] <= self.starts[i + 1] for i in range(len(kept) - 1))

    def __call__(self, start: float, end: float) -> list[tuple[float, float]]:
        if self.ordered:
            first = bisect.bisect_right(
                self.ends, start)  # the first segment that ends after the span starts
            last = bisect.bisect_left(
                self.starts, end)  # the segments before this one start before the span ends
            indexes = range(first, last)
        else:
            indexes = range(len(self.segments))
        pieces: list[tuple[float, float]] = []
        for i in indexes:
            a = self.starts[i]
            high, low = min(end, self.ends[i]), max(start, a)
            if high - low > 1e-6:
                lo, hi = (self.before[i] + low -
                          a) / self.speed, (self.before[i] + high - a) / self.speed
                if self.reverse and self.total is not None:
                    lo, hi = self.total - hi, self.total - lo
                pieces.append((lo, hi))
        pieces.sort()
        joined: list[tuple[float, float]] = []
        for lo, hi in pieces:
            if joined and lo - joined[-1][1] < 0.001:
                joined[-1] = (joined[-1][0], max(joined[-1][1], hi))
            else:
                joined.append((lo, hi))
        return [(lo, hi) for lo, hi in joined if hi - lo >= 0.02]


def map_span(
    start: float, end: float, segments: list[Segment], speed: float = 1.0, reverse: bool = False,
) -> list[tuple[float, float]]:
    """Where the source time span start..end lands on the export's timeline: one piece for each
    kept part it overlaps (so a cue that runs across a cut shows before and after it), nothing if
    it was cut out. Pieces that meet are joined."""
    return SpanMapper(segments, speed, reverse)(start, end)


def _ass_seconds(text: str) -> Optional[float]:
    found = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2})[.:](\d{1,3})\s*", text)
    if not found:
        return None
    hours, minutes, seconds, fraction = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction) / 10**len(fraction)


def _ass_time(seconds: float) -> str:
    cs = int(round(max(seconds, 0.0) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def retime_ass(text: str, mapper) -> tuple[str, int]:
    """An ASS script with every Dialogue line moved by `mapper(start, end) -> [(start, end), ...]`
    (the same text on each piece), and how many lines are left. Styles and everything else stay as
    they were."""
    out, left = [], 0
    columns: list[str] = []
    in_events = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("["):
            in_events = stripped.lower() == "[events]"
        if in_events and stripped.lower().startswith("format:"):
            columns = [c.strip().lower() for c in stripped.split(":", 1)[1].split(",")]
        if not (in_events and stripped.lower().startswith("dialogue:") and "start" in columns
                and "end" in columns):
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


_ASS_NOTES = re.compile(r"\{[^}]*\}|\\[Nnh]")
_BRACE_BLOCK = re.compile(r"\{(?!\\)([^{}]*)\}")
_BRACE_OPEN = re.compile(r"(?<!\\)\{(?!\\)")


def srt_cue_text(text: str) -> str:
    """A cue's text as it goes into the rewritten .srt that libass reads: no control characters, a
    blank line in the middle of a cue kept as a line holding a no-break space (a truly empty one
    would end the cue), and "{...}" in the words escaped so it is shown, not taken for an override
    block. (A real override such as {\\an8}, which starts with a backslash, is left alone.)"""
    lines = [line if line.strip() else " " for line in clean_text(text).split("\n")]
    text = "\n".join(lines)
    text = _BRACE_BLOCK.sub(lambda m: "\\{" + m.group(1) + "\\}", text)
    return _BRACE_OPEN.sub(lambda m: "\\{", text)


def _fix_ass_fonts(text: str) -> str:
    """An ASS script with the font of each style swapped for one that can draw the script's words,
    when the style's own font can't (Arial leaves a box after every Arabic lam-alef). The rest of
    the design stays."""
    lines = text.split("\n")
    words: list[str] = []
    columns: list[str] = []
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.lower()
        elif section == "[events]" and stripped.lower().startswith("format:"):
            columns = [c.strip().lower() for c in stripped.split(":", 1)[1].split(",")]
        elif section == "[events]" and stripped.lower().startswith("dialogue:") and columns:
            fields = line.split(":", 1)[1].lstrip().split(",", len(columns) - 1)
            words.append(_ASS_NOTES.sub(" ", fields[-1]))
    sample = "\n".join(words)
    if not sample.strip():
        return text
    out, columns, section = [], [], ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.lower()
        elif section.endswith("styles]") and stripped.lower().startswith("format:"):
            columns = [c.strip().lower() for c in stripped.split(":", 1)[1].split(",")]
        elif section.endswith("styles]") and stripped.lower().startswith(
                "style:") and "fontname" in columns:
            fields = line.split(":", 1)[1].lstrip().split(",", len(columns) - 1)
            if len(fields) == len(columns):
                at = columns.index("fontname")
                bold = "bold" in columns and fields[columns.index("bold")].strip() in ("-1", "1")
                chosen = subtitle_font(fields[at].strip(), bold, sample)
                if chosen != fields[at].strip():
                    fields[at] = chosen
                    line = "Style: " + ",".join(fields)
        out.append(line)
    return "\n".join(out)


def retimed_subtitles(
        subtitles: Subtitles, segments: list[Segment], speed: float = 1.0,
        reverse: bool = False) -> tuple[Path, str, int]:
    """The subtitle file rewritten for the export: (the new file, "srt" or "ass", the height of
    its script, which the font size is relative to). `segments` are the kept parts of the
    source."""
    path = Path(subtitles.file)
    try:
        text = read_text(path)
    except OSError:
        raise ConversionError(
            tr("The subtitle file can't be read: {name}", name=path.name
               or subtitles.file)) from None
    kind = subtitle_kind(path, text)
    mapper = SpanMapper(segments, speed, reverse)

    if kind == "ass":
        rewritten, _ = retime_ass(text, mapper)
        if subtitles.own_style:
            rewritten = _fix_ass_fonts(rewritten)
        return _scratch_file(rewritten.encode("utf-8"), ".ass"), "ass", _play_res_y(text)
    lines, number = [], 0
    moved = []
    for cue in parse_cues(text, kind):
        body = mark_right_to_left(srt_cue_text(cue.text))
        if body.strip():
            moved += [(lo, hi, body) for lo, hi in mapper(cue.start, cue.end)]
    for lo, hi, body in sorted(moved, key=lambda m: (m[0], m[1])):
        number += 1
        lines += [str(number), f"{_srt_time(lo)} --> {_srt_time(hi)}", body, ""]
    if not number:
        # An empty file would make the filter fail; a cue that is never on screen does nothing.
        lines = ["1", "00:00:00,000 --> 00:00:00,020", " ", ""]
    return _scratch_file("\n".join(lines).encode("utf-8"), ".srt"), "srt", 288
