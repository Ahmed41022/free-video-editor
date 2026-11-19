"""The labels and options the panel's sections offer."""

from __future__ import annotations

from ...i18n import N_
from ...sound import AUDIO_DENOISE

FIT_LABEL = N_("Fit in a file size")
FIT_DATA = "fit"  # the Quality box's value for it (the others are x264 quality numbers)

ADJUST_LABELS = {
    "brightness": N_("Brightness"),
    "contrast": N_("Contrast"),
    "saturation": N_("Saturation"),
    "gamma": N_("Gamma"),
    "sharpen": N_("Sharpen"),
    "denoise": N_("Denoise")
}
STABILIZE_LABELS = {"off": N_("Off"), "light": N_("Light"), "strong": N_("Strong")}
NOISE_LABELS = {"off": N_("Off"), "light": N_("Light"), "strong": N_("Strong")}
assert set(NOISE_LABELS) == set(AUDIO_DENOISE)
LOOK_CHIPS = {"bw": N_("B&W"), "contrast": N_("Contrast")}  # short names that fit the panel

SHAPES = [
    (N_("Original"), None), ("9:16", (9, 16)), ("1:1", (1, 1)), ("4:5", (4, 5)), ("16:9", (16, 9))
]
SHAPE_HINTS = {
    None: N_("Keep the video's own shape"),
    (9, 16): N_("Phone: Stories, Reels, TikTok, Shorts"),
    (1, 1): N_("Square post"),
    (4, 5): N_("Portrait post"),
    (16, 9): N_("Widescreen, YouTube")
}
