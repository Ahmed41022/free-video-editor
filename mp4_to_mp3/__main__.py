"""Entry point.

    python -m mp4_to_mp3                          # open the desktop app
    python -m mp4_to_mp3 video.mp4 [...]          # convert to MP3 from the command line
    python -m mp4_to_mp3 video.mp4 --video phone  # export an edited MP4 instead
"""

from __future__ import annotations

import argparse
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
)
from .video import VIDEO_QUALITY, VideoEdit, VideoJob, convert_video, output_tag

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


def run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mp4_to_mp3", description="Convert videos to MP3, or export edited MP4s.")
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
    video.add_argument("--position", type=float, nargs=2, metavar=("X", "Y"), default=(0.5, 0.5),
                       help="where to crop, 0..1 from left/top (default: 0.5 0.5 = center)")
    video.add_argument("--zoom", type=float, default=1.0, help="crop tighter, 1.0 to 3.0 (default: 1.0)")
    video.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0, help="degrees clockwise")
    video.add_argument("--mirror", action="store_true", help="flip left-right")
    video.add_argument("--size", type=int, choices=(1080, 720, 480), help="short side in pixels (default: as source)")
    video.add_argument("--video-quality", choices=CLI_VIDEO_QUALITY, default="balanced")
    video.add_argument("--mute", action="store_true", help="remove the sound")
    args = parser.parse_args(argv)

    if args.start is not None and args.end is not None and args.end <= args.start:
        parser.error("--end must be after --start")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("error: ffmpeg not found. Install ffmpeg or run `pip install imageio-ffmpeg`.", file=sys.stderr)
        return 2

    edit = None
    if args.video:
        edit = VideoEdit(
            aspect=CLI_SHAPES[args.video], fit=args.fit, pos_x=args.position[0], pos_y=args.position[1],
            zoom=args.zoom, rotate=args.rotate, flip=args.mirror, resolution=args.size,
            crf=VIDEO_QUALITY[CLI_VIDEO_QUALITY[args.video_quality]], mute=args.mute,
            start=args.start, end=args.end,
        )

    failures = 0
    for source in args.files:
        def show(fraction: float, name: str = source.name) -> None:
            print(f"\r{name}: {fraction:6.1%}", end="", flush=True)

        try:
            if edit is None:
                target = output_path_for(source, args.output_dir, args.overwrite)
                job = ConversionJob(source, target, CLI_QUALITY[args.quality], args.start, args.end)
                output = convert(ffmpeg, job, on_progress=show)
            else:
                target = output_path_for(source, args.output_dir, args.overwrite, ".mp4", f" ({output_tag(edit)})")
                output = convert_video(ffmpeg, VideoJob(source, target, edit), on_progress=show)
            print(f"\r{source.name} -> {output}")
        except ConversionError as exc:
            failures += 1
            print(f"\r{source.name}: FAILED - {exc}", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    if len(sys.argv) > 1:
        return run_cli(sys.argv[1:])
    from .gui import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
