"""Reading and writing times such as 1:02:30."""

from __future__ import annotations

import math
from typing import Optional

from ..i18n import tr

def parse_time(text: str) -> Optional[float]:
    """Parse "90", "1:30", "01:02:03.5" into seconds. Empty text means no value."""
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(tr("Invalid time: {text}", text=repr(text)))
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    except ValueError:
        raise ValueError(tr("Invalid time: {text} (use seconds, m:ss or h:mm:ss)", text=repr(text))) from None
    # float() takes "nan" and "inf" (and "1e999"), which are not times and would break everything after them.
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(tr("Invalid time: {text}", text=repr(text)))
    return seconds


def format_time(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h >= 1 else f"{int(m)}:{s:04.1f}"
