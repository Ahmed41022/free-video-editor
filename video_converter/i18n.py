"""The app's translation layer: the English text is the key, and a language file holds the translations.

    tr("Export")                          -> "تصدير" (Arabic) or "Export" (English)
    tr("Saved as {name}", name=file)      -> placeholders are filled in after translating
    ntr(3, "{n} cut", "{n} cuts")         -> "٣ قصوص" style plurals, with Arabic's six forms
    N_("Export")                          -> marks a string that is translated later (a table of labels)

Why this and not Qt's own tr()/.ts files: the text is produced all over the app, not only in widgets (job
notes, error messages, summaries in the engine modules), and the engine must not depend on Qt. A plain
dictionary per language works everywhere, a string with no entry simply shows in English, and the tests can
scan the source for every key and compare the placeholders (see tests/test_i18n.py). A new string needs only an
entry in `locales/<language>.json`.

The language is chosen once at start-up (a change asks for a restart), so text may be translated at the moment
it is produced. The command line never calls `set_language`, so its output stays English.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# code -> the language's own name, as shown in the View > Language menu.
LANGUAGES = {"en": "English", "ar": "العربية"}
RTL_LANGUAGES = frozenset({"ar"})
SYSTEM = "system"             # the setting's value for "Same as Windows"
SETTING_KEY = "language"      # where the choice is kept in the app's QSettings
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_language = "en"
_table: dict[str, object] = {}


def N_(text: str, /) -> str:
    """Marks `text` for translation without translating it now (for tables of labels; show them with `tr`)."""
    return text


def load_table(code: str) -> dict[str, object]:
    """The translations of `code` ({} for English or a language with no file)."""
    if code == "en":
        return {}
    path = LOCALES_DIR / f"{code}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("No usable translations in %s", path)
        return {}
    return {key: value for key, value in data.items() if not key.startswith("@")} if isinstance(data, dict) else {}


def set_language(code: str) -> str:
    """Switch the language every `tr` call uses. An unknown code means English. Returns the code in use."""
    global _language, _table
    code = code if code in LANGUAGES else "en"
    _table = load_table(code)
    _language = code
    return code


def language() -> str:
    return _language


def is_rtl(code: Optional[str] = None) -> bool:
    """Whether the language (the current one by default) is written right to left."""
    return (code or _language) in RTL_LANGUAGES


def tr(text: str, /, **fmt) -> str:
    """`text` in the current language, with `{name}` placeholders filled from `fmt`."""
    value = _table.get(text, text)
    if isinstance(value, dict):          # a plural entry used without a count
        value = value.get("other", text)
    if not isinstance(value, str):
        value = text
    return value.format(**fmt) if fmt else value


def trc(context: str, text: str, /, **fmt) -> str:
    """`text` for a particular use. English has one word for several things ("Start" a task, the "Start" time;
    "Remove" a file from a list, "Remove" a logo), another language may need two. The translation of the
    use is the entry `"<text>|<context>"`; without one the plain translation of `text` is used."""
    value = _table.get(f"{text}|{context}")
    if not isinstance(value, str):
        return tr(text, **fmt)
    return value.format(**fmt) if fmt else value


# Unicode isolates: a stretch of text between FSI and PDI is laid out on its own, in the direction of its first
# strong letter, and does not reorder the text around it.
FSI, PDI = "⁨", "⁩"


def isolate(text: object) -> str:
    """`text` kept together in a right-to-left interface, so a file name, a number or "MP4" inside an Arabic
    sentence keeps its own order. Unchanged in a left-to-right language."""
    text = str(text)
    return f"{FSI}{text}{PDI}" if text and is_rtl() else text


def join_parts(parts, separator: str = " · ") -> str:
    """A line made of short parts ("MP4 · 9:16 · crop"). In a right-to-left interface every part is isolated, so
    the Latin and numeric ones ("MP4", "9:16", "0:05–0:20") are not shuffled between the Arabic ones."""
    return separator.join(isolate(part) for part in parts)


def plural_category(count: int, code: Optional[str] = None) -> str:
    """CLDR's plural category of a whole number: "zero", "one", "two", "few", "many" or "other"."""
    code = code or _language
    if code == "ar":
        if count == 0:
            return "zero"
        if count == 1:
            return "one"
        if count == 2:
            return "two"
        rest = count % 100
        if 3 <= rest <= 10:
            return "few"
        if 11 <= rest <= 99:
            return "many"
        return "other"
    return "one" if count == 1 else "other"


def ntr(count: int, one: str, other: str, /, **fmt) -> str:
    """A phrase that depends on a count. `one` and `other` are the English singular and plural, with `{n}`
    standing for the number; the translation entry is keyed by `one` and holds a form per plural category
    ("zero", "one", "two", "few", "many", "other"; a missing one falls back to "other")."""
    entry = _table.get(one)
    if isinstance(entry, dict):
        form = entry.get(plural_category(count)) or entry.get("other") or (one if count == 1 else other)
    elif isinstance(entry, str):
        form = entry
    else:
        form = one if count == 1 else other
    return form.format(n=count, **fmt)


# ----- which language to use -----------------------------------------------------------

def system_ui_language() -> str:
    """The primary language code of Windows' display language ("ar", "en", ...), or "" when it is unknown."""
    try:
        if sys.platform == "win32":
            import ctypes

            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return {0x01: "ar", 0x09: "en"}.get(lang_id & 0x3FF, f"other-{lang_id & 0x3FF}")
        import locale

        return (locale.getlocale()[0] or "").split("_")[0].lower()
    except (OSError, AttributeError, ValueError):
        return ""


def system_language() -> str:
    """The app language that follows Windows: Arabic when Windows' display language is Arabic, else English."""
    return "ar" if system_ui_language() == "ar" else "en"


def resolve(choice: object) -> str:
    """The language code to use for a saved choice ("system", "en", "ar"; anything else counts as "system")."""
    if isinstance(choice, str) and choice in LANGUAGES:
        return choice
    return system_language()


def saved_choice(store) -> str:
    """The language choice kept in `store` (a QSettings or anything with `.value`): a code or "system"."""
    value = store.value(SETTING_KEY, SYSTEM)
    return value if isinstance(value, str) and value in LANGUAGES else SYSTEM
