"""The Text and logo controls, the draggable overlays on the preview, and the timeline track (2.8).
They run without a visible window and are skipped when PySide6 isn't installed."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    import shiboken6
    from PySide6.QtCore import QEvent, QPoint, QSettings, Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter.captions import Logo, Subtitles, TextItem
from video_converter.core import find_ffmpeg
from video_converter.project import OUTPUT_AUDIO, ClipSettings
from video_converter.video import VideoEdit

FFMPEG = find_ffmpeg()
TIMEOUT = 30


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


def make_video(path, width=320, height=180, seconds=4):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=0x203040:s={width}x{height}:d={seconds}:r=15", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(path)], check=True)


def png(path: Path) -> Path:
    from tests.test_captions import write_png
    write_png(path, 40, 20, lambda x, y: (255, 0, 0, 255))
    return path


class PanelTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.inspector import Inspector
        self.inspector = Inspector()
        self.panel = self.inspector.captions
        self.edits = []
        self.inspector.settingsEdited.connect(lambda s, label, final: self.edits.append((s, label, final)))
        self.selected = []
        self.inspector.textSelected.connect(self.selected.append)
        self.times = []
        self.inspector.textTimeRequested.connect(self.times.append)
        self.show(VideoEdit())

    def tearDown(self):
        self.inspector.close()
        shiboken6.delete(self.inspector)     # free it now: every widget left alive slows each later theme change

    def show(self, edit: VideoEdit, output="video"):
        settings = ClipSettings(edit=edit)
        settings.output = output
        self.inspector.set_settings(settings, 10.0)

    def apply_last(self):
        """Show the settings the last edit produced, like the main window does after it."""
        self.inspector.set_settings(self.edits[-1][0], 10.0)
        return self.edits[-1][0].edit

    def test_the_section_is_there_for_video_and_hidden_for_mp3(self):
        self.assertFalse(self.inspector.captions_section.isHidden())
        self.assertEqual(self.inspector.captions_section.header.text(), "Text and logo")
        self.show(VideoEdit(), OUTPUT_AUDIO)
        self.assertTrue(self.inspector.captions_section.isHidden())

    def test_showing_settings_reports_no_edits(self):
        self.show(VideoEdit(texts=[TextItem(text="a"), TextItem(text="b")], logo=Logo(file="x.png"),
                            subtitles=Subtitles(file="s.srt")))
        self.assertEqual(self.edits, [])

    def test_without_texts_there_is_a_hint_and_no_editor(self):
        self.assertFalse(self.panel.empty_hint.isHidden())
        self.assertTrue(self.panel.editor.isHidden())
        self.assertFalse(self.panel.remove_button.isEnabled())
        self.assertEqual(self.panel.selected, -1)

    def test_add_text_adds_one_and_selects_it(self):
        self.panel.add_button.click()
        settings, label, final = self.edits[-1]
        self.assertEqual((label, final, len(settings.edit.texts)), ("Add text", True, 1))
        self.apply_last()
        self.assertEqual(self.panel.selected, 0)
        self.assertEqual(self.panel.list.count(), 1)
        self.assertFalse(self.panel.editor.isHidden())
        self.panel.add_button.click()
        self.apply_last()
        self.assertEqual((self.panel.selected, self.panel.list.count()), (1, 2))

    def test_remove_and_duplicate(self):
        self.show(VideoEdit(texts=[TextItem(text="one", y=0.3), TextItem(text="two", y=0.5)]))
        self.panel.list.setCurrentRow(0)
        self.panel.duplicate_button.click()
        edit = self.apply_last()
        self.assertEqual([t.text for t in edit.texts], ["one", "one", "two"])
        self.assertAlmostEqual(edit.texts[1].y, 0.37)
        self.assertEqual(self.panel.selected, 1)
        self.panel.remove_button.click()
        edit = self.apply_last()
        self.assertEqual([t.text for t in edit.texts], ["one", "two"])
        self.assertEqual(self.panel.selected, 0)
        self.panel.remove_button.click()
        edit = self.apply_last()
        self.assertEqual([t.text for t in edit.texts], ["two"])

    def test_choosing_a_row_selects_that_text_and_tells_the_preview(self):
        self.show(VideoEdit(texts=[TextItem(text="one"), TextItem(text="two", size=0.1)]))
        self.selected.clear()
        self.panel.list.setCurrentRow(1)
        self.assertEqual(self.selected, [1])
        self.assertEqual(self.panel.size_box.value(), 10.0)
        self.panel.select(0)
        self.assertEqual(self.selected, [1, 0])
        self.assertEqual(self.panel.text_box.toPlainText(), "one")

    def test_each_control_edits_only_its_own_field_of_the_selected_text(self):
        self.show(VideoEdit(texts=[TextItem(text="one"), TextItem(text="two")]))
        self.panel.list.setCurrentRow(1)
        self.edits.clear()
        self.panel.size_box.setValue(12.5)
        self.panel.bold_box.setChecked(True)
        self.panel.outline_box.setValue(15)
        self.panel.box_check.setChecked(True)
        self.panel.box_opacity.setValue(40)
        self.panel.align_group.button(2).click()
        self.panel.fade_in_box.setValue(0.5)
        self.panel.fade_out_box.setValue(1.5)
        edit = self.edits[-1][0].edit
        first, second = edit.texts
        self.assertEqual(first, TextItem(text="one"))
        self.assertEqual((second.size, second.bold, second.outline, second.box, second.box_opacity, second.align,
                          second.fade_in, second.fade_out), (0.125, True, 0.15, True, 0.4, "right", 0.5, 1.5))
        self.assertEqual({label for _, label, _ in self.edits},
                         {"Text size", "Text bold", "Outline", "Text box", "Box opacity", "Text alignment", "Text fade"})
        self.assertTrue(all(final for _, _, final in self.edits))

    def test_colors_come_from_the_color_dialog(self):
        self.show(VideoEdit(texts=[TextItem(text="one")]))
        for button, field in ((self.panel.color_button, "color"), (self.panel.outline_button, "outline_color")):
            with mock.patch("video_converter.ui.captions_panel.QColorDialog.getColor", return_value=QColor("#336699")):
                button.click()
            self.assertEqual(getattr(self.edits[-1][0].edit.texts[0], field), "#336699")
        self.panel.box_check.setChecked(True)
        self.apply_last()
        with mock.patch("video_converter.ui.captions_panel.QColorDialog.getColor", return_value=QColor("#112233")):
            self.panel.box_button.click()
        self.assertEqual(self.edits[-1][0].edit.texts[0].box_color, "#112233")
        count = len(self.edits)
        with mock.patch("video_converter.ui.captions_panel.QColorDialog.getColor", return_value=QColor()):    # cancelled
            self.panel.color_button.click()
        self.assertEqual(len(self.edits), count)

    def test_the_box_controls_wait_for_the_box(self):
        self.show(VideoEdit(texts=[TextItem(text="one", box=False)]))
        self.assertFalse(self.panel.box_button.isEnabled())
        self.assertFalse(self.panel.box_opacity.isEnabled())
        self.show(VideoEdit(texts=[TextItem(text="one", box=True)]))
        self.assertTrue(self.panel.box_button.isEnabled())

    def test_the_position_buttons(self):
        self.show(VideoEdit(texts=[TextItem(text="one", x=0.1, y=0.4)]))
        for button, y in zip(self.panel.position_group.buttons(), (0.12, 0.5, 0.85)):
            button.click()
            text = self.edits[-1][0].edit.texts[0]
            self.assertEqual((text.x, text.y), (0.5, y))

    def test_a_font_choice(self):
        self.show(VideoEdit(texts=[TextItem(text="one")]))
        if self.panel.font_box.count() < 2:
            self.skipTest("this machine has only one font")
        index = next(i for i in range(self.panel.font_box.count()) if self.panel.font_box.itemText(i) != self.panel.font_box.currentText())
        family = self.panel.font_box.itemText(index)
        self.panel.font_box.setCurrentIndex(index)
        self.assertEqual(self.edits[-1][0].edit.texts[0].font, family)
        self.assertEqual(self.edits[-1][1], "Text font")

    def test_typing_is_one_undo_step_that_finishes_after_a_pause(self):
        self.show(VideoEdit(texts=[TextItem(text="one")]))
        for text in ("t", "tw", "two"):
            self.panel.text_box.setPlainText(text)
        steps = [(s.edit.texts[0].text, label, final) for s, label, final in self.edits]
        self.assertEqual(steps, [("t", "Edit text", False), ("tw", "Edit text", False), ("two", "Edit text", False)])
        self.assertTrue(pump(3, lambda: self.edits[-1][2]), "the edit was never finished")
        self.assertEqual(self.edits[-1][0].edit.texts[0].text, "two")
        self.assertEqual(len(self.edits), 4)

    def test_leaving_the_text_box_finishes_the_edit_at_once(self):
        self.show(VideoEdit(texts=[TextItem(text="one")]))
        self.panel.text_box.setPlainText("two")
        self.panel.text_box.leftBox.emit()
        self.assertTrue(self.edits[-1][2])
        count = len(self.edits)
        self.panel.text_box.leftBox.emit()
        self.assertEqual(len(self.edits), count)

    def test_switching_text_finishes_the_typing_of_the_one_before(self):
        self.show(VideoEdit(texts=[TextItem(text="one"), TextItem(text="two")]))
        self.panel.text_box.setPlainText("changed")
        self.panel.list.setCurrentRow(1)
        self.assertTrue(self.edits[-1][2])
        self.assertEqual(self.edits[-1][0].edit.texts[0].text, "changed")

    def test_unicode_text_can_be_typed(self):
        self.show(VideoEdit(texts=[TextItem(text="one")]))
        self.panel.text_box.setPlainText("مرحبا \U0001F600\nline 2")
        self.panel.text_box.leftBox.emit()
        self.assertEqual(self.edits[-1][0].edit.texts[0].text, "مرحبا \U0001F600\nline 2")

    def test_typed_times(self):
        self.show(VideoEdit(texts=[TextItem(text="one")]))
        self.panel.start_edit.setText("0:02")
        self.panel.start_edit.editingFinished.emit()
        self.panel.end_edit.setText("0:05.5")
        self.panel.end_edit.editingFinished.emit()
        text = self.edits[-1][0].edit.texts[0]
        self.assertEqual((text.start, text.end), (2.0, 5.5))
        self.assertEqual(self.edits[-1][1], "Text time")

    def test_an_empty_end_means_to_the_end_of_the_video(self):
        self.show(VideoEdit(texts=[TextItem(text="one", start=1.0, end=4.0)]))
        self.assertEqual((self.panel.start_edit.text(), self.panel.end_edit.text()), ("0:01.0", "0:04.0"))
        self.panel.end_edit.setText("")
        self.panel.end_edit.editingFinished.emit()
        self.assertIsNone(self.edits[-1][0].edit.texts[0].end)
        self.show(VideoEdit(texts=[TextItem(text="one")]))
        self.assertEqual((self.panel.start_edit.text(), self.panel.end_edit.text()), ("", ""))

    def test_bad_times_are_refused_with_a_message(self):
        self.show(VideoEdit(texts=[TextItem(text="one", start=3.0)]))
        for which, value in (("end", "abc"), ("end", "0:02"), ("start", "-1")):
            self.panel.time_error.hide()
            edit = self.panel.end_edit if which == "end" else self.panel.start_edit
            edit.setText(value)
            edit.editingFinished.emit()
            self.assertFalse(self.panel.time_error.isHidden(), value)
        self.assertEqual(self.edits, [])
        self.panel.end_edit.setText("0:04")
        self.panel.end_edit.editingFinished.emit()
        self.assertTrue(self.panel.time_error.isHidden())

    def test_leaving_a_time_box_alone_keeps_the_exact_time(self):
        self.show(VideoEdit(texts=[TextItem(text="one", start=1.2345, end=3.4567)]))
        self.panel.start_edit.editingFinished.emit()
        self.panel.end_edit.editingFinished.emit()
        self.assertEqual(self.edits, [])

    def test_use_playhead_buttons_ask_for_the_playhead(self):
        self.show(VideoEdit(texts=[TextItem(text="one")]))
        buttons = [b for b in self.panel.editor.findChildren(type(self.panel.add_button)) if b.text() == "Use playhead"]
        self.assertEqual(len(buttons), 2)
        for button in buttons:
            button.click()
        self.assertEqual(self.times, ["start", "end"])

    def test_the_logo_controls(self):
        self.assertTrue(self.panel.logo_options.isHidden())
        self.assertFalse(self.panel.logo_remove.isEnabled())
        with tempfile.TemporaryDirectory() as tmp:
            path = png(Path(tmp) / "logo.png")
            with mock.patch("video_converter.ui.captions_panel.QFileDialog.getOpenFileName", return_value=(str(path), "")):
                self.panel.logo_choose.click()
            settings, label, _ = self.edits[-1]
            self.assertEqual((label, settings.edit.logo.file), ("Choose logo", str(path)))
            self.inspector.set_settings(settings, 10.0)
            self.assertFalse(self.panel.logo_options.isHidden())
            self.assertEqual(self.panel.logo_name.text(), "logo.png")
            self.assertTrue(self.panel.logo_missing.isHidden())
            self.panel.corner_box.setCurrentIndex(self.panel.corner_box.findData("top-left"))
            self.panel.logo_size.setValue(30)
            self.panel.logo_opacity.setValue(60)
            self.panel.logo_margin.setValue(5)
            logo = self.edits[-1][0].edit.logo
            self.assertEqual((logo.corner, logo.size, logo.opacity, logo.margin), ("top-left", 0.3, 0.6, 0.05))
            self.panel.logo_remove.click()
            self.assertEqual(self.edits[-1][0].edit.logo.file, "")

    def test_a_cancelled_file_dialog_changes_nothing(self):
        with mock.patch("video_converter.ui.captions_panel.QFileDialog.getOpenFileName", return_value=("", "")):
            self.panel.logo_choose.click()
            self.panel.sub_choose.click()
        self.assertEqual(self.edits, [])

    def test_a_missing_logo_file_is_flagged(self):
        self.show(VideoEdit(logo=Logo(file=str(Path(tempfile.gettempdir()) / "no-such-logo.png"))))
        self.assertFalse(self.panel.logo_missing.isHidden())
        self.assertTrue(self.panel.sub_message.isHidden())

    def test_the_subtitle_controls(self):
        self.assertTrue(self.panel.sub_options.isHidden())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "talk.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
            with mock.patch("video_converter.ui.captions_panel.QFileDialog.getOpenFileName", return_value=(str(path), "")):
                self.panel.sub_choose.click()
            settings, label, _ = self.edits[-1]
            self.assertEqual((label, settings.edit.subtitles.file), ("Choose subtitles", str(path)))
            self.inspector.set_settings(settings, 10.0)
            self.assertFalse(self.panel.sub_options.isHidden())
            self.assertEqual(self.panel.sub_name.text(), "talk.srt")
            self.panel.sub_size.setValue(8)
            self.panel.sub_bold.setChecked(True)
            self.panel.sub_position.setCurrentIndex(self.panel.sub_position.findData("top"))
            with mock.patch("video_converter.ui.captions_panel.QColorDialog.getColor", return_value=QColor("#ffee00")):
                self.panel.sub_color.click()
            sub = self.edits[-1][0].edit.subtitles
            self.assertEqual((sub.size, sub.bold, sub.position, sub.color), (0.08, True, "top", "#ffee00"))
            self.panel.sub_remove.click()
            self.assertEqual(self.edits[-1][0].edit.subtitles.file, "")

    def test_a_missing_subtitle_file_is_flagged(self):
        self.show(VideoEdit(subtitles=Subtitles(file=str(Path(tempfile.gettempdir()) / "gone.srt"))))
        self.assertFalse(self.panel.sub_message.isHidden())
        self.assertTrue(self.panel.logo_missing.isHidden())

    def test_duplicating_a_text_near_the_bottom_puts_the_copy_above_it(self):
        self.show(VideoEdit(texts=[TextItem(text="low", y=0.93), TextItem(text="mid", y=0.5), TextItem(text="edge", y=0.95)]))
        for row, wanted in ((0, 0.86), (1, 0.57), (2, 0.88)):
            self.panel.list.setCurrentRow(row)
            self.panel.duplicate_button.click()
            edit = self.apply_last()
            self.assertAlmostEqual(edit.texts[row + 1].y, wanted, places=3)
            self.assertNotAlmostEqual(edit.texts[row + 1].y, edit.texts[row].y, places=2)      # never stacked on the original
            self.panel.list.setCurrentRow(row + 1)
            self.panel.remove_button.click()
            self.apply_last()

    def test_the_logo_dialog_offers_only_still_pictures(self):
        with mock.patch("video_converter.ui.captions_panel.QFileDialog.getOpenFileName", return_value=("", "")) as dialog:
            self.panel.logo_choose.click()
        filters = dialog.call_args.args[3]
        for extension in ("*.png", "*.jpg", "*.webp", "*.bmp"):
            self.assertIn(extension, filters)
        self.assertNotIn("*.gif", filters)
        self.assertNotIn("All files", filters)

    def test_an_ass_file_can_keep_its_own_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            ass, srt = Path(tmp) / "talk.ass", Path(tmp) / "talk.srt"
            ass.write_text("[Script Info]\n", encoding="utf-8")
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
            self.show(VideoEdit(subtitles=Subtitles(file=str(srt))))
            self.assertTrue(self.panel.sub_own_style.isHidden())                  # only for .ass / .ssa
            self.assertTrue(self.panel.sub_options.isEnabled())
            self.show(VideoEdit(subtitles=Subtitles(file=str(ass))))
            self.assertFalse(self.panel.sub_own_style.isHidden())
            self.assertTrue(self.panel.sub_own_style.isChecked())                 # on by default
            self.assertFalse(self.panel.sub_options.isEnabled())                  # the file decides, not these controls
            self.panel.sub_own_style.click()
            settings, label, _ = self.edits[-1]
            self.assertEqual(label, "Subtitle style")
            self.assertFalse(settings.edit.subtitles.own_style)
            self.inspector.set_settings(settings, 10.0)
            self.assertTrue(self.panel.sub_options.isEnabled())
            self.assertFalse(self.panel.sub_own_style.isChecked())

    def test_the_limit_on_how_many_texts(self):
        from video_converter.captions import MAX_TEXTS
        self.show(VideoEdit(texts=[TextItem(text=str(i)) for i in range(MAX_TEXTS)]))
        self.assertFalse(self.panel.add_button.isEnabled())
        self.assertFalse(self.panel.duplicate_button.isEnabled())

    def test_a_preset_chosen_in_the_inspector_leaves_the_texts_alone(self):
        self.show(VideoEdit(texts=[TextItem(text="mine")], logo=Logo(file="x.png")))
        self.inspector.set_presets({"saved": ClipSettings(edit=VideoEdit(texts=[TextItem(text="theirs")],
                                                                        logo=Logo(file="y.png"))).to_dict()})
        self.inspector.preset_box.setCurrentIndex(self.inspector.preset_box.findText("★ saved"))
        edit = self.edits[-1][0].edit
        self.assertEqual([t.text for t in edit.texts], ["mine"])
        self.assertEqual(edit.logo.file, "y.png")
        self.inspector.preset_box.setCurrentIndex(1)           # the first built-in preset
        self.assertEqual([t.text for t in self.edits[-1][0].edit.texts], ["mine"])
        self.assertEqual(self.edits[-1][0].edit.logo.file, "y.png")      # it is the one the saved preset brought


class OverlayViewTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.player import VideoView
        self.view = VideoView()
        self.view.resize(416, 250)
        image = QImage(1280, 720, QImage.Format.Format_RGB32)
        image.fill(QColor("#203040"))
        self.view.set_frame(image)
        self.view.show()
        self.events = []
        self.view.overlayPicked.connect(lambda kind, i: self.events.append(("picked", kind, i)))
        self.view.overlayMoved.connect(lambda kind, i, x, y: self.events.append(("moved", kind, i, x, y)))
        self.view.overlayEditFinished.connect(lambda: self.events.append(("finished",)))
        self.logo_file = png(Path(tempfile.mkdtemp()) / "logo.png")

    def tearDown(self):
        self.view.close()
        shiboken6.delete(self.view)     # free it now: every widget left alive slows each later theme change

    def center_of(self, kind, index):
        rect = next(r for k, i, r in self.view.overlay_rects() if (k, i) == (kind, index))
        return rect.center().toPoint()

    def drag(self, start, end, steps=4):
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        for i in range(1, steps + 1):
            point = QPoint(start.x() + (end.x() - start.x()) * i // steps, start.y() + (end.y() - start.y()) * i // steps)
            QTest.mouseMove(self.view, point)
        QTest.mouseRelease(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)

    def test_a_text_is_a_box_around_where_it_is_drawn(self):
        self.view.set_overlays([TextItem(text="Hello", x=0.5, y=0.85)], Logo(), (1280, 720), 1.0)
        out = self.view.output_rect()
        rect = next(r for k, i, r in self.view.overlay_rects())
        self.assertAlmostEqual(rect.center().x(), out.center().x(), delta=2)
        self.assertAlmostEqual(rect.center().y(), out.top() + 0.85 * out.height(), delta=3)
        self.assertTrue(out.contains(rect))

    def test_dragging_a_text_reports_where_its_middle_goes(self):
        self.view.set_overlays([TextItem(text="Hello", x=0.5, y=0.5)], Logo(), (1280, 720), 1.0)
        out = self.view.output_rect()
        start = self.center_of("text", 0)
        self.drag(start, QPoint(start.x() + int(out.width() * 0.2), start.y() - int(out.height() * 0.1)))
        moves = [e for e in self.events if e[0] == "moved"]
        self.assertGreaterEqual(len(moves), 3)
        _, kind, index, x, y = moves[-1]
        self.assertEqual((kind, index), ("text", 0))
        self.assertAlmostEqual(x, 0.7, delta=0.02)
        self.assertAlmostEqual(y, 0.4, delta=0.02)
        self.assertEqual(self.events[0], ("picked", "text", 0))
        self.assertEqual(self.events[-1], ("finished",))
        self.assertEqual(self.events.count(("finished",)), 1)

    def test_dragging_past_the_edge_stops_at_the_edge(self):
        self.view.set_overlays([TextItem(text="Hello")], Logo(), (1280, 720), 1.0)
        start = self.center_of("text", 0)
        self.drag(start, QPoint(start.x() + 1000, start.y() + 1000))
        _, _, _, x, y = [e for e in self.events if e[0] == "moved"][-1]
        self.assertEqual((x, y), (1.0, 1.0))

    def test_dragging_the_logo_reports_a_custom_place(self):
        self.view.set_overlays([], Logo(file=str(self.logo_file), corner="top-left"), (1280, 720), None)
        start = self.center_of("logo", 0)
        out = self.view.output_rect()
        self.drag(start, QPoint(start.x() + int(out.width() * 0.3), start.y() + int(out.height() * 0.2)))
        _, kind, index, x, y = [e for e in self.events if e[0] == "moved"][-1]
        self.assertEqual((kind, index), ("logo", 0))
        self.assertGreater(x, 0.3)
        self.assertGreater(y, 0.2)

    def test_the_logo_sits_in_its_corner(self):
        self.view.set_overlays([], Logo(file=str(self.logo_file), corner="bottom-right", size=0.2, margin=0.05),
                               (1280, 720), None)
        out = self.view.output_rect()
        rect = next(r for k, i, r in self.view.overlay_rects())
        self.assertAlmostEqual(rect.width(), 0.2 * out.width(), delta=1)
        self.assertAlmostEqual(rect.right(), out.right() - 0.05 * out.width(), delta=1)
        self.assertAlmostEqual(rect.bottom(), out.bottom() - 0.05 * out.width(), delta=1)
        self.assertAlmostEqual(rect.height() / rect.width(), 0.5, places=2)       # the picture is 40 x 20

    def test_a_text_that_is_not_on_screen_now_cannot_be_grabbed_unless_selected(self):
        self.view.set_overlays([TextItem(text="Later", start=5.0, end=6.0)], Logo(), (1280, 720), 1.0)
        self.assertEqual(self.view.overlay_rects(), [])
        self.view.set_selected_overlay(("text", 0))
        self.assertEqual(len(self.view.overlay_rects()), 1)
        self.view.set_selected_overlay(None)
        self.view.set_overlay_time(5.5)
        self.assertEqual(len(self.view.overlay_rects()), 1)
        self.view.set_overlay_time(None)        # the playhead is on a cut-out part
        self.assertEqual(self.view.overlay_rects(), [])

    def test_empty_text_has_no_box(self):
        self.view.set_overlays([TextItem(text="  ")], Logo(), (1280, 720), 1.0)
        self.assertEqual(self.view.overlay_rects(), [])

    def test_pressing_elsewhere_grabs_nothing(self):
        self.view.set_overlays([TextItem(text="Hello", x=0.5, y=0.9)], Logo(), (1280, 720), 1.0)
        self.view.set_crop(None, 1.0)
        out = self.view.output_rect()
        self.drag(QPoint(int(out.left()) + 5, int(out.top()) + 5), QPoint(int(out.left()) + 50, int(out.top()) + 50))
        self.assertEqual([e for e in self.events if e[0] in ("picked", "moved")], [])

    def test_the_overlays_go_over_the_crop_box(self):
        self.view.set_crop((1280, 720, 404, 720, 438, 0), 1.0)
        self.view.set_overlays([TextItem(text="Hi", x=0.5, y=0.5)], Logo(), (1080, 1920), 1.0)
        crop = self.view.crop_rect()
        self.assertEqual(self.view.output_rect(), crop)
        rect = next(r for k, i, r in self.view.overlay_rects())
        self.assertTrue(crop.contains(rect))
        # a drag of the text moves the text, not the crop
        moves = []
        self.view.cropMoved.connect(lambda x, y: moves.append((x, y)))
        start = rect.center().toPoint()
        self.drag(start, QPoint(start.x() + 10, start.y() + 10))
        self.assertEqual(moves, [])
        self.assertTrue([e for e in self.events if e[0] == "moved"])

    def test_the_whole_output_frame_is_shown_when_the_picture_does_not_fill_it(self):
        self.view.set_overlays([TextItem(text="Hi")], Logo(), (1080, 1920), 1.0, canvas=True)
        out, frame = self.view.output_rect(), self.view.frame_rect()
        self.assertAlmostEqual(out.width() / out.height(), 1080 / 1920, places=2)
        self.assertTrue(out.adjusted(-1, -1, 1, 1).contains(frame))
        self.assertAlmostEqual(frame.width(), out.width(), delta=1)
        self.view.set_overlays([TextItem(text="Hi")], Logo(), (1080, 1920), 1.0, canvas=False)
        self.assertAlmostEqual(self.view.output_rect().width() / self.view.output_rect().height(), 16 / 9, places=2)

    def test_in_the_after_picture_the_box_is_drawn_but_the_text_is_not_added(self):
        self.view.set_overlays([TextItem(text="Hello", color="#ffffff", outline=0, size=0.3)], Logo(), (1280, 720), 1.0)
        after = QImage(640, 360, QImage.Format.Format_RGB32)
        after.fill(QColor("black"))
        self.view.set_after(after, True)
        self.view.set_selected_overlay(("text", 0))
        picture = self.view.grab().toImage()
        out = self.view.output_rect()
        white = sum(1 for x in range(int(out.left()), int(out.right()), 2)
                    for y in range(int(out.top()) + 50, int(out.bottom()), 2)       # under the "After" badge
                    if QColor(picture.pixel(x, y)).lightness() > 200)
        self.assertEqual(white, 0)
        # and it can still be dragged there
        start = self.center_of("text", 0)
        self.drag(start, QPoint(start.x() + 20, start.y()))
        self.assertTrue([e for e in self.events if e[0] == "moved"])

    def test_text_is_painted_over_the_picture_in_the_before_view(self):
        self.view.set_overlays([TextItem(text="WWWW", color="#ffffff", outline=0, size=0.3, y=0.5)], Logo(),
                               (1280, 720), 1.0)
        picture = self.view.grab().toImage()
        out = self.view.output_rect()
        bright = [(x, y) for x in range(int(out.left()), int(out.right())) for y in range(int(out.top()), int(out.bottom()))
                  if QColor(picture.pixel(x, y)).lightness() > 200]
        self.assertGreater(len(bright), 100)
        cx = sum(p[0] for p in bright) / len(bright)
        cy = sum(p[1] for p in bright) / len(bright)
        self.assertAlmostEqual(cx, out.center().x(), delta=0.03 * out.width())
        self.assertAlmostEqual(cy, out.center().y(), delta=0.05 * out.height())

    def test_text_that_is_not_shown_now_is_not_painted(self):
        self.view.set_overlays([TextItem(text="WWWW", color="#ffffff", outline=0, size=0.3, start=5.0)], Logo(),
                               (1280, 720), 1.0)
        picture = self.view.grab().toImage()
        out = self.view.output_rect()
        self.assertFalse(any(QColor(picture.pixel(x, y)).lightness() > 200 for x in range(int(out.left()), int(out.right()), 2)
                             for y in range(int(out.top()), int(out.bottom()), 2)))

    def test_arabic_and_emoji_are_painted_without_trouble(self):
        self.view.set_overlays([TextItem(text="مرحبا \U0001F600\nline two", box=True, y=0.5),
                                TextItem(text="x" * 400, y=0.2)], Logo(file="/no/such/logo.png"), (1280, 720), 1.0)
        self.assertFalse(self.view.grab().toImage().isNull())

    def test_clearing_forgets_the_overlays(self):
        self.view.set_overlays([TextItem(text="Hello")], Logo(), (1280, 720), 1.0)
        self.view.set_selected_overlay(("text", 0))
        self.view.clear("Loading…")
        self.assertEqual((self.view.texts, self.view.out_size, self.view.selected), ([], None, None))
        self.assertEqual(self.view.overlay_rects(), [])

    def test_a_selection_that_no_longer_exists_is_dropped(self):
        self.view.set_overlays([TextItem(text="a"), TextItem(text="b")], Logo(), (1280, 720), 1.0)
        self.view.set_selected_overlay(("text", 1))
        self.view.set_overlays([TextItem(text="a")], Logo(), (1280, 720), 1.0)
        self.assertIsNone(self.view.selected)


class MainWindowOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.video = cls.tmp / "clip.mp4"
        make_video(cls.video)
        cls.other = cls.tmp / "other.mp4"
        make_video(cls.other, seconds=2)
        cls.logo = png(cls.tmp / "logo.png")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        from video_converter.ui.main_window import MainWindow
        self.ini = self.tmp / f"settings-{self.id().split('.')[-1]}.ini"
        self.window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        self.window.resize(1200, 800)
        self.window.show()
        self.window.add_paths([self.video])
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)), "files never loaded")
        self.item = self.window.current

    def tearDown(self):
        self.window.close()
        self.window.loader.shutdown(wait=True)
        self.window.deleteLater()
        self.window = None
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(0.1)

    def set_edit(self, **fields):
        new = self.item.settings.copy()
        for name, value in fields.items():
            setattr(new.edit, name, value)
        self.window.push_settings(new, "Test edit")
        self.window.undo_stack.clear()

    def test_dragging_a_text_on_the_preview_moves_it_in_one_undo_step(self):
        self.set_edit(texts=[TextItem(text="Hello", x=0.5, y=0.5)], aspect=None)
        view = self.window.view
        start = next(r for k, i, r in view.overlay_rects() if k == "text").center().toPoint()
        QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        for dx in (10, 20, 30, 40):
            QTest.mouseMove(view, QPoint(start.x() + dx, start.y() + dx // 2))
        QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           QPoint(start.x() + 40, start.y() + 20))
        moved = self.item.settings.edit.texts[0]
        self.assertGreater(moved.x, 0.5)
        self.assertGreater(moved.y, 0.5)
        self.assertEqual(self.window.undo_stack.count(), 1)
        self.assertEqual(self.window.undo_stack.undoText(), "Move text")
        self.window.undo_stack.undo()
        self.assertEqual((self.item.settings.edit.texts[0].x, self.item.settings.edit.texts[0].y), (0.5, 0.5))
        self.window.undo_stack.redo()
        self.assertEqual(self.item.settings.edit.texts[0].x, moved.x)

    def test_a_second_drag_is_a_second_undo_step(self):
        self.set_edit(texts=[TextItem(text="Hello")], aspect=None)
        for x in (0.4, 0.3):
            self.window._overlay_moved("text", 0, x, 0.5)
            self.window._edit_finished()
        self.assertEqual(self.window.undo_stack.count(), 2)

    def test_dragging_the_logo_makes_its_place_custom_and_can_be_undone(self):
        self.set_edit(logo=Logo(file=str(self.logo), corner="top-left"), aspect=None)
        self.window._overlay_moved("logo", 0, 0.6, 0.7)
        self.window._edit_finished()
        logo = self.item.settings.edit.logo
        self.assertEqual((logo.corner, logo.x, logo.y), ("custom", 0.6, 0.7))
        self.window.undo_stack.undo()
        self.assertEqual(self.item.settings.edit.logo.corner, "top-left")

    def test_the_inspector_shows_the_dragged_position(self):
        self.set_edit(texts=[TextItem(text="Hello")], aspect=None)
        self.window._overlay_moved("text", 0, 0.2, 0.3)
        self.window._edit_finished()
        self.assertEqual(self.window.inspector.settings.edit.texts[0].x, 0.2)

    def test_clicking_a_text_on_the_preview_selects_it_in_the_list(self):
        self.set_edit(texts=[TextItem(text="one", y=0.3), TextItem(text="two", y=0.7)], aspect=None)
        self.window.inspector.captions.select(0)
        view = self.window.view
        second = next(r for k, i, r in view.overlay_rects() if (k, i) == ("text", 1)).center().toPoint()
        QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, second)
        QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, second)
        self.assertEqual(self.window.inspector.captions.selected, 1)
        self.assertEqual(view.selected, ("text", 1))

    def test_choosing_a_text_in_the_list_marks_it_on_the_preview_and_the_timeline(self):
        self.set_edit(texts=[TextItem(text="one"), TextItem(text="two")], aspect=None)
        self.window.inspector.captions.list.setCurrentRow(1)
        self.assertEqual(self.window.view.selected, ("text", 1))
        self.assertEqual(self.window.timeline.text_selected, 1)

    def test_the_timeline_track_shows_each_texts_time_on_the_source(self):
        self.set_edit(texts=[TextItem(text="a", start=0.5, end=1.5), TextItem(text="b", start=2.0)], aspect=None)
        self.assertEqual(self.window.timeline.text_spans, [[(0.5, 1.5)], [(2.0, 4.0)]])

    def test_the_timeline_track_follows_cuts_and_speed(self):
        # 1-2 s cut out: the text's first half second is the source's 0.5-1.0 s, then it jumps to 2.0-2.5 s.
        self.set_edit(texts=[TextItem(text="a", start=0.5, end=1.5)], aspect=None, keep=[(0.0, 1.0), (2.0, 4.0)])
        self.assertEqual(self.window.timeline.text_spans, [[(0.5, 1.0), (2.0, 2.5)]])
        self.set_edit(speed=2.0)
        # at double speed the same 0.5-1.5 s of the result is 1-3 s of what is kept: all of the second part
        self.assertEqual(self.window.timeline.text_spans, [[(2.0, 4.0)]])

    def test_no_track_without_texts(self):
        self.set_edit(texts=[], aspect=None)
        self.assertEqual(self.window.timeline.text_spans, [])

    def test_use_playhead_sets_the_texts_time_on_the_finished_video(self):
        from video_converter.ui.player import Player
        self.set_edit(texts=[TextItem(text="a")], aspect=None, keep=[(0.0, 1.0), (2.0, 4.0)], speed=2.0)
        self.window.inspector.captions.select(0)
        # source 3.0 s is 1 s into the second kept part: (1.0 + 1.0) / 2 = 1.0 s on the finished video
        with mock.patch.object(Player, "position", new_callable=mock.PropertyMock, return_value=3.0):
            self.window.text_time_at_playhead("start")
            self.assertEqual(self.item.settings.edit.texts[0].start, 1.0)
        with mock.patch.object(Player, "position", new_callable=mock.PropertyMock, return_value=3.9):
            self.window.text_time_at_playhead("end")
        self.assertEqual(self.item.settings.edit.texts[0].end, 1.45)
        self.assertEqual(self.window.undo_stack.undoText(), "Text time")

    def test_use_playhead_refuses_a_time_that_does_not_make_sense(self):
        from video_converter.ui.player import Player
        self.set_edit(texts=[TextItem(text="a", start=1.0, end=2.0)], aspect=None, keep=[(0.0, 1.0), (2.0, 4.0)])
        self.window.inspector.captions.select(0)
        with mock.patch.object(Player, "position", new_callable=mock.PropertyMock, return_value=1.5):   # cut out
            self.window.text_time_at_playhead("end")
        self.assertIn("cut out", self.window.statusBar().currentMessage())
        with mock.patch.object(Player, "position", new_callable=mock.PropertyMock, return_value=0.5):
            self.window.text_time_at_playhead("end")                  # before the start
            self.assertIn("after it starts", self.window.statusBar().currentMessage())
            self.window.text_time_at_playhead("start")
        self.assertEqual(self.item.settings.edit.texts[0].start, 0.5)
        with mock.patch.object(Player, "position", new_callable=mock.PropertyMock, return_value=3.0):
            self.window.text_time_at_playhead("start")                # past the end of the text
            self.assertIn("before it ends", self.window.statusBar().currentMessage())
        self.assertEqual(self.item.settings.edit.texts[0].start, 0.5)

    def test_the_preview_follows_the_playhead_on_the_finished_timeline(self):
        self.set_edit(texts=[TextItem(text="a", start=1.0, end=2.0)], aspect=None, keep=[(0.0, 1.0), (2.0, 4.0)])
        self.window._on_position(0.5)
        self.assertFalse(self.window.view._text_shown(0))
        # source 2.5 s is 1.5 s on the finished video: inside the text's 1-2 s
        from video_converter.ui.player import Player
        with mock.patch.object(Player, "position", new_callable=mock.PropertyMock, return_value=2.5):
            self.window._on_position(2.5)
            self.assertTrue(self.window.view._text_shown(0))

    def test_in_a_fit_mode_the_preview_shows_the_whole_output_frame(self):
        self.set_edit(texts=[TextItem(text="a")], aspect=(9, 16), fit="bars")
        self.assertTrue(self.window.view.canvas)
        self.assertEqual(self.window.view.out_size, (180, 320))
        self.set_edit(fit="crop")
        self.assertFalse(self.window.view.canvas)
        self.set_edit(texts=[], fit="bars")
        self.assertFalse(self.window.view.canvas)

    def test_mp3_output_has_no_overlays(self):
        self.set_edit(texts=[TextItem(text="a")], aspect=None)
        audio = self.item.settings.copy()
        audio.output = OUTPUT_AUDIO
        self.window.push_settings(audio, "Output")
        self.assertEqual(self.window.view.texts, [])
        self.assertEqual(self.window.timeline.text_spans, [])

    def test_before_after_shows_the_text_the_export_will_have(self):
        self.set_edit(texts=[TextItem(text="WWWW", size=0.3, outline=0, color="#ffffff", y=0.5)], aspect=None)
        self.window.compare_button.setChecked(True)
        self.window._compare_toggled(True)
        self.assertTrue(pump(TIMEOUT, lambda: self.window.view.after_image is not None), "no preview")
        image = self.window.view.after_image
        bright = sum(1 for x in range(0, image.width(), 2) for y in range(0, image.height(), 2)
                     if QColor(image.pixel(x, y)).lightness() > 200)
        self.assertGreater(bright, 100)

    def test_apply_to_all_copies_the_logo_but_not_the_texts(self):
        self.window.add_paths([self.other])
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)))
        first, second = self.window.items
        self.window.select(first)
        self.set_edit(texts=[TextItem(text="first only")], logo=Logo(file=str(self.logo), size=0.3),
                      subtitles=Subtitles(file="a.srt", size=0.09))
        second.settings.edit.texts = [TextItem(text="second's own")]
        second.settings.edit.subtitles.file = "b.srt"
        self.window.apply_to_all()
        self.assertEqual([t.text for t in second.settings.edit.texts], ["second's own"])
        self.assertEqual(second.settings.edit.logo, Logo(file=str(self.logo), size=0.3))
        self.assertEqual((second.settings.edit.subtitles.file, second.settings.edit.subtitles.size), ("b.srt", 0.09))
        self.assertEqual([t.text for t in first.settings.edit.texts], ["first only"])

    def test_the_list_summary_mentions_text_and_logo(self):
        self.set_edit(texts=[TextItem(text="hi")], logo=Logo(file=str(self.logo)), aspect=None)
        summary = self.item.list_item.text().splitlines()[-1]
        self.assertIn("text", summary)
        self.assertIn("logo", summary)

    def test_exporting_burns_the_text_in(self):
        self.set_edit(texts=[TextItem(text="WWWW", size=0.3, outline=0, y=0.5)], aspect=None, use_gpu=False)
        out_dir = self.tmp / "exports"
        out_dir.mkdir(exist_ok=True)
        self.window._set_output_folder(out_dir)
        self.window.export_current()
        self.assertTrue(pump(60, lambda: not self.window.queue.busy), "the export never finished")
        result = next(out_dir.glob("clip*.mp4"))
        raw = subprocess.run([FFMPEG, "-v", "error", "-ss", "0.5", "-i", str(result), "-frames:v", "1", "-f", "rawvideo",
                              "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
        self.assertGreater(sum(1 for v in raw if v > 200), 300)


class TimelineTrackTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.timeline import Timeline
        self.timeline = Timeline()
        self.timeline.resize(612, 120)
        self.timeline.setDuration(10.0)
        self.timeline.show()

    def tearDown(self):
        self.timeline.close()
        shiboken6.delete(self.timeline)     # free it now: every widget left alive slows each later theme change

    def test_the_track_has_room_and_is_drawn(self):
        from video_converter.ui.timeline import RULER_H, TEXT_H, THUMBS_H, WAVE_H
        self.assertGreaterEqual(self.timeline.minimumHeight(), RULER_H + THUMBS_H + WAVE_H + TEXT_H)
        self.timeline.setTexts([[(2.0, 4.0)], [(6.0, 8.0), (9.0, 10.0)]], 1)
        self.assertEqual(self.timeline.text_spans, [[(2.0, 4.0)], [(6.0, 8.0), (9.0, 10.0)]])
        self.assertEqual(self.timeline.text_selected, 1)
        image = self.timeline.grab().toImage()
        self.assertFalse(image.isNull())

    def test_the_selected_text_is_drawn_in_another_color(self):
        self.timeline.setTexts([[(2.0, 4.0)], [(6.0, 8.0)]], 1)
        lane_y = 18 + 46 + 4 + 30 + 4 + 4        # the thin track sits under the waveform
        image = self.timeline.grab().toImage()
        x1 = int(self.timeline._x(3.0))
        x2 = int(self.timeline._x(7.0))
        self.assertNotEqual(image.pixel(x1, lane_y), image.pixel(x2, lane_y))
        self.assertNotEqual(image.pixel(x1, lane_y), image.pixel(int(self.timeline._x(5.0)), lane_y))

    def test_clearing_removes_the_track(self):
        self.timeline.setTexts([[(2.0, 4.0)]], 0)
        self.timeline.clear()
        self.assertEqual((self.timeline.text_spans, self.timeline.text_selected), ([], -1))

    def test_clicking_the_track_still_seeks(self):
        seeks = []
        self.timeline.seekRequested.connect(seeks.append)
        self.timeline.setTexts([[(2.0, 4.0)]], 0)
        QTest.mouseClick(self.timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(int(self.timeline._x(5.0)), 100))
        self.assertAlmostEqual(seeks[-1], 5.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
