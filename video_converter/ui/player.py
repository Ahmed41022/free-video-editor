"""Video playback and the preview canvas with the interactive crop box."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPointF, QRectF, QSizeF, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QTransform, QWheelEvent,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QtVideo, QVideoFrame, QVideoSink
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..captions import Logo, TextItem, laid_out, text_box
from ..i18n import tr
from ..timing import Segment
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
    """Wraps QMediaPlayer: decodes frames to QImages, keeps playback inside the trim range and
    skips the parts that are cut out."""

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
        self.media.mediaStatusChanged.connect(self._on_status)
        self.segments: list[Segment] = []      # what the export keeps; [] = everything
        self.source: Optional[Path] = None
        # Frames of a preview copy are already upright (ffmpeg turned them when making it), whatever
        # rotation the original file asks for.
        self.upright = False
        self._resume: Optional[tuple[int, bool, bool]] = None    # position (ms), playing, upright: set while swapping
        self._new_source_loading = False

    @property
    def playing(self) -> bool:
        if self._resume is not None:
            return self._resume[1]
        return self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    @property
    def position(self) -> float:
        return (self._resume[0] if self._resume is not None else self.media.position()) / 1000

    def load(self, path: Optional[Path], upright: bool = False) -> None:
        """Open `path`. `upright` says it is a preview copy, whose frames need no turning."""
        self._resume = None
        self.media.stop()
        self.source, self.upright = path, upright and path is not None
        self.media.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())
        if path:
            # Pausing straight away decodes the first frame, so the preview isn't blank.
            self.media.pause()

    def swap_source(self, path: Path, upright: bool) -> None:
        """Carry on from the same moment in another copy of the same video (the preview copy once it is
        ready, or the original again), playing if it was playing."""
        if path == self.source:
            return
        if self._resume is not None:        # swapped again before the last one finished
            self._resume = (self._resume[0], self._resume[1], upright)
        else:
            self._resume = (self.media.position(), self.playing, upright)
        self._new_source_loading = False
        self.source = path
        self.media.setSource(QUrl.fromLocalFile(str(path)))

    def _on_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if self._resume is None:
            return
        # The old source still reports "loaded" for a moment; only what comes after the new one starts
        # loading counts.
        if status == QMediaPlayer.MediaStatus.LoadingMedia:
            self._new_source_loading = True
        elif self._new_source_loading and status in (QMediaPlayer.MediaStatus.LoadedMedia,
                                                     QMediaPlayer.MediaStatus.BufferedMedia):
            position, playing, self.upright = self._resume
            self._resume = None
            self.media.setPosition(position)
            if playing:
                self.media.play()
            else:
                self.media.pause()

    def set_segments(self, segments: list[Segment]) -> None:
        self.segments = list(segments)

    def set_rate(self, rate: float) -> None:
        if abs(self.media.playbackRate() - rate) > 1e-6:
            self.media.setPlaybackRate(rate)

    def next_play_position(self, t: float) -> Optional[float]:
        """Where playback continues from `t`: `t` itself inside a kept part, the start of the
        next kept part inside a cut, or None after the last one."""
        for a, b in self.segments:
            if t < a - 0.05:
                return a
            if b is None or t < b - 0.05:
                return max(t, a)
        return None if self.segments else t

    def toggle(self) -> None:
        if self.playing:
            self.media.pause()
            return
        resume = self.next_play_position(self.position)
        if resume is None:          # past the end: start over
            resume = self.segments[0][0]
        if abs(resume - self.position) > 0.01:
            self.seek(resume)
        self.media.play()

    def pause(self) -> None:
        self.media.pause()

    def seek(self, seconds: float) -> None:
        ms = int(max(seconds, 0.0) * 1000)
        if self._resume is not None:        # still swapping: that is where to carry on from
            self._resume = (ms, self._resume[1], self._resume[2])
            return
        self.media.setPosition(ms)

    def step(self, seconds: float) -> None:
        self.seek(self.position + seconds)

    def set_muted(self, muted: bool) -> None:
        self.audio.setMuted(muted)

    def _on_position(self, ms: int) -> None:
        if self._resume is not None:        # the new source starts at 0 for a moment; not a real position
            return
        seconds = ms / 1000
        if self.playing and self.segments:
            resume = self.next_play_position(seconds)
            if resume is None:
                self.media.pause()
                end = self.segments[-1][1]
                if end is not None:
                    self.seek(end)
            elif resume - seconds > 0.01:
                self.seek(resume)       # jump over a cut
        self.positionChanged.emit(seconds)

    def _on_frame(self, frame: QVideoFrame) -> None:
        if not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            # toImage() applies the rotation Qt knows about, which it reports either on the
            # frame or (Qt 6.12) on the frame's surface format.
            none = QtVideo.Rotation.None_
            oriented = self.upright or frame.rotation() != none or frame.surfaceFormat().rotation() != none
            self.frameReady.emit(image, oriented)


class VideoView(QWidget):
    """Shows the current frame oriented like the export, with a draggable,
    resizable crop box. In "after" mode it shows a rendered preview instead."""

    cropMoved = Signal(float, float)       # new pos_x, pos_y (0..1)
    zoomChanged = Signal(float)
    cropEditFinished = Signal()
    overlayPicked = Signal(str, int)               # "text" and its index, or "logo" and 0: pressed on it
    overlayMoved = Signal(str, int, float, float)  # kind, index, new x, y (shares of the output frame)
    overlayEditFinished = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 200)
        self.setMouseTracking(True)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)      # the picture is never mirrored
        self.raw: Optional[QImage] = None
        self.raw_is_oriented = False
        self.display_rotation = 0        # from file metadata
        self.user_rotate = 0
        self.mirror = False
        self.after_image: Optional[QImage] = None
        self.show_after = False
        self.placeholder = tr("Drop videos here, or click Add files")
        # Crop overlay, in oriented source pixels: (frame w, frame h, crop w, crop h, x, y) or None
        self.crop: Optional[tuple[int, int, int, int, int, int]] = None
        self.zoom = 1.0
        self._image: Optional[QImage] = None
        self._drag: Optional[tuple[str, QPointF, float, float, float]] = None
        # Text and logo drawn over the output frame, which they can be dragged around.
        self.texts: list[TextItem] = []
        self.logo = Logo()
        self.out_size: Optional[tuple[int, int]] = None     # the export's width and height
        self.overlay_time: Optional[float] = None           # the playhead on the export's timeline; None = cut out
        self.selected: Optional[tuple[str, int]] = None     # ("text", index) or ("logo", 0)
        self.canvas = False         # show the whole output frame even when it is bigger than the picture
        self._logo_cache: Optional[tuple[str, int, QImage]] = None
        self._overlay_drag: Optional[tuple[str, int, QPointF, float, float]] = None

    # ----- state --------------------------------------------------------------

    def clear(self, placeholder: Optional[str] = None) -> None:
        self.raw = self._image = self.after_image = None
        self.crop = None
        self.texts, self.out_size, self.selected, self.canvas = [], None, None, False
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
        bounds = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        if self.crop:
            size = QSizeF(self.crop[0], self.crop[1])
        else:
            image = self.oriented_image()
            if image is None:
                return QRectF()
            size = QSizeF(image.width(), image.height())
            if self.canvas and self.out_size:
                bounds = fit_rect(QSizeF(*self.out_size), bounds)       # the picture sits inside the output frame
        return fit_rect(size, bounds)

    def crop_rect(self) -> Optional[QRectF]:
        if not self.crop:
            return None
        fw, fh, cw, ch, cx, cy = self.crop
        frame = self.frame_rect()
        sx, sy = frame.width() / fw, frame.height() / fh
        return QRectF(frame.left() + cx * sx, frame.top() + cy * sy, cw * sx, ch * sy)

    # ----- text and logo --------------------------------------------------------

    def set_overlays(self, texts: list[TextItem], logo: Logo, out_size: Optional[tuple[int, int]],
                     time: Optional[float], canvas: bool = False) -> None:
        """Show these texts and this logo over the output frame (`out_size` is the export's width and
        height). `time` is the playhead on the export's timeline, which decides which texts show;
        `canvas` shows the whole output frame, as the export will have it, around a picture that doesn't fill it."""
        self.texts, self.logo, self.out_size, self.overlay_time, self.canvas = list(texts), logo, out_size, time, canvas
        if self.selected and (self.selected[0] == "text" and self.selected[1] >= len(texts)
                              or self.selected[0] == "logo" and not logo.file):
            self.selected = None
        self.update()

    def set_overlay_time(self, time: Optional[float]) -> None:
        if time != self.overlay_time:
            self.overlay_time = time
            self.update()

    def set_selected_overlay(self, selected: Optional[tuple[str, int]]) -> None:
        if selected != self.selected:
            self.selected = selected
            self.update()

    def output_rect(self) -> QRectF:
        """Where the exported frame is drawn: the rendered preview, the crop box, or the picture."""
        bounds = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        if self.show_after and self.after_image is not None:
            return fit_rect(QSizeF(self.after_image.size()), bounds)
        if self.crop:
            return self.crop_rect() or QRectF()
        if self.canvas and self.out_size:
            return fit_rect(QSizeF(*self.out_size), bounds)
        return self.frame_rect()

    def _text_shown(self, index: int) -> bool:
        item = self.texts[index]
        if self.overlay_time is None:
            return False
        return item.start <= self.overlay_time and (item.end is None or self.overlay_time <= item.end)

    def _text_rect(self, item: TextItem, out: QRectF) -> QRectF:
        """The box a text takes up in widget coordinates, kept inside the frame like ffmpeg keeps it."""
        scale = out.height() / self.out_size[1]
        width, height = (v * scale for v in text_box(item, self.out_size))
        left = min(max(out.left() + item.x * out.width() - width / 2, out.left()), max(out.right() - width, out.left()))
        top = min(max(out.top() + item.y * out.height() - height / 2, out.top()), max(out.bottom() - height, out.top()))
        return QRectF(left, top, width, height)

    def _logo_image(self) -> QImage:
        path = self.logo.file
        if not path:
            return QImage()
        try:
            stamp = Path(path).stat().st_mtime_ns
        except OSError:
            return QImage()
        if self._logo_cache is None or self._logo_cache[:2] != (path, stamp):
            self._logo_cache = (path, stamp, QImage(path))
        return self._logo_cache[2]

    def _logo_rect(self, out: QRectF) -> Optional[QRectF]:
        image = self._logo_image()
        if image.isNull() or self.out_size is None:
            return None
        logo = self.logo
        width = max(logo.size * out.width(), 4.0)
        height = width * image.height() / image.width()
        margin = logo.margin * out.width()
        if logo.corner == "custom":
            left, top = out.left() + logo.x * out.width() - width / 2, out.top() + logo.y * out.height() - height / 2
        else:
            vertical, _, horizontal = logo.corner.partition("-")
            left = out.center().x() - width / 2 if logo.corner == "center" else (
                out.left() + margin if horizontal == "left" else out.right() - width - margin)
            top = out.center().y() - height / 2 if logo.corner == "center" else (
                out.top() + margin if vertical == "top" else out.bottom() - height - margin)
        left = min(max(left, out.left()), max(out.right() - width, out.left()))
        top = min(max(top, out.top()), max(out.bottom() - height, out.top()))
        return QRectF(left, top, width, height)

    def overlay_rects(self) -> list[tuple[str, int, QRectF]]:
        """What can be grabbed, bottom to top: the logo, then the texts that show now (and the selected one)."""
        out = self.output_rect()
        if out.isEmpty() or self.out_size is None:
            return []
        rects = []
        logo = self._logo_rect(out)
        if logo is not None:
            rects.append(("logo", 0, logo))
        for i, item in enumerate(self.texts):
            if item.text.strip() and (self._text_shown(i) or self.selected == ("text", i)):
                rects.append(("text", i, self._text_rect(item, out)))
        return rects

    def _overlay_at(self, point: QPointF) -> Optional[tuple[str, int, QRectF]]:
        for kind, index, rect in reversed(self.overlay_rects()):
            if rect.adjusted(-4, -4, 4, 4).contains(point):
                return kind, index, rect
        return None

    def _paint_overlays(self, p: QPainter, content: bool) -> None:
        """Draw the texts and the logo (`content`; the rendered preview already has them) and a frame around the selected one."""
        out = self.output_rect()
        rects = self.overlay_rects()
        p.save()
        p.setClipRect(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for kind, index, rect in rects:
            if content and kind == "logo":
                p.setOpacity(self.logo.opacity)
                p.drawImage(rect, self._logo_image())
                p.setOpacity(1.0)
            elif content:
                self._draw_text(p, self.texts[index], rect, out, 1.0 if self._text_shown(index) else 0.4)
        p.restore()
        for kind, index, rect in rects:
            if self.selected == (kind, index):
                pen = QPen(QColor("#f5b83d"))
                pen.setWidthF(1.5)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect.adjusted(-2, -2, 2, 2))

    def _draw_text(self, p: QPainter, item: TextItem, rect: QRectF, out: QRectF, opacity: float) -> None:
        """One text, drawn with Qt's own text layout (so Arabic and other scripts join and run the right way).
        The export draws it with ffmpeg; this is the live version while editing."""
        scale = out.height() / self.out_size[1]
        wrapped, _, pixels = laid_out(item, self.out_size)
        font = QFont(item.font)
        font.setPixelSize(max(int(round(pixels * scale)), 1))
        font.setBold(item.bold)
        metrics = QFontMetricsF(font)
        p.setOpacity(opacity)
        if item.box:
            box = QColor(item.box_color)
            box.setAlphaF(item.box_opacity)
            p.fillRect(rect, box)
        lines = wrapped.split("\n")
        pad = (0.3 if item.box else item.outline) * pixels * scale
        inner = rect.adjusted(pad, pad, -pad, -pad)
        for i, line in enumerate(lines):
            width = metrics.horizontalAdvance(line)
            x = {"left": inner.left(), "right": inner.right() - width}.get(item.align, inner.center().x() - width / 2)
            path = QPainterPath()
            path.addText(x, inner.top() + i * metrics.lineSpacing() + metrics.ascent(), font, line)
            if item.outline > 0:
                pen = QPen(QColor(item.outline_color))
                pen.setWidthF(max(2 * item.outline * pixels * scale, 1.0))
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.drawPath(path)
            p.fillPath(path, QColor(item.color))
        p.setOpacity(1.0)

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
        grabbed = self._overlay_at(event.position())
        if grabbed is not None:
            kind, index, rect = grabbed
            self.set_selected_overlay((kind, index))
            self.overlayPicked.emit(kind, index)
            out = self.output_rect()
            # Where the middle of it is now, as shares of the frame (a logo in a corner starts from there).
            x, y = (rect.center().x() - out.left()) / out.width(), (rect.center().y() - out.top()) / out.height()
            self._overlay_drag = (kind, index, event.position(), x, y)
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
        if self._overlay_drag is not None:
            kind, index, start, x, y = self._overlay_drag
            out = self.output_rect()
            delta = event.position() - start
            if not out.isEmpty():
                self.overlayMoved.emit(kind, index, round(min(max(x + delta.x() / out.width(), 0.0), 1.0), 4),
                                       round(min(max(y + delta.y() / out.height(), 0.0), 1.0), 4))
            return
        if self._drag is None and self._overlay_at(event.position()) is not None:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            return
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
        if self._overlay_drag is not None:
            self._overlay_drag = None
            self.overlayEditFinished.emit()
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
            self._paint_overlays(p, content=False)
            self._badge(p, tr("After"))
            return

        image = self.oriented_image()
        if image is None:
            p.setPen(QColor("#9c9da4"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            return
        frame = self.frame_rect()
        if self.canvas and self.out_size and not self.crop:
            p.fillRect(self.output_rect(), QColor("black"))
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
        self._paint_overlays(p, content=True)
        if self.show_after:
            self._badge(p, tr("Rendering preview…"))

    def _badge(self, p: QPainter, text: str) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        metrics = p.fontMetrics()
        box = QRectF(14, 14, metrics.horizontalAdvance(text) + 16, metrics.height() + 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 160))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
