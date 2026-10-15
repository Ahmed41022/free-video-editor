"""Entry point.

    python -m mp4_to_mp3                      # open the desktop app
    python -m mp4_to_mp3 video.mp4 [...]      # convert from the command line
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


def run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mp4_to_mp3", description="Convert video files to MP3.")
    parser.add_argument("files", nargs="+", type=Path, help="video files to convert")
    parser.add_argument("-o", "--output-dir", type=Path, help="folder for the MP3 files (default: next to each video)")
    parser.add_argument(
        "-q", "--quality", choices=CLI_QUALITY,
        default=next(k for k, v in CLI_QUALITY.items() if v == DEFAULT_QUALITY),
        help="MP3 bitrate in kbps, or vbr / vbr-high (default: %(default)s)",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace existing MP3 files instead of renaming")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("error: ffmpeg not found. Install ffmpeg or run `pip install imageio-ffmpeg`.", file=sys.stderr)
        return 2

    failures = 0
    for source in args.files:
        job = ConversionJob(source, output_path_for(source, args.output_dir, args.overwrite), CLI_QUALITY[args.quality])

        def show(fraction: float, name: str = source.name) -> None:
            print(f"\r{name}: {fraction:6.1%}", end="", flush=True)

        try:
            output = convert(ffmpeg, job, on_progress=show)
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
