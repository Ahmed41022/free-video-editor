"""Projects: the editing progress that is kept between runs, and the recovery copy of work that
is not in a project.

A project is one small JSON file in the projects folder (named by a number that never changes, so
renaming never moves files), plus a thumbnail picture next to it. Writes are atomic: the new
content goes into a temporary file that then replaces the old one, so a crash or a power cut
leaves either the old project or the new one, never half of one. Nothing here uses Qt, and
listing the projects only reads the JSON files (it never looks at a video)."""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
FOLDER_VARIABLE = "VIDEO_EDITOR_PROJECTS_DIR"  # where the projects are kept (for developers)
NO_QUIT_PROMPT_VARIABLE = "VIDEO_EDITOR_NO_QUIT_PROMPT"  # set by the tests: closing never asks
MAX_NAME_LENGTH = 80
RECOVERY_PREFIX = "recovery-"
THUMBNAIL_SUFFIX = ".jpg"


class ProjectError(Exception):
    """A project that can't be read, or a name that can't be used. The text is for the user, in
    the language in use, and is made by the caller that shows it."""


class InvalidName(ProjectError):
    """The name is empty, too long or has characters a name can't have."""


class DuplicateName(ProjectError):
    """Another project already has this name (names are compared without regard to case)."""


class UnreadableProject(ProjectError):
    """The file isn't a project this version can read."""


def default_folder() -> Path:
    """`%LOCALAPPDATA%\\Free Video Editor\\projects` on Windows, the XDG data folder elsewhere."""
    override = os.environ.get(FOLDER_VARIABLE)
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Free Video Editor" / "projects"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "free-video-editor" / "projects"


def quit_prompt_enabled() -> bool:
    return not os.environ.get(NO_QUIT_PROMPT_VARIABLE)


def now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: Any) -> Optional[datetime]:
    """A saved time as an aware datetime, or None for anything else."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_date(when: Optional[datetime]) -> str:
    """A date as day/month/year with slashes, in the user's own time zone: 19/11/2026."""
    if when is None:
        return ""
    return when.astimezone().strftime("%d/%m/%Y")


def format_date_time(when: Optional[datetime]) -> str:
    """The same with the time: 19/11/2026 14:05."""
    if when is None:
        return ""
    return when.astimezone().strftime("%d/%m/%Y %H:%M")


def clean_name(name: str) -> str:
    """The name as it is kept (trimmed, one space between words). Raises InvalidName."""
    text = " ".join(str(name).split())
    if not text or len(text) > MAX_NAME_LENGTH or any(ord(ch) < 32 for ch in text):
        raise InvalidName(text)
    return text


def _same_name(name: str) -> str:
    return " ".join(name.split()).casefold()


@dataclass
class FileEntry:
    path: str
    settings: dict = field(default_factory=dict)  # ClipSettings.to_dict()
    play_original: bool = False


@dataclass
class ProjectData:
    """What a project keeps: the files in order with their settings, the selected file, and the
    name and dates. Not the undo history, and never thumbnails, waveforms or smooth previews
    (those are rebuilt when a file loads)."""
    id: str
    name: str
    created: str = ""
    saved: str = ""
    selected: int = -1
    files: list[FileEntry] = field(default_factory=list)
    thumbnail_of: str = ""  # the file the thumbnail picture came from

    def to_json(self) -> dict[str, Any]:
        return {
            "format":
            FORMAT_VERSION,
            "id":
            self.id,
            "name":
            self.name,
            "created":
            self.created,
            "saved":
            self.saved,
            "selected":
            self.selected,
            "thumbnail_of":
            self.thumbnail_of,
            "files": [
                {
                    "path": f.path,
                    "settings": f.settings,
                    "play_original": f.play_original
                } for f in self.files
            ]
        }

    @classmethod
    def from_json(cls, data: Any, fallback_id: str = "") -> "ProjectData":
        if not isinstance(data, dict):
            raise UnreadableProject("not a project")
        version = data.get("format")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise UnreadableProject("no format version")
        if version > FORMAT_VERSION:
            raise UnreadableProject(f"made by a newer version (format {version})")
        files = []
        for entry in data.get("files") if isinstance(data.get("files"), list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"]:
                settings = entry.get("settings")
                files.append(
                    FileEntry(
                        entry["path"], settings if isinstance(settings, dict) else {},
                        entry.get("play_original") is True))
        selected = data.get("selected")
        name, ident = data.get("name"), data.get("id")
        return cls(
            id=ident if isinstance(ident, str) and ident else fallback_id,
            name=name if isinstance(name, str) else "", created=_text(data.get("created")),
            saved=_text(data.get("saved")), selected=selected
            if isinstance(selected, int) and not isinstance(selected, bool) else -1, files=files,
            thumbnail_of=_text(data.get("thumbnail_of")))


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


@dataclass
class ProjectInfo:
    """A line of the list on the home page."""
    id: str
    name: str
    saved: Optional[datetime]
    file_count: int
    thumbnail: Optional[Path]


@dataclass
class Recovery:
    """Work left behind by a run that didn't end cleanly."""
    path: Path
    saved: Optional[datetime]
    file_count: int


def write_json(path: Path, data: Any) -> None:
    """Replace `path` with `data`, all at once: a temporary file next to it, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


class ProjectStore:
    """The projects folder."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    # ----- paths

    def project_path(self, ident: str) -> Path:
        return self.folder / f"{ident}.json"

    def thumbnail_path(self, ident: str) -> Path:
        return self.folder / f"{ident}{THUMBNAIL_SUFFIX}"

    def recovery_path(self, session: str) -> Path:
        return self.folder / f"{RECOVERY_PREFIX}{session}.json"

    # ----- the list

    def list(self) -> list[ProjectInfo]:
        """Every readable project, the last saved first. Only the JSON files are read."""
        found = []
        for path in self._json_files():
            if path.name.startswith(RECOVERY_PREFIX):
                continue
            try:
                data = ProjectData.from_json(
                    json.loads(path.read_text(encoding="utf-8")), path.stem)
            except (OSError, ValueError, ProjectError) as exc:
                log.warning("Skipping %s: %s", path.name, exc)
                continue
            thumbnail = self.thumbnail_path(data.id)
            found.append(
                ProjectInfo(
                    data.id, data.name, parse_time(data.saved), len(data.files),
                    thumbnail if thumbnail.is_file() else None))
        epoch = datetime.fromtimestamp(0, timezone.utc)
        found.sort(key=lambda info: info.saved or epoch, reverse=True)
        return found

    def _json_files(self) -> list[Path]:
        try:
            return [p for p in self.folder.glob("*.json") if p.is_file()]
        except OSError:
            return []

    def names(self, exclude: Optional[str] = None) -> list[str]:
        return [info.name for info in self.list() if info.id != exclude]

    def existing_name(self, name: str, exclude: Optional[str] = None) -> Optional[str]:
        """The name another project has that counts as `name` (capitals don't count), as that
        project saved it, or None if the name is free."""
        wanted = _same_name(name)
        return next((other for other in self.names(exclude) if _same_name(other) == wanted), None)

    def name_taken(self, name: str, exclude: Optional[str] = None) -> bool:
        wanted = _same_name(name)
        return any(_same_name(other) == wanted for other in self.names(exclude))

    def suggest_name(self, base: str) -> str:
        """`base`, or `base 2`, `base 3`... whichever is free."""
        taken = {_same_name(n) for n in self.names()}
        if _same_name(base) not in taken:
            return base
        number = 2
        while _same_name(f"{base} {number}") in taken:
            number += 1
        return f"{base} {number}"

    # ----- one project

    def create(self, name: str) -> ProjectData:
        """Make an empty project (raises InvalidName or DuplicateName)."""
        name = clean_name(name)
        if self.name_taken(name):
            raise DuplicateName(name)
        stamp = now_text()
        project = ProjectData(uuid.uuid4().hex, name, stamp, stamp)
        self.save(project, stamp=False)
        return project

    def load(self, ident: str) -> ProjectData:
        path = self.project_path(ident)
        try:
            return ProjectData.from_json(json.loads(path.read_text(encoding="utf-8")), ident)
        except ValueError as exc:
            raise UnreadableProject(str(exc)) from exc
        except OSError as exc:
            raise UnreadableProject(exc.strerror or str(exc)) from exc

    def save(self, project: ProjectData, stamp: bool = True) -> None:
        if stamp:
            project.saved = now_text()
        write_json(self.project_path(project.id), project.to_json())

    def rename(self, ident: str, name: str) -> str:
        """Give a project another name (raises InvalidName or DuplicateName); returns the name
        as kept."""
        name = clean_name(name)
        if self.name_taken(name, exclude=ident):
            raise DuplicateName(name)
        project = self.load(ident)
        project.name = name
        self.save(project, stamp=False)
        return name

    def delete(self, ident: str) -> None:
        for path in (self.project_path(ident), self.thumbnail_path(ident)):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Couldn't delete %s: %s", path, exc)

    # ----- recovery

    def write_recovery(self, session: str, project: ProjectData) -> None:
        project.saved = now_text()
        write_json(self.recovery_path(session), project.to_json())

    def remove_recovery(self, session: str) -> None:
        try:
            self.recovery_path(session).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Couldn't remove the recovery file: %s", exc)

    def recoveries(self, own_session: str = "") -> list[Recovery]:
        """Work of other runs that was never saved or discarded, the latest first."""
        found = []
        for path in self._json_files():
            if not path.name.startswith(RECOVERY_PREFIX) or path == self.recovery_path(
                    own_session):
                continue
            try:
                data = ProjectData.from_json(
                    json.loads(path.read_text(encoding="utf-8")), path.stem)
            except (OSError, ValueError, ProjectError) as exc:
                log.warning("Skipping %s: %s", path.name, exc)
                continue
            if data.files:
                found.append(Recovery(path, parse_time(data.saved), len(data.files)))
        epoch = datetime.fromtimestamp(0, timezone.utc)
        found.sort(key=lambda r: r.saved or epoch, reverse=True)
        return found

    def load_recovery(self, path: Path) -> ProjectData:
        try:
            return ProjectData.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise UnreadableProject(str(exc)) from exc

    def recovery_session(self, path: Path) -> str:
        name = Path(path).stem
        return name[len(RECOVERY_PREFIX):] if name.startswith(RECOVERY_PREFIX) else name
