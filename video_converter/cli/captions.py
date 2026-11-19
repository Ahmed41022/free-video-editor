"""Caption, text and logo options, and making an .srt file from the speech."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from ..autocaptions import LANGUAGES, CaptionOptions, make_captions, save_captions
from ..captions import LOGO_CORNERS, Logo, Subtitles, TextItem
from ..core import ConversionError
from ..speech_models import MODELS, is_downloaded

PHASE_NAMES = {
    "prepare": "getting the sound ready",
    "download": "downloading the model",
    "load": "checking the model",
    "listen": "listening"
}

COLOR_NAMES = {
    "white": "#ffffff",
    "black": "#000000",
    "yellow": "#ffe600",
    "red": "#ff2a2a",
    "green": "#2ecc40",
    "blue": "#2a7fff",
    "orange": "#ff9800",
    "pink": "#ff6ec7",
    "purple": "#a259ff",
    "gray": "#808080"
}


def _color(text: str) -> str:
    name = text.strip().lower()
    if name in COLOR_NAMES:
        return COLOR_NAMES[name]
    if re.fullmatch(r"#?[0-9a-f]{6}", name):
        return "#" + name.lstrip("#")
    raise argparse.ArgumentTypeError(
        f"not a color: {text!r} (use #RRGGBB or a name like white or yellow)")


def _position(text: str) -> tuple[float, float]:
    parts = text.split(",")
    try:
        x, y = (float(p) for p in parts) if len(parts) == 2 else (None, None)
    except ValueError:
        x = None
    if x is None or not (0 <= x <= 1 and 0 <= y <= 1):
        raise argparse.ArgumentTypeError(
            f"use X,Y with both between 0 and 1, e.g. 0.5,0.85 (got {text!r})")
    return x, y


def _logo_position(text: str):
    """A corner name, or "X,Y" (a tuple)."""
    name = text.strip().lower()
    if name in LOGO_CORNERS and name != "custom":
        return name
    return _position(text)


def _caption_edit(args, parser: argparse.ArgumentParser) -> dict:
    """The text, logo and subtitles options as VideoEdit fields (checked against the other options)."""
    used = [
        name for name, on in (
            ("--text", args.text), ("--logo", args.logo), ("--subtitles", args.subtitles)) if on
    ]
    others = [
        flag for flag, value, default in (
            ("--text-start", args.text_start, 0.0), ("--text-end", args.text_end, None),
            ("--text-pos", args.text_pos, None), ("--text-size", args.text_size, TextItem().size),
            ("--text-color", args.text_color,
             TextItem().color), ("--text-font", args.text_font, TextItem().font))
        if value != default
    ]
    if others and not args.text:
        parser.error(f"{', '.join(others)} need --text")
    for flag, on in (("--logo-pos", args.logo_pos
                      != "bottom-right"), ("--logo-size", args.logo_size
                                           != Logo().size), ("--logo-opacity", args.logo_opacity
                                                             != Logo().opacity)):
        if on and not args.logo:
            parser.error(f"{flag} needs --logo")
    if not args.subtitles and not args.auto_captions and (
            args.subtitle_size != Subtitles().size or args.subtitle_position
            != Subtitles().position or args.subtitle_color != Subtitles().color):
        parser.error("the subtitle options need --subtitles (or --auto-captions)")
    if used and not args.video:
        parser.error(f"{', '.join(used)} need --video")
    if args.subtitles and args.join:
        parser.error(
            "--subtitles can't be used with --join: a subtitle file is made for one video")
    if args.text_end is not None and args.text_end <= args.text_start:
        parser.error("--text-end must be after --text-start")
    values: dict = {}
    if args.text:
        x, y = args.text_pos or (TextItem().x, TextItem().y)
        values["texts"] = [
            TextItem(
                text=args.text.replace("\\n", "\n"), font=args.text_font, size=args.text_size,
                color=args.text_color, x=x, y=y, start=args.text_start, end=args.text_end)
        ]
    if args.logo:
        corner = args.logo_pos
        spot = dict(corner=corner) if isinstance(corner, str) else dict(
            corner="custom", x=corner[0], y=corner[1])
        values["logo"] = Logo(
            file=str(args.logo), size=args.logo_size, opacity=args.logo_opacity, **spot)
    if args.subtitles:
        values["subtitles"] = Subtitles(
            file=str(args.subtitles), size=args.subtitle_size, color=args.subtitle_color,
            position=args.subtitle_position)
    return values


def _language(text: str) -> str:
    code = text.strip().lower()
    if code in ("auto", ""):
        return ""
    if code in LANGUAGES:
        return code
    raise argparse.ArgumentTypeError(
        f"not a language code: {text!r} (use auto, or one of {', '.join(k for k in LANGUAGES if k)})"
    )


def _make_srt(ffmpeg: str, source: Path, info, args, note) -> Path | None:
    """Listen to `source` (inside the --start/--end trim) and save its captions as an .srt, using the
    --caption-* options. Returns the file, or None when no speech was found."""
    if not info.has_audio:
        raise ConversionError("This file has no sound, so there is nothing to make captions from.")
    model = MODELS[args.caption_model]
    if not is_downloaded(model):
        note(
            f"{source.name}: downloading the {model.label} speech model ({model.size_text}), once; after this "
            f"captions work offline")
    last = [""]

    def progress(phase: str, fraction: float, text: str) -> None:
        shown = f"{source.name}: {PHASE_NAMES[phase]} {fraction:6.1%}"
        if shown != last[0]:
            last[0] = shown
            print(f"\r{shown}", end="", flush=True)

    result = make_captions(
        ffmpeg, source,
        CaptionOptions(
            args.caption_model, args.caption_language, args.translate_captions, args.start,
            args.end), info.duration, on_progress=progress)
    print()
    if not result.cues:
        return None
    return save_captions(result.cues, source, args.output_dir, args.overwrite)
