"""The Arabic interface: the translation layer, the language files, and the window with Arabic and right-to-left
turned on. The window tests run without a visible window and are skipped when PySide6 isn't installed.

Two scans keep later features honest: every text passed to `tr()`, `ntr()` or `N_()` must have an entry in
locales/ar.json, and no label, tooltip, message or dialog text in the UI code may be left out of `tr()`. A new
feature needs only to wrap its texts and add their translations."""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("VIDEO_EDITOR_NO_GPU_SEARCH", "1")      # a window never searches for graphics cards (see tests/__init__.py)
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    import shiboken6  # noqa: F401
    from PySide6.QtCore import QEvent, QLocale, QSettings, Qt
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QAbstractButton, QAbstractSpinBox, QApplication, QComboBox, QFontComboBox, QGroupBox, QLabel, QLineEdit,
        QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QToolBar, QWidget,
    )
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter import APP_NAME, diagnostics, i18n
from video_converter.core import find_ffmpeg
from video_converter.i18n import N_, ntr, plural_category, tr

FFMPEG = find_ffmpeg()
PACKAGE = Path(__file__).resolve().parent.parent / "video_converter"
AR_FILE = PACKAGE / "locales" / "ar.json"
ARABIC_LETTER = re.compile(r"[\u0600-\u06FF]")
PLACEHOLDER = re.compile(r"\{[^{}]*\}")
CATEGORIES = ("zero", "one", "two", "few", "many", "other")

# Words that stay in Latin letters in an Arabic interface: product and brand names, formats, units, key names
# and the commands the ffmpeg message tells people to type.
KEPT_LATIN = re.compile(
    r"(?<![A-Za-z])(?:Qt for Python|TikTok|Reels|Shorts|Stories|YouTube|WhatsApp|Discord|Instagram|Spotify|Apple|GitHub|Windows|Intel|AMD|NVIDIA"
    r"|MP4|MP3|GIF|PNG|JPG|JPEG|srt|vtt|ass|H\.26[45]|WebM|MOV|VP9|AAC|VBR|LUFS|ffmpeg|FFmpeg|libvidstab|Qt|PySide6"
    r"|MIT|CPU|HTML|Ctrl|Shift|Enter|Space|Submit|Mbps|kbps|fps|MB|GB|dB|px|Segoe|UI|winget|install|pip|imageio"
    r"|Gyan|Python|English|High|Hugging Face|Hugging|Face|huggingface|co)(?![A-Za-z])")


def translatable(text: str) -> bool:
    """Whether `text` has words that need translating (not just numbers, units, symbols and brand names)."""
    text = PLACEHOLDER.sub("", text)
    text = re.sub(r"<[^>]*>|https?://[^\s<]+", "", text)
    return bool(re.search(r"[A-Za-z]{2,}", KEPT_LATIN.sub("", text)))


def english_words(text: str, extra: tuple[str, ...] = ()) -> list[str]:
    """The English words left in a text that is meant to be Arabic (brand names, units and `extra` aside)."""
    text = re.sub(r"<[^>]*>|https?://[^\s<]+|'[^']*'|\b(?:h:)?mm?:ss\b", " ", text)    # also what the user typed, and m:ss
    for name in sorted((*extra, APP_NAME), key=len, reverse=True):
        if name:
            text = text.replace(name, " ")
    return re.findall(r"[A-Za-z]{2,}", KEPT_LATIN.sub(" ", text))


# ----- scanning the source ---------------------------------------------------------------------------

def _literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _call_name(node: ast.Call):
    return node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None


def package_files():
    return sorted(p for p in PACKAGE.rglob("*.py"))


def scan_keys():
    """(keys, plurals): every literal text given to tr()/N_() and every (singular, plural) pair given to ntr()."""
    keys, plurals = {}, {}
    for path in package_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            where = f"{path.relative_to(PACKAGE)}:{node.lineno}"
            if name in ("tr", "N_") and node.args and isinstance(node.func, ast.Name):
                text = _literal(node.args[0])
                if text is not None:
                    keys.setdefault(text, []).append(where)
            elif name == "trc" and len(node.args) >= 2 and isinstance(node.func, ast.Name):
                context, text = _literal(node.args[0]), _literal(node.args[1])
                if context is not None and text is not None:
                    keys.setdefault(f"{text}|{context}", []).append(where)     # a use of `text` of its own
                    keys.setdefault(text, []).append(where)                    # and the plain text is the fallback
            elif name == "ntr" and len(node.args) >= 3 and isinstance(node.func, ast.Name):
                one, other = _literal(node.args[1]), _literal(node.args[2])
                if one is not None and other is not None:
                    plurals.setdefault(one, set()).add(other)
    return keys, plurals


def load_ar():
    """ar.json read strictly: a repeated key is an error (json would silently keep the last one)."""
    def no_repeats(pairs):
        names = [k for k, _ in pairs]
        repeated = sorted({k for k in names if names.count(k) > 1})
        if repeated:
            raise ValueError(f"repeated keys in ar.json: {repeated}")
        return dict(pairs)

    return json.loads(AR_FILE.read_text(encoding="utf-8"), object_pairs_hook=no_repeats)


# Calls whose text arguments the user reads. A literal given to one of them must go through tr().
UI_SINKS = {
    "QLabel", "QPushButton", "QCheckBox", "QAction", "QToolBar", "QGroupBox", "QToolButton", "QMessageBox",
    "setText", "setToolTip", "setStatusTip", "setWindowTitle", "setPlaceholderText", "setSpecialValueText",
    "setInformativeText", "setDetailedText", "setTitle", "addAction", "addMenu", "addItem", "addButton", "showMessage",
    "drawText", "question", "information", "warning", "critical", "about", "getText", "getOpenFileName", "getOpenFileNames",
    "getSaveFileName", "getExistingDirectory", "createUndoAction", "createRedoAction", "beginMacro",
    "Section", "_muted", "_heading", "ColorButton",
}


def _unwrapped_text(node) -> bool:
    """Whether an argument is text with words in it that did not go through tr()."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return translatable(node.value)
    if isinstance(node, ast.JoinedStr):        # an f-string: only its fixed parts are words
        return any(isinstance(part, ast.Constant) and translatable(part.value) for part in node.values)
    if isinstance(node, ast.BinOp):
        return _unwrapped_text(node.left) or _unwrapped_text(node.right)
    if isinstance(node, ast.IfExp):
        return _unwrapped_text(node.body) or _unwrapped_text(node.orelse)
    return False


def hard_coded_texts():
    """Texts in the UI code (and engine error messages) that the user would see but that skip tr()."""
    found = []
    for path in package_files():
        relative = path.relative_to(PACKAGE).as_posix()
        in_ui = relative.startswith("ui/")
        if not in_ui and relative in ("i18n.py", "__main__.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if in_ui and name in UI_SINKS and not (name == "row" and isinstance(node.func, ast.Name)):
                arguments = [*node.args, *(k.value for k in node.keywords)]
            elif not in_ui and name == "ConversionError":
                arguments = list(node.args)
            else:
                continue
            for argument in arguments:
                if _unwrapped_text(argument):
                    found.append(f"{relative}:{node.lineno}: {name}({ast.unparse(argument)[:70]})")
    return found


# ----- the translation layer ---------------------------------------------------------------------------

class TranslationLayerTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.set_language, "en")
        i18n.set_language("en")

    def test_english_text_is_the_key_and_the_default(self):
        self.assertEqual(tr("Export"), "Export")
        self.assertEqual(tr("Never heard of this one"), "Never heard of this one")
        self.assertEqual(N_("Export"), "Export")
        self.assertEqual(i18n.language(), "en")
        self.assertFalse(i18n.is_rtl())

    def test_placeholders_are_filled_after_translating(self):
        self.assertEqual(tr("Saved as {name}", name="a.mp4"), "Saved as a.mp4")
        i18n.set_language("ar")
        self.assertEqual(tr("Saved as {name}", name="a.mp4"), "تم الحفظ باسم a.mp4")
        self.assertTrue(i18n.is_rtl())

    def test_a_placeholder_may_be_called_text(self):
        self.assertEqual(tr("Invalid time: {text}", text="'abc'"), "Invalid time: 'abc'")

    def test_numbers_keep_their_western_digits_and_format_specs(self):
        i18n.set_language("ar")
        text = tr("Made smaller ({short}p) so it fits in {limit:g} MB", short=720, limit=10.0)
        self.assertIn("720", text)
        self.assertIn("10", text)
        self.assertEqual(re.findall(r"[\u0660-\u0669]", text), [])        # no Arabic-Indic digits

    def test_an_unknown_language_means_english(self):
        self.assertEqual(i18n.set_language("xx"), "en")
        self.assertEqual(tr("Export"), "Export")

    def test_a_missing_translation_falls_back_to_english(self):
        i18n.set_language("ar")
        self.assertEqual(tr("A text nobody translated"), "A text nobody translated")
        self.assertEqual(tr("A {thing} nobody translated", thing="text"), "A text nobody translated")

    def test_english_plurals(self):
        self.assertEqual(ntr(1, "{n} cut", "{n} cuts"), "1 cut")
        self.assertEqual(ntr(0, "{n} cut", "{n} cuts"), "0 cuts")
        self.assertEqual(ntr(2, "{n} cut", "{n} cuts"), "2 cuts")
        self.assertEqual(ntr(3, "Joining {n} file into one {kind}", "Joining {n} files into one {kind}", kind="MP3"),
                         "Joining 3 files into one MP3")

    def test_arabic_plural_categories(self):
        expected = {0: "zero", 1: "one", 2: "two", 3: "few", 4: "few", 10: "few", 11: "many", 12: "many", 99: "many",
                    100: "other", 101: "other", 102: "other", 103: "few", 110: "few", 111: "many", 200: "other",
                    1000: "other"}
        for count, category in expected.items():
            self.assertEqual(plural_category(count, "ar"), category, count)

    def test_english_plural_categories(self):
        self.assertEqual([plural_category(n, "en") for n in (0, 1, 2, 11)], ["other", "one", "other", "other"])

    def test_arabic_plural_forms(self):
        i18n.set_language("ar")
        forms = {n: ntr(n, "{n} cut", "{n} cuts") for n in (0, 1, 2, 3, 10, 11, 100)}
        self.assertEqual(forms[0], "بدون أجزاء محذوفة")
        self.assertEqual(forms[1], "جزء محذوف")
        self.assertEqual(forms[2], "جزءان محذوفان")
        self.assertEqual(forms[3], "3 أجزاء محذوفة")
        self.assertEqual(forms[10], "10 أجزاء محذوفة")
        self.assertEqual(forms[11], "11 جزءًا محذوفًا")
        self.assertEqual(forms[100], "100 جزء محذوف")
        self.assertEqual(len(set(forms.values())), 7)          # every one of the seven is different

    def test_every_plural_phrase_has_a_form_for_every_category(self):
        for key, entry in load_ar().items():
            if isinstance(entry, dict) and not key.startswith("@"):
                self.assertEqual(sorted(entry), sorted(CATEGORIES), key)

    def test_a_plural_phrase_with_other_placeholders(self):
        i18n.set_language("ar")
        text = ntr(3, "Found {n} pause, {length} in total. The video goes from {before} to {after}.",
                   "Found {n} pauses, {length} in total. The video goes from {before} to {after}.",
                   length="4.2 s", before="1:00", after="0:56")
        for part in ("3", "4.2 s", "1:00", "0:56"):
            self.assertIn(part, text)
        self.assertNotIn("{", text)

    def test_the_command_line_stays_english(self):
        # Only the desktop app picks a language; a script that never calls set_language gets English.
        code = ("from video_converter.core import parse_time\n"
                "try:\n    parse_time('abc')\nexcept ValueError as e:\n    print(e)\n")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                                cwd=PACKAGE.parent, timeout=60)
        self.assertEqual(result.stdout.strip(), "Invalid time: 'abc' (use seconds, m:ss or h:mm:ss)")


class SystemLanguageTests(unittest.TestCase):
    def test_same_as_windows_follows_the_windows_display_language(self):
        for windows, expected in (("ar", "ar"), ("en", "en"), ("fr", "en"), ("", "en"), ("other-7", "en")):
            with mock.patch.object(i18n, "system_ui_language", return_value=windows):
                self.assertEqual(i18n.system_language(), expected, windows)
                self.assertEqual(i18n.resolve(i18n.SYSTEM), expected, windows)

    def test_an_explicit_choice_wins_over_windows(self):
        with mock.patch.object(i18n, "system_ui_language", return_value="ar"):
            self.assertEqual(i18n.resolve("en"), "en")
        with mock.patch.object(i18n, "system_ui_language", return_value="en"):
            self.assertEqual(i18n.resolve("ar"), "ar")

    def test_a_damaged_choice_counts_as_same_as_windows(self):
        with mock.patch.object(i18n, "system_ui_language", return_value="ar"):
            for bad in (None, "", "klingon", 3, ["ar"]):
                self.assertEqual(i18n.resolve(bad), "ar", bad)

    @unittest.skipUnless(sys.platform == "win32", "reads the Windows display language")
    def test_the_windows_language_ids(self):
        import ctypes

        for lang_id, expected in ((0x0401, "ar"), (0x0C01, "ar"), (0x3801, "ar"), (0x0409, "en"), (0x0809, "en"),
                                  (0x040C, "other-12")):
            with mock.patch.object(ctypes.windll.kernel32, "GetUserDefaultUILanguage", return_value=lang_id):
                self.assertEqual(i18n.system_ui_language(), expected, hex(lang_id))

    def test_the_saved_choice(self):
        class Store:
            def __init__(self, value):
                self.value_ = value

            def value(self, key, default=None):
                return self.value_ if self.value_ is not None else default

        self.assertEqual(i18n.saved_choice(Store(None)), "system")
        self.assertEqual(i18n.saved_choice(Store("ar")), "ar")
        self.assertEqual(i18n.saved_choice(Store("en")), "en")
        self.assertEqual(i18n.saved_choice(Store("bogus")), "system")
        self.assertEqual(i18n.saved_choice(Store(7)), "system")


# ----- the language file --------------------------------------------------------------------------------

class ArabicFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = {k: v for k, v in load_ar().items() if not k.startswith("@")}
        cls.keys, cls.plurals = scan_keys()

    def test_the_file_is_valid_and_has_a_glossary(self):
        raw = load_ar()
        self.assertIn("@glossary", raw)
        self.assertIsInstance(raw["@glossary"], dict)
        for key, value in self.table.items():
            if isinstance(value, dict):
                self.assertTrue(all(isinstance(v, str) and v.strip() for v in value.values()), key)
            else:
                self.assertIsInstance(value, str, key)
                self.assertTrue(value.strip(), key)

    def test_the_translations_contain_arabic(self):
        for key, value in self.table.items():
            for text in (value.values() if isinstance(value, dict) else [value]):
                if translatable(key):
                    self.assertRegex(text, ARABIC_LETTER, key)

    def test_every_text_in_the_code_has_a_translation(self):
        missing = sorted(key for key in self.keys if translatable(key) and key not in self.table)
        self.assertEqual(missing, [], "add these to video_converter/locales/ar.json")
        missing = sorted(key for key in self.plurals if key not in self.table)
        self.assertEqual(missing, [], "add these plural phrases to video_converter/locales/ar.json")

    def test_plural_phrases_are_entries_with_every_form(self):
        for one in self.plurals:
            self.assertIsInstance(self.table[one], dict, one)

    def test_the_two_english_forms_of_a_plural_agree_on_their_placeholders(self):
        for one, others in self.plurals.items():
            for other in others:
                self.assertEqual(sorted(PLACEHOLDER.findall(one)), sorted(PLACEHOLDER.findall(other)), one)

    def test_a_translation_keeps_the_placeholders_of_its_text(self):
        for key, value in self.table.items():
            wanted = sorted(PLACEHOLDER.findall(key))
            if isinstance(value, dict):
                for category, text in value.items():
                    shown = sorted(PLACEHOLDER.findall(text))
                    # a plural form may leave out the number ("a cut") but never invent a placeholder
                    allowed = sorted({*wanted, "{n}"})
                    self.assertTrue(set(shown) <= set(allowed), f"{key} [{category}]: {shown}")
                    self.assertTrue(set(wanted) - {"{n}"} <= set(shown) or category == "zero", f"{key} [{category}]")
            else:
                self.assertEqual(sorted(PLACEHOLDER.findall(value)), wanted, key)

    def test_every_translation_formats_without_error(self):
        i18n.set_language("ar")
        self.addCleanup(i18n.set_language, "en")
        values = {"n": 3, "size": 10.5, "limit": 10.0, "rate": 1.5, "short": 720, "percent": 0.5, "fps": 29.97,
                  "speed": 2.0, "degrees": 90, "code": 1}
        for key, value in self.table.items():
            names = {name.strip("{}").split(":")[0] for name in PLACEHOLDER.findall(key)}
            args = {name: values.get(name, "x") for name in names}
            if isinstance(value, dict):
                for text in value.values():
                    text.format(**{**args, "n": 3})
            else:
                value.format(**args)

    def test_nothing_in_the_file_is_unused(self):
        unused = sorted(key for key in self.table if key not in self.keys and key not in self.plurals)
        if unused:      # a report, not a failure: old entries are harmless
            warnings.warn(f"{len(unused)} entries in ar.json are not used by the code: {unused}")
        self.assertLess(len(unused), 5, f"clean up ar.json: {unused}")

    def test_no_text_is_left_out_of_tr(self):
        self.assertEqual(hard_coded_texts(), [], "wrap these in tr() and add them to video_converter/locales/ar.json")

    def test_the_scan_notices_an_unwrapped_text(self):
        # The check itself works: texts with words are found, numbers, units, brand names and tr() calls are fine.
        def unwrapped(source):
            return _unwrapped_text(ast.parse(source, mode="eval").body)

        self.assertTrue(unwrapped("'Export the file'"))
        self.assertTrue(unwrapped("f'Saved {name}'"))
        self.assertTrue(unwrapped("'a' if x else 'Choose a folder'"))
        self.assertFalse(unwrapped("tr('Export the file')"))
        self.assertFalse(unwrapped("f'{value:g}×'"))
        self.assertFalse(unwrapped("'1080p'"))
        self.assertFalse(unwrapped("f'{value} fps'"))
        self.assertFalse(unwrapped("'MP4 · GIF'"))
        self.assertFalse(unwrapped("'9:16'"))

    def test_the_scan_notices_text_painted_on_a_widget(self):
        # drawText(rect, flags, "No sound") is a text the user reads, like a label's.
        call = ast.parse("p.drawText(rect, Qt.AlignCenter, 'No sound')", mode="eval").body
        self.assertIn(_call_name(call), UI_SINKS)
        self.assertTrue(any(_unwrapped_text(argument) for argument in call.args))
        call = ast.parse("p.drawText(rect, Qt.AlignCenter, tr('No sound'))", mode="eval").body
        self.assertFalse(any(_unwrapped_text(argument) for argument in call.args))
        call = ast.parse("p.drawText(rect, flags, format_time(t))", mode="eval").body
        self.assertFalse(any(_unwrapped_text(argument) for argument in call.args))


class MessageTests(unittest.TestCase):
    """The texts the engine produces (job notes, errors, summaries, warnings) come out in Arabic."""

    def setUp(self):
        i18n.set_language("ar")
        self.addCleanup(i18n.set_language, "en")

    def messages(self):
        from video_converter import core, gpu, output, silence, timing, video
        from video_converter.core import MediaInfo
        from video_converter.jobs import Job, JobState
        from video_converter.project import ClipSettings, describe, describe_join
        from video_converter.video import VideoEdit

        edit = VideoEdit(target_mb=10, mute=True, rotate=90, look="warm", texts=[], keep=[(0, 2), (3, 5)], speed=2.0,
                         fade_in=1.0, reverse=True, normalize=True, music="x.mp3", stabilize="light",
                         audio_denoise="light", volume=3.0, start=1.0, end=4.0, frame_rate=60, brightness=0.2)
        edit.logo.file = "logo.png"
        edit.subtitles.file = "a.srt"
        info = MediaInfo(duration=10.0, width=1920, height=1080, fps=120.0)
        job = Job(Path("clip.mp4"), ClipSettings(edit=edit), None, info)
        plan = silence.SilencePlan([(0, 2)], [(2, 3), (4, 5)], 10.0, 8.0)

        def raised(function, *args):
            try:
                function(*args)
            except (ValueError, core.ConversionError) as exc:
                return str(exc)
            raise AssertionError("no error")

        yield describe(ClipSettings(edit=edit), 10.0)
        yield describe(ClipSettings(output="audio"), 10.0)
        yield describe_join(ClipSettings(), 3)
        yield video.frame_rate_hint(MediaInfo(duration=5, fps=120.0))
        yield video.frame_rate_hint(MediaInfo(duration=5, fps=30.0, max_fps=60.0))
        yield video.reverse_warning(5 * 1024 ** 3)
        yield output.gif_warning(60.0)
        yield output.fit_note(output.SizeFit(1, 2500, 128, 10.0, tr("Made smaller ({short}p) so it fits in {limit:g} MB", short=720, limit=10.0)))
        yield output.fit_note(output.SizeFit(1, 500, 128, 10.0))
        yield raised(output.fit_plan, 1_000_000, 600.0, "h264", "aac", 30.0, [(None, 1920, 1080), (360, 640, 360)])
        yield raised(output.fit_plan, 1_000_000, 0.0, "h264", "aac", 30.0, [(None, 1920, 1080)])
        yield raised(core.parse_time, "abc")
        yield raised(core.parse_time, "1:2:3:4")
        yield raised(core.check_trim, 5.0, 2.0)
        yield str(core.no_segments_error())
        yield str(core.music_length_error())
        yield silence.summary(plan)
        yield silence.summary(silence.SilencePlan([], [], 10.0, 10.0))
        yield tr("No supported graphics card found")
        yield gpu.gpu_name("h264_amf")
        yield tr(gpu.CPU_FALLBACK_NOTE)
        for state in JobState:
            yield state.label
        yield Job(Path("clip.mp4"), ClipSettings(), clips=[mock.Mock(), mock.Mock()]).title

    def test_engine_messages_are_in_arabic(self):
        for text in self.messages():
            if translatable(text):
                self.assertRegex(text, ARABIC_LETTER, text)
            self.assertEqual(english_words(text, ("clip.mp4",)), [], text)

    def test_the_labels_the_engine_keeps_in_tables_are_translated(self):
        from video_converter import captions, gpu, output, project, video
        from video_converter.core import QUALITY_PRESETS
        from video_converter.jobs import JobState
        from video_converter.ui import inspector

        labels = [
            *video.FIT_MODES.values(), *video.RESOLUTIONS, *video.FRAME_RATES, *video.VIDEO_QUALITY,
            *(label for label, _ in video.LOOKS.values()),
            *(fmt.label for fmt in output.OUTPUT_FORMATS.values()),
            *(fmt.hint for fmt in output.OUTPUT_FORMATS.values() if fmt.hint),
            *output.TARGET_PRESETS, *QUALITY_PRESETS, *project.PRESETS,
            *captions.ALIGNMENTS.values(), *captions.LOGO_CORNERS.values(), *captions.SUBTITLE_POSITIONS.values(),
            *gpu.LABELS.values(), *(s.value for s in JobState),
            *inspector.ADJUST_LABELS.values(), *inspector.STABILIZE_LABELS.values(), *inspector.NOISE_LABELS.values(),
            *inspector.LOOK_CHIPS.values(), inspector.FIT_LABEL, *(text for text, _ in inspector.SHAPES),
            *inspector.SHAPE_HINTS.values(),
        ]
        for label in labels:
            self.assertEqual(english_words(tr(label)), [], label)

    def test_the_parts_of_a_summary_are_isolated_in_arabic(self):
        from video_converter.i18n import FSI, PDI
        from video_converter.project import ClipSettings, describe, describe_join
        from video_converter.video import VideoEdit

        edit = VideoEdit(keep=[(0, 2), (3, 10)], mute=True, start=1.0, end=4.0)
        parts = [i18n.tr("crop"), i18n.tr("no sound"), "1:00"]
        text = describe(ClipSettings(edit=edit), 10.0)
        pieces = text.split(" · ")
        self.assertGreaterEqual(len(pieces), 5)
        for piece in pieces:                                   # every part sits between FSI and PDI
            self.assertTrue(piece.startswith(FSI) and piece.endswith(PDI), repr(piece))
        self.assertEqual(pieces[0], f"{FSI}MP4{PDI}")          # the Latin and numeric parts keep their own order
        self.assertEqual(pieces[1], f"{FSI}9:16{PDI}")
        times = [p for p in pieces if "–" in p]
        self.assertEqual(len(times), 1)
        self.assertTrue(times[0].startswith(f"{FSI}0:01") and times[0].endswith(f"0:04.0{PDI}"), times)
        self.assertIn(f"{FSI}{parts[0]}{PDI}", pieces)
        self.assertIn(f"{FSI}{parts[1]}{PDI}", pieces)
        for piece in pieces:                                   # nothing else was added to the words
            self.assertEqual(piece.replace(FSI, "").replace(PDI, "").count(FSI), 0)
        self.assertEqual(text.replace(FSI, "").replace(PDI, "").split(" · ")[:2], ["MP4", "9:16"])
        joined = describe_join(ClipSettings(), 3)
        self.assertTrue(all(p.startswith(FSI) and p.endswith(PDI) for p in joined.split(" · ")), joined)
        self.assertEqual(i18n.isolate("a.mp4"), f"{FSI}a.mp4{PDI}")
        self.assertEqual(i18n.isolate(""), "")

    def test_nothing_is_isolated_in_english(self):
        from video_converter.i18n import FSI, PDI

        i18n.set_language("en")
        self.assertEqual(i18n.isolate("a.mp4"), "a.mp4")
        self.assertEqual(i18n.join_parts(["MP4", "9:16"]), "MP4 · 9:16")
        self.assertNotIn(FSI, i18n.join_parts(["MP4", "9:16"]))
        self.assertNotIn(PDI, i18n.join_parts(["MP4", "9:16"]))

    def test_in_english_the_texts_are_unchanged(self):
        i18n.set_language("en")
        from video_converter.project import ClipSettings, describe
        from video_converter.video import VideoEdit

        edit = VideoEdit(keep=[(0, 2), (3, 10)], mute=True)
        self.assertEqual(describe(ClipSettings(edit=edit), 10.0), "MP4 · 9:16 · crop · no sound · 1 cut")


# ----- the window -----------------------------------------------------------------------------------------

def setUpModule():
    if QApplication is None:
        raise unittest.SkipTest("PySide6 is not installed")
    global app
    app = QApplication.instance() or QApplication([])


def pump(seconds, until=lambda: False):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        QApplication.processEvents()
        time.sleep(0.01)
    return until()


def make_video(path, seconds=3):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=duration={seconds}:size=320x180:rate=15", "-f", "lavfi", "-i", f"sine=duration={seconds}",
                    "-c:a", "aac", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)], check=True)


def use_language(test, code):
    """Switch the whole app to `code` for the length of one test."""
    from video_converter.ui import language

    i18n.set_language(code)
    language.apply_to_app(app)

    def restore():
        i18n.set_language("en")
        language.apply_to_app(app)

    test.addCleanup(restore)


def visible_texts(root):
    """Every text the user can read in `root` and below, as (what, text) pairs: labels, buttons, tooltips,
    placeholders, spin box words, combo box items, menus and actions, window and group titles."""
    seen = set()

    def add(where, text):
        if isinstance(text, str) and text.strip():
            seen.add((where, text))

    def menu_texts(menu):
        add("menu", menu.title())
        for action in menu.actions():
            add("action", action.text())
            add("action tip", action.toolTip())
            add("action status", action.statusTip())
            if action.menu() is not None:
                menu_texts(action.menu())

    widgets = [root, *root.findChildren(QWidget)]
    for widget in widgets:
        name = type(widget).__name__
        add(f"{name} tooltip", widget.toolTip())
        add(f"{name} status tip", widget.statusTip())
        if widget.isWindow():
            add(f"{name} title", widget.windowTitle())
        if isinstance(widget, QLabel):
            add(f"{name} text", widget.text())
        if isinstance(widget, QAbstractButton):
            add(f"{name} text", widget.text())
        if isinstance(widget, (QLineEdit, QPlainTextEdit)):
            add(f"{name} placeholder", widget.placeholderText())
        if isinstance(widget, QAbstractSpinBox):
            if hasattr(widget, "specialValueText"):
                add(f"{name} special", widget.specialValueText())
        if isinstance(widget, QGroupBox):
            add(f"{name} title", widget.title())
        if isinstance(widget, QProgressBar):
            add(f"{name} format", widget.format())
        if isinstance(widget, QComboBox) and not isinstance(widget, QFontComboBox):
            for i in range(widget.count()):
                add(f"{name} item", widget.itemText(i))
        if isinstance(widget, QToolBar):
            add("toolbar title", widget.windowTitle())
        if isinstance(widget, QMenu):
            menu_texts(widget)
    for action in root.findChildren(QAction):
        add("action", action.text())
        add("action tip", action.toolTip())
        if action.menu() is not None:
            menu_texts(action.menu())
    menu_bar = getattr(root, "menuBar", None)
    if menu_bar is not None:
        for action in menu_bar().actions():
            add("menu", action.text())
            if action.menu() is not None:
                menu_texts(action.menu())
    return sorted(seen)


class WindowCase(unittest.TestCase):
    """A main window made fresh for each test, in the language chosen with `use_language`."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.video = cls.tmp / "clip.mp4"
        if FFMPEG:
            make_video(cls.video)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def new_window(self, code="en", load=True):
        from video_converter.ui.main_window import MainWindow

        use_language(self, code)
        self.ini = self.tmp / f"{self.id().split('.')[-1]}.ini"
        with mock.patch("video_converter.gpu.detect_encoders", return_value=("h264_nvenc",)), \
                mock.patch("video_converter.gpu.gpu_name", return_value=None):
            self.window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        self.window.resize(1300, 820)
        self.window.show()
        if load:
            self.window.add_paths([self.video])
            self.assertTrue(pump(30, lambda: all(not i.loading for i in self.window.items)), "file never loaded")
        return self.window

    def tearDown(self):
        window = getattr(self, "window", None)
        if window is not None:
            tmpdir = window.tmpdir
            window.close()
            window.loader.shutdown(wait=True)
            self.assertFalse(tmpdir.exists(), "the window left its temp folder behind")
            window.deleteLater()
            self.window = None
            QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            pump(0.1)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class ArabicInterfaceTests(WindowCase):
    def settle(self):
        pump(0.3)

    def check_all_arabic(self, root, state, extra=()):
        names = (*extra, self.video.name, self.video.stem, str(self.tmp), "gone.mp3", "gone.png", "gone.srt")
        left = [(where, text) for where, text in visible_texts(root)
                if english_words(text, names) and not text.endswith(".mp4")]
        self.assertEqual(left, [], f"still English ({state})")

    def test_no_visible_text_is_left_in_english(self):
        from video_converter.project import OUTPUT_AUDIO, ClipSettings
        from video_converter.video import VideoEdit

        window = self.new_window("ar")
        self.settle()
        item = window.current
        self.assertIsNotNone(item)
        self.check_all_arabic(window, "a video is selected")

        edit = VideoEdit(target_mb=10, keep=[(0.5, 1.0), (1.5, 2.5)], reverse=True, stabilize="strong", mute=False,
                         music=str(self.tmp / "gone.mp3"), normalize=True, audio_denoise="light",
                         speed=2.0, fade_in=1.0, video_format="hevc", aspect=None, frame_rate=60)
        edit.logo.file = str(self.tmp / "gone.png")
        edit.subtitles.file = str(self.tmp / "gone.srt")
        edit.texts = [mock_text(), mock_text()]
        states = [("with edits", ClipSettings(edit=edit)),
                  ("as a GIF", ClipSettings(edit=VideoEdit(video_format="gif"))),
                  ("a custom size", ClipSettings(edit=VideoEdit(target_mb=7.5))),
                  ("as audio", ClipSettings(output=OUTPUT_AUDIO))]
        for state, settings in states:
            window.apply_settings(item, settings)
            self.settle()
            self.check_all_arabic(window, state)
            self.check_all_arabic(window.statusBar(), state)

        window.set_language_choice  # the Language menu exists
        for message in window.statusBar().findChildren(QLabel):
            self.assertEqual(english_words(message.text(), (self.video.name,)), [])

    def test_messages_in_the_status_bar_and_boxes_are_in_arabic(self):
        window = self.new_window("ar")
        shown = []
        bar = window.statusBar()
        with mock.patch.object(type(bar), "showMessage", lambda _bar, text, *_: shown.append(text)):
            window.apply_to_all()                                  # one file: nothing to copy to
            window.join_clips()                                    # one file: nothing to join
            window.cut_at("split", 0.01)                           # too close to the start
            window.cut_at("remove", 1.0)                           # nothing to remove yet
            window.trim_at_playhead("end")                         # the playhead is at the start
            window.add_paths([self.tmp])                           # the folder holds only this file
            window._on_frame_saved("", "a problem")
            window._on_frame_saved(str(self.tmp / "frame.png"), "")
            window._on_proxy_done(window.current, None, "")
            window._on_player_error("Resource error")
        self.assertGreaterEqual(len(shown), 8)
        for text in shown:
            if "Resource error" in text or "a problem" in text:
                text = text.replace("Resource error", "").replace("a problem", "")
            self.assertRegex(text, ARABIC_LETTER, text)
            self.assertEqual(english_words(text, (self.video.name, "frame.png", "png")), [], text)

    def test_the_dialogs_and_boxes_are_in_arabic(self):
        from video_converter.jobs import Job, JobState
        from video_converter.project import ClipSettings
        from video_converter.ui.app import crash_box
        from video_converter.ui.queue_panel import JobRow, QueuePanel, details_box, status_text
        from video_converter.ui.silence_dialog import SilenceDialog

        window = self.new_window("ar")
        boxes = [crash_box("ValueError: bad")]
        job = Job(self.video, ClipSettings())
        job.state, job.error = JobState.FAILED, "ffmpeg said no\nlast line"
        boxes.append(details_box(job))
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question, \
                mock.patch.object(QMessageBox, "about") as about:
            window.report_problem()
            window.show_about()
            window._confirm_heavy([Job(self.video, ClipSettings(), info=window.current.info)])
        texts = [str(a) for call in (*question.call_args_list, *about.call_args_list) for a in call.args[1:]]
        for box in boxes:
            texts += [box.text(), box.informativeText(), box.detailedText(), box.windowTitle(),
                      *(b.text() for b in box.buttons())]
            box.deleteLater()
        for text in texts:
            self.assertEqual(english_words(text, (self.video.name, "ValueError", "bad", "ffmpeg said no", "last line",
                                                  "Traceback", "log", "logs", str(diagnostics.log_path()))), [], text)
        # the queue row and its summary
        panel = QueuePanel(window.queue, window.bridge)
        self.addCleanup(panel.deleteLater)
        for state in (JobState.QUEUED, JobState.DONE, JobState.FAILED, JobState.CANCELLED, JobState.RUNNING):
            job.state, job.output, job.note, job.progress = state, Path("clip (phone).mp4"), "", 0.5
            job.started_at = time.monotonic() - 5
            row = JobRow(job)
            self.addCleanup(row.deleteLater)
            texts = [status_text(job), *(w for _, w in visible_texts(row))]
            for text in texts:
                self.assertEqual(english_words(text, ("clip", "phone", "mp4", "ffmpeg said no", "last line")), [], text)
        self.check_all_arabic(panel, "the queue")

        dialog = SilenceDialog(FFMPEG, self.video, 3.0, [], None, None, window)
        self.assertTrue(pump(30, lambda: dialog.silences is not None or dialog.summary_label.objectName() == "error"))
        self.check_all_arabic(dialog, "the silence dialog")
        dialog.reject()
        dialog.deleteLater()

    def test_the_captions_dialogs_are_in_arabic(self):
        from video_converter.autocaptions import LANGUAGES, CaptionResult
        from video_converter.captions import Cue
        from video_converter.speech_models import MODELS
        from video_converter.ui.auto_captions_dialog import AutoCaptionsDialog
        from video_converter.ui.captions_editor import CaptionsEditor

        window = self.new_window("ar")
        dialog = AutoCaptionsDialog(FFMPEG, self.video, 3.0, None, None, None, window, models_root=self.tmp / "models")
        # no model on disk: every accuracy level says that it needs a download
        self.check_all_arabic(dialog, "the automatic captions window")
        for index in range(dialog.model_box.count()):
            dialog.model_box.setCurrentIndex(index)
            self.check_all_arabic(dialog, "the automatic captions window")
            self.assertRegex(dialog.model_note.text(), ARABIC_LETTER)
        for code, name in LANGUAGES.items():
            self.assertRegex(dialog.language_box.itemText(dialog.language_box.findData(code)), ARABIC_LETTER, name)
        for key, model in MODELS.items():
            self.assertRegex(model.display_label, ARABIC_LETTER, key)
        for phase, fraction, text in (("prepare", 0.2, ""), ("download", 0.3, tr_text("{done} of {total} MB")),
                                      ("load", 0.0, ""), ("listen", 0.4, "")):
            dialog._running, dialog._run = True, 1
            dialog._progressed(1, phase, fraction, text)
            self.check_all_arabic(dialog, f"the automatic captions window ({phase})")
        dialog._running = False
        dialog._worked(1, CaptionResult([]), "")                     # no speech found
        self.assertRegex(dialog.error.text(), ARABIC_LETTER)
        self.check_all_arabic(dialog, "the automatic captions window (no speech)")

        editor = CaptionsEditor([Cue(0.0, 1.5, "hello"), Cue(2.0, 3.0, "world")], window)
        self.check_all_arabic(editor, "the captions editor", extra=("hello", "world"))
        editor.table.item(0, 0).setText("nonsense")
        self.assertRegex(editor.message.text(), ARABIC_LETTER)
        self.check_all_arabic(editor, "the captions editor (a bad time)", extra=("hello", "world"))
        self.assertRegex(editor.count_label.text(), ARABIC_LETTER)

        panel = window.inspector.captions
        self.check_all_arabic(panel, "the captions panel")

    def test_the_language_menu_has_three_choices(self):
        window = self.new_window("ar", load=False)
        actions = window.language_actions
        self.assertEqual(list(actions), ["system", "en", "ar"])
        self.assertEqual(actions["ar"].text(), "العربية")
        self.assertEqual(actions["en"].text(), "English")
        self.assertEqual(actions["system"].text(), "حسب لغة Windows")
        self.assertTrue(actions["system"].isChecked())        # nothing saved yet: follow Windows
        self.assertEqual(sum(a.isChecked() for a in actions.values()), 1)


def tr_text(key):
    return i18n.tr(key, done="30", total="120")


def mock_text():
    from video_converter.captions import TextItem

    return TextItem(text="مرحبا hello")


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class SettingsPanelFitTests(WindowCase):
    """The settings panel (Inspector) has to fit its width at the window's default size, in every language:
    nothing may be cut off on the right (English) or on the left (Arabic)."""

    def test_the_default_width_is_not_below_the_narrowest(self):
        from video_converter.ui.inspector import Inspector
        from video_converter.ui.main_window import INSPECTOR_WIDTH

        inspector = Inspector()
        self.addCleanup(shiboken6.delete, inspector)
        self.assertGreaterEqual(INSPECTOR_WIDTH, inspector.minimumWidth() + 30)

    @unittest.skipUnless(sys.platform == "win32", "needs the fonts of the real Windows platform")
    def test_every_section_fits_in_english_and_in_arabic(self):
        """Measured in another process on the real platform: the "offscreen" one used by the other tests has no
        fonts, so its widths mean nothing. (The panel is never shown on the screen.)"""
        from video_converter.ui.main_window import INSPECTOR_WIDTH

        probe, results = Path(__file__).with_name("panel_fit_probe.py"), []
        for code in ("en", "ar"):
            answer = self.tmp / f"panel-fit-{code}.json"
            try:
                done = subprocess.run([sys.executable, str(probe), code, str(answer)], timeout=120,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      env={**os.environ, "QT_QPA_PLATFORM": "windows"})
            except subprocess.TimeoutExpired:
                self.skipTest("the real Windows platform did not answer")
            if done.returncode != 0 or not answer.is_file():
                self.skipTest("the real Windows platform is not available here")
            report = json.loads(answer.read_text(encoding="utf-8"))
            if report["platform"] != "windows":
                self.skipTest("the real Windows platform is not available here")
            results += report["results"]
        self.assertEqual({r["language"] for r in results}, {"en", "ar"})
        for r in results:
            where = f"{r['language']} / {r['state']}: widest section {r['widest']}"
            self.assertLessEqual(r["needs"], r["viewport"], f"the settings panel needs {r['needs']} px, has {r['viewport']} ({where})")
            self.assertLessEqual(r["needs"], r["narrowest"],
                                 f"the panel needs {r['needs']} px but only {r['narrowest']} are left when it is "
                                 f"as narrow as it goes, as in a 1024x700 window ({where})")
            self.assertLessEqual(r["content"], r["viewport"], where)
            self.assertAlmostEqual(r["panel"], INSPECTOR_WIDTH, delta=6, msg=f"at the default size ({where})")
            self.assertEqual(r["outside"], [], f"cut off ({where})")


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class RightToLeftTests(WindowCase):
    def test_arabic_turns_the_app_right_to_left_and_english_does_not(self):
        use_language(self, "ar")
        self.assertEqual(QApplication.layoutDirection(), Qt.LayoutDirection.RightToLeft)
        use_language(self, "en")
        self.assertEqual(QApplication.layoutDirection(), Qt.LayoutDirection.LeftToRight)
        use_language(self, "ar")
        self.assertEqual(QApplication.layoutDirection(), Qt.LayoutDirection.RightToLeft)

    def test_time_stays_left_to_right(self):
        window = self.new_window("ar")
        pump(0.3)
        ltr = Qt.LayoutDirection.LeftToRight
        self.assertEqual(window.layoutDirection(), Qt.LayoutDirection.RightToLeft)
        for name in ("timeline", "view", "time_label", "back_button", "play_button", "forward_button",
                     "in_button", "out_button", "compare_button"):
            self.assertEqual(getattr(window, name).layoutDirection(), ltr, name)
        # the back button is left of the forward button, and the timeline's start is its left end
        self.assertLess(window.back_button.mapTo(window, window.back_button.rect().center()).x(),
                        window.forward_button.mapTo(window, window.forward_button.rect().center()).x())
        self.assertLess(window.in_button.x(), window.out_button.x())
        window.timeline.setDuration(10.0)
        window.timeline.resize(800, 120)
        from video_converter.ui.timeline import time_to_x
        self.assertLess(time_to_x(2, 10, 12, 776), time_to_x(8, 10, 12, 776))

    def test_standalone_timeline_and_video_view_ignore_the_language(self):
        from video_converter.ui.player import VideoView
        from video_converter.ui.timeline import Timeline

        use_language(self, "ar")
        for widget in (Timeline(), VideoView()):
            self.addCleanup(widget.deleteLater)
            self.assertEqual(widget.layoutDirection(), Qt.LayoutDirection.LeftToRight)

    def test_the_panels_are_mirrored(self):
        window = self.new_window("ar")
        pump(0.5)
        center = lambda w: w.mapTo(window, w.rect().center()).x()
        # the file list is on the right and the settings are on the left
        self.assertGreater(center(window.media_list), center(window.inspector))
        # inside a section the label is at the right of its control
        section = window.inspector.frame_section
        label, control = section.grid.itemAtPosition(2, 0).widget(), section.grid.itemAtPosition(2, 1).widget()
        self.assertGreater(center(label), center(control))
        # the toolbar starts on the right
        toolbar = window.findChildren(QToolBar)[0]
        buttons = [b for b in toolbar.findChildren(QAbstractButton) if b.isVisible()]
        add_button = next(b for b in buttons if b.text() == window.add_action.text())
        folder_button = next(b for b in buttons if b.text() == window.folder_action.text())
        self.assertGreater(add_button.x(), folder_button.x())

    def test_english_keeps_the_usual_order(self):
        window = self.new_window("en")
        pump(0.5)
        center = lambda w: w.mapTo(window, w.rect().center()).x()
        self.assertLess(center(window.media_list), center(window.inspector))
        section = window.inspector.frame_section
        label, control = section.grid.itemAtPosition(2, 0).widget(), section.grid.itemAtPosition(2, 1).widget()
        self.assertLess(center(label), center(control))

    def test_the_rotate_buttons_keep_left_on_the_left(self):
        window = self.new_window("ar")
        pump(0.3)
        left = next(b for b in window.inspector.findChildren(QPushButton) if b.text().startswith("⟲"))
        right = next(b for b in window.inspector.findChildren(QPushButton) if b.text().startswith("⟳"))
        self.assertLess(left.x(), right.x())

    def test_a_closed_section_points_the_way_the_text_reads(self):
        window = self.new_window("ar", load=False)
        header = window.inspector.captions_section.header          # starts closed
        self.assertFalse(header.isChecked())
        self.assertEqual(header.arrowType(), Qt.ArrowType.LeftArrow)
        header.setChecked(True)
        self.assertEqual(header.arrowType(), Qt.ArrowType.DownArrow)
        use_language(self, "en")
        english = self.new_inspector()
        self.assertEqual(english.captions_section.header.arrowType(), Qt.ArrowType.RightArrow)

    def new_inspector(self):
        from video_converter.ui.inspector import Inspector

        inspector = Inspector()
        self.addCleanup(inspector.deleteLater)
        return inspector

    def test_undo_and_redo_arrows_are_mirrored(self):
        from video_converter.ui import icons

        use_language(self, "en")
        plain = icons.icon("undo").pixmap(20, 20).toImage()
        use_language(self, "ar")
        mirrored = icons.icon("undo").pixmap(20, 20).toImage()
        def difference(a, b):         # how far apart two pictures are, by their see-through values
            return sum(abs(a.pixelColor(x, y).alpha() - b.pixelColor(x, y).alpha())
                       for x in range(a.width()) for y in range(a.height()))

        flipped = plain.mirrored(True, False)
        self.assertLess(difference(mirrored, flipped), difference(mirrored, plain) / 4)
        # the skip buttons belong to the timeline and are never mirrored
        use_language(self, "en")
        back = icons.icon("back").pixmap(20, 20).toImage()
        use_language(self, "ar")
        self.assertEqual(difference(icons.icon("back").pixmap(20, 20).toImage(), back), 0)

    def test_numbers_use_western_digits(self):
        window = self.new_window("ar", load=False)
        self.assertEqual(QLocale().toString(1234.5), "1,234.5")
        window.inspector.set_settings(__import__("video_converter.project", fromlist=["ClipSettings"]).ClipSettings(),
                                      65.5)
        text = window.inspector.speed_box.text() + window.inspector.trim_info.text() + window.time_label.text()
        self.assertEqual(re.findall(r"[\u0660-\u0669\u06F0-\u06F9]", text), [])
        self.assertIn("0:00.0", window.time_label.text())
        self.assertIn("1:05.5", window.inspector.trim_info.text())

    def test_arabic_gets_fonts_that_can_show_it(self):
        from video_converter.ui import language

        use_language(self, "ar")
        families = QApplication.font().families()
        self.assertIn("Segoe UI", families)
        self.assertGreater(len(families), 2)
        self.assertEqual(language.FONT_FAMILIES[0], "Segoe UI")
        use_language(self, "en")
        self.assertNotIn("Noto Naskh Arabic", QApplication.font().families())

    def test_arabic_text_fits_without_clipping_in_the_main_buttons(self):
        window = self.new_window("ar")
        pump(0.5)
        for button in (window.export_button, window.export_all_button, window.join_button, window.compare_button):
            hint = button.sizeHint().width()
            self.assertGreaterEqual(button.width(), min(hint, button.minimumSizeHint().width()), button.text())


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class LanguageSettingTests(WindowCase):
    def test_the_choice_is_saved_and_used_at_the_next_start(self):
        from video_converter.ui import app as app_module

        store = QSettings(str(self.tmp / "choice.ini"), QSettings.Format.IniFormat)
        use_language(self, "en")
        with mock.patch.object(i18n, "system_ui_language", return_value="en"):
            self.assertEqual(app_module.choose_language(store, app), "en")
            store.setValue(i18n.SETTING_KEY, "ar")
            store.sync()
            again = QSettings(str(self.tmp / "choice.ini"), QSettings.Format.IniFormat)
            self.assertEqual(app_module.choose_language(again, app), "ar")
            self.assertEqual(i18n.language(), "ar")
            self.assertEqual(QApplication.layoutDirection(), Qt.LayoutDirection.RightToLeft)
            store.setValue(i18n.SETTING_KEY, "en")
            self.assertEqual(app_module.choose_language(store, app), "en")
            self.assertEqual(QApplication.layoutDirection(), Qt.LayoutDirection.LeftToRight)

    def test_the_default_follows_windows(self):
        from video_converter.ui import app as app_module

        store = QSettings(str(self.tmp / "default.ini"), QSettings.Format.IniFormat)
        use_language(self, "en")
        with mock.patch.object(i18n, "system_ui_language", return_value="ar"):
            self.assertEqual(app_module.choose_language(store, app), "ar")
            self.assertEqual(QApplication.layoutDirection(), Qt.LayoutDirection.RightToLeft)
        with mock.patch.object(i18n, "system_ui_language", return_value="en"):
            self.assertEqual(app_module.choose_language(store, app), "en")
        self.assertIsNone(store.value(i18n.SETTING_KEY))         # following Windows saves nothing

    def test_choosing_a_language_saves_it_and_offers_a_restart(self):
        window = self.new_window("en", load=False)
        with mock.patch.object(type(window), "_ask_restart", return_value=False) as ask, \
                mock.patch.object(type(window), "restart_app") as restart:
            window.language_actions["ar"].trigger()
            self.assertEqual(window.store.value(i18n.SETTING_KEY), "ar")
            ask.assert_called_once()
            restart.assert_not_called()                          # "Later"
            ask.return_value = True
            window.language_actions["system"].trigger()          # follows Windows: English here, no change
            self.assertEqual(window.store.value(i18n.SETTING_KEY), "system")
            with mock.patch.object(i18n, "system_ui_language", return_value="en"):
                window.language_actions["ar"].trigger()
            restart.assert_called_once()                         # "Restart now"

    def test_no_restart_is_offered_when_the_language_does_not_change(self):
        window = self.new_window("en", load=False)
        with mock.patch.object(type(window), "_ask_restart") as ask:
            window.language_actions["en"].trigger()
            with mock.patch.object(i18n, "system_ui_language", return_value="fr"):
                window.language_actions["system"].trigger()
        ask.assert_not_called()
        self.assertEqual(window.store.value(i18n.SETTING_KEY), "system")

    def test_the_restart_message_says_what_happens(self):
        window = self.new_window("en", load=False)
        seen = []

        def answer(box):
            seen.extend([box.text(), box.informativeText(), *(b.text() for b in box.buttons())])
            next(b for b in box.buttons() if b.text() == "Restart now").click()
            return 0

        with mock.patch.object(QMessageBox, "exec", new=answer):
            self.assertTrue(window._ask_restart())
        self.assertIn("The new language is used the next time the app starts.", seen)
        self.assertIn("Later", seen)
        self.assertTrue(any("not kept" in text for text in seen))

        with mock.patch.object(QMessageBox, "exec", new=lambda box: 0):
            self.assertFalse(window._ask_restart())

        use_language(self, "ar")
        arabic = []
        with mock.patch.object(QMessageBox, "exec",
                               new=lambda box: arabic.extend([box.text(), box.informativeText(),
                                                              *(b.text() for b in box.buttons())]) or 0):
            window._ask_restart()
        self.assertEqual([t for t in arabic if english_words(t)], [])
        self.assertIn("إعادة التشغيل الآن", arabic)

    def test_restarting_reopens_the_files(self):
        from video_converter.ui import language

        window = self.new_window("en")
        with mock.patch.object(language, "restart") as restart, mock.patch.object(type(window), "close",
                                                                                   return_value=True):
            window.restart_app()
        restart.assert_called_once_with([str(self.video)])

    def test_a_window_that_stays_open_is_not_restarted(self):
        from video_converter.ui import language

        window = self.new_window("en", load=False)
        with mock.patch.object(language, "restart") as restart, \
                mock.patch.object(type(window.queue), "busy", new_callable=mock.PropertyMock, return_value=True), \
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            window.restart_app()           # exports are running and the user chose to wait
        restart.assert_not_called()

    def test_the_restart_command(self):
        from video_converter.ui import language

        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", "C:/App/Free.exe"):
            program, args, folder = language.restart_command(["a.mp4", "b.mp4"])
        self.assertEqual((program, args), ("C:/App/Free.exe", ["a.mp4", "b.mp4"]))
        program, args, folder = language.restart_command(["a.mp4"])
        self.assertEqual(program, sys.executable)
        self.assertEqual(args[0], "-c")
        self.assertIn("video_converter.ui.app", args[1])
        self.assertEqual(args[-1], "a.mp4")
        self.assertTrue((Path(folder) / "video_converter" / "i18n.py").is_file())

    def test_restart_really_starts_a_program(self):
        # Through the real QProcess, not a stand-in: PySide6 6.12's startDetached() returns a bool where older
        # versions returned (bool, pid), and unpacking it broke every language change in 3.0.0.
        from video_converter.ui import language

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "started"
            code = f"open({str(marker)!r}, 'w').close()"
            with mock.patch.object(language, "restart_command", return_value=(sys.executable, ["-c", code], tmp)):
                self.assertTrue(language.restart([]))
            deadline = time.monotonic() + 30
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "the program was not started")
        with mock.patch.object(language, "restart_command", return_value=("C:/no/such/program.exe", [], tmp)), \
                self.assertLogs("video_converter.ui.language", "ERROR"):
            self.assertFalse(language.restart([]))

    def test_the_arabic_file_ships_with_the_package(self):
        self.assertTrue(AR_FILE.is_file())
        self.assertIn("--add-data", (PACKAGE.parent / "build_exe.py").read_text(encoding="utf-8"))
        self.assertIn("locales", (PACKAGE.parent / "build_exe.py").read_text(encoding="utf-8"))

    def test_existing_windows_are_english_by_default(self):
        window = self.new_window("en")
        self.assertEqual(window.export_button.text(), "Export")
        self.assertEqual(QApplication.layoutDirection(), Qt.LayoutDirection.LeftToRight)
        self.assertEqual(window.inspector.output_section.header.text(), "Output")


if __name__ == "__main__":
    unittest.main()
