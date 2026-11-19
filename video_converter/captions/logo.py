"""A logo on the picture."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import uuid
from pathlib import Path

from ..core import ConversionError
from ..i18n import tr
from .escaping import _n, _path_value
from .model import Logo
from .scratch import _publish, scratch_dir


def logo_source(logo: Logo) -> Path:
    """A copy of the logo's image under a plain name (so odd characters in its path can't matter)."""
    path = Path(logo.file)
    if not path.is_file():
        raise ConversionError(
            tr("The logo picture can't be found: {name}", name=path.name or logo.file))
    stat = path.stat()
    key = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    target = scratch_dir() / (
        hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + (path.suffix.lower() or ".png"))
    if not target.is_file():
        temp = target.with_name(
            f"{target.name}.{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}.part")
        try:
            shutil.copyfile(path, temp)
            _publish(temp, target, lambda t: t.is_file() and t.stat().st_size == stat.st_size)
        except OSError as exc:
            raise ConversionError(
                tr("The logo picture can't be read: {name}", name=path.name)) from exc
        finally:
            temp.unlink(missing_ok=True)
    return target


def logo_position(logo: Logo, frame: tuple[int, int]) -> tuple[str, str]:
    """The overlay's x and y expressions (W, H = the video, w, h = the logo)."""
    margin = int(round(logo.margin * frame[0]))
    if logo.corner == "custom":
        return (f"max(0,min(W-w,W*{_n(logo.x)}-w/2))", f"max(0,min(H-h,H*{_n(logo.y)}-h/2))")
    vertical, _, horizontal = logo.corner.partition("-")
    if logo.corner == "center":
        return "(W-w)/2", "(H-h)/2"
    x = str(margin) if horizontal == "left" else f"W-w-{margin}"
    y = str(margin) if vertical == "top" else f"H-h-{margin}"
    return x, y


def logo_graph(src: str, out: str, logo: Logo, frame: tuple[int, int], tag: str) -> list[str]:
    """The filters that lay the logo over `src`: its picture is read from the file, scaled and made
    see-through by itself, then overlaid."""
    width = max(int(round(logo.size * frame[0])), 4)
    opacity = f",colorchannelmixer=aa={_n(logo.opacity, 3)}" if logo.opacity < 1 else ""
    x, y = logo_position(logo, frame)
    source = logo_source(logo)
    # A "%" in the path (a user name or a temp folder can have one) would be read as an image2 file-number
    # pattern, so say it is a plain file. (The logo picker offers still pictures only: a .gif or a video chosen
    # another way would show its first play and then freeze; looping it made ffmpeg run forever.)
    options = "" if source.suffix.lower(
    ) == ".gif" else ":format_opts=pattern_type\\=none"  # gif has its own demuxer
    return [
        f"movie=filename={_path_value(source)}{options},scale={width}:-1:flags=lanczos,format=rgba{opacity}[logo{tag}]",
        f"{src}[logo{tag}]overlay=x='{x}':y='{y}'{out}",
    ]
