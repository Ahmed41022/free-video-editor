"""Entry point.

    python -m video_converter                          # open the desktop app
    python -m video_converter video.mp4 [...]          # convert to MP3 from the command line
    python -m video_converter video.mp4 --video phone  # export an edited MP4 instead
    python -m video_converter a.mp4 b.mp4 --video original --join   # one MP4 made of both
    python -m video_converter clip.mp4 --video original --fps 60    # a copy that plays on a phone
    python -m video_converter talk.mp4 --remove-silences            # an MP3 without the pauses
    python -m video_converter talk.mp4 --normalize --music song.mp3 --duck    # even loudness,
    music under it python -m video_converter clip.mp4 --video original --text "Hello" --text-end 3
    --logo logo.png   # title, watermark python -m video_converter clip.mp4 --video original
    --subtitles clip.srt                         # burned-in subtitles python -m video_converter
    talk.mp4 --video original --auto-captions       # listen, write talk.srt, burn the captions in
    python -m video_converter talk.mp4 --captions-only --caption-language ar  # just write talk.srt
    (Arabic) python -m video_converter clip.mp4 --video original --target-size 10      # a copy
    that fits Discord's 10 MB python -m video_converter clip.mp4 --format gif --end 0:05
    # a looping GIF of the first 5 seconds python -m video_converter clip.mp4 --frame-at 0:12
    --frame-out frame.png  # save one frame as a picture
"""

from __future__ import annotations

from .run import main, run_cli

__all__ = ["main", "run_cli", ]
