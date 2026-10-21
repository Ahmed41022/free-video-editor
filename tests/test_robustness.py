"""Playback, loading and app-lifecycle fixes (3.0.1): software decoding, frame handling, files that never finish
loading, processes that outlive the window, temp folders, the single running copy, and a few cheap limits.

Everything runs offscreen, decodes on the CPU and uses small files."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("VIDEO_EDITOR_NO_GPU_SEARCH", "1")      # a window never searches for graphics cards (see tests/__init__.py)
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    from PySide6.QtCore import QEvent, QSettings
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtMultimedia import QVideoFrame
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter import decoding, media, tempclean
from video_converter.core import ConversionCancelled, MediaInfo, find_ffmpeg
from video_converter.locks import FileLock, LockTimeout
from video_converter.procs import DaemonPool, ProcessRegistry, run_tracked

FFMPEG = find_ffmpeg()
TIMEOUT = 30
SLEEP_FOR_A_MINUTE = [sys.executable, "-c", "import time; time.sleep(60)"]


def setUpModule():
    if QApplication is not None:
        global app
        app = QApplication.instance() or QApplication([])


def pump(seconds, until=lambda: False):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        if QApplication is not None:
            QApplication.processEvents()
        time.sleep(0.01)
    return until()


def make_video(path, seconds=2):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=duration={seconds}:size=320x180:rate=15", "-f", "lavfi", "-i", f"sine=duration={seconds}",
                    "-c:a", "aac", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)], check=True)


def in_thread(fn, *args):
    """Run `fn` on another thread while the test keeps the Qt event loop going; returns (thread, result list)."""
    result = []
    thread = threading.Thread(target=lambda: result.append(fn(*args)), daemon=True)
    thread.start()
    return thread, result


def finish(thread, result, seconds=TIMEOUT):
    pump(seconds, lambda: not thread.is_alive())
    thread.join(1)
    return result[0] if result else None


SECOND_START = (
    "import sys; from pathlib import Path; from PySide6.QtWidgets import QApplication\n"
    "from video_converter.ui import single_instance as si\n"
    "app = QApplication([])\n"
    "if sys.argv[1] == 'fast':\n"
    "    si.ANSWER_MS = si.CONNECT_MS = 200; si.HANDOVER_SECONDS = 0.5\n"
    "kept = si.claim([Path(p) for p in sys.argv[4:]], sys.argv[2], Path(sys.argv[3]))\n"
    "print('exit' if kept is None else 'listening' if kept.listening else 'alone', flush=True)\n"
    "if kept is not None and kept.listening:\n"
    "    import time; end = time.monotonic() + 1.5      # stays the running copy for a moment, as the real app would\n"
    "    while time.monotonic() < end:\n"
    "        app.processEvents(); time.sleep(0.01)\n"
    "    kept.close()\n")


def second_start(name, lock_dir, *paths, fast=False):
    """What a second start of the app does, as a real separate process: returns the process, which prints
    `exit` (it gave its files to the running copy), `listening` (it became the running copy) or `alone` (it
    runs on its own)."""
    return subprocess.Popen(
        [sys.executable, "-c", SECOND_START, "fast" if fast else "normal", name, str(lock_dir), *map(str, paths)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})


def outcome(proc, seconds=60):
    """The word the second start printed, once it ended (the test keeps Qt's events going meanwhile)."""
    pump(seconds, lambda: proc.poll() is not None)
    if proc.poll() is None:
        proc.kill()
    with proc.stdout:
        return proc.stdout.read().strip()


# ----- 1a: software decoding --------------------------------------------------------------------------------------

class DecodingTests(unittest.TestCase):
    def test_the_graphics_card_is_not_asked_to_decode(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(decoding.HW_DEVICES_VARIABLE, None)
            decoding.force_software_decoding()
            # Qt ignores a name it doesn't know, so "none" means no device; an empty value would be dropped by Windows.
            self.assertEqual(os.environ[decoding.HW_DEVICES_VARIABLE], "none")
            self.assertEqual(os.environ["QT_DISABLE_HW_TEXTURES_CONVERSION"], "1")

    @unittest.skipIf(QApplication is None, "PySide6 is not installed")
    def test_the_player_module_sets_it_when_it_is_loaded(self):
        code = ("import os; os.environ.pop('QT_FFMPEG_DECODING_HW_DEVICE_TYPES', None);"
                "import video_converter.ui.player; print(os.environ.get('QT_FFMPEG_DECODING_HW_DEVICE_TYPES'))")
        shown = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               cwd=Path(__file__).resolve().parents[1],
                               env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}).stdout
        self.assertEqual(shown.strip(), "none")

    @unittest.skipIf(QApplication is None, "PySide6 is not installed")
    def test_the_app_sets_it_before_anything_else_is_made(self):
        from video_converter.ui import app as app_module
        order = []
        fake_app = mock.MagicMock()
        fake_qt = mock.MagicMock()
        fake_qt.instance.side_effect = lambda: order.append("application") or fake_app
        with mock.patch.object(app_module, "force_software_decoding", side_effect=lambda: order.append("software")), \
                mock.patch.object(app_module, "QApplication", fake_qt), \
                mock.patch.object(app_module.single_instance, "claim", side_effect=lambda paths: order.append("claim")):
            self.assertEqual(app_module.main([]), 0)
        self.assertEqual(order, ["software", "application", "claim"])


# ----- 1c: frames ---------------------------------------------------------------------------------------------------

def video_frame(width, height):
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(0xFF336699)
    return QVideoFrame(image)


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class FrameTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.player import Player
        self.player = Player()
        self.shown = []
        self.player.frameReady.connect(lambda image, oriented: self.shown.append(image))

    def tearDown(self):
        self.player.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)     # free it now: pump() alone never runs deleteLater

    def test_a_frame_bigger_than_the_view_is_shrunk_and_a_smaller_one_is_not(self):
        self.player.frame_limit = lambda: 500
        self.player._on_frame(video_frame(1920, 1080))
        self.assertEqual((self.shown[0].width(), self.shown[0].height()), (500, 281))
        self.player.frame_painted()
        self.player._on_frame(video_frame(320, 180))
        self.assertEqual((self.shown[1].width(), self.shown[1].height()), (320, 180))

    def test_frames_are_not_shrunk_below_a_useful_size(self):
        self.player.frame_limit = lambda: 100
        self.player._on_frame(video_frame(1920, 1080))
        self.assertEqual(self.shown[0].width(), 480)

    def test_without_a_limit_the_frame_is_left_alone(self):
        self.player._on_frame(video_frame(1920, 1080))
        self.assertEqual(self.shown[0].width(), 1920)

    def test_frames_that_arrive_before_the_last_was_drawn_are_dropped_not_queued(self):
        self.player._on_frame(video_frame(640, 360))
        for _ in range(5):
            self.player._on_frame(video_frame(640, 360))
        self.assertEqual(len(self.shown), 1)
        self.assertEqual(self.player.dropped, 5)
        self.player.frame_painted()
        self.player._on_frame(video_frame(640, 360))
        self.assertEqual(len(self.shown), 2)

    def test_the_newest_frame_is_shown_after_the_skipped_ones(self):
        self.player._on_frame(video_frame(640, 360))
        self.player.sink.setVideoFrame(video_frame(400, 300))       # the sink's current frame, which arrives and is dropped
        self.assertEqual(len(self.shown), 1)
        self.player.frame_painted()
        self.assertTrue(pump(2, lambda: len(self.shown) == 2), "the last frame was lost")
        self.assertEqual((self.shown[1].width(), self.shown[1].height()), (400, 300))

    def test_a_frame_that_never_gets_drawn_does_not_stall_the_picture(self):
        self.player._on_frame(video_frame(640, 360))
        self.player._last_delivery -= 1.0                       # long ago, and the view is hidden or busy
        self.player._on_frame(video_frame(640, 360))
        self.assertEqual(len(self.shown), 2)

    def test_an_invalid_frame_is_ignored(self):
        self.player._on_frame(QVideoFrame())
        self.assertEqual(self.shown, [])

    def test_a_frame_that_cannot_be_read_is_skipped_and_the_next_one_still_shows(self):
        class Unreadable:
            def isValid(self): return True
            def width(self): return 10
            def height(self): return 10
            def toImage(self): raise RuntimeError("unmappable")

        self.player._on_frame(Unreadable())
        self.assertEqual(self.shown, [])
        self.assertFalse(self.player._converting)
        self.player._on_frame(video_frame(64, 36))
        self.assertEqual(len(self.shown), 1)

    def test_the_view_says_when_it_has_drawn_the_picture(self):
        from video_converter.ui.player import VideoView
        view = VideoView()
        drawn = []
        view.framePainted.connect(lambda: drawn.append(1))
        view.resize(400, 300)
        view.grab()
        self.assertTrue(drawn)
        view.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ----- 3: a file never stays on "Loading…" -----------------------------------------------------------------------

class MediaTimeoutTests(unittest.TestCase):
    def test_the_time_allowed_grows_with_the_length_of_the_video(self):
        self.assertEqual(media.waveform_timeout(None), 60.0)
        self.assertEqual(media.waveform_timeout(600.0), 180.0)
        self.assertGreater(media.thumbnail_timeout(3600.0), media.thumbnail_timeout(10.0))
        self.assertEqual(media.PROBE_TIMEOUT, 30.0)

    def test_the_waveform_reading_has_a_limit(self):
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(media, "run_tracked", return_value=done) as run:
            media.extract_waveform("ffmpeg", Path("a.mp4"), 600.0)
        self.assertEqual(run.call_args.args[1], 180.0)
        with mock.patch.object(media, "run_tracked", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                media.extract_waveform("ffmpeg", Path("a.mp4"), 10.0)

    def test_reading_the_details_has_a_limit_and_says_so_plainly(self):
        with mock.patch.object(media, "run_tracked", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)) as run:
            with self.assertRaises(media.ReadTimeout) as caught:
                media.probe_file("ffmpeg", Path("a.mp4"))
        self.assertEqual(str(caught.exception), "Reading this file took too long")
        self.assertEqual(run.call_args.args[1], media.PROBE_TIMEOUT)

    def test_the_thumbnail_reading_has_a_limit_and_gives_up_quietly(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(media, "run_tracked", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)) as run:
            self.assertEqual(media.extract_thumbnails("ffmpeg", Path("a.mp4"), Path(folder), 60.0), [])
        self.assertEqual(run.call_count, 1, "it should stop after the first run that timed out")
        self.assertEqual(run.call_args.args[1], media.thumbnail_timeout(60.0))

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_the_real_details_are_read(self):
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "a.mp4"
            make_video(clip)
            info = media.probe_file(FFMPEG, clip, ProcessRegistry())
        self.assertEqual((info.width, info.height), (320, 180))
        self.assertTrue(info.has_audio)

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_the_first_picture_is_saved_upright_and_no_bigger_than_asked(self):
        with tempfile.TemporaryDirectory() as folder:
            clip, still = Path(folder) / "a.mp4", Path(folder) / "still.png"
            make_video(clip)
            self.assertTrue(media.extract_still(FFMPEG, clip, still, long_side=160))
            image = QImage(str(still)) if QApplication is not None else None
        if image is not None:
            self.assertEqual((image.width(), image.height()), (160, 90))


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class WindowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.first, cls.second = cls.tmp / "first.mp4", cls.tmp / "second.mp4"
        make_video(cls.first)
        make_video(cls.second)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        from video_converter.ui.main_window import MainWindow
        self.ini = self.tmp / f"settings-{self.id().split('.')[-1]}.ini"
        self.window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        self.window.resize(1000, 700)
        self.window.show()

    def tearDown(self):
        window = self.window
        tmpdir = window.tmpdir
        window.close()
        window.loader.shutdown(wait=True)
        self.assertFalse(tmpdir.exists(), "the window left its temp folder behind")
        window.deleteLater()
        self.window = None
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)     # free it now: every widget left alive slows each later theme change
        pump(0.05)

    def junk(self, *names):
        paths = []
        for name in names:
            path = self.tmp / name
            path.write_bytes(b"x")
            paths.append(path)
        return paths

    def loaded(self, *items):
        return pump(TIMEOUT, lambda: all(not i.loading for i in items))


class LoadingTests(WindowCase):
    def test_a_probe_that_times_out_ends_loading_with_a_readable_error(self):
        with mock.patch("video_converter.ui.main_window.probe_file", side_effect=media.ReadTimeout()):
            self.window.add_paths([self.first])
            item = self.window.items[0]
            self.assertTrue(self.loaded(item), "the file is stuck on Loading…")
        self.assertEqual(item.error, "Reading this file took too long")
        self.assertIn("Reading this file took too long", item.list_item.text())
        self.assertNotIn("Loading", item.list_item.text())

    def test_any_other_failure_also_ends_loading(self):
        with mock.patch("video_converter.ui.main_window.probe_file", side_effect=RuntimeError("boom")):
            self.window.add_paths([self.first])
            item = self.window.items[0]
            self.assertTrue(self.loaded(item), "the file is stuck on Loading…")
        self.assertIn("boom", item.error)

    def test_a_waveform_that_takes_too_long_leaves_the_file_usable(self):
        with mock.patch("video_converter.ui.main_window.extract_waveform",
                        side_effect=subprocess.TimeoutExpired("ffmpeg", 300)):
            self.window.add_paths([self.first])
            item = self.window.items[0]
            self.assertTrue(self.loaded(item), "the file is stuck on Loading…")
        self.assertEqual(item.error, "")
        self.assertTrue(item.info.has_video)
        self.assertEqual(item.waveform, [])
        self.assertTrue(item.thumbs)

    def test_a_seek_asked_for_before_the_player_has_the_file_is_not_lost(self):
        self.window.add_paths([self.first])
        item = self.window.items[0]
        self.assertTrue(self.loaded(item))
        self.window.player.seek(1.0)            # the file is handed to the player only now that its details are known
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.window.player.position - 1.0) < 0.05), "the seek was lost")
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.window.player.media.position() - 1000) < 60))

    def test_thumbnails_that_fail_leave_the_file_usable(self):
        with mock.patch("video_converter.ui.main_window.extract_thumbnails", side_effect=OSError("disk")):
            self.window.add_paths([self.first])
            item = self.window.items[0]
            self.assertTrue(self.loaded(item))
        self.assertEqual(item.error, "")
        self.assertTrue(item.waveform)


# ----- an export still being written is not a file to add

class PartialExportTests(unittest.TestCase):
    def test_a_part_file_is_not_a_media_file_but_a_normal_one_is(self):
        from video_converter.core import is_media_file
        self.assertFalse(is_media_file(Path("clip (edited).part.mp4")))
        self.assertFalse(is_media_file(Path("clip.part2.MP3")))
        self.assertTrue(is_media_file(Path("clip (edited).mp4")))
        self.assertTrue(is_media_file(Path("my.partner.mp4")))
        self.assertTrue(is_media_file(Path("departure.mp4")))


# ----- 4: closing stops the work ------------------------------------------------------------------------------------

class ProcessTests(unittest.TestCase):
    def test_a_run_that_takes_too_long_is_stopped(self):
        registry = ProcessRegistry()
        seen = []
        real_add = registry.add
        registry.add = lambda proc: seen.append(proc) or real_add(proc)
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_tracked(SLEEP_FOR_A_MINUTE, 0.5, registry)
        self.assertLess(time.monotonic() - started, 20)
        self.assertIsNotNone(seen[0].poll(), "the program was left running")
        self.assertEqual(registry.running(), [])

    def test_killing_the_registry_ends_the_runs_in_progress(self):
        registry = ProcessRegistry()
        thread, result = in_thread(lambda: self.cancelled(registry))
        self.assertTrue(pump(TIMEOUT, lambda: registry.running()), "the program never started")
        proc = registry.running()[0]
        registry.kill_all()
        self.assertEqual(finish(thread, result), "cancelled")
        self.assertIsNotNone(proc.poll())

    @unittest.skipUnless(sys.platform == "win32", "job objects are a Windows feature")
    def test_the_programs_the_loader_starts_are_in_a_job_so_their_children_die_with_them(self):
        import ctypes

        def alive(pid):
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == 259                                               # STILL_ACTIVE

        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "grandchild.pid"
            code = ("import subprocess, sys, time; from pathlib import Path\n"
                    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                    "Path(sys.argv[1]).write_text(str(g.pid)); time.sleep(120)\n")
            registry = ProcessRegistry()
            def run():
                try:
                    run_tracked([sys.executable, "-c", code, str(pid_file)], 120, registry)
                except ConversionCancelled:
                    return "cancelled"

            thread, result = in_thread(run)
            self.assertTrue(pump(TIMEOUT, lambda: pid_file.is_file() and pid_file.read_text()), "never started")
            grandchild = int(pid_file.read_text())
            self.assertTrue(alive(grandchild))
            registry.kill_all()
            finish(thread, result)
            self.assertTrue(pump(10, lambda: not alive(grandchild)), "the program's own child was left running")

    @staticmethod
    def cancelled(registry):
        try:
            run_tracked(SLEEP_FOR_A_MINUTE, 120, registry)
        except ConversionCancelled:
            return "cancelled"
        return "finished"

    def test_nothing_new_starts_after_the_registry_closed(self):
        registry = ProcessRegistry()
        registry.kill_all()
        with self.assertRaises(ConversionCancelled):
            run_tracked([sys.executable, "-c", "print(1)"], 30, registry)

    def test_the_pool_runs_jobs_in_order_and_can_be_shut_down(self):
        pool = DaemonPool(1, "test")
        results = [pool.submit(lambda n=n: n * 2) for n in range(4)]
        self.assertEqual([r.result(TIMEOUT) for r in results], [0, 2, 4, 6])
        pool.shutdown(wait=True)
        with self.assertRaises(RuntimeError):
            pool.submit(print)

    def test_shutting_down_cancels_the_jobs_that_have_not_started(self):
        gate = threading.Event()
        pool = DaemonPool(1, "test")
        busy = pool.submit(gate.wait, TIMEOUT)
        queued = pool.submit(lambda: "ran")
        time.sleep(0.1)
        pool.shutdown(wait=False, cancel_futures=True)
        gate.set()
        busy.result(TIMEOUT)
        self.assertTrue(queued.cancelled())

    def test_a_job_still_running_does_not_keep_python_from_exiting(self):
        code = ("import time; from video_converter.procs import DaemonPool;"
                "pool = DaemonPool(1, 'x'); pool.submit(time.sleep, 120); time.sleep(0.3)")
        started = time.monotonic()
        subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], timeout=60)
        self.assertLess(time.monotonic() - started, 30)


class ClosingTests(WindowCase):
    def test_closing_ends_the_ffmpeg_runs_that_are_reading_files(self):
        started = threading.Event()
        procs = []

        def slow_probe(ffmpeg, source, registry=None, timeout=30.0):
            real_add = registry.add
            registry.add = lambda proc: procs.append(proc) or real_add(proc)
            started.set()
            return run_tracked(SLEEP_FOR_A_MINUTE, 120, registry)

        with mock.patch("video_converter.ui.main_window.probe_file", side_effect=slow_probe):
            self.window.add_paths([self.first])
            self.assertTrue(started.wait(TIMEOUT))
            self.assertTrue(pump(TIMEOUT, lambda: procs), "the program never started")
            began = time.monotonic()
            self.window.close()
            thread, result = in_thread(lambda: self.window.loader.shutdown(wait=True) or "stopped")
            self.assertEqual(finish(thread, result, 20), "stopped", "the loader is still waiting for ffmpeg")
        self.assertLess(time.monotonic() - began, 20)
        self.assertIsNotNone(procs[0].poll(), "ffmpeg was left running after the window closed")

    def test_files_removed_before_their_turn_are_never_read(self):
        gate, read = threading.Event(), []

        def probe(ffmpeg, source, registry=None, timeout=30.0):
            read.append(Path(source).name)
            gate.wait(TIMEOUT)
            return MediaInfo(duration=None)

        a, b, c, d = self.junk("a.mp4", "b.mp4", "c.mp4", "d.mp4")
        with mock.patch("video_converter.ui.main_window.probe_file", side_effect=probe):
            self.window.add_paths([a, b, c, d])
            self.assertTrue(pump(TIMEOUT, lambda: len(read) == 2), "the two loaders never started")
            for name in ("d.mp4", "c.mp4"):
                self.window.media_list.setCurrentRow([i.path.name for i in self.window.items].index(name))
                self.window.remove_selected()
            gate.set()
            self.window.loader.shutdown(wait=True)
        self.assertEqual(sorted(read), ["a.mp4", "b.mp4"])

    def test_a_file_removed_while_it_is_being_read_leaves_no_thumbnails(self):
        gate, folders = threading.Event(), []

        def thumbnails(ffmpeg, source, out_dir, duration, **kwargs):
            folders.append(out_dir)
            gate.wait(TIMEOUT)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "thumb_000.jpg").write_bytes(b"x")
            return []

        with mock.patch("video_converter.ui.main_window.extract_thumbnails", side_effect=thumbnails):
            self.window.add_paths([self.first])
            self.assertTrue(pump(TIMEOUT, lambda: folders))
            self.window.remove_selected()
            gate.set()
            self.window.loader.shutdown(wait=True)
        pump(0.2)
        self.assertFalse(folders[0].exists(), "the removed file's thumbnails were left behind")


# ----- 6: before/after pile-up --------------------------------------------------------------------------------------

class BeforeAfterTests(WindowCase):
    def test_only_one_picture_is_rendered_at_a_time_and_the_last_request_wins(self):
        lock, running, most, positions = threading.Lock(), [0], [0], []

        def slow_render(ffmpeg, source, target, at, size, edit, info):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
                positions.append(at)
            time.sleep(0.25)
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(0xFF00FF00)
            image.save(str(target))
            with lock:
                running[0] -= 1

        self.window.add_paths([self.first])
        item = self.window.items[0]
        self.assertTrue(self.loaded(item))
        with mock.patch("video_converter.ui.main_window.render_frame", side_effect=slow_render):
            self.window.compare_button.setChecked(True)
            for _ in range(6):
                self.window._render_after()
                pump(0.03)
            self.assertTrue(pump(TIMEOUT, lambda: self.window.view.after_image is not None))
            pump(0.8)
        self.assertEqual(most[0], 1, "several before/after renders ran at once")
        self.assertLessEqual(len(positions), 4, "older requests should not each be rendered")
        self.assertFalse(self.window._after_busy)
        self.assertIsNotNone(self.window.view.after_image)

    def test_a_render_that_fails_does_not_block_the_next_one(self):
        self.window.add_paths([self.first])
        self.assertTrue(self.loaded(self.window.items[0]))
        with mock.patch("video_converter.ui.main_window.render_frame", side_effect=ValueError("odd")):
            self.window.compare_button.setChecked(True)
            self.window._render_after()
            self.assertTrue(pump(TIMEOUT, lambda: not self.window._after_busy and self.window._after_token == 1))
        self.assertFalse(self.window._after_busy)


# ----- 7: temp folders ----------------------------------------------------------------------------------------------

class TempFolderTests(WindowCase):
    def test_removing_a_file_deletes_its_thumbnails(self):
        self.window.add_paths([self.first])
        item = self.window.items[0]
        self.assertTrue(self.loaded(item))
        folder = self.window._thumb_folder(item)
        self.assertTrue(folder.is_dir() and any(folder.iterdir()), "no thumbnails were made")
        self.window.remove_selected()
        self.assertFalse(folder.exists())

    def test_each_item_has_a_folder_of_its_own_that_is_never_reused(self):
        self.window.add_paths([self.first, self.second])
        one, two = self.window.items
        self.assertNotEqual(self.window._thumb_folder(one), self.window._thumb_folder(two))
        self.assertTrue(self.loaded(one, two))
        old = self.window._thumb_folder(one)
        self.window.media_list.setCurrentRow(0)
        self.window.remove_selected()
        self.window.add_paths([self.first])
        again = self.window.items[-1]
        self.assertNotEqual(self.window._thumb_folder(again), old)


class SweepTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.now = time.time()

    def folder(self, name, age_hours, owner=None):
        path = self.root / name
        path.mkdir()
        (path / "x.jpg").write_bytes(b"x")
        if owner is not None:
            (path / tempclean.OWNER_FILE).write_text(str(owner))
        old = self.now - age_hours * 3600
        for target in (path / "x.jpg", path / tempclean.OWNER_FILE, path):
            if target.exists():
                os.utime(target, (old, old))
        return path

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_old_leftovers_go_and_recent_or_unrelated_folders_stay(self):
        old_editor = self.folder("videoconverter-abc", 30)
        old_export = self.folder("fve-export-xyz", 48)
        recent = self.folder("videoconverter-new", 2)
        recent_export = self.folder("fve-frame-new", 0.1)
        other = self.folder("holiday-photos", 500)
        removed = tempclean.sweep_stale(self.root, now=self.now)
        self.assertCountEqual(removed, [old_editor, old_export])
        self.assertFalse(old_editor.exists() or old_export.exists())
        for kept in (recent, recent_export, other):
            self.assertTrue(kept.exists(), kept.name)

    def test_a_folder_a_running_copy_uses_is_kept_however_old(self):
        mine = self.folder("videoconverter-live", 100, owner=os.getpid())
        gone = self.folder("videoconverter-crashed", 100, owner=self.dead_pid())
        tempclean.sweep_stale(self.root, now=self.now)
        self.assertTrue(mine.exists())
        self.assertFalse(gone.exists())

    def test_a_folder_with_recent_files_is_kept(self):
        busy = self.folder("fve-export-busy", 100)
        (busy / "fresh.mp4").write_bytes(b"x")          # written just now
        tempclean.sweep_stale(self.root, now=time.time())
        self.assertTrue(busy.exists())

    def test_the_folder_it_is_told_to_keep_is_kept(self):
        mine = self.folder("videoconverter-mine", 100)
        tempclean.sweep_stale(self.root, now=self.now, keep=[mine])
        self.assertTrue(mine.exists())

    def test_it_uses_the_temp_folder_by_default_and_ignores_errors(self):
        old = self.folder("videoconverter-default", 30)
        with mock.patch.object(tempfile, "gettempdir", return_value=str(self.root)):
            tempclean.sweep_stale(now=self.now)
        self.assertFalse(old.exists())
        self.assertEqual(tempclean.sweep_stale(self.root / "missing"), [])
        with mock.patch.object(tempclean.shutil, "rmtree", side_effect=OSError("locked")):
            self.folder("fve-locked", 30)
            tempclean.sweep_stale(self.root, now=self.now)          # must not raise

    @unittest.skipIf(QApplication is None, "PySide6 is not installed")
    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_a_window_marks_its_folder_as_in_use(self):
        from video_converter.ui.main_window import MainWindow
        window = MainWindow(FFMPEG, QSettings(str(self.root / "s.ini"), QSettings.Format.IniFormat))
        try:
            self.assertEqual((window.tmpdir / tempclean.OWNER_FILE).read_text().strip(), str(os.getpid()))
        finally:
            window.close()
            window.loader.shutdown(wait=True)
            window.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ----- 8: one running copy ------------------------------------------------------------------------------------------

@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui import single_instance
        self.module = single_instance
        self.name = f"FVE-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._locks = tempfile.TemporaryDirectory()
        self.addCleanup(self._locks.cleanup)
        self.lock_dir = Path(self._locks.name)
        self.received = []
        self.instances = []

    def tearDown(self):
        for instance in self.instances:
            instance.close()
            instance.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(0.05)

    def first_copy(self):
        instance = self.module.SingleInstance(self.name, self.lock_dir)
        self.instances.append(instance)
        instance.pathsReceived.connect(self.received.append)
        self.assertTrue(instance.listen(), instance.server.errorString())
        return instance

    def test_the_second_start_hands_its_files_to_the_first_and_exits(self):
        self.first_copy()
        second = second_start(self.name, self.lock_dir, "one.mp4", "two.mp4")
        self.assertEqual(outcome(second), "exit", "the second copy did not exit")
        self.assertTrue(pump(TIMEOUT, lambda: self.received))
        self.assertEqual([Path(p).name for p in self.received[0]], ["one.mp4", "two.mp4"])
        self.assertTrue(all(Path(p).is_absolute() for p in self.received[0]))

    def test_a_start_with_no_files_still_reaches_the_first_copy(self):
        self.first_copy()
        self.assertEqual(outcome(second_start(self.name, self.lock_dir)), "exit")
        self.assertTrue(pump(TIMEOUT, lambda: self.received))
        self.assertEqual(self.received[0], [])

    def test_two_starts_at_the_same_moment_make_one_running_copy(self):
        first = second_start(self.name, self.lock_dir)
        second = second_start(self.name, self.lock_dir)
        self.assertEqual(sorted([outcome(first), outcome(second)]), ["exit", "listening"])

    def test_the_first_start_becomes_the_running_copy(self):
        instance = self.module.claim([], self.name, self.lock_dir)
        self.instances.append(instance)
        self.assertTrue(instance.listening)
        second = self.module.SingleInstance(self.name, self.lock_dir)
        self.instances.append(second)
        self.assertFalse(second.listen(), "two copies listened on the same name")

    def test_the_name_is_free_again_once_the_first_copy_closed(self):
        first = self.first_copy()
        first.close()
        again = self.module.claim([], self.name, self.lock_dir)
        self.instances.append(again)
        self.assertIsNotNone(again)
        self.assertTrue(again.listening)

    def test_a_copy_that_does_not_answer_does_not_stop_a_new_one_from_starting(self):
        deaf = self.first_copy()
        deaf.server.newConnection.disconnect(deaf._accept)          # holds the lock, never reads what it is sent
        self.assertEqual(outcome(second_start(self.name, self.lock_dir, "x.mp4", fast=True)), "alone")
        self.assertEqual(self.received, [])

    def test_messages_are_read_carefully(self):
        decode = self.module.decode_paths
        self.assertEqual(decode(b'{"paths": ["a.mp4", "b.mp4"]}'), ["a.mp4", "b.mp4"])
        for bad in (b"", b"not json", b"[1, 2]", b'{"paths": "a.mp4"}', b"\xff\xfe"):
            self.assertEqual(decode(bad), [], bad)
        self.assertEqual(decode(json.dumps({"paths": ["a", 3, "", None, "b"]}).encode()), ["a", "b"])

    def test_the_developer_switch_allows_several_copies(self):
        self.first_copy()
        with mock.patch.dict(os.environ, {self.module.DISABLE_VARIABLE: "1"}):
            instance = self.module.claim([], self.name, self.lock_dir)
        self.instances.append(instance)
        self.assertFalse(instance.listening)

    def test_the_name_is_different_for_each_user(self):
        with mock.patch.object(self.module.getpass, "getuser", return_value="Ann Lee"):
            ann = self.module.instance_name()
        with mock.patch.object(self.module.getpass, "getuser", return_value="bob"):
            bob = self.module.instance_name()
        self.assertNotEqual(ann, bob)
        self.assertNotIn(" ", ann)


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class WindowInstanceTests(WindowCase):
    def setUp(self):
        super().setUp()
        from video_converter.ui import single_instance
        self.module = single_instance
        self.name = f"FVE-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._locks = tempfile.TemporaryDirectory()
        self.addCleanup(self._locks.cleanup)
        self.lock_dir = Path(self._locks.name)
        self.instance = self.module.SingleInstance(self.name, self.lock_dir)
        self.assertTrue(self.instance.listen())
        self.window.watch_for_other_copies(self.instance)
        self.received_paths = []
        self.instance.pathsReceived.connect(self.received_paths.append)

    def tearDown(self):
        super().tearDown()
        self.instance.close()
        self.instance.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_files_from_a_second_start_are_added_to_the_open_window(self):
        self.assertEqual(outcome(second_start(self.name, self.lock_dir, self.first, self.second)), "exit")
        self.assertTrue(pump(TIMEOUT, lambda: len(self.window.items) == 2))
        self.assertEqual([item.path.name for item in self.window.items], ["first.mp4", "second.mp4"])
        # A file it already has is not added again.
        self.assertEqual(outcome(second_start(self.name, self.lock_dir, self.first)), "exit")
        pump(0.5)
        self.assertEqual(len(self.window.items), 2)

    def test_a_second_start_without_files_just_brings_the_window_forward(self):
        with mock.patch.object(type(self.window), "raise_") as raised:
            self.assertEqual(outcome(second_start(self.name, self.lock_dir)), "exit")
            self.assertTrue(pump(TIMEOUT, lambda: raised.called))
        self.assertEqual(self.window.items, [])

    def test_closing_the_window_frees_the_name_so_a_restart_can_start(self):
        self.window.close()
        again = self.module.claim([], self.name, self.lock_dir)
        self.assertTrue(again.listening, "the new copy would hand its files to a window that is closing")
        again.close()
        self.assertEqual(outcome(second_start(self.name, self.lock_dir)), "listening")

    def test_a_restart_frees_the_name_before_the_new_copy_starts(self):
        """The language restart starts the new copy first and closes this one after: the new copy must not find
        this window still listening, or it would hand its files to a window that is closing and exit."""
        from video_converter.ui import language
        started = []

        def start_new_copy(paths):
            started.append(second_start(self.name, self.lock_dir, *paths))
            return True

        self.window.add_paths([self.first])
        with mock.patch.object(language, "restart", side_effect=start_new_copy):
            self.window.restart_app()
        self.assertTrue(self.window.closed)
        self.assertEqual(outcome(started[0]), "listening", "the new copy was sent to the closing window")
        self.assertEqual(self.received_paths, [])

    def test_a_restart_that_cannot_start_leaves_this_window_the_running_copy(self):
        from video_converter.ui import language
        with mock.patch.object(language, "restart", return_value=False):
            with mock.patch("video_converter.ui.main_window.QMessageBox.warning"):
                self.window.restart_app()
        self.assertFalse(self.window.closed)
        self.assertTrue(self.instance.listening)
        self.assertEqual(outcome(second_start(self.name, self.lock_dir)), "exit")


# ----- 9, 10, 11 and the graphics-card search ---------------------------------------------------------------------

class LimitsTests(WindowCase):
    def test_the_undo_history_is_limited(self):
        self.assertEqual(self.window.undo_stack.undoLimit(), 300)

    def test_scaled_thumbnails_are_remembered(self):
        timeline = self.window.timeline
        timeline.resize(800, 160)
        pixmaps = [QPixmap(96, 54) for _ in range(4)]
        for pixmap in pixmaps:
            pixmap.fill(0xFF445566)
        timeline.setDuration(10.0)
        timeline.setThumbnails(pixmaps)
        timeline.grab()
        cached = dict(timeline._thumb_cache)
        self.assertEqual(len(cached), 4)
        timeline.grab()
        self.assertEqual(len(timeline._thumb_cache), 4)
        for key, value in cached.items():
            self.assertIs(timeline._thumb_cache[key], value, "a thumbnail was scaled again")
        timeline.setThumbnails([])
        self.assertEqual(timeline._thumb_cache, {})

    def test_adding_files_looks_up_the_paths_of_the_ones_already_there_only_once(self):
        paths = self.junk(*[f"many-{n}.mp4" for n in range(8)])
        calls = []
        real = Path.resolve

        def counting(self, *args, **kwargs):
            calls.append(self)
            return real(self, *args, **kwargs)

        with mock.patch.object(self.window.loader, "submit"):         # no reading, only the list
            self.window.add_paths(paths)
            with mock.patch.object(Path, "resolve", counting):
                self.window.add_paths([self.junk("one-more.mp4")[0]])
                self.assertEqual(self.window.add_paths(paths), 0)
        # One new file once, and the 8 known files once each to see they are known: no look-up of the 9 items again.
        self.assertLessEqual(len(calls), 1 + len(paths))

    def test_the_search_for_graphics_cards_waits_for_the_first_file(self):
        from video_converter import gpu
        with mock.patch.object(gpu, "detect_encoders", return_value=()) as detect, \
                mock.patch.object(gpu, "gpu_name", return_value=None), \
                mock.patch.dict(os.environ, {"VIDEO_EDITOR_NO_GPU_SEARCH": "0"}), \
                mock.patch("video_converter.ui.main_window.GPU_SEARCH_DELAY_MS", 100):
            pump(0.4)
            self.assertFalse(self.window._gpu_started, "the search started with the window")
            detect.assert_not_called()
            self.window.add_paths([self.first])
            self.assertTrue(self.loaded(self.window.items[0]))
            self.assertFalse(self.window._gpu_started, "the search started the moment the file loaded")
            self.assertTrue(pump(TIMEOUT, lambda: self.window.gpu_encoders is not None))
            detect.assert_called_once()


class FileLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "locks" / "download.lock"

    def test_a_second_holder_waits_and_then_gives_up(self):
        with FileLock(self.path, timeout=5):
            started = time.monotonic()
            with self.assertRaises(LockTimeout):
                with FileLock(self.path, timeout=0.4, poll=0.05):
                    self.fail("two holders at once")
            self.assertGreaterEqual(time.monotonic() - started, 0.35)

    def test_it_is_free_again_after_release_and_a_waiter_can_be_cancelled(self):
        with FileLock(self.path, timeout=5):
            pass
        with FileLock(self.path, timeout=1):
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(LockTimeout):
                FileLock(self.path, cancel=cancel).acquire()

    def test_another_process_is_kept_out_too(self):
        code = ("import sys; from pathlib import Path; from video_converter.locks import FileLock, LockTimeout\n"
                "try:\n    FileLock(Path(sys.argv[1]), timeout=0.3, poll=0.05).acquire(); print('got')\n"
                "except LockTimeout:\n    print('blocked')\n")
        with FileLock(self.path, timeout=5):
            shown = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, text=True,
                                   cwd=Path(__file__).resolve().parents[1], timeout=60).stdout
        self.assertEqual(shown.strip(), "blocked")
        shown = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, text=True,
                               cwd=Path(__file__).resolve().parents[1], timeout=60).stdout
        self.assertEqual(shown.strip(), "got")


if __name__ == "__main__":
    unittest.main()
