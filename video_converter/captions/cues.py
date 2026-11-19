"""Subtitle cues: reading SRT, WebVTT and the like, and writing SRT."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Cue:
    start: float
    end: float
    text: str


_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[.,](\d+))?")
_ARROW = re.compile(r"([\d:.,]+)\s*-->\s*([\d:.,]+)")
_VTT_TAG = re.compile(r"<(?!/?(?:b|i|u|font)\b)[^>]*>", re.IGNORECASE)


def _seconds(match: re.Match) -> float:
    hours, minutes, seconds, fraction = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + (
        round(int(fraction) / 10**len(fraction), 6) if fraction else 0.0)


def subtitle_kind(path: Path, text: str) -> str:
    """"srt", "vtt" or "ass" for a subtitle file's contents."""
    suffix = path.suffix.lower()
    if suffix in (".ass", ".ssa") or "[Script Info]" in text[:2000]:
        return "ass"
    if suffix == ".vtt" or text.lstrip().startswith("WEBVTT"):
        return "vtt"
    return "srt"


def _next_cue_start(lines: list[str], timing: int, floor: int) -> int:
    """Where the cue whose timing line is `timing` begins: that line, or the number / name line just above it
    (`floor` is the line the previous cue's timing is on)."""
    above = timing - 1
    if above > floor and lines[above].strip():
        if lines[above].strip().isdigit() or above - 1 <= floor or not lines[above - 1].strip():
            return above
    return timing


def parse_cues(text: str, kind: str = "srt") -> list[Cue]:
    """The cues of an SRT or WebVTT file, in file order. A block with no usable time is skipped. Times may
    have any number of decimals or none. In an SRT file a cue's text may hold a blank line: the cue goes on until
    the next cue (its number or its time) starts; in WebVTT a blank line always ends it."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip("﻿\n").split("\n")
    timings = []
    for i, line in enumerate(lines):
        found = _ARROW.search(line)
        if found:
            start, end = _TIME.fullmatch(found.group(1)), _TIME.fullmatch(found.group(2))
            if start and end:
                timings.append((i, _seconds(start), _seconds(end)))
    cues = []
    for n, (i, start, end) in enumerate(timings):
        stop = _next_cue_start(lines, timings[n + 1][0], i) if n + 1 < len(timings) else len(lines)
        body = lines[i + 1:stop]
        if kind == "vtt":
            blank = next((k for k, line in enumerate(body) if not line.strip()), len(body))
            body = body[:blank]
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        joined = "\n".join(body)
        if kind == "vtt":
            joined = _VTT_TAG.sub("", joined)
        if joined.strip():
            cues.append(Cue(start, end, joined))
    return cues


def _srt_time(seconds: float) -> str:
    seconds = seconds if math.isfinite(seconds) else 0.0
    ms = int(round(max(seconds, 0.0) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def format_srt(cues: list[Cue]) -> str:
    """The text of an .srt file for `cues`, numbered in order. A blank line inside a cue would end it
    early in the file, so those are dropped."""
    blocks = []
    for number, cue in enumerate(cues, 1):
        body = "\n".join(
            line.strip() for line in cue.text.replace("\r", "").split("\n") if line.strip())
        blocks.append(f"{number}\n{_srt_time(cue.start)} --> {_srt_time(cue.end)}\n{body}\n")
    return "\n".join(blocks)


def encode_srt(cues: list[Cue]) -> bytes:
    """The bytes of an .srt file for `cues`: UTF-8 with a byte order mark, which Windows players and TVs
    need to read Arabic and other scripts correctly."""
    return b"\xef\xbb\xbf" + format_srt(cues).encode("utf-8")


def write_srt(cues: list[Cue], path: Path) -> None:
    """Save `cues` as a UTF-8 .srt file at `path` (written aside first, so a failure can't leave half a file)."""
    temp = path.with_name(path.name + ".part")
    try:
        temp.write_bytes(encode_srt(cues))
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
