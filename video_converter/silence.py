"""Remove silences: find the quiet stretches of a video's sound and turn them into cuts.

ffmpeg's `silencedetect` reports where the sound stays under a level for long enough. Those
stretches become cuts on the kept ranges (see timing.py), leaving a little silence next to the
speech so words aren't clipped. Everything here except `detect_silences` is plain arithmetic."""

from __future__ import annotations

import re
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .core import _NO_WINDOW, ConversionCancelled, ConversionError, stop_process
from .i18n import ntr, tr
from .timing import MIN_PART, SNAP_EDGE, Range, cut_range, kept_segments, segments_length

DEFAULT_DB = -35.0          # sound quieter than this counts as silence
DB_RANGE = (-60.0, -20.0)
DEFAULT_MIN = 0.6           # a silence has to last at least this long, in seconds
MIN_RANGE = (0.3, 3.0)
DEFAULT_PAD = 0.15          # silence left on each side of speech, in seconds
PAD_RANGE = (0.0, 0.5)

_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+(?:e-?\d+)?)")
_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+(?:e-?\d+)?)")


# ----- finding silences -------------------------------------------------------

def parse_silences(text: str, duration: Optional[float] = None) -> list[Range]:
    """The silent stretches in ffmpeg's `silencedetect` output, as (start, end) in seconds.

    A silence that has a start but no end runs to the end of the file (`duration`, or forever
    when that isn't known)."""
    silences: list[Range] = []
    start: Optional[float] = None
    for line in text.splitlines():
        if "silencedetect" not in line:
            continue
        found = _START_RE.search(line)
        if found:
            start = max(float(found.group(1)), 0.0)      # the first one can be a hair under zero
            continue
        found = _END_RE.search(line)
        if found and start is not None:
            end = float(found.group(1))
            if end > start:
                silences.append((start, end))
            start = None
    if start is not None:
        end = duration if duration is not None else float("inf")
        if end > start:
            silences.append((start, end))
    return silences


def detect_silences(ffmpeg: str, source: Path, noise_db: float = DEFAULT_DB, min_length: float = DEFAULT_MIN,
                    duration: Optional[float] = None,
                    cancel_event: Optional[threading.Event] = None) -> list[Range]:
    """Listen to the first audio track of `source` and return its silent stretches.

    Setting `cancel_event` stops ffmpeg and raises ConversionCancelled."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-i", str(source), "-map", "0:a:0", "-vn",
           "-af", f"silencedetect=noise={noise_db:g}dB:d={min_length:g}", "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    # silencedetect reports on stderr; read it on another thread so ffmpeg never waits on a full pipe.
    lines: deque[str] = deque()
    reader = threading.Thread(target=lambda: lines.extend(proc.stderr), daemon=True)
    reader.start()
    while True:
        if cancel_event is not None and cancel_event.is_set():
            stop_process(proc)
            proc.wait()
            reader.join(timeout=2)
            proc.stderr.close()
            raise ConversionCancelled()
        try:
            proc.wait(timeout=0.05)
            break
        except subprocess.TimeoutExpired:
            pass
    reader.join(timeout=5)
    proc.stderr.close()
    text = "".join(lines)
    if proc.returncode != 0:
        if "matches no streams" in text:
            raise ConversionError(tr("This file has no sound, so there are no silences to remove."))
        message = "".join(list(lines)[-10:]).strip() or tr("ffmpeg exited with code {code}", code=proc.returncode)
        raise ConversionError(message)
    return parse_silences(text, duration)


# ----- turning silences into cuts ---------------------------------------------

def silence_cuts(silences: Iterable[Range], start: Optional[float], end: Optional[float], duration: float,
                 padding: float = DEFAULT_PAD) -> list[Range]:
    """The parts to cut out: each silence inside the trim, less `padding` on the sides that touch speech.

    Cuts shorter than the editor's smallest part are dropped, and bits of speech shorter than that
    between two cuts (a click, a cough) are cut too. If everything inside the trim is silent, nothing is
    cut: there would be nothing left."""
    low = start or 0.0
    high = min(end, duration) if end is not None else duration
    padding = max(padding, 0.0)
    cuts: list[list[float]] = []
    for a, b in sorted(silences):
        lead, trail = a <= low + SNAP_EDGE, b >= high - SNAP_EDGE      # no speech on that side to protect
        a, b = max(a, low), min(b, high)
        if b <= a:
            continue
        if not lead:
            a += padding
        if not trail:
            b -= padding
        if b - a < MIN_PART:
            continue
        if cuts and a - cuts[-1][1] < MIN_PART:        # too little speech between two cuts to keep
            cuts[-1][1] = max(cuts[-1][1], b)
        else:
            cuts.append([a, b])
    if not cuts:
        return []
    if cuts[0][0] - low < MIN_PART:
        cuts[0][0] = low
    if high - cuts[-1][1] < MIN_PART:
        cuts[-1][1] = high
    if len(cuts) == 1 and cuts[0][0] <= low and cuts[0][1] >= high:
        return []
    return [(a, b) for a, b in cuts]


@dataclass
class SilencePlan:
    keep: list[Range]                                       # the new kept ranges
    cuts: list[Range] = field(default_factory=list)         # the pauses that were actually cut out
    before: float = 0.0                                     # what the export keeps, in seconds, before...
    after: float = 0.0                                      # ...and after (at normal speed)

    @property
    def removed(self) -> float:
        return self.before - self.after


def plan_silence_removal(keep: list[Range], silences: Iterable[Range], start: Optional[float], end: Optional[float],
                         duration: float, padding: float = DEFAULT_PAD) -> SilencePlan:
    """Cut the silences out of what `keep` (with the trim `start`..`end`) already keeps.

    The user's own cuts and splits stay as they are, and nothing outside the trim changes."""
    before = segments_length(kept_segments(keep, start, end, duration)) or 0.0
    result, cuts = list(keep), []
    for a, b in silence_cuts(silences, start, end, duration, padding):
        changed = cut_range(result, a, b, duration)
        if changed != result:
            cuts.append((a, b))
            result = changed
    # Cutting beside a user's cut can leave a sliver; drop the ones that weren't there before.
    original = set(keep)
    result = [r for r in result if r[1] - r[0] >= MIN_PART or r in original] or list(keep)
    after = segments_length(kept_segments(result, start, end, duration)) or 0.0
    return SilencePlan(result, cuts, before, after)


def summary(plan: SilencePlan) -> str:
    """One plain sentence about what removing the silences does."""
    if not plan.cuts:
        return tr('No pauses found. Try raising "Quieter than" or lowering "Longer than".')
    return ntr(len(plan.cuts),
               "Found {n} pause, {length} in total. The video goes from {before} to {after}.",
               "Found {n} pauses, {length} in total. The video goes from {before} to {after}.",
               length=_length(plan.removed), before=_clock(plan.before), after=_clock(plan.after))


def _length(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 10 else f"{seconds:.0f} s" if seconds < 60 else _clock(seconds)


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
