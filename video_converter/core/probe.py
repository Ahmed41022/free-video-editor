"""What ffmpeg says about a file: duration, size, frame rate, sound, rotation."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .winproc import _NO_WINDOW

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Video: .*?(\d{2,5})x(\d{2,5})")
_COVER_STREAM_RE = re.compile(r"Stream #\d+:(\d+)[^\n]*?: Video: [^\n]*\(attached pic\)")
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+)\s*kb/s")
_ROTATION_RE = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?) fps")
_TBR_RE = re.compile(r"(\d+(?:\.\d+)?)(k?) tbr")
# A declared rate this many times the average means the frame rate changes; see
# MediaInfo.variable_frame_rate.
VARIABLE_RATIO = 1.25
STANDARD_RATES = (24000 / 1001, 24.0, 25.0, 30000 / 1001, 30.0, 50.0, 60000 / 1001, 60.0)


@dataclass
class MediaInfo:
    duration: Optional[float]
    width: Optional[int] = None  # as displayed, i.e. after any rotation metadata
    height: Optional[int] = None
    has_audio: bool = False
    # Clockwise turn (0, 90, 180, 270) that rotation metadata asks players to apply to
    # the stored frames. ffmpeg applies it automatically; a raw decoder may not.
    display_rotation: int = 0
    fps: Optional[float] = None  # frames per second of the first video stream (the average)
    bitrate: Optional[int] = None  # whole file, in kilobits per second, when ffmpeg says
    max_fps: Optional[
        float] = None  # the highest rate the stream declares ("tbr"), when ffmpeg says
    cover_stream: Optional[
        int] = None  # index of the cover picture of a music file (an "attached pic"), if any

    @property
    def has_video(self) -> bool:
        return bool(self.width and self.height)

    @property
    def variable_frame_rate(self) -> bool:
        """Whether the picture's frame rate changes: the average is well below the highest rate the
        stream declares, like the 57.97 fps average of a game recording that declares 120.

        Constant videos sometimes declare exactly twice their rate (25 fps, 50 tbr: the fields of
        an interlaced picture), so a standard average rate is never counted as variable."""
        if not self.fps or not self.max_fps or self.max_fps < self.fps * VARIABLE_RATIO:
            return False
        return not any(abs(self.fps - rate) <= 0.02 for rate in STANDARD_RATES)


def _ffmpeg_info_text(ffmpeg: str, source: Path) -> str:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(source)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
    )
    return result.stderr


def parse_media_info(text: str) -> MediaInfo:
    """Read duration, display size and audio presence from `ffmpeg -i` output."""
    duration = None
    match = _DURATION_RE.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    info = MediaInfo(duration, has_audio=bool(re.search(r"Stream #\d+:\d+.*?: Audio:", text)))
    rate = _BITRATE_RE.search(text)
    if rate:
        info.bitrate = int(rate.group(1))
    # Cover art in a music file is a "video" stream too, but not one to edit.
    cover = _COVER_STREAM_RE.search(text)
    if cover:
        info.cover_stream = int(cover.group(1))
    video = next(
        (
            m for m in _VIDEO_STREAM_RE.finditer(text)
            if "(attached pic)" not in text[m.start():].split("\n", 1)[0]), None)
    if video:
        width, height = int(video.group(1)), int(video.group(2))
        # ffmpeg applies rotation metadata automatically, so a phone video stored
        # as 1920x1080 "rotated 90" is really 1080x1920.
        # Only look at this stream's own lines; later streams (e.g. cover art) have their own.
        section = text[video.start():]
        next_stream = re.search(r"\n\s*Stream #", section[1:])
        if next_stream:
            section = section[:next_stream.start() + 1]
        rotation = _ROTATION_RE.search(section)
        if rotation:
            # ffmpeg reports the display matrix angle counter-clockwise.
            info.display_rotation = (-round(float(rotation.group(1)) / 90) * 90) % 360
        if info.display_rotation in (90, 270):
            width, height = height, width
        info.width, info.height = width, height
        line = text[video.start():].split("\n", 1)[0]
        rate = _FPS_RE.search(line)
        if rate:
            info.fps = float(rate.group(1))
        tbr = _TBR_RE.search(line)
        if tbr:
            info.max_fps = float(tbr.group(1)) * (1000 if tbr.group(2) else 1)
            if info.fps is None:
                info.fps = info.max_fps
        if info.fps is not None and not 0 < info.fps <= 1000:
            info.fps = None
        if info.max_fps is not None and not 0 < info.max_fps <= 1000:
            info.max_fps = None
    return info


def probe_media(ffmpeg: str, source: Path) -> MediaInfo:
    return parse_media_info(_ffmpeg_info_text(ffmpeg, source))


def probe_duration(ffmpeg: str, source: Path) -> Optional[float]:
    """Return the media duration in seconds, or None if it can't be determined."""
    return probe_media(ffmpeg, source).duration
