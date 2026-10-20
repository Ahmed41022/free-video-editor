"""Finding the font file for a family name, and measuring text with it.

ffmpeg draws text from a font FILE, but the editor lists font FAMILIES (what a person picks
from a menu). On Windows the registry says which file belongs to which family; `font_file` reads it.
A family that is missing, or that has no glyph for the text (Impact has no Arabic), falls back to a
font that does, so the text never turns into boxes.

The measuring reads just the few tables of a TrueType/OpenType file it needs (character map,
advance widths, line height), so it is the same on every machine and needs no graphics toolkit. It is
only used to wrap long lines and to size the boxes drawn over the preview; ffmpeg does the real drawing.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_FONT = "Arial"
# Tried in this order when the chosen font is missing or can't show the text.
FALLBACK_FONTS = ("Arial", "Segoe UI", "Tahoma", "Microsoft Sans Serif", "Times New Roman",
                  "Microsoft YaHei", "Malgun Gothic", "Yu Gothic", "Segoe UI Symbol", "Segoe UI Emoji")

_FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}
_REGISTRY_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
_STYLE_WORDS = {"bold", "italic", "oblique", "regular"}

_lock = threading.Lock()
_index: Optional[dict[str, dict[tuple[bool, bool], str]]] = None


def windows_fonts_dir() -> Optional[Path]:
    """The folder of installed fonts, or None when this isn't Windows."""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    folder = Path(root) / "Fonts" if root else Path(r"C:\Windows\Fonts")
    return folder if folder.is_dir() else None


def _split_style(name: str) -> tuple[str, bool, bool]:
    """"Arial Bold Italic" -> ("Arial", bold, italic)."""
    words, bold, italic = name.split(), False, False
    while len(words) > 1 and words[-1].lower() in _STYLE_WORDS:
        word = words.pop().lower()
        bold |= word == "bold"
        italic |= word in ("italic", "oblique")
    return " ".join(words), bold, italic


def _registry_entries() -> list[tuple[str, str]]:
    """(font name, file) pairs from the registry's list of installed fonts: the machine's, then the user's."""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    entries = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, _REGISTRY_KEY) as key:
                count = winreg.QueryInfoKey(key)[1]
                for i in range(count):
                    name, value, _ = winreg.EnumValue(key, i)
                    if isinstance(value, str):
                        entries.append((name, value))
        except OSError:
            continue
    return entries


def _build_index() -> dict[str, dict[tuple[bool, bool], str]]:
    """family (lower case) -> {(bold, italic): font file}."""
    folder = windows_fonts_dir()
    index: dict[str, dict[tuple[bool, bool], str]] = {}
    for name, file in _registry_entries():
        path = Path(file) if Path(file).is_absolute() else (folder / file if folder else Path(file))
        if path.suffix.lower() not in _FONT_EXTENSIONS or not path.is_file():
            continue
        # "(TrueType)" says how the font is stored; a collection lists several families: "MS Gothic & MS UI Gothic".
        name = name.rsplit("(", 1)[0].strip()
        for part in name.split(" & ")[:1 if path.suffix.lower() == ".ttc" else None]:
            family, bold, italic = _split_style(part.strip())
            index.setdefault(family.lower(), {}).setdefault((bold, italic), str(path))
    if not index and folder:
        # No registry (or an odd one): the common fonts by their usual file names.
        for family, files in {"arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
                              "segoe ui": ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
                              "tahoma": ("tahoma.ttf", "tahomabd.ttf", None, None)}.items():
            for style, file in zip(((False, False), (True, False), (False, True), (True, True)), files):
                if file and (folder / file).is_file():
                    index.setdefault(family, {})[style] = str(folder / file)
    return index


def installed_fonts() -> dict[str, dict[tuple[bool, bool], str]]:
    """Every font family the registry lists, with its files (built once)."""
    global _index
    with _lock:
        if _index is None:
            _index = _build_index()
        return _index


def forget_installed_fonts() -> None:
    """Read the list of installed fonts again next time (for tests)."""
    global _index
    with _lock:
        _index = None
    load_font.cache_clear()


def font_file(family: str, bold: bool = False, italic: bool = False) -> Optional[str]:
    """The file of `family` in the wanted style, or the closest style it has, or None for an unknown family."""
    styles = installed_fonts().get((family or "").strip().lower())
    if not styles:
        return None
    for wanted in ((bold, italic), (bold, False), (False, italic), (False, False)):
        if wanted in styles:
            return styles[wanted]
    return next(iter(styles.values()))


def pick_font(family: str, bold: bool = False, text: str = "") -> Optional[str]:
    """The font file to draw `text` in: `family`'s own when it has a glyph for every character,
    otherwise the first fallback that has, otherwise whichever covers the most. None when no font
    is known at all (then ffmpeg picks one itself)."""
    candidates: list[str] = []
    for name in (family, *FALLBACK_FONTS):
        path = font_file(name, bold)
        if path and path not in candidates:
            candidates.append(path)
    if not candidates:
        return None
    wanted = {ch for ch in text if not ch.isspace()}
    if not wanted:
        return candidates[0]
    best, best_count = candidates[0], -1
    for path in candidates:
        font = load_font(path)
        count = len(wanted) if font is None else sum(1 for ch in wanted if font.has_char(ord(ch)))
        if count == len(wanted):
            return path
        if count > best_count:
            best, best_count = path, count
    return best


# ----- reading a font file -------------------------------------------------------

@dataclass
class Font:
    """The measurements of one font file."""
    path: str
    units_per_em: int
    ascent: int
    descent: int            # negative, below the baseline
    line_gap: int
    _cmap: list                     # [(format, data)]
    _advances: list[int]
    _glyph_count: int

    def glyph(self, code: int) -> int:
        """The glyph number for a character code, 0 (the "missing" box) when the font lacks it."""
        for kind, data in self._cmap:
            found = _lookup(kind, data, code)
            if found:
                return found
        return 0

    def has_char(self, code: int) -> bool:
        return self.glyph(code) != 0

    def advance(self, code: int) -> float:
        """How far the pen moves after a character, in em (1.0 = the text size)."""
        glyph = self.glyph(code)
        if not self._advances:
            return 0.5
        return self._advances[min(glyph, len(self._advances) - 1)] / self.units_per_em

    @property
    def line_height(self) -> float:
        """The distance between two lines, in em."""
        return (self.ascent - self.descent + self.line_gap) / self.units_per_em

    def width(self, text: str, size: float) -> float:
        # Formatting marks (the right-to-left embedding marks, zero width spaces) take no room.
        return sum(self.advance(ord(ch)) for ch in text if unicodedata.category(ch) != "Cf") * size


def _lookup(kind: int, data, code: int) -> int:
    if kind == 4:
        ends, starts, deltas, offsets, glyphs = data
        lo, hi = 0, len(ends) - 1
        while lo < hi:      # the first segment that ends at or after the code
            mid = (lo + hi) // 2
            if ends[mid] < code:
                lo = mid + 1
            else:
                hi = mid
        if not ends or code > 0xFFFF or ends[lo] < code or starts[lo] > code:
            return 0
        if offsets[lo] == 0:
            return (code + deltas[lo]) & 0xFFFF
        index = offsets[lo] // 2 + (code - starts[lo]) - (len(ends) - lo)
        if 0 <= index < len(glyphs) and glyphs[index]:
            return (glyphs[index] + deltas[lo]) & 0xFFFF
        return 0
    if kind == 12:
        for start, end, first in data:
            if start <= code <= end:
                return first + code - start
        return 0
    return 0


def _parse_cmap(blob: bytes) -> list:
    """The Unicode character maps of a 'cmap' table (formats 4 and 12), the full-range one first."""
    if len(blob) < 4:
        return []
    count = struct.unpack(">H", blob[2:4])[0]
    found = []
    for i in range(count):
        platform, encoding, offset = struct.unpack(">HHL", blob[4 + 8 * i:12 + 8 * i])
        if not ((platform == 3 and encoding in (1, 10)) or platform == 0):
            continue
        kind = struct.unpack(">H", blob[offset:offset + 2])[0]
        try:
            if kind == 4:
                seg_x2 = struct.unpack(">H", blob[offset + 6:offset + 8])[0]
                n = seg_x2 // 2
                base = offset + 14
                ends = struct.unpack(f">{n}H", blob[base:base + seg_x2])
                starts = struct.unpack(f">{n}H", blob[base + seg_x2 + 2:base + 2 * seg_x2 + 2])
                deltas = struct.unpack(f">{n}H", blob[base + 2 * seg_x2 + 2:base + 3 * seg_x2 + 2])
                offsets = struct.unpack(f">{n}H", blob[base + 3 * seg_x2 + 2:base + 4 * seg_x2 + 2])
                length = struct.unpack(">H", blob[offset + 2:offset + 4])[0]
                glyph_start = base + 4 * seg_x2 + 2
                glyph_count = max((offset + length - glyph_start) // 2, 0)
                glyphs = struct.unpack(f">{glyph_count}H", blob[glyph_start:glyph_start + 2 * glyph_count])
                found.append((4, (ends, starts, deltas, offsets, glyphs)))
            elif kind == 12:
                n = struct.unpack(">L", blob[offset + 12:offset + 16])[0]
                groups = [struct.unpack(">LLL", blob[offset + 16 + 12 * g:offset + 28 + 12 * g]) for g in range(n)]
                found.append((12, groups))
        except struct.error:
            continue
    found.sort(key=lambda entry: entry[0] != 12)
    return found


@lru_cache(maxsize=32)
def load_font(path: str) -> Optional[Font]:
    """Read the measurements of a font file, or None when it can't be read."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
            base = 0
            if head[:4] == b"ttcf":                         # a collection: the first font in it
                handle.seek(12)
                base = struct.unpack(">L", handle.read(4))[0]
                handle.seek(base)
                head = handle.read(12)
            tables = {}
            for _ in range(struct.unpack(">H", head[4:6])[0]):
                tag, _, offset, length = struct.unpack(">4sLLL", handle.read(16))
                tables[tag] = (offset, length)

            def table(tag: bytes) -> bytes:
                offset, length = tables[tag]
                handle.seek(offset)
                return handle.read(length)

            units = struct.unpack(">H", table(b"head")[18:20])[0] or 1000
            hhea = table(b"hhea")
            ascent, descent, gap = struct.unpack(">hhh", hhea[4:10])
            metrics = struct.unpack(">H", hhea[34:36])[0]
            glyph_count = struct.unpack(">H", table(b"maxp")[4:6])[0]
            hmtx = table(b"hmtx")
            advances = list(struct.unpack(f">{2 * metrics}H", hmtx[:4 * metrics])[::2])     # each entry: advance, bearing
            if len(advances) < glyph_count:
                advances += [advances[-1]] * (glyph_count - len(advances))     # the rest repeat the last advance
            return Font(path, units, ascent, descent, gap, _parse_cmap(table(b"cmap")), advances, glyph_count)
    except (OSError, KeyError, struct.error, ValueError) as exc:
        log.warning("Couldn't read the font %s: %s", path, exc)
        return None


def wrap_text(text: str, font: Optional[Font], size: float, max_width: float) -> str:
    """`text` with line breaks added so no line is wider than `max_width` pixels at text size `size`.
    Lines the person broke themselves stay; a single word that is too long is left alone. Without a
    font the text is returned as it is."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if font is None or max_width <= 0:
        return text
    lines = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split(" "):
            trial = f"{current} {word}" if current else word
            if current and font.width(trial, size) > max_width:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
    return "\n".join(lines)


def text_size(text: str, font: Optional[Font], size: float) -> tuple[float, float]:
    """About how big `text` is when drawn at `size` pixels: (width, height) of its block."""
    lines = text.split("\n") or [""]
    if font is None:
        return (max(len(line) for line in lines) * size * 0.55, len(lines) * size * 1.2)
    return (max(font.width(line, size) for line in lines), len(lines) * font.line_height * size)
