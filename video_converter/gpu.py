"""Hardware H.264 encoders: find the ones that really work on this PC, and the ffmpeg options to use them."""

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
# What to tell the user, in the order to prefer them.
LABELS = {
    "h264_nvenc": "NVIDIA graphics card",
    "h264_qsv": "Intel graphics",
    "h264_amf": "AMD graphics card",
}

PROBE_SECONDS = 15        # a graphics card that wakes up slowly still gets a fair chance
CPU_FALLBACK_NOTE = "Exported on the CPU because the graphics card failed"


def encoder_args(encoder: str, crf: int, fps: float) -> list[str]:
    """The ffmpeg video options for `encoder` at quality `crf`, for a video at `fps` frames a second."""
    return ENCODERS[encoder](crf, fps)


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


def detect_encoders(ffmpeg: str) -> tuple[str, ...]:
    """The hardware encoders that work, best first. Checked once per run (per ffmpeg)."""
    with _lock:
        if ffmpeg in _found:
            return _found[ffmpeg]
        names = list(ENCODERS)
        with ThreadPoolExecutor(max_workers=len(names), thread_name_prefix="gpu-probe") as pool:
            works = list(pool.map(lambda name: _encoder_works(ffmpeg, name), names))
        _found[ffmpeg] = tuple(name for name, ok in zip(names, works) if ok)
        log.info("Hardware encoders that work: %s", ", ".join(_found[ffmpeg]) or "none")
        return _found[ffmpeg]


def forget_detection() -> None:
    """Check again next time (for tests)."""
    with _lock:
        _found.clear()


def best_encoder(ffmpeg: str) -> Optional[str]:
    found = detect_encoders(ffmpeg)
    return found[0] if found else None


def choose_encoder(ffmpeg: str, use_gpu: bool) -> Optional[str]:
    """The hardware encoder an export should try: the best one that works, if the user wants it."""
    return best_encoder(ffmpeg) if use_gpu else None


def gpu_name(encoder: str) -> str:
    """A readable name for the hardware behind `encoder`: the card's own name where the driver tells
    us (NVIDIA), otherwise the maker."""
    label = LABELS.get(encoder, "graphics card")
    if encoder == "h264_nvenc":
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
