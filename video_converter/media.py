"""Timeline material: thumbnail strips and audio waveforms, read with ffmpeg."""

from __future__ import annotations

import logging
import subprocess
from array import array
from pathlib import Path
from typing import Optional

from .core import ConversionError, MediaInfo, parse_media_info
from .i18n import tr
from .procs import ProcessRegistry, run_tracked

log = logging.getLogger(__name__)

# How long reading a file may take before it is given up on (seconds). The fixed part covers starting
# ffmpeg and a slow drive; the rest grows with the length of the video.
PROBE_TIMEOUT = 30.0
WAVEFORM_BASE_TIMEOUT, WAVEFORM_PER_SECOND = 60.0, 0.2
THUMBNAIL_BASE_TIMEOUT, THUMBNAIL_PER_SECOND = 30.0, 0.05
STILL_TIMEOUT = 30.0


class ReadTimeout(ConversionError):
    """Reading a file's details took so long that it was given up on."""

    def __init__(self) -> None:
        super().__init__(tr("Reading this file took too long"))


def waveform_timeout(duration: Optional[float]) -> float:
    return WAVEFORM_BASE_TIMEOUT + WAVEFORM_PER_SECOND * (duration or 0.0)


def thumbnail_timeout(duration: Optional[float]) -> float:
    return THUMBNAIL_BASE_TIMEOUT + THUMBNAIL_PER_SECOND * (duration or 0.0)


def probe_file(ffmpeg: str, source: Path, registry: Optional[ProcessRegistry] = None,
               timeout: float = PROBE_TIMEOUT) -> MediaInfo:
    """The details of `source` (what `core.probe_media` reads), but the run can be stopped through `registry`
    and is given up on after `timeout` seconds, with `ReadTimeout`."""
    try:
        result = run_tracked([ffmpeg, "-hide_banner", "-i", str(source)], timeout, registry, text=True)
    except subprocess.TimeoutExpired:
        raise ReadTimeout() from None
    return parse_media_info(result.stderr or "")


def thumbnail_times(duration: float, count: int) -> list[float]:
    """Evenly spaced times for `count` thumbnails, each taken from the middle of its slot."""
    if duration <= 0 or count <= 0:
        return []
    slot = duration / count
    return [slot * (i + 0.5) for i in range(count)]


# Seeks handled by one ffmpeg run. Starting ffmpeg costs about a third of a second on Windows, so
# sharing a run saves most of the time; each input keeps a decoder in memory, so batches stay small.
THUMBNAILS_PER_RUN = 6


def extract_thumbnails(
    ffmpeg: str,
    source: Path,
    out_dir: Path,
    duration: float,
    count: int = 12,
    height: int = 72,
    registry: Optional[ProcessRegistry] = None,
    timeout: Optional[float] = None,
) -> list[Path]:
    """Save `count` JPEG thumbnails spread over the video; returns the files that worked.

    Every thumbnail is its own fast seek (an `-ss` input), so this stays quick on long videos,
    and a few of them share each ffmpeg run. A seek that finds no frame just leaves its file out, and
    a run that takes longer than `timeout` seconds (by default, a time that grows with the video) is
    stopped and the rest of the thumbnails are left out.
    """
    timeout = thumbnail_timeout(duration) if timeout is None else timeout
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(at, out_dir / f"thumb_{index:03d}.jpg") for index, at in enumerate(thumbnail_times(duration, count))]
    for start in range(0, len(jobs), THUMBNAILS_PER_RUN):
        batch = jobs[start:start + THUMBNAILS_PER_RUN]
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
        for at, _ in batch:
            # One decoding thread per input: several inputs already run side by side, and
            # frame threading would hold many full-size frames per input for no gain.
            cmd += ["-threads", "1", "-ss", f"{at:.3f}", "-i", str(source)]
        for n, (_, target) in enumerate(batch):
            target.unlink(missing_ok=True)      # what exists afterwards was written by this run
            cmd += ["-map", f"{n}:v:0", "-frames:v", "1",
                    "-vf", f"scale=-2:{height}", "-q:v", "4", "-update", "1", str(target)]
        try:
            # A failed seek makes ffmpeg exit with an error but the other files are still good.
            run_tracked(cmd, timeout, registry)
        except subprocess.TimeoutExpired:
            log.warning("Reading thumbnails of %s took longer than %.0f s", source.name, timeout)
            break
    return [target for _, target in jobs if target.is_file() and target.stat().st_size > 0]


def extract_still(ffmpeg: str, source: Path, target: Path, at: float = 0.0, long_side: int = 960,
                  registry: Optional[ProcessRegistry] = None, timeout: float = STILL_TIMEOUT) -> bool:
    """Save one upright frame of `source` as a PNG no more than `long_side` pixels on its longer side (a
    placeholder picture to show while a smooth preview is made). Returns whether the file was written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    scale = f"scale='if(gt(iw,ih),min({long_side},iw),-2)':'if(gt(iw,ih),-2,min({long_side},ih))'"
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{max(at, 0.0):.3f}",
           "-i", str(source), "-map", "0:v:0", "-frames:v", "1", "-vf", scale, "-update", "1", str(target)]
    try:
        run_tracked(cmd, timeout, registry)
    except subprocess.TimeoutExpired:
        return False
    return target.is_file() and target.stat().st_size > 0


def waveform_sample_rate(duration: Optional[float], buckets: int) -> int:
    """Decode just enough samples (about 200 per bucket) to draw a faithful outline."""
    if not duration or duration <= 0:
        return 8000
    return int(min(8000, max(100, buckets * 200 / duration)))


def peaks(samples: "array[int] | list[int]", buckets: int) -> list[float]:
    """Split 16-bit samples into `buckets` groups and return each group's peak level, 0..1."""
    if not samples or buckets <= 0:
        return []
    size = len(samples) / buckets
    levels = []
    for i in range(buckets):
        chunk = samples[int(i * size):max(int((i + 1) * size), int(i * size) + 1)]
        levels.append(min(max((abs(s) for s in chunk), default=0) / 32768, 1.0))
    return levels


def extract_waveform(ffmpeg: str, source: Path, duration: Optional[float], buckets: int = 600,
                     registry: Optional[ProcessRegistry] = None, timeout: Optional[float] = None) -> list[float]:
    """Peak levels (0..1) of the first audio track; an empty list if there's no audio. Raises
    `subprocess.TimeoutExpired` if reading it takes longer than `timeout` seconds (by default 60 s plus
    a fifth of the video's length)."""
    rate = waveform_sample_rate(duration, buckets)
    result = run_tracked(
        [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(source),
         "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-acodec", "pcm_s16le", "-"],
        waveform_timeout(duration) if timeout is None else timeout, registry,
    )
    if result.returncode != 0 or not result.stdout:
        return []
    data = result.stdout[: len(result.stdout) // 2 * 2]
    samples = array("h")
    samples.frombytes(data)
    return peaks(samples, buckets)
