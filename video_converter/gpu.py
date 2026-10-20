"""Hardware H.264 and H.265 encoders: find the ones that really work on this PC, and the ffmpeg options to use them."""

from __future__ import annotations

import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .core import _NO_WINDOW

log = logging.getLogger(__name__)

# Encoder name -> function(crf, fps) giving the ffmpeg options for a constant-quality video that looks
# about like x264's at that CRF. The scales differ (measured on noisy 1080p test video): the same number
# gives a much bigger NVENC file, so each encoder gets its own offset. The result is 8-bit 4:2:0 H.264
# like the CPU's, so it plays everywhere; Intel's encoder takes that as nv12 (the same pixels in a
# different order) and needs the frame rate said out loud after cuts and speed changes.
ENCODERS: dict[str, Callable[[int, float], list[str]]] = {
    "h264_nvenc": lambda crf, fps: ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(crf + 8),
                                    "-b:v", "0", "-pix_fmt", "yuv420p"],
    "h264_qsv": lambda crf, fps: ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(crf),
                                  "-r", f"{fps:.6g}", "-pix_fmt", "nv12"],
    "h264_amf": lambda crf, fps: ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", str(crf + 5),
                                  "-qp_p", str(crf + 5), "-pix_fmt", "yuv420p"],
}


def _hevc(make: Callable[[int, float], list[str]]) -> Callable[[int, float], list[str]]:
    return lambda crf, fps: [arg.replace("h264", "hevc") for arg in make(crf, fps)] + ["-tag:v", "hvc1"]


# The same cards' H.265 encoders. They take the same quality numbers as their H.264 cousins (a file of the
# same quality is smaller anyway), and the result is 8-bit 4:2:0 tagged hvc1, which Apple's players want.
HEVC_ENCODERS: dict[str, Callable[[int, float], list[str]]] = {
    name.replace("h264", "hevc"): _hevc(make) for name, make in ENCODERS.items()
}
# What to tell the user, in the order to prefer them.
LABELS = {
    "h264_nvenc": "NVIDIA graphics card",
    "h264_qsv": "Intel graphics",
    "h264_amf": "AMD graphics card",
}

PROBE_SECONDS = 15        # a graphics card that wakes up slowly still gets a fair chance
CPU_FALLBACK_NOTE = "Exported on the CPU because the graphics card failed"


def encoder_codec(encoder: str) -> str:
    """The picture format an encoder makes: "h264" for h264_nvenc, "hevc" for hevc_qsv."""
    return encoder.split("_", 1)[0]


# The options that set an encoder's quality; asking for a bitrate takes them out.
_QUALITY_OPTIONS = {"-cq", "-global_quality", "-rc", "-qp_i", "-qp_p", "-b:v"}


def _bitrate_args(encoder: str, kbps: int) -> list[str]:
    """The options that give an encoder a bitrate, held down by a maximum of the same size."""
    limit = [f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k"]
    maker = encoder.split("_", 1)[1] if "_" in encoder else encoder
    if maker == "nvenc":
        return ["-rc", "vbr", "-b:v", *limit]
    if maker == "amf":
        return ["-rc", "vbr_peak", "-b:v", *limit]
    return ["-b:v", *limit]


def encoder_args(encoder: str, crf: int, fps: float, level: Optional[str] = None,
                 kbps: Optional[int] = None) -> list[str]:
    """The ffmpeg video options for `encoder` at quality `crf`, for a video at `fps` frames a second.
    With a `level` such as "4.2" the video is H.264 High profile at that level (a phone-safe export);
    all three encoders take the level as its number, 42. With `kbps` the picture gets that bitrate
    instead of a quality, to fit a file size."""
    args = (ENCODERS[encoder] if encoder in ENCODERS else HEVC_ENCODERS[encoder])(crf, fps)
    if kbps:
        kept, skip = [], False
        for arg in args:            # each quality option comes with its value
            if skip:
                skip = False
            elif arg in _QUALITY_OPTIONS:
                skip = True
            else:
                kept.append(arg)
        args = [*kept, *_bitrate_args(encoder, kbps)]
    if level:
        args += ["-profile:v", "high", "-level", str(round(float(level) * 10))]
    return args


def _encoder_works(ffmpeg: str, encoder: str) -> bool:
    """Whether ffmpeg can really encode with `encoder` here. Being listed by `-encoders` proves
    nothing: the program is built with all of them, but the PC needs the matching hardware and driver."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
           "color=c=gray:s=256x144:d=0.2:r=25", *encoder_args(encoder, 23, 25.0), "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                                timeout=PROBE_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


_lock = threading.Lock()
_found: dict[str, tuple[str, ...]] = {}       # ffmpeg path -> working encoders, best first


def detect_encoders(ffmpeg: str, codec: str = "h264") -> tuple[str, ...]:
    """The hardware encoders that work, best first: the H.264 ones, or with `codec` "hevc" the H.265 ones.
    Checked once per run (per ffmpeg)."""
    key = ffmpeg if codec == "h264" else f"{ffmpeg}|{codec}"
    with _lock:
        if key in _found:
            return _found[key]
        names = list(ENCODERS if codec == "h264" else HEVC_ENCODERS)
        with ThreadPoolExecutor(max_workers=len(names), thread_name_prefix="gpu-probe") as pool:
            works = list(pool.map(lambda name: _encoder_works(ffmpeg, name), names))
        _found[key] = tuple(name for name, ok in zip(names, works) if ok)
        log.info("Hardware %sencoders that work: %s", "" if codec == "h264" else "H.265 ",
                 ", ".join(_found[key]) or "none")
        return _found[key]


def forget_detection() -> None:
    """Check again next time (for tests)."""
    with _lock:
        _found.clear()


def best_encoder(ffmpeg: str, codec: str = "h264") -> Optional[str]:
    found = detect_encoders(ffmpeg) if codec == "h264" else detect_encoders(ffmpeg, codec)
    return found[0] if found else None


def choose_encoder(ffmpeg: str, use_gpu: bool, codec: str = "h264") -> Optional[str]:
    """The hardware encoder an export should try: the best one that works, if the user wants it.
    `codec` is the picture format, "h264" or "hevc"; any other (WebM, GIF) has no hardware encoder."""
    if not use_gpu or codec not in ("h264", "hevc"):
        return None
    return best_encoder(ffmpeg) if codec == "h264" else best_encoder(ffmpeg, codec)


def gpu_name(encoder: str) -> str:
    """A readable name for the hardware behind `encoder`: the card's own name where the driver tells
    us (NVIDIA), otherwise the maker."""
    label = LABELS.get(encoder) or LABELS.get(encoder.replace("hevc", "h264"), "graphics card")
    if encoder.endswith("_nvenc"):
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=5)
            name = result.stdout.strip().splitlines()[0].strip() if result.returncode == 0 and result.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError):
            name = ""
        if name:
            return name
    return label


def describe_gpu(ffmpeg: str) -> str:
    """What the Video quality section shows next to the option."""
    encoder = best_encoder(ffmpeg)
    return gpu_name(encoder) if encoder else "No supported graphics card found"
