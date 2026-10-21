"""Decoding settings for the preview player.

The preview turns every frame into a QImage on the CPU, so a graphics card decoding the video gains nothing,
and Qt's hardware decoding has crashed the app natively (an access violation while a frame was converted,
and COM errors from Qt's Windows backend). Software decoding is also what the tests use, so nothing here
ever touches the graphics card."""

from __future__ import annotations

import os

# Qt reads the list of hardware device types it may decode with from this variable, once, when the first
# player is made. A name Qt doesn't know means "none" (it only logs a warning), and an empty value would be
# dropped by Windows, so a word is used.
HW_DEVICES_VARIABLE = "QT_FFMPEG_DECODING_HW_DEVICE_TYPES"
NO_HARDWARE = "none"


def force_software_decoding() -> None:
    """Make the preview player decode on the CPU. Call it before the QApplication (or the first player) exists."""
    os.environ[HW_DEVICES_VARIABLE] = NO_HARDWARE
    os.environ["QT_DISABLE_HW_TEXTURES_CONVERSION"] = "1"
