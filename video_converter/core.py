"""Shared ffmpeg plumbing: locate ffmpeg, probe files, run conversions, extract MP3 audio."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .i18n import N_, tr
from .sound import Loudness, Sound, master_filters, music_args, parse_loudness, voice_filters
from .timing import Range, Segment, kept_segments, output_duration

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = (
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".ts",
)
# Sound-only files the editor can also open (to trim or cut them, or save them as MP3).
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus")

# Label shown in the UI -> ffmpeg audio arguments.
QUALITY_PRESETS = {
    "128 kbps": ["-b:a", "128k"],
    "192 kbps": ["-b:a", "192k"],
    "256 kbps": ["-b:a", "256k"],
    "320 kbps": ["-b:a", "320k"],
    N_("VBR (high quality)"): ["-q:a", "0"],
    N_("VBR (standard)"): ["-q:a", "4"],
}
DEFAULT_QUALITY = "192 kbps"

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Video: .*?(\d{2,5})x(\d{2,5})")
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+)\s*kb/s")
_ROTATION_RE = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?) fps")
_TBR_RE = re.compile(r"(\d+(?:\.\d+)?)(k?) tbr")
# A declared rate this many times the average means the frame rate changes; see MediaInfo.variable_frame_rate.
VARIABLE_RATIO = 1.25
STANDARD_RATES = (24000 / 1001, 24.0, 25.0, 30000 / 1001, 30.0, 50.0, 60000 / 1001, 60.0)
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


def is_media_file(path: Path) -> bool:
    """A video or an audio file, by its extension."""
    return path.suffix.lower() in VIDEO_EXTENSIONS + AUDIO_EXTENSIONS


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


def output_path_for(
    source: Path,
    output_dir: Optional[Path] = None,
    overwrite: bool = False,
    suffix: str = ".mp3",
    name_tag: str = "",
) -> Path:
    """Where to save the result for `source`. `name_tag` is appended to the file name,
    e.g. " (edited)", so an exported MP4 never replaces the original video."""
    target = (output_dir or source.parent) / f"{source.stem}{name_tag}{suffix}"
    return target if overwrite else unique_output_path(target)


def parse_time(text: str) -> Optional[float]:
    """Parse "90", "1:30", "01:02:03.5" into seconds. Empty text means no value."""
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(tr("Invalid time: {text}", text=repr(text)))
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    except ValueError:
        raise ValueError(tr("Invalid time: {text} (use seconds, m:ss or h:mm:ss)", text=repr(text))) from None
    if seconds < 0:
        raise ValueError(tr("Invalid time: {text}", text=repr(text)))
    return seconds


def format_time(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h >= 1 else f"{int(m)}:{s:04.1f}"


@dataclass
class MediaInfo:
    duration: Optional[float]
    width: Optional[int] = None    # as displayed, i.e. after any rotation metadata
    height: Optional[int] = None
    has_audio: bool = False
    # Clockwise turn (0, 90, 180, 270) that rotation metadata asks players to apply to
    # the stored frames. ffmpeg applies it automatically; a raw decoder may not.
    display_rotation: int = 0
    fps: Optional[float] = None    # frames per second of the first video stream (the average)
    bitrate: Optional[int] = None  # whole file, in kilobits per second, when ffmpeg says
    max_fps: Optional[float] = None    # the highest rate the stream declares ("tbr"), when ffmpeg says

    @property
    def has_video(self) -> bool:
        return bool(self.width and self.height)

    @property
    def variable_frame_rate(self) -> bool:
        """Whether the picture's frame rate changes: the average is well below the highest rate the
        stream declares, like the 57.97 fps average of a game recording that declares 120.

        Constant videos sometimes declare exactly twice their rate (25 fps, 50 tbr: the fields of an
        interlaced picture), so a standard average rate is never counted as variable."""
        if not self.fps or not self.max_fps or self.max_fps < self.fps * VARIABLE_RATIO:
            return False
        return not any(abs(self.fps - rate) <= 0.02 for rate in STANDARD_RATES)


def _ffmpeg_info_text(ffmpeg: str, source: Path) -> str:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
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
    video = next((m for m in _VIDEO_STREAM_RE.finditer(text)
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


def trim_args(start: Optional[float], end: Optional[float]) -> list[str]:
    """Input options that keep only start..end of the source."""
    args = []
    if start:
        args += ["-ss", f"{start:.3f}"]
    if end is not None:
        args += ["-to", f"{end:.3f}"]
    return args


def stop_process(proc: subprocess.Popen) -> None:
    """Stop `proc` and anything it started. An ffmpeg found through a launcher (a Chocolatey or
    Scoop shim, say) is only the launcher: ending it alone leaves the real ffmpeg running with its
    files open, so on Windows the whole tree is ended."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    if proc.poll() is None:
        proc.terminate()


def remove_file(path: Path, seconds: float = 2.0) -> None:
    """Delete `path` if it exists. Windows refuses while ffmpeg, a virus scanner or the indexer still
    has the file open, so keep trying for a moment; a file that stays locked is left behind."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                log.warning("Couldn't delete %s: it is still in use", path)
                return
            time.sleep(0.05)


def run_ffmpeg(
    cmd: list[str],
    output: Path,
    duration: Optional[float],
    on_progress: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    cwd: Optional[Path] = None,
    report: Optional[list[str]] = None,
) -> Path:
    """Run an ffmpeg command that ends with `-progress pipe:1 -nostats <output>`.

    `on_progress` receives a fraction between 0 and 1 (only when the duration
    is known). Setting `cancel_event` stops ffmpeg and removes the partial file.
    `cwd` is ffmpeg's working folder, for filters that read or write files by name.
    `report` collects everything ffmpeg prints on stderr (for filters that print their results).
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    log.info("ffmpeg: %s", subprocess.list2cmdline(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
        cwd=cwd,
    )

    # Drain stderr on a separate thread so a chatty ffmpeg can't block on a full pipe.
    errors: deque[str] = deque(maxlen=20)
    def read_errors() -> None:
        for line in proc.stderr:
            errors.append(line)
            if report is not None:
                report.append(line)

    stderr_reader = threading.Thread(target=read_errors, daemon=True)
    stderr_reader.start()

    # ffmpeg only prints progress every so often, and a slow encode can go seconds between lines, so
    # a cancel is acted on by a watcher instead of waiting for the next line. Windows won't delete
    # (or replace) a file ffmpeg still has open, so the stop has to be prompt.
    stopped = threading.Event()
    finished = threading.Event()

    def watch_for_cancel() -> None:
        while not finished.is_set():
            if cancel_event.wait(0.05):
                if proc.poll() is None:
                    stopped.set()
                    stop_process(proc)
                return

    if cancel_event is not None:
        threading.Thread(target=watch_for_cancel, daemon=True).start()

    for line in proc.stdout:
        if stopped.is_set():
            break
        key, _, value = line.strip().partition("=")
        # Despite its name, out_time_ms is in microseconds.
        if key == "out_time_ms" and duration and on_progress and value.isdigit():
            on_progress(min(int(value) / 1_000_000 / duration, 1.0))

    proc.wait()
    finished.set()
    cancelled = stopped.is_set()
    stderr_reader.join(timeout=2)
    proc.stdout.close()
    proc.stderr.close()

    if cancelled:
        remove_file(output)
        log.info("ffmpeg was stopped on request")
        raise ConversionCancelled()
    if proc.returncode != 0:
        remove_file(output)
        message = "".join(errors).strip() or tr("ffmpeg exited with code {code}", code=proc.returncode)
        log.error("ffmpeg exited with code %s. Last lines of its output: %s", proc.returncode, message)
        if "matches no streams" in message:
            message = tr("This file has no audio track.")
        elif "No such filter" in message and "vidstab" in message:
            message = tr("This copy of ffmpeg can't stabilize video (it was built without libvidstab).")
        raise ConversionError(message)

    if on_progress:
        on_progress(1.0)
    return output


@dataclass
class ConversionJob:
    source: Path
    output: Path
    quality: str = DEFAULT_QUALITY
    start: Optional[float] = None   # seconds; None = from the beginning
    end: Optional[float] = None     # seconds; None = to the end
    keep: list[Range] = field(default_factory=list)   # kept parts of the source; [] = no cuts
    speed: float = 1.0
    reverse: bool = False
    fade_in: float = 0.0
    fade_out: float = 0.0
    sound: Sound = field(default_factory=Sound)       # volume, loudness, noise reduction, music


def check_trim(start: Optional[float], end: Optional[float]) -> None:
    if start is not None and end is not None and end <= start:
        raise ConversionError(tr("The trim end time must be after the start time."))


def no_segments_error() -> ConversionError:
    return ConversionError(tr("Nothing is left to export: the trim and cuts remove the whole video."))


def check_music(ffmpeg: str, sound: Sound, muted: bool = False) -> None:
    """Fail with a clear message when the background music can't be used. A `muted` export has no
    music (Mute wins), so then there is nothing to check."""
    if not sound.music or muted:
        return
    path = Path(sound.music)
    if not path.is_file():
        raise ConversionError(tr("The background music file can't be found: {path}. "
                                 "Choose it again under Sound, or remove it.", path=path))
    if not probe_media(ffmpeg, path).has_audio:
        raise ConversionError(tr("{name} has no sound, so it can't be used as background music.", name=path.name))


def music_length_error() -> ConversionError:
    return ConversionError(tr("Can't add background music: the length of this file is unknown."))


# Where the first loudness pass sits in the progress bar. It only reads and filters the sound, so it is
# quick next to a video export, and about half as long as making an MP3.
LOUDNESS_SHARE = 0.1
LOUDNESS_SHARE_AUDIO = 0.4


def scaled(on_progress: Optional[Callable[[float], None]], start: float, share: float
           ) -> Optional[Callable[[float], None]]:
    """A progress callback that reports 0..1 as `start` .. `start + share` of the real one."""
    if on_progress is None:
        return None
    return lambda fraction: on_progress(start + share * min(max(fraction, 0.0), 1.0))


def measure_loudness(cmd: list[str], scratch: Path, duration: Optional[float],
                     on_progress: Optional[Callable[[float], None]],
                     cancel_event: Optional[threading.Event], cwd: Optional[Path] = None) -> Optional[Loudness]:
    """Run the first loudness pass `cmd` (see `sound.loudnorm`) and return what it measured, or None when
    the sound is silent or can't be measured, so that there is nothing to even out. `scratch` is a file
    name that is never written: run_ffmpeg wants an output to clean up after a cancel."""
    report: list[str] = []
    run_ffmpeg(cmd, scratch, duration, on_progress, cancel_event, cwd, report=report)
    measured = parse_loudness("".join(report))
    if measured is None:
        log.warning("The loudness pass printed no measurement; the loudness is left as it is")
    elif not measured.usable:
        log.info("The sound is silent; the loudness is left as it is")
        return None
    return measured


def mp3_command(ffmpeg: str, job: ConversionJob, segments: list[Segment], voice: bool, total: Optional[float],
                loudness: Optional[Loudness] = None, measure: bool = False) -> list[str]:
    """The ffmpeg command that makes `job`'s MP3. `voice` is whether the source's own sound is used
    (False when the file has none and only the music is). With `measure` it is the loudness pass
    instead: the same sound, thrown away after loudnorm has measured it."""
    sound = job.sound
    music = sound.music and Path(sound.music).resolve()
    filters: list[str] = []
    label = None
    if voice:
        graph = voice_filters("[0:a:0]", "[voice]", segments, sound, job.speed, job.reverse, job.fade_in, job.fade_out)
        if graph:
            filters.append(graph)
        label = "[voice]" if graph else "[0:a:0]"
    final = None
    if music or (sound.normalize and (measure or loudness)):
        filters += master_filters(sound, label, 1 if music else None, total, "[a]", loudness, measure)
        final = "[a]"
    elif filters:
        final = label
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info" if measure else "error", "-y",
           *trim_args(segments[0][0], segments[-1][1]), "-i", str(job.source),
           *(music_args(str(music)) if music else []),
           *(["-filter_complex", ";".join(filters)] if filters else [])]
    cmd += ["-map", final or "0:a:0", "-vn"]
    if measure:
        return [*cmd, "-f", "null", "-progress", "pipe:1", "-nostats", "-"]
    return [*cmd, "-map_metadata", "0", "-c:a", "libmp3lame", *QUALITY_PRESETS[job.quality],
            "-progress", "pipe:1", "-nostats", str(job.output)]


def convert(
    ffmpeg: str,
    job: ConversionJob,
    on_progress: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Path:
    """Extract the first audio track of `job.source` into an MP3 at `job.output`.

    With `job.sound` the volume, noise reduction and background music are applied too, and the
    loudness is evened out in two passes: measuring first, then converting."""
    if not job.source.is_file():
        raise ConversionError(tr("File not found: {path}", path=job.source))
    check_trim(job.start, job.end)

    info = probe_media(ffmpeg, job.source)
    segments = kept_segments(job.keep, job.start, job.end, info.duration)
    if not segments:
        raise no_segments_error()
    sound = job.sound
    check_music(ffmpeg, sound)
    total = output_duration(segments, job.speed)
    if sound.music and total is None:
        raise music_length_error()
    voice = info.has_audio or not sound.music      # without sound of its own, the music is all there is
    measured = None
    share = 0.0
    if sound.normalize and (info.has_audio or sound.music):
        share = LOUDNESS_SHARE_AUDIO
        with tempfile.TemporaryDirectory(prefix="fve-loudness-") as folder:
            measured = measure_loudness(mp3_command(ffmpeg, job, segments, voice, total, measure=True),
                                        Path(folder) / "loudness", total, scaled(on_progress, 0.0, share),
                                        cancel_event, Path(folder))
    cmd = mp3_command(ffmpeg, job, segments, voice, total, measured)
    return run_ffmpeg(cmd, job.output, total, scaled(on_progress, share, 1 - share), cancel_event)
