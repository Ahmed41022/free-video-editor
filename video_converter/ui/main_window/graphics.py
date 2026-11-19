"""Looking for graphics-card encoders in the background."""

from __future__ import annotations

import os
import threading

from ... import gpu

# The search for graphics-card encoders starts a few seconds after the first file has loaded (or at the latest this
# long after the window opened), so it never competes with the first playback.
GPU_SEARCH_DELAY_MS = 4000
GPU_SEARCH_LATEST_MS = 20000


class GraphicsMixin:
    """The search for graphics-card encoders."""

    def start_gpu_search(self) -> None:
        """Look for graphics-card encoders in the background, once. It is started a few seconds after the first
        file loaded rather than when the window opens, so it doesn't compete with the first playback."""
        if self._gpu_started or self.closed:
            return
        self._gpu_started = True
        self._gpu_timer.stop()
        if os.environ.get(
                "VIDEO_EDITOR_NO_GPU_SEARCH") == "1":  # set by the tests: never touch the card
            self.signals.gpuReady.emit(((), None))
            return
        threading.Thread(target=self._find_gpu, name="gpu-search", daemon=True).start()

    def _find_gpu(self) -> None:
        """Background thread: find out which hardware encoders work (checked once per run)."""
        try:
            found = gpu.detect_encoders(self.ffmpeg)
            name = gpu.gpu_name(found[0]) if found else None
        except Exception:  # never let this stop the app
            found, name = (), None
        self.signals.gpuReady.emit((found, name))

    def _on_gpu_ready(self, result) -> None:
        found, name = result
        self.gpu_encoders = found
        self.inspector.set_gpu_name(name)
