"""Saving one frame of an edited video as a picture."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .core import _NO_WINDOW, ConversionError, MediaInfo
from .i18n import tr
from .video import VideoEdit, plan_geometry, render_frame

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
JPEG_QUALITY = "2"        # ffmpeg's scale runs from 2 (best) to 31


def save_frame(ffmpeg: str, source: Path, output: Path, at: float, edit: VideoEdit, info: MediaInfo) -> Path:
    """Save the frame at source time `at` as a PNG or JPG (by `output`'s extension), reframed, adjusted,
    faded and stamped like the export, at the size the export has: not the preview's smaller one.
    Stabilization is left out, as in the preview, since it needs the whole clip."""
    extension = output.suffix.lower()
    if extension not in IMAGE_EXTENSIONS:
        raise ConversionError(tr("A frame can be saved as a PNG or a JPG: end the file name with .png or .jpg."))
    if not info.has_video:
        raise ConversionError(tr("This file has no video track."))
    geometry = plan_geometry(info.width, info.height, edit)
    size = (geometry.out_width, geometry.out_height)
    output.parent.mkdir(parents=True, exist_ok=True)
    if extension == ".png":
        return render_frame(ffmpeg, source, output, at, size, edit, info)
    with tempfile.TemporaryDirectory(prefix="fve-frame-") as folder:
        png = Path(folder) / "frame.png"
        render_frame(ffmpeg, source, png, at, size, edit, info)
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(png),
               "-frames:v", "1", "-update", "1", "-q:v", JPEG_QUALITY, str(output)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=60)
    if result.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        raise ConversionError(result.stderr.strip() or tr("Couldn't save the frame as a JPG."))
    return output
