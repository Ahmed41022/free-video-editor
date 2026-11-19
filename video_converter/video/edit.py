"""What an edit asks for (VideoEdit) and the choices offered for it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..captions import Logo, Subtitles, TextItem
from ..i18n import N_
from ..output import DEFAULT_VIDEO_FORMAT, GIF_FPS, GIF_WIDTH, OUTPUT_FORMATS
from ..sound import DEFAULT_MUSIC_VOLUME, Sound
from ..timing import Range, Segment, kept_segments, snap_segments

# Label -> (width, height) aspect ratio; None keeps the video's own shape.
FORMATS = {
    "Original shape": None,
    "Phone: Stories, Reels, TikTok, Shorts (9:16)": (9, 16),
    "Square post (1:1)": (1, 1),
    "Portrait post (4:5)": (4, 5),
    "Widescreen, YouTube (16:9)": (16, 9),
}
DEFAULT_FORMAT = "Phone: Stories, Reels, TikTok, Shorts (9:16)"

# Short name used in the output file name, e.g. "clip (phone).mp4".
FORMAT_TAGS = {
    None: "edited",
    (9, 16): "phone",
    (1, 1): "square",
    (4, 5): "portrait",
    (16, 9): "widescreen"
}

FIT_MODES = {
    "crop": N_("Crop to fill the frame"),
    "blur": N_("Fit inside, blurred background"),
    "bars": N_("Fit inside, black bars"),
}

ROTATIONS = {"None": 0, "90Â° clockwise": 90, "180Â°": 180, "90Â° counter-clockwise": 270}

# "Up to 1080p" is stored as minus 1080: a size that is a limit, not a target.
UP_TO_1080 = -1080

# Length of the short side of the output. None = as large as the picture being exported, never
# enlarged: see plan_geometry. UP_TO_1080 = the same, but never above 1080.
RESOLUTIONS = {
    N_("Same as source"): None,
    N_("Up to 1080p"): UP_TO_1080,
    "1080p": 1080,
    "720p": 720,
    "480p": 480
}

# Steady frame rates the user can ask for; None keeps the source's own (which may vary).
FRAME_RATES = {N_("Same as source"): None, N_("Steady 60 fps"): 60, N_("Steady 30 fps"): 30}

# With "Same as source", phone, portrait and square videos are not made bigger than this (short
# side).
SHORT_SIDE_CAP = 1080

# x264 CRF: lower = better quality and bigger files.
VIDEO_QUALITY = {N_("High"): 18, N_("Balanced"): 23, N_("Smaller file"): 28}
DEFAULT_VIDEO_QUALITY = "Balanced"

# Picture adjustments: field -> (lowest, highest, neutral value).
ADJUSTMENTS = {
    "brightness": (-0.5, 0.5, 0.0),  # eq brightness
    "contrast": (0.5, 2.0, 1.0),  # eq contrast
    "saturation": (0.0, 3.0, 1.0),  # eq saturation; 0 = grey
    "gamma": (0.5, 2.0, 1.0),  # eq gamma
    "sharpen": (0.0, 1.5, 0.0),  # unsharp luma amount
    "denoise": (0.0, 10.0, 0.0),  # hqdn3d strength
}

# Color looks: key -> (label, ffmpeg filters).
LOOKS = {
    "none": (N_("None"), []),
    "bw": (N_("Black and white"), ["hue=s=0"]),
    "warm": (N_("Warm"), ["colorbalance=rs=0.08:gs=0.02:bs=-0.08:rm=0.06:bm=-0.06"]),
    "cool": (N_("Cool"), ["colorbalance=rs=-0.08:bs=0.08:rm=-0.06:bm=0.06"]),
    "vintage": (N_("Vintage"), ["curves=preset=vintage", "eq=saturation=0.85"]),
    "contrast": (N_("High contrast"), ["curves=preset=strong_contrast"]),
}

# The looks whose filters (colorbalance, curves) only work on RGB pictures: ffmpeg converts the
# frames to 24-bit RGB for them, twice the size of yuv420p, and a reversed clip holds them like
# that.
RGB_LOOKS = frozenset({"warm", "cool", "vintage", "contrast"})

# Stabilization strength ->(vidstabdetect shakiness, vidstabtransform smoothing), or None for off.
STABILIZE = {"off": None, "light": (5, 10), "strong": (8, 30)}


@dataclass
class VideoEdit:
    aspect: Optional[tuple[int, int]] = (9, 16)
    fit: str = "crop"
    pos_x: float = 0.5  # where the crop box sits: 0 = left/top, 1 = right/bottom
    pos_y: float = 0.5
    zoom: float = 1.0  # >1 crops tighter
    rotate: int = 0  # degrees clockwise: 0, 90, 180 or 270
    flip: bool = False  # mirror left-right
    resolution: Optional[int] = None
    crf: int = VIDEO_QUALITY[DEFAULT_VIDEO_QUALITY]
    use_gpu: bool = True  # let the graphics card encode when there is a working one
    mute: bool = False
    start: Optional[float] = None
    end: Optional[float] = None
    keep: list[Range] = field(default_factory=list)  # kept parts of the source; [] = no cuts
    speed: float = 1.0  # 0.25 to 4
    reverse: bool = False
    fade_in: float = 0.0  # picture fades, in seconds
    fade_out: float = 0.0
    audio_fade_in: float = 0.0
    audio_fade_out: float = 0.0
    brightness: float = 0.0  # picture adjustments, see ADJUSTMENTS
    contrast: float = 1.0
    saturation: float = 1.0
    gamma: float = 1.0
    sharpen: float = 0.0
    denoise: float = 0.0
    look: str = "none"  # a LOOKS key
    stabilize: str = "off"  # a STABILIZE key; applied when exporting only
    frame_rate: Optional[
        int] = None  # a FRAME_RATES value: a steady, phone-safe export; None = as source
    volume: float = 0.0  # sound, see sound.py: the volume in dB
    normalize: bool = False  # even out the loudness (-14 LUFS), in two passes
    audio_denoise: str = "off"  # an AUDIO_DENOISE key
    music: str = ""  # background music file; "" = none
    music_volume: float = DEFAULT_MUSIC_VOLUME  # dB, next to the video's own sound
    music_duck: bool = False  # lower the music while the video's sound plays
    # text on the picture, see the captions package
    texts: list[TextItem] = field(default_factory=list)
    logo: Logo = field(default_factory=Logo)  # a logo or watermark over the whole video
    subtitles: Subtitles = field(
        default_factory=Subtitles)  # a subtitle file burned into the picture
    video_format: str = DEFAULT_VIDEO_FORMAT  # an OUTPUT_FORMATS key: MP4, H.265, WebM, MOV or GIF
    # more than 0: make the file fit in this many megabytes (crf is then ignored)
    target_mb: float = 0.0
    # a GIF's frame rate and width are limits; a slower, narrower video keeps its own
    gif_fps: int = GIF_FPS
    gif_width: int = GIF_WIDTH

    @property
    def output_format(self):
        """The OutputFormat of `video_format` (MP4 when it isn't a known one)."""
        return OUTPUT_FORMATS.get(self.video_format, OUTPUT_FORMATS[DEFAULT_VIDEO_FORMAT])

    @property
    def sound(self) -> Sound:
        return Sound(
            self.volume, self.normalize, self.audio_denoise, self.music, self.music_volume,
            self.music_duck)

    @property
    def crops(self) -> bool:
        return self.fit == "crop" or self.aspect is None

    def segments(self, duration: Optional[float], fps: Optional[float] = None) -> list[Segment]:
        """The parts of a source `duration` seconds long that the export keeps. Given the source's
        `fps`, their edges sit on whole frames (see `snap_segments`)."""
        return snap_segments(
            kept_segments(self.keep, self.start, self.end, duration), fps, duration)


def output_tag(edit: VideoEdit) -> str:
    """The word in the output's file name: "clip (phone).mp4". The original shape at a steady frame
    rate is the "make it play on my phone" export, so it says so."""
    tag = "phone-ready" if edit.aspect is None and edit.frame_rate else FORMAT_TAGS.get(
        edit.aspect, "edited")
    # An H.265 file is an MP4 too, so its name says what is different about it.
    return f"{tag} H.265" if edit.video_format == "hevc" else tag
