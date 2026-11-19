"""Shared ffmpeg plumbing: locate ffmpeg, probe files, run conversions, extract MP3 audio."""

from __future__ import annotations

from .audio import (
    DEFAULT_QUALITY, QUALITY_PRESETS, ConversionJob, convert, measure_loudness, mp3_command,
)
from .checks import check_music, check_trim, music_length_error, no_segments_error
from .commands import (
    GRAPH_FILE_MIN, SEEK_SLACK, graph_files, has_long_graph, trim_args, with_graph_files,
)
from .errors import ConversionCancelled, ConversionError
from .ffmpeg import find_ffmpeg
from .paths import (
    AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, check_not_input, input_files, is_media_file,
    is_partial_export, is_video_file, output_path_for, remove_file, remove_folder, same_file,
    unique_output_path, work_folder,
)
from .probe import (
    STANDARD_RATES, VARIABLE_RATIO, MediaInfo, parse_media_info, probe_duration, probe_media,
)
from .processes import start_process, stop_process
from .progress import LOUDNESS_SHARE, LOUDNESS_SHARE_AUDIO, never_back, scaled
from .runner import run_ffmpeg
from .staging import StagedOutput, part_path, staged_output
from .timecode import format_time, parse_time
from .winproc import ProcessJob

__all__ = [
    "AUDIO_EXTENSIONS",
    "DEFAULT_QUALITY",
    "GRAPH_FILE_MIN",
    "LOUDNESS_SHARE",
    "LOUDNESS_SHARE_AUDIO",
    "QUALITY_PRESETS",
    "SEEK_SLACK",
    "STANDARD_RATES",
    "VARIABLE_RATIO",
    "VIDEO_EXTENSIONS",
    "ConversionCancelled",
    "ConversionError",
    "ConversionJob",
    "MediaInfo",
    "ProcessJob",
    "StagedOutput",
    "check_music",
    "check_not_input",
    "check_trim",
    "convert",
    "find_ffmpeg",
    "format_time",
    "graph_files",
    "has_long_graph",
    "input_files",
    "is_media_file",
    "is_partial_export",
    "is_video_file",
    "measure_loudness",
    "mp3_command",
    "music_length_error",
    "never_back",
    "no_segments_error",
    "output_path_for",
    "parse_media_info",
    "parse_time",
    "part_path",
    "probe_duration",
    "probe_media",
    "remove_file",
    "remove_folder",
    "run_ffmpeg",
    "same_file",
    "scaled",
    "staged_output",
    "start_process",
    "stop_process",
    "trim_args",
    "unique_output_path",
    "with_graph_files",
    "work_folder",
]
