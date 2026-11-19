"""Video editing: reframe for phones and other shapes, crop, rotate, cut, change speed, fade,
adjust the picture, stabilize, join clips, and export MP4 (or H.265, WebM, MOV or GIF), optionally
fitted into a file size."""

from __future__ import annotations

from .commands import (
    BIG_PICTURE, DECODER_THREADS, ENCODER_THREADS, FILTER_THREADS, GIF_PALETTE, LEAN_LOOKAHEAD,
    PASSLOG, VideoJob, audio_codec_args, big_picture, detect_command, gif_command, input_args,
    measure_command, music_path, video_codec_args, video_command,
)
from .edit import (
    ADJUSTMENTS, DEFAULT_FORMAT, DEFAULT_VIDEO_QUALITY, FIT_MODES, FORMATS, FORMAT_TAGS,
    FRAME_RATES, LOOKS, RESOLUTIONS, RGB_LOOKS, ROTATIONS, SHORT_SIDE_CAP, STABILIZE, UP_TO_1080,
    VIDEO_QUALITY, VideoEdit, output_tag,
)
from .export import DETECT_SHARE, PREPASS_SHARE, TRY_SPANS, convert_video
from .filters import (
    ROTATE_FILTERS, ClipGraph, build_filter_graph, clip_graph, orientation_filters,
    picture_filters, rotation_args,
)
from .fit import edit_fit, fit_summary, format_warning, plan_fit, wants_loudness_pass
from .geometry import Geometry, oriented_size, plan_geometry
from .join import JOIN_AUDIO, JoinJob, JoinPart, join_clips, join_command
from .memory import (
    DEFAULT_FPS, REVERSE_BASE_BYTES, REVERSE_WARN_BYTES, format_memory, reverse_memory,
    reverse_warning,
)
from .preview import render_frame, render_source_frames
from .rates import (
    FAST_FRAME_RATE, SLOW_SOURCE, UNKNOWN_RATE_PEAK, edit_rate, encoder_level, frame_rate_hint,
    h264_level, h265_level, native_rate, output_rate, source_rate, steady_rate,
)

__all__ = [
    "ADJUSTMENTS", "BIG_PICTURE", "DECODER_THREADS", "DEFAULT_FORMAT", "DEFAULT_FPS",
    "DEFAULT_VIDEO_QUALITY", "DETECT_SHARE", "ENCODER_THREADS", "FAST_FRAME_RATE",
    "FILTER_THREADS", "FIT_MODES", "FORMATS", "FORMAT_TAGS", "FRAME_RATES", "GIF_PALETTE",
    "JOIN_AUDIO", "LEAN_LOOKAHEAD", "LOOKS", "PASSLOG", "PREPASS_SHARE", "RESOLUTIONS",
    "REVERSE_BASE_BYTES", "REVERSE_WARN_BYTES", "RGB_LOOKS", "ROTATE_FILTERS", "ROTATIONS",
    "SHORT_SIDE_CAP", "SLOW_SOURCE", "STABILIZE", "TRY_SPANS", "UNKNOWN_RATE_PEAK", "UP_TO_1080",
    "VIDEO_QUALITY", "ClipGraph", "Geometry", "JoinJob", "JoinPart", "VideoEdit", "VideoJob",
    "audio_codec_args", "big_picture", "build_filter_graph", "clip_graph", "convert_video",
    "detect_command", "edit_fit", "edit_rate", "encoder_level", "fit_summary", "format_memory",
    "format_warning", "frame_rate_hint", "gif_command", "h264_level", "h265_level", "input_args",
    "join_clips", "join_command", "measure_command", "music_path", "native_rate",
    "orientation_filters", "oriented_size", "output_rate", "output_tag", "picture_filters",
    "plan_fit", "plan_geometry", "render_frame", "render_source_frames", "reverse_memory",
    "reverse_warning", "rotation_args", "source_rate", "steady_rate", "video_codec_args",
    "video_command", "wants_loudness_pass",
]
