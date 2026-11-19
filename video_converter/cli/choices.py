"""The short names the command line gives to qualities, shapes, sizes and frame rates."""

from __future__ import annotations

from ..core import QUALITY_PRESETS
from ..video import UP_TO_1080

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

CLI_SHAPES = {"phone": (9, 16), "square": (1, 1), "portrait": (4, 5), "widescreen": (16, 9), "original": None}
CLI_SIZES = {"up1080": UP_TO_1080, "1080": 1080, "720": 720, "480": 480}
CLI_FPS = {"source": None, "60": 60, "30": 30}
CLI_VIDEO_QUALITY = {"high": "High", "balanced": "Balanced", "small": "Smaller file"}
