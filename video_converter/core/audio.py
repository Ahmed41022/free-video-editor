"""Converting to MP3."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

from ..i18n import N_, tr
from ..sound import Loudness, Sound, master_filters, music_args, parse_loudness, voice_filters
from ..timing import Range, Segment, kept_segments, output_duration
from .checks import check_music, check_trim, music_length_error, no_segments_error
from .commands import trim_args
from .errors import ConversionError
from .paths import work_folder
from .probe import probe_media
from .progress import LOUDNESS_SHARE_AUDIO, scaled
from .runner import run_ffmpeg
from .staging import staged_output

log = logging.getLogger(__name__)


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
    overwrite: bool = True      # False: the output name was chosen as free, and is made another if it gets taken


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
                loudness: Optional[Loudness] = None, measure: bool = False, cover: Optional[int] = None) -> list[str]:
    """The ffmpeg command that makes `job`'s MP3. `voice` is whether the source's own sound is used
    (False when the file has none and only the music is). With `measure` it is the loudness pass
    instead: the same sound, thrown away after loudnorm has measured it. `cover` is the index of the
    source's cover picture, which the MP3 keeps."""
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
    cmd += ["-map", final or "0:a:0"]
    if measure or cover is None:
        cmd.append("-vn")
    if measure:
        return [*cmd, "-f", "null", "-progress", "pipe:1", "-nostats", "-"]
    if cover is not None:
        # The cover picture of a music file travels as it is (an MP3 holds a JPEG or PNG one).
        cmd += ["-map", f"0:{cover}", "-c:v", "copy", "-disposition:v:0", "attached_pic", "-id3v2_version", "3"]
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
    inputs = [job.source, *([Path(sound.music)] if sound.music else [])]
    with staged_output(job.output, inputs, job.overwrite) as staged:
        measured = None
        share = 0.0
        if sound.normalize and (info.has_audio or sound.music):
            share = LOUDNESS_SHARE_AUDIO
            with work_folder("fve-loudness-") as folder:
                measured = measure_loudness(mp3_command(ffmpeg, job, segments, voice, total, measure=True),
                                            Path(folder) / "loudness", total, scaled(on_progress, 0.0, share),
                                            cancel_event, Path(folder))
        cmd = mp3_command(ffmpeg, replace(job, output=staged.part), segments, voice, total, measured,
                          cover=info.cover_stream if info.has_audio else None)
        run_ffmpeg(cmd, staged.part, total, scaled(on_progress, share, 1 - share), cancel_event)
    return staged.final
