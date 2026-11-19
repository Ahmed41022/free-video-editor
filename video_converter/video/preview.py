"""Single pictures: the edited frame, and the frames before and after."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from ..captions import Subtitles, overlay_graph
from ..core import SEEK_SLACK, ConversionError, MediaInfo, graph_files
from ..core.winproc import _NO_WINDOW
from ..i18n import tr
from ..timing import fade_filters, output_duration, source_to_output
from .edit import VideoEdit
from .filters import build_filter_graph, orientation_filters, picture_filters
from .geometry import plan_geometry
from .memory import DEFAULT_FPS


def _grab_frames(
        ffmpeg: str, source: Path, at: float, graph: str, outputs: dict[str, Path],
        info: Optional[MediaInfo] = None) -> None:
    """Run `graph` on the frame at `at` seconds, saving each labelled output as a PNG.

    `at` is held to the last frame of the video (when `info` says how long it is): a seek to or
    past the end finds no frame at all. Pictures from an earlier run are deleted first, so a run
    that fails can't leave an old one to be taken for its result."""
    at = max(at, 0.0)
    if info and info.duration:
        at = min(at, max(info.duration - 1.0 / (info.fps or DEFAULT_FPS), 0.0))
    for png in outputs.values():
        try:
            png.unlink(missing_ok=True)
        except OSError:
            pass

    def attempt(seek: list[str], once: bool) -> subprocess.CompletedProcess:
        cmd = [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *seek, "-i",
            str(source), "-filter_complex", graph
        ]
        for label, png in outputs.items():
            cmd += [
                "-map", f"[{label}]", *(["-frames:v", "1"] if once else []), "-update", "1",
                str(png)
            ]
        with graph_files(cmd) as run_cmd:
            try:
                return subprocess.run(
                    run_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=60,
                )
            except OSError as exc:
                raise ConversionError(tr("Couldn't start ffmpeg: {error}", error=exc)) from exc

    result = attempt(["-ss", f"{max(at - SEEK_SLACK, 0.0):.6f}"], once=True)
    if result.returncode != 0 or not all(png.is_file() for png in outputs.values()):
        # Nothing at that time (a video whose last frame is earlier than it says): take the last
        # frame of its final second, which is what writing every frame to the same file ends with.
        result = attempt(["-sseof", "-1"], once=False)
    if result.returncode != 0 or not all(png.is_file() for png in outputs.values()):
        raise ConversionError(
            result.stderr.strip() or tr("Couldn't read a frame from this video."))


def _shrink(max_size: tuple[int, int]) -> str:
    return f"scale={max_size[0]}:{max_size[1]}:force_original_aspect_ratio=decrease"


def render_frame(
    ffmpeg: str, source: Path, output_png: Path, at: float, max_size: tuple[int, int],
    edit: VideoEdit, info: MediaInfo,
) -> Path:
    """Save the edited frame at source time `at` as a PNG no larger than `max_size`.

    The frame is reframed and adjusted like the export, then stamped with the time it will have
    in the export and run through the export's own fade filters, so fades look the same too.
    The text, logo and subtitles are drawn at the export's size before the frame is shrunk, with
    the same filters, so they match the export. A frame that is cut out of the export gets only the
    logo. Stabilization is left out: it needs the whole clip."""
    geometry = plan_geometry(info.width, info.height, edit)
    graph = build_filter_graph(geometry, edit, out="[framed]")
    segments = edit.segments(info.duration)
    when = source_to_output(at, segments, edit.speed, edit.reverse)
    chain = picture_filters(edit)
    if when is not None:
        chain.append(f"setpts={when:.3f}/TB")
        chain += fade_filters(
            edit.fade_in, edit.fade_out, output_duration(segments, edit.speed), audio=False)
    kept = when is not None
    overlays = overlay_graph(
        "[drawn]", "[over]", edit.texts if kept else [], edit.logo,
        edit.subtitles if kept else Subtitles(), (geometry.out_width, geometry.out_height),
        segments, edit.speed, edit.reverse)
    if overlays:
        stages = [
            f"{graph};[framed]{','.join(chain) or 'null'}[drawn]", *overlays,
            f"[over]{_shrink(max_size)}[p]"
        ]
    else:
        stages = [f"{graph};[framed]{','.join([*chain, _shrink(max_size)])}[p]"]
    _grab_frames(ffmpeg, source, at, ";".join(stages), {"p": output_png}, info)
    return output_png


def render_source_frames(
    ffmpeg: str, source: Path, normal_png: Path, dimmed_png: Path, at: float,
    max_size: tuple[int, int], edit: VideoEdit, info: Optional[MediaInfo] = None,
) -> None:
    """Save the source frame (rotated and flipped only) plus a dimmed copy of it.

    The editor shows the dimmed copy with the bright crop area drawn on top.
    """
    pre = "".join(f"{f}," for f in orientation_filters(edit))
    graph = (
        f"[0:v]{pre}{_shrink(max_size)},split=2[n][d];[d]eq=brightness=-0.3:saturation=0."
        "5[dim]")
    _grab_frames(ffmpeg, source, at, graph, {"n": normal_png, "dim": dimmed_png}, info)
