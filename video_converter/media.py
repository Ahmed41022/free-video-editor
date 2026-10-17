"""Timeline material: thumbnail strips and audio waveforms, read with ffmpeg."""

from __future__ import annotations

import subprocess
from array import array
from pathlib import Path
from typing import Optional

from .core import _NO_WINDOW


def thumbnail_times(duration: float, count: int) -> list[float]:
    """Evenly spaced times for `count` thumbnails, each taken from the middle of its slot."""
    if duration <= 0 or count <= 0:
        return []
    slot = duration / count
    return [slot * (i + 0.5) for i in range(count)]


def extract_thumbnails(
    ffmpeg: str,
    source: Path,
    out_dir: Path,
    duration: float,
    count: int = 12,
    height: int = 72,
) -> list[Path]:
    """Save `count` JPEG thumbnails spread over the video; returns the files that worked.

    Each thumbnail is a separate fast seek, so this stays quick on long videos.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, at in enumerate(thumbnail_times(duration, count)):
        target = out_dir / f"thumb_{index:03d}.jpg"
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
             "-ss", f"{at:.3f}", "-i", str(source), "-frames:v", "1",
             "-vf", f"scale=-2:{height}", "-q:v", "4", "-update", "1", str(target)],
            capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=60,
        )
        if result.returncode == 0 and target.is_file():
            paths.append(target)
    return paths


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


def extract_waveform(ffmpeg: str, source: Path, duration: Optional[float], buckets: int = 600) -> list[float]:
    """Peak levels (0..1) of the first audio track; an empty list if there's no audio."""
    rate = waveform_sample_rate(duration, buckets)
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(source),
         "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-acodec", "pcm_s16le", "-"],
        capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=300,
    )
    if result.returncode != 0 or not result.stdout:
        return []
    data = result.stdout[: len(result.stdout) // 2 * 2]
    samples = array("h")
    samples.frombytes(data)
    return peaks(samples, buckets)
