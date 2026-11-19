"""Exporting one video: the passes, the retries and the file kept."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable, Optional

from ..core import (
    LOUDNESS_SHARE, ConversionCancelled, ConversionError, MediaInfo, check_music, check_trim,
    measure_loudness, never_back, probe_media, remove_file, run_ffmpeg, scaled, staged_output,
    work_folder,
)
from ..gpu import CPU_FALLBACK_NOTE, encoder_codec
from ..i18n import tr
from ..output import TRIES, UNDERUSE, SizeFit, next_kbps
from ..sound import Loudness
from ..timing import Segment, output_duration, segments_length
from .commands import VideoJob, detect_command, measure_command, music_path, video_command
from .edit import STABILIZE, VideoEdit
from .fit import edit_fit, wants_loudness_pass

log = logging.getLogger(__name__)

DETECT_SHARE = 0.2  # part of the progress bar for the stabilizer's detect passes, which take about a fifth


def _encode(
        command: Callable[[Optional[str]], list[str]], encoder: Optional[str], output: Path,
        duration: Optional[float], on_progress: Optional[Callable[[float], None]],
        cancel_event: Optional[threading.Event], cwd: Optional[Path] = None,
        on_note: Optional[Callable[[str], None]] = None) -> Path:
    """Run the export command `command(encoder)`. When the hardware `encoder` fails, whatever the
    reason (a driver that balks, a size it can't do), run the same job again on the CPU, once."""
    if encoder is None:
        return run_ffmpeg(command(None), output, duration, on_progress, cancel_event, cwd)
    try:
        return run_ffmpeg(command(encoder), output, duration, on_progress, cancel_event, cwd)
    except ConversionError as exc:
        # run_ffmpeg has already removed the partial file.
        if cancel_event is not None and cancel_event.is_set():
            raise ConversionCancelled() from None
        log.warning(
            "The graphics card encoder %s failed (%s); trying again on the CPU", encoder, exc)
        if on_note:
            on_note(tr(CPU_FALLBACK_NOTE))
    return run_ffmpeg(command(None), output, duration, on_progress, cancel_event, cwd)


# Where a file size's encode goes in the bar, first try and then each retry: it only ever moves forward.
TRY_SPANS = ((0.0, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 0.98))
PREPASS_SHARE = 0.4  # part of what is left of the progress bar for a first pass (the notes for a file size, or a GIF's colors)


def _finish(
        command: Callable[[dict[int, str], Optional[str], Optional[Loudness]],
                          list[str]], transforms: dict[int, str], loudness: Optional[Loudness],
        encoder: Optional[str], output: Path, duration: Optional[float],
        on_progress: Optional[Callable[[float], None]], cancel_event: Optional[threading.Event],
        folder: Path, on_note: Optional[Callable[[str], None]], fit: Optional[SizeFit],
        prepass: Optional[Callable[[dict[int, str]], list[str]]]) -> Path:
    """The last step of an export in `folder`: the first pass that the encode needs (`prepass`, which
    only the CPU needs), the encode itself, and, with a `fit`, a look at how big the file came out.

    A file over its size limit is made again with a lower bitrate (both passes, as they have to agree on
    it), and one that comes out well under it with a higher one, up to TRIES times in all (see
    `next_kbps`); the biggest file that fits is kept. A rate control that can't be told exactly how many bytes to make
    (the graphics card's, or any with a very hard picture) needs this. If the card fails the whole job
    runs again on the CPU, as in `_encode`.

    The file that is kept while another try is made sits next to `output` (a ".best" file), not in `folder`
    (which is in the temporary files, often on another drive): moving it back would copy the whole file."""

    def encode(enc: Optional[str]) -> list[str]:
        return command(transforms, enc, loudness)

    def first_pass(report: Optional[Callable[[float], None]]) -> None:
        run_ffmpeg(prepass(transforms), folder / "prepass", duration, report, cancel_event, folder)

    on_progress = never_back(
        on_progress)  # a second try, or the CPU after the card failed, starts over inside
    share = PREPASS_SHARE if prepass and encoder is None else 0.0
    if prepass and encoder is None:
        first_pass(scaled(on_progress, 0.0, share))
    final = scaled(on_progress, share, 1 - share)
    if fit is None:
        return _encode(
            encode, encoder, output, duration, final, cancel_event, cwd=folder, on_note=on_note)
    best: Optional[Path] = None  # the biggest file so far that fit, kept while another try is made
    best_path = output.with_name(f"{output.stem}.best{output.suffix}")
    best_size = 0
    history: list[tuple[int, int]] = []  # (kbps, size) of every try so far
    prepared = fit.kbps if prepass and encoder is None else None  # the bitrate the first pass was made at
    try:
        while True:
            if prepass and encoder is None and prepared != fit.kbps:
                first_pass(
                    None
                )  # the two passes have to be told the same bitrate, or the second can't aim
                prepared = fit.kbps
            # The first try has most of the bar; each further one only a little of what is left, so the bar goes on.
            start, end = TRY_SPANS[min(len(history), len(TRY_SPANS) - 1)]
            try:
                run_ffmpeg(
                    encode(encoder), output, duration, scaled(final, start, end - start),
                    cancel_event, folder)
            except ConversionError as exc:
                if encoder is None:
                    raise
                if cancel_event is not None and cancel_event.is_set():
                    raise ConversionCancelled() from None
                log.warning(
                    "The graphics card encoder %s failed (%s); trying again on the CPU", encoder,
                    exc)
                if on_note:
                    on_note(tr(CPU_FALLBACK_NOTE))
                encoder = None
                continue
            size = output.stat().st_size
            history.append((fit.kbps, size))
            log.info(
                "The file came out at %.2f MB; the limit is %.2f MB", size / 1e6,
                fit.target_bytes / 1e6)
            if size <= fit.target_bytes:
                if size >= fit.target_bytes * UNDERUSE:
                    if on_progress:
                        on_progress(1.0)
                    return output
                if size > best_size:
                    best, best_size = best_path, size
                    os.replace(output, best)  # over the smaller one kept before
            more = next_kbps(fit, history) if len(history) < TRIES else None
            if more is None:
                break
            log.info("Trying again at %d kbps", more)
            fit.kbps = more
        if best is None:
            remove_file(output)
            raise ConversionError(
                tr(
                    "The video came out at {size:.1f} MB, over the {limit:g} MB limit, even after "
                    "trying again. Shorten the video, or choose a bigger size.", size=size / 1e6,
                    limit=fit.target_bytes / 1e6))
        os.replace(best, output)
        best = None
        if on_progress:
            on_progress(1.0)
        return output
    finally:
        if best is not None:
            remove_file(best)


def _run_export(
        ffmpeg: str, clips: list[tuple[Path, VideoEdit, list[Segment], Optional[float]]],
        command: Callable[[dict[int, str], Optional[str], Optional[Loudness]],
                          list[str]], output: Path, duration: Optional[float],
        on_progress: Optional[Callable[[float], None]], cancel_event: Optional[threading.Event],
        encoder: Optional[str] = None, on_note: Optional[Callable[[str], None]] = None,
        measure: Optional[list[str]] = None, measure_share: float = LOUDNESS_SHARE,
        fit: Optional[SizeFit] = None, prepass: Optional[Callable[[dict[int, str]],
                                                                  list[str]]] = None) -> Path:
    """Run an export, first running the stabilizer's detect pass for every clip that asks for it, and
    the loudness pass (the command `measure`) when the loudness is to be evened out.

    Each clip is (source, edit, kept segments, frame rate). `command(transforms, encoder, loudness)`
    builds the export command given {clip index: transforms file name}, the hardware encoder to use
    (None = the CPU) and what the loudness pass measured (None = nothing to apply). The transforms
    files live in a temporary folder that is ffmpeg's working folder. The passes come first and take
    their share of the progress bar; cancelling stops whichever one is running.

    A file size to `fit` or a first pass for the encode itself (`prepass(transforms)` builds its
    command: the two-pass notes, a GIF's colors) comes last, just before the encode; see `_finish`."""
    shaky = [i for i, (_, edit, _, _) in enumerate(clips) if STABILIZE.get(edit.stabilize)]
    if not shaky and measure is None and fit is None and prepass is None:
        return _encode(
            lambda enc: command({}, enc, None), encoder, output, duration, on_progress,
            cancel_event, on_note=on_note)
    with work_folder("fve-export-") as folder:
        folder = Path(folder)
        used = 0.0
        transforms: dict[int, str] = {}
        if shaky:
            lengths = {i: segments_length(clips[i][2]) or 0.0 for i in shaky}
            total = sum(lengths.values()) or 1.0
            done = 0.0
            for i in shaky:
                source, edit, segments, fps = clips[i]
                name = f"shake{i}.trf"

                def detect_progress(
                        fraction: float, done: float = done, length: float = lengths[i]) -> None:
                    on_progress(DETECT_SHARE * min((done + fraction * length) / total, 1.0))

                run_ffmpeg(
                    detect_command(ffmpeg, source, edit, segments, name,
                                   fps), folder / name, lengths[i] or None,
                    detect_progress if on_progress else None, cancel_event, cwd=folder)
                done += lengths[i]
                transforms[i] = name
            used = DETECT_SHARE
        loudness = None
        if measure is not None:
            loudness = measure_loudness(
                measure, folder / "loudness", duration, scaled(on_progress, used, measure_share),
                cancel_event, folder)
            used += measure_share
        return _finish(
            command, transforms, loudness, encoder, output, duration,
            scaled(on_progress, used, 1 - used), cancel_event, folder, on_note, fit, prepass)


def convert_video(
    ffmpeg: str, job: VideoJob, on_progress: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None, info: Optional[MediaInfo] = None,
    encoder: Optional[str] = None, on_note: Optional[Callable[[str], None]] = None,
) -> Path:
    """Export `job.source` as an edited video at `job.output`: an MP4 unless `job.edit.video_format` says
    another (H.265, WebM, MOV or GIF).

    With a hardware `encoder` (see `gpu.choose_encoder`) the graphics card does the encoding; if that
    fails the export is run again on the CPU and `on_note` hears about it. With `job.edit.target_mb` the
    file is made to fit that size (see `plan_fit`), and `on_note` hears when the picture had to be made smaller."""
    if not job.source.is_file():
        raise ConversionError(tr("File not found: {path}", path=job.source))
    check_trim(job.edit.start, job.edit.end)
    info = info or probe_media(ffmpeg, job.source)
    if not info.has_video:
        raise ConversionError(tr("This file has no video track."))
    gif = job.edit.output_format.codec == "gif"
    if not gif:
        check_music(ffmpeg, job.edit.sound, job.edit.mute)

    job = VideoJob(
        job.source.resolve(), job.output, job.edit,
        job.overwrite)  # ffmpeg may run in another folder
    video_command(ffmpeg, job, info)  # fail early on bad settings
    edit, fit = edit_fit(job.edit, info)
    if fit and fit.note and on_note:
        on_note(fit.note)
    segments = edit.segments(info.duration, info.fps)
    duration = output_duration(segments, edit.speed)
    if encoder and encoder_codec(encoder) != edit.output_format.codec:
        encoder = None  # a card that makes H.264 can't make H.265
    music = music_path(edit)
    # The result is written as "name.part.mp4" and gets its name when it is complete (see staged_output).
    with staged_output(job.output, [job.source, *([Path(music)] if music else [])],
                       job.overwrite) as staged:
        job = VideoJob(job.source, staged.part, edit)
        measure = measure_command(ffmpeg, job, info) if wants_loudness_pass(edit, info) else None
        # The first pass of a file size (CPU only) or of a GIF: not the picture itself, but what the encode needs.
        prepass = (lambda transforms: video_command(ffmpeg, job, info, transforms.get(0), None, None, fit, True)) \
            if gif or fit else None
        _run_export(
            ffmpeg, [(job.source, edit, segments, info.fps)],
            lambda transforms, enc, loudness: video_command(
                ffmpeg, job, info, transforms.get(0), enc, loudness, fit), job.output, duration,
            on_progress, cancel_event, encoder, on_note, measure, fit=fit, prepass=prepass)
    return staged.final
