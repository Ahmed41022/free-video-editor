"""Cutting, speed and fades: which parts of a source end up in an export, and where.

Cuts are stored as the ranges of the source that are kept, in source seconds. An
empty list keeps everything. Two kept ranges that touch are a split: the export is
the same, but the editor can remove one side or drag the split point.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional

Range = tuple[float, float]
Segment = tuple[float, Optional[float]]    # the end is None only when the length is unknown

MIN_PART = 0.1          # shortest part the editor makes, in seconds
SNAP_EDGE = 0.05        # kept ranges this close to the very start or end reach it
SPEED_MIN, SPEED_MAX = 0.25, 4.0
FADE_MAX = 30.0
_EPS = 1e-6


# ----- kept ranges ------------------------------------------------------------

def normalize_keep(ranges: Iterable[Range], duration: Optional[float] = None) -> list[Range]:
    """Sorted kept ranges inside 0..duration, overlaps merged, splits kept.

    Returns [] when what's left is the whole video, which means "no cuts"."""
    clean: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in ranges):
        a = max(a, 0.0)
        if duration:
            b = min(b, duration)
        if b - a < 0.01:
            continue
        if clean and a < clean[-1][1] - 0.001:          # overlap: merge
            clean[-1][1] = max(clean[-1][1], b)
        elif clean and a - clean[-1][1] < 0.001:        # touching: a split
            clean.append([clean[-1][1], b])
        else:
            clean.append([a, b])
    if clean and clean[0][0] <= SNAP_EDGE:
        clean[0][0] = 0.0
    if clean and duration and clean[-1][1] >= duration - SNAP_EDGE:
        clean[-1][1] = duration
    if len(clean) == 1 and clean[0][0] == 0.0 and duration and clean[0][1] == duration:
        return []
    return [(a, b) for a, b in clean]


def _ranges(keep: list[Range], duration: float) -> list[Range]:
    """The kept ranges, with "no cuts" spelled out as one range."""
    return list(keep) if keep else [(0.0, duration)]


def removed_parts(keep: list[Range], duration: Optional[float]) -> list[Range]:
    """The gaps between kept ranges, including any before the first or after the last."""
    if not keep:
        return []
    gaps, previous = [], 0.0
    for a, b in keep:
        if a - previous > _EPS:
            gaps.append((previous, a))
        previous = b
    if duration and duration - previous > _EPS:
        gaps.append((previous, duration))
    return gaps


def split_points(keep: list[Range]) -> list[float]:
    """Times where two kept ranges touch."""
    return [b for (_, b), (a, _) in zip(keep, keep[1:]) if abs(a - b) <= _EPS]


def part_index(keep: list[Range], t: float, duration: float) -> Optional[int]:
    """Index of the kept range that contains `t`, or None if `t` has been cut."""
    ranges = _ranges(keep, duration)
    for i, (a, b) in enumerate(ranges):
        if a <= t < b or (i == len(ranges) - 1 and t == b):
            return i
    return None


def split_at(keep: list[Range], t: float, duration: float) -> list[Range]:
    """Split the kept range under `t`. Nothing changes too close to an edge or inside a cut."""
    ranges = _ranges(keep, duration)
    i = part_index(keep, t, duration)
    if i is None:
        return list(keep)
    a, b = ranges[i]
    if t - a < MIN_PART or b - t < MIN_PART:
        return list(keep)
    return normalize_keep(ranges[:i] + [(a, t), (t, b)] + ranges[i + 1:], duration)


def remove_part_at(keep: list[Range], t: float, duration: float) -> list[Range]:
    """Cut out the kept range under `t`. The last remaining range can't be removed."""
    ranges = _ranges(keep, duration)
    i = part_index(keep, t, duration)
    if i is None or len(ranges) < 2:
        return list(keep)
    return normalize_keep(ranges[:i] + ranges[i + 1:], duration)


def restore_part_at(keep: list[Range], t: float, duration: float) -> list[Range]:
    """Put back the cut part under `t`."""
    for a, b in removed_parts(keep, duration):
        if a <= t <= b:
            return normalize_keep(list(keep) + [(a, b)], duration)
    return list(keep)


def cut_range(keep: list[Range], start: float, end: float, duration: float) -> list[Range]:
    """Remove start..end from whatever is kept."""
    result = []
    for a, b in _ranges(keep, duration):
        if end <= a or start >= b:
            result.append((a, b))
            continue
        if start > a:
            result.append((a, start))
        if end < b:
            result.append((end, b))
    if not result:
        return list(keep)       # refuse to cut everything
    return normalize_keep(result, duration)


# ----- what gets exported -----------------------------------------------------

def kept_segments(keep: list[Range], start: Optional[float], end: Optional[float],
                  duration: Optional[float]) -> list[Segment]:
    """The parts of the source in the export, in order: the kept ranges inside the trim,
    with splits joined back together."""
    low = start or 0.0
    high = end if end is not None else duration
    if end is not None and duration:
        high = min(end, duration)       # an end past the file's length would throw off the fade out and progress
    ranges: list[Segment] = list(keep) if keep else [(0.0, high)]
    segments: list[Segment] = []
    for a, b in ranges:
        a = max(a, low)
        if high is not None:
            b = high if b is None else min(b, high)
        if b is not None and b - a <= _EPS:
            continue
        if segments and segments[-1][1] is not None and abs(a - segments[-1][1]) <= _EPS:
            segments[-1] = (segments[-1][0], b)
        else:
            segments.append((a, b))
    return segments


def frame_rate(fps: float) -> float:
    """The exact rate a video most likely has. ffmpeg prints 30000/1001 as "29.97", and a frame
    grid built on that rounded figure drifts by a frame after a few minutes."""
    ntsc = round(fps * 1001 / 1000) * 1000 / 1001
    if abs(ntsc - fps) < 0.006:
        return ntsc
    return float(round(fps)) if abs(fps - round(fps)) < 0.005 else fps


def snap_segments(segments: list[Segment], fps: Optional[float],
                  duration: Optional[float] = None) -> list[Segment]:
    """`segments` with every start and end moved to the nearest frame boundary.

    The picture of each part is cut in whole frames while the sound is cut to the sample, so
    parts that start between frames come out slightly longer or shorter in the picture than in
    the sound, and many cuts add that up into a visible lag. With the boundaries on frames both
    are the same length. Nothing changes when the frame rate is unknown."""
    if not fps or fps <= 0 or not segments:
        return list(segments)
    rate = frame_rate(fps)
    snapped: list[Segment] = []
    for a, b in segments:
        a = round(a * rate) / rate
        if b is not None:
            b = round(b * rate) / rate
            if duration:
                b = min(b, duration)        # the last frame can't reach past the end of the file
            if b - a < 0.5 / rate:
                continue                    # shorter than one frame: nothing of it would show
        if snapped and snapped[-1][1] is not None and a - snapped[-1][1] <= _EPS:
            snapped[-1] = (snapped[-1][0], b)       # the gap between two parts was less than a frame
        else:
            snapped.append((a, b))
    return snapped or list(segments)


def frame_margin(fps: Optional[float]) -> float:
    """How far before a frame boundary the picture is cut, in seconds: half a frame. A cut
    placed exactly on a frame's time could land just after it and drop the frame."""
    return 0.5 / frame_rate(fps) if fps and fps > 0 else 0.0


def segments_length(segments: list[Segment]) -> Optional[float]:
    if any(b is None for _, b in segments):
        return None
    return sum(b - a for a, b in segments)


def output_duration(segments: list[Segment], speed: float = 1.0) -> Optional[float]:
    length = segments_length(segments)
    return None if length is None else length / clamp_speed(speed)


def source_to_output(t: float, segments: list[Segment], speed: float = 1.0,
                     reverse: bool = False) -> Optional[float]:
    """Where source time `t` lands in the export, or None if it's cut out."""
    done = 0.0
    for i, (a, b) in enumerate(segments):
        last = i == len(segments) - 1
        if a <= t and (b is None or t < b or (last and t == b)):
            position = (done + t - a) / clamp_speed(speed)
            if reverse:
                total = output_duration(segments, speed)
                return None if total is None else max(total - position, 0.0)
            return position
        if b is None:
            break
        done += b - a
    return None


def output_to_source(t: float, segments: list[Segment], speed: float = 1.0, reverse: bool = False) -> Optional[float]:
    """Which source time plays at time `t` of the export, or None past its end."""
    spans = output_spans_to_source(t, t + 1e-9, segments, speed, reverse)
    return spans[0][0] if spans else None


def output_spans_to_source(start: float, end: Optional[float], segments: list[Segment], speed: float = 1.0,
                           reverse: bool = False) -> list[Range]:
    """The parts of the source that play between `start` and `end` (None = the end) of the export:
    the other way round from `source_to_output`. A span across a cut comes out as one part for each
    side of it. Used to show where a text's time on the export falls on the source's timeline."""
    speed = clamp_speed(speed)
    total = output_duration(segments, speed)
    if end is None:
        end = total if total is not None else math.inf
    low_bound, high_bound = (total - end, total - start) if reverse and total is not None else (start, end)
    spans: list[Range] = []
    done = 0.0
    for a, b in segments:
        length = (b - a) if b is not None else math.inf
        lo, hi = max(low_bound, done / speed), min(high_bound, (done + length) / speed)
        if hi - lo > 0:
            spans.append((a + lo * speed - done, a + hi * speed - done))
        if b is None:
            break
        done += length
    merged: list[Range] = []
    for a, b in sorted(spans):
        if merged and a - merged[-1][1] <= _EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def clamp_speed(speed: float) -> float:
    return min(max(speed, SPEED_MIN), SPEED_MAX)


def atempo_factors(speed: float) -> list[float]:
    """Split a speed change into atempo steps, each between 0.5 and 2.0."""
    speed = clamp_speed(speed)
    factors = []
    while speed > 2.0 + _EPS:
        factors.append(2.0)
        speed /= 2.0
    while speed < 0.5 - _EPS:
        factors.append(0.5)
        speed /= 0.5
    if abs(speed - 1.0) > _EPS or not factors:
        factors.append(speed)
    return [] if factors == [1.0] else factors


# ----- ffmpeg filters ---------------------------------------------------------

def _n(value: float, places: int = 3) -> str:
    return f"{value:.{places}f}".rstrip("0").rstrip(".") if math.isfinite(value) else "0"


def relative(segments: list[Segment]) -> list[Segment]:
    """Segments measured from the first one's start, which is where the input seek lands."""
    base = segments[0][0]
    return [(a - base, None if b is None else b - base) for a, b in segments]


def select_filters(src: str, out: str, segments: list[Segment], audio: bool, tag: str = "",
                   margin: float = 0.0) -> list[str]:
    """Filters that keep `segments` (relative to the input start) of stream `src` and play
    them back to back as `out`. A picture's cuts move `margin` seconds earlier (see `frame_margin`);
    the sound's never do."""
    setpts = "asetpts" if audio else "setpts"
    if len(segments) <= 1:
        return [f"{src}{setpts}=PTS-STARTPTS{out}"]
    kind = "a" if audio else "v"
    trim, split = ("atrim", "asplit") if audio else ("trim", "split")
    copies = [f"[{kind}in{tag}_{i}]" for i in range(len(segments))]
    pieces = [f"[{kind}seg{tag}_{i}]" for i in range(len(segments))]
    filters = [f"{src}{split}={len(segments)}{''.join(copies)}"]
    for copy, piece, (a, b) in zip(copies, pieces, segments):
        a = max(a - margin, 0.0)
        bounds = f"start={_n(a, 6)}" + (f":end={_n(max(b - margin, 0.0), 6)}" if b is not None else "")
        filters.append(f"{copy}{trim}={bounds},{setpts}=PTS-STARTPTS{piece}")
    flags = "v=0:a=1" if audio else "v=1:a=0"
    filters.append(f"{''.join(pieces)}concat=n={len(segments)}:{flags}{out}")
    return filters


def fade_filters(fade_in: float, fade_out: float, total: Optional[float], audio: bool) -> list[str]:
    """Fades on the export's own timeline. A fade out needs the length to know where to start."""
    name = "afade" if audio else "fade"
    filters = []
    if fade_in > 0:
        length = min(fade_in, total) if total else fade_in
        filters.append(f"{name}=t=in:st=0:d={_n(length)}")
    if fade_out > 0 and total:
        length = min(fade_out, total)
        filters.append(f"{name}=t=out:st={_n(total - length)}:d={_n(length)}")
    return filters


def audio_filters(src: str, out: str, segments: list[Segment], speed: float = 1.0, reverse: bool = False,
                  fade_in: float = 0.0, fade_out: float = 0.0, tag: str = "", always: bool = False) -> str:
    """A filter graph from audio stream `src` to `out` with cuts, speed, reverse and fades.

    Returns "" when nothing needs changing (and `always` isn't set), so the caller can
    map the stream directly."""
    rel = relative(segments)
    chain = [f"atempo={f:.6g}" for f in atempo_factors(speed)]
    if reverse:
        # areverse replays the old timestamps in reverse order, which are uneven after cuts.
        chain += ["areverse", "asetpts=N/SR/TB"]
    chain += fade_filters(fade_in, fade_out, output_duration(rel, speed), audio=True)
    if len(rel) <= 1 and not chain and not always:
        return ""
    if not chain:
        return ";".join(select_filters(src, out, rel, audio=True, tag=tag))
    selected = f"[asel{tag}]"
    return ";".join(select_filters(src, selected, rel, audio=True, tag=tag) + [f"{selected}{','.join(chain)}{out}"])
