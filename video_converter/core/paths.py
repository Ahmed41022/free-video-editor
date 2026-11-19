"""File names and folders: which files are media, free output names, removing files and folders."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ..i18n import tr
from .errors import ConversionError

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = (
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp",
    ".ts",
)
# Sound-only files the editor can also open (to trim or cut them, or save them as MP3).
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus")


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


_PART_STEM_RE = re.compile(r"\.part\d*$", re.IGNORECASE)


def is_partial_export(path: Path) -> bool:
    """Whether `path` is an export still being written ("name.part.mp4", see `staged_output`)."""
    return bool(_PART_STEM_RE.search(path.stem))


def is_media_file(path: Path) -> bool:
    """A video or an audio file, by its extension. An export that is still being written is not one yet."""
    return path.suffix.lower(
    ) in VIDEO_EXTENSIONS + AUDIO_EXTENSIONS and not is_partial_export(path)


def _path_key(path: Path) -> str:
    """A file's identity for comparing names: absolute, with any symbolic links and the case of the
    drive's file names (Windows ignores it) taken out."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    return os.path.normcase(str(resolved))


def same_file(a: Path, b: Path) -> bool:
    """Whether `a` and `b` name the same file (either of them may not exist yet)."""
    if _path_key(a) == _path_key(b):
        return True
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def unique_output_path(path: Path, avoid: Iterable[Path] = ()) -> Path:
    """Return `path`, or `name (1).mp3`, `name (2).mp3`, ... if it already exists or is one of `avoid`
    (the files of the same run, whose names are taken even though nothing has been written there yet)."""
    taken = {_path_key(p) for p in avoid}
    if not path.exists() and _path_key(path) not in taken:
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists() and _path_key(candidate) not in taken:
            return candidate
        n += 1


def output_path_for(
        source: Path, output_dir: Optional[Path] = None, overwrite: bool = False,
        suffix: str = ".mp3", name_tag: str = "", avoid: Iterable[Path] = (),
) -> Path:
    """Where to save the result for `source`. `name_tag` is appended to the file name,
    e.g. " (edited)", so an exported MP4 never replaces the original video.

    The result is never `source` itself, nor any of `avoid` (the other inputs of the run, and the outputs
    already chosen in it): a name that would land on one of them gets a number instead, even with
    `overwrite`, which only replaces files that are neither."""
    target = (output_dir or source.parent) / f"{source.stem}{name_tag}{suffix}"
    protected = [source, *avoid]
    if overwrite and not any(same_file(target, p) for p in protected):
        return target
    return unique_output_path(target, protected)


def input_files(cmd: list[str], cwd: Optional[Path] = None) -> list[Path]:
    """The files an ffmpeg command reads (its -i options)."""
    base = Path(cwd) if cwd else Path.cwd()
    return [
        base / cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "-i" and cmd[i + 1] != "-"
    ]


def remove_file(path: Path, seconds: float = 2.0) -> None:
    """Delete `path` if it exists. Windows refuses while ffmpeg, a virus scanner or the indexer still
    has the file open, so keep trying for a moment; a file that stays locked is left behind."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                log.warning("Couldn't delete %s: it is still in use", path)
                return
            time.sleep(0.05)


def remove_folder(folder: Path, seconds: float = 2.0) -> None:
    """Delete `folder` and everything in it, trying again for a moment while a file in it is still in use (a
    stopped ffmpeg can hold its files a little after it has ended). Whatever stays is left for the start-up
    sweep of old work folders (tempclean.py)."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            shutil.rmtree(folder)
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() >= deadline:
                log.warning(
                    "Couldn't delete the work folder %s: something in it is still in use", folder)
                return
            time.sleep(0.05)


@contextmanager
def work_folder(prefix: str) -> Iterator[str]:
    """A new temporary folder (its path, like `tempfile.TemporaryDirectory`) that is removed afterwards with
    `remove_folder`, so a file that is still in use never turns a finished or cancelled export into an error."""
    folder = tempfile.mkdtemp(prefix=prefix)
    try:
        yield folder
    finally:
        remove_folder(Path(folder))


def check_not_input(output: Path, inputs: Iterable[Path]) -> None:
    """Refuse an `output` that is one of the files being read: ffmpeg would write over its own input, and a
    failed or cancelled export would delete it."""
    for source in inputs:
        if same_file(output, source):
            raise ConversionError(
                tr(
                    "The result can't be saved over {name}, a file it is made from. "
                    "Choose another name or folder.", name=Path(source).name))
