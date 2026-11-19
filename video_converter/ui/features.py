"""The features the home page offers. Each one lands on a section of the settings panel, with its
choice already made (Save as MP3 really is an MP3 output). Export, Export all and Before / after
are helpers, not features, so they are not here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..i18n import N_
from ..output import DEFAULT_TARGET_MB, DEFAULT_VIDEO_FORMAT
from ..project import OUTPUT_AUDIO, OUTPUT_VIDEO, ClipSettings


@dataclass(frozen=True)
class Feature:
    id: str
    title: str  # English, shown through tr()
    text: str  # one line about what it does
    icon: str
    section: Optional[str]  # the Inspector attribute of the section it opens (None: none)
    choice: Optional[Callable[[ClipSettings], None]] = None  # makes the feature's choice
    focus: str = ""  # a widget of the Inspector to give the keyboard, as a dotted attribute path
    needs_picture: bool = False  # the choice is about the picture, so a sound file can't use it


def _mp3(s: ClipSettings) -> None:
    s.output = OUTPUT_AUDIO


def _vertical(s: ClipSettings) -> None:
    s.output = OUTPUT_VIDEO
    s.edit.aspect = (9, 16)


def _fit_size(s: ClipSettings) -> None:
    s.output = OUTPUT_VIDEO
    if s.edit.video_format == "gif":
        s.edit.video_format = DEFAULT_VIDEO_FORMAT  # a GIF has no file size target
    s.edit.target_mb = s.edit.target_mb or DEFAULT_TARGET_MB


def _gif(s: ClipSettings) -> None:
    s.output = OUTPUT_VIDEO
    s.edit.video_format = "gif"


def _backwards(s: ClipSettings) -> None:
    s.output = OUTPUT_VIDEO
    s.edit.reverse = True


def _steady(s: ClipSettings) -> None:
    s.output = OUTPUT_VIDEO
    if s.edit.stabilize == "off":
        s.edit.stabilize = "light"


def _no_sound(s: ClipSettings) -> None:
    s.output = OUTPUT_VIDEO
    s.edit.mute = True


FEATURES = [
    Feature(
        "trim", N_("Trim and cut"),
        N_("Keep just the part you want, or cut a piece out of the middle."), "scissors",
        "trim_section"),
    Feature(
        "silences", N_("Remove silences"), N_("Find the pauses in the sound and cut them out."),
        "split", "trim_section", focus="silence_button"),
    Feature(
        "mp3", N_("Save as MP3"), N_("Keep only the sound, as an MP3 file."), "music",
        "output_section", _mp3),
    Feature(
        "vertical", N_("Vertical video"),
        N_("Make a 9:16 video for Stories, Reels, TikTok and Shorts."), "phone", "frame_section",
        _vertical, needs_picture=True),
    Feature(
        "crop", N_("Crop and rotate"),
        N_("Change the shape, zoom in, turn or mirror the picture."), "crop", "frame_section",
        needs_picture=True),
    Feature(
        "size", N_("Fit a file size"),
        N_("Make the file small enough for Discord, WhatsApp or email."), "size",
        "quality_section", _fit_size, needs_picture=True),
    Feature(
        "gif", N_("Make a GIF"), N_("Turn a short clip into an animated picture."), "film",
        "quality_section", _gif, needs_picture=True),
    Feature(
        "format", N_("Format and quality"), N_("MP4, H.265, WebM or MOV, and how sharp it is."),
        "film", "quality_section", needs_picture=True),
    Feature(
        "speed", N_("Speed"), N_("Slow motion or fast forward, with the sound's pitch kept."),
        "speed", "speed_section"),
    Feature(
        "reverse", N_("Play backwards"), N_("Run the clip from its end to its start."), "back",
        "speed_section", _backwards, needs_picture=True),
    Feature(
        "look", N_("Colour and looks"),
        N_("Brightness, contrast, filters like black and white, and sharpening."), "palette",
        "adjust_section", needs_picture=True),
    Feature(
        "steady", N_("Steady shaky video"), N_("Smooth out shaky footage when exporting."),
        "palette", "adjust_section", _steady, needs_picture=True),
    Feature(
        "sound", N_("Volume and noise"),
        N_("Louder, quieter, even loudness, and less background noise."), "volume",
        "audio_section"),
    Feature(
        "mute", N_("Remove the sound"), N_("Export the video without any sound."), "mute",
        "audio_section", _no_sound, needs_picture=True),
    Feature(
        "music", N_("Background music"), N_("Play a song behind the video."), "music",
        "audio_section", focus="music_choose", needs_picture=True),
    Feature(
        "text", N_("Text and logo"), N_("Put words or your logo on the picture."), "text",
        "captions_section", needs_picture=True),
    Feature(
        "captions", N_("Automatic captions"), N_("Write down the speech as captions, on this PC."),
        "text", "captions_section", focus="captions.auto_button", needs_picture=True),
    Feature(
        "fades", N_("Fades"), N_("Fade the picture and the sound in and out."), "volume",
        "fade_section"),
    Feature(
        "presets", N_("Presets"), N_("One click for YouTube, a phone, WhatsApp and more."), "film",
        "preset_section"),
    Feature(
        "join", N_("Join into one"), N_("Put several clips one after the other in one video."),
        "join", None),
]

BY_ID = {feature.id: feature for feature in FEATURES}
