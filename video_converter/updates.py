"""Telling the user when a newer version is out: one plain request to GitHub, at most once a day.
Nothing is sent but the request itself (with the app's name and version as the User-Agent)."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from . import APP_NAME, REPO_URL, __version__

log = logging.getLogger(__name__)

API_URL = "https://api.github.com/repos/Ahmed41022/free-video-editor/releases/latest"
TIMEOUT = 5.0                    # seconds; a slow network never holds anything up
CHECK_INTERVAL = 24 * 60 * 60    # seconds between two checks
MAX_BYTES = 1_000_000            # a release's description can be long; the answer is read no further than this

# The settings kept between runs.
ENABLED_KEY = "checkForUpdates"
LAST_CHECK_KEY = "updateLastCheck"
SKIPPED_KEY = "updateSkippedVersion"


@dataclass(frozen=True)
class Release:
    version: str       # "2.9.1", without the "v"
    url: str           # the page to download it from


class Store(Protocol):
    """Where the settings live (a thin layer over QSettings in the app)."""
    def get(self, key: str, default): ...
    def set(self, key: str, value) -> None: ...


_VERSION = re.compile(r"^\s*v?(\d+(?:\.\d+)*)(.*)$", re.IGNORECASE)


def parse_version(text: str) -> Optional[tuple[tuple[int, ...], bool]]:
    """Read a tag such as "v2.10.0" or "2.9.0-rc1" as (numbers, is_release); None if it isn't a version.
    A tag with anything after the numbers ("-rc1", "-beta") is not a release of those numbers yet."""
    match = _VERSION.match(text or "")
    if not match:
        return None
    numbers = tuple(int(part) for part in match.group(1).split("."))
    while len(numbers) > 1 and numbers[-1] == 0:      # 2.9 is 2.9.0
        numbers = numbers[:-1]
    return numbers, not match.group(2).strip()


def is_newer(tag: str, current: str = __version__) -> bool:
    """Whether the version `tag` is newer than `current`, comparing the numbers (2.10.0 is newer than 2.9.0)."""
    latest, mine = parse_version(tag), parse_version(current)
    if latest is None or mine is None:
        return False
    return (latest[0], latest[1]) > (mine[0], mine[1])


def release_from(data) -> Optional[Release]:
    """The release described by GitHub's answer, or None if it isn't one. The page to open is always on
    this project's own GitHub page, whatever the answer says."""
    if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str):
        return None
    parsed = parse_version(data["tag_name"])
    if parsed is None:
        return None
    version = ".".join(str(n) for n in parsed[0]) if parsed[1] else data["tag_name"].strip().lstrip("vV")
    url = data.get("html_url")
    if not (isinstance(url, str) and url.startswith(f"{REPO_URL}/")):
        url = f"{REPO_URL}/releases/latest"
    return Release(version, url)


def fetch_latest(timeout: float = TIMEOUT) -> Release:
    """Ask GitHub for the latest release. Raises OSError, ValueError or a urllib error when that fails."""
    request = urllib.request.Request(API_URL, headers={
        "User-Agent": f"{APP_NAME.replace(' ', '-')}/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read(MAX_BYTES).decode("utf-8", errors="replace"))
    release = release_from(data)
    if release is None:
        raise ValueError("GitHub's answer doesn't describe a release")
    return release


class MemoryStore:
    """A copy of the settings for the thread that checks, which must not touch the window's own."""

    def __init__(self, values: Optional[dict] = None):
        self.values = dict(values or {})

    def get(self, key: str, default):
        return self.values.get(key, default)

    def set(self, key: str, value) -> None:
        self.values[key] = value


def check_for_update(store: Store, fetch: Optional[Callable[[], Release]] = None,
                     now: Optional[float] = None) -> Optional[Release]:
    """The newer release to tell the user about, or None. Does nothing when update checks are off, when one
    was made in the last day, when the network fails (that is only logged), or when the newer version is
    the one the user said "Not now" to. Never raises. `fetch` asks GitHub (see `fetch_latest`)."""
    try:
        if not store.get(ENABLED_KEY, True):
            return None
        now = time.time() if now is None else now
        last = store.get(LAST_CHECK_KEY, 0.0)
        if isinstance(last, (int, float)) and 0 <= now - last < CHECK_INTERVAL:
            log.info("Update check skipped: the last one was less than a day ago")
            return None
        store.set(LAST_CHECK_KEY, now)       # a failed try counts too: at most once a day
        release = (fetch or fetch_latest)()
        if not is_newer(release.version):
            log.info("Checked for updates: %s is the latest, this is %s", release.version, __version__)
            return None
        if store.get(SKIPPED_KEY, "") == release.version:
            log.info("Checked for updates: %s is out, but the user skipped it", release.version)
            return None
        log.info("Checked for updates: %s is out, this is %s", release.version, __version__)
        return release
    except Exception as exc:      # the network, GitHub or a damaged setting: never the user's problem
        log.info("Couldn't check for updates: %s", exc)
        return None
