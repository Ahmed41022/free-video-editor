"""Reading a subtitle file whatever its encoding."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Optional

# Tried in this order; the first of equal scores wins, so plain Western text stays Windows-1252.
LEGACY_CODEPAGES = ("cp1252", "cp1256", "cp1255", "cp1251", "cp1253", "cp1254")
# The most used letters of a language: text decoded in the right code page is full of them, text decoded in the wrong one is not.
_COMMON_LETTERS = {
    "ARABIC": set("اليمونرتبةهعدسىأ"),
    "HEBREW": set("יוהלארבתמשנע"),
    "CYRILLIC": set("оеаинтсрвлкмдпуя"),
    "GREEK": set("αοιενταςσρκπυμηλ"),
}
_HEBREW_FINAL = set("ךםןףץ")                  # these only ever end a word
_TURKISH_BYTES = bytes([0xD0, 0xF0, 0xDD, 0xFD, 0xDE, 0xFE])        # Ğ ğ İ ı Ş ş in Windows-1254 (Ð ð Ý ý Þ þ in 1252)


def _script(ch: str) -> str:
    try:
        return unicodedata.name(ch).split(" ", 1)[0]
    except ValueError:
        return ""


def code_page_score(text: str) -> float:
    """How much `text` (some bytes read in one code page) looks like real writing, 0..1 (negative when it
    holds control or replacement characters): ASCII-only text is fine; accented Latin letters are a small
    part of a text; other scripts are one script, made of that language's usual letters, with capitals only
    at the start of words."""
    letters = [ch for ch in text if ch.isalpha()]
    # Control and replacement characters never belong in subtitles; the direction marks Windows-1256 turns
    # Turkish ı and ş into (bytes FD and FE) are about as unlikely.
    odd = sum(1 for ch in text if ch in "�‎‏" or "\x80" <= ch <= "\x9f")
    high = [ch for ch in letters if ord(ch) > 127]
    if not high:
        return 1.0 - 10.0 * odd / max(len(text), 1)
    counts: dict[str, int] = {}
    for ch in high:
        counts[_script(ch)] = counts.get(_script(ch), 0) + 1
    script, count = max(counts.items(), key=lambda kv: kv[1])
    purity = count / len(high)
    if script == "LATIN":
        share = len(high) / len(letters)
        plausible = 1.0 if share <= 0.2 else max(0.0, 1.0 - (share - 0.2) * 2.5)
    else:
        same = [ch for ch in high if _script(ch) == script]
        common = _COMMON_LETTERS.get(script)
        plausible = 0.5 if common is None else min(1.0, sum(ch.lower() in common for ch in same) / len(same) / 0.55)
        words = [w for w in re.split(r"\W+", text) if w]
        # A word of Latin and Arabic (or Cyrillic...) letters together ("cafי") means the wrong code page.
        with_script = [w for w in words if any(_script(ch) == script for ch in w)]
        mixed = sum(1 for w in with_script if any(ch.isascii() and ch.isalpha() for ch in w))
        plausible *= 1.0 - mixed / max(len(with_script), 1)
        if script in ("CYRILLIC", "GREEK"):         # a capital in the middle of a word means the wrong code page
            inner = [ch for w in words for ch in w[1:] if _script(ch) == script]
            if inner:
                plausible *= 1.0 - sum(ch.isupper() for ch in inner) / len(inner)
        elif script == "HEBREW":                    # final letters only end words
            misplaced = sum(1 for w in words for ch in w[:-1] if ch in _HEBREW_FINAL)
            plausible *= 1.0 - min(1.0, 4.0 * misplaced / max(len(same), 1))
    return purity * plausible - 10.0 * odd / max(len(text), 1)


def decode_text(data: bytes) -> tuple[str, str]:
    """(the text of subtitle `data`, the encoding it was in): a byte order mark says it outright, a file
    with the NUL bytes of UTF-16 is UTF-16 without one, valid UTF-8 is UTF-8, and anything else is read in
    the Windows code page (Arabic, Hebrew, Cyrillic, Greek, Turkish or Western) that makes the most sense of it."""
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return data.decode("utf-32", errors="replace"), "utf-32"
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace"), "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace"), "utf-16-be"
    if b"\x00" in data[:4096]:
        sample = data[:4096]
        even, odd = sample[0::2].count(0), sample[1::2].count(0)
        half = max(len(sample) // 2, 1)
        for name, nuls, other in (("utf-16-le", odd, even), ("utf-16-be", even, odd)):
            if nuls / half > 0.1 and nuls > 4 * other:
                return data.decode(name, errors="replace"), name
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    best: Optional[tuple[float, str, str]] = None
    for name in LEGACY_CODEPAGES:
        try:
            text = data.decode(name)
        except UnicodeDecodeError:
            continue
        score = code_page_score(text)
        if best is None or score > best[0] + 1e-9:
            best = (score, name, text)
    if best is None:
        return data.decode("cp1252", errors="replace"), "cp1252"
    if best[1] == "cp1252" and any(byte in data for byte in _TURKISH_BYTES):
        try:
            turkish = data.decode("cp1254")
            if code_page_score(turkish) >= best[0] - 1e-9:
                return turkish, "cp1254"       # Ğ ş ı... are there and nothing else tells Icelandic from Turkish
        except UnicodeDecodeError:
            pass
    return best[2], best[1]


def read_text_with_encoding(path: Path) -> tuple[str, str]:
    """(the text of a file, the encoding it was in)."""
    return decode_text(path.read_bytes())


def read_text(path: Path) -> str:
    """The text of a file, whatever Unicode form or Windows code page it is in (see `decode_text`)."""
    return read_text_with_encoding(path)[0]
