"""Timeline: thumbnails, audio waveform, trim handles and the playhead."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core import format_time
from . import theme

MARGIN = 12          # space left and right of the track, so handles at the ends stay grabbable
RULER_H = 18
THUMBS_H = 46
WAVE_H = 30
HANDLE_GRAB = 7      # pixels either side of a handle that start dragging it
SNAP_EDGE = 0.05     # seconds; trims this close to the ends mean "no trim"


def time_to_x(t: float, duration: float, left: float, width: float) -> float:
    if duration <= 0:
        return left
    return left + max(0.0, min(t / duration, 1.0)) * width


def x_to_time(x: float, duration: float, left: float, width: float) -> float:
    if width <= 0 or duration <= 0:
        return 0.0
    return max(0.0, min((x - left) / width, 1.0)) * duration


def normalize_trim(start: Optional[float], end: Optional[float], duration: float) -> tuple[Optional[float], Optional[float]]:
    """Trims at (or within SNAP_EDGE of) the very start or end are stored as "no trim"."""
    if start is not None and start <= SNAP_EDGE:
        start = None
    if end is not None and duration > 0 and end >= duration - SNAP_EDGE:
        end = None
    return start, end


def tick_step(duration: float, width: float, min_gap_px: float = 70) -> float:
    """A readable ruler step (1, 2, 5, 10, 15, 30 s, 1, 2, 5, 10 min…) for the visible width."""
    if duration <= 0 or width <= 0:
        return 1.0
    for step in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if step / duration * width >= min_gap_px:
            return float(step)
    return 7200.0


class Timeline(QWidget):
    seekRequested = Signal(float)
    trimChanged = Signal(object, object)   # start, end (None = untrimmed); emitted while dragging
    trimEditFinished = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMinimumHeight(RULER_H + THUMBS_H + WAVE_H + 14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.duration = 0.0
        self.position = 0.0
        self.start: Optional[float] = None
        self.end: Optional[float] = None
        self.thumbnails: list[QPixmap] = []
        self.waveform: list[float] = []
        self._drag: Optional[str] = None   # "in", "out" or "playhead"

    # ----- data ---------------------------------------------------------------

    def clear(self) -> None:
        self.duration = self.position = 0.0
        self.start = self.end = None
        self.thumbnails, self.waveform = [], []
        self.update()

    def setDuration(self, duration: float) -> None:
        self.duration = max(duration or 0.0, 0.0)
        self.update()

    def setPosition(self, seconds: float) -> None:
        if self._drag != "playhead":
            self.position = seconds
            self.update()

    def setTrim(self, start: Optional[float], end: Optional[float]) -> None:
        if self._drag not in ("in", "out"):
            self.start, self.end = start, end
            self.update()

    def setThumbnails(self, pixmaps: list[QPixmap]) -> None:
        self.thumbnails = pixmaps
        self.update()

    def setWaveform(self, levels: list[float]) -> None:
        self.waveform = levels
        self.update()

    # ----- geometry -----------------------------------------------------------

    @property
    def track_left(self) -> float:
        return MARGIN

    @property
    def track_width(self) -> float:
        return max(self.width() - 2 * MARGIN, 1)

    def _x(self, t: float) -> float:
        return time_to_x(t, self.duration, self.track_left, self.track_width)

    def _t(self, x: float) -> float:
        return x_to_time(x, self.duration, self.track_left, self.track_width)

    def _in_time(self) -> float:
        return self.start or 0.0

    def _out_time(self) -> float:
        return self.end if self.end is not None else self.duration

    def hit_test(self, x: float) -> str:
        """Which handle a press at `x` grabs; trim handles win over the playhead."""
        if self.duration <= 0:
            return "none"
        in_x, out_x = self._x(self._in_time()), self._x(self._out_time())
        d_in, d_out = abs(x - in_x), abs(x - out_x)
        if min(d_in, d_out) <= HANDLE_GRAB:
            return "in" if d_in < d_out or (d_in == d_out and x <= in_x) else "out"
        return "playhead"

    # ----- mouse --------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.duration <= 0:
            return
        x = event.position().x()
        self._drag = self.hit_test(x)
        self._drag_to(x)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        x = event.position().x()
        if self._drag:
            self._drag_to(x)
        else:
            grab = self.hit_test(x) in ("in", "out")
            self.setCursor(Qt.CursorShape.SizeHorCursor if grab else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        drag, self._drag = self._drag, None
        if drag in ("in", "out"):
            self.trimEditFinished.emit()
        self.update()

    def _drag_to(self, x: float) -> None:
        t = self._t(x)
        min_len = min(0.1, self.duration)
        if self._drag == "in":
            t = min(t, self._out_time() - min_len)
            self.start, self.end = normalize_trim(max(t, 0.0), self.end, self.duration)
            self.trimChanged.emit(self.start, self.end)
            self.seekRequested.emit(self._in_time())
        elif self._drag == "out":
            t = max(t, self._in_time() + min_len)
            self.start, self.end = normalize_trim(self.start, min(t, self.duration), self.duration)
            self.trimChanged.emit(self.start, self.end)
            self.seekRequested.emit(max(self._out_time() - 0.04, 0.0))
        elif self._drag == "playhead":
            self.position = t
            self.seekRequested.emit(t)
        self.update()

    # ----- painting -----------------------------------------------------------

    def paintEvent(self, _event) -> None:
        c = theme.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(c["panel"]))
        left, width = self.track_left, self.track_width
        thumbs = QRectF(left, RULER_H, width, THUMBS_H)
        wave = QRectF(left, RULER_H + THUMBS_H + 4, width, WAVE_H)

        p.setPen(QColor(c["muted"]))
        if self.duration <= 0:
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "The timeline appears when you select a video")
            return

        # ruler
        step = tick_step(self.duration, width)
        t = 0.0
        while t <= self.duration + 1e-6:
            x = self._x(t)
            p.drawLine(QPointF(x, RULER_H - 5), QPointF(x, RULER_H - 1))
            p.drawText(QRectF(x + 3, 0, 80, RULER_H - 3), Qt.AlignmentFlag.AlignVCenter, format_time(t))
            t += step

        # thumbnails
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(thumbs, 5, 5)
        p.setClipPath(clip)
        p.fillRect(thumbs, QColor(c["track"]))
        if self.thumbnails:
            slot = width / len(self.thumbnails)
            for i, pix in enumerate(self.thumbnails):
                target = QRectF(left + i * slot, thumbs.top(), slot + 1, THUMBS_H)
                scaled = pix.scaled(int(target.width()) + 1, THUMBS_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
                sx = (scaled.width() - target.width()) / 2
                p.drawPixmap(target, scaled, QRectF(sx, 0, target.width(), THUMBS_H))
        p.restore()

        # waveform
        p.fillRect(wave, QColor(c["track"]))
        if self.waveform:
            mid, half = wave.center().y(), WAVE_H / 2 - 2
            pen = QPen(QColor(c["accent"]))
            pen.setWidthF(1.0)
            p.setPen(pen)
            n = len(self.waveform)
            columns = int(width)
            for col in range(columns):
                level = self.waveform[min(int(col / columns * n), n - 1)]
                h = max(level * half, 0.5)
                x = left + col + 0.5
                p.drawLine(QPointF(x, mid - h), QPointF(x, mid + h))
        else:
            p.setPen(QColor(c["muted"]))
            p.drawText(wave, Qt.AlignmentFlag.AlignCenter, "No sound")

        # trimmed-away parts
        shade = QColor(0, 0, 0, 150)
        top, bottom = thumbs.top(), wave.bottom()
        in_x, out_x = self._x(self._in_time()), self._x(self._out_time())
        if in_x > left:
            p.fillRect(QRectF(left, top, in_x - left, bottom - top), shade)
        if out_x < left + width:
            p.fillRect(QRectF(out_x, top, left + width - out_x, bottom - top), shade)

        # trim handles
        accent = QColor("#f5b83d")
        for x, side in ((in_x, -1), (out_x, 1)):
            handle = QRectF(x - 4 if side < 0 else x, top, 4, bottom - top)
            p.fillRect(handle, accent)
        pen = QPen(accent)
        pen.setWidthF(2)
        p.setPen(pen)
        p.drawLine(QPointF(in_x, top), QPointF(out_x, top))
        p.drawLine(QPointF(in_x, bottom), QPointF(out_x, bottom))

        # playhead
        x = self._x(self.position)
        pen = QPen(QColor(c["danger"]))
        pen.setWidthF(2)
        p.setPen(pen)
        p.drawLine(QPointF(x, 2), QPointF(x, bottom + 3))
        p.setBrush(QColor(c["danger"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon([QPointF(x - 5, 2), QPointF(x + 5, 2), QPointF(x, 8)])
