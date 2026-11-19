"""Smooth previews (2.4): which videos get a small preview copy, making it, and playing it in the
editor."""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "VIDEO_EDITOR_NO_GPU_SEARCH",
    "1")  # a window never searches for graphics cards (see tests/__init__.py)
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    from PySide6.QtCore import QEvent, QSettings
    from PySide6.QtWidgets import QApplication, QMenu
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter import proxy  # noqa: E402
from video_converter.core import (  # noqa: E402
    ConversionCancelled, ConversionError, MediaInfo, find_ffmpeg, parse_media_info, probe_media,
)
from video_converter.core import runner  # noqa: E402
from video_converter.proxy import ProxyMaker, needs_proxy, proxy_command, proxy_size  # noqa: E402

FFMPEG = find_ffmpeg()
TIMEOUT = 30


def setUpModule():
    global app
    app = QApplication.instance() or QApplication([]) if QApplication is not None else None


def pump(seconds, until=lambda: False):
    """Process Qt events until `until()` is true or time runs out. Returns until()."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        if QApplication is not None:
            QApplication.processEvents()
        time.sleep(0.01)
    return until()


def make_video(path, width=320, height=180, seconds=4, rotation=None, audio=True):
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"testsrc2=duration={seconds}:size={width}x{height}:rate=15"
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    if rotation is not None:
        tmp = path.with_name("tmp-" + path.name)
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-display_rotation",
                str(rotation), "-i",
                str(path), "-c", "copy",
                str(tmp)
            ], check=True)
        tmp.replace(path)


def info(width, height, kbps=None, duration=60.0, audio=True):
    return MediaInfo(
        duration=duration, width=width, height=height, has_audio=audio, fps=30.0, bitrate=kbps)


class DecisionTests(unittest.TestCase):

    def test_ordinary_videos_are_played_as_they_are(self):
        for size in ((1920, 1080), (1080, 1920), (1280, 720), (640, 360), (1080, 1080)):
            with self.subTest(size=size):
                self.assertFalse(needs_proxy(info(*size, kbps=8000)))

    def test_videos_bigger_than_1080p_get_a_copy(self):
        for size in ((2560, 1440), (3840, 2160), (2160, 3840), (4096, 2160), (7680, 4320), (2704,
                                                                                            1520)):
            with self.subTest(size=size):
                self.assertTrue(needs_proxy(info(*size, kbps=8000)))

    def test_a_very_high_bitrate_gets_a_copy_at_any_size(self):
        self.assertTrue(needs_proxy(info(1920, 1080, kbps=45000)))
        self.assertTrue(needs_proxy(info(1280, 720, kbps=20001)))
        self.assertFalse(needs_proxy(info(1920, 1080, kbps=20000)))  # "above" 20 Mbit/s
        self.assertFalse(needs_proxy(info(1920, 1080, kbps=None)))

    def test_things_that_are_not_playable_pictures_never_do(self):
        self.assertFalse(needs_proxy(None))
        self.assertFalse(
            needs_proxy(MediaInfo(duration=60.0, has_audio=True, bitrate=900000)))  # audio only
        self.assertFalse(needs_proxy(info(3840, 2160, duration=None)))
        self.assertFalse(needs_proxy(info(3840, 2160, duration=0.0)))

    def test_the_thresholds_can_be_lowered(self):
        with mock.patch.object(proxy, "BIG_PIXELS", 100 * 100):
            self.assertTrue(needs_proxy(info(320, 180)))
            self.assertFalse(needs_proxy(info(96, 96)))

    @staticmethod
    def fast(width, height, fps, max_fps=None, kbps=8000):
        return MediaInfo(
            duration=60.0, width=width, height=height, has_audio=True, fps=fps, max_fps=max_fps,
            bitrate=kbps)

    def test_a_fast_picture_of_720p_or_more_gets_a_copy(self):
        for size, fps in (((1920, 1080), 60.0), ((1728, 1080), 59.94), ((1280, 720), 60.0),
                          ((1080, 1920), 60.0), ((1920, 1080), 36.0), ((1920, 1080), 120.0)):
            with self.subTest(size=size, fps=fps):
                self.assertTrue(needs_proxy(self.fast(*size, fps)))

    def test_ordinary_rates_and_small_pictures_are_left_alone(self):
        for size, fps in (((1920, 1080), 30.0), ((1920, 1080), 29.97), ((1920, 1080), 35.0),
                          ((1280, 720), 25.0), ((640, 360), 120.0), ((854, 480), 60.0)):
            with self.subTest(size=size, fps=fps):
                self.assertFalse(needs_proxy(self.fast(*size, fps)))

    def test_a_variable_rate_recording_counts_by_the_highest_rate_it_declares(self):
        # The owner's game recordings: 1728x1080, an average of 58 fps, up to 120 declared.
        self.assertTrue(needs_proxy(self.fast(1728, 1080, 57.97, max_fps=120.0)))
        slow_average = self.fast(1728, 1080, 25.5, max_fps=120.0)
        self.assertTrue(slow_average.variable_frame_rate)
        self.assertTrue(needs_proxy(slow_average))
        # 25 fps that declares 50 (interlaced fields) is not variable, and not fast.
        self.assertFalse(needs_proxy(self.fast(1920, 1080, 25.0, max_fps=50.0)))

    def test_a_missing_rate_never_makes_a_copy(self):
        self.assertFalse(needs_proxy(self.fast(1920, 1080, None)))

    def test_the_bitrate_is_read_from_ffmpegs_description(self):
        text = (
            "Input #0, mov,mp4, from 'a.mp4':\n  Duration: 00:00:20.00, start: 0.000000, "
            "bitrate: 45123 kb/s\n"
            "  Stream #0:0[0x1](und): Video: h264 (High), yuv420p, 3840x2160, 44000 kb/s, 30 "
            "fps, 30 tbr\n")
        self.assertEqual(parse_media_info(text).bitrate, 45123)
        self.assertIsNone(parse_media_info("Duration: N/A, bitrate: N/A\n").bitrate)


class SizeAndCommandTests(unittest.TestCase):

    def test_the_copy_is_960_pixels_on_its_long_side_with_the_same_shape(self):
        self.assertEqual(proxy_size(3840, 2160), (960, 540))
        self.assertEqual(proxy_size(2160, 3840), (540, 960))
        self.assertEqual(proxy_size(2560, 1440), (960, 540))
        self.assertEqual(proxy_size(4096, 2160), (960, 506))

    def test_it_is_never_made_bigger_and_the_sides_are_even(self):
        self.assertEqual(proxy_size(640, 360), (640, 360))
        for width, height in ((3001, 1999), (1921, 1081), (4000, 3001)):
            w, h = proxy_size(width, height)
            self.assertEqual((w % 2, h % 2), (0, 0))
            self.assertLessEqual(max(w, h), 960)

    def test_the_command_is_small_fast_and_seekable(self):
        cmd = proxy_command("ffmpeg", Path("big.mp4"), Path("out.mp4"), info(3840, 2160))
        text = " ".join(cmd)
        self.assertIn("scale=960:540", text)
        self.assertEqual(cmd[cmd.index("-preset") + 1], "ultrafast")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertIn("expr:gte(t,n_forced*1)", text)  # a keyframe every second
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertIn("0:a:0?", cmd)  # no audio is fine
        self.assertEqual(cmd[cmd.index("-i") + 1], "big.mp4")
        self.assertEqual(cmd[-1], "out.mp4")
        self.assertNotIn("fps=", text, "a 30 fps video needs no rate change")

    def test_a_fast_video_is_copied_at_30_frames_per_second(self):
        for fps, max_fps in ((60.0, None), (57.97, 120.0), (120.0, None)):
            with self.subTest(fps=fps):
                cmd = proxy_command(
                    "ffmpeg", Path("game.mp4"), Path("out.mp4"),
                    MediaInfo(60.0, 1728, 1080, True, fps=fps, max_fps=max_fps))
                graph = cmd[cmd.index("-vf") + 1]
                self.assertTrue(
                    graph.startswith("fps=30,scale="),
                    graph)  # before the scaler, which sees fewer frames

    def test_a_slow_video_keeps_its_own_rate(self):
        cmd = proxy_command(
            "ffmpeg", Path("a.mp4"), Path("out.mp4"), MediaInfo(60.0, 3840, 2160, True, fps=24.0))
        self.assertNotIn("fps=", cmd[cmd.index("-vf") + 1])


def slowly(*args, **kwargs):
    """The preview command with a slow x264 preset, so a copy takes long enough to be cancelled
    midway."""
    return [
        ("veryslow" if part == "ultrafast" else part) for part in proxy_command(*args, **kwargs)
    ]


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class ProxyMakerTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.source = cls.tmp / "big.mp4"
        make_video(cls.source, 320, 180, 4)
        cls.info = probe_media(FFMPEG, cls.source)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.folder = self.tmp / f"preview-{self.id().split('.')[-1]}"
        self.progress, self.done = [], []
        self.finished = threading.Event()
        self.maker = ProxyMaker(
            FFMPEG, self.folder, lambda k, f: self.progress.append((k, f)), self.report)
        self.addCleanup(self.maker.shutdown)
        patcher = mock.patch.object(proxy, "PROXY_LONG_SIDE", 160)
        patcher.start()
        self.addCleanup(patcher.stop)

    def report(self, key, path, error):
        self.done.append((key, path, error))
        self.finished.set()

    def test_a_copy_is_small_playable_and_as_long_as_the_original(self):
        self.assertTrue(self.maker.request("a", self.source, self.info))
        self.assertTrue(self.finished.wait(TIMEOUT), "no copy was made")
        key, path, error = self.done[0]
        self.assertEqual((key, error), ("a", ""))
        copy = probe_media(FFMPEG, path)
        self.assertEqual((copy.width, copy.height), (160, 90))
        self.assertAlmostEqual(copy.duration, self.info.duration, delta=0.3)
        self.assertTrue(copy.has_audio)
        self.assertEqual(self.progress[-1], ("a", 1.0))
        self.assertEqual(
            list(self.folder.glob("*.part.mp4")), [], "the half-made file should be renamed")

    def test_a_60_fps_video_is_copied_at_30_frames_per_second(self):
        fast = self.tmp / "fast.mp4"
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=duration=2:size=320x180:rate=60", "-c:v", "libx264", "-preset",
                "ultrafast", "-pix_fmt", "yuv420p",
                str(fast)
            ], check=True)
        fast_info = probe_media(FFMPEG, fast)
        self.assertAlmostEqual(fast_info.fps, 60.0, delta=0.5)
        self.maker.request("f", fast, fast_info)
        self.assertTrue(self.finished.wait(TIMEOUT), "no copy was made")
        self.assertEqual(self.done[0][2], "")
        copy = probe_media(FFMPEG, self.done[0][1])
        self.assertAlmostEqual(copy.fps, 30.0, delta=0.5)
        self.assertAlmostEqual(copy.duration, 2.0, delta=0.3)

    def test_there_is_a_keyframe_every_second(self):
        self.maker.request("a", self.source, self.info)
        self.assertTrue(self.finished.wait(TIMEOUT))
        shown = subprocess.run(
            [
                FFMPEG, "-hide_banner", "-i",
                str(self.done[0][1]), "-vf", "select=key,showinfo", "-f", "null", "-"
            ], capture_output=True, text=True).stderr
        times = [float(t) for t in re.findall(r"pts_time:([\d.]+)", shown)]
        self.assertGreaterEqual(len(times), 3)
        for before, after in zip(times, times[1:]):
            self.assertLessEqual(after - before, 1.1)

    def test_a_phone_video_comes_out_upright_with_no_rotation_left(self):
        phone = self.tmp / "phone.mp4"
        make_video(phone, 320, 180, 2, rotation=-90)
        phone_info = probe_media(FFMPEG, phone)
        self.assertEqual((phone_info.width, phone_info.height), (180, 320))
        self.maker.request("p", phone, phone_info)
        self.assertTrue(self.finished.wait(TIMEOUT))
        copy = probe_media(FFMPEG, self.done[0][1])
        self.assertEqual((copy.width, copy.height, copy.display_rotation), (90, 160, 0))

    def test_asking_twice_makes_one_copy(self):
        self.assertTrue(self.maker.request("a", self.source, self.info))
        self.assertFalse(self.maker.request("a", self.source, self.info))
        self.assertTrue(self.finished.wait(TIMEOUT))
        time.sleep(0.2)
        self.assertEqual(len(self.done), 1)

    def test_a_file_that_cannot_be_read_reports_why(self):
        bad = self.tmp / "bad.mp4"
        bad.write_bytes(b"this is not a video")
        self.maker.request("bad", bad, self.info)
        self.assertTrue(self.finished.wait(TIMEOUT))
        key, path, error = self.done[0]
        self.assertIsNone(path)
        self.assertTrue(error)
        self.assertEqual(list(self.folder.glob("*")), [])

    def test_cancelling_a_copy_that_is_waiting_means_it_is_never_made(self):
        release = threading.Event()
        real = proxy.run_ffmpeg

        def slow(cmd, output, duration, on_progress, cancel_event, cwd=None):
            release.wait(TIMEOUT)
            return real(cmd, output, duration, on_progress, cancel_event, cwd)

        with mock.patch.object(proxy, "run_ffmpeg", side_effect=slow):
            self.maker.request("first", self.source, self.info)
            self.maker.request("second", self.source, self.info)
            self.assertEqual(self.maker.cancel_waiting(keep="first"), ["second"])
            self.assertFalse(self.maker.active("second"))
            release.set()
            self.assertTrue(self.finished.wait(TIMEOUT))
            time.sleep(0.3)
        self.assertEqual([key for key, _, _ in self.done], ["first"])
        self.assertEqual(len(list(self.folder.glob("*.mp4"))), 1)

    def test_cancelling_a_running_copy_stops_ffmpeg_and_leaves_no_file(self):
        big = self.tmp / "long.mp4"
        make_video(big, 1280, 720, 40, audio=False)
        long_info = probe_media(FFMPEG, big)
        with mock.patch.object(proxy, "PROXY_LONG_SIDE",
                               1280), mock.patch.object(proxy, "proxy_command", slowly):
            self.maker.request("long", big, long_info)
            self.assertTrue(pump(TIMEOUT, lambda: self.progress), "the copy never started")
            self.maker.cancel("long")
            self.maker.shutdown()
        self.assertEqual(self.done, [], "a cancelled copy should report nothing")
        self.assertLess(self.progress[-1][1], 1.0)
        # Windows won't delete a file ffmpeg still has open, so this also shows ffmpeg is gone.
        shutil.rmtree(self.folder)

    def test_shutting_down_stops_everything_and_frees_the_folder(self):
        big = self.tmp / "long2.mp4"
        make_video(big, 1280, 720, 40, audio=False)
        long_info = probe_media(FFMPEG, big)
        with mock.patch.object(proxy, "PROXY_LONG_SIDE",
                               1280), mock.patch.object(proxy, "proxy_command", slowly):
            self.maker.request("one", big, long_info)
            self.maker.request("two", big, long_info)
            self.assertTrue(pump(TIMEOUT, lambda: self.progress))
            self.maker.shutdown()
        self.assertEqual(self.done, [])
        self.assertFalse(
            self.maker.request("three", self.source, self.info), "no new work after shutting down")
        shutil.rmtree(self.folder)

    def test_a_cancel_raised_inside_ffmpeg_reports_nothing(self):
        with mock.patch.object(proxy, "run_ffmpeg", side_effect=ConversionCancelled()):
            self.maker.request("a", self.source, self.info)
            self.maker.shutdown()
        self.assertEqual(self.done, [])


def paced(*args, **kwargs):
    """The preview command reading its input no faster than it plays (-re), so that a copy of a
    short file takes as long as the file does and can be paused part-way."""
    cmd = proxy_command(*args, **kwargs)
    cmd.insert(cmd.index("-i"), "-re")
    return cmd


class FakeRuns:
    """Stands in for run_ffmpeg. A run "works" until the test releases it, stands still while it is
    paused and raises ConversionCancelled when it is cancelled. Runs are told apart by the name of
    the source file (without its extension), which is the key the tests use."""

    def __init__(self, sample=None):
        self.sample = sample  # a real video to write as the finished copy
        self.guard = threading.Lock()
        self.events = []  # (what, key), in order
        self.working = set()  # the keys whose run is going on right now
        self.gates = {}

    def gate(self, key):
        with self.guard:
            return self.gates.setdefault(key, threading.Event())

    def release(self, key):
        self.gate(key).set()

    def note(self, what, key, working):
        with self.guard:
            self.events.append((what, key))
            (self.working.add if working else self.working.discard)(key)

    def count(self, what, key):
        with self.guard:
            return self.events.count((what, key))

    def __call__(self, cmd, output, duration, on_progress, cancel_event, cwd=None):
        key = Path(cmd[cmd.index("-i") + 1]).stem
        gate = self.gate(key)
        self.note("start", key, True)
        on_progress(0.4)
        paused = False
        while not cancel_event.is_set():
            if cancel_event.paused != paused:
                paused = cancel_event.paused
                self.note("pause" if paused else "resume", key, not paused)
            if gate.is_set() and not paused:
                output.write_bytes(self.sample.read_bytes() if self.sample else b"copy")
                self.note("finish", key, False)
                on_progress(1.0)
                return output
            time.sleep(0.005)
        self.note("cancel", key, False)
        raise ConversionCancelled()

    def working_is(self, *keys):
        with self.guard:
            return self.working == set(keys)


def recording_processes(test):
    """Remember every ffmpeg the run_ffmpeg calls of this test start, and return the list."""
    started = []
    real = runner.start_process

    def start(*args, **kwargs):
        proc = real(*args, **kwargs)
        started.append(proc)
        return proc

    patcher = mock.patch.object(runner, "start_process", side_effect=start)
    patcher.start()
    test.addCleanup(patcher.stop)
    return started


def rmtree_when_free(path, seconds=10):
    """Remove a folder, trying again while Windows still has a file in it open (it lets go a moment
    after the process that had it is gone)."""
    end = time.monotonic() + seconds
    while True:
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() > end:
                raise
            time.sleep(0.1)


class ProxyMakerSchedulingTests(unittest.TestCase):
    """Which copy gets the work, with stand-ins for ffmpeg: pausing, carrying on, the cap, the time
    limit."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.fake = FakeRuns()
        patcher = mock.patch.object(proxy, "run_ffmpeg", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.done, self.dropped = [], []
        self.maker = ProxyMaker(
            "ffmpeg", self.tmp / f"folder-{self.id().split('.')[-1]}", lambda key, fraction: None,
            lambda key, path, error: self.done.append(key), self.dropped.append)
        self.addCleanup(self.maker.shutdown)

    def ask(self, *keys):
        for key in keys:
            self.assertTrue(self.maker.request(key, self.tmp / f"{key}.mp4", info(1920, 1080)))

    def until(self, condition, what="it never happened"):
        self.assertTrue(pump(TIMEOUT, condition), what)

    def test_moving_the_focus_pauses_the_running_copy_and_runs_the_other(self):
        self.ask("a")
        self.until(lambda: self.fake.working_is("a"))
        self.ask("b")
        self.assertEqual(self.maker.state("b"), "waiting")
        self.assertTrue(self.fake.working_is("a"), "a waiting copy took the work")
        self.maker.focus("b")
        self.until(lambda: self.fake.working_is("b"))
        self.assertEqual((self.maker.state("a"), self.maker.state("b")), ("paused", "running"))
        self.assertTrue(self.maker.paused("a"))
        self.assertTrue(self.maker.active("a"), "a paused copy is still wanted")
        self.assertEqual(self.fake.count("start", "a"), 1)

    def test_a_paused_copy_carries_on_when_the_one_in_front_is_done(self):
        self.ask("a", "b")
        self.until(lambda: self.fake.working_is("a"))
        self.maker.focus("b")
        self.until(lambda: self.fake.working_is("b"))
        self.fake.release("b")
        self.until(lambda: self.fake.working_is("a"))
        self.assertEqual(self.done, ["b"])
        self.assertEqual(self.fake.count("resume", "a"), 1)
        self.assertEqual(self.fake.count("start", "a"), 1, "the paused copy started over")
        self.fake.release("a")
        self.until(lambda: self.done == ["b", "a"])

    def test_going_back_to_a_paused_copy_resumes_it_and_pauses_the_other(self):
        self.ask("a", "b")
        self.maker.focus("b")
        self.until(lambda: self.fake.working_is("b"))
        self.maker.focus("a")
        self.until(lambda: self.fake.working_is("a"))
        self.assertEqual(self.maker.state("b"), "paused")
        self.assertEqual(self.fake.count("start", "a"), 1)

    def test_without_a_focus_the_copies_are_made_in_the_order_they_were_asked_for(self):
        self.ask("a", "b", "c")
        self.until(lambda: self.fake.working_is("a"))
        self.fake.release("a")
        self.until(lambda: self.fake.working_is("b"))
        self.fake.release("b")
        self.until(lambda: self.fake.working_is("c"))
        self.assertEqual(self.fake.count("pause", "a"), 0)

    def test_a_parked_copy_stays_paused_whatever_the_focus(self):
        self.ask("a")
        self.until(lambda: self.fake.working_is("a"))
        self.maker.park("a")
        self.until(lambda: self.fake.working_is())
        self.maker.focus("a")
        self.maker.focus(None)
        pump(0.3)
        self.assertTrue(self.fake.working_is(), "a parked copy carried on")
        self.assertEqual(self.maker.state("a"), "paused")
        self.ask("b")
        self.until(lambda: self.fake.working_is("b"))
        self.maker.park("a", False)
        pump(0.2)
        self.assertTrue(self.fake.working_is("b"), "the copy running was interrupted")
        self.fake.release("b")
        self.until(lambda: self.fake.working_is("a"))

    def test_a_copy_that_is_in_front_goes_first_when_it_is_no_longer_parked(self):
        self.ask("a", "b")
        self.until(lambda: self.fake.working_is("a"))
        self.maker.park("a")
        self.until(lambda: self.fake.working_is("b"))
        self.maker.focus("a")
        pump(0.2)
        self.assertTrue(self.fake.working_is("b"), "a parked copy went first")
        self.maker.park("a", False)
        self.until(lambda: self.fake.working_is("a"))
        self.assertEqual(self.maker.state("b"), "paused")
        self.fake.release("a")
        self.until(lambda: self.fake.working_is("b"))

    def test_holding_keeps_the_other_paused_copies_paused(self):
        self.ask("a", "b")
        self.until(lambda: self.fake.working_is("a"))
        self.maker.focus("b")
        self.until(lambda: self.fake.working_is("b"))
        self.maker.focus("somebody else", hold=True)
        self.until(lambda: self.fake.working_is())
        pump(0.3)
        self.assertTrue(self.fake.working_is(), "a paused copy carried on during the hold")
        self.assertEqual((self.maker.state("a"), self.maker.state("b")), ("paused", "paused"))
        self.maker.focus(None, hold=False)
        self.until(lambda: self.fake.working_is("b"), "nothing carried on after the hold")

    def test_the_copy_in_front_runs_even_during_a_hold(self):
        self.ask("a", "b")
        self.until(lambda: self.fake.working_is("a"))
        self.maker.focus("b", hold=True)
        self.until(lambda: self.fake.working_is("b"))
        self.assertEqual(self.maker.state("a"), "paused")
        self.fake.release("b")
        pump(0.3)
        self.assertTrue(self.fake.working_is(), "the paused copy carried on during the hold")

    def test_at_most_MAX_PAUSED_copies_are_kept_and_the_oldest_is_dropped(self):
        with mock.patch.object(proxy, "MAX_PAUSED", 2):
            self.ask("a")
            self.until(lambda: self.fake.working_is("a"))
            for key in ("b", "c", "d"):
                self.ask(key)
                self.maker.focus(key)
                self.until(lambda key=key: self.fake.working_is(key))
        self.assertEqual(self.dropped, ["a"])
        self.assertFalse(self.maker.active("a"))
        self.until(lambda: self.fake.count("cancel", "a") == 1, "the dropped copy kept running")
        self.assertEqual([self.maker.state(key) for key in "bcd"], ["paused", "paused", "running"])

    def test_copies_paused_in_the_same_clock_tick_are_still_told_apart(self):
        # On Windows the clock ticks every 15.6 ms, so two pauses can get the same time.
        with mock.patch.object(proxy, "time", mock.Mock(monotonic=lambda: 5.0)):
            self.ask("a", "b")
            self.until(lambda: self.fake.working_is("a"))
            self.maker.focus("b")
            self.until(lambda: self.fake.working_is("b"))
            self.maker.focus("somebody else", hold=True)
            self.until(lambda: self.fake.working_is())
            self.maker.focus(None, hold=False)
            self.until(lambda: self.fake.working_is("b"), "the copy paused first carried on")
            self.assertEqual(self.maker.state("a"), "paused")

    def test_the_oldest_pause_is_the_one_dropped_even_when_the_clock_does_not_move(self):
        # b is paused before a, though a was asked for first (it comes first in the list).
        with mock.patch.object(proxy, "MAX_PAUSED",
                               1), mock.patch.object(proxy, "time",
                                                     mock.Mock(monotonic=lambda: 5.0)):
            self.ask("a", "b", "c")
            self.until(lambda: self.fake.working_is("a"))
            self.maker.focus("b")
            self.until(lambda: self.fake.working_is("b"))
            self.maker.focus("a")
            self.until(lambda: self.fake.working_is("a"))
            self.assertEqual(self.dropped, [])
            self.maker.focus("c")
            self.until(lambda: self.fake.working_is("c"))
        self.assertEqual(self.dropped, ["b"])
        self.assertEqual(self.maker.state("a"), "paused")

    def test_a_copy_paused_for_too_long_is_dropped(self):
        with mock.patch.object(proxy, "PAUSED_KEEP_SECONDS", 0.3):
            self.ask("a", "b")
            self.until(lambda: self.fake.working_is("a"))
            self.maker.focus("b")
            self.until(lambda: self.dropped == ["a"], "the paused copy was never dropped")
        self.assertFalse(self.maker.active("a"))
        self.until(lambda: self.fake.count("cancel", "a") == 1)
        self.assertEqual(self.maker.state("b"), "running", "the copy in front was disturbed")

    def test_a_copy_that_carried_on_is_not_dropped_by_the_timer_of_its_pause(self):
        with mock.patch.object(proxy, "PAUSED_KEEP_SECONDS", 0.4):
            self.ask("a", "b")
            self.maker.focus("b")
            self.until(lambda: self.fake.working_is("b"))
            self.maker.focus("a")
            self.until(lambda: self.fake.working_is("a"))
            pump(0.9)
        self.assertNotIn("a", self.dropped)
        self.assertTrue(self.fake.working_is("a"))

    def test_the_default_limits_are_a_few_copies_and_minutes(self):
        self.assertEqual(proxy.MAX_PAUSED, 3)
        self.assertEqual(proxy.PAUSED_KEEP_SECONDS, 300.0)

    def test_a_dropped_copy_can_be_asked_for_again_and_starts_over(self):
        with mock.patch.object(proxy, "PAUSED_KEEP_SECONDS", 0.2):
            self.ask("a", "b")
            self.maker.focus("b")
            self.until(lambda: self.dropped == ["a"])
        self.assertTrue(self.maker.request("a", self.tmp / "a.mp4", info(1920, 1080)))
        self.maker.focus("a")
        self.until(lambda: self.fake.count("start", "a") == 2)

    def test_cancelling_a_paused_copy_stops_it(self):
        self.ask("a", "b")
        self.maker.focus("b")
        self.until(lambda: self.fake.working_is("b"))
        self.maker.cancel("a")
        self.until(lambda: self.fake.count("cancel", "a") == 1)
        self.assertFalse(self.maker.active("a"))
        self.assertTrue(self.fake.working_is("b"))

    def test_cancelling_the_running_copy_lets_a_paused_one_carry_on(self):
        self.ask("a", "b")
        self.maker.focus("b")
        self.until(lambda: self.fake.working_is("b"))
        self.maker.focus(None)
        self.maker.cancel("b")
        self.until(lambda: self.fake.working_is("a"))
        self.assertEqual(self.done, [])

    def test_cancelling_the_waiting_copies_leaves_the_paused_ones_alone(self):
        self.ask("a", "b")
        self.until(lambda: self.fake.working_is("a"))
        self.maker.focus("b")
        self.until(lambda: self.fake.working_is("b"))
        self.ask("c")
        self.assertEqual(self.maker.cancel_waiting(), ["c"])
        self.assertEqual((self.maker.state("a"), self.maker.state("b")), ("paused", "running"))

    def test_shutting_down_ends_the_paused_copies_too(self):
        self.ask("a", "b")
        self.until(lambda: self.fake.working_is("a"))
        self.maker.focus("b")
        self.until(lambda: self.fake.working_is("b"))
        self.maker.shutdown()
        self.assertEqual(self.fake.count("cancel", "a"), 1)
        self.assertEqual(self.fake.count("cancel", "b"), 1)
        self.assertFalse(self.maker.request("c", self.tmp / "c.mp4", info(1920, 1080)))
        self.assertEqual((self.done, self.dropped), ([], []))


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class ProxyMakerPauseTests(unittest.TestCase):
    """Pausing real copies: nothing is written while a copy is paused, it carries on from where it
    was, and a paused copy can be cancelled without leaving ffmpeg or a file behind."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.source = cls.tmp / "big.mp4"
        make_video(cls.source, 320, 180, 4)
        cls.info = probe_media(FFMPEG, cls.source)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.folder = self.tmp / f"preview-{self.id().split('.')[-1]}"
        self.progress, self.done = [], []
        self.procs = recording_processes(self)
        for name, value in (("PROXY_LONG_SIDE", 160), ("proxy_command", paced)):
            patcher = mock.patch.object(proxy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.maker = ProxyMaker(
            FFMPEG, self.folder, lambda key, fraction: self.progress.append((key, fraction)),
            lambda key, path, error: self.done.append((key, path, error)))
        self.addCleanup(self.maker.shutdown)

    def until(self, condition, what="it never happened"):
        self.assertTrue(pump(TIMEOUT, condition), what)

    def written(self):
        return sum(f.stat().st_size for f in self.folder.glob("*.part.mp4"))

    def test_a_paused_copy_writes_nothing_and_carries_on_from_where_it_was(self):
        self.maker.request("a", self.source, self.info)
        self.until(lambda: self.progress and self.progress[-1][1] > 0.2, "no progress")
        self.maker.park("a")
        self.assertTrue(self.maker.paused("a"))
        pump(0.5)
        seen, size = len(self.progress), self.written()
        self.assertGreater(size, 0)
        pump(1.2)
        self.assertEqual(self.written(), size, "a paused copy kept writing")
        self.assertEqual(len(self.progress), seen, "a paused copy kept reporting")
        self.maker.park("a", False)
        self.until(lambda: self.done, "the copy never finished after being resumed")
        key, path, error = self.done[0]
        self.assertEqual((key, error), ("a", ""))
        self.assertAlmostEqual(probe_media(FFMPEG, path).duration, self.info.duration, delta=0.3)
        fractions = [fraction for _, fraction in self.progress]
        self.assertEqual(fractions, sorted(fractions), "the copy started over")
        self.assertEqual(len(self.procs), 1, "a second ffmpeg was started for the same copy")

    def test_the_work_moves_to_the_copy_in_front_and_comes_back(self):
        self.maker.request("a", self.source, self.info)
        self.until(lambda: self.progress and self.progress[-1][1] > 0.2, "no progress")
        self.maker.request("b", self.source, self.info)
        self.maker.focus("b")
        self.assertTrue(self.maker.paused("a"))
        pump(0.5)
        size = self.written()
        self.until(lambda: [key for key, _, _ in self.done] == ["b"], "b was not made first")
        self.until(lambda: len(self.done) == 2, "a never carried on")
        self.assertEqual([key for key, _, _ in self.done], ["b", "a"])
        self.assertTrue(all(error == "" for _, _, error in self.done))
        self.assertGreater(size, 0)
        fractions = [f for key, f in self.progress if key == "a"]
        self.assertEqual(fractions, sorted(fractions), "a started over")

    def test_a_paused_copy_can_be_cancelled_and_leaves_nothing_behind(self):
        self.maker.request("a", self.source, self.info)
        self.until(lambda: self.progress and self.progress[-1][1] > 0.1, "no progress")
        self.maker.park("a")
        pump(0.3)
        self.maker.cancel("a")
        self.until(lambda: all(p.poll() is not None for p in self.procs), "ffmpeg is still there")
        self.until(lambda: not list(self.folder.glob("*")), "a file was left behind")
        self.assertEqual(self.done, [], "a cancelled copy should report nothing")
        self.assertFalse(self.maker.active("a"))

    def test_shutting_down_with_a_paused_copy_leaves_no_ffmpeg_and_no_file(self):
        self.maker.request("a", self.source, self.info)
        self.until(lambda: self.progress and self.progress[-1][1] > 0.1, "no progress")
        self.maker.request("b", self.source, self.info)
        self.maker.focus("b")
        self.assertTrue(self.maker.paused("a"))
        self.until(lambda: any(key == "b" for key, _ in self.progress), "b never started")
        self.maker.shutdown()
        self.assertEqual(len(self.procs), 2)
        self.assertTrue(all(p.poll() is not None for p in self.procs), "ffmpeg is still there")
        rmtree_when_free(self.folder)
        self.assertEqual(self.done, [])


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class PlayerSwapTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.original = cls.tmp / "original.mp4"
        cls.small = cls.tmp / "small.mp4"
        make_video(cls.original, 320, 180, 6)
        make_video(cls.small, 160, 90, 6)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        from video_converter.ui.player import Player
        self.player = Player()
        self.frames = []
        self.player.frameReady.connect(
            lambda image, oriented: self.frames.append((image.width(), oriented)))
        self.addCleanup(self.player.load, None)

    def open(self, path):
        """Load a file and wait until it is ready to be seeked in (its first frame has been
        decoded)."""
        self.player.load(path)
        self.assertTrue(pump(TIMEOUT, lambda: self.frames), "the first frame never came")

    def test_swapping_carries_on_from_the_same_moment(self):
        self.open(self.original)
        self.player.seek(2.5)
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.player.position - 2.5) < 0.1))
        self.frames.clear()
        self.player.swap_source(self.small, upright=True)
        self.assertAlmostEqual(
            self.player.position, 2.5, delta=0.1)  # right away, not 0 while loading
        self.assertTrue(
            pump(TIMEOUT, lambda: any(width == 160 for width, _ in self.frames)),
            "no frame from the copy")
        self.assertTrue(pump(TIMEOUT, lambda: self.player._resume is None))
        self.assertAlmostEqual(self.player.position, 2.5, delta=0.3)
        self.assertFalse(self.player.playing)
        self.assertEqual(self.player.source, self.small)

    def test_a_swap_whose_loading_was_never_reported_still_finishes(self):
        # Qt sometimes reports the new source as loaded without ever reporting that it started
        # loading.
        self.open(self.original)
        self.assertTrue(pump(TIMEOUT, lambda: self.player.media.duration() > 0))
        from PySide6.QtMultimedia import QMediaPlayer
        loaded = (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia)
        self.assertTrue(pump(TIMEOUT, lambda: self.player.media.mediaStatus() in loaded))
        self.player._resume = (1500, False, True)
        self.player._new_source_loading = False
        self.player._check_swap()
        self.assertIsNone(self.player._resume, self.player.media.mediaStatus().name)
        self.assertTrue(self.player.upright)
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.player.position - 1.5) < 0.2))
        self.player.swap_source(
            self.small, upright=True)  # and the timer that does it is armed by a swap
        self.assertTrue(self.player._swap_watch.isActive())

    def test_the_position_never_shows_the_new_sources_zero(self):
        self.open(self.original)
        self.player.seek(3.0)
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.player.position - 3.0) < 0.1))
        heard = []
        self.player.positionChanged.connect(heard.append)
        self.player.swap_source(self.small, upright=True)
        pump(TIMEOUT, lambda: self.player._resume is None and bool(heard))
        self.assertTrue(heard)
        self.assertGreater(min(heard), 2.0, f"playhead jumped back: {heard}")

    def test_playing_keeps_playing_after_the_swap(self):
        self.open(self.original)
        self.player.seek(1.0)
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.player.position - 1.0) < 0.1))
        self.player.media.play()
        self.assertTrue(pump(TIMEOUT, lambda: self.player.position > 1.3))
        self.player.swap_source(self.small, upright=True)
        self.assertTrue(self.player.playing)
        self.assertTrue(pump(TIMEOUT, lambda: self.player._resume is None))
        self.assertTrue(
            pump(TIMEOUT, lambda: self.player.position > 1.8), "playback did not carry on")
        self.assertTrue(self.player.playing)
        self.player.pause()

    def test_a_seek_during_the_swap_is_not_lost(self):
        self.open(self.original)
        self.player.seek(1.0)
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.player.position - 1.0) < 0.1))
        self.player.swap_source(self.small, upright=True)
        self.player.seek(4.0)
        self.assertAlmostEqual(self.player.position, 4.0, delta=0.01)
        self.assertTrue(pump(TIMEOUT, lambda: self.player._resume is None))
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.player.position - 4.0) < 0.3))

    def test_frames_of_a_copy_are_marked_upright(self):
        self.player.load(self.small, upright=True)
        self.assertTrue(pump(TIMEOUT, lambda: self.frames))
        self.assertTrue(all(oriented for _, oriented in self.frames))

    def test_swapping_to_what_is_already_playing_does_nothing(self):
        self.player.load(self.original)
        self.player.swap_source(self.original, upright=False)
        self.assertIsNone(self.player._resume)

    def test_loading_something_else_cancels_a_swap(self):
        self.player.load(self.original)
        self.player.swap_source(self.small, upright=True)
        self.player.load(None)
        self.assertIsNone(self.player._resume)
        self.assertFalse(self.player.upright)


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class MainWindowPreviewTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.landscape = cls.tmp / "landscape.mp4"
        cls.portrait = cls.tmp / "portrait.mp4"
        make_video(cls.landscape, 320, 180, 4)
        make_video(cls.portrait, 320, 180, 3, rotation=-90)
        cls.small = cls.tmp / "small.mp4"
        make_video(cls.small, 128, 72, 2)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        # "Large" here means anything over 160 x 100 pixels, and the copy is 160 pixels on its long
        # side.
        for name, value in (("BIG_PIXELS", 160 * 100), ("PROXY_LONG_SIDE", 160)):
            patcher = mock.patch.object(proxy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ini = self.tmp / f"settings-{self.id().split('.')[-1]}.ini"
        self.window = self.new_window()

    def new_window(self):
        from video_converter.ui.main_window import MainWindow
        window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        window.resize(1200, 800)
        window.show()
        return window

    def tearDown(self):
        window = self.window
        tmpdir = window.tmpdir
        window.close()
        window.loader.shutdown(wait=True)
        self.assertFalse(tmpdir.exists(), "the window left its temp folder behind")
        window.deleteLater()
        self.window = None
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(0.1)

    def load(self, *paths):
        self.window.add_paths(list(paths))
        self.assertTrue(
            pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)),
            "files never loaded")

    def hold_copies(self):
        """Make the preview copies wait until the test lets them go (or they are cancelled)."""
        release = threading.Event()
        real = proxy.run_ffmpeg

        def slow(cmd, output, duration, on_progress, cancel_event, cwd=None):
            while not release.is_set() and not cancel_event.is_set():
                time.sleep(0.01)
            return real(cmd, output, duration, on_progress, cancel_event, cwd)

        patcher = mock.patch.object(proxy, "run_ffmpeg", side_effect=slow)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(release.set)
        return release

    def test_a_large_video_gets_a_copy_which_then_plays_instead(self):
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: item.proxy is not None), "no preview copy was made")
        self.assertTrue(pump(TIMEOUT, lambda: self.window.player.source == item.proxy))
        self.assertTrue(item.proxy.is_file())
        self.assertTrue(str(item.proxy).startswith(str(self.window.tmpdir)))
        self.assertEqual(self.window.preview_status.text(), "")
        self.assertTrue(
            pump(
                TIMEOUT,
                lambda: self.window.view.raw is not None and self.window.view.raw.width() == 160))

    def test_the_status_shows_the_progress_while_it_is_made(self):
        release = self.hold_copies()
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: item.proxy_progress is not None))
        self.assertEqual(self.window.preview_status.text(), "Preparing a smooth preview… 0%")
        self.assertIsNone(
            self.window.player.source, "the original must not play while its copy is made")
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.view.raw is not None), "no first picture was shown")
        release.set()
        self.assertTrue(pump(TIMEOUT, lambda: item.proxy is not None))
        self.assertTrue(pump(TIMEOUT, lambda: self.window.preview_status.text() == ""))

    def test_a_small_video_is_left_alone(self):
        with mock.patch.object(proxy, "BIG_PIXELS", 1920 * 1080):
            self.load(self.landscape)
            item = self.window.current
            pump(0.5)
            self.assertIsNone(item.proxy)
            self.assertIsNone(item.proxy_progress)
            self.assertFalse(self.window.proxies.active(item))
            self.assertEqual(self.window.player.source, item.path)

    def test_playback_carries_on_from_where_it_was_when_the_copy_arrives(self):
        self.window.smooth_action.setChecked(
            False)  # the original plays; the copy is asked for later
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: self.window.player.source == item.path))
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.player.media.duration() > 0),
            "the original never loaded")
        self.window.player.seek(2.0)
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.window.player.position - 2.0) < 0.1))
        release = self.hold_copies()
        self.window.smooth_action.setChecked(True)
        release.set()
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
        self.assertTrue(pump(TIMEOUT, lambda: self.window.player._resume is None))
        self.assertAlmostEqual(self.window.player.position, 2.0, delta=0.3)
        self.assertAlmostEqual(self.window.timeline.position, 2.0, delta=0.3)

    def test_the_crop_box_and_everything_that_makes_the_output_stay_on_the_original(self):
        release = self.hold_copies()
        self.load(self.landscape)
        item = self.window.current
        view = self.window.view
        self.assertTrue(pump(TIMEOUT, lambda: view.raw is not None))
        crop_before, rect_before, frame_before = view.crop, view.crop_rect(), view.frame_rect()
        self.assertEqual(crop_before[:2], (320, 180), "the box is drawn on the original's size")
        release.set()
        self.assertTrue(
            pump(TIMEOUT, lambda: view.raw is not None and view.raw.width() == 160),
            "no frame from the copy")
        self.assertEqual(view.raw.height(), 90)  # the frames really are the small ones
        self.assertEqual(view.crop, crop_before)
        self.assertEqual(view.crop_rect(), rect_before)
        self.assertEqual(view.frame_rect(), frame_before)
        # Moving the box still means the same thing.
        moves = []
        view.cropMoved.connect(lambda x, y: moves.append((x, y)))
        view._move_center_to(rect_before.center())
        self.assertAlmostEqual(moves[-1][0], item.settings.edit.pos_x, places=2)
        # Exports and the before/after picture read the original file at its own size.
        job = self.window._make_job(item)
        self.assertEqual(job.source, item.path)
        self.assertEqual((job.info.width, job.info.height), (320, 180))
        with mock.patch("video_converter.ui.main_window.playback.render_frame") as render:
            self.window.compare_button.setChecked(True)
            self.window._render_after()
            self.assertTrue(pump(TIMEOUT, lambda: render.called))
        self.assertEqual(render.call_args.args[1], item.path)
        self.assertEqual(
            (render.call_args.args[5].aspect, render.call_args.args[6].width),
            (item.settings.edit.aspect, 320))

    def test_a_phone_video_stays_upright_when_the_copy_takes_over(self):
        release = self.hold_copies()
        self.load(self.portrait)
        item = self.window.current
        view = self.window.view
        self.assertTrue(pump(TIMEOUT, lambda: view.raw is not None))
        self.assertLess(view.oriented_image().width(), view.oriented_image().height())
        release.set()
        self.assertTrue(
            pump(TIMEOUT, lambda: view.raw is not None and view.raw.width() < 100),
            "no frame from the copy")
        self.assertEqual(self.window.player.source, item.proxy)
        self.assertLess(view.oriented_image().width(), view.oriented_image().height())
        self.assertEqual(view.crop[:2], (180, 320))

    def test_selecting_a_file_whose_copy_is_ready_plays_the_copy_straight_away(self):
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: first.proxy is not None))
        self.window.media_list.setCurrentRow(1)
        self.assertTrue(pump(TIMEOUT, lambda: second.proxy is not None))
        self.window.media_list.setCurrentRow(0)
        self.assertEqual(self.window.player.source, first.proxy)

    def test_only_the_file_in_front_is_prepared_and_others_wait_their_turn(self):
        release = self.hold_copies()
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: self.window.proxies.active(first)))
        self.assertFalse(
            self.window.proxies.active(second), "only the selected file asks for a copy")
        self.window.media_list.setCurrentRow(1)
        self.assertTrue(pump(TIMEOUT, lambda: self.window.proxies.active(second)))
        release.set()

    def test_removing_the_file_stops_its_copy_and_deletes_it(self):
        self.hold_copies()
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: self.window.proxies.active(item)))
        self.window.remove_selected()
        self.assertFalse(self.window.proxies.active(item))
        self.assertIsNone(item.proxy_progress)
        self.assertEqual(self.window.preview_status.text(), "")
        self.assertEqual(list((self.window.tmpdir / "preview").glob("*.mp4")), [])

    def test_removing_a_file_with_a_ready_copy_deletes_the_copy(self):
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
        copy = item.proxy
        self.window.remove_selected()
        self.assertTrue(pump(TIMEOUT, lambda: not copy.exists()), "the copy was left behind")

    def test_closing_stops_the_work_and_removes_the_files(self):
        self.hold_copies()
        self.load(self.landscape, self.portrait)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: self.window.proxies.active(item)))
        folder = self.window.tmpdir
        self.window.close()
        self.assertFalse(folder.exists())
        self.assertTrue(self.window.proxies._closed)
        pump(0.3)
        self.assertFalse(folder.exists(), "a copy that finished late made the folder again")

    def test_closing_with_a_finished_copy_leaves_nothing_behind(self):
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
        folder = self.window.tmpdir
        self.window.close()
        self.assertFalse(folder.exists())

    def test_the_setting_is_on_by_default_and_remembered(self):
        self.assertTrue(self.window.smooth_previews)
        self.assertTrue(self.window.smooth_action.isChecked())
        self.window.smooth_action.setChecked(False)
        self.assertFalse(self.window.smooth_previews)
        self.window.close()
        self.window.loader.shutdown(wait=True)
        self.window.deleteLater()  # freed with the next window in tearDown
        pump(0.1)
        self.window = self.new_window()
        self.assertFalse(self.window.smooth_previews)
        self.assertFalse(self.window.smooth_action.isChecked())
        self.assertFalse(
            QSettings(str(self.ini),
                      QSettings.Format.IniFormat).value("smoothPreviews", True, type=bool))

    def test_with_the_setting_off_no_copy_is_made(self):
        self.window.smooth_action.setChecked(False)
        self.load(self.landscape)
        item = self.window.current
        pump(0.5)
        self.assertIsNone(item.proxy)
        self.assertFalse(self.window.proxies.active(item))
        self.assertEqual(self.window.player.source, item.path)

    def test_turning_it_off_goes_back_to_the_original_and_on_again_uses_the_copy(self):
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
        self.window.smooth_action.setChecked(False)
        self.assertEqual(self.window.player.source, item.path)
        self.assertTrue(
            pump(
                TIMEOUT,
                lambda: self.window.view.raw is not None and self.window.view.raw.width() == 320))
        self.window.smooth_action.setChecked(True)
        self.assertEqual(self.window.player.source, item.proxy)

    def test_turning_it_off_cancels_copies_that_are_being_made(self):
        self.hold_copies()
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: self.window.proxies.active(item)))
        self.window.smooth_action.setChecked(False)
        self.assertFalse(self.window.proxies.active(item))
        self.assertEqual(self.window.preview_status.text(), "")

    def test_a_copy_that_cannot_be_made_leaves_the_original_playing(self):
        with mock.patch.object(proxy, "run_ffmpeg", side_effect=ConversionError("no space")):
            self.load(self.landscape)
            item = self.window.current
            self.assertTrue(pump(TIMEOUT, lambda: item.proxy_failed))
        self.assertEqual(self.window.player.source, item.path)
        self.assertEqual(self.window.preview_status.text(), "")
        self.assertIn("original", self.window.statusBar().currentMessage())

    def test_the_original_is_never_played_while_its_copy_is_made(self):
        # Decoding a huge original while the copy was being made crashed Qt: it waits for the copy
        # instead.
        release = self.hold_copies()
        loaded = []
        real_load = self.window.player.load
        self.window.player.load = lambda path, upright=False: loaded.append(path) or real_load(
            path, upright=upright)
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: self.window.proxies.active(item)))
        pump(0.3)
        self.assertNotIn(item.path, loaded)
        self.assertIsNone(self.window.player.source)
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.view.raw is not None),
            "no first picture while waiting")
        self.assertFalse(self.window.play_button.isEnabled() and self.window.player.playing)
        release.set()
        self.assertTrue(
            pump(
                TIMEOUT,
                lambda: item.proxy is not None and self.window.player.source == item.proxy))
        self.assertNotIn(item.path, loaded)
        self.assertTrue(
            pump(
                TIMEOUT,
                lambda: self.window.view.raw is not None and self.window.view.raw.width() == 160))

    def test_pressing_play_while_the_copy_is_made_does_nothing_harmful(self):
        self.hold_copies()
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: self.window.proxies.active(item)))
        self.window.player.toggle()
        self.assertFalse(self.window.player.playing)
        self.assertIsNone(self.window.player.source)

    def test_a_file_waiting_for_its_copy_does_not_wait_behind_another_files_copy(self):
        self.hold_copies()
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: self.window.proxies.active(first)))
        self.window.media_list.setCurrentRow(1)
        # (since phase 3 the copy of the file left behind is paused, not cancelled)
        self.assertTrue(pump(TIMEOUT, lambda: self.window.proxies.state(second) == "running"))
        self.assertEqual(
            self.window.proxies.state(first), "paused",
            "the copy of the file left behind kept the queue busy")
        self.assertIsNotNone(first.proxy_progress)

    def test_when_the_original_will_not_play_the_copy_is_used(self):
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
        self.window.player.swap_source(item.path, upright=False)
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.player._resume is None), "the swap never finished")
        self.assertEqual(self.window.player.source, item.path)
        self.window._on_player_error("the decoder gave up")
        self.assertEqual(self.window.player.source, item.proxy)
        self.assertFalse(item.proxy_failed)

    def test_a_copy_that_will_not_play_falls_back_to_the_original(self):
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
        self.window._on_player_error("decoder said no")
        self.assertEqual(self.window.player.source, item.path)
        self.assertTrue(item.proxy_failed)

    # ----- copies kept paused, and "play as it is" (phase 3) ------------------------

    def fake_copies(self):
        """Replace ffmpeg with stand-ins that work until released and stand still when paused. The
        finished copy is a real (small) video, so that the player can load it."""
        fake = FakeRuns(self.landscape)
        patcher = mock.patch.object(proxy, "run_ffmpeg", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def select_row(self, row):
        self.window.media_list.setCurrentRow(row)

    def test_switching_to_another_file_pauses_the_copy_and_works_on_the_other(self):
        fake = self.fake_copies()
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.assertTrue(pump(TIMEOUT, lambda: first.proxy_progress == 0.4))
        self.select_row(1)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("portrait")))
        self.assertEqual(self.window.proxies.state(first), "paused")
        self.assertEqual(
            first.proxy_progress, 0.4, "the progress of the file left behind was lost")
        self.assertTrue(self.window.proxies.active(first))
        self.assertIn("Preparing", self.window.preview_status.text())

    def test_going_back_to_a_file_carries_its_copy_on_from_where_it_was(self):
        fake = self.fake_copies()
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: first.proxy_progress == 0.4))
        self.select_row(1)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("portrait")))
        self.select_row(0)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.assertEqual(self.window.proxies.state(second), "paused")
        self.assertEqual(fake.count("start", "landscape"), 1, "the copy started over")
        self.assertEqual(first.proxy_progress, 0.4)

    def test_a_paused_copy_carries_on_in_the_background_when_the_other_is_done(self):
        fake = self.fake_copies()
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.select_row(1)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("portrait")))
        fake.release("portrait")
        self.assertTrue(pump(TIMEOUT, lambda: second.proxy is not None))
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.assertIs(self.window.current, second)
        fake.release("landscape")
        self.assertTrue(pump(TIMEOUT, lambda: first.proxy is not None), "the copy was never made")
        self.select_row(0)
        self.assertEqual(self.window.player.source, first.proxy)

    def test_selecting_a_small_file_does_not_stop_the_copy_of_another(self):
        fake = self.fake_copies()
        self.load(self.landscape, self.portrait, self.small)
        first, second, small = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.select_row(2)
        pump(0.2)
        self.assertTrue(fake.working_is("landscape"), "a small file stopped the copy of another")
        self.assertIsNone(self.window.proxies.state(second))
        self.assertIsNone(self.window.proxies.state(small))

    def test_playing_a_file_as_it_is_pauses_its_copy_and_ticking_it_again_carries_on(self):
        fake = self.fake_copies()
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: item.proxy_progress == 0.4))
        self.window.set_play_original(item, True)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is()))
        self.assertEqual(self.window.proxies.state(item), "paused")
        self.assertEqual(item.proxy_progress, 0.4, "pausing threw the progress away")
        self.assertEqual(self.window.player.source, item.path)
        self.assertIn("paused", self.window.preview_status.text())
        self.assertIn("40%", self.window.preview_status.text())
        self.assertTrue(self.window.as_is_action.isChecked())
        self.window.set_play_original(item, False)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.assertEqual(fake.count("start", "landscape"), 1, "the copy started over")
        self.assertEqual(fake.count("resume", "landscape"), 1)
        self.assertNotIn("paused", self.window.preview_status.text())
        self.assertFalse(self.window.as_is_action.isChecked())
        self.assertIsNone(self.window.player.source, "the original plays while its copy is made")
        fake.release("landscape")
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))

    def test_a_copy_that_finished_before_playing_as_it_is_is_used_again_when_ticked_off(self):
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(
            pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
        copy = item.proxy
        self.window.set_play_original(item, True)
        self.assertEqual(self.window.player.source, item.path)
        self.assertEqual(item.proxy, copy, "the copy was thrown away")
        self.assertTrue(copy.is_file())
        self.window.set_play_original(item, False)
        self.assertEqual(self.window.player.source, copy)

    def test_the_as_is_choice_is_shown_again_when_the_file_is_selected(self):
        self.fake_copies()
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        action = self.window.as_is_action
        self.assertTrue(action.isEnabled())
        self.assertFalse(action.isChecked())
        self.window.set_play_original(first, True)
        self.assertTrue(action.isChecked())
        self.select_row(1)
        self.assertFalse(action.isChecked())
        self.select_row(0)
        self.assertTrue(action.isChecked())
        action.setChecked(False)  # the View menu item does the same as the list's menu
        self.assertFalse(first.play_original)
        self.window.set_smooth_previews(False)
        self.assertFalse(
            action.isEnabled(), "there is no copy to choose against with the setting off")

    def test_the_list_menu_has_the_choice_and_shows_whether_it_is_on(self):
        self.fake_copies()
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        self.window.set_play_original(second, True)
        menus = []

        class RecordingMenu(QMenu):
            """A menu that is only recorded, not shown (showing one waits for a click)."""

            def exec(self, *args):
                menus.append(self)

        def open_menu(row):
            point = self.window.media_list.visualItemRect(
                self.window.items[row].list_item).center()
            with mock.patch("video_converter.ui.main_window.library.QMenu", RecordingMenu):
                self.window._media_menu(point)
            return [a for a in menus[-1].actions() if a.text() == "Play this file as it is"]

        (off, ) = open_menu(0)
        (on, ) = open_menu(1)
        self.assertTrue(off.isCheckable() and not off.isChecked())
        self.assertTrue(on.isChecked())
        off.toggle()
        self.assertTrue(first.play_original)
        on.toggle()
        self.assertFalse(second.play_original)

    def test_a_file_set_to_play_as_it_is_gets_no_copy_when_it_is_selected(self):
        fake = self.fake_copies()
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        self.window.set_play_original(second, True)
        self.select_row(1)
        pump(0.3)
        self.assertEqual(self.window.player.source, second.path)
        self.assertIsNone(self.window.proxies.state(second))
        self.assertIsNone(second.proxy_progress)
        self.assertEqual(fake.count("start", "portrait"), 0)
        self.assertEqual(self.window.preview_status.text(), "")

    def test_a_big_file_played_as_it_is_keeps_the_other_paused_copies_paused(self):
        fake = self.fake_copies()
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.select_row(1)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("portrait")))
        self.window.set_play_original(second, True)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is()))
        pump(0.4)
        self.assertTrue(fake.working_is(), "a paused copy carried on next to a big original")
        self.assertEqual(self.window.proxies.state(first), "paused")
        self.assertEqual(self.window.proxies.state(second), "paused")
        # Ticking it off again gives that copy the work first; the other carries on after it.
        self.window.set_play_original(second, False)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("portrait")))
        self.assertEqual(self.window.proxies.state(first), "paused")
        fake.release("portrait")
        self.assertTrue(pump(TIMEOUT, lambda: second.proxy is not None))
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))

    def test_moving_off_the_big_original_lets_the_other_copies_carry_on(self):
        fake = self.fake_copies()
        self.load(self.landscape, self.portrait, self.small)
        first, second, small = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.select_row(1)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("portrait")))
        self.window.set_play_original(second, True)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is()))
        self.select_row(2)  # a small file: nothing heavy is playing now
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.assertEqual(
            self.window.proxies.state(second), "paused",
            "a copy of a file set to play as it is carried on")

    def test_a_small_file_played_as_it_is_does_not_hold_the_other_copies(self):
        fake = self.fake_copies()
        self.load(self.landscape, self.portrait, self.small)
        first, second, small = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.select_row(1)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("portrait")))
        self.select_row(2)
        self.window.set_play_original(small, True)
        pump(0.3)
        self.assertTrue(fake.working_is("portrait"), "a small original held the copy back")
        fake.release("portrait")
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))

    def test_a_paused_copy_nobody_comes_back_to_is_dropped(self):
        fake = self.fake_copies()
        with mock.patch.object(proxy, "PAUSED_KEEP_SECONDS", 0.3):
            self.load(self.landscape, self.portrait)
            first, second = self.window.items
            self.assertTrue(pump(TIMEOUT, lambda: first.proxy_progress == 0.4))
            self.select_row(1)
            self.assertTrue(pump(TIMEOUT, lambda: first.proxy_progress is None))
        self.assertFalse(self.window.proxies.active(first))
        self.assertTrue(pump(TIMEOUT, lambda: fake.count("cancel", "landscape") == 1))
        self.assertTrue(fake.working_is("portrait"), "the copy in front was disturbed")
        self.select_row(0)  # it has to start again
        self.assertTrue(pump(TIMEOUT, lambda: fake.count("start", "landscape") == 2))

    def test_turning_the_setting_off_drops_the_paused_copies_too(self):
        fake = self.fake_copies()
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("landscape")))
        self.select_row(1)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is("portrait")))
        self.window.smooth_action.setChecked(False)
        self.assertFalse(self.window.proxies.active(first))
        self.assertFalse(self.window.proxies.active(second))
        self.assertIsNone(first.proxy_progress)
        self.assertTrue(pump(TIMEOUT, lambda: fake.working_is()))

    def test_removing_a_file_with_a_paused_copy_stops_ffmpeg_and_leaves_no_file(self):
        procs = recording_processes(self)
        with mock.patch.object(proxy, "proxy_command", paced):
            self.load(self.landscape)
            item = self.window.current
            self.assertTrue(pump(TIMEOUT, lambda: item.proxy_progress))
            self.window.set_play_original(item, True)
            self.assertEqual(self.window.proxies.state(item), "paused")
            self.window.remove_selected()
            self.assertTrue(
                pump(TIMEOUT, lambda: procs and all(p.poll() is not None for p in procs)),
                "ffmpeg is still there")
            self.assertTrue(
                pump(TIMEOUT, lambda: not list((self.window.tmpdir / "preview").glob("*"))),
                "a file was left behind")
        self.assertFalse(self.window.proxies.active(item))

    def test_closing_with_a_paused_copy_leaves_no_ffmpeg_and_no_folder(self):
        procs = recording_processes(self)
        with mock.patch.object(proxy, "proxy_command", paced):
            self.load(self.landscape)
            item = self.window.current
            self.assertTrue(pump(TIMEOUT, lambda: item.proxy_progress))
            self.window.set_play_original(item, True)
            self.assertEqual(self.window.proxies.state(item), "paused")
            folder = self.window.tmpdir
            self.window.close()
            self.assertTrue(
                all(p.poll() is not None for p in procs), "ffmpeg was left running after closing")
            self.assertFalse(folder.exists())


if __name__ == "__main__":
    unittest.main()
