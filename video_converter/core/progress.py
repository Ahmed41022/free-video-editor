"""Progress reports: where each pass sits in the bar, and a bar that never moves back."""

from __future__ import annotations

from typing import Callable, Optional

# Where the first loudness pass sits in the progress bar. It only reads and filters the sound, so it is
# quick next to a video export, and about half as long as making an MP3.
LOUDNESS_SHARE = 0.1
LOUDNESS_SHARE_AUDIO = 0.4


def scaled(on_progress: Optional[Callable[[float], None]], start: float, share: float
           ) -> Optional[Callable[[float], None]]:
    """A progress callback that reports 0..1 as `start` .. `start + share` of the real one."""
    if on_progress is None:
        return None
    return lambda fraction: on_progress(start + share * min(max(fraction, 0.0), 1.0))


def never_back(on_progress: Optional[Callable[[float], None]]) -> Optional[Callable[[float], None]]:
    """A progress callback that passes on only what moves forward: a step that starts over (a second try
    of an export, or the CPU taking over from the graphics card) doesn't send the bar back to 0."""
    if on_progress is None:
        return None
    reached = [0.0]

    def report(fraction: float) -> None:
        if fraction > reached[0]:
            reached[0] = fraction
            on_progress(fraction)
    return report
