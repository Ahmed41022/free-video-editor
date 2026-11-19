"""Keyboard shortcuts that must not fire while a field has the keys."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QPlainTextEdit,
    QTextEdit, QWidget,
)

FRAME_STEP = 1 / 30


class ShortcutsMixin:
    """Keyboard handling."""

    def _owns_shortcuts(self, focus: Optional[QWidget], key: int) -> bool:
        """Whether the playback and editing keys (Space, J K L, I O, S X, B, arrows, Home) are ours while `focus`
        has the keyboard. They are only for the places the person is not typing or operating a control: the
        window itself, the file list, the preview, the timeline and the transport bar. Every text input, and
        every button, check box, slider and list of the settings panel, keeps its own keys."""
        if focus is None or focus is self:
            return True
        if isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)) or (
                isinstance(focus, QComboBox) and focus.isEditable()):
            return False
        if focus is self.media_list or focus is self.media_list.viewport() or focus is self.view \
                or focus is self.timeline:
            return True
        if self.transport.isAncestorOf(focus):
            # A transport button keeps Space (it presses the button); the other keys are the transport's own.
            return not (isinstance(focus, QAbstractButton) and key == Qt.Key.Key_Space)
        return False

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not self.isActiveWindow() or event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            return super().eventFilter(obj, event)
        focus = QApplication.focusWidget()
        if not self._owns_shortcuts(focus, event.key()) or self.current is None:
            return super().eventFilter(obj, event)
        key = event.key()
        if not (Qt.Key.Key_A <= key <= Qt.Key.Key_Z) and 0x41 <= event.nativeVirtualKey() <= 0x5A:
            key = event.nativeVirtualKey()      # the letter's key on an Arabic or other non-Latin keyboard layout
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        handlers = {
            Qt.Key.Key_Space: self.player.toggle,
            Qt.Key.Key_K: self.player.pause,
            Qt.Key.Key_J: lambda: self.player.step(-5),
            Qt.Key.Key_L: lambda: self.player.step(5),
            Qt.Key.Key_Left: lambda: self.player.step(-1 if shift else -FRAME_STEP),
            Qt.Key.Key_Right: lambda: self.player.step(1 if shift else FRAME_STEP),
            Qt.Key.Key_I: lambda: self.trim_at_playhead("start"),
            Qt.Key.Key_O: lambda: self.trim_at_playhead("end"),
            Qt.Key.Key_S: lambda: self.cut_at("split"),
            Qt.Key.Key_X: lambda: self.cut_at("remove"),
            Qt.Key.Key_B: self.compare_button.click,
            Qt.Key.Key_Home: lambda: self.player.seek(self.current.settings.edit.start or 0.0),
        }
        if key == Qt.Key.Key_Delete and focus is self.media_list:
            self.remove_selected()
            return True
        handler = handlers.get(key)
        if handler is None:
            return super().eventFilter(obj, event)
        handler()
        return True
