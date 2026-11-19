"""Text, logo and subtitles together."""

from __future__ import annotations

from ..timing import Segment, output_duration
from .logo import logo_graph
from .model import Logo, Subtitles, TextItem
from .subtitles import subtitle_filter
from .text import text_filter


def overlay_graph(
        src: str, out: str, texts: list[TextItem], logo: Logo, subtitles: Subtitles,
        frame: tuple[int, int], segments: list[Segment], speed: float = 1.0, reverse: bool = False,
        tag: str = "") -> list[str]:
    """Graph statements that draw the logo, the subtitles and the texts (in that order, so text is
    on top) over stream `src`, ending in `out`. [] when there is nothing to draw.

    `frame` is the video's width and height at this point, `segments` the kept parts of the source
    (see `VideoEdit.segments`), which the subtitles' source times are moved through. The texts'
    times are already on the export's timeline."""
    chain = []
    if subtitles.file:
        chain.append(subtitle_filter(subtitles, segments, speed, reverse))
    total = output_duration(segments, speed) if segments else None
    chain += [f for f in (text_filter(item, frame, total) for item in texts) if f]
    statements: list[str] = []
    current = src
    if logo.file:
        mid = f"[lg{tag}]" if chain else out
        statements += logo_graph(current, mid, logo, frame, tag)
        current = mid
    if chain:
        statements.append(f"{current}{','.join(chain)}{out}")
    return statements
