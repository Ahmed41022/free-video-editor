"""Finding the ffmpeg program."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

def find_ffmpeg() -> Optional[str]:
    """Return a path to an ffmpeg executable, or None if none can be found.

    Checks the FFMPEG_PATH environment variable, then the copy bundled into the
    .exe build, then PATH, then the optional imageio-ffmpeg package (which
    ships its own binary).
    """
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path and Path(env_path).is_file():
        return env_path

    bundle_dir = getattr(sys, "_MEIPASS", None)  # set by PyInstaller
    if bundle_dir:
        bundled = Path(bundle_dir) / "ffmpeg" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        if bundled.is_file():
            return str(bundled)

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
