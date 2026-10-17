"""Video playback and the preview canvas with the interactive crop box."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPointF, QRectF, QSizeF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QTransform, QWheelEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QtVideo, QVideoFrame, QVideoSink
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme

ZOOM_MIN, ZOOM_MAX = 1.0, 3.0


def orient_image(image: QImage, rotate_cw: int, mirror: bool) -> QImage:
    """Rotate `image` clockwise by 0/90/180/270 degrees, then optionally mirror it,
    which is the same order the exporter applies them."""
    if rotate_cw % 360:
        image = image.transformed(QTransform().rotate(rotate_cw % 360))
    if mirror:
        image = image.flipped(Qt.Orientation.Horizontal)
    return image


def fit_rect(content: QSizeF, bounds: QRectF) -> QRectF:
    """The largest rectangle with `content`'s shape, centered in `bounds`."""
    if content.width() <= 0 or content.height() <= 0:
        return QRectF()
    scale = min(bounds.width() / content.width(), bounds.height() / content.height())
    w, h = content.width() * scale, content.height() * scale
    return QRectF(bounds.center().x() - w / 2, bounds.center().y() - h / 2, w, h)


class Player(QObject):
    """Wraps QMediaPlayer: decodes frames to QImages and keeps playback inside the trim range."""

    frameReady = Signal(QImage, bool)       # image, whether Qt already applied rotation metadata
    positionChanged = Signal(float)
    durationChanged = Signal(float)
    playingChanged = Signal(bool)
    errorOccurred = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.media = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.sink = QVideoSink(self)
        self.media.setAudioOutput(self.audio)
        self.media.setVideoOutput(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.media.positionChanged.connect(self._on_position)
        self.media.durationChanged.connect(lambda ms: self.durationChanged.emit(ms / 1000))
        self.media.playbackStateChanged.connect(
            lambda state: self.playingChanged.emit(state == QMediaPlayer.PlaybackState.PlayingState))
        self.media.errorOccurred.connect(lambda _e, message: self.errorOccurred.emit(message))
        self.trim_start: Optional[float] = None
        self.trim_end: Optional[float] = None

    @property
    def playing(self) -> bool:
        return self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    @property
    def position(self) -> float:
        return self.media.position() / 1000

    def load(self, path: Optional[Path]) -> None:
        self.media.stop()
        self.media.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())
        if path:
            # Pausing straight away decodes the first frame, so the preview isn't blank.
            self.media.pause()

    def set_trim(self, start: Optional[float], end: Optional[float]) -> None:
        self.trim_start, self.trim_end = start, end

    def toggle(self) -> None:
        if self.playing:
            self.media.pause()
            return
        start, end = self.trim_start or 0.0, self.trim_end
        if self.position < start - 0.05 or (end is not None and self.position >= end - 0.05):
            self.seek(start)
        self.media.play()

    def pause(self) -> None:
        self.media.pause()

    def seek(self, seconds: float) -> None:
        self.media.setPosition(int(max(seconds, 0.0) * 1000))

    def step(self, seconds: float) -> None:
        self.seek(self.position + seconds)

    def set_muted(self, muted: bool) -> None:
        self.audio.setMuted(muted)

    def _on_position(self, ms: int) -> None:
        seconds = ms / 1000
        if self.playing and self.trim_end is not None and seconds >= self.trim_end:
            self.media.pause()
            self.seek(self.trim_end)
        self.positionChanged.emit(seconds)

    def _on_frame(self, frame: QVideoFrame) -> None:
        if not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            # toImage() applies the rotation Qt knows about, which it reports either on the
            # frame or (Qt 6.12) on the frame's surface format.
            none = QtVideo.Rotation.None_
            oriented = frame.rotation() != none or frame.surfaceFormat().rotation() != none
            self.frameReady.emit(image, oriented)


class VideoView(QWidget):
    """Shows the current frame oriented like the export, with a draggable,
    resizable crop box. In "after" mode it shows a rendered preview instead."""

    cropMoved = Signal(float, float)       # new pos_x, pos_y (0..1)
    zoomChanged = Signal(float)
    cropEditFinished = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 200)
        self.setMouseTracking(True)
        self.raw: Optional[QImage] = None
        self.raw_is_oriented = False
        self.display_rotation = 0        # from file metadata
        self.user_rotate = 0
        self.mirror = False
        self.after_image: Optional[QImage] = None
        self.show_after = False
        self.placeholder = "Drop videos here, or click Add files"
        # Crop overlay, in oriented source pixels: (frame w, frame h, crop w, crop h, x, y) or None
        self.crop: Optional[tuple[int, int, int, int, int, int]] = None
        self.zoom = 1.0
        self._image: Optional[QImage] = None
        self._drag: Optional[tuple[str, QPointF, float, float, float]] = None

    # ----- state --------------------------------------------------------------

    def clear(self, placeholder: Optional[str] = None) -> None:
        self.raw = self._image = self.after_image = None
        self.crop = None
        if placeholder is not None:
            self.placeholder = placeholder
        self.update()

    def set_frame(self, image: QImage, already_oriented: bool = False) -> None:
        self.raw, self.raw_is_oriented = image, already_oriented
        self._image = None
        self.update()

    def set_orientation(self, display_rotation: int, user_rotate: int, mirror: bool) -> None:
        if (display_rotation, user_rotate, mirror) != (self.display_rotation, self.user_rotate, self.mirror):
            self.display_rotation, self.user_rotate, self.mirror = display_rotation, user_rotate, mirror
            self._image = None
            self.update()

    def set_crop(self, crop: Optional[tuple[int, int, int, int, int, int]], zoom: float) -> None:
        self.crop, self.zoom = crop, zoom
        self.update()

    def set_after(self, image: Optional[QImage], show: bool) -> None:
        self.after_image, self.show_after = image, show
        self.update()

    def oriented_image(self) -> Optional[QImage]:
        if self._image is None and self.raw is not None:
            base = 0 if self.raw_is_oriented else self.display_rotation
            self._image = orient_image(self.raw, base + self.user_rotate, self.mirror)
        return self._image

    # ----- geometry -----------------------------------------------------------

    def frame_rect(self) -> QRectF:
        """Where the (oriented) frame is drawn in widget coordinates."""
        if self.crop:
            size = QSizeF(self.crop[0], self.crop[1])
        else:
            image = self.oriented_image()
            if image is None:
                return QRectF()
            size = QSizeF(image.width(), image.height())
        return fit_rect(size, QRectF(self.rect()).adjusted(8, 8, -8, -8))

    def crop_rect(self) -> Optional[QRectF]:
        if not self.crop:
            return None
        fw, fh, cw, ch, cx, cy = self.crop
        frame = self.frame_rect()
        sx, sy = frame.width() / fw, frame.height() / fh
        return QRectF(frame.left() + cx * sx, frame.top() + cy * sy, cw * sx, ch * sy)

    def _handle_at(self, point: QPointF) -> Optional[str]:
        rect = self.crop_rect()
        if rect is None or self.show_after:
            return None
        for name, corner in (("tl", rect.topLeft()), ("tr", rect.topRight()),
                             ("bl", rect.bottomLeft()), ("br", rect.bottomRight())):
            if (point - corner).manhattanLength() <= 14:
                return name
        return "move" if rect.contains(point) else "outside"

    # ----- mouse --------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        handle = self._handle_at(event.position())
        if handle is None:
            return
        rect = self.crop_rect()
        if handle == "outside":
            # Jump the box to where the user clicked, then keep dragging it.
            self._move_center_to(event.position())
            handle, rect = "move", self.crop_rect()
        self._drag = (handle, event.position(), rect.center().x(), rect.center().y(), self.zoom)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag is None:
            handle = self._handle_at(event.position())
            cursor = {"move": Qt.CursorShape.SizeAllCursor, "tl": Qt.CursorShape.SizeFDiagCursor,
                      "br": Qt.CursorShape.SizeFDiagCursor, "tr": Qt.CursorShape.SizeBDiagCursor,
                      "bl": Qt.CursorShape.SizeBDiagCursor}.get(handle or "", Qt.CursorShape.ArrowCursor)
            self.setCursor(cursor)
            return
        handle, start, cx, cy, zoom = self._drag
        delta = event.position() - start
        if handle == "move":
            self._move_center_to(QPointF(cx + delta.x(), cy + delta.y()))
        else:
            # Corner drags resize the box around its center; size maps to zoom.
            rect = self.crop_rect()
            center = rect.center()
            start_half = max(abs(start.x() - cx), 1.0)
            new_half = max(abs(event.position().x() - center.x()), 1.0)
            new_zoom = min(max(zoom * start_half / new_half, ZOOM_MIN), ZOOM_MAX)
            if abs(new_zoom - self.zoom) > 0.005:
                self.zoomChanged.emit(round(new_zoom, 3))

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        if self._drag is not None:
            self._drag = None
            self.cropEditFinished.emit()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.crop and not self.show_after:
            step = 0.1 if event.angleDelta().y() > 0 else -0.1
            self.zoomChanged.emit(round(min(max(self.zoom + step, ZOOM_MIN), ZOOM_MAX), 3))
            self.cropEditFinished.emit()

    def _move_center_to(self, center: QPointF) -> None:
        fw, fh, cw, ch, _, _ = self.crop
        frame = self.frame_rect()
        sx, sy = frame.width() / fw, frame.height() / fh
        x = (center.x() - frame.left()) / sx - cw / 2
        y = (center.y() - frame.top()) / sy - ch / 2
        free_x, free_y = fw - cw, fh - ch
        pos_x = min(max(x / free_x, 0.0), 1.0) if free_x > 0 else 0.5
        pos_y = min(max(y / free_y, 0.0), 1.0) if free_y > 0 else 0.5
        self.cropMoved.emit(pos_x, pos_y)

    # ----- painting -----------------------------------------------------------

    def paintEvent(self, _event) -> None:
        c = theme.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(c["video_bg"]))

        if self.show_after and self.after_image is not None:
            target = fit_rect(QSizeF(self.after_image.size()), QRectF(self.rect()).adjusted(8, 8, -8, -8))
            p.drawImage(target, self.after_image)
            self._badge(p, "After")
            return

        image = self.oriented_image()
        if image is None:
            p.setPen(QColor("#9c9da4"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            return
        frame = self.frame_rect()
        p.drawImage(frame, image)

        rect = self.crop_rect()
        if rect is not None:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            shade = QColor(0, 0, 0, 140)
            p.fillRect(QRectF(frame.left(), frame.top(), frame.width(), rect.top() - frame.top()), shade)
            p.fillRect(QRectF(frame.left(), rect.bottom(), frame.width(), frame.bottom() - rect.bottom()), shade)
            p.fillRect(QRectF(frame.left(), rect.top(), rect.left() - frame.left(), rect.height()), shade)
            p.fillRect(QRectF(rect.right(), rect.top(), frame.right() - rect.right(), rect.height()), shade)
            thirds = QPen(QColor(255, 255, 255, 70))
            p.setPen(thirds)
            for i in (1, 2):
                x = rect.left() + rect.width() * i / 3
                y = rect.top() + rect.height() * i / 3
                p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            border = QPen(QColor("white"))
            border.setWidthF(2)
            p.setPen(border)
            p.drawRect(rect)
            p.setBrush(QColor("white"))
            for corner in (rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()):
                p.drawEllipse(corner, 5, 5)
        if self.show_after:
            self._badge(p, "Rendering preview…")

    def _badge(self, p: QPainter, text: str) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        metrics = p.fontMetrics()
        box = QRectF(14, 14, metrics.horizontalAdvance(text) + 16, metrics.height() + 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 160))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
