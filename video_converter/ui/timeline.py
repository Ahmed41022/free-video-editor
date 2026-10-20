"""Timeline: thumbnails, audio waveform, trim handles, cut-out sections and the playhead."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QContextMenuEvent, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core import format_time
from ..timing import MIN_PART, Range, normalize_keep, removed_parts, split_points
from . import theme

MARGIN = 12          # space left and right of the track, so handles at the ends stay grabbable
RULER_H = 18
THUMBS_H = 46
WAVE_H = 30
TEXT_H = 8           # the thin track that shows when each text is on screen
HANDLE_GRAB = 7      # pixels either side of a handle that start dragging it
SNAP_EDGE = 0.05     # seconds; trims this close to the ends mean "no trim"
_EPS = 1e-6


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


def cut_edges(keep: list[Range], duration: float) -> list[tuple[float, int, str]]:
    """The draggable edges of the cut-out sections: (time, kept range index, side).

    "start" and "end" move one side of a kept range; "split" moves the point where
    range i meets range i + 1."""
    edges = []
    for i, (a, b) in enumerate(keep):
        joined_before = i > 0 and abs(a - keep[i - 1][1]) <= _EPS
        if a > _EPS and not joined_before:
            edges.append((a, i, "start"))
        if i + 1 < len(keep) and abs(keep[i + 1][0] - b) <= _EPS:
            edges.append((b, i, "split"))
        elif b < duration - _EPS:
            edges.append((b, i, "end"))
    return edges


def move_edge(keep: list[Range], index: int, side: str, t: float, duration: float) -> list[Range]:
    """Kept ranges with one edge moved to `t`, kept clear of its neighbours."""
    keep = list(keep)
    a, b = keep[index]
    if side == "start":
        low = keep[index - 1][1] if index > 0 else 0.0
        keep[index] = (min(max(t, low), b - MIN_PART), b)
    elif side == "end":
        high = keep[index + 1][0] if index + 1 < len(keep) else duration
        keep[index] = (a, max(min(t, high), a + MIN_PART))
    else:
        next_end = keep[index + 1][1]
        t = min(max(t, a + MIN_PART), next_end - MIN_PART)
        keep[index], keep[index + 1] = (a, t), (t, next_end)
    return keep


class Timeline(QWidget):
    seekRequested = Signal(float)
    trimChanged = Signal(object, object)   # start, end (None = untrimmed); emitted while dragging
    trimEditFinished = Signal()
    keepChanged = Signal(object)           # kept ranges; emitted while dragging a cut's edge
    keepEditFinished = Signal()
    menuRequested = Signal(float, QPoint)  # right-click: source time, global position

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMinimumHeight(RULER_H + THUMBS_H + WAVE_H + TEXT_H + 14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)      # time runs left to right in every language
        self.duration = 0.0
        self.position = 0.0
        self.start: Optional[float] = None
        self.end: Optional[float] = None
        self.keep: list[Range] = []
        self.thumbnails: list[QPixmap] = []
        self.waveform: list[float] = []
        self.text_spans: list[list[Range]] = []     # for each text, the parts of the source it is shown over
        self.text_selected = -1
        self._drag: Optional[str] = None   # "in", "out", "cut" or "playhead"
        self._edge: Optional[tuple[int, str]] = None

    # ----- data ---------------------------------------------------------------

    def clear(self) -> None:
        self.duration = self.position = 0.0
        self.start = self.end = None
        self.keep = []
        self.thumbnails, self.waveform = [], []
        self.text_spans, self.text_selected = [], -1
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

    def setKeep(self, keep: list[Range]) -> None:
        if self._drag != "cut":
            self.keep = list(keep)
            self.update()

    def setTexts(self, spans: list[list[Range]], selected: int = -1) -> None:
        """Show when the texts are on screen: `spans[i]` are the parts of the source (in source seconds)
        text i plays over, `selected` the one being edited."""
        self.text_spans, self.text_selected = [list(s) for s in spans], selected
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

    def edge_at(self, x: float) -> Optional[tuple[int, str]]:
        """The cut edge within reach of `x`, if any."""
        best, best_d = None, HANDLE_GRAB + 0.5
        for t, index, side in cut_edges(self.keep, self.duration):
            d = abs(x - self._x(t))
            if d < best_d:
                best, best_d = (index, side), d
        return best

    def hit_test(self, x: float) -> str:
        """What a press at `x` grabs: trim handles win over cut edges, which win over the playhead."""
        if self.duration <= 0:
            return "none"
        in_x, out_x = self._x(self._in_time()), self._x(self._out_time())
        d_in, d_out = abs(x - in_x), abs(x - out_x)
        if min(d_in, d_out) <= HANDLE_GRAB:
            return "in" if d_in < d_out or (d_in == d_out and x <= in_x) else "out"
        if self.edge_at(x) is not None:
            return "cut"
        return "playhead"

    # ----- mouse --------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.duration <= 0:
            return
        x = event.position().x()
        self._drag = self.hit_test(x)
        self._edge = self.edge_at(x) if self._drag == "cut" else None
        self._drag_to(x)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        x = event.position().x()
        if self._drag:
            self._drag_to(x)
        else:
            grab = self.hit_test(x) in ("in", "out", "cut")
            self.setCursor(Qt.CursorShape.SizeHorCursor if grab else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        drag, self._drag = self._drag, None
        if drag in ("in", "out"):
            self.trimEditFinished.emit()
        elif drag == "cut":
            self._edge = None
            tidy = normalize_keep(self.keep, self.duration)
            if tidy != self.keep:
                self.keep = tidy
                self.keepChanged.emit(list(tidy))
            self.keepEditFinished.emit()
        self.update()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if self.duration > 0 and self._drag is None:
            self.menuRequested.emit(self._t(event.pos().x()), event.globalPos())

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
        elif self._drag == "cut" and self._edge is not None:
            index, side = self._edge
            keep = move_edge(self.keep, index, side, t, self.duration)
            if keep != self.keep:
                self.keep = keep
                self.keepChanged.emit(list(keep))
            # Show the frame that now starts (or, for an "end" edge, ends) the kept part.
            a, b = keep[index]
            self.seekRequested.emit(a if side == "start" else b if side == "split" else max(b - 0.04, 0.0))
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

        top, bottom = thumbs.top(), wave.bottom()

        # texts: one thin bar for each stretch of source a text is shown over
        if self.text_spans:
            lane = QRectF(left, bottom + 4, width, TEXT_H)
            p.fillRect(lane, QColor(c["track"]))
            p.setPen(Qt.PenStyle.NoPen)
            for i, spans in enumerate(self.text_spans):
                color = QColor("#f5b83d" if i == self.text_selected else c["accent"])
                color.setAlpha(230 if i == self.text_selected else 150)
                p.setBrush(color)
                for a, b in spans:
                    x1, x2 = self._x(a), self._x(b)
                    p.drawRoundedRect(QRectF(x1, lane.top(), max(x2 - x1, 2.0), TEXT_H), 2, 2)

        # cut-out sections: dark, hatched, with grabbable edges
        danger = QColor(c["danger"])
        for a, b in removed_parts(self.keep, self.duration):
            area = QRectF(self._x(a), top, self._x(b) - self._x(a), bottom - top)
            p.fillRect(area, QColor(0, 0, 0, 170))
            hatch = QColor(danger)
            hatch.setAlpha(150)
            p.fillRect(area, QBrush(hatch, Qt.BrushStyle.BDiagPattern))
            for x in (area.left(), area.right()):
                p.fillRect(QRectF(x - 1.5, top, 3, bottom - top), danger)
        split_pen = QPen(QColor(c["text"]))
        split_pen.setWidthF(2)
        split_pen.setStyle(Qt.PenStyle.DashLine)
        for t in split_points(self.keep):
            x = self._x(t)
            p.setPen(split_pen)
            p.drawLine(QPointF(x, top), QPointF(x, bottom))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["text"]))
            p.drawPolygon([QPointF(x - 4, top - 1), QPointF(x + 4, top - 1), QPointF(x, top + 5)])

        # trimmed-away parts
        shade = QColor(0, 0, 0, 150)
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
