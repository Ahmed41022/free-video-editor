"""The ffmpeg command lines of an export: codecs, one clip, a GIF, loudness and stabilizer passes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Optional

from ..core import MediaInfo, music_length_error, no_segments_error, trim_args
from ..gpu import encoder_args, encoder_codec
from ..output import (
    DEFAULT_VIDEO_FORMAT, MIN_STEREO_KBPS, OUTPUT_FORMATS, SizeFit, gif_rate, gif_scale,
)
from ..sound import Loudness, master_filters, music_args
from ..timing import Segment, clamp_speed, frame_margin, relative, select_filters
from .edit import STABILIZE, VideoEdit
from .filters import clip_graph, rotation_args
from .geometry import plan_geometry
from .memory import DEFAULT_FPS
from .rates import _rate_text, edit_rate, encoder_level, native_rate, output_rate

# Memory: a 4K export that lets ffmpeg use every thread of a 24-thread PC holds a 4K frame for each decoder
# thread and a 1080p one for each encoder thread, and reached 1.9 GB on 8 seconds (4.9 GB on a longer
# clip). Eight threads for the decoder (it is the 4K frames that are big), 12 for the encoder (x264 and
# x265 stop getting faster there), 4 for the filters, and a shorter look ahead keep it around 700 MB at
# the same speed. Smaller pictures are left alone: their frames are small.
BIG_PICTURE = 2_500_000       # pixels; 1080p is 2.07 million
DECODER_THREADS = 8
FILTER_THREADS = 4
ENCODER_THREADS = max(2, min(os.cpu_count() or 4, 12))
LEAN_LOOKAHEAD = 20           # x264's rc-lookahead for a big picture (it is 40 at the medium preset)


def big_picture(info: Optional[MediaInfo]) -> bool:
    return bool(info and info.width and info.height and info.width * info.height > BIG_PICTURE)


def input_args(segments: list[Segment], source: Path, info: Optional[MediaInfo] = None,
               upright: bool = False) -> list[str]:
    """Seek straight to the first kept part and stop reading after the last one. With `upright` an
    input whose picture is stored turned (a phone video) is opened as stored; see `rotation_args`.
    A big picture is decoded with a few threads (see BIG_PICTURE)."""
    return [*(rotation_args(info) if upright and info else []),
            *(["-threads", str(DECODER_THREADS)] if big_picture(info) else []),
            *trim_args(segments[0][0], segments[-1][1]), "-i", str(source)]


@dataclass
class VideoJob:
    source: Path
    output: Path
    edit: VideoEdit
    overwrite: bool = True      # False: the output name was chosen as free, and is made another if it gets taken


_HEAD = ["-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
_HEAD_INFO = ["-hide_banner", "-nostdin", "-loglevel", "info", "-y"]       # loudnorm prints its result at this level
_TAIL = ["-progress", "pipe:1", "-nostats"]
_X264 = ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
_FASTSTART = ["-movflags", "+faststart"]
# A picture that keeps the changing frame rate of its source (no `-r`) is told so out loud. Measured with a
# 30/120 fps clip: without this, ffmpeg 6.1.1 (Ubuntu 24.04, WSL) wrote an MP4 with a steady step at the
# highest rate and copies in the gaps (360 frames instead of 225); the bundled 7.1 on Windows kept the times
# (225) with or without it. Other ffmpeg versions and platforms: untested.
_KEEP_TIMES = ["-fps_mode", "vfr"]
_AAC = ["-c:a", "aac", "-b:a", "192k"]
# What every phone plays: AAC LC at 48 kHz in stereo (more channels are mixed down).
_AAC_PHONE = [*_AAC, "-ar", "48000", "-ac", "2"]
_X265 = ["-c:v", "libx265", "-preset", "medium", "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
_VP9 = ["-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4"]
# The same quality number asks more of x265 and VP9, which both count a little differently from x264.
_CRF_OFFSET = {"h264": 0, "hevc": 5, "vp9": 9}
PASSLOG = "passlog"       # the first pass's notes for the second, in ffmpeg's working folder


def audio_codec_args(edit: VideoEdit, phone: bool = False, kbps: int = 0) -> list[str]:
    """How to encode the sound for `edit`'s format. `phone` is a steady-frame-rate export, whose sound
    is AAC at 48 kHz in stereo; `kbps` (more than 0) sets the bitrate, for an export that has to fit a file size."""
    mono = 0 < kbps < MIN_STEREO_KBPS       # the least a file size leaves for the sound is a mono voice
    if edit.output_format.audio == "opus":
        return ["-c:a", "libopus", "-b:a", f"{kbps or 128}k", "-ar", "48000", *(["-ac", "1"] if mono else [])]
    args = ["-c:a", "aac", "-b:a", f"{kbps or 192}k"]
    if mono:
        return [*args, "-ar", "48000", "-ac", "1"]
    return [*args, "-ar", "48000", "-ac", "2"] if phone else args


def video_codec_args(crf: int, encoder: Optional[str] = None, fps: Optional[float | Fraction] = None,
                     level: Optional[str] = None, video_format: str = DEFAULT_VIDEO_FORMAT,
                     fit: Optional[SizeFit] = None, first_pass: bool = False, lean: bool = False,
                     changing_rate: Optional[tuple[float, float]] = None) -> list[str]:
    """How to encode the picture at quality `crf`: with the hardware `encoder`, or on the CPU (x264, x265
    or VP9, by `video_format`). `fps` is the frame rate of the result, when known; it is always passed on
    to the encoder, which otherwise works one out from the filtered picture's time base (often 90000) and
    writes the file as the highest level there is. With a `level` (see `h264_level`) an H.264 video is
    High profile at that level, which is how a phone-safe export says what it needs; an H.265 video is
    given its level (`h265_level`) only when there is no `fps`, because x265 works it out from the rate.

    `changing_rate` is (average, highest) for a picture without `fps` that keeps the changing frame rate of
    its source (a game recording). The encoders that need a rate are told one that fits it: Intel's, which
    has to make the picture steady, gets the average (not 30 fps, which drops half of a 60 fps recording),
    and x265 the highest, which its level is checked against (else it works a rate of 1000 or more out of
    the time base and refuses the level).

    With a `fit` the picture gets its bitrate instead of a quality: from the graphics card, or on the CPU
    in two passes (`first_pass` is the first one, which makes the notes the second needs). On the CPU, x264
    and x265 use at most ENCODER_THREADS threads, and with `lean` (a big picture) x264 looks ahead less."""
    fmt = OUTPUT_FORMATS.get(video_format, OUTPUT_FORMATS[DEFAULT_VIDEO_FORMAT])
    codec = fmt.codec
    if encoder and encoder_codec(encoder) != codec:
        encoder = None          # an H.264 card can't make H.265
    kbps = fit.kbps if fit else None
    # The encoders spread a bitrate over frames at the frame rate they are told, and a picture that has
    # been through the filters doesn't tell them (x265, VP9 and the graphics cards then aim far too low):
    # a file size says it out loud, and the export runs at that steady rate.
    known = fps or (DEFAULT_FPS if kbps else None)
    rate = ["-r", _rate_text(known)] if known else []
    if encoder:
        card_rate = fps or (native_rate(changing_rate[0]) if changing_rate else DEFAULT_FPS)
        # Only H.264 gets a level here: the cards refuse an H.265 one ("Invalid Level") for a changing rate.
        args = encoder_args(encoder, crf, float(card_rate), level if codec == "h264" else None, kbps)
        return [*args, *((rate or _KEEP_TIMES) if "-r" not in args else []), *_FASTSTART]
    number = "1" if first_pass else "2"
    bitrate = [*rate, "-b:v", f"{kbps}k"]
    if codec == "hevc":
        params = [*([f"pass={number}", f"stats={PASSLOG}.log"] if kbps else []),
                  *([f"level-idc={level}"] if level and not fps else []),
                  *([f"fps={_rate_text(native_rate(changing_rate[1]))}"] if changing_rate and not known else [])]
        args = [*_X265, "-threads", str(ENCODER_THREADS),
                *([*bitrate] if kbps else [*rate, "-crf", str(crf + _CRF_OFFSET[codec])]),
                *(["-x265-params", ":".join(params)] if params else [])]
    elif codec == "vp9":
        args = [*_VP9, *([*bitrate, "-pass", number, "-passlogfile", PASSLOG] if kbps
                         else [*rate, "-crf", str(crf + _CRF_OFFSET[codec]), "-b:v", "0"])]
    else:
        args = [*_X264, "-threads", str(ENCODER_THREADS),
                *([*bitrate, "-pass", number, "-passlogfile", PASSLOG] if kbps else [*rate, "-crf", str(crf)])]
        if lean:
            args += ["-x264-params", f"rc-lookahead={LEAN_LOOKAHEAD}"]
        if level:
            args += ["-profile:v", "high", "-level", level]
    # The first pass writes nothing but its notes, and only an MP4 or MOV has a front to move.
    if not known:
        args += _KEEP_TIMES
    return args if first_pass or fmt.extension == ".webm" else [*args, *_FASTSTART]


def music_path(edit: VideoEdit, video: bool = True) -> Optional[str]:
    """The background music to lay under an export of `edit`, as a full path, or None. A video export
    that removes the sound has no music either: Mute wins over everything."""
    if not edit.music or (video and edit.mute):
        return None
    return str(Path(edit.music).resolve())      # ffmpeg may run in another folder


def video_command(ffmpeg: str, job: VideoJob, info: MediaInfo, transforms: Optional[str] = None,
                  encoder: Optional[str] = None, loudness: Optional[Loudness] = None,
                  fit: Optional[SizeFit] = None, first_pass: bool = False) -> list[str]:
    """The export command. `loudness` is what the first loudness pass measured (see `measure_command`);
    with it, and `edit.normalize`, the sound is evened out.

    With a `fit` the picture is encoded at its bitrate; on the CPU that takes two passes, and
    `first_pass` is the command for the first (the picture only, thrown away). A GIF has its own two
    commands (see `gif_command`), where `first_pass` makes the palette."""
    edit = job.edit
    if edit.output_format.codec == "gif":
        return gif_command(ffmpeg, job, info, transforms, first_pass)
    segments = edit.segments(info.duration, info.fps)
    if not segments:
        raise no_segments_error()
    rate = edit_rate(edit, info)
    music = None if first_pass else music_path(edit)
    has_voice = info.has_audio and not edit.mute
    # The sound goes on to the final mix when there is music or a loudness to apply to it.
    mastered = (has_voice or bool(music)) and (bool(music) or (edit.normalize and loudness is not None))
    graph = clip_graph(0, edit, info, segments, audio=not first_pass, fps=rate, transforms=transforms,
                       a_out="[voice]" if mastered else "[a]")
    filters = list(graph.filters)
    if music and graph.duration is None:
        raise music_length_error()
    if mastered and not first_pass:
        filters += master_filters(edit.sound, "[voice]" if graph.has_audio else None, 1 if music else None,
                                  graph.duration, "[a]", loudness)
    cmd = [ffmpeg, *_HEAD, *(["-filter_complex_threads", str(FILTER_THREADS)] if big_picture(info) else []),
           *input_args(segments, job.source, info), *(music_args(music) if music else []),
           "-filter_complex", ";".join(filters), "-map", "[v]"]
    if graph.has_audio or music:
        cmd += ["-map", "[a]", *audio_codec_args(edit, bool(rate), fit.audio_kbps if fit else 0)]
    else:
        cmd += ["-an"]
    # Always say the frame rate and the level: left to itself, the encoder takes the time base of the filtered
    # picture for the frame rate, and the file comes out flagged as H.264 level 6.2 (H.265 6.1).
    out_rate = output_rate(edit, info)
    geometry = plan_geometry(info.width, info.height, edit)
    level = encoder_level(edit.output_format.codec, geometry.out_width, geometry.out_height, out_rate, info)
    # A file size is planned at the rate the picture has on average, even a changing one, so it is told so.
    told = out_rate or (info.fps if fit and info.fps else None)
    changing = (info.fps, info.max_fps or info.fps) if not told and info.fps else None
    cmd += video_codec_args(edit.crf, encoder, told, level, edit.video_format, fit, first_pass,
                            lean=big_picture(info), changing_rate=changing)
    if first_pass:
        return [*cmd, "-f", "null", *_TAIL, "-"]
    return [*cmd, "-map_metadata", "0", *_TAIL, str(job.output.resolve())]


GIF_PALETTE = "palette.png"      # the colors of the GIF, made by its first pass in ffmpeg's working folder


def gif_command(ffmpeg: str, job: VideoJob, info: MediaInfo, transforms: Optional[str] = None,
                palette: bool = False) -> list[str]:
    """The export command for a GIF, which takes two: the first (`palette`) picks the best 256 colors for
    the whole clip, and the second uses them. The picture goes through the same cuts, speed, reframing and
    adjustments as any export, then is made no wider than `edit.gif_width` at no more than `edit.gif_fps`
    frames a second, and loops forever. There is no sound."""
    edit = job.edit
    segments = edit.segments(info.duration, info.fps)
    if not segments:
        raise no_segments_error()
    rate = gif_rate(edit.gif_fps, info.fps * clamp_speed(edit.speed) if info.fps else None)
    graph = clip_graph(0, edit, info, segments, audio=False, fps=rate, transforms=transforms)
    filters = [*graph.filters, f"[v]{gif_scale(edit.gif_width)}[g]"]
    cmd = [ffmpeg, *_HEAD, *input_args(segments, job.source)]
    if palette:
        # A second copy of the picture goes nowhere, but gives the progress bar something to count.
        filters += ["[g]split=2[a][b]", "[a]palettegen=stats_mode=diff[p]"]
        return [*cmd, "-filter_complex", ";".join(filters), *_TAIL, "-map", "[p]", "-frames:v", "1", "-update", "1",
                GIF_PALETTE, "-map", "[b]", "-f", "null", "-"]
    filters.append("[g][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle[out]")
    return [*cmd, "-i", GIF_PALETTE, "-filter_complex", ";".join(filters), "-map", "[out]", "-an", "-loop", "0",
            *_TAIL, str(job.output.resolve())]


def measure_command(ffmpeg: str, job: VideoJob, info: MediaInfo) -> list[str]:
    """The first loudness pass: the sound of the export, music included, measured by loudnorm and
    thrown away. It prints what it measured as JSON, which `sound.parse_loudness` reads."""
    edit = job.edit
    segments = edit.segments(info.duration, info.fps)
    if not segments:
        raise no_segments_error()
    music = music_path(edit)
    graph = clip_graph(0, edit, info, segments, picture=False, a_out="[voice]")
    if music and graph.duration is None:
        raise music_length_error()
    filters = graph.filters + master_filters(edit.sound, "[voice]" if graph.has_audio else None,
                                             1 if music else None, graph.duration, "[a]", measure=True)
    return [ffmpeg, *_HEAD_INFO, *input_args(segments, job.source), *(music_args(music) if music else []),
            "-filter_complex", ";".join(filters), "-map", "[a]", "-f", "null", *_TAIL, "-"]


def detect_command(ffmpeg: str, source: Path, edit: VideoEdit, segments: list[Segment], transforms: str,
                   fps: Optional[float] = None) -> list[str]:
    """The stabilizer's first pass: measure the shake of the kept frames into `transforms`.
    `segments` and `fps` are the export's own, so both passes see the same frames."""
    shakiness = STABILIZE[edit.stabilize][0]
    graph = select_filters("[0:v:0]", "[t]", relative(segments), audio=False, margin=frame_margin(fps))
    graph.append(f"[t]vidstabdetect=shakiness={shakiness}:accuracy=15:result={transforms}[d]")
    return [ffmpeg, *_HEAD, *input_args(segments, source), "-filter_complex", ";".join(graph),
            "-map", "[d]", "-f", "null", *_TAIL, "-"]
