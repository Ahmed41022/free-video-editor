"""Smooth previews: small, quick-to-play copies of big videos, made in the background.

Playing a 4K or very high bitrate video means decoding and converting every frame, which many PCs
can't do in real time. The editor plays a small copy instead. The copy is only for watching: crops,
the before/after picture, thumbnails and exports always use the original file."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Hashable, Optional

from .core import (
    ConversionCancelled, ConversionError, MediaInfo, PausableEvent, remove_file, run_ffmpeg,
)

log = logging.getLogger(__name__)

BIG_PIXELS = 1920 * 1080  # a video with more pixels than a 1080p one gets a preview copy...
BIG_KBPS = 20_000  # ...and so does one with more than about 20 megabits per second
# a picture of at least HIGH_FPS_PIXELS that runs faster than this gets a copy too...
HIGH_FPS = 35
# (about 720p: game recordings at 60 or 120 fps cost the player far too much)
HIGH_FPS_PIXELS = 1280 * 720 - 1
PROXY_FPS = 30  # ...which is made at no more than this many frames per second
PROXY_LONG_SIDE = 960  # the copy's longer side, in pixels
KEYFRAME_SECONDS = 1  # a keyframe this often, so jumping around the timeline is quick
# A copy that is put on hold (its file was left for another, or is played as it is) is frozen where
# it is, not thrown away, so it carries on later instead of starting again. A frozen ffmpeg keeps
# its memory and its files open, so only a few are kept, and one nobody comes back to is dropped.
MAX_PAUSED = 3  # at most this many copies are kept on hold; the one on hold longest is dropped
PAUSED_KEEP_SECONDS = 300.0  # a copy on hold this long (5 minutes) is dropped


def frame_rate_of(info: MediaInfo) -> float:
    """How fast the picture can run: its average rate, or for a variable-rate video (a game
    recording that averages 58 fps but declares 120) the highest rate it declares."""
    rate = info.fps or 0.0
    if info.variable_frame_rate and info.max_fps:
        rate = max(rate, info.max_fps)
    return rate


def needs_proxy(info: Optional[MediaInfo]) -> bool:
    """Whether a video is big enough, or fast enough, to be worth a preview copy."""
    if info is None or not info.has_video or not info.duration:
        return False
    pixels = info.width * info.height
    return (
        pixels > BIG_PIXELS or (info.bitrate or 0) > BIG_KBPS
        or (pixels > HIGH_FPS_PIXELS and frame_rate_of(info) > HIGH_FPS))


def proxy_size(width: int, height: int) -> tuple[int, int]:
    """The size of the preview copy of a `width` x `height` video: the same shape, never bigger
    than the original, with even sides (which H.264 needs)."""
    scale = min(PROXY_LONG_SIDE / max(width, height), 1.0)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def proxy_command(ffmpeg: str, source: Path, output: Path, info: MediaInfo) -> list[str]:
    """Make a small, fast-to-decode copy: ultrafast x264, a keyframe every second, and the sound as
    AAC. ffmpeg turns phone videos upright while reading them, so the copy has no rotation to
    apply."""
    width, height = proxy_size(info.width, info.height)
    # Cap the rate before shrinking, so the scaler only sees the frames that are kept.
    slow_down = f"fps={PROXY_FPS}," if frame_rate_of(info) > PROXY_FPS + 0.5 else ""
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i",
        str(source), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-vf",
        f"{slow_down}scale={width}:{height}:flags=fast_bilinear,setsar=1", "-c:v", "libx264",
        "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p", "-force_key_frames",
        f"expr:gte(t,n_forced*{KEYFRAME_SECONDS})", "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
        str(output),
    ]


WAITING, RUNNING, PAUSED = "waiting", "running", "paused"


class _Job:
    """One copy: waiting for its turn (never started), running, or paused part-way."""

    def __init__(self, key: Hashable, source: Path, info: MediaInfo, target: Path):
        self.key, self.source, self.info, self.target = key, source, info, target
        self.cancel = PausableEvent()  # set to stop it; it also freezes and releases the run
        self.state = WAITING
        self.paused_at = 0.0
        self.timer: Optional[threading.Timer] = None  # drops it if it stays paused too long
        self.thread: Optional[threading.Thread] = None


class ProxyMaker:
    """Makes preview copies in the background, one at a time.

    `on_progress(key, fraction)` and `on_done(key, path, error)` are called from the worker thread
    (`path` is None and `error` says why when a copy couldn't be made). A copy that is cancelled
    reports nothing and leaves no file behind.

    Which copy gets the work: the one `focus` names, then the others in the order they were asked
    for, then the ones that were paused (the latest first). Moving the focus to a copy that is
    waiting or paused freezes the one that is running: its ffmpeg stays in memory and carries on
    from the same point when it gets its turn. `park` keeps a copy frozen whatever the focus is,
    and `focus(..., hold=True)` lets nothing but the focused copy run. At most MAX_PAUSED copies
    are kept paused and each is dropped after PAUSED_KEEP_SECONDS (`on_dropped(key)` says so)."""

    def __init__(
            self, ffmpeg: str, folder: Path, on_progress: Callable[[Hashable, float], None],
            on_done: Callable[[Hashable, Optional[Path], str],
                              None], on_dropped: Optional[Callable[[Hashable], None]] = None):
        self.ffmpeg, self.folder = ffmpeg, folder
        self.on_progress, self.on_done, self.on_dropped = on_progress, on_done, on_dropped
        self._lock = threading.RLock()
        self._jobs: dict[Hashable, _Job] = {}  # in the order they were asked for
        self._front: Optional[Hashable] = None
        self._hold = False
        self._parked: set[Hashable] = set()
        self._threads: list[threading.Thread] = []
        self._closed = False
        self._counter = 0

    def request(self, key: Hashable, source: Path, info: MediaInfo) -> bool:
        """Ask for a copy for `key`, unless one is already on its way (running or paused). Returns
        whether it was asked for."""
        with self._lock:
            if self._closed or key in self._jobs:
                return False
            self._counter += 1
            target = self.folder / f"preview-{self._counter}.mp4"
            self._jobs[key] = _Job(key, source, info, target)
        self._balance()
        return True

    def active(self, key: Hashable) -> bool:
        """Whether a copy for `key` is waiting, running or paused."""
        with self._lock:
            return key in self._jobs

    def state(self, key: Hashable) -> Optional[str]:
        """"waiting" (not started), "running" or "paused" for `key`'s copy; None when there is
        none."""
        with self._lock:
            job = self._jobs.get(key)
            return job.state if job is not None else None

    def paused(self, key: Hashable) -> bool:
        return self.state(key) == PAUSED

    def focus(self, key: Optional[Hashable], hold: bool = False) -> None:
        """Give `key`'s copy (if there is or will be one) the work first. With `hold`, nothing else
        runs: copies that are paused stay paused and waiting ones wait."""
        with self._lock:
            self._front, self._hold = key, hold
        self._balance()

    def park(self, key: Hashable, parked: bool = True) -> None:
        """Freeze `key`'s copy and keep it frozen, whatever else is going on, until it is parked
        no more."""
        with self._lock:
            (self._parked.add if parked else self._parked.discard)(key)
        self._balance()

    def cancel(self, key: Hashable) -> None:
        """Stop making (or waiting to make) the copy for `key`, running or paused."""
        self._end(key)
        self._balance()

    def cancel_waiting(self, keep: Optional[Hashable] = None) -> list[Hashable]:
        """Cancel the copies that haven't started yet, except `keep`'s. Returns whose they were."""
        with self._lock:
            waiting = [
                key for key, job in self._jobs.items() if key != keep and job.state == WAITING
            ]
        for key in waiting:
            self._end(key)
        self._balance()
        return waiting

    def cancel_all(self) -> None:
        with self._lock:
            keys = list(self._jobs)
        for key in keys:
            self._end(key)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop everything and wait (a little) for ffmpeg to let go of its files."""
        with self._lock:
            self._closed = True
            threads = list(self._threads)
        self.cancel_all()
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    # ----- who runs ----------------------------------------------------------

    def _end(self, key: Hashable) -> None:
        """Forget `key`'s copy and tell its ffmpeg (running or frozen) to stop."""
        with self._lock:
            job = self._jobs.pop(key, None)
            self._parked.discard(key)
        if job is not None:
            if job.timer is not None:
                job.timer.cancel()
            job.cancel.set()

    def _balance(self) -> None:
        """Decide which copy runs now, and freeze the one that shouldn't."""
        dropped: list[Hashable] = []
        with self._lock:
            if not self._closed:
                self._decide(dropped)
        self._tell_dropped(dropped)

    def _tell_dropped(self, keys: list[Hashable]) -> None:
        if self.on_dropped is not None:
            for key in keys:
                self.on_dropped(key)

    def _decide(self, dropped: list[Hashable]) -> None:
        front = self._jobs.get(self._front)
        if front is not None and front.key in self._parked:
            front = None
        running = next((job for job in self._jobs.values() if job.state == RUNNING), None)
        if running is not None:
            if (running.key not in self._parked and not (self._hold and running is not front)
                    and (front is None or front is running)):
                return  # it carries on
            self._pause(running, dropped)
        pick = front
        if pick is None and not self._hold:
            pick = self._background()
        if pick is not None and pick.key in self._jobs:  # (not dropped just now)
            self._run(pick)

    def _background(self) -> Optional[_Job]:
        """The copy to work on when none is in front: the oldest request, else the one paused
        most recently."""
        free = [job for job in self._jobs.values() if job.key not in self._parked]
        for job in free:
            if job.state == WAITING:
                return job
        paused = [job for job in free if job.state == PAUSED]
        return max(paused, key=lambda job: job.paused_at) if paused else None

    def _run(self, job: _Job) -> None:
        if job.timer is not None:
            job.timer.cancel()
            job.timer = None
        if job.state == PAUSED:
            job.state = RUNNING
            job.cancel.resume()
        elif job.state == WAITING:
            job.state = RUNNING
            # a daemon thread: a copy still running never holds up quitting
            job.thread = threading.Thread(
                target=self._make, args=(job.key, job.source, job.info, job.target, job.cancel),
                daemon=True, name="preview-copy")
            self._threads = [t for t in self._threads if t.is_alive()] + [job.thread]
            job.thread.start()

    def _pause(self, job: _Job, dropped: list[Hashable]) -> None:
        job.cancel.pause()
        job.state = PAUSED
        job.paused_at = time.monotonic()
        job.timer = threading.Timer(PAUSED_KEEP_SECONDS, self._expire, (job.key, job.cancel))
        job.timer.daemon = True
        job.timer.start()
        paused = [other for other in self._jobs.values() if other.state == PAUSED]
        while len(paused) > MAX_PAUSED:
            oldest = min(paused, key=lambda other: other.paused_at)
            paused.remove(oldest)
            del self._jobs[oldest.key]
            self._parked.discard(oldest.key)
            if oldest.timer is not None:
                oldest.timer.cancel()
            oldest.cancel.set()
            dropped.append(oldest.key)
            log.info("Dropped the oldest paused smooth preview: %s are kept", MAX_PAUSED)

    def _expire(self, key: Hashable, token: threading.Event) -> None:
        """A copy has been paused for too long: drop it."""
        with self._lock:
            job = self._jobs.get(key)
            if job is None or job.cancel is not token or job.state != PAUSED:
                return
            del self._jobs[key]
            self._parked.discard(key)
        token.set()
        log.info("Dropped a smooth preview that was paused for %s seconds", PAUSED_KEEP_SECONDS)
        self._tell_dropped([key])
        self._balance()

    # ----- worker -------------------------------------------------------------

    def _make(
            self, key: Hashable, source: Path, info: MediaInfo, target: Path,
            cancel: threading.Event) -> None:
        if cancel.is_set():
            return
        part = target.with_name(target.stem + ".part.mp4")

        def progress(fraction: float) -> None:
            if not cancel.is_set():
                self.on_progress(key, fraction)

        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            log.info("Making a smooth preview of %s (%sx%s)", source.name, info.width, info.height)
            run_ffmpeg(
                proxy_command(self.ffmpeg, source, part, info), part, info.duration, progress,
                cancel)
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
            job = self._jobs.get(key)
            if job is not None and job.cancel is cancel:
                del self._jobs[key]
                self._parked.discard(key)
        self._balance()  # the next copy gets its turn
