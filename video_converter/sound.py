"""Sound tools: volume, even loudness, noise reduction and background music.

Everything here builds ffmpeg filter graphs; running them is up to `video.py` and `core.py`. The
loudness is evened out in two passes (measure, then apply), which is how loudnorm is accurate.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Optional

from .timing import Segment, audio_filters, fade_filters

VOLUME_MIN, VOLUME_MAX = -20.0, 20.0                  # dB
MUSIC_VOLUME_MIN, MUSIC_VOLUME_MAX = -40.0, 0.0       # dB
DEFAULT_MUSIC_VOLUME = -18.0
MUSIC_FADE = 2.0            # the music fades out over this many seconds at the end

# YouTube and Spotify play at about -14 LUFS; -1 dB keeps the loudest peaks from clipping after encoding.
TARGET_LUFS = -14.0
TARGET_PEAK = -1.0
TARGET_RANGE = 11.0
NORMALIZED_RATE = 48000     # loudnorm works at 192 kHz inside, so its result is brought back down

# Noise reduction -> afftdn settings, or None for off. nr is how many dB of noise to take out, nf the
# level the noise is assumed to start at, and tn lets it follow the noise as it changes. Light only
# takes away a gentle hiss, so speech keeps its body; Strong cleans up louder noise at the price of
# a little more of the "underwater" sound that every noise remover has.
#
# Measured on a spoken clip over white noise at -36 dB, in the pauses (and the speech level stays within
# 0.1 dB): the old values (nr 10/18, tn=1 without tr) took out 0.4 dB and Light and Strong were the same;
# with residual tracking (tr=1) Light takes out 10 dB, and Strong, which runs the filter twice, 33 dB.
# Noise that is mostly low rumble (pink, brown) is out of reach of this filter: 1 to 2 dB.
AUDIO_DENOISE = {
    "off": None,
    "light": "afftdn=nr=12:nf=-50:tn=1:tr=1",
    "strong": "afftdn=nr=24:nf=-45:tn=1:tr=1,afftdn=nr=24:nf=-45:tn=1:tr=1",
}

# The last stage of any sound that was made louder, mixed with music or evened out: a limiter that keeps the
# peaks under 0 dBFS. Without it a +6 dB boost, or music on top of speech, goes past the top and is cut
# off (clipped) by the encoder; loudnorm's own limiter doesn't work in the steady (linear) mode used here.
# `level=disabled` leaves the volume as it is. The ceiling is a little under -1 dBFS, because an MP3 or AAC
# file overshoots what it was given by up to half a dB, and the file should stay under -0.5 dBTP.
LIMIT = 0.89
LIMITER = f"alimiter=limit={LIMIT}:level=disabled"

# Lets the music duck under the video's sound: a quick dip when it gets loud, a slow return.
_DUCK = "sidechaincompress=threshold=0.015:ratio=8:attack=30:release=400"

STEREO_48K = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


@dataclass
class Sound:
    """The sound settings of one file (a view of the fields of a `VideoEdit`)."""
    volume: float = 0.0                 # dB
    normalize: bool = False
    denoise: str = "off"                # an AUDIO_DENOISE key
    music: str = ""                     # path of the background music; "" = none
    music_volume: float = DEFAULT_MUSIC_VOLUME      # dB, next to the video's own sound
    duck: bool = False                  # lower the music while the video's sound is playing


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _db(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def clip_filters(sound: Sound) -> list[str]:
    """Filters for one clip's own sound: noise reduction, then the volume."""
    filters = []
    if AUDIO_DENOISE.get(sound.denoise):
        filters.append(AUDIO_DENOISE[sound.denoise])
    volume = _clamp(sound.volume, VOLUME_MIN, VOLUME_MAX)
    if abs(volume) > 1e-6:
        filters.append(f"volume={_db(volume)}dB")
    if volume > 1e-6:
        filters.append(LIMITER)         # the boost must not go past the top
    return filters


def voice_filters(src: str, out: str, segments: list[Segment], sound: Sound, speed: float = 1.0,
                  reverse: bool = False, fade_in: float = 0.0, fade_out: float = 0.0, tag: str = "",
                  always: bool = False) -> str:
    """`timing.audio_filters` (cuts, speed, reverse, fades) followed by the clip's `clip_filters`.
    Returns "" only when nothing at all needs doing and `always` isn't set."""
    extra = clip_filters(sound)
    if not extra:
        return audio_filters(src, out, segments, speed, reverse, fade_in, fade_out, tag=tag, always=always)
    middle = f"[snd{tag}]"
    base = audio_filters(src, middle, segments, speed, reverse, fade_in, fade_out, tag=tag, always=True)
    return f"{base};{middle}{','.join(extra)}{out}"


# ----- loudness -----------------------------------------------------------------

@dataclass
class Loudness:
    """What loudnorm measured in its first pass."""
    integrated: float       # LUFS
    true_peak: float        # dBTP
    range: float            # LU
    threshold: float        # LUFS
    offset: float           # dB

    @property
    def usable(self) -> bool:
        """False for a silent sound: its loudness is minus infinity, and there is nothing to even out."""
        return all(math.isfinite(v) for v in (self.integrated, self.true_peak, self.range, self.threshold,
                                              self.offset))


def parse_loudness(text: str) -> Optional[Loudness]:
    """The numbers loudnorm prints (as JSON) at the end of its first pass, or None if they're not there."""
    for block in reversed(re.findall(r"\{[^{}]*\}", text)):
        try:
            data = json.loads(block)
            return Loudness(float(data["input_i"]), float(data["input_tp"]), float(data["input_lra"]),
                            float(data["input_thresh"]), float(data["target_offset"]))
        except (ValueError, KeyError, TypeError):
            continue
    return None


def loudnorm(measured: Optional[Loudness] = None) -> str:
    """The loudnorm filter. Without `measured` it only measures (the first pass, printing JSON); with it,
    it applies one steady gain that lands on the target (the second)."""
    target = f"loudnorm=I={_db(TARGET_LUFS)}:TP={_db(TARGET_PEAK)}:LRA={_db(TARGET_RANGE)}"
    if measured is None:
        return f"{target}:print_format=json"
    return (f"{target}:measured_I={measured.integrated:.2f}:measured_TP={measured.true_peak:.2f}"
            f":measured_LRA={measured.range:.2f}:measured_thresh={measured.threshold:.2f}"
            f":offset={measured.offset:.2f}:linear=true,aresample={NORMALIZED_RATE}")


# ----- music and the final mix ----------------------------------------------------

def music_args(path: str) -> list[str]:
    """Input options for the music: read it over and over, because the video may be longer."""
    return ["-stream_loop", "-1", "-i", path]


def master_filters(sound: Sound, voice: Optional[str], music: Optional[int], total: Optional[float], out: str,
                   loudness: Optional[Loudness] = None, measure: bool = False) -> list[str]:
    """Filters that lay the music under the finished sound of the export and even out its loudness.

    `voice` is the label of the export's own sound (None when it has none), `music` the index of the
    music input (None for no music) and `total` the export's length in seconds, which the music is
    cut to, with a fade out at its end. With a `loudness` measured by a first pass the sound is
    normalized; with `measure` the last filter is the measuring one instead, for that first pass.
    A sound without music or normalizing needs no filters, and gets one `anull`."""
    steps: list[str] = []
    current = voice
    if music is not None:
        # With the video's own sound the music sits next to it at its volume; alone, it plays as it is.
        song = [STEREO_48K]
        if voice:
            song.append(f"volume={_db(_clamp(sound.music_volume, MUSIC_VOLUME_MIN, MUSIC_VOLUME_MAX))}dB")
        if total:
            song += [f"atrim=duration={total:.3f}", "asetpts=PTS-STARTPTS"]
            song += fade_filters(0.0, MUSIC_FADE, total, audio=True)
        song_chain = ",".join(song)
        if voice is None:
            steps.append(f"[{music}:a:0]{song_chain}[mixed]")
        else:
            under = "[song]" if sound.duck else "[ducked]"
            steps.append(f"[{music}:a:0]{song_chain}{under}")
            if sound.duck:
                steps.append(f"{voice}{STEREO_48K},asplit=2[heard][key]")
                steps.append(f"[song][key]{_DUCK}[ducked]")
            else:
                steps.append(f"{voice}{STEREO_48K}[heard]")
            steps.append("[heard][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixed]")
        current = "[mixed]"
    if current and sound.normalize and (measure or loudness is not None):
        # The measuring pass is thrown away; the real one ends with the limiter (see LIMITER).
        steps.append(f"{current}{loudnorm(None if measure else loudness)}{'' if measure else ',' + LIMITER}{out}")
    elif current and current != out:
        steps.append(f"{current}{LIMITER if music is not None else 'anull'}{out}")
    return steps
