"""Conversion logic: locate ffmpeg, probe durations, and extract MP3 audio."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

VIDEO_EXTENSIONS = (
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".ts",
)

# Label shown in the UI -> ffmpeg audio arguments.
QUALITY_PRESETS = {
    "128 kbps": ["-b:a", "128k"],
    "192 kbps": ["-b:a", "192k"],
    "256 kbps": ["-b:a", "256k"],
    "320 kbps": ["-b:a", "320k"],
    "VBR (high quality)": ["-q:a", "0"],
    "VBR (standard)": ["-q:a", "4"],
}
DEFAULT_QUALITY = "192 kbps"

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class ConversionError(Exception):
    pass


class ConversionCancelled(Exception):
    pass


def find_ffmpeg() -> Optional[str]:
    """Return a path to an ffmpeg executable, or None if none can be found.

    Checks the FFMPEG_PATH environment variable, then the copy bundled into the
    .exe build, then PATH, then the optional imageio-ffmpeg package (which
    ships its own binary).
    """
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path and Path(env_path).is_file():
        return env_path

    bundle_dir = getattr(sys, "_MEIPASS", None)  # set by PyInstaller
    if bundle_dir:
        bundled = Path(bundle_dir) / "ffmpeg" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        if bundled.is_file():
            return str(bundled)

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def unique_output_path(path: Path) -> Path:
    """Return `path`, or `name (1).mp3`, `name (2).mp3`, ... if it already exists."""
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def output_path_for(source: Path, output_dir: Optional[Path] = None, overwrite: bool = False) -> Path:
    target = (output_dir or source.parent) / f"{source.stem}.mp3"
    return target if overwrite else unique_output_path(target)


def probe_duration(ffmpeg: str, source: Path) -> Optional[float]:
    """Return the media duration in seconds, or None if it can't be determined."""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    match = _DURATION_RE.search(result.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


@dataclass
class ConversionJob:
    source: Path
    output: Path
    quality: str = DEFAULT_QUALITY


def convert(
    ffmpeg: str,
    job: ConversionJob,
    on_progress: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Path:
    """Extract the first audio track of `job.source` into an MP3 at `job.output`.

    `on_progress` receives a fraction between 0 and 1 (only when the duration
    is known). Setting `cancel_event` stops ffmpeg and removes the partial file.
    """
    if not job.source.is_file():
        raise ConversionError(f"File not found: {job.source}")

    duration = probe_duration(ffmpeg, job.source)
    job.output.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(job.source),
        "-map", "0:a:0", "-vn", "-map_metadata", "0",
        "-c:a", "libmp3lame", *QUALITY_PRESETS[job.quality],
        "-progress", "pipe:1", "-nostats",
        str(job.output),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )

    # Drain stderr on a separate thread so a chatty ffmpeg can't block on a full pipe.
    errors: deque[str] = deque(maxlen=20)
    stderr_reader = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
    stderr_reader.start()

    cancelled = False
    for line in proc.stdout:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            proc.terminate()
            break
        key, _, value = line.strip().partition("=")
        # Despite its name, out_time_ms is in microseconds.
        if key == "out_time_ms" and duration and on_progress and value.isdigit():
            on_progress(min(int(value) / 1_000_000 / duration, 1.0))

    proc.wait()
    stderr_reader.join(timeout=2)
    proc.stdout.close()
    proc.stderr.close()

    if cancelled:
        job.output.unlink(missing_ok=True)
        raise ConversionCancelled()
    if proc.returncode != 0:
        job.output.unlink(missing_ok=True)
        message = "".join(errors).strip() or f"ffmpeg exited with code {proc.returncode}"
        if "matches no streams" in message:
            message = "This file has no audio track."
        raise ConversionError(message)

    if on_progress:
        on_progress(1.0)
    return job.output
