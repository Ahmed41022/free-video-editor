"""The app's log file, crash handling, and the "Report a problem" and "About" texts."""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from . import APP_NAME, REPO_URL, __version__
from .core import _NO_WINDOW
from .i18n import tr

LOG_NAME = "app.log"
MAX_LOG_BYTES = 1_000_000          # about 10,000 lines; three older files are kept as app.log.1 ... .3
LOG_FILES_KEPT = 3
ISSUES_URL = f"{REPO_URL}/issues/new"
URL_LIMIT = 6000                   # browsers and GitHub cope with a lot more; this stays well clear
REPORT_LOG_LINES = 30
MAX_LINE = 300                     # a longer log line is cut when it goes into a report

log = logging.getLogger("video_converter")
crash_log = logging.getLogger("video_converter.crash")
_handler: Optional[logging.Handler] = None


# ----- the log file ---------------------------------------------------------------

def log_folder() -> Path:
    """Where the log goes: %LOCALAPPDATA%\\Free Video Editor\\logs, or the temp folder if that can't be used."""
    base = os.environ.get("LOCALAPPDATA")
    candidates = [Path(base) / APP_NAME / "logs"] if base else []
    candidates.append(Path(tempfile.gettempdir()) / f"{APP_NAME} logs")
    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return folder
        except OSError:
            continue
    return candidates[-1]


def log_path() -> Path:
    """The log file the running app writes (or would write)."""
    return _handler_path() or log_folder() / LOG_NAME


def _handler_path() -> Optional[Path]:
    return Path(_handler.baseFilename) if isinstance(_handler, logging.handlers.RotatingFileHandler) else None


def setup_logging(folder: Optional[Path] = None) -> Path:
    """Start writing the app's log, with a rotating file in `folder` (default: `log_folder()`).
    Returns the log file. Calling it again moves the log to the new place."""
    global _handler
    shutdown_logging()
    path = (folder or log_folder()) / LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_FILES_KEPT,
                                                   encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    _handler = handler
    return path


def shutdown_logging() -> None:
    """Stop writing the log and let go of the file."""
    global _handler
    if _handler is not None:
        log.removeHandler(_handler)
        _handler.close()
        _handler = None


def recent_log_lines(count: int = REPORT_LOG_LINES, path: Optional[Path] = None) -> list[str]:
    """The last `count` lines of the log, oldest first (empty if there is no log yet)."""
    path = path or log_path()
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(size - 64 * 1024, 0))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return [line for line in lines if line.strip()][-count:]


# ----- what this PC looks like ----------------------------------------------------

def windows_version() -> str:
    """e.g. "Windows 11 (build 26300)". Python reports Windows 11 as "10", so the build number decides."""
    if sys.platform != "win32":
        return platform.platform()
    try:
        build = sys.getwindowsversion().build
    except AttributeError:
        return platform.platform()
    return f"Windows {'11' if build >= 22000 else '10'} (build {build})"


_ffmpeg_versions: dict[str, str] = {}


def ffmpeg_version(ffmpeg: str) -> str:
    """The version ffmpeg reports about itself (asked once), or "unknown"."""
    if ffmpeg not in _ffmpeg_versions:
        try:
            result = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                                    timeout=15)
            match = re.match(r"ffmpeg version (\S+)", result.stdout)
            _ffmpeg_versions[ffmpeg] = match.group(1) if match else "unknown"
        except (OSError, subprocess.SubprocessError):
            _ffmpeg_versions[ffmpeg] = "unknown"
    return _ffmpeg_versions[ffmpeg]


def encoders_text(encoders: Optional[tuple[str, ...]]) -> str:
    """The graphics encoders found, for a log or report. None = the search hasn't finished."""
    if encoders is None:
        return "still being checked"
    return ", ".join(encoders) if encoders else "none"


def system_lines(ffmpeg: Optional[str], encoders: Optional[tuple[str, ...]]) -> list[str]:
    """Facts about the app and this PC that help to find a problem."""
    return [
        f"{APP_NAME} {__version__}",
        windows_version(),
        f"Python {platform.python_version()}",
        f"ffmpeg {ffmpeg_version(ffmpeg) if ffmpeg else 'not found'}",
        f"Graphics card encoders: {encoders_text(encoders)}",
    ]


def log_startup(ffmpeg: Optional[str]) -> None:
    """The first entries of a run: which app, on which Windows, with which ffmpeg."""
    log.info("%s %s started", APP_NAME, __version__)
    log.info("%s; Python %s", windows_version(), platform.python_version())
    log.info("ffmpeg: %s (version %s)", ffmpeg or "not found", ffmpeg_version(ffmpeg) if ffmpeg else "n/a")


# ----- crashes --------------------------------------------------------------------

def install_excepthooks(on_error: Optional[Callable[[str], None]] = None) -> Callable[[], None]:
    """Log every exception nobody caught, in the main thread (`sys.excepthook`, which Qt also feeds
    with errors from its slots) and in other threads (`threading.excepthook`), then tell `on_error`
    a short description so the app can say something friendly. Returns a function that puts the
    old hooks back."""
    old_main, old_thread = sys.excepthook, threading.excepthook

    def handle(exc_type, exc, tb, where: str = "") -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
        try:
            crash_log.critical("Unexpected error%s:\n%s", where, text)
        except Exception:
            pass
        if on_error is not None:
            try:
                on_error(f"{exc_type.__name__}: {exc}" if str(exc) else exc_type.__name__)
            except Exception:
                pass

    def main_hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            old_main(exc_type, exc, tb)
            return
        handle(exc_type, exc, tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread else "unknown"
        handle(args.exc_type, args.exc_value, args.exc_traceback, f" in thread {name}")

    sys.excepthook, threading.excepthook = main_hook, thread_hook

    def restore() -> None:
        sys.excepthook, threading.excepthook = old_main, old_thread

    return restore


# ----- "Report a problem" ---------------------------------------------------------

REPORT_TITLE = f"Problem with {APP_NAME} {__version__}"


def report_body(system: list[str], log_lines: list[str]) -> str:
    """The text that starts a GitHub issue: a place to describe the problem, this PC, and the end of the log."""
    details = "\n".join(f"- {line}" for line in system)
    shown = "\n".join(line[:MAX_LINE].replace("```", "'''") for line in log_lines) or "(the log is empty)"
    return ("**What happened?**\n"
            "(Please say what you were doing, and what you expected.)\n\n"
            f"**About this PC**\n{details}\n\n"
            f"**End of the log**\n```\n{shown}\n```\n")


def issue_url(system: list[str], log_lines: list[str], limit: int = URL_LIMIT) -> str:
    """The link that opens GitHub's "new issue" page with the report filled in. The oldest log
    lines are left out as needed to keep the link under `limit` characters."""
    def build(lines: list[str], details: list[str]) -> str:
        return f"{ISSUES_URL}?title={quote(REPORT_TITLE, safe='')}&body={quote(report_body(details, lines), safe='')}"

    lines, details = list(log_lines), list(system)
    url = build(lines, details)
    while len(url) > limit and lines:
        del lines[0]
        url = build(lines, details)
    while len(url) > limit and details:      # only a very long description of this PC could get here
        details[-1] = details[-1][:max(len(details[-1]) // 2, 0)]
        if not details[-1]:
            details.pop()
        url = build(lines, details)
    return url


# ----- About ----------------------------------------------------------------------

LICENSES_URL = f"{REPO_URL}/tree/main/licenses"


def licenses_folder() -> Optional[Path]:
    """The folder with the license texts and third-party notices that ship with the app: next to the program
    when it was installed, inside the bundle for the portable single file, the repository's own folder when run
    from source. None if it can't be found."""
    if getattr(sys, "frozen", False):
        candidates = [Path(sys.executable).resolve().parent / "licenses"]
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            candidates.append(Path(bundle) / "licenses")
    else:
        candidates = [Path(__file__).resolve().parent.parent / "licenses"]
    for folder in candidates:
        if folder.is_dir():
            return folder
    return None


def licenses_link() -> str:
    """Where the About box's "Third-party licenses" link goes: the local folder, or the one on GitHub."""
    folder = licenses_folder()
    return folder.as_uri() if folder is not None else LICENSES_URL


def about_text() -> str:
    """The About box's text, as HTML for a Qt label."""
    return (f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>" + tr("A free video editor for your PC: reshape videos for phones, crop, cut, join, speed up, "
                       "color and stabilize them, or turn them into MP3s. Everything happens on your "
                       "computer.") + "</p>"
            "<p>" + tr("Released under the MIT License.") + "<br>"
            + tr("Source code and help: {link}", link=f'<a href="{REPO_URL}">{REPO_URL}</a>') + "<br>"
            + f'<a href="{licenses_link()}">' + tr("Third-party licenses") + "</a></p>"
            "<p>" + tr("Built with {ffmpeg} for all the video work and {qt} (PySide6) for the window. "
                       "Thanks to everyone who makes them.",
                       ffmpeg='<a href="https://ffmpeg.org">FFmpeg</a>',
                       qt='<a href="https://www.qt.io/qt-for-python">Qt for Python</a>') + "</p>")
