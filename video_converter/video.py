"""Video editing: reframe for phones and other shapes, crop, rotate, cut, change speed, fade,
adjust the picture, stabilize, join clips, and export MP4."""

from __future__ import annotations

import logging
import math
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

from .core import (
    _NO_WINDOW,
    DEFAULT_QUALITY,
    LOUDNESS_SHARE,
    LOUDNESS_SHARE_AUDIO,
    QUALITY_PRESETS,
    ConversionCancelled,
    ConversionError,
    MediaInfo,
    check_music,
    check_trim,
    measure_loudness,
    music_length_error,
    no_segments_error,
    probe_media,
    run_ffmpeg,
    scaled,
    trim_args,
)
from .gpu import CPU_FALLBACK_NOTE, encoder_args
from .sound import (
    DEFAULT_MUSIC_VOLUME,
    STEREO_48K,
    Loudness,
    Sound,
    master_filters,
    music_args,
    voice_filters,
)
from .timing import (
    Range,
    Segment,
    clamp_speed,
    fade_filters,
    frame_margin,
    kept_segments,
    output_duration,
    relative,
    segments_length,
    select_filters,
    snap_segments,
    source_to_output,
)

log = logging.getLogger(__name__)

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

# "Up to 1080p" is stored as minus 1080: a size that is a limit, not a target.
UP_TO_1080 = -1080

# Length of the short side of the output. None = as large as the picture being exported, never
# enlarged: see plan_geometry. UP_TO_1080 = the same, but never above 1080.
RESOLUTIONS = {"Same as source": None, "Up to 1080p": UP_TO_1080, "1080p": 1080, "720p": 720, "480p": 480}

# Steady frame rates the user can ask for; None keeps the source's own (which may vary).
FRAME_RATES = {"Same as source": None, "Steady 60 fps": 60, "Steady 30 fps": 30}

# With "Same as source", phone, portrait and square videos are not made bigger than this (short side).
SHORT_SIDE_CAP = 1080

# x264 CRF: lower = better quality and bigger files.
VIDEO_QUALITY = {"High": 18, "Balanced": 23, "Smaller file": 28}
DEFAULT_VIDEO_QUALITY = "Balanced"

# Picture adjustments: field -> (lowest, highest, neutral value).
ADJUSTMENTS = {
    "brightness": (-0.5, 0.5, 0.0),     # eq brightness
    "contrast": (0.5, 2.0, 1.0),        # eq contrast
    "saturation": (0.0, 3.0, 1.0),      # eq saturation; 0 = grey
    "gamma": (0.5, 2.0, 1.0),           # eq gamma
    "sharpen": (0.0, 1.5, 0.0),         # unsharp luma amount
    "denoise": (0.0, 10.0, 0.0),        # hqdn3d strength
}

# Color looks: key -> (label, ffmpeg filters).
LOOKS = {
    "none": ("None", []),
    "bw": ("Black and white", ["hue=s=0"]),
    "warm": ("Warm", ["colorbalance=rs=0.08:gs=0.02:bs=-0.08:rm=0.06:bm=-0.06"]),
    "cool": ("Cool", ["colorbalance=rs=-0.08:bs=0.08:rm=-0.06:bm=0.06"]),
    "vintage": ("Vintage", ["curves=preset=vintage", "eq=saturation=0.85"]),
    "contrast": ("High contrast", ["curves=preset=strong_contrast"]),
}

# Stabilization strength -> (vidstabdetect shakiness, vidstabtransform smoothing), or None for off.
STABILIZE = {"off": None, "light": (5, 10), "strong": (8, 30)}


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
    use_gpu: bool = True      # let the graphics card encode when there is a working one
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
    brightness: float = 0.0   # picture adjustments, see ADJUSTMENTS
    contrast: float = 1.0
    saturation: float = 1.0
    gamma: float = 1.0
    sharpen: float = 0.0
    denoise: float = 0.0
    look: str = "none"        # a LOOKS key
    stabilize: str = "off"    # a STABILIZE key; applied when exporting only
    frame_rate: Optional[int] = None   # a FRAME_RATES value: a steady, phone-safe export; None = as source
    volume: float = 0.0       # sound, see sound.py: the volume in dB
    normalize: bool = False   # even out the loudness (-14 LUFS), in two passes
    audio_denoise: str = "off"      # an AUDIO_DENOISE key
    music: str = ""           # background music file; "" = none
    music_volume: float = DEFAULT_MUSIC_VOLUME      # dB, next to the video's own sound
    music_duck: bool = False  # lower the music while the video's sound plays

    @property
    def sound(self) -> Sound:
        return Sound(self.volume, self.normalize, self.audio_denoise, self.music, self.music_volume, self.music_duck)

    @property
    def crops(self) -> bool:
        return self.fit == "crop" or self.aspect is None

    def segments(self, duration: Optional[float], fps: Optional[float] = None) -> list[Segment]:
        """The parts of a source `duration` seconds long that the export keeps. Given the source's
        `fps`, their edges sit on whole frames (see `snap_segments`)."""
        return snap_segments(kept_segments(self.keep, self.start, self.end, duration), fps, duration)


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
    """Work out the output size and crop box for a `width` x `height` source.

    The output's short side is `edit.resolution` when that is set (a negative one, UP_TO_1080, is a
    limit: smaller pictures keep their size). Otherwise it is the source's
    short side, so a 1080p video makes a standard 1080x1920 phone video, and phone, portrait and
    square videos stop at SHORT_SIDE_CAP, so a 4K source doesn't turn into a huge 2160x3840 file."""
    w, h = oriented_size(width, height, edit.rotate)
    aw, ah = edit.aspect or (w, h)
    ratio = aw / ah

    crop = None
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
        crop = (crop_w, crop_h, x, y)

    resolution = edit.resolution
    if resolution and resolution < 0:
        # "Up to": the source's own size, unless its short side is above the limit.
        resolution = -resolution if min(w, h) > -resolution else None
    if not resolution and edit.aspect is None:
        # The original shape at "same as source" is exactly the source size (made even).
        return Geometry(w, h, _even_floor(w), _even_floor(h), crop)
    if resolution:
        short = resolution
    else:
        short = min(w, h)
        if edit.aspect and ratio <= 1:
            short = min(short, SHORT_SIDE_CAP)
    short = _even(short)
    # The other side follows from the exact ratio (360 -> 640 for 9:16), not from an odd source size.
    if ratio < 1:
        out_w, out_h = short, _even(short * ah / aw)
    else:
        out_w, out_h = _even(short * aw / ah), short
    return Geometry(w, h, out_w, out_h, crop)


# ----- steady frame rate and phone-safe encoding ---------------------------------

FAST_FRAME_RATE = 60.5      # above this (59.94 and 60 are fine) some phones give up
# A source this much slower than the chosen steady rate keeps its own rate, not the chosen one.
SLOW_SOURCE = 0.9
# Rates with a fraction (NTSC) that a plain 29.97 would only approximate.
_NTSC_RATES = (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001))


def _source_rate(fps: float) -> Fraction:
    """A source's frame rate as an exact rate: 23.98 -> 24000/1001, 25 -> 25, 24.7 -> 25."""
    for rate in _NTSC_RATES:
        if abs(fps - float(rate)) <= 0.02:
            return rate
    return Fraction(max(round(fps), 1))


def steady_rate(chosen: Optional[int], source_fps: Optional[float]) -> Optional[Fraction]:
    """The constant frame rate to export at, or None when the export keeps the source's own.

    `chosen` is the user's Steady 60 / Steady 30 (a VideoEdit.frame_rate) and `source_fps` the
    picture's frame rate as it will arrive at the end of the edit (after any speed change). The
    result is never above `chosen`, and a source that is clearly slower than `chosen` (more than
    10% below it, so a 58 fps game recording still counts as 60) keeps its own rate instead of
    having frames repeated: a 25 fps video stays at 25 fps, steady."""
    if not chosen:
        return None
    if not source_fps or source_fps >= chosen * SLOW_SOURCE:
        return Fraction(chosen)
    return min(_source_rate(source_fps), Fraction(chosen))


def edit_rate(edit: VideoEdit, info: MediaInfo) -> Optional[Fraction]:
    """The steady rate for exporting `info` with `edit`, or None (see `steady_rate`)."""
    return steady_rate(edit.frame_rate, info.fps * clamp_speed(edit.speed) if info.fps else None)


def _rate_text(rate: float | Fraction) -> str:
    """A frame rate as ffmpeg takes it: 30, 24000/1001, or (a plain number) 29.97."""
    if isinstance(rate, Fraction):
        return str(rate.numerator) if rate.denominator == 1 else f"{rate.numerator}/{rate.denominator}"
    return f"{rate:.6g}"


# H.264 levels from 4.1 up: (name, biggest picture in 16x16 blocks, most blocks a second). Phones that
# play 1080p60 want 4.2; 4.1 is 1080p30. Smaller pictures also say 4.1, which every phone plays.
_H264_LEVELS = (
    ("4.1", 8192, 245760), ("4.2", 8192, 522240), ("5.0", 22080, 589824), ("5.1", 36864, 983040),
    ("5.2", 36864, 2073600), ("6.0", 139264, 4177920), ("6.1", 139264, 8355840), ("6.2", 139264, 16711680),
)


def h264_level(width: int, height: int, fps: float) -> str:
    """The lowest H.264 level (from 4.1) that a `width` x `height` picture at `fps` fits, like "4.2"."""
    blocks = math.ceil(width / 16) * math.ceil(height / 16)
    for name, frame_limit, rate_limit in _H264_LEVELS:
        if blocks <= frame_limit and blocks * fps <= rate_limit:
            return name
    return _H264_LEVELS[-1][0]


def frame_rate_hint(info: MediaInfo) -> str:
    """What to tell the user about a source whose frame rate may not play on a phone, or "" when
    there is nothing to say. Shown while the frame rate is left at "Same as source"."""
    if info.variable_frame_rate and info.max_fps:
        return (f"This video's frame rate changes (up to {info.max_fps:g} fps), which some phones can't play. "
                "Choose Steady 60 fps.")
    if info.fps and info.fps > FAST_FRAME_RATE:
        return f"This video runs at {info.fps:g} fps, which some phones can't play. Choose Steady 60 fps."
    return ""


# ----- memory for reverse -------------------------------------------------------

DEFAULT_FPS = 30.0                     # assumed when the video doesn't say
REVERSE_WARN_BYTES = 2 * 1024 ** 3     # more than this is worth a warning
_SOUND_BYTES_PER_SECOND = 48000 * 2 * 4    # stereo floats, which is what the sound is reversed as


def reverse_memory(edit: VideoEdit, info: MediaInfo, fps: Optional[float] = None, sound_only: bool = False) -> int:
    """About how many bytes ffmpeg needs to play this clip backwards, or 0 when it isn't reversed
    (or its length is unknown).

    Reversing holds every frame of the result in memory, as raw yuv420p: width x height x 1.5
    bytes each. The size is the exported one (see `plan_geometry`), the number of frames comes from
    the kept, sped-up length at `fps` (a join's frame rate; the clip's own by default, or 30 when
    unknown). A `sound_only` export holds just the sound instead."""
    if not edit.reverse:
        return 0
    segments = edit.segments(info.duration)
    length, seconds = segments_length(segments), output_duration(segments, edit.speed)
    if length is None or seconds is None:
        return 0
    if sound_only:
        return int(seconds * _SOUND_BYTES_PER_SECOND)
    if not info.has_video:
        return 0
    geometry = plan_geometry(info.width, info.height, edit)
    steady = edit_rate(edit, info)
    rate = fps or (float(steady) if steady else info.fps)
    frames = seconds * rate if rate else length * DEFAULT_FPS
    return int(frames * geometry.out_width * geometry.out_height * 1.5)


def format_memory(nbytes: float) -> str:
    gigabytes = nbytes / 1024 ** 3
    return f"{gigabytes:.1f} GB" if gigabytes >= 1 else f"{max(round(nbytes / 1024 ** 2), 1)} MB"


def reverse_warning(nbytes: int) -> str:
    """The warning to show next to a reversed clip that needs `nbytes`, or "" when that is fine."""
    if nbytes <= REVERSE_WARN_BYTES:
        return ""
    return (f"Reversing this needs about {format_memory(nbytes)} of memory. "
            "Shorten the clip or lower the size if your PC has less.")


def orientation_filters(edit: VideoEdit) -> list[str]:
    filters = {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}.get(edit.rotate, [])
    return filters + (["hflip"] if edit.flip else [])


def _num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def picture_filters(edit: VideoEdit) -> list[str]:
    """Denoise, brightness/contrast/saturation/gamma, the color look, then sharpening."""
    def value(name: str) -> float:
        low, high, _ = ADJUSTMENTS[name]
        return min(max(getattr(edit, name), low), high)

    filters = []
    if value("denoise") > 0:
        filters.append(f"hqdn3d={_num(value('denoise'))}")
    eq = [f"{name}={_num(value(name))}" for name in ("brightness", "contrast", "saturation", "gamma")
          if abs(value(name) - ADJUSTMENTS[name][2]) > 1e-6]
    if eq:
        filters.append("eq=" + ":".join(eq))
    filters += LOOKS.get(edit.look, LOOKS["none"])[1]
    if value("sharpen") > 0:
        filters.append(f"unsharp=5:5:{_num(value('sharpen'))}:5:5:0")
    return filters


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
               fps: Optional[float | Fraction] = None, v_out: str = "[v]", a_out: str = "[a]",
               transforms: Optional[str] = None, picture: bool = True) -> ClipGraph:
    """Filters that take input `index` through cuts, stabilization, speed, reframing, picture
    adjustments, reverse and fades; the sound goes through its cuts, speed, reverse, fades, noise
    reduction and volume. With `picture` off (the loudness pass) only the sound is built, though a
    muted video export still has none.

    The input must start at the first segment (see `input_args`), and `segments` should come from
    `edit.segments(duration, fps)` so their edges sit on whole frames. `frame` and `fps` force the
    output size and frame rate, for joining clips (and `fps` for a steady frame rate): a clip whose
    shape differs is fitted inside that frame with black bars. The `fps` filter comes after the cuts
    and the speed change, so every frame the export keeps lands on the steady grid in its final time.
    `transforms` names the file from the stabilizer's detect pass, relative to ffmpeg's working folder."""
    tag = str(index)
    rel = relative(segments)
    speed = clamp_speed(edit.speed)
    duration = output_duration(rel, speed)
    filters: list[str] = []
    if video and picture:
        timed = f"[t{tag}]"
        filters += select_filters(f"[{index}:v:0]", timed, rel, audio=False, tag=tag,
                                 margin=frame_margin(info.fps))
        tempo = []
        strength = STABILIZE.get(edit.stabilize)
        if transforms and strength:
            # Right after the cuts, so the frames match the ones the detect pass saw.
            tempo.append(f"vidstabtransform=input={transforms}:smoothing={strength[1]}:optzoom=1:interpol=bicubic")
        if speed != 1.0:
            tempo.append(f"setpts=PTS/{speed:.6g}")
        # A faster clip would otherwise have more frames per second than its source.
        rate = fps or (info.fps if speed != 1.0 else None)
        if rate:
            tempo.append(f"fps={_rate_text(rate)}")
        if tempo:
            filters.append(f"{timed}{','.join(tempo)}[tempo{tag}]")
            timed = f"[tempo{tag}]"
        framed = f"[f{tag}]"
        filters.append(build_filter_graph(plan_geometry(info.width, info.height, edit), edit, timed, framed, tag))
        # Reverse after scaling: it holds every frame in memory, so smaller frames help.
        post = picture_filters(edit)
        post += ["reverse"] if edit.reverse else []
        post += fade_filters(edit.fade_in, edit.fade_out, duration, audio=False)
        if frame:
            w, h = frame
            post += [f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black", "setsar=1", "format=yuv420p"]
        filters.append(f"{framed}{','.join(post) or 'null'}{v_out}")
    has_audio = audio and info.has_audio and not (video and edit.mute)
    if has_audio:
        filters.append(voice_filters(f"[{index}:a:0]", a_out, segments, edit.sound, speed, edit.reverse,
                                     edit.audio_fade_in, edit.audio_fade_out, tag=tag, always=True))
    return ClipGraph(filters, duration, has_audio)


def input_args(segments: list[Segment], source: Path) -> list[str]:
    """Seek straight to the first kept part and stop reading after the last one."""
    return [*trim_args(segments[0][0], segments[-1][1]), "-i", str(source)]


def output_tag(edit: VideoEdit) -> str:
    """The word in the output's file name: "clip (phone).mp4". The original shape at a steady frame
    rate is the "make it play on my phone" export, so it says so."""
    if edit.aspect is None and edit.frame_rate:
        return "phone-ready"
    return FORMAT_TAGS.get(edit.aspect, "edited")


@dataclass
class VideoJob:
    source: Path
    output: Path
    edit: VideoEdit


_HEAD = ["-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
_HEAD_INFO = ["-hide_banner", "-nostdin", "-loglevel", "info", "-y"]       # loudnorm prints its result at this level
_TAIL = ["-progress", "pipe:1", "-nostats"]
_X264 = ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
_FASTSTART = ["-movflags", "+faststart"]
_AAC = ["-c:a", "aac", "-b:a", "192k"]
# What every phone plays: AAC LC at 48 kHz in stereo (more channels are mixed down).
_AAC_PHONE = [*_AAC, "-ar", "48000", "-ac", "2"]


def video_codec_args(crf: int, encoder: Optional[str] = None, fps: Optional[float] = None,
                     level: Optional[str] = None) -> list[str]:
    """How to encode the picture at quality `crf`: with the hardware `encoder`, or on the CPU (x264).
    `fps` is the frame rate of the result, when known. With a `level` (see `h264_level`) the video is
    H.264 High profile at that level, which is how a phone-safe export says what it needs."""
    if encoder:
        codec = encoder_args(encoder, crf, fps or DEFAULT_FPS, level)
    else:
        codec = [*_X264, "-crf", str(crf)] + (["-profile:v", "high", "-level", level] if level else [])
    return [*codec, *_FASTSTART]


def music_path(edit: VideoEdit, video: bool = True) -> Optional[str]:
    """The background music to lay under an export of `edit`, as a full path, or None. A video export
    that removes the sound has no music either: Mute wins over everything."""
    if not edit.music or (video and edit.mute):
        return None
    return str(Path(edit.music).resolve())      # ffmpeg may run in another folder


def video_command(ffmpeg: str, job: VideoJob, info: MediaInfo, transforms: Optional[str] = None,
                  encoder: Optional[str] = None, loudness: Optional[Loudness] = None) -> list[str]:
    """The export command. `loudness` is what the first loudness pass measured (see `measure_command`);
    with it, and `edit.normalize`, the sound is evened out."""
    edit = job.edit
    segments = edit.segments(info.duration, info.fps)
    if not segments:
        raise no_segments_error()
    rate = edit_rate(edit, info)
    music = music_path(edit)
    has_voice = info.has_audio and not edit.mute
    # The sound goes on to the final mix when there is music or a loudness to apply to it.
    mastered = (has_voice or bool(music)) and (bool(music) or (edit.normalize and loudness is not None))
    graph = clip_graph(0, edit, info, segments, fps=rate, transforms=transforms,
                       a_out="[voice]" if mastered else "[a]")
    filters = list(graph.filters)
    if music and graph.duration is None:
        raise music_length_error()
    if mastered:
        filters += master_filters(edit.sound, "[voice]" if graph.has_audio else None, 1 if music else None,
                                  graph.duration, "[a]", loudness)
    cmd = [ffmpeg, *_HEAD, *input_args(segments, job.source), *(music_args(music) if music else []),
           "-filter_complex", ";".join(filters), "-map", "[v]"]
    cmd += ["-map", "[a]", *(_AAC_PHONE if rate else _AAC)] if graph.has_audio or music else ["-an"]
    level = None
    if rate:
        geometry = plan_geometry(info.width, info.height, edit)
        level = h264_level(geometry.out_width, geometry.out_height, float(rate))
    cmd += [*video_codec_args(edit.crf, encoder, float(rate) if rate else info.fps, level),
            "-map_metadata", "0", *_TAIL, str(job.output.resolve())]
    return cmd


def wants_loudness_pass(edit: VideoEdit, info: MediaInfo) -> bool:
    """Whether exporting `info` with `edit` evens out the loudness, which takes a first pass.
    There is nothing to even out in a file without sound, and nothing at all with the sound removed."""
    return edit.normalize and ((info.has_audio and not edit.mute) or bool(music_path(edit)))


def measure_command(ffmpeg: str, job: VideoJob, info: MediaInfo) -> list[str]:
    """The first loudness pass: the sound of the export, music included, measured by loudnorm and
    thrown away. It prints what it measured as JSON, which `sound.parse_loudness` reads."""
    edit = job.edit
    segments = edit.segments(info.duration, info.fps)
    if not segments:
        raise no_segments_error()
    music = music_path(edit)
    graph = clip_graph(0, edit, info, segments, picture=False, a_out="[voice]")
    if music and graph.duration is None:
        raise music_length_error()
    filters = graph.filters + master_filters(edit.sound, "[voice]" if graph.has_audio else None,
                                             1 if music else None, graph.duration, "[a]", measure=True)
    return [ffmpeg, *_HEAD_INFO, *input_args(segments, job.source), *(music_args(music) if music else []),
            "-filter_complex", ";".join(filters), "-map", "[a]", "-f", "null", *_TAIL, "-"]


def detect_command(ffmpeg: str, source: Path, edit: VideoEdit, segments: list[Segment], transforms: str,
                   fps: Optional[float] = None) -> list[str]:
    """The stabilizer's first pass: measure the shake of the kept frames into `transforms`.
    `segments` and `fps` are the export's own, so both passes see the same frames."""
    shakiness = STABILIZE[edit.stabilize][0]
    graph = select_filters("[0:v:0]", "[t]", relative(segments), audio=False, margin=frame_margin(fps))
    graph.append(f"[t]vidstabdetect=shakiness={shakiness}:accuracy=15:result={transforms}[d]")
    return [ffmpeg, *_HEAD, *input_args(segments, source), "-filter_complex", ";".join(graph),
            "-map", "[d]", "-f", "null", *_TAIL, "-"]


DETECT_SHARE = 0.2      # part of the progress bar for the stabilizer's detect passes, which take about a fifth


def _encode(command: Callable[[Optional[str]], list[str]], encoder: Optional[str], output: Path,
            duration: Optional[float], on_progress: Optional[Callable[[float], None]],
            cancel_event: Optional[threading.Event], cwd: Optional[Path] = None,
            on_note: Optional[Callable[[str], None]] = None) -> Path:
    """Run the export command `command(encoder)`. When the hardware `encoder` fails, whatever the
    reason (a driver that balks, a size it can't do), run the same job again on the CPU, once."""
    if encoder is None:
        return run_ffmpeg(command(None), output, duration, on_progress, cancel_event, cwd)
    try:
        return run_ffmpeg(command(encoder), output, duration, on_progress, cancel_event, cwd)
    except ConversionError as exc:
        # run_ffmpeg has already removed the partial file.
        if cancel_event is not None and cancel_event.is_set():
            raise ConversionCancelled() from None
        log.warning("The graphics card encoder %s failed (%s); trying again on the CPU", encoder, exc)
        if on_note:
            on_note(CPU_FALLBACK_NOTE)
    return run_ffmpeg(command(None), output, duration, on_progress, cancel_event, cwd)


def _run_export(ffmpeg: str, clips: list[tuple[Path, VideoEdit, list[Segment], Optional[float]]],
                command: Callable[[dict[int, str], Optional[str], Optional[Loudness]], list[str]], output: Path,
                duration: Optional[float], on_progress: Optional[Callable[[float], None]],
                cancel_event: Optional[threading.Event], encoder: Optional[str] = None,
                on_note: Optional[Callable[[str], None]] = None, measure: Optional[list[str]] = None,
                measure_share: float = LOUDNESS_SHARE) -> Path:
    """Run an export, first running the stabilizer's detect pass for every clip that asks for it, and
    the loudness pass (the command `measure`) when the loudness is to be evened out.

    Each clip is (source, edit, kept segments, frame rate). `command(transforms, encoder, loudness)`
    builds the export command given {clip index: transforms file name}, the hardware encoder to use
    (None = the CPU) and what the loudness pass measured (None = nothing to apply). The transforms
    files live in a temporary folder that is ffmpeg's working folder. The passes come first and take
    their share of the progress bar; cancelling stops whichever one is running."""
    shaky = [i for i, (_, edit, _, _) in enumerate(clips) if STABILIZE.get(edit.stabilize)]
    if not shaky and measure is None:
        return _encode(lambda enc: command({}, enc, None), encoder, output, duration, on_progress, cancel_event,
                       on_note=on_note)
    with tempfile.TemporaryDirectory(prefix="fve-export-") as folder:
        folder = Path(folder)
        used = 0.0
        transforms: dict[int, str] = {}
        if shaky:
            lengths = {i: segments_length(clips[i][2]) or 0.0 for i in shaky}
            total = sum(lengths.values()) or 1.0
            done = 0.0
            for i in shaky:
                source, edit, segments, fps = clips[i]
                name = f"shake{i}.trf"

                def detect_progress(fraction: float, done: float = done, length: float = lengths[i]) -> None:
                    on_progress(DETECT_SHARE * min((done + fraction * length) / total, 1.0))

                run_ffmpeg(detect_command(ffmpeg, source, edit, segments, name, fps), folder / name,
                           lengths[i] or None, detect_progress if on_progress else None, cancel_event, cwd=folder)
                done += lengths[i]
                transforms[i] = name
            used = DETECT_SHARE
        loudness = None
        if measure is not None:
            loudness = measure_loudness(measure, folder / "loudness", duration, scaled(on_progress, used, measure_share),
                                        cancel_event, folder)
            used += measure_share
        return _encode(lambda enc: command(transforms, enc, loudness), encoder, output, duration,
                       scaled(on_progress, used, 1 - used), cancel_event, cwd=folder, on_note=on_note)


def convert_video(
    ffmpeg: str,
    job: VideoJob,
    on_progress: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    info: Optional[MediaInfo] = None,
    encoder: Optional[str] = None,
    on_note: Optional[Callable[[str], None]] = None,
) -> Path:
    """Export `job.source` as an edited MP4 at `job.output`.

    With a hardware `encoder` (see `gpu.choose_encoder`) the graphics card does the encoding; if that
    fails the export is run again on the CPU and `on_note` hears about it."""
    if not job.source.is_file():
        raise ConversionError(f"File not found: {job.source}")
    check_trim(job.edit.start, job.edit.end)
    info = info or probe_media(ffmpeg, job.source)
    if not info.has_video:
        raise ConversionError("This file has no video track.")
    check_music(ffmpeg, job.edit.sound, job.edit.mute)

    job = VideoJob(job.source.resolve(), job.output, job.edit)   # ffmpeg may run in another folder
    video_command(ffmpeg, job, info)                               # fail early on bad settings
    segments = job.edit.segments(info.duration, info.fps)
    duration = output_duration(segments, job.edit.speed)
    measure = measure_command(ffmpeg, job, info) if wants_loudness_pass(job.edit, info) else None
    return _run_export(ffmpeg, [(job.source, job.edit, segments, info.fps)],
                       lambda transforms, enc, loudness: video_command(ffmpeg, job, info, transforms.get(0), enc,
                                                                      loudness),
                       job.output, duration, on_progress, cancel_event, encoder, on_note, measure)


# ----- joining clips ------------------------------------------------------------

JOIN_AUDIO = STEREO_48K


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


def _join_sound(job: JoinJob) -> tuple[bool, Optional[str]]:
    """Whether any clip of a join has sound to play, and the background music laid over the whole result.
    The music is the first clip's, like the video quality; the first clip removing the sound removes it."""
    video = not job.audio_only
    any_audio = any(p.info.has_audio and not (video and p.edit.mute) for p in job.parts)
    return any_audio, music_path(job.parts[0].edit, video)


def join_command(ffmpeg: str, job: JoinJob, transforms: Optional[dict[int, str]] = None,
                 encoder: Optional[str] = None, loudness: Optional[Loudness] = None,
                 measure: bool = False) -> tuple[list[str], float]:
    """The ffmpeg command that plays every part back to back, and the result's length.

    Each clip is cut, sped up, reframed and faded with its own settings, then fitted into
    the first clip's frame size and frame rate. The video quality is the first clip's, and so is the
    steady frame rate if it has one: then the result is phone-safe at that rate (the fastest of the
    clips' own rates when none is above it), whatever rate the first clip has. Each clip keeps its own
    volume and noise reduction; the loudness and the background music are the first clip's, applied to
    the joined sound. With `measure` it is the first loudness pass instead (see `measure_command`)."""
    parts, video = job.parts, not job.audio_only
    picture = video and not measure
    first = parts[0]
    frame = fps = level = None
    steady = None
    if picture:
        geometry = plan_geometry(first.info.width, first.info.height, first.edit)
        frame = (geometry.out_width, geometry.out_height)
        rates = [p.info.fps * clamp_speed(p.edit.speed) for p in parts if p.info.fps]
        steady = steady_rate(first.edit.frame_rate, max(rates, default=None))
        fps = steady or first.info.fps or 30.0
        if steady:
            level = h264_level(*frame, float(steady))
    any_audio, music = _join_sound(job)
    if not video and not any_audio and not music:
        raise ConversionError("None of these files have sound.")
    sound = first.edit.sound
    mastered = (any_audio or bool(music)) and (bool(music) or (sound.normalize and loudness is not None))
    voice = "[voice]" if mastered else "[a]"

    inputs, filters, labels, total = [], [], [], 0.0
    for i, part in enumerate(parts):
        segments = part.edit.segments(part.info.duration, part.info.fps if picture else None)
        if not segments:
            raise ConversionError(f"Nothing is left of {part.source.name}: the trim and cuts remove all of it.")
        graph = clip_graph(i, part.edit, part.info, segments, video=video, audio=any_audio, frame=frame,
                           fps=fps, v_out=f"[v{i}]", a_out=f"[sound{i}]", transforms=(transforms or {}).get(i),
                           picture=picture)
        if graph.duration is None:
            raise ConversionError(f"Couldn't read the length of {part.source.name}.")
        inputs += input_args(segments, part.source)
        filters += graph.filters
        total += graph.duration
        label = f"[v{i}]" if picture else ""
        if any_audio:
            if graph.has_audio:
                filters.append(f"[sound{i}]{JOIN_AUDIO}[a{i}]")
            else:   # silence, so the sound stays in step with the pictures
                filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={graph.duration:.3f},{JOIN_AUDIO}[a{i}]")
            label += f"[a{i}]"
        labels.append(label)
    outputs = ("[v]" if picture else "") + (voice if any_audio else "")
    if outputs:
        filters.append(f"{''.join(labels)}concat=n={len(parts)}:v={int(picture)}:a={int(any_audio)}{outputs}")
    if mastered:
        filters += master_filters(sound, voice if any_audio else None, len(parts) if music else None, total, "[a]",
                                  loudness, measure)

    cmd = [ffmpeg, *(_HEAD_INFO if measure else _HEAD), *inputs, *(music_args(music) if music else []),
           "-filter_complex", ";".join(filters)]
    if measure:
        return [*cmd, "-map", "[a]", "-f", "null", *_TAIL, "-"], total
    if video:
        sounds = any_audio or bool(music)
        cmd += ["-map", "[v]", *(["-map", "[a]", *(_AAC_PHONE if steady else _AAC)] if sounds else ["-an"]),
                *video_codec_args(first.edit.crf, encoder, float(fps), level)]
    else:
        cmd += ["-map", "[a]", "-vn", "-c:a", "libmp3lame", *QUALITY_PRESETS[job.quality]]
    cmd += ["-map_metadata", "0", *_TAIL, str(job.output.resolve())]
    return cmd, total


def join_clips(
    ffmpeg: str,
    job: JoinJob,
    on_progress: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    encoder: Optional[str] = None,
    on_note: Optional[Callable[[str], None]] = None,
) -> Path:
    """Export several clips, in order, as one MP4 (or one MP3 with `job.audio_only`).
    `encoder` and `on_note` work as in `convert_video`."""
    if len(job.parts) < 2:
        raise ConversionError("Joining needs at least two clips.")
    for part in job.parts:
        if not part.source.is_file():
            raise ConversionError(f"File not found: {part.source}")
        check_trim(part.edit.start, part.edit.end)
        part.info = part.info or probe_media(ffmpeg, part.source)
        if not job.audio_only and not part.info.has_video:
            raise ConversionError(f"{part.source.name} has no video track.")
        part.source = part.source.resolve()     # ffmpeg may run in another folder
    first = job.parts[0].edit
    check_music(ffmpeg, first.sound, first.mute and not job.audio_only)
    _, duration = join_command(ffmpeg, job)     # fail early on bad settings
    any_audio, music = _join_sound(job)
    measure = join_command(ffmpeg, job, measure=True)[0] if first.normalize and (any_audio or music) else None
    clips = [] if job.audio_only else [(p.source, p.edit, p.edit.segments(p.info.duration, p.info.fps), p.info.fps)
                                       for p in job.parts]
    return _run_export(ffmpeg, clips,
                       lambda transforms, enc, loudness: join_command(ffmpeg, job, transforms, enc, loudness)[0],
                       job.output, duration, on_progress, cancel_event, None if job.audio_only else encoder, on_note,
                       measure, LOUDNESS_SHARE_AUDIO if job.audio_only else LOUDNESS_SHARE)


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

    The frame is reframed and adjusted like the export, then stamped with the time it will have
    in the export and run through the export's own fade filters, so fades look the same too.
    Stabilization is left out: it needs the whole clip."""
    graph = build_filter_graph(plan_geometry(info.width, info.height, edit), edit, out="[framed]")
    segments = edit.segments(info.duration)
    when = source_to_output(at, segments, edit.speed, edit.reverse)
    chain = picture_filters(edit)
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
