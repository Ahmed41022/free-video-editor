"""A local video editor and converter: reshape, crop, cut, join, speed up, color and stabilize videos, or turn them into MP3s, with ffmpeg."""

import logging

__version__ = "2.4.0"
APP_NAME = "Free Video Editor"
REPO_URL = "https://github.com/Ahmed41022/free-video-editor"

# The app's own log file is set up by the desktop app (see diagnostics.py); until then nothing is printed.
logging.getLogger(__name__).addHandler(logging.NullHandler())
