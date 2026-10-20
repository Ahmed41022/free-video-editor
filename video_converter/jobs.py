"""A background export queue that runs one job at a time. Independent of any UI toolkit."""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .core import ConversionCancelled, ConversionError, ConversionJob, MediaInfo, convert
from .gpu import choose_encoder
from .i18n import N_, tr
from .project import OUTPUT_AUDIO, ClipSettings, describe, describe_join, output_file_for
from .video import DEFAULT_FPS, JoinJob, JoinPart, VideoEdit, VideoJob, convert_video, join_clips, reverse_memory

log = logging.getLogger(__name__)


class JobState(Enum):
    QUEUED = N_("Queued")
    RUNNING = N_("Exporting")
    DONE = N_("Done")
    FAILED = N_("Failed")
    CANCELLED = N_("Cancelled")

    @property
    def label(self) -> str:
        """The state's name in the user's language (the value is the English name)."""
        return tr(self.value)

    @property
    def finished(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


_ids = itertools.count(1)


@dataclass
class Clip:
    """One file in a joined export."""
    source: Path
    settings: ClipSettings
    info: Optional[MediaInfo] = None


@dataclass
class Job:
    source: Path
    settings: ClipSettings                 # a snapshot; later edits in the UI don't affect it
    output_dir: Optional[Path] = None      # None = next to the source file
    info: Optional[MediaInfo] = None
    id: int = field(default_factory=lambda: next(_ids))
    state: JobState = JobState.QUEUED
    progress: float = 0.0
    output: Optional[Path] = None
    error: str = ""
    note: str = ""                         # something worth telling the user about how the export went
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    # Set for a joined export: every clip in order, starting with `source`.
    clips: list[Clip] = field(default_factory=list)

    @property
    def title(self) -> str:
        more = len(self.clips) - 1
        return tr("{name} + {n} more", name=self.source.name, n=more) if more > 0 else self.source.name

    @property
    def summary(self) -> str:
        return describe_join(self.settings, len(self.clips)) if self.clips else describe(self.settings, self.info.duration if self.info else None)

    @property
    def reverse_memory(self) -> int:
        """About how many bytes ffmpeg needs to play this export backwards (0 if nothing is reversed).
        In a join each clip is reversed on its own, so this is the biggest clip's."""
        sound_only = self.settings.output == OUTPUT_AUDIO
        if self.clips:
            fps = (self.clips[0].info.fps if self.clips[0].info else None) or DEFAULT_FPS
            return max((reverse_memory(c.settings.edit, c.info, fps, sound_only) for c in self.clips if c.info),
                       default=0)
        return reverse_memory(self.settings.edit, self.info, sound_only=sound_only) if self.info else 0

    def remaining_seconds(self, now: Optional[float] = None) -> Optional[float]:
        """Estimated time left, once there's enough progress to guess."""
        if self.state is not JobState.RUNNING or self.started_at is None or self.progress < 0.02:
            return None
        elapsed = (now if now is not None else time.monotonic()) - self.started_at
        return max(elapsed * (1 - self.progress) / self.progress, 0.0)


Runner = Callable[[str, Job, Callable[[float], None]], Path]


def run_job(ffmpeg: str, job: Job, on_progress: Callable[[float], None]) -> Path:
    """Export one job with ffmpeg. The output name is chosen now, so queued jobs for
    the same file never collide. Video goes through the graphics card when the job's settings
    allow it and there is one that works; if it fails, the job is done again on the CPU."""
    def note(text: str) -> None:
        # A job can have more to say than one thing (made smaller to fit, and then the card failed).
        job.note = f"{job.note} · {text}" if job.note and text not in job.note else (job.note or text)

    if job.clips:
        output = output_file_for(job.source, job.settings, job.output_dir, joined=True)
        parts = [JoinPart(clip.source, clip.settings.edit, clip.info) for clip in job.clips]
        join = JoinJob(parts, output, job.settings.output == OUTPUT_AUDIO, job.settings.audio_quality)
        encoder = None if join.audio_only else choose_encoder(ffmpeg, job.settings.edit.use_gpu, hardware_codec(
            job.settings.edit))
        return join_clips(ffmpeg, join, on_progress, job.cancel_event, encoder, note)
    output = output_file_for(job.source, job.settings, job.output_dir)
    edit = job.settings.edit
    if job.settings.output == OUTPUT_AUDIO:
        audio = ConversionJob(job.source, output, job.settings.audio_quality, edit.start, edit.end, edit.keep,
                              edit.speed, edit.reverse, edit.audio_fade_in, edit.audio_fade_out, edit.sound)
        return convert(ffmpeg, audio, on_progress, job.cancel_event)
    return convert_video(ffmpeg, VideoJob(job.source, output, edit), on_progress, job.cancel_event, job.info,
                         choose_encoder(ffmpeg, edit.use_gpu, hardware_codec(edit)), note)


def hardware_codec(edit: VideoEdit) -> str:
    """Which graphics card encoders suit `edit`'s format: "h264" or "hevc" (WebM and GIF have none)."""
    return edit.output_format.codec


def _describe_for_log(job: Job) -> str:
    return f"{job.title} ({job.summary}); graphics card: {'allowed' if job.settings.edit.use_gpu else 'off'}"


class JobQueue:
    """Runs jobs in order on one worker thread.

    `on_update(job)` is called from the worker thread whenever a job changes
    (UI code should hand it over to its own thread).
    """

    def __init__(self, ffmpeg: str, on_update: Optional[Callable[[Job], None]] = None,
                 runner: Runner = run_job):
        self.ffmpeg = ffmpeg
        self.on_update = on_update or (lambda job: None)
        self.runner = runner
        self.jobs: list[Job] = []
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._worker: Optional[threading.Thread] = None
        self._worker_running = False   # guarded by _lock; avoids racing a worker that is exiting
        self._closed = False

    def _notify(self, job: Job) -> None:
        # A broken listener must never stop the queue.
        try:
            self.on_update(job)
        except Exception:
            pass

    # ----- public API ---------------------------------------------------------

    def add(self, job: Job) -> Job:
        with self._lock:
            if self._closed:
                raise RuntimeError("The queue has been shut down.")
            self.jobs.append(job)
            if not self._worker_running:
                self._worker_running = True
                self._worker = threading.Thread(target=self._work, name="export-queue", daemon=True)
                self._worker.start()
            self._wake.notify_all()
        self._notify(job)
        return job

    def cancel(self, job_id: int) -> None:
        with self._lock:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if job is None or job.state.finished:
                return
            job.cancel_event.set()
            notify = job.state is JobState.QUEUED
            if notify:
                job.state = JobState.CANCELLED
                job.finished_at = time.monotonic()
                self._wake.notify_all()
        if notify:
            self._notify(job)

    def cancel_all(self) -> None:
        for job in list(self.jobs):
            self.cancel(job.id)

    def clear_finished(self) -> list[Job]:
        with self._lock:
            removed = [j for j in self.jobs if j.state.finished]
            self.jobs = [j for j in self.jobs if not j.state.finished]
        return removed

    @property
    def busy(self) -> bool:
        with self._lock:
            return any(not j.state.finished for j in self.jobs)

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until every job has finished. Returns False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while any(not j.state.finished for j in self.jobs):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._wake.wait(remaining if remaining is not None else 0.5)
        return True

    def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel everything and stop the worker (used when the app closes)."""
        self.cancel_all()
        with self._lock:
            self._closed = True
            self._wake.notify_all()
            worker = self._worker
        if worker is not None:
            worker.join(timeout)

    # ----- worker -------------------------------------------------------------

    def _next_job(self) -> Optional[Job]:
        with self._lock:
            job = next((j for j in self.jobs if j.state is JobState.QUEUED), None)
            if job is None or self._closed:
                # Idle: let the thread end; add() starts a new one when needed.
                self._worker_running = False
                self._wake.notify_all()
                return None
            job.state = JobState.RUNNING
            job.started_at = time.monotonic()
            return job

    @staticmethod
    def _log_finish(job: Job, state: JobState) -> None:
        seconds = time.monotonic() - job.started_at if job.started_at is not None else 0.0
        if state is JobState.DONE:
            log.info("Export %d finished in %.1f s: %s%s", job.id, seconds, job.output,
                     f" ({job.note})" if job.note else "")
        elif state is JobState.FAILED:
            log.error("Export %d failed after %.1f s: %s", job.id, seconds, job.error)
        else:
            log.info("Export %d cancelled after %.1f s", job.id, seconds)

    def _work(self) -> None:
        while True:
            job = self._next_job()
            if job is None:
                return
            log.info("Export %d started: %s", job.id, _describe_for_log(job))
            self._notify(job)

            def progress(fraction: float, job: Job = job) -> None:
                job.progress = fraction
                self._notify(job)

            try:
                job.output = self.runner(self.ffmpeg, job, progress)
                state = JobState.DONE
            except ConversionCancelled:
                state = JobState.CANCELLED
            except (ConversionError, OSError) as exc:
                job.error = str(exc)
                state = JobState.FAILED
            except Exception as exc:  # never let one bad job kill the queue
                log.exception("Export %d crashed", job.id)
                job.error = tr("Unexpected error: {error}", error=exc)
                state = JobState.FAILED
            self._log_finish(job, state)
            with self._lock:
                job.state = state
                job.finished_at = time.monotonic()
                if state is JobState.DONE:
                    job.progress = 1.0
                self._wake.notify_all()
            self._notify(job)
