"""The speech models behind automatic captions: which ones there are, where they are kept, and how they
are downloaded and checked.

The models are not part of the program. Each one is fetched once from Hugging Face (the official
Systran/faster-whisper repositories) into %LOCALAPPDATA%\\Free Video Editor\\models\\<name>, and after that
nothing needs the internet. This is the only place the program contacts Hugging Face, and it only ever
talks HTTPS to huggingface.co (or hf.co, the address its downloads are served from).

Every file of every model is pinned here by size and SHA-256 at a fixed revision of its repository. A
download is written to a temporary folder, checked file by file, and only then moved into place, and a
model is checked again before it is loaded, so a damaged, half-finished or swapped file is never used.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import logging
import os
import shutil
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import APP_NAME, __version__
from .core import ConversionCancelled, ConversionError
from .i18n import N_, tr

log = logging.getLogger(__name__)

BASE_URL = "https://huggingface.co"
ALLOWED_HOSTS = ("huggingface.co", "hf.co")      # and their subdomains (the files are served from a CDN of hf.co)
TIMEOUT = 30.0                                   # seconds without an answer before a download gives up
CHUNK = 256 * 1024
SPACE_MARGIN = 64 * 1024 * 1024                  # free space wanted on top of the model itself


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int          # bytes
    sha256: str


@dataclass(frozen=True)
class SpeechModel:
    key: str                       # what the settings and the command line call it
    label: str                     # what the window calls it
    note: str                      # one line about when to choose it
    folder: str                    # the model's folder name on disk
    repo: str                      # the Hugging Face repository
    revision: str                  # the exact revision of it that the hashes belong to
    files: tuple[ModelFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def display_label(self) -> str:
        """The label in the language of the window (`label` and `note` are the English keys)."""
        return tr(self.label)

    @property
    def display_note(self) -> str:
        return tr(self.note)

    @property
    def size_text(self) -> str:
        return f"{round(self.total_bytes / 1_048_576)} MB"

    def url(self, file: ModelFile) -> str:
        return f"{BASE_URL}/{self.repo}/resolve/{self.revision}/{file.name}"


# The three files every model repository shares are identical across sizes; only config.json and model.bin differ.
_TOKENIZER = ModelFile("tokenizer.json", 2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab")
_VOCABULARY = ModelFile("vocabulary.txt", 459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913")

MODELS: dict[str, SpeechModel] = {
    "fast": SpeechModel(
        "fast", N_("Fast"), N_("Quickest. Good for clear speech in English; weaker in other languages."), "tiny",
        "Systran/faster-whisper-tiny", "d90ca5fe260221311c53c58e660288d3deb8d356", (
            ModelFile("config.json", 2249, "a73a28cdfe1c43ccc7202fa333d1f89c202477271407ae9a7f19afa52039cac8"),
            ModelFile("model.bin", 75538270, "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1"),
            _TOKENIZER, _VOCABULARY)),
    "balanced": SpeechModel(
        "balanced", N_("Balanced"), N_("A good mix of speed and accuracy for most videos, in most languages."), "base",
        "Systran/faster-whisper-base", "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66", (
            ModelFile("config.json", 2309, "56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a"),
            ModelFile("model.bin", 145217532, "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"),
            _TOKENIZER, _VOCABULARY)),
    "accurate": SpeechModel(
        "accurate", N_("Accurate"), N_("Slowest, but best with accents, noise and languages like Arabic."), "small",
        "Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120", (
            ModelFile("config.json", 2370, "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828"),
            ModelFile("model.bin", 483546902, "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"),
            _TOKENIZER, _VOCABULARY)),
}
DEFAULT_MODEL = "balanced"

# (fraction of the whole download, bytes so far, bytes in all)
DownloadProgress = Callable[[float, int, int], None]


# ----- where things are --------------------------------------------------------------

def models_folder(root: Optional[Path] = None) -> Path:
    """The folder the models are kept in: `root` when given (the tests', mostly), otherwise
    %LOCALAPPDATA%\\Free Video Editor\\models."""
    if root is not None:
        return Path(root)
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / APP_NAME / "models"


def model_path(model: SpeechModel, root: Optional[Path] = None) -> Path:
    return models_folder(root) / model.folder


def _staging_path(model: SpeechModel, root: Optional[Path]) -> Path:
    return models_folder(root) / (model.folder + ".downloading")


def is_downloaded(model: SpeechModel, root: Optional[Path] = None) -> bool:
    """Whether the model's folder holds every file at its exact size. This is the quick check that the
    window uses; `verify_model` also reads the files through and is what runs before a model is used."""
    folder = model_path(model, root)
    try:
        return all((folder / f.name).is_file() and (folder / f.name).stat().st_size == f.size for f in model.files)
    except OSError:
        return False


def file_sha256(path: Path, cancel_event: Optional[threading.Event] = None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)
            if cancel_event is not None and cancel_event.is_set():
                raise ConversionCancelled()


def verify_model(model: SpeechModel, root: Optional[Path] = None,
                 cancel_event: Optional[threading.Event] = None) -> Path:
    """The model's folder, once every file in it has been read through and matches its pinned size and
    SHA-256. Raises ConversionError (naming the problem) if the model is missing or not intact."""
    folder = model_path(model, root)
    for file in model.files:
        path = folder / file.name
        try:
            size = path.stat().st_size
        except OSError:
            raise ConversionError(tr("The {model} speech model is missing {file}. It has to be downloaded again.",
                                     model=model.display_label, file=file.name)) from None
        if size != file.size or file_sha256(path, cancel_event) != file.sha256:
            raise ConversionError(tr("The {model} speech model is damaged ({file} is not the file that was expected). "
                                 "Delete the folder {folder} and download it again.",
                                 model=model.display_label, file=file.name, folder=folder))
    return folder


# ----- talking to Hugging Face ----------------------------------------------------------

def allowed_url(url: str) -> bool:
    """Whether `url` is an HTTPS address on huggingface.co or hf.co (or one of their subdomains)."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


class _OnlyHuggingFace(urllib.request.HTTPRedirectHandler):
    """Follows redirects (Hugging Face sends the big files to its CDN) but only to allowed addresses."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if not allowed_url(target):
            raise urllib.error.URLError("the download was sent somewhere other than Hugging Face")  # never shown
        return super().redirect_request(req, fp, code, msg, headers, target)


_opener: Optional[urllib.request.OpenerDirector] = None


def open_url(url: str, headers: Optional[dict] = None):
    """Open `url` for reading (HTTPS to Hugging Face only). The caller closes the answer."""
    global _opener
    if not allowed_url(url):
        raise ConversionError(tr("Models are only downloaded over HTTPS from huggingface.co."))
    if _opener is None:
        _opener = urllib.request.build_opener(_OnlyHuggingFace)
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__}", **(headers or {})})
    return _opener.open(request, timeout=TIMEOUT)


Opener = Callable[[str, dict], object]


def _network_problem(exc: BaseException) -> ConversionError:
    if isinstance(exc, urllib.error.HTTPError):
        return ConversionError(tr("Hugging Face answered with an error ({code}) while downloading the speech model. "
                                  "Try again later.", code=exc.code))
    return ConversionError(tr("Couldn't download the speech model. Check that this PC is connected to the internet "
                              "and try again. (This download is needed once; after that, captions work offline.)"))


def _no_space(model: SpeechModel, folder: Path) -> ConversionError:
    return ConversionError(tr("There isn't enough free disk space for the {model} speech model ({size}). "
                              "Free some space on the drive that holds {folder} and try again.",
                              model=model.display_label, size=model.size_text, folder=folder))


def _free_space(folder: Path) -> Optional[int]:
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


# ----- downloading ----------------------------------------------------------------------

def _content_range_start(answer) -> Optional[int]:
    header = answer.headers.get("Content-Range") if getattr(answer, "headers", None) else None
    if not header or not header.startswith("bytes "):
        return None
    try:
        return int(header[6:].split("-", 1)[0])
    except ValueError:
        return None


def _stop_if_cancelled(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ConversionCancelled()


def _fetch_file(model: SpeechModel, file: ModelFile, staging: Path, open_: Opener, done_before: int,
                report: Callable[[int], None], cancel_event: Optional[threading.Event]) -> None:
    """Get one file into `staging`, carrying on from a half-finished copy when there is one. The finished
    file is checked against its pinned size and SHA-256 and only then given its real name.

    Each download run has its own `cancel_event`. A run that was cancelled may still be waiting for the
    network when the next run starts on the same folder, so every step that changes a file looks at the
    event first: a cancelled run's late bytes are dropped, never written."""
    final, part = staging / file.name, staging / (file.name + ".part")
    _stop_if_cancelled(cancel_event)
    if final.is_file():
        if final.stat().st_size == file.size and file_sha256(final, cancel_event) == file.sha256:
            report(done_before + file.size)
            return
        final.unlink()
    have = part.stat().st_size if part.is_file() else 0
    if have > file.size:
        part.unlink()
        have = 0
    if have < file.size:
        headers = {"Range": f"bytes={have}-"} if have else {}
        answer = open_(model.url(file), headers)
        try:
            _stop_if_cancelled(cancel_event)
            if have and getattr(answer, "status", 200) == 206 and _content_range_start(answer) == have:
                mode = "ab"
            else:
                have, mode = 0, "wb"         # no resuming (or the server sent the whole file): start again
            with open(part, mode) as out:
                while have < file.size:
                    _stop_if_cancelled(cancel_event)
                    block = answer.read(min(CHUNK, file.size - have))
                    if not block:
                        break
                    _stop_if_cancelled(cancel_event)         # cancelled while waiting for these bytes: drop them
                    out.write(block)
                    have += len(block)
                    report(done_before + have)
                extra = answer.read(1) if have >= file.size else b""
        finally:
            close = getattr(answer, "close", None)
            if close:
                close()
        if have != file.size or extra:
            part.unlink(missing_ok=True)
            raise ConversionError(tr("The download of {file} came out the wrong size. Try again.", file=file.name))
    if file_sha256(part, cancel_event) != file.sha256:
        _stop_if_cancelled(cancel_event)
        part.unlink(missing_ok=True)
        raise ConversionError(tr("The downloaded {file} is damaged (its checksum doesn't match). Try again.",
                                 file=file.name))
    _stop_if_cancelled(cancel_event)
    part.replace(final)


def download_model(model: SpeechModel, root: Optional[Path] = None, on_progress: Optional[DownloadProgress] = None,
                   cancel_event: Optional[threading.Event] = None, open_: Optional[Opener] = None,
                   recheck: bool = True) -> Path:
    """Download `model` into its folder and return it. Nothing happens if it is already there and intact
    (`recheck=False` skips that look, for a caller that has just found the model damaged). A damaged model
    is repaired: the files that are fine are kept and only the others are fetched again.

    The files are written to `<name>.downloading` next to the final folder; a download that was
    stopped (cancelled, offline, out of space) carries on from there next time. Raises
    ConversionCancelled when `cancel_event` is set, and ConversionError with a plain message otherwise.
    `open_(url, headers)` replaces the real network (for tests)."""
    open_ = open_ or open_url
    target, staging = model_path(model, root), _staging_path(model, root)
    if recheck and is_downloaded(model, root):
        try:
            return verify_model(model, root, cancel_event)
        except ConversionCancelled:
            raise
        except ConversionError:
            log.warning("The %s model on disk is not intact; downloading it again", model.label)
    if target.is_dir() and not staging.exists():
        try:
            target.replace(staging)      # the files that are fine stay; each one is checked again below
        except OSError:
            pass                         # in use by something: everything is fetched into a fresh folder
    total = model.total_bytes
    present = sum(f.size for f in model.files if (staging / f.name).is_file())
    present += sum((staging / (f.name + ".part")).stat().st_size for f in model.files
                   if not (staging / f.name).is_file() and (staging / (f.name + ".part")).is_file())
    free = _free_space(models_folder(root))
    if free is not None and free < total - present + SPACE_MARGIN:
        raise _no_space(model, models_folder(root))

    def report(done: int) -> None:
        if on_progress:
            on_progress(min(done / total, 1.0), done, total)

    try:
        staging.mkdir(parents=True, exist_ok=True)
        done = 0
        for file in model.files:
            _fetch_file(model, file, staging, open_, done, report, cancel_event)
            done += file.size
        # One more look at everything together, then the folder takes its real name in a single step.
        for file in model.files:
            if (staging / file.name).stat().st_size != file.size:
                raise ConversionError(tr("The downloaded {file} is not the expected size. Try again.", file=file.name))
        _stop_if_cancelled(cancel_event)
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
    except (ConversionCancelled, ConversionError):
        raise
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise _no_space(model, models_folder(root)) from None
        if isinstance(exc, (urllib.error.URLError, socket.timeout, ConnectionError)):
            raise _network_problem(exc) from None
        raise ConversionError(tr("Couldn't save the speech model: {reason}", reason=exc.strerror or exc)) from None
    except (http.client.HTTPException, socket.timeout) as exc:
        raise _network_problem(exc) from None
    log.info("Downloaded the %s speech model to %s", model.label, target)
    if on_progress:
        on_progress(1.0, total, total)
    return target


def models_status(root: Optional[Path] = None) -> Iterator[tuple[SpeechModel, bool]]:
    """Each model with whether it is already downloaded."""
    for model in MODELS.values():
        yield model, is_downloaded(model, root)
