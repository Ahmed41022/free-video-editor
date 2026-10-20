"""The file formats an export can make (MP4, H.265, WebM, MOV, GIF), and fitting a video into a file size.
Only arithmetic and options live here; the ffmpeg commands are built in video.py."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .core import ConversionError, format_time


@dataclass(frozen=True)
class OutputFormat:
    label: str            # shown in the panel
    short: str            # shown in the list and the queue
    extension: str
    codec: str            # the picture: "h264", "hevc", "vp9" or "gif"
    audio: str            # the sound: "aac", "opus", or "" for none
    hint: str = ""        # what to tell the user about choosing it


OUTPUT_FORMATS = {
    "mp4": OutputFormat("MP4 (H.264, plays everywhere)", "MP4", ".mp4", "h264", "aac"),
    "hevc": OutputFormat("MP4 (H.265, smaller files)", "H.265", ".mp4", "hevc", "aac",
                         "H.265 files are smaller, but older phones and programs can't play them."),
    "webm": OutputFormat("WebM (VP9, for the web)", "WebM", ".webm", "vp9", "opus",
                         "WebM plays in browsers and most players. Exporting takes longer."),
    "mov": OutputFormat("MOV (H.264, for Apple and editors)", "MOV", ".mov", "h264", "aac"),
    "gif": OutputFormat("GIF (animated picture, no sound)", "GIF", ".gif", "gif", "",
                        "A GIF has no sound and gets big quickly, so it suits short clips."),
}
DEFAULT_VIDEO_FORMAT = "mp4"

# A GIF's frame rate and width are limits: a slower or narrower video keeps its own.
GIF_FPS = 15
GIF_WIDTH = 480
GIF_FPS_RANGE = (1, 30)
GIF_WIDTH_RANGE = (16, 1920)
GIF_FPS_CHOICES = (10, 15, 20, 25)
GIF_WIDTH_CHOICES = (240, 320, 480, 640, 800)
GIF_LONG_SECONDS = 15      # a GIF longer than this is worth a warning


def gif_warning(seconds: Optional[float]) -> str:
    """What to say about a GIF that runs `seconds`, or "" when it is short enough not to need it."""
    if seconds is None or seconds <= GIF_LONG_SECONDS:
        return ""
    return (f"This GIF runs {format_time(seconds)}, and GIFs get big fast: it may come out at tens of megabytes. "
            "Shorten the clip, or lower the width or frame rate.")


def gif_rate(cap: int, source_fps: Optional[float]) -> float:
    """The frame rate of a GIF: the cap, or the picture's own rate when that is lower (frames are never repeated)."""
    return min(float(cap), source_fps) if source_fps else float(cap)


def gif_scale(width: int) -> str:
    """The filter that makes a GIF at most `width` pixels wide (never wider than its picture)."""
    return f"scale=w='min({width},iw)':h=-2:flags=lanczos"


# ----- fitting a file size --------------------------------------------------------

MB = 1_000_000                 # a megabyte, counted the way file sizes are advertised
TARGET_PRESETS = {"Discord (10 MB)": 10.0, "WhatsApp (16 MB)": 16.0, "Email (25 MB)": 25.0}
DEFAULT_TARGET_MB = 10.0
TARGET_RANGE = (0.5, 5000.0)

# The container, and the rate control's own wobble, take a little more than the picture and sound add
# up to. The plan aims this far under the limit; a file that still comes out over is made again.
SAFETY = 0.025
TRIES = 4                      # how many times a file is made, at most, to land just under the limit
UNDERUSE = 0.93                # a file smaller than this share of the limit is made again, bigger, if that is likely to help

# The sound of a file that has to fit: smaller than the usual 192 kbps, but never more than a sixth of it.
AUDIO_FIT_KBPS = {"aac": 128, "opus": 96}
MIN_AUDIO_KBPS = 48
# Fewest bits per pixel and frame that still look like a picture, and the least bitrate at all.
MIN_BITS_PER_PIXEL = {"h264": 0.04, "hevc": 0.028, "vp9": 0.028}
MIN_VIDEO_KBPS = 80
# Short sides to try, biggest first, when the picture has to be made smaller to fit.
LADDER = (1080, 720, 480, 360)


@dataclass
class SizeFit:
    """What an export that has to fit a file size encodes with."""
    target_bytes: int       # the file must not be bigger than this
    kbps: int               # the picture's bitrate to try; lowered when a try comes out too big
    audio_kbps: int         # the sound's bitrate, or 0 for none
    seconds: float          # the length of the result
    note: str = ""          # what was done to make it fit (the picture was made smaller), or ""
    short: Optional[int] = None     # the short side the picture was lowered to, if it was


def target_bytes(target_mb: float) -> int:
    return int(target_mb * MB)


def audio_kbps(audio: str, total_kbps: float) -> int:
    """The sound's bitrate when everything has `total_kbps`: 0 for no sound, else the usual 128 (AAC) or
    96 (Opus), less when that would take more than a sixth of it."""
    if not audio:
        return 0
    return int(max(MIN_AUDIO_KBPS, min(AUDIO_FIT_KBPS[audio], total_kbps / 6)))


def total_kbps(target: int, seconds: float) -> float:
    """The bitrate of everything in a file of `target` bytes that runs `seconds`, aiming a little under."""
    return target * (1 - SAFETY) * 8 / seconds / 1000


def fit_plan(target: int, seconds: float, codec: str, audio: str, fps: float,
             rungs: list[tuple[Optional[int], int, int]]) -> SizeFit:
    """How to fit a video of `seconds` into `target` bytes.

    `rungs` are the sizes to try, biggest first, as (short side or None for the picture's own size,
    width, height). The first one that leaves the picture enough bits per pixel wins, and its note
    says so when it isn't the first. Raises ConversionError when not even the smallest does."""
    if seconds <= 0:
        raise ConversionError("Can't fit this into a file size: it has no length.")
    total = total_kbps(target, seconds)
    sound = audio_kbps(audio, total)
    kbps = int(total - sound)
    fps = fps or 30.0
    minimum = MIN_BITS_PER_PIXEL[codec]
    for index, (short, width, height) in enumerate(rungs):
        need = max(MIN_VIDEO_KBPS, minimum * width * height * fps / 1000)
        if kbps >= need:
            if index and short:
                return SizeFit(target, kbps, sound, seconds, f"Made smaller ({short}p) so it fits in {target / MB:g} MB",
                               short)
            return SizeFit(target, kbps, sound, seconds)
    _, width, height = rungs[-1]
    need = max(MIN_VIDEO_KBPS, minimum * width * height * fps / 1000)
    fits = seconds * (total - sound) / need if total > sound else 0
    raise ConversionError(
        f"This video is too long to fit in {target / MB:g} MB. Squeezed that far, the picture would look "
        f"terrible: about {format_time(fits)} of it fits. Shorten the video, or choose a bigger size.")


def fit_note(plan: SizeFit) -> str:
    """A sentence about a plan, for the panel: what the picture will get."""
    text = f"The picture gets about {plan.kbps / 1000:.1f} Mbps" if plan.kbps >= 1000 else \
        f"The picture gets about {plan.kbps} kbps"
    return f"{text}. {plan.note}." if plan.note else f"{text}."


def next_kbps(fit: SizeFit, tries: list[tuple[int, int]]) -> Optional[int]:
    """The picture's bitrate for another try, given the (kbps, bytes) of the tries so far; None when no
    other try is worth making.

    A file is good when it is at most the limit and not under UNDERUSE of it. The next bitrate is the last
    one scaled by how far its file was from the middle of the margin under the limit. Bits and bytes don't
    follow each other one for one when the picture is very hard (a rate control that has run out of quality
    to give away turns 2 times the bits into 1.4 times the bytes), so with two tries the scaling uses the
    power they showed. A simple picture that comes out under half the limit is left alone: it wouldn't
    use more bits."""
    tried, size = tries[-1]
    if size < fit.target_bytes * 0.5 and all(other <= fit.target_bytes for _, other in tries):
        return None
    power = 1.0
    if len(tries) > 1:
        (before_kbps, before), (kbps, now) = tries[-2], tries[-1]
        if before_kbps != kbps and before != now:
            power = min(max(math.log(now / before) / math.log(kbps / before_kbps), 0.3), 1.5)
    aim = fit.target_bytes * (1 - SAFETY / 2)
    kbps = int(min(max(tried * (aim / size) ** (1 / power), tried * 0.3), tried * 2.5))
    return None if kbps < MIN_VIDEO_KBPS or kbps == tried else kbps
