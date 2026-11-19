"""Joining clips into one file."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

from ..core import (
    DEFAULT_QUALITY, LOUDNESS_SHARE, LOUDNESS_SHARE_AUDIO, QUALITY_PRESETS, ConversionError,
    MediaInfo, check_music, check_trim, probe_media, staged_output,
)
from ..gpu import encoder_codec
from ..i18n import tr
from ..output import SizeFit
from ..sound import STEREO_48K, Loudness, master_filters, music_args
from .commands import (
    _HEAD, _HEAD_INFO, _TAIL, audio_codec_args, big_picture, input_args, music_path,
    video_codec_args,
)
from .edit import VideoEdit
from .export import _run_export
from .filters import clip_graph
from .fit import plan_fit
from .geometry import plan_geometry
from .rates import encoder_level, native_rate, source_rate, steady_rate

JOIN_AUDIO = STEREO_48K


@dataclass
class JoinPart:
    source: Path
    edit: VideoEdit
    info: Optional[MediaInfo] = None


@dataclass
class JoinJob:
    parts: list[JoinPart]
    output: Path
    audio_only: bool = False  # True: one MP3 instead of one MP4
    quality: str = DEFAULT_QUALITY  # MP3 quality
    overwrite: bool = True  # False: the output name was chosen as free, and is made another if it gets taken


def _join_sound(job: JoinJob) -> tuple[bool, Optional[str]]:
    """Whether any clip of a join has sound to play, and the background music laid over the whole result.
    The music is the first clip's, like the video quality; the first clip removing the sound removes it."""
    video = not job.audio_only
    any_audio = any(p.info.has_audio and not (video and p.edit.mute) for p in job.parts)
    return any_audio, music_path(job.parts[0].edit, video)


def join_command(
        ffmpeg: str, job: JoinJob, transforms: Optional[dict[int, str]] = None,
        encoder: Optional[str] = None, loudness: Optional[Loudness] = None, measure: bool = False,
        fit: Optional[SizeFit] = None, first_pass: bool = False) -> tuple[list[str], float]:
    """The ffmpeg command that plays every part back to back, and the result's length.

    Each clip is cut, sped up, reframed and faded with its own settings, then fitted into
    the first clip's frame size and frame rate. The video quality is the first clip's, and so is the
    steady frame rate if it has one: then the result is phone-safe at that rate (the fastest of the
    clips' own rates when none is above it), whatever rate the first clip has. The file format and the
    file size to fit are the first clip's too. Each clip keeps its own
    volume and noise reduction; the loudness and the background music are the first clip's, applied to
    the joined sound. With `measure` it is the first loudness pass instead (see `measure_command`);
    with `first_pass` it is the first of the two passes of a `fit` (the picture only)."""
    parts, video = job.parts, not job.audio_only
    picture = video and not measure
    first = parts[0]
    if video and first.edit.output_format.codec == "gif":
        raise ConversionError(
            tr(
                "Joined clips can't be saved as a GIF. Export the clips one at a time, "
                "or choose another format for the first clip."))
    frame = fps = level = None
    steady = None
    if picture:
        geometry = plan_geometry(first.info.width, first.info.height, first.edit)
        frame = (geometry.out_width, geometry.out_height)
        rates = [source_rate(p.info, p.edit.speed) for p in parts if p.info.fps]
        steady = steady_rate(first.edit.frame_rate, max(rates, default=None))
        fps = steady or (native_rate(first.info.fps) if first.info.fps else Fraction(30))
        level = encoder_level(first.edit.output_format.codec, *frame, fps, first.info)
    any_audio, music = _join_sound(job)
    if first_pass:
        any_audio, music = False, None
    if not video and not any_audio and not music:
        raise ConversionError(tr("None of these files have sound."))
    sound = first.edit.sound
    # The sound goes through the final mix when there is music, or a loudness to apply or to measure.
    mastered = (any_audio or bool(music)) and (
        bool(music) or (sound.normalize and (loudness is not None or measure)))
    voice = "[voice]" if mastered else "[a]"

    inputs, filters, labels, total = [], [], [], 0.0
    for i, part in enumerate(parts):
        segments = part.edit.segments(part.info.duration, part.info.fps if picture else None)
        if not segments:
            raise ConversionError(
                tr(
                    "Nothing is left of {name}: the trim and cuts remove all of it.",
                    name=part.source.name))
        graph = clip_graph(
            i, part.edit, part.info, segments, video=video, audio=any_audio, frame=frame, fps=fps,
            v_out=f"[v{i}]", a_out=f"[sound{i}]", transforms=(transforms or {}).get(i),
            picture=picture, upright=picture)
        if graph.duration is None:
            raise ConversionError(tr("Couldn't read the length of {name}.", name=part.source.name))
        inputs += input_args(
            segments, part.source, part.info if picture else None, upright=picture)
        filters += graph.filters
        total += graph.duration
        label = f"[v{i}]" if picture else ""
        if any_audio:
            if graph.has_audio:
                filters.append(f"[sound{i}]{JOIN_AUDIO}[a{i}]")
            else:  # silence, so the sound stays in step with the pictures
                filters.append(
                    f"anullsrc=r=48000:cl=stereo,atrim=duration={graph.duration:.3f},{JOIN_AUDIO}[a{i}]"
                )
            label += f"[a{i}]"
        labels.append(label)
    outputs = ("[v]" if picture else "") + (voice if any_audio else "")
    if outputs:
        filters.append(
            f"{''.join(labels)}concat=n={len(parts)}:v={int(picture)}:a={int(any_audio)}{outputs}")
    if mastered:
        filters += master_filters(
            sound, voice if any_audio else None,
            len(parts) if music else None, total, "[a]", loudness, measure)

    cmd = [
        ffmpeg, *(_HEAD_INFO if measure else _HEAD), *inputs,
        *(music_args(music) if music else []), "-filter_complex", ";".join(filters)
    ]
    if measure:
        return [*cmd, "-map", "[a]", "-f", "null", *_TAIL, "-"], total
    if video:
        sounds = any_audio or bool(music)
        cmd += [
            "-map", "[v]", *(
                [
                    "-map", "[a]",
                    *audio_codec_args(first.edit, bool(steady), fit.audio_kbps if fit else 0)
                ] if sounds else ["-an"]), *video_codec_args(
                    first.edit.crf, encoder, fps, level, first.edit.video_format, fit, first_pass,
                    lean=any(big_picture(p.info) for p in parts))
        ]
        if first_pass:
            return [*cmd, "-f", "null", *_TAIL, "-"], total
    else:
        cmd += ["-map", "[a]", "-vn", "-c:a", "libmp3lame", *QUALITY_PRESETS[job.quality]]
    cmd += ["-map_metadata", "0", *_TAIL, str(job.output.resolve())]
    return cmd, total


def join_clips(
    ffmpeg: str, job: JoinJob, on_progress: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None, encoder: Optional[str] = None,
    on_note: Optional[Callable[[str], None]] = None,
) -> Path:
    """Export several clips, in order, as one MP4 (or one MP3 with `job.audio_only`), or in the first
    clip's other format and file size (see `join_command`). `encoder` and `on_note` work as in `convert_video`."""
    if len(job.parts) < 2:
        raise ConversionError(tr("Joining needs at least two clips."))
    for part in job.parts:
        if not part.source.is_file():
            raise ConversionError(tr("File not found: {path}", path=part.source))
        check_trim(part.edit.start, part.edit.end)
        part.info = part.info or probe_media(ffmpeg, part.source)
        if not job.audio_only and not part.info.has_video:
            raise ConversionError(tr("{name} has no video track.", name=part.source.name))
        part.source = part.source.resolve()  # ffmpeg may run in another folder
    first = job.parts[0].edit
    check_music(ffmpeg, first.sound, first.mute and not job.audio_only)
    _, duration = join_command(ffmpeg, job)  # fail early on bad settings
    any_audio, music = _join_sound(job)
    fit = None
    if not job.audio_only and first.target_mb > 0:
        head = job.parts[0]
        # The picture is the first clip's size and frame rate, so that is what has to be made smaller.
        rates = [source_rate(p.info, p.edit.speed) for p in job.parts if p.info.fps]
        steady = steady_rate(first.frame_rate, max(rates, default=None))
        lowered, fit = plan_fit(
            first, head.info, duration, any_audio or bool(music),
            float(steady) if steady else head.info.fps)
        if fit and fit.short:
            head.edit = lowered
            first = lowered
        if fit and fit.note and on_note:
            on_note(fit.note)
    measure = join_command(ffmpeg, job,
                           measure=True)[0] if first.normalize and (any_audio or music) else None
    clips = [] if job.audio_only else [
        (p.source, p.edit, p.edit.segments(p.info.duration, p.info.fps), p.info.fps)
        for p in job.parts
    ]
    encoder = None if job.audio_only or (
        encoder and encoder_codec(encoder) != first.output_format.codec) else encoder
    with staged_output(job.output,
                       [*(p.source for p in job.parts), *([Path(music)] if music else [])],
                       job.overwrite) as staged:
        export = replace(job, output=staged.part)
        _run_export(
            ffmpeg, clips, lambda transforms, enc, loudness: join_command(
                ffmpeg, export, transforms, enc, loudness, fit=fit)[0], export.output, duration,
            on_progress, cancel_event, encoder, on_note, measure,
            LOUDNESS_SHARE_AUDIO if job.audio_only else LOUDNESS_SHARE, fit, (
                lambda transforms: join_command(
                    ffmpeg, export, transforms, fit=fit, first_pass=True)[0]) if fit else None)
    return staged.final
