"""The picture's filter graph: orientation, adjustments, one clip's graph."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from ..captions import overlay_graph
from ..core import MediaInfo
from ..sound import voice_filters
from ..timing import (
    Segment, clamp_speed, fade_filters, frame_margin, output_duration, relative, select_filters,
)
from .edit import ADJUSTMENTS, LOOKS, STABILIZE, VideoEdit
from .geometry import Geometry, _even, plan_geometry
from .rates import _rate_text, native_rate

def orientation_filters(edit: VideoEdit) -> list[str]:
    filters = {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}.get(edit.rotate, [])
    return filters + (["hflip"] if edit.flip else [])


def _num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def picture_filters(edit: VideoEdit) -> list[str]:
    """Denoise, brightness/contrast/saturation/gamma, the color look, then sharpening."""
    def value(name: str) -> float:
        low, high, _ = ADJUSTMENTS[name]
        return min(max(getattr(edit, name), low), high)

    filters = []
    if value("denoise") > 0:
        filters.append(f"hqdn3d={_num(value('denoise'))}")
    eq = [f"{name}={_num(value(name))}" for name in ("brightness", "contrast", "saturation", "gamma")
          if abs(value(name) - ADJUSTMENTS[name][2]) > 1e-6]
    if eq:
        filters.append("eq=" + ":".join(eq))
    filters += LOOKS.get(edit.look, LOOKS["none"])[1]
    if value("sharpen") > 0:
        filters.append(f"unsharp=5:5:{_num(value('sharpen'))}:5:5:0")
    return filters


def build_filter_graph(geometry: Geometry, edit: VideoEdit, src: str = "[0:v]", out: str = "[v]",
                       tag: str = "") -> str:
    """An ffmpeg -filter_complex graph that reframes `src` (rotate, crop or fit, scale) into `out`.
    `tag` keeps its internal labels apart from those of other clips in the same graph."""
    tw, th = geometry.out_width, geometry.out_height
    pre = "".join(f"{f}," for f in orientation_filters(edit))

    if geometry.crop:
        cw, ch, cx, cy = geometry.crop
        return f"{src}{pre}crop={cw}:{ch}:{cx}:{cy},scale={tw}:{th}:flags=lanczos,setsar=1{out}"

    fit_inside = f"scale={tw}:{th}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    if edit.fit == "bars":
        return f"{src}{pre}{fit_inside},pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1{out}"

    # Blurred background: a zoomed-in, blurred copy fills the frame behind the whole video.
    # The blur runs at quarter size, which looks the same and is much faster.
    bw, bh = _even(tw / 4), _even(th / 4)
    return (
        f"{src}{pre}split=2[bg{tag}][fg{tag}];"
        f"[bg{tag}]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"gblur=sigma=6,eq=brightness=-0.08,scale={tw}:{th}[blurred{tag}];"
        f"[fg{tag}]{fit_inside}[front{tag}];"
        f"[blurred{tag}][front{tag}]overlay=(W-w)/2:(H-h)/2,setsar=1{out}"
    )


@dataclass
class ClipGraph:
    """The filters for one input of an export."""
    filters: list[str]
    duration: Optional[float]      # length in the export, when known
    has_audio: bool


def clip_graph(index: int, edit: VideoEdit, info: MediaInfo, segments: list[Segment], *,
               video: bool = True, audio: bool = True, frame: Optional[tuple[int, int]] = None,
               fps: Optional[float | Fraction] = None, v_out: str = "[v]", a_out: str = "[a]",
               transforms: Optional[str] = None, picture: bool = True, upright: bool = False) -> ClipGraph:
    """Filters that take input `index` through cuts, stabilization, speed, reframing, picture
    adjustments, reverse and fades; the sound goes through its cuts, speed, reverse, fades, noise
    reduction and volume. With `picture` off (the loudness pass) only the sound is built, though a
    muted video export still has none.

    The input must start at the first segment (see `input_args`), and `segments` should come from
    `edit.segments(duration, fps)` so their edges sit on whole frames. `frame` and `fps` force the
    output size and frame rate, for joining clips (and `fps` for a steady frame rate): a clip whose
    shape differs is fitted inside that frame with black bars. The `fps` filter comes after the cuts
    and the speed change, so every frame the export keeps lands on the steady grid in its final time.
    `transforms` names the file from the stabilizer's detect pass, relative to ffmpeg's working folder.
    With `upright` the input's rotation metadata is turned into a rotation of the picture here, in the graph
    (the input is opened with `rotation_args`, which leaves it to the graph)."""
    tag = str(index)
    rel = relative(segments)
    speed = clamp_speed(edit.speed)
    duration = output_duration(rel, speed)
    filters: list[str] = []
    if video and picture:
        timed = f"[t{tag}]"
        source = f"[{index}:v:0]"
        if upright and info.display_rotation:
            # The input was opened without ffmpeg's own rotation (see `rotation_args`): turn it here.
            filters.append(f"{source}{','.join(ROTATE_FILTERS[info.display_rotation])}[up{tag}]")
            source = f"[up{tag}]"
        filters += select_filters(source, timed, rel, audio=False, tag=tag,
                                 margin=frame_margin(info.fps))
        tempo = []
        strength = STABILIZE.get(edit.stabilize)
        if transforms and strength:
            # Right after the cuts, so the frames match the ones the detect pass saw.
            tempo.append(f"vidstabtransform=input={transforms}:smoothing={strength[1]}:optzoom=1:interpol=bicubic")
        # A faster clip would otherwise have more frames per second than its source.
        rate = fps or (native_rate(info.fps) if speed != 1.0 and info.fps else None)
        if speed != 1.0:
            # The sped-up frames sit exactly half way between two slots of the output rate (every other
            # frame at 2x), and which way the fps filter rounds those ties changes at every cut. A
            # quarter of an output frame later puts each one clearly in a slot.
            nudge = f"+{0.25 / float(rate):.6f}/TB" if rate else ""
            tempo.append(f"setpts=PTS/{speed:.6g}{nudge}")
        if rate:
            tempo.append(f"fps={_rate_text(rate)}")
        if tempo:
            filters.append(f"{timed}{','.join(tempo)}[tempo{tag}]")
            timed = f"[tempo{tag}]"
        framed = f"[f{tag}]"
        geometry = plan_geometry(info.width, info.height, edit)
        filters.append(build_filter_graph(geometry, edit, timed, framed, tag))
        # Reverse after scaling: it holds every frame in memory, so smaller frames help.
        post = picture_filters(edit)
        post += ["reverse"] if edit.reverse else []
        post += fade_filters(edit.fade_in, edit.fade_out, duration, audio=False)
        if frame:
            w, h = frame
            post += [f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black", "setsar=1", "format=yuv420p"]
        # Text, the logo and subtitles go on last, at the size of the final picture.
        overlays = overlay_graph(f"[ov{tag}]", v_out, edit.texts, edit.logo, edit.subtitles,
                                 frame or (geometry.out_width, geometry.out_height), segments, speed, edit.reverse, tag)
        filters.append(f"{framed}{','.join(post) or 'null'}{f'[ov{tag}]' if overlays else v_out}")
        filters += overlays
    has_audio = audio and info.has_audio and not (video and edit.mute)
    if has_audio:
        filters.append(voice_filters(f"[{index}:a:0]", a_out, segments, edit.sound, speed, edit.reverse,
                                     edit.audio_fade_in, edit.audio_fade_out, tag=tag, always=True))
    return ClipGraph(filters, duration, has_audio)


# Clockwise turn -> the filters that make a stored picture upright.
ROTATE_FILTERS = {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}


def rotation_args(info: MediaInfo) -> list[str]:
    """Input options that stop ffmpeg turning a rotated input (and then marking the output as rotated).
    With several inputs ffmpeg turns each picture upright, but writes the first input's rotation into the
    result as well, which makes a player turn the file a second time. So the rotation is ignored here
    and done in the graph (see clip_graph's `upright`)."""
    if not info.display_rotation:
        return []
    return ["-noautorotate", "-display_rotation:v:0", "0"]
