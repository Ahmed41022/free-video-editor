"""Entry point.

    python -m video_converter                          # open the desktop app
    python -m video_converter video.mp4 [...]          # convert to MP3 from the command line
    python -m video_converter video.mp4 --video phone  # export an edited MP4 instead
    python -m video_converter a.mp4 b.mp4 --video original --join   # one MP4 made of both
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
    output_path_for,
    parse_time,
    probe_media,
)
from .gpu import choose_encoder
from .timing import FADE_MAX, SPEED_MAX, SPEED_MIN, cut_range
from .video import (
    ADJUSTMENTS, DEFAULT_FPS, LOOKS, STABILIZE, VIDEO_QUALITY, JoinJob, JoinPart, VideoEdit, VideoJob, convert_video,
    join_clips, output_tag, reverse_memory, reverse_warning,
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
    video.add_argument("--size", type=int, choices=(1080, 720, 480), help="short side in pixels (default: as source)")
    video.add_argument("--video-quality", choices=CLI_VIDEO_QUALITY, default="balanced")
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
    timing.add_argument("--join", action="store_true", help="make one file of all the inputs, in order")

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
    picture_values = {name: getattr(args, name) for name in (*ADJUSTMENTS, "look", "stabilize")}
    if not args.video and picture_values != {name: getattr(VideoEdit(), name) for name in picture_values}:
        parser.error("the picture options need --video")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("error: ffmpeg not found. Install ffmpeg or run `pip install imageio-ffmpeg`.", file=sys.stderr)
        return 2

    timing = dict(start=args.start, end=args.end, speed=args.speed, reverse=args.reverse, fade_in=args.fade_in,
                  fade_out=args.fade_out, audio_fade_in=args.fade_in, audio_fade_out=args.fade_out)
    edit = VideoEdit(**timing)
    if args.video:
        edit = VideoEdit(
            aspect=CLI_SHAPES[args.video], fit=args.fit, pos_x=args.position[0], pos_y=args.position[1],
            zoom=args.zoom, rotate=args.rotate, flip=args.mirror, resolution=args.size,
            crf=VIDEO_QUALITY[CLI_VIDEO_QUALITY[args.video_quality]], use_gpu=args.gpu == "auto",
            mute=args.mute, **timing,
            **picture_values,
        )
    quality = CLI_QUALITY[args.quality]

    def show(fraction: float, name: str) -> None:
        print(f"\r{name}: {fraction:6.1%}", end="", flush=True)

    def note(text: str) -> None:
        print(f"\r{text}.", file=sys.stderr)

    if args.join:
        first = args.files[0]
        suffix = ".mp4" if args.video else ".mp3"
        target = output_path_for(first, args.output_dir, args.overwrite, suffix, " (joined)")
        try:
            parts = []
            for source in args.files:
                if not source.is_file():
                    raise ConversionError(f"File not found: {source}")
                info = probe_media(ffmpeg, source)
                parts.append(JoinPart(source, _with_cuts(edit, args.cut, info.duration), info))
            for part in parts:
                _warn_reverse(part.source.name, part.edit, part.info, not args.video, parts[0].info.fps or DEFAULT_FPS)
            job = JoinJob(parts, target, audio_only=not args.video, quality=quality)
            output = join_clips(ffmpeg, job, on_progress=lambda f: show(f, target.name),
                                encoder=choose_encoder(ffmpeg, edit.use_gpu), on_note=note)
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
            info = probe_media(ffmpeg, source) if args.cut or args.reverse else None
            file_edit = _with_cuts(edit, args.cut, info.duration) if args.cut else edit
            if args.reverse:
                _warn_reverse(source.name, file_edit, info, not args.video)
            if not args.video:
                target = output_path_for(source, args.output_dir, args.overwrite)
                job = ConversionJob(source, target, quality, args.start, args.end, file_edit.keep, args.speed,
                                    args.reverse, args.fade_in, args.fade_out)
                output = convert(ffmpeg, job, on_progress=lambda f, n=source.name: show(f, n))
            else:
                target = output_path_for(source, args.output_dir, args.overwrite, ".mp4", f" ({output_tag(edit)})")
                output = convert_video(ffmpeg, VideoJob(source, target, file_edit),
                                       on_progress=lambda f, n=source.name: show(f, n),
                                       encoder=choose_encoder(ffmpeg, edit.use_gpu), on_note=note)
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
