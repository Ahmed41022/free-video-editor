"""The home page: the projects at the top, the features to start with below."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
    QWidget,
)

from .. import APP_NAME
from ..i18n import isolate, ntr, tr
from ..projects import ProjectInfo, Recovery, format_date, format_date_time
from . import icons
from .features import FEATURES

CARD_WIDTH = 196
THUMB_SIZE = QSize(172, 97)  # a card's picture is never bigger, so 50 cards stay small in memory
BATCH = 6  # project cards made per turn of the event loop, so the page stays responsive


class CardGrid(QWidget):
    """Cards in rows: as many to a row as fit, laid out again when the width changes."""

    def __init__(self, card_width: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.card_width = card_width
        self.cards: list[QWidget] = []
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(12)
        self._columns = 0

    def add(self, card: QWidget) -> None:
        self.cards.append(card)
        self._place(card, len(self.cards) - 1)

    def clear(self) -> None:
        for card in self.cards:
            self._grid.removeWidget(card)
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self.cards = []

    def _per_row(self) -> int:
        return max(1, (self.width() + self._grid.spacing()) // (self.card_width + 12))

    def _place(self, card: QWidget, index: int) -> None:
        columns = self._columns or self._per_row()
        self._grid.setColumnStretch(columns, 1)  # the cards stay together at the start of a row
        self._grid.addWidget(
            card, index // columns, index % columns,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeading)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        columns = self._per_row()
        if columns != self._columns and self.cards:
            for card in self.cards:
                self._grid.removeWidget(card)
            self._columns = columns
            for index, card in enumerate(self.cards):
                self._place(card, index)
        self._columns = columns


def _label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    label.setWordWrap(wrap)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    return label


class FeatureCard(QPushButton):
    """A feature to start with."""

    def __init__(self, feature, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.feature = feature
        self.setObjectName("card")
        self.setFixedSize(CARD_WIDTH, 112)
        self.setToolTip(tr(feature.text))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        picture = _label("")
        picture.setPixmap(icons.icon(feature.icon, size=22).pixmap(22, 22))
        picture.setFixedHeight(24)
        layout.addWidget(picture)
        layout.addWidget(_label(tr(feature.title), "cardTitle"))
        layout.addWidget(_label(tr(feature.text), "muted", wrap=True), 1)
        self.setAccessibleName(tr(feature.title))

    def sizeHint(self) -> QSize:
        return QSize(CARD_WIDTH, 112)


class ProjectCard(QPushButton):
    """A project: its picture, name and the day it was last saved. Clicking it opens it."""
    renameRequested = Signal(str)
    deleteRequested = Signal(str)

    def __init__(self, info: ProjectInfo, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.info = info
        self.setObjectName("card")
        self.setFixedSize(CARD_WIDTH, 196)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(4)
        self.picture = _label("")
        self.picture.setObjectName("cardPicture")
        self.picture.setFixedSize(THUMB_SIZE)
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.load_picture()
        layout.addWidget(self.picture, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = _label("", "cardTitle")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setText(
            self.title.fontMetrics().elidedText(
                info.name, Qt.TextElideMode.ElideRight, CARD_WIDTH - 24))
        layout.addWidget(self.title)
        self.date = _label(
            tr("Saved {date}", date=isolate(format_date(info.saved))) if info.saved else "",
            "muted")
        self.date.setToolTip(format_date_time(info.saved))
        layout.addWidget(self.date)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.rename_button = QPushButton(tr("Rename"))
        self.rename_button.setObjectName("chip")
        self.rename_button.clicked.connect(lambda: self.renameRequested.emit(info.id))
        self.delete_button = QPushButton(tr("Delete"))
        self.delete_button.setObjectName("chip")
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(info.id))
        row.addWidget(self.rename_button)
        row.addWidget(self.delete_button)
        layout.addLayout(row)
        self.setToolTip(
            ntr(info.file_count, "{n} file", "{n} files") + "\n" + format_date_time(info.saved))
        self.setAccessibleName(info.name)

    def sizeHint(self) -> QSize:
        return QSize(CARD_WIDTH, 196)

    def load_picture(self) -> None:
        """Read the small picture the project keeps, already shrunk to the size shown."""
        if self.info.thumbnail is None:
            self.picture.setPixmap(icons.icon("film", size=40).pixmap(40, 40))
            return
        reader = QImageReader(str(self.info.thumbnail))
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(size.scaled(THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            self.picture.setPixmap(icons.icon("film", size=40).pixmap(40, 40))
        else:
            self.picture.setPixmap(QPixmap.fromImage(image))


class RecoveryRow(QFrame):
    """Work that a run which didn't end cleanly left behind."""
    recoverRequested = Signal(object)
    discardRequested = Signal(object)

    def __init__(self, recovery: Recovery, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.recovery = recovery
        self.setObjectName("section")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        text = tr("Recover the work from {date}", date=isolate(format_date_time(recovery.saved)))
        self.label = QLabel(text)
        self.label.setObjectName("cardTitle")
        row.addWidget(self.label, 1)
        row.addWidget(QLabel(ntr(recovery.file_count, "{n} file", "{n} files")), 0)
        self.recover_button = QPushButton(tr("Recover"))
        self.recover_button.setObjectName("primary")
        self.recover_button.clicked.connect(lambda: self.recoverRequested.emit(recovery))
        self.discard_button = QPushButton(tr("Discard"))
        self.discard_button.clicked.connect(lambda: self.discardRequested.emit(recovery))
        row.addWidget(self.recover_button)
        row.addWidget(self.discard_button)


class HomePage(QScrollArea):
    """Projects first (recovered work, then the cards), then the features."""
    newProjectRequested = Signal()
    openProjectRequested = Signal(str)
    renameProjectRequested = Signal(str)
    deleteProjectRequested = Signal(str)
    recoverRequested = Signal(object)
    discardRecoveryRequested = Signal(object)
    featureChosen = Signal(str)
    startRequested = Signal()  # start in general: add files and edit
    backToEditorRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("home")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self.setWidget(content)
        outer = QVBoxLayout(content)
        outer.setContentsMargins(32, 24, 32, 24)
        outer.setSpacing(14)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("homeTitle")
        titles.addWidget(title)
        subtitle = QLabel(tr("Pick up where you left off, or start with the feature you need."))
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        titles.addWidget(subtitle)
        head.addLayout(titles, 1)
        self.back_button = QPushButton(tr("Back to editing"))
        self.back_button.setObjectName("primary")
        self.back_button.clicked.connect(self.backToEditorRequested)
        self.back_button.hide()
        head.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)

        self.recovery_box = QWidget()
        self.recovery_layout = QVBoxLayout(self.recovery_box)
        self.recovery_layout.setContentsMargins(0, 0, 0, 0)
        self.recovery_layout.setSpacing(6)
        self.recovery_box.hide()
        outer.addWidget(self.recovery_box)

        row = QHBoxLayout()
        heading = QLabel(tr("Projects"))
        heading.setObjectName("heading")
        row.addWidget(heading)
        row.addStretch(1)
        self.new_button = QPushButton(tr("New project"))
        self.new_button.setObjectName("primary")
        self.new_button.clicked.connect(self.newProjectRequested)
        row.addWidget(self.new_button)
        outer.addLayout(row)
        self.empty_label = QLabel(
            tr(
                "No projects yet. A project keeps your editing, so you can close the app and "
                "carry on later."))
        self.empty_label.setObjectName("muted")
        self.empty_label.setWordWrap(True)
        outer.addWidget(self.empty_label)
        self.project_grid = CardGrid(CARD_WIDTH)
        outer.addWidget(self.project_grid)

        row = QHBoxLayout()
        heading = QLabel(tr("Start with a feature"))
        heading.setObjectName("heading")
        row.addWidget(heading)
        row.addStretch(1)
        self.start_button = QPushButton(tr("Start editing"))
        self.start_button.setToolTip(tr("Open the editor and add files, with every setting open"))
        self.start_button.clicked.connect(self.startRequested)
        row.addWidget(self.start_button)
        outer.addLayout(row)
        self.feature_grid = CardGrid(CARD_WIDTH)
        self.feature_cards: dict[str,
                                 FeatureCard] = {}  # made a few at a time, after the first paint
        outer.addWidget(self.feature_grid)
        outer.addStretch(1)

        self.project_cards: list[ProjectCard] = []
        self._projects: list[ProjectInfo] = []
        self._features = list(FEATURES)
        self._timer = QTimer(self, singleShot=True, interval=0, timeout=self._make_batch)
        self._timer.start()

    @property
    def loading(self) -> bool:
        """Whether cards are still being made."""
        return bool(self._projects or self._features)

    def finish(self) -> None:
        """Make every card now (the page normally makes them a few at a time)."""
        while self.loading:
            self._make_batch()

    def set_work(self, name: Optional[str]) -> None:
        """Offer a way back to the work that is open (`name`: its project's name, or "" for work
        that is not in a project); None means there is none."""
        self.back_button.setVisible(name is not None)
        if name is not None:
            self.back_button.setText(
                tr("Back to {name}", name=isolate(name)) if name else tr("Back to editing"))

    def set_recoveries(self, recoveries: list[Recovery]) -> None:
        while self.recovery_layout.count():
            item = self.recovery_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for recovery in recoveries:
            row = RecoveryRow(recovery)
            row.recoverRequested.connect(self.recoverRequested)
            row.discardRequested.connect(self.discardRecoveryRequested)
            self.recovery_layout.addWidget(row)
        self.recovery_box.setVisible(bool(recoveries))

    def set_projects(self, projects: list[ProjectInfo]) -> None:
        """Show the projects. The cards are made a few at a time, so a long list doesn't hold up
        the window."""
        self._timer.stop()
        self.project_grid.clear()
        self.project_cards = []
        self._projects = list(projects)
        self.empty_label.setVisible(not projects)
        self._timer.start()

    def _make_batch(self) -> None:
        made = 0
        while self._projects and made < BATCH:
            info = self._projects.pop(0)
            card = ProjectCard(info)
            card.clicked.connect(lambda _=False, key=info.id: self.openProjectRequested.emit(key))
            card.renameRequested.connect(self.renameProjectRequested)
            card.deleteRequested.connect(self.deleteProjectRequested)
            self.project_grid.add(card)
            self.project_cards.append(card)
            made += 1
        while not self._projects and self._features and made < BATCH:
            feature = self._features.pop(0)
            card = FeatureCard(feature)
            card.clicked.connect(lambda _=False, key=feature.id: self.featureChosen.emit(key))
            self.feature_grid.add(card)
            self.feature_cards[feature.id] = card
            made += 1
        if self.loading:
            self._timer.start()
