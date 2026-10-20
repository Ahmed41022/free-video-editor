"""Entry point.

    python -m video_converter                          # open the desktop app
    python -m video_converter video.mp4 [...]          # convert to MP3 from the command line
    python -m video_converter video.mp4 --video phone  # export an edited MP4 instead
    python -m video_converter a.mp4 b.mp4 --video original --join   # one MP4 made of both
    python -m video_converter clip.mp4 --video original --fps 60    # a copy that plays on a phone
    python -m video_converter talk.mp4 --remove-silences            # an MP3 without the pauses
    python -m video_converter talk.mp4 --normalize --music song.mp3 --duck    # even loudness, music under it
    python -m video_converter clip.mp4 --video original --target-size 10      # a copy that fits Discord's 10 MB
    python -m video_converter clip.mp4 --format gif --end 0:05                # a looping GIF of the first 5 seconds
    python -m video_converter clip.mp4 --frame-at 0:12 --frame-out frame.png  # save one frame as a picture
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

from . import __version__
from .core import (
    DEFAULT_QUALITY,
    QUALITY_PRESETS,
    ConversionError,
    ConversionJob,
    convert,
    find_ffmpeg,
    format_time,
    output_path_for,
    parse_time,
    probe_media,
    unique_output_path,
)
from .gpu import choose_encoder
from .output import GIF_FPS, GIF_FPS_RANGE, GIF_WIDTH, GIF_WIDTH_RANGE, OUTPUT_FORMATS, TARGET_RANGE
from .silence import (
    DB_RANGE, DEFAULT_DB, DEFAULT_MIN, DEFAULT_PAD, MIN_RANGE, PAD_RANGE, detect_silences, plan_silence_removal,
)
from .sound import AUDIO_DENOISE, DEFAULT_MUSIC_VOLUME, MUSIC_VOLUME_MAX, MUSIC_VOLUME_MIN, VOLUME_MAX, VOLUME_MIN
from .stills import IMAGE_EXTENSIONS, save_frame
from .timing import FADE_MAX, SPEED_MAX, SPEED_MIN, cut_range
from .video import (
    ADJUSTMENTS, DEFAULT_FPS, LOOKS, STABILIZE, UP_TO_1080, VIDEO_QUALITY, JoinJob, JoinPart, VideoEdit, VideoJob, convert_video,
    format_warning, join_clips, output_tag, reverse_memory, reverse_warning,
)

# Short command-line names for the quality presets.
CLI_QUALITY = {
    "128": "128 kbps",
    "192": "192 kbps",
    "256": "256 kbps",
    "320": "320 kbps",
    "vbr-high": "VBR (high quality)",
    "vbr": "VBR (standard)",
}
assert set(CLI_QUALITY.values()) == set(QUALITY_PRESETS)

CLI_SHAPES = {"phone": (9, 16), "square": (1, 1), "portrait": (4, 5), "widescreen": (16, 9), "original": None}
CLI_SIZES = {"up1080": UP_TO_1080, "1080": 1080, "720": 720, "480": 480}
CLI_FPS = {"source": None, "60": 60, "30": 30}
CLI_VIDEO_QUALITY = {"high": "High", "balanced": "Balanced", "small": "Smaller file"}


def _time(text: str) -> float:
    try:
        value = parse_time(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    if value is None:
        raise argparse.ArgumentTypeError("empty time")
    return value


def _range(text: str) -> tuple[float, float]:
    start, sep, end = text.partition("-")
    if not sep:
        raise argparse.ArgumentTypeError(f"use START-END, e.g. 0:10-0:15 (got {text!r})")
    a, b = _time(start), _time(end)
    if b <= a:
        raise argparse.ArgumentTypeError(f"the end of {text!r} must be after its start")
    return a, b


def _speed(text: str) -> float:
    try:
        value = float(text.rstrip("xX×"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not SPEED_MIN <= value <= SPEED_MAX:
        raise argparse.ArgumentTypeError(f"speed must be between {SPEED_MIN:g} and {SPEED_MAX:g}")
    return value


def _seconds(text: str) -> float:
    value = _time(text)
    if value > FADE_MAX:
        raise argparse.ArgumentTypeError(f"at most {FADE_MAX:g} seconds")
    return value


def _between(name: str, low: float, high: float):
    """An argument type: a number from `low` to `high`, refused with a clear message otherwise."""
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{name} must be between {low:g} and {high:g}")
        return value
    return parse


def _whole(name: str, low: int, high: int):
    """An argument type: a whole number from `low` to `high`."""
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{name} must be between {low} and {high}")
        return value
    return parse


def _warn_gif(name: str, edit: VideoEdit, info) -> None:
    """Say so on stderr when a GIF will run long enough to get big. The export still runs."""
    warning = format_warning(edit, info)
    if warning:
        print(f"\r{name}: warning - {warning}", file=sys.stderr)


def _adjustment(name: str):
    low, high, _ = ADJUSTMENTS[name]
    return _between(name, low, high)


def _warn_reverse(name: str, edit: VideoEdit, info, sound_only: bool, fps: float | None = None) -> None:
    """Say so on stderr when playing `edit` backwards will need a lot of memory. The export still runs."""
    warning = reverse_warning(reverse_memory(edit, info, fps, sound_only))
    if warning:
        print(f"\r{name}: warning - {warning}", file=sys.stderr)


def _with_cuts(edit: VideoEdit, cuts: list[tuple[float, float]], duration: float | None) -> VideoEdit:
    """A copy of `edit` with `cuts` removed from a source `duration` seconds long."""
    edit = copy.deepcopy(edit)
    if cuts:
        if not duration:
            raise ConversionError("Can't cut this file: its length is unknown.")
        for a, b in cuts:
            edit.keep = cut_range(edit.keep, a, b, duration)
    return edit


def _without_silences(edit: VideoEdit, ffmpeg: str, source: Path, info, args, note) -> VideoEdit:
    """A copy of `edit` with the silences of `source` cut out (inside its trim, next to any cuts it has)."""
    if not info.duration:
        raise ConversionError("Can't remove silences: this file's length is unknown.")
    if not info.has_audio:
        raise ConversionError("This file has no sound, so there are no silences to remove.")
    silences = detect_silences(ffmpeg, source, args.silence_db, args.silence_min, info.duration)
    plan = plan_silence_removal(edit.keep, silences, edit.start, edit.end, info.duration, args.silence_pad)
    note(f"{source.name}: cut out {len(plan.cuts)} {'pause' if len(plan.cuts) == 1 else 'pauses'}, "
         f"{plan.removed:.1f} s in total")
    edit = copy.deepcopy(edit)
    edit.keep = plan.keep
    return edit


def run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="video_converter", description="Convert videos to MP3, or export edited MP4s.")
    parser.add_argument("files", nargs="+", type=Path, help="video files to convert")
    parser.add_argument("-o", "--output-dir", type=Path, help="folder for the results (default: next to each video)")
    parser.add_argument(
        "-q", "--quality", choices=CLI_QUALITY,
        default=next(k for k, v in CLI_QUALITY.items() if v == DEFAULT_QUALITY),
        help="MP3 bitrate in kbps, or vbr / vbr-high (default: %(default)s)",
    )
    parser.add_argument("--start", type=_time, help="trim: start time, e.g. 0:15")
    parser.add_argument("--end", type=_time, help="trim: end time, e.g. 1:02:30")
    parser.add_argument("--overwrite", action="store_true", help="replace existing files instead of renaming")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    video = parser.add_argument_group("video export (makes an MP4 instead of an MP3)")
    video.add_argument("--video", choices=CLI_SHAPES, help="output shape; phone = 9:16")
    video.add_argument("--fit", choices=("crop", "blur", "bars"), default="crop",
                       help="crop to fill, or fit inside with a blurred background / black bars (default: crop)")
    video.add_argument("--position", type=_between("--position", 0.0, 1.0), nargs=2, metavar=("X", "Y"),
                       default=(0.5, 0.5),
                       help="where to crop, 0..1 from left/top (default: 0.5 0.5 = center)")
    video.add_argument("--zoom", type=_between("--zoom", 1.0, 3.0), default=1.0,
                       help="crop tighter, 1.0 to 3.0 (default: 1.0)")
    video.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0, help="degrees clockwise")
    video.add_argument("--mirror", action="store_true", help="flip left-right")
    video.add_argument("--size", choices=CLI_SIZES,
                       help="short side in pixels, or up1080 = as source but at most 1080, never enlarged "
                            "(default: as source)")
    video.add_argument("--fps", choices=CLI_FPS, default="source",
                       help="30 or 60 = a steady frame rate that phones can play, with phone-safe H.264 and AAC "
                            "(a slower video keeps its own rate); source = leave it as it is (default: source). "
                            "--video original --fps 60 makes a copy that plays on a phone")
    video.add_argument("--video-quality", choices=CLI_VIDEO_QUALITY, default="balanced")
    video.add_argument("--format", choices=list(OUTPUT_FORMATS),
                       help="mp4 = H.264 (plays everywhere, the default), hevc = H.265 in an MP4 (smaller, but older "
                            "phones can't play it), webm = VP9 and Opus, mov = H.264 in a MOV, gif = a looping "
                            "picture with no sound. Without --video this makes a video of the original shape")
    video.add_argument("--target-size", type=_between("--target-size", *TARGET_RANGE), metavar="MB",
                       help="make the file fit in this many megabytes (10 = Discord's limit, 16 = WhatsApp, "
                            "25 = email), in two passes. A picture that can't fit is made smaller, or the "
                            "export fails with a message. Not for a GIF")
    video.add_argument("--gif-fps", type=_whole("--gif-fps", *GIF_FPS_RANGE), metavar="N",
                       help=f"with --format gif: the most frames a second (default: {GIF_FPS})")
    video.add_argument("--gif-width", type=_whole("--gif-width", *GIF_WIDTH_RANGE), metavar="PIXELS",
                       help=f"with --format gif: the widest the GIF gets (default: {GIF_WIDTH})")
    video.add_argument("--mute", action="store_true", help="remove the sound")
    video.add_argument("--gpu", choices=("auto", "off"), default="auto",
                       help="auto = encode on the graphics card when there is a working one (and fall back to "
                            "the CPU if it fails), off = always use the CPU (default: auto)")

    timing = parser.add_argument_group("cutting, speed, fades and joining (MP3 or MP4)")
    timing.add_argument("--cut", type=_range, action="append", default=[], metavar="START-END",
                        help="remove a section, e.g. 0:10-0:15; repeat to remove more")
    timing.add_argument("--speed", type=_speed, default=1.0, help="0.25 to 4, e.g. 2 = twice as fast (default: 1)")
    timing.add_argument("--reverse", action="store_true", help="play backwards")
    timing.add_argument("--fade-in", type=_seconds, default=0.0, metavar="SECONDS",
                        help="fade in from black and silence")
    timing.add_argument("--fade-out", type=_seconds, default=0.0, metavar="SECONDS",
                        help="fade out to black and silence")
    timing.add_argument("--remove-silences", action="store_true",
                        help="cut out the pauses (stretches with no sound), keeping a little silence around the speech")
    timing.add_argument("--silence-db", type=_between("--silence-db", *DB_RANGE), default=None, metavar="DB",
                        help=f"sound quieter than this is silence, {DB_RANGE[0]:g} to {DB_RANGE[1]:g} "
                             f"(default: {DEFAULT_DB:g})")
    timing.add_argument("--silence-min", type=_between("--silence-min", *MIN_RANGE), default=None, metavar="SECONDS",
                        help=f"only cut pauses at least this long, {MIN_RANGE[0]:g} to {MIN_RANGE[1]:g} "
                             f"(default: {DEFAULT_MIN:g})")
    timing.add_argument("--silence-pad", type=_between("--silence-pad", *PAD_RANGE), default=None, metavar="SECONDS",
                        help=f"silence to leave on each side of the speech, {PAD_RANGE[0]:g} to {PAD_RANGE[1]:g} "
                             f"(default: {DEFAULT_PAD:g})")
    timing.add_argument("--join", action="store_true", help="make one file of all the inputs, in order")

    sound = parser.add_argument_group("sound (MP3 or MP4)")
    sound.add_argument("--volume", type=_between("--volume", VOLUME_MIN, VOLUME_MAX), default=0.0, metavar="DB",
                       help=f"louder or quieter, {VOLUME_MIN:g} to {VOLUME_MAX:g} dB (default: 0)")
    sound.add_argument("--normalize", action="store_true",
                       help="make the loudness even, at -14 LUFS like YouTube and Spotify (takes two passes)")
    sound.add_argument("--denoise-audio", choices=[k for k in AUDIO_DENOISE if k != "off"], default="off",
                       metavar="light|strong", help="reduce background noise in the sound")
    sound.add_argument("--music", type=Path, metavar="FILE",
                       help="lay this music under the whole result (it repeats if shorter and fades out over "
                            "the last 2 seconds)")
    sound.add_argument("--music-volume", type=_between("--music-volume", MUSIC_VOLUME_MIN, MUSIC_VOLUME_MAX),
                       default=DEFAULT_MUSIC_VOLUME, metavar="DB",
                       help=f"the music's volume, {MUSIC_VOLUME_MIN:g} to {MUSIC_VOLUME_MAX:g} dB "
                            f"(default: {DEFAULT_MUSIC_VOLUME:g})")
    sound.add_argument("--duck", action="store_true", help="lower the music while the video's own sound plays")

    frame = parser.add_argument_group("save a frame (with the picture options below, instead of an export)")
    frame.add_argument("--frame-at", type=_time, metavar="TIME",
                       help="save the frame at this time of the video (e.g. 0:12) as a picture, with the edits "
                            "below applied; needs --frame-out and one file")
    frame.add_argument("--frame-out", type=Path, metavar="FILE",
                       help="where to save the frame: a .png or .jpg file")

    picture = parser.add_argument_group("picture (with --video)")
    hints = {"brightness": "0 = unchanged", "contrast": "1 = unchanged", "saturation": "1 = unchanged, 0 = grey",
             "gamma": "1 = unchanged", "sharpen": "0 = off", "denoise": "0 = off"}
    for name, (low, high, neutral) in ADJUSTMENTS.items():
        picture.add_argument(f"--{name}", type=_adjustment(name), default=neutral, metavar="N",
                             help=f"{low:g} to {high:g}; {hints[name]}")
    picture.add_argument("--look", choices=LOOKS, default="none",
                         help="color look; bw = black and white, contrast = high contrast")
    picture.add_argument("--stabilize", choices=STABILIZE, default="off", help="steady shaky footage (slower)")
    args = parser.parse_args(argv)

    if args.start is not None and args.end is not None and args.end <= args.start:
        parser.error("--end must be after --start")
    if args.join and len(args.files) < 2:
        parser.error("--join needs at least two files")
    if (args.frame_at is None) != (args.frame_out is None):
        parser.error("--frame-at and --frame-out go together")
    frame_mode = args.frame_at is not None
    if frame_mode and (len(args.files) != 1 or args.join):
        parser.error("--frame-at saves one frame of one file")
    if frame_mode and args.frame_out.suffix.lower() not in IMAGE_EXTENSIONS:
        parser.error("--frame-out must end with .png or .jpg")
    if frame_mode and (args.format or args.target_size is not None):
        parser.error("--format and --target-size make a video; --frame-at saves a picture")
    if args.format != "gif" and (args.gif_fps is not None or args.gif_width is not None):
        parser.error("--gif-fps and --gif-width need --format gif")
    if args.format == "gif" and args.target_size is not None:
        parser.error("--target-size doesn't work for a GIF; lower --gif-width or --gif-fps, or shorten it")
    # A format, a file size or a frame is about the picture: without --video it is the original shape.
    if not args.video and (args.format or args.target_size is not None or frame_mode):
        args.video = "original"
    picture_values = {name: getattr(args, name) for name in (*ADJUSTMENTS, "look", "stabilize")}
    if not args.remove_silences and any(v is not None for v in (args.silence_db, args.silence_min, args.silence_pad)):
        parser.error("--silence-db, --silence-min and --silence-pad need --remove-silences")
    args.silence_db = DEFAULT_DB if args.silence_db is None else args.silence_db
    args.silence_min = DEFAULT_MIN if args.silence_min is None else args.silence_min
    args.silence_pad = DEFAULT_PAD if args.silence_pad is None else args.silence_pad
    if not args.music and (args.duck or args.music_volume != DEFAULT_MUSIC_VOLUME):
        parser.error("--music-volume and --duck need --music")
    if not args.video and args.fps != "source":
        parser.error("--fps needs --video")
    if not args.video and picture_values != {name: getattr(VideoEdit(), name) for name in picture_values}:
        parser.error("the picture options need --video")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("error: ffmpeg not found. Install ffmpeg or run `pip install imageio-ffmpeg`.", file=sys.stderr)
        return 2

    timing = dict(start=args.start, end=args.end, speed=args.speed, reverse=args.reverse, fade_in=args.fade_in,
                  fade_out=args.fade_out, audio_fade_in=args.fade_in, audio_fade_out=args.fade_out,
                  volume=args.volume, normalize=args.normalize, audio_denoise=args.denoise_audio,
                  music=str(args.music) if args.music else "", music_volume=args.music_volume, music_duck=args.duck)
    edit = VideoEdit(**timing)
    if args.video:
        edit = VideoEdit(
            aspect=CLI_SHAPES[args.video], fit=args.fit, pos_x=args.position[0], pos_y=args.position[1],
            zoom=args.zoom, rotate=args.rotate, flip=args.mirror, resolution=CLI_SIZES.get(args.size),
            frame_rate=CLI_FPS[args.fps],
            crf=VIDEO_QUALITY[CLI_VIDEO_QUALITY[args.video_quality]], use_gpu=args.gpu == "auto",
            mute=args.mute, video_format=args.format or "mp4", target_mb=args.target_size or 0.0,
            gif_fps=args.gif_fps or GIF_FPS, gif_width=args.gif_width or GIF_WIDTH, **timing,
            **picture_values,
        )
    quality = CLI_QUALITY[args.quality]
    extension = edit.output_format.extension if args.video else ".mp3"

    def show(fraction: float, name: str) -> None:
        print(f"\r{name}: {fraction:6.1%}", end="", flush=True)

    def note(text: str) -> None:
        print(f"\r{text}.", file=sys.stderr)

    if frame_mode:
        source = args.files[0]
        try:
            if not source.is_file():
                raise ConversionError(f"File not found: {source}")
            info = probe_media(ffmpeg, source)
            if not info.has_video:
                raise ConversionError("This file has no video track.")
            if info.duration and args.frame_at > info.duration:
                raise ConversionError(f"The video is only {format_time(info.duration)} long.")
            frame_edit = _with_cuts(edit, args.cut, info.duration) if args.cut else edit
            target = args.frame_out if args.overwrite else unique_output_path(args.frame_out)
            output = save_frame(ffmpeg, source, target, args.frame_at, frame_edit, info)
        except ConversionError as exc:
            print(f"{source.name}: FAILED - {exc}", file=sys.stderr)
            return 1
        print(f"{source.name} -> {output}")
        return 0

    if args.join:
        first = args.files[0]
        target = output_path_for(first, args.output_dir, args.overwrite, extension, " (joined)")
        try:
            parts = []
            for source in args.files:
                if not source.is_file():
                    raise ConversionError(f"File not found: {source}")
                info = probe_media(ffmpeg, source)
                part_edit = _with_cuts(edit, args.cut, info.duration)
                if args.remove_silences:
                    part_edit = _without_silences(part_edit, ffmpeg, source, info, args, note)
                parts.append(JoinPart(source, part_edit, info))
            for part in parts:
                _warn_reverse(part.source.name, part.edit, part.info, not args.video, parts[0].info.fps or DEFAULT_FPS)
            job = JoinJob(parts, target, audio_only=not args.video, quality=quality)
            output = join_clips(ffmpeg, job, on_progress=lambda f: show(f, target.name),
                                encoder=choose_encoder(ffmpeg, edit.use_gpu, edit.output_format.codec), on_note=note)
        except ConversionError as exc:
            print(f"\r{target.name}: FAILED - {exc}", file=sys.stderr)
            return 1
        print(f"\r{len(parts)} files -> {output}")
        return 0

    failures = 0
    for source in args.files:
        try:
            if not source.is_file():
                raise ConversionError(f"File not found: {source}")
            gif = args.format == "gif"
            info = (probe_media(ffmpeg, source) if args.cut or args.reverse or args.remove_silences or gif
                    else None)
            file_edit = _with_cuts(edit, args.cut, info.duration) if args.cut else edit
            if args.remove_silences:
                file_edit = _without_silences(file_edit, ffmpeg, source, info, args, note)
            if args.reverse:
                _warn_reverse(source.name, file_edit, info, not args.video)
            if gif and info.has_video:
                _warn_gif(source.name, file_edit, info)
            if not args.video:
                target = output_path_for(source, args.output_dir, args.overwrite)
                job = ConversionJob(source, target, quality, args.start, args.end, file_edit.keep, args.speed,
                                    args.reverse, args.fade_in, args.fade_out, file_edit.sound)
                output = convert(ffmpeg, job, on_progress=lambda f, n=source.name: show(f, n))
            else:
                target = output_path_for(source, args.output_dir, args.overwrite, extension, f" ({output_tag(edit)})")
                output = convert_video(ffmpeg, VideoJob(source, target, file_edit),
                                       on_progress=lambda f, n=source.name: show(f, n),
                                       encoder=choose_encoder(ffmpeg, edit.use_gpu, edit.output_format.codec),
                                       on_note=note)
            print(f"\r{source.name} -> {output}")
        except ConversionError as exc:
            failures += 1
            print(f"\r{source.name}: FAILED - {exc}", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    if len(sys.argv) > 1:
        return run_cli(sys.argv[1:])
    from .ui.app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
