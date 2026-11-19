"""The settings panel on the right: collapsible sections that edit one file's ClipSettings."""

from __future__ import annotations

from .choices import (
    ADJUST_LABELS, FIT_DATA, FIT_LABEL, LOOK_CHIPS, NOISE_LABELS, SHAPES, SHAPE_HINTS,
    STABILIZE_LABELS,
)
from .panel import Inspector
from .widgets import Section, WheelGuard

__all__ = [
    "ADJUST_LABELS",
    "FIT_DATA",
    "FIT_LABEL",
    "LOOK_CHIPS",
    "NOISE_LABELS",
    "SHAPES",
    "SHAPE_HINTS",
    "STABILIZE_LABELS",
    "Inspector",
    "Section",
    "WheelGuard",
]
