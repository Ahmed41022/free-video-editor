"""Measures the settings panel (Inspector) on the real Windows platform, where fonts and the style give the true
widths, and prints the result as JSON. Run by tests/test_i18n.py in a separate process, because the rest of the
test suite runs on Qt's "offscreen" platform, which has no fonts and so cannot say what fits.

Nothing is shown on the screen: the panel is created with Qt.WA_DontShowOnScreen."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "none")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

app = QApplication([])

from video_converter import i18n  # noqa: E402
from video_converter.captions import TextItem  # noqa: E402
from video_converter.project import ClipSettings  # noqa: E402
from video_converter.ui import language, theme  # noqa: E402
from video_converter.video import VideoEdit  # noqa: E402


def states():
    gif = VideoEdit(video_format="gif")
    fit = VideoEdit(target_mb=7.5)
    subs = VideoEdit()
    subs.subtitles.file = "gone.srt"
    text = VideoEdit(texts=[TextItem(text="Hello world", box=True, bold=True, fade_in=1.5, fade_out=1.5)])
    everything = VideoEdit(texts=[TextItem(text="Hello", box=True), TextItem(text="مرحبا", box=True, bold=True)],
                           target_mb=7.5)
    everything.logo.file = "gone.png"
    everything.subtitles.file = "gone.srt"
    return {"default": ClipSettings(), "gif": ClipSettings(edit=gif), "fit a size": ClipSettings(edit=fit),
            "subtitles": ClipSettings(edit=subs), "a text selected": ClipSettings(edit=text),
            "text, logo and subtitles": ClipSettings(edit=everything)}


def measure(code: str, window, name: str, settings: ClipSettings) -> dict:
    panel = window.inspector
    panel.set_settings(settings, 10.0)
    for section in [*panel.sections, panel.export_section]:
        section.header.setChecked(True)
    app.processEvents()
    content, viewport = panel.widget(), panel.viewport().width()
    outside = []
    for widget in content.findChildren(QWidget):
        if widget.isVisibleTo(content) and widget.width() > 0:
            left = widget.mapTo(content, widget.rect().topLeft()).x()
            right = widget.mapTo(content, widget.rect().topRight()).x()
            if left < 0 or right > viewport:
                outside.append(f"{type(widget).__name__} {getattr(widget, 'text', lambda: '')()!r}"[:60])
    wide = [(section.header.text(), section.minimumSizeHint().width()) for section in panel.sections]
    # What the viewport is when the panel is as narrow as it can be made (a 1024x700 window): the minimum width
    # less the frame and the scroll bar, which shows whenever the sections are taller than the window.
    narrowest = panel.minimumWidth() - 2 * panel.frameWidth() - panel.verticalScrollBar().sizeHint().width()
    return {"language": code, "state": name, "window": window.width(), "panel": panel.width(), "viewport": viewport,
            "narrowest": narrowest,
            "needs": content.minimumSizeHint().width(), "content": content.width(),
            "widest": max(wide, key=lambda pair: pair[1]), "outside": outside}


def run(code: str) -> list:
    from video_converter.core import find_ffmpeg
    from video_converter.ui.main_window import MainWindow

    i18n.set_language(code)
    language.apply_to_app(app)
    with tempfile.TemporaryDirectory() as folder,             mock.patch("video_converter.gpu.detect_encoders", return_value=("h264_nvenc",)),             mock.patch("video_converter.gpu.gpu_name", return_value=None):
        window = MainWindow(find_ffmpeg(), QSettings(str(Path(folder) / "probe.ini"), QSettings.Format.IniFormat))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        window.resize(1320, 840)           # the default size
        for _ in range(10):
            app.processEvents()
        results = [measure(code, window, name, settings) for name, settings in states().items()]
        window.close()
        window.loader.shutdown(wait=True)
        window.deleteLater()
        app.processEvents()
    return results


if __name__ == "__main__":
    # One language per process: a second window in the same process can hang inside Qt's Windows multimedia.
    theme.apply_theme(app, "light")
    code = sys.argv[1]
    results = run(code)
    text = json.dumps({"platform": app.platformName(), "results": results}, ensure_ascii=True)
    if len(sys.argv) > 2:               # a file, not a pipe: a helper process the window may leave behind would
        Path(sys.argv[2]).write_text(text, encoding="utf-8")      # keep a pipe open and the reader waiting
    else:
        sys.stdout.write(text + chr(10))
        sys.stdout.flush()
    sys.stdout.flush()
    # Leave at once: closing Qt's Windows multimedia down the usual way can hang this process for good.
    import ctypes

    ctypes.windll.kernel32.TerminateProcess(ctypes.windll.kernel32.GetCurrentProcess(), 0)
