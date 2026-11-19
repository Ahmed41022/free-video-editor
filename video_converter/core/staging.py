"""Writing the result: a part file, replaced when it is done."""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from ..i18n import tr
from .errors import ConversionError
from .paths import check_not_input, remove_file, unique_output_path

log = logging.getLogger(__name__)


@dataclass
class StagedOutput:
    part: Path  # where ffmpeg writes: next to `final`, as "name.part.mp4"
    final: Path  # the name the finished file gets


def part_path(final: Path) -> Path:
    """"name.part.mp4" for "name.mp4": the real extension stays last, so ffmpeg still knows the
    format. The name is made now (an empty file), so that a second export into the same folder
    can't pick it too."""
    n = 1
    while True:
        tag = ".part" if n == 1 else f".part{n}"
        candidate = final.with_name(f"{final.stem}{tag}{final.suffix}")
        try:
            with open(candidate, "xb"):
                return candidate
        except FileExistsError:
            n += 1
        except OSError as exc:
            raise ConversionError(
                tr("Can't save in {folder}: {error}", folder=final.parent, error=exc)) from exc


def _replace(part: Path, final: Path) -> Path:
    """Move the finished `part` onto `final`. A final name that stays in use (an open player) gets
    a number."""
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.replace(part, final)
            return final
        except PermissionError:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    other = unique_output_path(final)
    log.warning("%s is in use; saving as %s instead", final, other)
    os.replace(part, other)
    return other


@contextmanager
def staged_output(final: Path, inputs: Iterable[Path] = (),
                  overwrite: bool = True) -> Iterator[StagedOutput]:
    """Write an export under a temporary name and give it its real one only when it is complete.

    The name `final` is taken at once (an empty file made exclusively, so no other program picks it
    too); with `overwrite` a file that is already there stays until it is replaced. ffmpeg writes
    `.part`, which is renamed over `final` when the block ends well and deleted when it ends any
    other way (an error, or a cancel), so a half-written file never has the real name. `final` is
    never one of the `inputs`. The yielded StagedOutput's `final` is the name in the end (another
    one if this was taken meanwhile)."""
    final = Path(final)
    inputs = [Path(p) for p in inputs]
    check_not_input(final, inputs)
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConversionError(
            tr("Can't save in {folder}: {error}", folder=final.parent, error=exc)) from exc
    reserved = False
    while True:
        try:
            with open(final, "xb"):
                reserved = True
            break
        except FileExistsError:
            if overwrite:
                break
            final = unique_output_path(
                final, inputs)  # someone else took the name since it was chosen
        except OSError as exc:
            raise ConversionError(
                tr("Can't save in {folder}: {error}", folder=final.parent, error=exc)) from exc
    part = None
    try:
        part = part_path(final)
        staged = StagedOutput(part, final)
        yield staged
        staged.final = _replace(part, final)
    except BaseException:
        if part is not None:
            remove_file(part)
        if reserved:
            try:
                if final.stat().st_size == 0:
                    remove_file(final)
            except OSError:
                pass
        raise
