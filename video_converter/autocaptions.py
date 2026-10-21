"""Automatic captions: listen to a video's speech and write it down as subtitle cues.

The sound is pulled out of the SOURCE file with ffmpeg (16 kHz mono, the trimmed part of it), a Whisper
model (faster-whisper, on the CPU, offline) turns it into words with times, and `build_cues` cuts those
words into short, readable cues. The cues are in the source video's own time, so the subtitle retiming
that already exists (cuts, speed, reverse) keeps them in sync with whatever the edit does later.

Nothing here touches the network except `speech_models.download_model`, the one-time download of the model.
"""

from __future__ import annotations

import gc
import logging
import os
import shutil
import tempfile
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import APP_NAME
from .captions import Cue, write_srt
from .core import ConversionCancelled, ConversionError, run_ffmpeg, unique_output_path
from .i18n import N_, tr
from .speech_models import DEFAULT_MODEL, MODELS, SpeechModel, download_model, is_downloaded, verify_model

log = logging.getLogger(__name__)

# "" asks the model to find out which language is spoken.
LANGUAGES = {
    "": N_("Detect automatically"), "ar": N_("Arabic"), "en": N_("English"), "fr": N_("French"),
    "es": N_("Spanish"), "de": N_("German"), "tr": N_("Turkish"), "hi": N_("Hindi"), "ur": N_("Urdu"),
    "bn": N_("Bengali"), "zh": N_("Chinese"), "nl": N_("Dutch"), "fa": N_("Persian"), "he": N_("Hebrew"),
    "id": N_("Indonesian"), "it": N_("Italian"), "ja": N_("Japanese"), "ko": N_("Korean"), "pl": N_("Polish"),
    "pt": N_("Portuguese"), "ru": N_("Russian"), "uk": N_("Ukrainian"),
}

LINE_LENGTH = 42             # most letters on a line
MAX_LINES = 2
MIN_DURATION = 1.0           # a cue is on screen at least this long (seconds), if the next one allows
MAX_DURATION = 6.0           # ...and at most this long
PAUSE = 0.8                  # a pause in the speech this long always starts a new cue
MIN_GAP = 0.04               # the least time between two cues
SENTENCE_END = ".!?…。！？؟۔"
CLAUSE_END = ",;:،؛、，；："
SAMPLE_RATE = 16000

# Phases reported while working.
PREPARING, DOWNLOADING, LOADING, LISTENING = "prepare", "download", "load", "listen"
Progress = Callable[[str, float, str], None]        # (phase, how far through that phase 0..1, a few words)


@dataclass
class Piece:
    """A word (or a stretch of text) and when it is spoken. `text` keeps its leading space, if any."""
    start: float
    end: float
    text: str


# ----- cutting words into cues -----------------------------------------------------

def _clean(text: str) -> str:
    return " ".join(text.split())


def _ends(text: str, marks: str) -> bool:
    text = text.rstrip().rstrip("\"'”’»)]")
    return bool(text) and text[-1] in marks


def wrap_lines(text: str, width: int = LINE_LENGTH) -> Optional[list[str]]:
    """`text` as one line, or as two lines no longer than `width` and as even as can be (preferring to
    break after a comma or a full stop). None if it can't be done in two lines. Text with no spaces
    (Chinese, Japanese) is broken between letters."""
    text = _clean(text)
    if len(text) <= width:
        return [text] if text else []
    if " " in text:
        cuts = [(i, i + 1) for i, ch in enumerate(text) if ch == " "]
    else:
        cuts = [(i, i) for i in range(1, len(text))]
    best, best_score = None, None
    for end, resume in cuts:
        first, second = text[:end].rstrip(), text[resume:].lstrip()
        if not first or not second or len(first) > width or len(second) > width:
            continue
        score = abs(len(first) - len(second)) - (8 if _ends(first, CLAUSE_END + SENTENCE_END) else 0)
        if best_score is None or score < best_score:
            best, best_score = [first, second], score
    return best


def _hard_wrap(text: str, width: int = LINE_LENGTH) -> list[str]:
    """Lines of at most `width` letters for text that two lines can't hold (a very long word)."""
    lines, line = [], ""
    for word in _clean(text).split(" "):
        while len(word) > width:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:width])
            word = word[width:]
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = ""
        line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines


def _text_of(pieces: Iterable[Piece]) -> str:
    return _clean("".join(p.text for p in pieces))


def _fits(pieces: list[Piece]) -> bool:
    return wrap_lines(_text_of(pieces)) is not None and pieces[-1].end - pieces[0].start <= MAX_DURATION


def _split_point(pieces: list[Piece]) -> Optional[int]:
    """Where in `pieces` to break the cue that has grown too big: after the last full stop or comma that
    leaves a sensible first part. None if there is no such place."""
    for k in range(len(pieces) - 2, -1, -1):
        if _ends(pieces[k].text, SENTENCE_END + CLAUSE_END) and len(_text_of(pieces[:k + 1])) >= 10:
            return k
    return None


def split_text(start: float, end: float, text: str) -> list[Piece]:
    """Pieces for a stretch of speech that came without word times: its words, spread over the time by length."""
    text = text.strip()
    if not text:
        return []
    tokens = text.split()
    glue = " "
    if len(tokens) == 1 and len(text) > LINE_LENGTH:        # no spaces to go by: break between letters
        tokens, glue = list(text), ""
    weights = [len(t) + len(glue) for t in tokens]
    scale = max(end - start, 0.0) / sum(weights)
    pieces, at = [], start
    for token, weight in zip(tokens, weights):
        pieces.append(Piece(at, at + weight * scale, glue + token))
        at += weight * scale
    return pieces


def pieces_from_segments(segments: Iterable) -> list[Piece]:
    """The words of Whisper's segments (each with `start`, `end`, `text` and, when asked for, `words`)."""
    pieces: list[Piece] = []
    for segment in segments:
        words = [w for w in (getattr(segment, "words", None) or []) if str(w.word).strip()]
        if words:
            pieces += [Piece(float(w.start), max(float(w.end), float(w.start)), str(w.word)) for w in words]
        else:
            pieces += split_text(float(segment.start), float(segment.end), str(segment.text))
    return pieces


def build_cues(pieces: Iterable[Piece], offset: float = 0.0, limit: Optional[float] = None) -> list[Cue]:
    """Cut words into cues of at most two lines of 42 letters, between 1 and 6 seconds long where the
    speech allows, breaking at pauses and punctuation. Times are moved by `offset` seconds, and cues
    are kept inside 0..`limit` (when given). The cues never overlap."""
    pieces = sorted(pieces, key=lambda p: (p.start, p.end))
    groups: list[list[Piece]] = []
    current: list[Piece] = []

    def flush(upto: int | None = None) -> None:
        nonlocal current
        part = current if upto is None else current[:upto]
        if part:
            groups.append(part)
        current = [] if upto is None else current[upto:]

    for piece in pieces:
        if not piece.text.strip():
            continue
        if current:
            if piece.start - current[-1].end >= PAUSE:
                flush()
            while current and not _fits(current + [piece]):
                k = _split_point(current)
                flush() if k is None else flush(k + 1)
            if current:
                text = _text_of(current)
                lasted = current[-1].end - current[0].start
                if _ends(text, SENTENCE_END) and (lasted >= 1.2 or len(text) >= 20):
                    flush()
                elif _ends(text, CLAUSE_END) and len(text) >= 32:
                    flush()
        current.append(piece)
    flush()

    cues: list[Cue] = []
    for group in groups:
        text = _text_of(group)
        if not any(ch.isalnum() for ch in text):             # only music notes or dots: nothing was said
            continue
        lines = wrap_lines(text) or _hard_wrap(text)
        cues.append(Cue(max(group[0].start, 0.0), max(group[-1].end, group[0].start + 0.1), "\n".join(lines)))
    return _timed(cues, offset, limit)


def _timed(cues: list[Cue], offset: float, limit: Optional[float]) -> list[Cue]:
    """Give the cues sensible times: no overlaps, not too short, not too long, inside the video."""
    for i, cue in enumerate(cues):
        if i and cue.start < cues[i - 1].end:
            previous = cues[i - 1]
            if cue.start - previous.start >= 0.2:
                previous.end = cue.start
            else:
                cue.start = previous.end
                cue.end = max(cue.end, cue.start + 0.1)
    for i, cue in enumerate(cues):
        room = cues[i + 1].start - MIN_GAP if i + 1 < len(cues) else float("inf")
        cue.end = max(cue.end, min(cue.start + MIN_DURATION, room))
        cue.end = min(cue.end, cue.start + MAX_DURATION)
    kept = []
    for cue in cues:
        cue.start, cue.end = round(cue.start + offset, 3), round(cue.end + offset, 3)
        if limit is not None:
            if cue.start >= limit - 0.05:
                continue
            cue.end = min(cue.end, limit)
        kept.append(cue)
    return kept


# ----- the sound -----------------------------------------------------------------------

def extract_audio(ffmpeg: str, source: Path, target: Path, start: float = 0.0, length: Optional[float] = None,
                  on_progress: Optional[Callable[[float], None]] = None,
                  cancel_event: Optional[threading.Event] = None) -> Path:
    """The first sound track of `source` as 16 kHz mono 16-bit WAV: from `start`, `length` seconds long
    (to the end if None). Raises ConversionError if the file has no sound."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(source)]
    if length is not None:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
            "-progress", "pipe:1", "-nostats", str(target)]
    try:
        return run_ffmpeg(cmd, target, length, on_progress, cancel_event)
    except ConversionError as exc:
        if "no audio track" in str(exc) or "matches no streams" in str(exc):
            raise ConversionError(tr("This video has no sound, so there is nothing to make captions from.")) from None
        raise


WAV_BLOCK = 1 << 20          # frames read at a time


def read_wav(path: Path):
    """The samples of a 16-bit mono WAV as a float32 array in -1..1. The file is read in blocks and
    converted into one array that is allocated once, so a long recording takes about twice the size of its WAV
    in memory instead of five times."""
    import numpy as np

    try:
        with wave.open(str(path), "rb") as wav:
            total = min(wav.getnframes(), max(path.stat().st_size // 2, 0))     # a header can promise more than the file has
            audio = np.empty(total, dtype=np.float32)
            done = 0
            while done < total:
                block = wav.readframes(min(WAV_BLOCK, total - done))
                if not block:
                    break
                samples = np.frombuffer(block, dtype="<i2", count=len(block) // 2)
                audio[done:done + samples.size] = samples
                audio[done:done + samples.size] *= np.float32(1.0 / 32768.0)
                done += samples.size
    except (wave.Error, EOFError, OSError) as exc:
        raise ConversionError(tr("Couldn't read the sound of this file ({reason}).", reason=exc)) from None
    return audio if done == total else audio[:done].copy()


# ----- the model -------------------------------------------------------------------------

def load_whisper(folder: Path):
    """A faster-whisper model from the (already verified) `folder`, on the CPU. It never goes online."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ConversionError(tr("The speech engine isn't available in this copy of the program ({reason}).",
                              reason=exc)) from None
    threads = max(1, min(8, os.cpu_count() or 4))
    try:
        return WhisperModel(str(folder), device="cpu", compute_type="int8", cpu_threads=threads,
                            local_files_only=True)
    except Exception as exc:
        log.exception("Couldn't load the speech model")
        raise ConversionError(tr("Couldn't load the speech model: {reason}", reason=exc)) from None


def _cancelled(cancel_event: Optional[threading.Event]) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def transcribe(model, audio, language: str = "", translate: bool = False, seconds: Optional[float] = None,
               on_progress: Optional[Callable[[float], None]] = None,
               cancel_event: Optional[threading.Event] = None) -> tuple[list[Piece], str]:
    """Run `model` over `audio`: (the words with their times, the language that was spoken).

    Speech is found first (the voice activity filter), so music and silence are not "transcribed"; word
    times are asked for so the cues can be cut at the right places. A cancel is noticed before the speech
    is looked for, before and after the engine produces each segment, so it never has to wait for more than
    the one step that is already running."""
    if _cancelled(cancel_event):
        raise ConversionCancelled()
    try:
        segments, info = model.transcribe(
            audio, language=language or None, task="translate" if translate else "transcribe",
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 500}, word_timestamps=True,
            condition_on_previous_text=False)
        if _cancelled(cancel_event):            # the language was being detected while the cancel came
            raise ConversionCancelled()
        found = []
        stream = iter(segments)
        while True:
            if _cancelled(cancel_event):
                raise ConversionCancelled()
            segment = next(stream, None)        # the engine does its work here, one segment at a time
            if segment is None:
                break
            if _cancelled(cancel_event):
                raise ConversionCancelled()
            found.append(segment)
            if on_progress and seconds:
                on_progress(min(float(segment.end) / seconds, 1.0))
        pieces = pieces_from_segments(found)
    except (ConversionCancelled, ConversionError):
        raise
    except Exception as exc:
        log.exception("Transcription failed")
        raise ConversionError(tr("Couldn't make captions: {reason}", reason=exc)) from None
    return pieces, str(getattr(info, "language", "") or language)


# ----- everything together ------------------------------------------------------------------

@dataclass
class CaptionOptions:
    model: str = DEFAULT_MODEL
    language: str = ""                 # a LANGUAGES key; "" = detect
    translate: bool = False            # write the speech down in English
    start: Optional[float] = None      # the part of the source to listen to (the trim); None = all of it
    end: Optional[float] = None


@dataclass
class CaptionResult:
    cues: list[Cue] = field(default_factory=list)       # in the source video's time
    language: str = ""                                  # the language that was spoken, when known


def ensure_model(model: SpeechModel, root: Optional[Path] = None, on_progress: Optional[Progress] = None,
                 cancel_event: Optional[threading.Event] = None, opener=None) -> Path:
    """The folder of an intact `model`: downloaded first if it isn't there yet, and fetched again (just
    what is wrong with it) when its files have the right sizes but fail their checksum."""
    def report(fraction: float, done: int, total: int) -> None:
        if on_progress:
            on_progress(DOWNLOADING, fraction, tr("{done} of {total} MB", done=f"{done / 1_048_576:.0f}",
                                                      total=f"{total / 1_048_576:.0f}"))

    if is_downloaded(model, root):
        if on_progress:
            on_progress(LOADING, 0.0, "")
        try:
            return verify_model(model, root, cancel_event)
        except ConversionCancelled:
            raise
        except ConversionError:
            log.warning("The %s speech model on disk is damaged; downloading it again", model.label)
    download_model(model, root, report, cancel_event, opener, recheck=False)
    if on_progress:
        on_progress(LOADING, 0.0, "")
    return verify_model(model, root, cancel_event)


def _stop_if_cancelled(cancel_event: Optional[threading.Event]) -> None:
    if _cancelled(cancel_event):
        raise ConversionCancelled()


def make_captions(ffmpeg: str, source: Path, options: CaptionOptions, duration: Optional[float] = None,
                  root: Optional[Path] = None, on_progress: Optional[Progress] = None,
                  cancel_event: Optional[threading.Event] = None, loader=load_whisper, opener=None) -> CaptionResult:
    """Make captions for the speech in `source` (the part of it inside the trim), in source time.

    `duration` is the file's length. `loader(folder)` makes the speech model and `opener` replaces the
    network for the model download (both for tests). Raises ConversionCancelled when `cancel_event` is set,
    and ConversionError with a plain-English message when something is wrong. A video with no speech in it
    gives no cues."""
    model = MODELS.get(options.model)
    if model is None:
        raise ConversionError(tr("Unknown accuracy choice: {name}", name=options.model))
    if options.language and options.language not in LANGUAGES:
        raise ConversionError(tr("Unknown language: {name}", name=options.language))
    say = on_progress or (lambda phase, fraction, text: None)
    low = max(options.start or 0.0, 0.0)
    high = min(options.end, duration) if options.end is not None and duration else (options.end or duration)
    length = (high - low) if high is not None else None
    if length is not None and length <= 0:
        raise ConversionError(tr("There is nothing to listen to: the trimmed part of the video is empty."))

    work = Path(tempfile.mkdtemp(prefix="fve-speech-"))
    try:
        say(PREPARING, 0.0, "")
        wav = extract_audio(ffmpeg, source, work / "speech.wav", low, length,
                            lambda f: say(PREPARING, f, ""), cancel_event)
        _stop_if_cancelled(cancel_event)
        audio = read_wav(wav)
        wav.unlink(missing_ok=True)
        _stop_if_cancelled(cancel_event)
        if audio.size == 0:
            raise ConversionError(tr("This video has no sound in the part you chose, so there is nothing to make "
                                     "captions from."))
        if not audio.any():                  # digital silence: nothing to hear, so nothing to download either
            return CaptionResult([], options.language)
        seconds = audio.size / SAMPLE_RATE
        folder = ensure_model(model, root, say, cancel_event, opener)
        _stop_if_cancelled(cancel_event)
        speech_model = loader(folder)
        _stop_if_cancelled(cancel_event)
        say(LISTENING, 0.0, "")
        pieces, language = transcribe(speech_model, audio, options.language, options.translate, seconds,
                                      lambda f: say(LISTENING, f, ""), cancel_event)
        cues = build_cues(pieces, offset=low, limit=high)
        log.info("Captions: %d cues, language %s, model %s%s", len(cues), language or "?", model.label,
                 ", translated to English" if options.translate else "")
        say(LISTENING, 1.0, "")
        return CaptionResult(cues, language)
    finally:
        gc.collect()
        shutil.rmtree(work, ignore_errors=True)


# ----- saving ---------------------------------------------------------------------------------

def captions_file_for(source: Path, output_dir: Optional[Path] = None, overwrite: bool = False) -> Path:
    """Where the captions of `source` are saved: `name.srt` next to it (or in `output_dir`), or
    `name (1).srt`... if that name is taken (unless `overwrite`)."""
    target = (output_dir or source.parent) / f"{source.stem}.srt"
    return target if overwrite else unique_output_path(target)


def save_captions(cues: list[Cue], source: Path, output_dir: Optional[Path] = None, overwrite: bool = False) -> Path:
    """Write `cues` to a new .srt file for `source` and return its path."""
    path = captions_file_for(source, output_dir, overwrite)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_srt(cues, path)
    except OSError as exc:
        raise ConversionError(tr("Couldn't save the captions to {path}: {reason}", path=path,
                                 reason=exc.strerror or exc)) from None
    return path


def fallback_folders() -> list[Path]:
    """Where captions are saved when the folder next to the video can't be written to (a read-only drive, a
    disc, a folder that needs rights): the user's Documents folder, then the temporary folder."""
    home = Path.home()
    documents = home / "Documents"
    return [(documents if documents.is_dir() else home) / APP_NAME, Path(tempfile.gettempdir()) / APP_NAME]


def save_captions_anywhere(cues: list[Cue], source: Path, output_dir: Optional[Path] = None, overwrite: bool = False,
                           fallbacks: Optional[list[Path]] = None, save=None) -> tuple[Path, bool]:
    """`save_captions` (or `save`, which does the same job), but finished work is never thrown away: when
    the file can't be written where it belongs, the first of the `fallbacks` folders that takes it is used.
    Returns (the file, whether a fallback was needed). Raises the first problem only when nowhere works."""
    save = save or save_captions
    try:
        return save(cues, source, output_dir, overwrite), False
    except ConversionError as first:
        for folder in (fallback_folders() if fallbacks is None else fallbacks):
            try:
                return save(cues, source, folder, False), True
            except ConversionError:
                continue
        raise first from None


def self_test() -> str:
    """Check that the speech engine and everything it needs can be loaded in this build (used by
    `--self-test-captions`). Returns a one-line report; raises if something is missing."""
    import ctranslate2
    import numpy as np
    import onnxruntime
    import tokenizers
    from faster_whisper import vad

    ctranslate2.get_supported_compute_types("cpu")
    silent = np.zeros(SAMPLE_RATE * 2, dtype=np.float32)
    found = vad.get_speech_timestamps(silent)           # runs the bundled voice detector through onnxruntime
    if found:
        raise RuntimeError("the voice detector heard speech in silence")
    return (f"ok: ctranslate2 {ctranslate2.__version__}, onnxruntime {onnxruntime.__version__}, "
            f"tokenizers {tokenizers.__version__}")
