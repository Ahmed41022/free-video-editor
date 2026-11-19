"""Pieces of an ffmpeg command line: trimming, and long filter graphs moved into files."""

from __future__ import annotations

import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .winproc import _NO_WINDOW

# How far inside a frame's time the input is cut. Frames sit exactly on multiples of 1/fps, and a start
# that is rounded up (0.0416667 -> 0.042) drops the frame there, one that is rounded up at the end keeps
# one frame too many. Starting and stopping a hair early keeps exactly the frames asked for.
SEEK_SLACK = 0.0005


def trim_args(start: Optional[float], end: Optional[float]) -> list[str]:
    """Input options that keep only start..end of the source."""
    args = []
    if start:
        args += ["-ss", f"{max(start - SEEK_SLACK, 0.0):.6f}"]
    if end is not None:
        args += ["-to", f"{max(end - SEEK_SLACK, 0.0):.6f}"]
    return args


# A filter graph longer than this goes in a file: the whole command line may not pass 32767 characters on
# Windows, and a video with many cuts (Remove silences makes hundreds) has a graph of tens of thousands.
GRAPH_FILE_MIN = 1500
_graph_flags: dict[str, str] = {}


def _graph_flag(ffmpeg: str) -> str:
    """The ffmpeg option that reads a filter graph from a file: -/filter_complex since ffmpeg 7, before that
    -filter_complex_script (which newer versions still know, but call deprecated)."""
    if ffmpeg not in _graph_flags:
        flag = "-/filter_complex"
        try:
            text = subprocess.run(
                [ffmpeg, "-version"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                timeout=15).stdout
            match = re.search(r"version n?(\d+)\.", text)
            if match and int(match.group(1)) < 7:
                flag = "-filter_complex_script"
        except (OSError, subprocess.SubprocessError):
            pass
        _graph_flags[ffmpeg] = flag
    return _graph_flags[ffmpeg]


def has_long_graph(cmd: list[str]) -> bool:
    return any(
        arg == "-filter_complex" and i + 1 < len(cmd) and len(cmd[i + 1]) > GRAPH_FILE_MIN
        for i, arg in enumerate(cmd))


def with_graph_files(cmd: list[str], folder: Path) -> list[str]:
    """`cmd` with each long -filter_complex graph written to a file in `folder` and read from there."""
    result: list[str] = []
    i = 0
    while i < len(cmd):
        if cmd[i] == "-filter_complex" and i + 1 < len(cmd) and len(cmd[i + 1]) > GRAPH_FILE_MIN:
            path = folder / f"graph{len(result)}.txt"
            path.write_text(cmd[i + 1], encoding="utf-8")
            result += [_graph_flag(cmd[0]), str(path)]
            i += 2
        else:
            result.append(cmd[i])
            i += 1
    return result


@contextmanager
def graph_files(cmd: list[str]) -> Iterator[list[str]]:
    """The command to run for `cmd`: itself, or (when a graph is long) with the graph in a temporary file."""
    if not has_long_graph(cmd):
        yield cmd
        return
    with tempfile.TemporaryDirectory(prefix="fve-graph-", ignore_cleanup_errors=True) as folder:
        yield with_graph_files(cmd, Path(folder))
