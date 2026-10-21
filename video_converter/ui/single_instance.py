"""One editor window per user. Starting the app a second time (for example "Open with" on several files) hands
its file names to the window that is already open, which adds them and comes to the front, and the new copy exits.

The copies talk through a local socket (a named pipe on Windows) named after the user. Which copy is the running one
is decided by a lock file the operating system holds for it: a copy that takes the lock is the running one (and
keeps it until it quits or crashes), so two copies started at the same moment can't both win."""

from __future__ import annotations

import getpass
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ..locks import FileLock, LockTimeout

log = logging.getLogger(__name__)

ACK = b"ok\n"
MAX_MESSAGE = 4 * 1024 * 1024       # a longer "list of files" is not something a person sent
CONNECT_MS = 800                     # how long a second copy waits for the first to answer
ANSWER_MS = 3000
HANDOVER_SECONDS = 5.0               # how long a second copy keeps trying while the first one is still starting
DISABLE_VARIABLE = "VIDEO_EDITOR_ALLOW_SEVERAL"      # set to 1 to run several copies at once (for developers)


def instance_name() -> str:
    """The socket name: the same for every copy run by the same user, different for another user."""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    safe = "".join(ch if ch.isalnum() else "_" for ch in user)
    return f"FreeVideoEditor-{safe}"


def encode_paths(paths: list[Path | str]) -> bytes:
    """The message a second copy sends: its file names, as absolute paths, on one line."""
    absolute = []
    for path in paths:
        try:
            absolute.append(str(Path(path).resolve()))
        except OSError:
            absolute.append(str(path))
    return json.dumps({"paths": absolute}).encode("utf-8") + b"\n"


def decode_paths(message: bytes) -> list[str]:
    """The file names in a message from another copy ([] if it is not one)."""
    try:
        data = json.loads(message.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    paths = data.get("paths") if isinstance(data, dict) else None
    return [p for p in paths if isinstance(p, str) and p] if isinstance(paths, list) else []


def send_paths(name: str, paths: list[Path | str], connect_ms: Optional[int] = None,
               answer_ms: Optional[int] = None) -> bool:
    """Hand `paths` to the copy that listens on `name`. True once it has taken them. False when no copy answers
    (nobody listens, or a crashed one left its name behind)."""
    connect_ms = CONNECT_MS if connect_ms is None else connect_ms
    answer_ms = ANSWER_MS if answer_ms is None else answer_ms
    socket = QLocalSocket()
    try:
        socket.connectToServer(name)
        if not socket.waitForConnected(connect_ms):
            return False
        socket.write(encode_paths(paths))
        if not socket.waitForBytesWritten(answer_ms):
            return False
        answered = socket.bytesAvailable() > 0 or socket.waitForReadyRead(answer_ms)
        return answered and bytes(socket.readAll()).startswith(ACK)
    finally:
        socket.abort()
        socket.deleteLater()


class SingleInstance(QObject):
    """Listens for other copies of the app. `pathsReceived` carries the file names they were started with
    (an empty list when they had none: the window should just come to the front)."""

    pathsReceived = Signal(list)

    def __init__(self, name: Optional[str] = None, lock_folder: Optional[Path] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.name = name or instance_name()
        self.lock = FileLock(Path(lock_folder or tempfile.gettempdir()) / f"{self.name}.lock", timeout=0)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        self._buffers: dict[QLocalSocket, bytearray] = {}

    @property
    def listening(self) -> bool:
        return self.server.isListening()

    def listen(self) -> bool:
        """Become the running copy: take the lock (which the operating system gives up when this program ends, so a
        crash leaves nothing behind) and listen for the others. Qt's own listen() would not do as the lock on
        Windows: it lets several servers share one name."""
        try:
            self.lock.acquire()
        except LockTimeout:
            return False
        except OSError as exc:
            log.error("Couldn't make the lock file %s: %s", self.lock.path, exc)
            return False
        QLocalServer.removeServer(self.name)         # a socket file a crashed copy left behind (not on Windows)
        if self.server.listen(self.name):
            return True
        log.error("Couldn't listen on %s: %s", self.name, self.server.errorString())
        self.lock.release()
        return False

    def close(self) -> None:
        """Stop being the running copy, so a copy started next (as a restart does) can take over."""
        if self.server.isListening():
            self.server.close()
        QLocalServer.removeServer(self.name)
        self.lock.release()

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._forget(s))
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        buffer = self._buffers.get(socket)
        if buffer is None:
            return
        buffer += bytes(socket.readAll())
        if len(buffer) > MAX_MESSAGE:
            socket.abort()
            return
        if b"\n" not in buffer:
            return
        message = bytes(buffer.split(b"\n", 1)[0])
        buffer.clear()
        paths = decode_paths(message)
        socket.write(ACK)
        socket.flush()
        self.pathsReceived.emit(paths)

    def _forget(self, socket: QLocalSocket) -> None:
        self._buffers.pop(socket, None)
        socket.deleteLater()


def claim(paths: list[Path | str], name: Optional[str] = None,
          lock_folder: Optional[Path] = None) -> Optional[SingleInstance]:
    """Become the one running copy, or hand `paths` to it.

    Returns the `SingleInstance` to keep alive (listening; or, when another copy holds the lock but never answers,
    one that is not, and this copy simply runs on its own), or None when another copy took the paths and this
    one should exit."""
    instance = SingleInstance(name, lock_folder)
    if os.environ.get(DISABLE_VARIABLE) == "1":
        return instance
    if instance.listen():
        return instance
    # Another copy is the running one. It may still be starting up and not listening yet, so ask for a few seconds.
    allow_foreground()
    deadline = time.monotonic() + HANDOVER_SECONDS
    while True:
        if send_paths(instance.name, paths):
            instance.deleteLater()
            return None
        if time.monotonic() >= deadline:
            break
        time.sleep(0.15)
        if instance.listen():           # the running copy quit or crashed while we were asking: take over
            return instance
    log.error("Another copy of the app holds %s but doesn't answer; starting on its own", instance.lock.path)
    return instance


def allow_foreground() -> None:
    """Let the copy that receives the files bring its window to the front (Windows only lets the process
    that is in front, or one it allows, do that)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)       # ASFW_ANY
    except (OSError, AttributeError):
        pass
