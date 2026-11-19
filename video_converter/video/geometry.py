"""The size and shape of the picture: crop, fit, rotation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .edit import SHORT_SIDE_CAP, VideoEdit


@dataclass
class Geometry:
    width: int  # size of the source after rotation
    height: int
    out_width: int  # size of the exported video
    out_height: int
    crop: Optional[tuple[int, int, int, int]] = None  # w, h, x, y in rotated source pixels


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def _even_floor(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def oriented_size(width: int, height: int, rotate: int) -> tuple[int, int]:
    return (height, width) if rotate in (90, 270) else (width, height)


def plan_geometry(width: int, height: int, edit: VideoEdit) -> Geometry:
    """Work out the output size and crop box for a `width` x `height` source.

    The output's short side is `edit.resolution` when that is set (a negative one, UP_TO_1080, is a
    limit: smaller pictures keep their size). Otherwise it is the source's
    short side, so a 1080p video makes a standard 1080x1920 phone video, and phone, portrait and
    square videos stop at SHORT_SIDE_CAP, so a 4K source doesn't turn into a huge 2160x3840 file."""
    w, h = oriented_size(width, height, edit.rotate)
    aw, ah = edit.aspect or (w, h)
    ratio = aw / ah

    crop = None
    if edit.crops:
        if w / h > ratio:
            crop_w, crop_h = h * ratio, h
        else:
            crop_w, crop_h = w, w / ratio
        zoom = min(max(edit.zoom, 1.0), 10.0)
        crop_w = min(_even_floor(crop_w / zoom), _even_floor(w))
        crop_h = min(_even_floor(crop_h / zoom), _even_floor(h))
        x = round((w - crop_w) * min(max(edit.pos_x, 0.0), 1.0))
        y = round((h - crop_h) * min(max(edit.pos_y, 0.0), 1.0))
        crop = (crop_w, crop_h, x, y)

    resolution = edit.resolution
    if resolution and resolution < 0:
        # "Up to": the source's own size, unless its short side is above the limit.
        resolution = -resolution if min(w, h) > -resolution else None
    if not resolution and edit.aspect is None:
        # The original shape at "same as source" is exactly the source size (made even).
        return Geometry(w, h, _even_floor(w), _even_floor(h), crop)
    if resolution:
        short = resolution
    else:
        short = min(w, h)
        if edit.aspect and ratio <= 1:
            short = min(short, SHORT_SIDE_CAP)
    short = _even(short)
    # The other side follows from the exact ratio (360 -> 640 for 9:16), not from an odd source size.
    if ratio < 1:
        out_w, out_h = short, _even(short * ah / aw)
    else:
        out_w, out_h = _even(short * aw / ah), short
    return Geometry(w, h, out_w, out_h, crop)
