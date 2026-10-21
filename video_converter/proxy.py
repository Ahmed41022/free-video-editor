"""Smooth previews: small, quick-to-play copies of big videos, made in the background.

Playing a 4K or very high bitrate video means decoding and converting every frame, which many PCs
can't do in real time. The editor plays a small copy instead. The copy is only for watching: crops,
the before/after picture, thumbnails and exports always use the original file."""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Callable, Hashable, Optional

from .core import ConversionCancelled, ConversionError, MediaInfo, remove_file, run_ffmpeg
from .procs import DaemonPool

log = logging.getLogger(__name__)

BIG_PIXELS = 1920 * 1080       # a video with more pixels than a 1080p one gets a preview copy...
BIG_KBPS = 20_000              # ...and so does one with more than about 20 megabits per second
HIGH_FPS = 35                  # a picture of at least HIGH_FPS_PIXELS that runs faster than this gets a copy too...
HIGH_FPS_PIXELS = 1280 * 720 - 1     # (about 720p: game recordings at 60 or 120 fps cost the player far too much)
PROXY_FPS = 30                 # ...which is made at no more than this many frames per second
PROXY_LONG_SIDE = 960          # the copy's longer side, in pixels
KEYFRAME_SECONDS = 1           # a keyframe this often, so jumping around the timeline is quick


def frame_rate_of(info: MediaInfo) -> float:
    """How fast the picture can run: its average rate, or for a variable-rate video (a game recording that
    averages 58 fps but declares 120) the highest rate it declares."""
    rate = info.fps or 0.0
    if info.variable_frame_rate and info.max_fps:
        rate = max(rate, info.max_fps)
    return rate


def needs_proxy(info: Optional[MediaInfo]) -> bool:
    """Whether a video is big enough, or fast enough, to be worth a preview copy."""
    if info is None or not info.has_video or not info.duration:
        return False
    pixels = info.width * info.height
    return (pixels > BIG_PIXELS or (info.bitrate or 0) > BIG_KBPS
            or (pixels > HIGH_FPS_PIXELS and frame_rate_of(info) > HIGH_FPS))


def proxy_size(width: int, height: int) -> tuple[int, int]:
    """The size of the preview copy of a `width` x `height` video: the same shape, never bigger than the
    original, with even sides (which H.264 needs)."""
    scale = min(PROXY_LONG_SIDE / max(width, height), 1.0)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def proxy_command(ffmpeg: str, source: Path, output: Path, info: MediaInfo) -> list[str]:
    """Make a small, fast-to-decode copy: ultrafast x264, a keyframe every second, and the sound as AAC.
    ffmpeg turns phone videos upright while reading them, so the copy has no rotation to apply."""
    width, height = proxy_size(info.width, info.height)
    # Cap the rate before shrinking, so the scaler only sees the frames that are kept.
    slow_down = f"fps={PROXY_FPS}," if frame_rate_of(info) > PROXY_FPS + 0.5 else ""
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", f"{slow_down}scale={width}:{height}:flags=fast_bilinear,setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-force_key_frames", f"expr:gte(t,n_forced*{KEYFRAME_SECONDS})",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output),
    ]


class ProxyMaker:
    """Makes preview copies one at a time on a background thread.

    `on_progress(key, fraction)` and `on_done(key, path, error)` are called from that thread
    (`path` is None and `error` says why when a copy couldn't be made). A copy that is cancelled
    reports nothing and leaves no file behind."""

    def __init__(self, ffmpeg: str, folder: Path,
                 on_progress: Callable[[Hashable, float], None], on_done: Callable[[Hashable, Optional[Path], str], None]):
        self.ffmpeg, self.folder = ffmpeg, folder
        self.on_progress, self.on_done = on_progress, on_done
        self._pool = DaemonPool(1, "preview-copy")      # daemon threads: a copy still running never holds up quitting
        self._lock = threading.Lock()
        self._jobs: dict[Hashable, tuple[Future, threading.Event]] = {}
        self._closed = False
        self._counter = 0

    def request(self, key: Hashable, source: Path, info: MediaInfo) -> bool:
        """Start making a copy for `key`, unless one is already on its way. Returns whether it was started."""
        with self._lock:
            if self._closed or key in self._jobs:
                return False
            self._counter += 1
            event = threading.Event()
            target = self.folder / f"preview-{self._counter}.mp4"
            self._jobs[key] = (self._pool.submit(self._make, key, source, info, target, event), event)
            return True

    def active(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._jobs

    def cancel(self, key: Hashable) -> None:
        """Stop making (or waiting to make) the copy for `key`."""
        with self._lock:
            entry = self._jobs.pop(key, None)
        if entry is not None:
            entry[1].set()
            entry[0].cancel()

    def cancel_waiting(self, keep: Optional[Hashable] = None) -> list[Hashable]:
        """Cancel the copies that haven't started yet, except `keep`'s. Returns whose they were."""
        with self._lock:
            waiting = [key for key, (future, _) in self._jobs.items() if key != keep and not future.running()
                       and not future.done()]
        for key in waiting:
            self.cancel(key)
        return waiting

    def cancel_all(self) -> None:
        with self._lock:
            keys = list(self._jobs)
        for key in keys:
            self.cancel(key)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop everything and wait (a little) for ffmpeg to let go of its files."""
        with self._lock:
            self._closed = True
        self.cancel_all()
        done = threading.Event()
        threading.Thread(target=lambda: (self._pool.shutdown(wait=True), done.set()), daemon=True).start()
        done.wait(timeout)

    # ----- worker -------------------------------------------------------------

    def _make(self, key: Hashable, source: Path, info: MediaInfo, target: Path, cancel: threading.Event) -> None:
        if cancel.is_set():
            return
        part = target.with_name(target.stem + ".part.mp4")

        def progress(fraction: float) -> None:
            if not cancel.is_set():
                self.on_progress(key, fraction)

        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            log.info("Making a smooth preview of %s (%sx%s)", source.name, info.width, info.height)
            run_ffmpeg(proxy_command(self.ffmpeg, source, part, info), part, info.duration, progress, cancel)
            if cancel.is_set():
                remove_file(part)
                return
            os.replace(part, target)
        except ConversionCancelled:
            remove_file(part)
            return
        except (ConversionError, OSError) as exc:
            remove_file(part)
            log.warning("Couldn't make a smooth preview of %s: %s", source.name, exc)
            self._finished(key, cancel)
            if not cancel.is_set():
                self.on_done(key, None, str(exc))
            return
        self._finished(key, cancel)
        if cancel.is_set():
            remove_file(target)
            return
        log.info("Smooth preview of %s is ready", source.name)
        self.on_done(key, target, "")

    def _finished(self, key: Hashable, cancel: threading.Event) -> None:
        with self._lock:
            entry = self._jobs.get(key)
            if entry is not None and entry[1] is cancel:
                del self._jobs[key]
