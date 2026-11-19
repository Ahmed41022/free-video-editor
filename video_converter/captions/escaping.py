"""Filter option values: escaping, numbers and colors."""

from __future__ import annotations

from pathlib import Path

def filter_value(text: str) -> str:
    """`text` as the value of a filter option, safe against every character ffmpeg treats specially. It
    goes through the two levels of escaping the filter syntax has: the option's and the graph's."""
    for chars in ("\\':", "\\',;[]"):
        text = "".join("\\" + ch if ch in chars else ch for ch in text)
    return text


def _path_value(path: Path) -> str:
    return filter_value(str(path).replace("\\", "/"))


def _n(value: float, places: int = 4) -> str:
    return f"{value:.{places}f}".rstrip("0").rstrip(".") or "0"


def _hex(color: str) -> str:
    return "0x" + color.lstrip("#").upper()
