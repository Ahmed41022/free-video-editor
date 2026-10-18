"""Smooth previews (2.4): which videos get a small preview copy, making it, and playing it in the editor."""

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
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    from PySide6.QtCore import QEvent, QSettings
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter import proxy
from video_converter.core import ConversionCancelled, ConversionError, MediaInfo, find_ffmpeg, parse_media_info, probe_media
from video_converter.proxy import ProxyMaker, needs_proxy, proxy_command, proxy_size

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
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={width}x{height}:rate=15"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    if rotation is not None:
        tmp = path.with_name("tmp-" + path.name)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", str(rotation),
                        "-i", str(path), "-c", "copy", str(tmp)], check=True)
        tmp.replace(path)


def info(width, height, kbps=None, duration=60.0, audio=True):
    return MediaInfo(duration=duration, width=width, height=height, has_audio=audio, fps=30.0, bitrate=kbps)


class DecisionTests(unittest.TestCase):
    def test_ordinary_videos_are_played_as_they_are(self):
        for size in ((1920, 1080), (1080, 1920), (1280, 720), (640, 360), (1080, 1080)):
            with self.subTest(size=size):
                self.assertFalse(needs_proxy(info(*size, kbps=8000)))

    def test_videos_bigger_than_1080p_get_a_copy(self):
        for size in ((2560, 1440), (3840, 2160), (2160, 3840), (4096, 2160), (7680, 4320), (2704, 1520)):
            with self.subTest(size=size):
                self.assertTrue(needs_proxy(info(*size, kbps=8000)))

    def test_a_very_high_bitrate_gets_a_copy_at_any_size(self):
        self.assertTrue(needs_proxy(info(1920, 1080, kbps=45000)))
        self.assertTrue(needs_proxy(info(1280, 720, kbps=20001)))
        self.assertFalse(needs_proxy(info(1920, 1080, kbps=20000)))       # "above" 20 Mbit/s
        self.assertFalse(needs_proxy(info(1920, 1080, kbps=None)))

    def test_things_that_are_not_playable_pictures_never_do(self):
        self.assertFalse(needs_proxy(None))
        self.assertFalse(needs_proxy(MediaInfo(duration=60.0, has_audio=True, bitrate=900000)))      # audio only
        self.assertFalse(needs_proxy(info(3840, 2160, duration=None)))
        self.assertFalse(needs_proxy(info(3840, 2160, duration=0.0)))

    def test_the_thresholds_can_be_lowered(self):
        with mock.patch.object(proxy, "BIG_PIXELS", 100 * 100):
            self.assertTrue(needs_proxy(info(320, 180)))
            self.assertFalse(needs_proxy(info(96, 96)))

    def test_the_bitrate_is_read_from_ffmpegs_description(self):
        text = ("Input #0, mov,mp4, from 'a.mp4':\n  Duration: 00:00:20.00, start: 0.000000, bitrate: 45123 kb/s\n"
                "  Stream #0:0[0x1](und): Video: h264 (High), yuv420p, 3840x2160, 44000 kb/s, 30 fps, 30 tbr\n")
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
        self.assertIn("expr:gte(t,n_forced*1)", text)         # a keyframe every second
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertIn("0:a:0?", cmd)                            # no audio is fine
        self.assertEqual(cmd[cmd.index("-i") + 1], "big.mp4")
        self.assertEqual(cmd[-1], "out.mp4")


def slowly(*args, **kwargs):
    """The preview command with a slow x264 preset, so a copy takes long enough to be cancelled midway."""
    return [("veryslow" if part == "ultrafast" else part) for part in proxy_command(*args, **kwargs)]


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
        self.maker = ProxyMaker(FFMPEG, self.folder, lambda k, f: self.progress.append((k, f)), self.report)
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
        self.assertEqual(list(self.folder.glob("*.part.mp4")), [], "the half-made file should be renamed")

    def test_there_is_a_keyframe_every_second(self):
        self.maker.request("a", self.source, self.info)
        self.assertTrue(self.finished.wait(TIMEOUT))
        shown = subprocess.run([FFMPEG, "-hide_banner", "-i", str(self.done[0][1]), "-vf", "select=key,showinfo",
                                "-f", "null", "-"], capture_output=True, text=True).stderr
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
        with mock.patch.object(proxy, "PROXY_LONG_SIDE", 1280), mock.patch.object(proxy, "proxy_command", slowly):
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
        with mock.patch.object(proxy, "PROXY_LONG_SIDE", 1280), mock.patch.object(proxy, "proxy_command", slowly):
            self.maker.request("one", big, long_info)
            self.maker.request("two", big, long_info)
            self.assertTrue(pump(TIMEOUT, lambda: self.progress))
            self.maker.shutdown()
        self.assertEqual(self.done, [])
        self.assertFalse(self.maker.request("three", self.source, self.info), "no new work after shutting down")
        shutil.rmtree(self.folder)

    def test_a_cancel_raised_inside_ffmpeg_reports_nothing(self):
        with mock.patch.object(proxy, "run_ffmpeg", side_effect=ConversionCancelled()):
            self.maker.request("a", self.source, self.info)
            self.maker.shutdown()
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
        self.player.frameReady.connect(lambda image, oriented: self.frames.append((image.width(), oriented)))
        self.addCleanup(self.player.load, None)

    def open(self, path):
        """Load a file and wait until it is ready to be seeked in (its first frame has been decoded)."""
        self.player.load(path)
        self.assertTrue(pump(TIMEOUT, lambda: self.frames), "the first frame never came")

    def test_swapping_carries_on_from_the_same_moment(self):
        self.open(self.original)
        self.player.seek(2.5)
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.player.position - 2.5) < 0.1))
        self.frames.clear()
        self.player.swap_source(self.small, upright=True)
        self.assertAlmostEqual(self.player.position, 2.5, delta=0.1)       # right away, not 0 while loading
        self.assertTrue(pump(TIMEOUT, lambda: any(width == 160 for width, _ in self.frames)), "no frame from the copy")
        self.assertTrue(pump(TIMEOUT, lambda: self.player._resume is None))
        self.assertAlmostEqual(self.player.position, 2.5, delta=0.3)
        self.assertFalse(self.player.playing)
        self.assertEqual(self.player.source, self.small)

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
        self.assertTrue(pump(TIMEOUT, lambda: self.player.position > 1.8), "playback did not carry on")
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

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        # "Large" here means anything over 160 x 100 pixels, and the copy is 160 pixels on its long side.
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
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)), "files never loaded")

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
        self.assertTrue(pump(TIMEOUT, lambda: self.window.view.raw is not None and self.window.view.raw.width() == 160))

    def test_the_status_shows_the_progress_while_it_is_made(self):
        release = self.hold_copies()
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: item.proxy_progress is not None))
        self.assertEqual(self.window.preview_status.text(), "Preparing a smooth preview… 0%")
        self.assertEqual(self.window.player.source, item.path, "the original plays until the copy is ready")
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
        release = self.hold_copies()
        self.load(self.landscape)
        item = self.window.current
        self.window.player.seek(2.0)
        self.assertTrue(pump(TIMEOUT, lambda: abs(self.window.player.position - 2.0) < 0.1))
        release.set()
        self.assertTrue(pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
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
        self.assertTrue(pump(TIMEOUT, lambda: view.raw is not None and view.raw.width() == 160), "no frame from the copy")
        self.assertEqual(view.raw.height(), 90)                          # the frames really are the small ones
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
        with mock.patch("video_converter.ui.main_window.render_frame") as render:
            self.window.compare_button.setChecked(True)
            self.window._render_after()
            self.assertTrue(pump(TIMEOUT, lambda: render.called))
        self.assertEqual(render.call_args.args[1], item.path)
        self.assertEqual((render.call_args.args[5].aspect, render.call_args.args[6].width), (item.settings.edit.aspect, 320))

    def test_a_phone_video_stays_upright_when_the_copy_takes_over(self):
        release = self.hold_copies()
        self.load(self.portrait)
        item = self.window.current
        view = self.window.view
        self.assertTrue(pump(TIMEOUT, lambda: view.raw is not None))
        self.assertLess(view.oriented_image().width(), view.oriented_image().height())
        release.set()
        self.assertTrue(pump(TIMEOUT, lambda: view.raw is not None and view.raw.width() < 100), "no frame from the copy")
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
        self.assertFalse(self.window.proxies.active(second), "only the selected file asks for a copy")
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
        self.assertTrue(pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
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
        self.assertTrue(pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
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
        pump(0.1)
        self.window = self.new_window()
        self.assertFalse(self.window.smooth_previews)
        self.assertFalse(self.window.smooth_action.isChecked())
        self.assertFalse(QSettings(str(self.ini), QSettings.Format.IniFormat).value("smoothPreviews", True, type=bool))

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
        self.assertTrue(pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
        self.window.smooth_action.setChecked(False)
        self.assertEqual(self.window.player.source, item.path)
        self.assertTrue(pump(TIMEOUT, lambda: self.window.view.raw is not None and self.window.view.raw.width() == 320))
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

    def test_a_copy_that_will_not_play_falls_back_to_the_original(self):
        self.load(self.landscape)
        item = self.window.current
        self.assertTrue(pump(TIMEOUT, lambda: self.window.player.source == item.proxy and item.proxy))
        self.window._on_player_error("decoder said no")
        self.assertEqual(self.window.player.source, item.path)
        self.assertTrue(item.proxy_failed)


if __name__ == "__main__":
    unittest.main()
