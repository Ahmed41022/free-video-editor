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
) -> list[Path]:
    """Save `count` JPEG thumbnails spread over the video; returns the files that worked.

    Every thumbnail is its own fast seek (an `-ss` input), so this stays quick on long videos,
    and a few of them share each ffmpeg run. A seek that finds no frame just leaves its file out.
    """
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
            subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW, timeout=120)
        except subprocess.TimeoutExpired:
            pass
    return [target for _, target in jobs if target.is_file() and target.stat().st_size > 0]


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
