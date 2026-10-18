"""Video editing: reframe for phones and other shapes, crop, rotate, cut, change speed, fade,
join clips, and export MP4."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .core import (
    _NO_WINDOW,
    DEFAULT_QUALITY,
    QUALITY_PRESETS,
    ConversionError,
    MediaInfo,
    check_trim,
    no_segments_error,
    probe_media,
    run_ffmpeg,
    trim_args,
)
from .timing import (
    Range,
    Segment,
    audio_filters,
    clamp_speed,
    fade_filters,
    kept_segments,
    output_duration,
    relative,
    select_filters,
    source_to_output,
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
    keep: list[Range] = field(default_factory=list)   # kept parts of the source; [] = no cuts
    speed: float = 1.0        # 0.25 to 4
    reverse: bool = False
    fade_in: float = 0.0      # picture fades, in seconds
    fade_out: float = 0.0
    audio_fade_in: float = 0.0
    audio_fade_out: float = 0.0

    @property
    def crops(self) -> bool:
        return self.fit == "crop" or self.aspect is None

    def segments(self, duration: Optional[float]) -> list[Segment]:
        """The parts of a source `duration` seconds long that the export keeps."""
        return kept_segments(self.keep, self.start, self.end, duration)


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


def build_filter_graph(geometry: Geometry, edit: VideoEdit, src: str = "[0:v]", out: str = "[v]",
                       tag: str = "") -> str:
    """An ffmpeg -filter_complex graph that reframes `src` (rotate, crop or fit, scale) into `out`.
    `tag` keeps its internal labels apart from those of other clips in the same graph."""
    tw, th = geometry.out_width, geometry.out_height
    pre = "".join(f"{f}," for f in orientation_filters(edit))

    if geometry.crop:
        cw, ch, cx, cy = geometry.crop
        return f"{src}{pre}crop={cw}:{ch}:{cx}:{cy},scale={tw}:{th}:flags=lanczos,setsar=1{out}"

    fit_inside = f"scale={tw}:{th}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    if edit.fit == "bars":
        return f"{src}{pre}{fit_inside},pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1{out}"

    # Blurred background: a zoomed-in, blurred copy fills the frame behind the whole video.
    # The blur runs at quarter size, which looks the same and is much faster.
    bw, bh = _even(tw / 4), _even(th / 4)
    return (
        f"{src}{pre}split=2[bg{tag}][fg{tag}];"
        f"[bg{tag}]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"gblur=sigma=6,eq=brightness=-0.08,scale={tw}:{th}[blurred{tag}];"
        f"[fg{tag}]{fit_inside}[front{tag}];"
        f"[blurred{tag}][front{tag}]overlay=(W-w)/2:(H-h)/2,setsar=1{out}"
    )


@dataclass
class ClipGraph:
    """The filters for one input of an export."""
    filters: list[str]
    duration: Optional[float]      # length in the export, when known
    has_audio: bool


def clip_graph(index: int, edit: VideoEdit, info: MediaInfo, segments: list[Segment], *,
               video: bool = True, audio: bool = True, frame: Optional[tuple[int, int]] = None,
               fps: Optional[float] = None, v_out: str = "[v]", a_out: str = "[a]") -> ClipGraph:
    """Filters that take input `index` through cuts, speed, reframing, reverse and fades.

    The input must start at the first segment (see `input_args`). `frame` and `fps` force the
    output size and frame rate, for joining clips: a clip whose shape differs is fitted inside
    that frame with black bars."""
    tag = str(index)
    rel = relative(segments)
    speed = clamp_speed(edit.speed)
    duration = output_duration(rel, speed)
    filters: list[str] = []
    if video:
        timed = f"[t{tag}]"
        filters += select_filters(f"[{index}:v:0]", timed, rel, audio=False, tag=tag)
        tempo = [f"setpts=PTS/{speed:.6g}"] if speed != 1.0 else []
        # A faster clip would otherwise have more frames per second than its source.
        rate = fps or (info.fps if speed != 1.0 else None)
        if rate:
            tempo.append(f"fps={rate:.6g}")
        if tempo:
            filters.append(f"{timed}{','.join(tempo)}[tempo{tag}]")
            timed = f"[tempo{tag}]"
        framed = f"[f{tag}]"
        filters.append(build_filter_graph(plan_geometry(info.width, info.height, edit), edit, timed, framed, tag))
        # Reverse after scaling: it holds every frame in memory, so smaller frames help.
        post = ["reverse"] if edit.reverse else []
        post += fade_filters(edit.fade_in, edit.fade_out, duration, audio=False)
        if frame:
            w, h = frame
            post += [f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black", "setsar=1", "format=yuv420p"]
        filters.append(f"{framed}{','.join(post) or 'null'}{v_out}")
    has_audio = audio and info.has_audio and not (video and edit.mute)
    if has_audio:
        filters.append(audio_filters(f"[{index}:a:0]", a_out, segments, speed, edit.reverse,
                                     edit.audio_fade_in, edit.audio_fade_out, tag=tag, always=True))
    return ClipGraph(filters, duration, has_audio)


def input_args(segments: list[Segment], source: Path) -> list[str]:
    """Seek straight to the first kept part and stop reading after the last one."""
    return [*trim_args(segments[0][0], segments[-1][1]), "-i", str(source)]


def output_tag(edit: VideoEdit) -> str:
    return FORMAT_TAGS.get(edit.aspect, "edited")


@dataclass
class VideoJob:
    source: Path
    output: Path
    edit: VideoEdit


_HEAD = ["-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
_TAIL = ["-progress", "pipe:1", "-nostats"]
_X264 = ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
_AAC = ["-c:a", "aac", "-b:a", "192k"]


def video_command(ffmpeg: str, job: VideoJob, info: MediaInfo) -> list[str]:
    edit = job.edit
    segments = edit.segments(info.duration)
    if not segments:
        raise no_segments_error()
    graph = clip_graph(0, edit, info, segments)
    cmd = [ffmpeg, *_HEAD, *input_args(segments, job.source),
           "-filter_complex", ";".join(graph.filters), "-map", "[v]"]
    cmd += ["-map", "[a]", *_AAC] if graph.has_audio else ["-an"]
    cmd += [*_X264, "-crf", str(edit.crf), "-map_metadata", "0", *_TAIL, str(job.output)]
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
    check_trim(job.edit.start, job.edit.end)
    info = info or probe_media(ffmpeg, job.source)
    if not info.has_video:
        raise ConversionError("This file has no video track.")

    cmd = video_command(ffmpeg, job, info)
    duration = output_duration(job.edit.segments(info.duration), job.edit.speed)
    return run_ffmpeg(cmd, job.output, duration, on_progress, cancel_event)


# ----- joining clips ------------------------------------------------------------

JOIN_AUDIO = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


@dataclass
class JoinPart:
    source: Path
    edit: VideoEdit
    info: Optional[MediaInfo] = None


@dataclass
class JoinJob:
    parts: list[JoinPart]
    output: Path
    audio_only: bool = False           # True: one MP3 instead of one MP4
    quality: str = DEFAULT_QUALITY     # MP3 quality


def join_command(ffmpeg: str, job: JoinJob) -> tuple[list[str], float]:
    """The ffmpeg command that plays every part back to back, and the result's length.

    Each clip is cut, sped up, reframed and faded with its own settings, then fitted into
    the first clip's frame size and frame rate. The video quality is the first clip's."""
    parts, video = job.parts, not job.audio_only
    first = parts[0]
    frame = fps = None
    if video:
        geometry = plan_geometry(first.info.width, first.info.height, first.edit)
        frame = (geometry.out_width, geometry.out_height)
        fps = first.info.fps or 30.0
    any_audio = any(p.info.has_audio and not (video and p.edit.mute) for p in parts)
    if not video and not any_audio:
        raise ConversionError("None of these files have sound.")

    inputs, filters, labels, total = [], [], [], 0.0
    for i, part in enumerate(parts):
        segments = part.edit.segments(part.info.duration)
        if not segments:
            raise ConversionError(f"Nothing is left of {part.source.name}: the trim and cuts remove all of it.")
        graph = clip_graph(i, part.edit, part.info, segments, video=video, audio=any_audio, frame=frame,
                           fps=fps, v_out=f"[v{i}]", a_out=f"[sound{i}]")
        if graph.duration is None:
            raise ConversionError(f"Couldn't read the length of {part.source.name}.")
        inputs += input_args(segments, part.source)
        filters += graph.filters
        total += graph.duration
        label = f"[v{i}]" if video else ""
        if any_audio:
            if graph.has_audio:
                filters.append(f"[sound{i}]{JOIN_AUDIO}[a{i}]")
            else:   # silence, so the sound stays in step with the pictures
                filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={graph.duration:.3f},{JOIN_AUDIO}[a{i}]")
            label += f"[a{i}]"
        labels.append(label)
    outputs = ("[v]" if video else "") + ("[a]" if any_audio else "")
    filters.append(f"{''.join(labels)}concat=n={len(parts)}:v={int(video)}:a={int(any_audio)}{outputs}")

    cmd = [ffmpeg, *_HEAD, *inputs, "-filter_complex", ";".join(filters)]
    if video:
        cmd += ["-map", "[v]", *(["-map", "[a]", *_AAC] if any_audio else ["-an"]),
                *_X264, "-crf", str(first.edit.crf)]
    else:
        cmd += ["-map", "[a]", "-vn", "-c:a", "libmp3lame", *QUALITY_PRESETS[job.quality]]
    cmd += ["-map_metadata", "0", *_TAIL, str(job.output)]
    return cmd, total


def join_clips(
    ffmpeg: str,
    job: JoinJob,
    on_progress: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Path:
    """Export several clips, in order, as one MP4 (or one MP3 with `job.audio_only`)."""
    if len(job.parts) < 2:
        raise ConversionError("Joining needs at least two clips.")
    for part in job.parts:
        if not part.source.is_file():
            raise ConversionError(f"File not found: {part.source}")
        check_trim(part.edit.start, part.edit.end)
        part.info = part.info or probe_media(ffmpeg, part.source)
        if not job.audio_only and not part.info.has_video:
            raise ConversionError(f"{part.source.name} has no video track.")
    cmd, duration = join_command(ffmpeg, job)
    return run_ffmpeg(cmd, job.output, duration, on_progress, cancel_event)


# ----- preview ------------------------------------------------------------------

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
    """Save the edited frame at source time `at` as a PNG no larger than `max_size`.

    The frame is reframed like the export, then stamped with the time it will have in the
    export and run through the export's own fade filters, so fades look the same too."""
    graph = build_filter_graph(plan_geometry(info.width, info.height, edit), edit, out="[framed]")
    segments = edit.segments(info.duration)
    when = source_to_output(at, segments, edit.speed, edit.reverse)
    chain = []
    if when is not None:
        chain.append(f"setpts={when:.3f}/TB")
        chain += fade_filters(edit.fade_in, edit.fade_out, output_duration(segments, edit.speed), audio=False)
    chain.append(_shrink(max_size))
    _grab_frames(ffmpeg, source, at, f"{graph};[framed]{','.join(chain)}[p]", {"p": output_png})
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
