"""The file formats an export can make (MP4, H.265, WebM, MOV, GIF), and fitting a video into a file size.
Only arithmetic and options live here; the ffmpeg commands are built in video.py."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .core import ConversionError, format_time
from .i18n import N_, tr


@dataclass(frozen=True)
class OutputFormat:
    label: str            # shown in the panel
    short: str            # shown in the list and the queue
    extension: str
    codec: str            # the picture: "h264", "hevc", "vp9" or "gif"
    audio: str            # the sound: "aac", "opus", or "" for none
    hint: str = ""        # what to tell the user about choosing it


OUTPUT_FORMATS = {
    "mp4": OutputFormat(N_("MP4 (H.264, plays everywhere)"), "MP4", ".mp4", "h264", "aac"),
    "hevc": OutputFormat(N_("MP4 (H.265, smaller files)"), "H.265", ".mp4", "hevc", "aac",
                         N_("H.265 files are smaller, but older phones and programs can't play them.")),
    "webm": OutputFormat(N_("WebM (VP9, for the web)"), "WebM", ".webm", "vp9", "opus",
                         N_("WebM plays in browsers and most players. Exporting takes longer.")),
    "mov": OutputFormat(N_("MOV (H.264, for Apple and editors)"), "MOV", ".mov", "h264", "aac"),
    "gif": OutputFormat(N_("GIF (animated picture, no sound)"), "GIF", ".gif", "gif", "",
                        N_("A GIF has no sound and gets big quickly, so it suits short clips.")),
}
DEFAULT_VIDEO_FORMAT = "mp4"

# A GIF's frame rate and width are limits: a slower or narrower video keeps its own.
GIF_FPS = 15
GIF_WIDTH = 480
GIF_FPS_RANGE = (1, 30)
GIF_WIDTH_RANGE = (16, 1920)
GIF_FPS_CHOICES = (10, 15, 20, 25)
GIF_WIDTH_CHOICES = (240, 320, 480, 640, 800)
GIF_LONG_SECONDS = 15      # a GIF longer than this is worth a warning when its size is not known
# What a GIF weighs: about this many bytes for every pixel of every frame (measured: a clean test picture
# 0.1, busy or noisy footage 0.5 to 0.8, so camera footage is taken at 0.3), and the size chat apps start
# refusing (Discord's 10 MB), above which a GIF is worth a warning.
GIF_BYTES_PER_PIXEL = 0.3
GIF_WARN_BYTES = 10_000_000


def gif_size_estimate(width: int, height: int, fps: float, seconds: float) -> int:
    """About how many bytes a GIF of this size, frame rate and length comes out at."""
    return int(width * height * fps * seconds * GIF_BYTES_PER_PIXEL)


def gif_warning(seconds: Optional[float], width: Optional[int] = None, height: Optional[int] = None,
                fps: Optional[float] = None) -> str:
    """What to say about a GIF that runs `seconds`, or "" when it will not get big. With its `width`, `height`
    and `fps` the warning is about the size it is estimated to come out at; without them, about its length."""
    if seconds is None:
        return ""
    if width and height and fps:
        size = gif_size_estimate(width, height, fps, seconds)
        if size <= GIF_WARN_BYTES:
            return ""
        return tr("This GIF runs {length} and may come out at about {size:.0f} MB, and GIFs get big fast. "
                  "Shorten the clip, or lower the width or frame rate.", length=format_time(seconds),
                  size=size / 1e6)
    if seconds <= GIF_LONG_SECONDS:
        return ""
    return tr("This GIF runs {length}, and GIFs get big fast: it may come out at tens of megabytes. "
              "Shorten the clip, or lower the width or frame rate.", length=format_time(seconds))


def gif_rate(cap: int, source_fps: Optional[float]) -> float:
    """The frame rate of a GIF: the cap, or the picture's own rate when that is lower (frames are never repeated)."""
    return min(float(cap), source_fps) if source_fps else float(cap)


def gif_scale(width: int) -> str:
    """The filter that makes a GIF at most `width` pixels wide (never wider than its picture)."""
    return f"scale=w='min({width},iw)':h=-2:flags=lanczos"


# ----- fitting a file size --------------------------------------------------------

MB = 1_000_000                 # a megabyte, counted the way file sizes are advertised
TARGET_PRESETS = {"Discord (10 MB)": 10.0, "WhatsApp (16 MB)": 16.0, N_("Email (25 MB)"): 25.0}
DEFAULT_TARGET_MB = 10.0
TARGET_RANGE = (0.5, 5000.0)

# The container, and the rate control's own wobble, take a little more than the picture and sound add
# up to. The plan aims this far under the limit; a file that still comes out over is made again.
SAFETY = 0.025
TRIES = 4                      # how many times a file is made, at most, to land just under the limit
UNDERUSE = 0.93                # a file smaller than this share of the limit is made again, bigger, if that is likely to help

# The sound of a file that has to fit: smaller than the usual 192 kbps, but never more than a sixth of it.
AUDIO_FIT_KBPS = {"aac": 128, "opus": 96}
MIN_STEREO_KBPS = 48        # sound with less than this is mono
MIN_AUDIO_KBPS = 24         # the least a voice can do with, in mono, when the picture needs every bit
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
    96 (Opus), less when that would take more than a sixth of it, and not less than MIN_STEREO_KBPS (see
    `fit_plan` for going lower)."""
    if not audio:
        return 0
    return int(max(MIN_STEREO_KBPS, min(AUDIO_FIT_KBPS[audio], total_kbps / 6)))


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
        raise ConversionError(tr("Can't fit this into a file size: it has no length."))
    total = total_kbps(target, seconds)
    fps = fps or 30.0
    minimum = MIN_BITS_PER_PIXEL[codec]
    # The usual sound first. When the picture doesn't fit with it, the sound is lowered to the least a voice
    # can do with (a mono 24 kbps) before the fit is given up on.
    sounds = [audio_kbps(audio, total)]
    if audio and sounds[0] > MIN_AUDIO_KBPS:
        sounds.append(MIN_AUDIO_KBPS)
    for sound in sounds:
        kbps = int(total - sound)
        for index, (short, width, height) in enumerate(rungs):
            need = max(MIN_VIDEO_KBPS, minimum * width * height * fps / 1000)
            if kbps >= need:
                if index and short:
                    return SizeFit(target, kbps, sound, seconds,
                                   tr("Made smaller ({short}p) so it fits in {limit:g} MB", short=short,
                                      limit=target / MB), short)
                return SizeFit(target, kbps, sound, seconds)
    if audio and total <= MIN_AUDIO_KBPS:
        raise ConversionError(tr("Not even the sound fits in {limit:g} MB; choose a bigger size or remove the sound.",
                                 limit=target / MB))
    _, width, height = rungs[-1]
    need = max(MIN_VIDEO_KBPS, minimum * width * height * fps / 1000)
    fits = seconds * (total - sounds[-1]) / need if total > sounds[-1] else 0
    raise ConversionError(tr(
        "This video is too long to fit in {limit:g} MB. Squeezed that far, the picture would look "
        "terrible: about {length} of it fits. Shorten the video, or choose a bigger size.",
        limit=target / MB, length=format_time(fits)))


def fit_note(plan: SizeFit) -> str:
    """A sentence about a plan, for the panel: what the picture will get."""
    text = tr("The picture gets about {rate:.1f} Mbps", rate=plan.kbps / 1000) if plan.kbps >= 1000 else \
        tr("The picture gets about {rate} kbps", rate=plan.kbps)
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
