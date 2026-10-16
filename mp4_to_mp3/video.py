"""Video editing: reframe for phones and other shapes, crop, rotate, trim, and export MP4."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .core import (
    _NO_WINDOW,
    ConversionError,
    MediaInfo,
    probe_media,
    run_ffmpeg,
    trim_args,
    trimmed_duration,
)

# Label -> (width, height) aspect ratio; None keeps the video's own shape.
FORMATS = {
    "Original shape": None,
    "Phone: Stories, Reels, TikTok, Shorts (9:16)": (9, 16),
    "Square post (1:1)": (1, 1),
    "Portrait post (4:5)": (4, 5),
    "Widescreen, YouTube (16:9)": (16, 9),
}
DEFAULT_FORMAT = "Phone: Stories, Reels, TikTok, Shorts (9:16)"

# Short name used in the output file name, e.g. "clip (phone).mp4".
FORMAT_TAGS = {None: "edited", (9, 16): "phone", (1, 1): "square", (4, 5): "portrait", (16, 9): "widescreen"}

FIT_MODES = {
    "crop": "Crop to fill the frame",
    "blur": "Fit inside, blurred background",
    "bars": "Fit inside, black bars",
}

ROTATIONS = {"None": 0, "90° clockwise": 90, "180°": 180, "90° counter-clockwise": 270}

# Length of the short side of the output; None = same as the source video.
RESOLUTIONS = {"Same as source": None, "1080p": 1080, "720p": 720, "480p": 480}

# x264 CRF: lower = better quality and bigger files.
VIDEO_QUALITY = {"High": 18, "Balanced": 23, "Smaller file": 28}
DEFAULT_VIDEO_QUALITY = "Balanced"


@dataclass
class VideoEdit:
    aspect: Optional[tuple[int, int]] = (9, 16)
    fit: str = "crop"
    pos_x: float = 0.5        # where the crop box sits: 0 = left/top, 1 = right/bottom
    pos_y: float = 0.5
    zoom: float = 1.0         # >1 crops tighter
    rotate: int = 0           # degrees clockwise: 0, 90, 180 or 270
    flip: bool = False        # mirror left-right
    resolution: Optional[int] = None
    crf: int = VIDEO_QUALITY[DEFAULT_VIDEO_QUALITY]
    mute: bool = False
    start: Optional[float] = None
    end: Optional[float] = None

    @property
    def crops(self) -> bool:
        return self.fit == "crop" or self.aspect is None


@dataclass
class Geometry:
    width: int                # size of the source after rotation
    height: int
    out_width: int            # size of the exported video
    out_height: int
    crop: Optional[tuple[int, int, int, int]] = None   # w, h, x, y in rotated source pixels


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def _even_floor(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def oriented_size(width: int, height: int, rotate: int) -> tuple[int, int]:
    return (height, width) if rotate in (90, 270) else (width, height)


def plan_geometry(width: int, height: int, edit: VideoEdit) -> Geometry:
    """Work out the output size and crop box for a `width` x `height` source."""
    w, h = oriented_size(width, height, edit.rotate)
    aw, ah = edit.aspect or (w, h)
    ratio = aw / ah

    short = edit.resolution or min(w, h)
    if ratio < 1:
        out_w, out_h = _even(short), _even(short / ratio)
    else:
        out_w, out_h = _even(short * ratio), _even(short)

    geometry = Geometry(w, h, out_w, out_h)
    if edit.crops:
        if w / h > ratio:
            crop_w, crop_h = h * ratio, h
        else:
            crop_w, crop_h = w, w / ratio
        zoom = min(max(edit.zoom, 1.0), 10.0)
        crop_w = min(_even_floor(crop_w / zoom), _even_floor(w))
        crop_h = min(_even_floor(crop_h / zoom), _even_floor(h))
        x = round((w - crop_w) * min(max(edit.pos_x, 0.0), 1.0))
        y = round((h - crop_h) * min(max(edit.pos_y, 0.0), 1.0))
        geometry.crop = (crop_w, crop_h, x, y)
    return geometry


def orientation_filters(edit: VideoEdit) -> list[str]:
    filters = {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}.get(edit.rotate, [])
    return filters + (["hflip"] if edit.flip else [])


def build_filter_graph(geometry: Geometry, edit: VideoEdit) -> str:
    """An ffmpeg -filter_complex graph that reads [0:v] and produces [v]."""
    tw, th = geometry.out_width, geometry.out_height
    pre = "".join(f"{f}," for f in orientation_filters(edit))

    if geometry.crop:
        cw, ch, cx, cy = geometry.crop
        return f"[0:v]{pre}crop={cw}:{ch}:{cx}:{cy},scale={tw}:{th}:flags=lanczos,setsar=1[v]"

    fit_inside = f"scale={tw}:{th}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    if edit.fit == "bars":
        return f"[0:v]{pre}{fit_inside},pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v]"

    # Blurred background: a zoomed-in, blurred copy fills the frame behind the whole video.
    # The blur runs at quarter size, which looks the same and is much faster.
    bw, bh = _even(tw / 4), _even(th / 4)
    return (
        f"[0:v]{pre}split=2[bg][fg];"
        f"[bg]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"gblur=sigma=6,eq=brightness=-0.08,scale={tw}:{th}[blurred];"
        f"[fg]{fit_inside}[front];"
        f"[blurred][front]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    )


def output_tag(edit: VideoEdit) -> str:
    return FORMAT_TAGS.get(edit.aspect, "edited")


@dataclass
class VideoJob:
    source: Path
    output: Path
    edit: VideoEdit


def video_command(ffmpeg: str, job: VideoJob, info: MediaInfo) -> list[str]:
    edit = job.edit
    geometry = plan_geometry(info.width, info.height, edit)
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        *trim_args(edit.start, edit.end),
        "-i", str(job.source),
        "-filter_complex", build_filter_graph(geometry, edit),
        "-map", "[v]",
    ]
    if info.has_audio and not edit.mute:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", str(edit.crf), "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-map_metadata", "0",
        "-progress", "pipe:1", "-nostats",
        str(job.output),
    ]
    return cmd


def convert_video(
    ffmpeg: str,
    job: VideoJob,
    on_progress: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    info: Optional[MediaInfo] = None,
) -> Path:
    """Export `job.source` as an edited MP4 at `job.output`."""
    if not job.source.is_file():
        raise ConversionError(f"File not found: {job.source}")
    edit = job.edit
    if edit.start is not None and edit.end is not None and edit.end <= edit.start:
        raise ConversionError("The trim end time must be after the start time.")
    info = info or probe_media(ffmpeg, job.source)
    if not info.has_video:
        raise ConversionError("This file has no video track.")

    duration = trimmed_duration(info.duration, edit.start, edit.end)
    return run_ffmpeg(video_command(ffmpeg, job, info), job.output, duration, on_progress, cancel_event)


def _grab_frames(ffmpeg: str, source: Path, at: float, graph: str, outputs: dict[str, Path]) -> None:
    """Run `graph` on the frame at `at` seconds, saving each labelled output as a PNG."""
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{max(at, 0.0):.3f}", "-i", str(source), "-filter_complex", graph,
    ]
    for label, png in outputs.items():
        cmd += ["-map", f"[{label}]", "-frames:v", "1", "-update", "1", str(png)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=60,
    )
    if result.returncode != 0 or not all(png.is_file() for png in outputs.values()):
        raise ConversionError(result.stderr.strip() or "Couldn't read a frame from this video.")


def _shrink(max_size: tuple[int, int]) -> str:
    return f"scale={max_size[0]}:{max_size[1]}:force_original_aspect_ratio=decrease"


def render_frame(
    ffmpeg: str,
    source: Path,
    output_png: Path,
    at: float,
    max_size: tuple[int, int],
    edit: VideoEdit,
    info: MediaInfo,
) -> Path:
    """Save the edited frame at `at` seconds as a PNG no larger than `max_size`."""
    graph = build_filter_graph(plan_geometry(info.width, info.height, edit), edit)
    _grab_frames(ffmpeg, source, at, f"{graph};[v]{_shrink(max_size)}[p]", {"p": output_png})
    return output_png


def render_source_frames(
    ffmpeg: str,
    source: Path,
    normal_png: Path,
    dimmed_png: Path,
    at: float,
    max_size: tuple[int, int],
    edit: VideoEdit,
) -> None:
    """Save the source frame (rotated and flipped only) plus a dimmed copy of it.

    The editor shows the dimmed copy with the bright crop area drawn on top.
    """
    pre = "".join(f"{f}," for f in orientation_filters(edit))
    graph = f"[0:v]{pre}{_shrink(max_size)},split=2[n][d];[d]eq=brightness=-0.3:saturation=0.5[dim]"
    _grab_frames(ffmpeg, source, at, graph, {"n": normal_png, "dim": dimmed_png})
