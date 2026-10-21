"""Tests for the Qt editor. They run without a visible window and are skipped when
PySide6 isn't installed (e.g. `python -m unittest` outside the build environment)."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("VIDEO_EDITOR_NO_GPU_SEARCH", "1")      # a window never searches for graphics cards (see tests/__init__.py)
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

try:
    import shiboken6
    from PySide6.QtCore import QEvent, QModelIndex, QPoint, QPointF, QSettings, Qt
    from PySide6.QtGui import QColor, QContextMenuEvent, QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
except ImportError:  # pragma: no cover - depends on the environment
    QApplication = None

from video_converter.core import MediaInfo, find_ffmpeg, probe_media
from video_converter.project import OUTPUT_AUDIO, ClipSettings
from video_converter.video import UP_TO_1080, VideoEdit

FFMPEG = find_ffmpeg()
TIMEOUT = 30


def setUpModule():
    if QApplication is None:
        raise unittest.SkipTest("PySide6 is not installed")
    global app
    app = QApplication.instance() or QApplication([])


def pump(seconds, until=lambda: False):
    """Process Qt events until `until()` is true or time runs out. Returns until()."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        QApplication.processEvents()
        time.sleep(0.01)
    return until()


def make_video(path, width=320, height=180, seconds=3, audio=True, rotation=None):
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


class TimelineMathTests(unittest.TestCase):
    def test_time_and_x_are_inverse_and_clamped(self):
        from video_converter.ui.timeline import time_to_x, x_to_time
        for t in (0.0, 1.25, 7.5, 10.0):
            self.assertAlmostEqual(x_to_time(time_to_x(t, 10, 12, 500), 10, 12, 500), t)
        self.assertEqual(time_to_x(-3, 10, 12, 500), 12)
        self.assertEqual(time_to_x(99, 10, 12, 500), 512)
        self.assertEqual(x_to_time(-100, 10, 12, 500), 0.0)
        self.assertEqual(x_to_time(9999, 10, 12, 500), 10.0)
        self.assertEqual(x_to_time(50, 0, 12, 500), 0.0)

    def test_trims_at_the_edges_mean_no_trim(self):
        from video_converter.ui.timeline import normalize_trim
        self.assertEqual(normalize_trim(0.01, 9.99, 10), (None, None))
        self.assertEqual(normalize_trim(2.0, 8.0, 10), (2.0, 8.0))
        self.assertEqual(normalize_trim(None, None, 10), (None, None))

    def test_ruler_steps_leave_room_for_labels(self):
        from video_converter.ui.timeline import tick_step
        for duration in (2, 30, 600, 7200):
            step = tick_step(duration, 800)
            self.assertGreaterEqual(step / duration * 800, 70)
        self.assertGreaterEqual(tick_step(3600, 800), tick_step(60, 800))


class CutEdgeMathTests(unittest.TestCase):
    def assertRanges(self, actual, expected):
        self.assertEqual(len(actual), len(expected), actual)
        for (a, b), (x, y) in zip(actual, expected):
            self.assertAlmostEqual(a, x)
            self.assertAlmostEqual(b, y)

    def test_edges_of_separate_parts(self):
        from video_converter.ui.timeline import cut_edges
        self.assertEqual(cut_edges([(2.0, 5.0), (7.0, 9.0)], 10.0),
                         [(2.0, 0, "start"), (5.0, 0, "end"), (7.0, 1, "start"), (9.0, 1, "end")])

    def test_the_ends_of_the_video_and_splits(self):
        from video_converter.ui.timeline import cut_edges
        # Nothing to grab at 0 or at the end of the video; touching ranges share one "split" edge.
        self.assertEqual(cut_edges([(0.0, 3.0), (3.0, 6.0), (8.0, 10.0)], 10.0),
                         [(3.0, 0, "split"), (6.0, 1, "end"), (8.0, 2, "start")])

    def test_no_cuts_no_edges(self):
        from video_converter.ui.timeline import cut_edges
        self.assertEqual(cut_edges([], 10.0), [])

    def test_moving_a_start_edge_stays_clear_of_its_neighbours(self):
        from video_converter.ui.timeline import move_edge
        keep = [(2.0, 5.0), (7.0, 9.0)]
        self.assertRanges(move_edge(keep, 1, "start", 7.5, 10.0), [(2, 5), (7.5, 9)])
        self.assertRanges(move_edge(keep, 1, "start", 3.0, 10.0), [(2, 5), (5, 9)])        # stops at the part before
        self.assertRanges(move_edge(keep, 1, "start", 8.95, 10.0), [(2, 5), (8.9, 9)])     # keeps MIN_PART
        self.assertRanges(move_edge(keep, 0, "start", -1.0, 10.0), [(0, 5), (7, 9)])       # stops at the video start

    def test_moving_an_end_edge_stays_clear_of_its_neighbours(self):
        from video_converter.ui.timeline import move_edge
        keep = [(2.0, 5.0), (7.0, 9.0)]
        self.assertRanges(move_edge(keep, 0, "end", 6.0, 10.0), [(2, 6), (7, 9)])
        self.assertRanges(move_edge(keep, 0, "end", 8.0, 10.0), [(2, 7), (7, 9)])          # stops at the next part
        self.assertRanges(move_edge(keep, 0, "end", 2.05, 10.0), [(2, 2.1), (7, 9)])       # keeps MIN_PART
        self.assertRanges(move_edge(keep, 1, "end", 12.0, 10.0), [(2, 5), (7, 10)])        # stops at the video end

    def test_moving_a_split_moves_both_ranges(self):
        from video_converter.ui.timeline import move_edge
        keep = [(0.0, 3.0), (3.0, 6.0), (8.0, 10.0)]
        self.assertRanges(move_edge(keep, 0, "split", 4.5, 10.0), [(0, 4.5), (4.5, 6), (8, 10)])
        self.assertRanges(move_edge(keep, 0, "split", 0.01, 10.0), [(0, 0.1), (0.1, 6), (8, 10)])
        self.assertRanges(move_edge(keep, 0, "split", 9.0, 10.0), [(0, 5.9), (5.9, 6), (8, 10)])

    def test_moving_an_edge_does_not_change_the_input(self):
        from video_converter.ui.timeline import move_edge
        keep = [(2.0, 5.0), (7.0, 9.0)]
        move_edge(keep, 0, "end", 6.0, 10.0)
        self.assertEqual(keep, [(2.0, 5.0), (7.0, 9.0)])


class TimelineWidgetTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.timeline import Timeline
        self.timeline = Timeline()
        self.timeline.resize(524, 130)        # 500 px of track between the 12 px margins
        self.timeline.setDuration(10.0)
        self.timeline.show()
        self.seeks, self.trims, self.finished = [], [], []
        self.timeline.seekRequested.connect(self.seeks.append)
        self.timeline.trimChanged.connect(lambda s, e: self.trims.append((s, e)))
        self.timeline.trimEditFinished.connect(lambda: self.finished.append(True))

    def tearDown(self):
        self.timeline.close()
        shiboken6.delete(self.timeline)     # free it now: every widget left alive slows each later theme change

    def drag(self, x0, x1):
        y = 60
        QTest.mousePress(self.timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x0, y))
        QTest.mouseMove(self.timeline, QPoint(x1, y))
        QTest.mouseRelease(self.timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x1, y))

    def test_clicking_the_track_seeks(self):
        self.drag(262, 262)                   # the middle of the track
        self.assertAlmostEqual(self.seeks[-1], 5.0, delta=0.05)
        self.assertEqual(self.trims, [])

    def test_dragging_the_start_handle_trims(self):
        self.drag(12, 112)                    # from 0 s to 2 s
        start, end = self.trims[-1]
        self.assertAlmostEqual(start, 2.0, delta=0.05)
        self.assertIsNone(end)
        self.assertEqual(self.finished, [True])

    def test_dragging_the_end_handle_trims(self):
        self.drag(512, 412)                   # from 10 s to 8 s
        self.assertAlmostEqual(self.trims[-1][1], 8.0, delta=0.05)

    def test_handles_cannot_cross(self):
        self.timeline.setTrim(4.0, 6.0)
        self.drag(12 + 200, 12 + 450)         # drag the start handle past the end
        start, end = self.trims[-1]
        self.assertLess(start, end)

    def test_external_trim_updates_are_ignored_mid_drag(self):
        QTest.mousePress(self.timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(12, 60))
        self.timeline.setTrim(5.0, None)      # e.g. an undo arriving while dragging
        self.assertIsNone(self.timeline.start)
        QTest.mouseRelease(self.timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(12, 60))


class TimelineCutTests(unittest.TestCase):
    """The track is 500 px wide from x=12, so 50 px per second of a 10 s video."""

    def setUp(self):
        from video_converter.ui.timeline import Timeline
        self.timeline = Timeline()
        self.timeline.resize(524, 130)
        self.timeline.setDuration(10.0)
        self.timeline.show()
        self.timeline.setKeep([(2.0, 5.0), (7.0, 9.0)])
        self.seeks, self.trims, self.keeps, self.keep_done, self.trim_done = [], [], [], [], []
        self.timeline.seekRequested.connect(self.seeks.append)
        self.timeline.trimChanged.connect(lambda s, e: self.trims.append((s, e)))
        self.timeline.keepChanged.connect(self.keeps.append)
        self.timeline.keepEditFinished.connect(lambda: self.keep_done.append(True))
        self.timeline.trimEditFinished.connect(lambda: self.trim_done.append(True))

    def tearDown(self):
        self.timeline.close()
        shiboken6.delete(self.timeline)     # free it now: every widget left alive slows each later theme change

    @staticmethod
    def x(seconds):
        return 12 + int(round(seconds * 50))

    def press(self, x, button=None):
        QTest.mousePress(self.timeline, button or Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(x, 60))

    def move(self, x):
        QTest.mouseMove(self.timeline, QPoint(x, 60))

    def release(self, x, button=None):
        QTest.mouseRelease(self.timeline, button or Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           QPoint(x, 60))

    def drag(self, x0, x1):
        self.press(x0)
        self.move(x1)
        self.release(x1)

    def test_dragging_a_cut_edge_moves_it(self):
        self.drag(self.x(5), self.x(6))                              # the end of the first part
        self.assertEqual(self.keeps[-1], [(2.0, 6.0), (7.0, 9.0)])
        self.assertEqual(self.timeline.keep, [(2.0, 6.0), (7.0, 9.0)])
        self.assertEqual(self.keep_done, [True])
        self.assertEqual(self.trims, [])
        self.assertAlmostEqual(self.seeks[-1], 5.96, delta=0.02)       # shows the new last frame of that part

    def test_dragging_a_start_edge_seeks_to_where_the_part_now_starts(self):
        self.drag(self.x(7), self.x(7.5))
        self.assertEqual(self.keeps[-1], [(2.0, 5.0), (7.5, 9.0)])
        self.assertAlmostEqual(self.seeks[-1], 7.5, delta=0.02)

    def test_a_cut_edge_cannot_be_dragged_past_its_neighbour(self):
        self.drag(self.x(5), self.x(9.5))
        self.assertEqual(self.timeline.keep, [(2.0, 7.0), (7.0, 9.0)])       # stops where the next part begins

    def test_one_edit_finished_signal_per_drag_however_many_moves(self):
        self.press(self.x(5))
        for seconds in (5.5, 6.0, 5.2):
            self.move(self.x(seconds))
        self.release(self.x(5.2))
        self.assertEqual(self.keep_done, [True])
        self.assertGreaterEqual(len(self.keeps), 3)

    def test_dragging_a_split_marker_moves_both_ranges(self):
        self.timeline.setKeep([(0.0, 3.0), (3.0, 6.0), (8.0, 10.0)])
        self.drag(self.x(3), self.x(2))
        self.assertEqual(self.timeline.keep, [(0.0, 2.0), (2.0, 6.0), (8.0, 10.0)])
        self.assertEqual(self.keeps[-1], [(0.0, 2.0), (2.0, 6.0), (8.0, 10.0)])
        self.assertAlmostEqual(self.seeks[-1], 2.0, delta=0.02)

    def test_the_trim_handle_wins_over_a_cut_edge_at_the_same_place(self):
        self.timeline.setTrim(2.0, None)                              # the first part also starts at 2 s
        self.assertEqual(self.timeline.hit_test(self.x(2)), "in")
        self.drag(self.x(2), self.x(3))
        self.assertAlmostEqual(self.trims[-1][0], 3.0, delta=0.05)
        self.assertEqual(self.keeps, [])
        self.assertEqual(self.timeline.keep, [(2.0, 5.0), (7.0, 9.0)])
        self.assertEqual((self.trim_done, self.keep_done), ([True], []))

    def test_what_a_press_grabs(self):
        self.assertEqual(self.timeline.hit_test(self.x(5)), "cut")
        self.assertEqual(self.timeline.hit_test(self.x(5) + 6), "cut")
        self.assertEqual(self.timeline.hit_test(self.x(6)), "playhead")
        self.assertEqual(self.timeline.hit_test(12), "in")
        self.assertEqual(self.timeline.hit_test(512), "out")
        self.timeline.setKeep([])
        self.assertEqual(self.timeline.hit_test(self.x(5)), "playhead")

    def test_the_nearest_edge_is_chosen(self):
        self.timeline.setKeep([(2.0, 2.1), (2.14, 6.0)])
        self.assertEqual(self.timeline.edge_at(self.x(2.0)), (0, "start"))
        self.assertEqual(self.timeline.edge_at(self.x(2.14) + 1), (1, "start"))

    def test_clicking_between_edges_seeks_and_leaves_the_cuts_alone(self):
        self.drag(self.x(6), self.x(6))
        self.assertAlmostEqual(self.seeks[-1], 6.0, delta=0.05)
        self.assertEqual(self.keeps, [])
        self.assertEqual(self.keep_done, [])

    def test_external_cut_updates_are_ignored_mid_drag(self):
        self.press(self.x(5))
        self.timeline.setKeep([])                                     # e.g. an undo arriving while dragging
        self.assertEqual(self.timeline.keep, [(2.0, 5.0), (7.0, 9.0)])
        self.release(self.x(5))
        self.timeline.setKeep([])                                     # fine again once the drag is over
        self.assertEqual(self.timeline.keep, [])

    def test_releasing_snaps_a_part_that_reaches_the_start_of_the_video(self):
        self.drag(self.x(2), self.x(0.04))
        self.assertEqual(self.timeline.keep, [(0.0, 5.0), (7.0, 9.0)])
        self.assertEqual(self.keeps[-1], [(0.0, 5.0), (7.0, 9.0)])
        self.assertEqual(self.keep_done, [True])

    def test_releasing_snaps_a_part_that_reaches_the_end_of_the_video(self):
        self.drag(self.x(9), self.x(9.97))
        self.assertEqual(self.timeline.keep, [(2.0, 5.0), (7.0, 10.0)])
        self.assertEqual(self.keeps[-1], [(2.0, 5.0), (7.0, 10.0)])

    def test_dragging_without_changing_anything_still_finishes_the_edit(self):
        self.drag(self.x(5), self.x(5))
        self.assertEqual(self.keeps, [])
        self.assertEqual(self.keep_done, [True])

    def test_right_click_does_not_start_a_drag(self):
        self.press(self.x(5), Qt.MouseButton.RightButton)
        self.move(self.x(6))
        self.release(self.x(6), Qt.MouseButton.RightButton)
        self.assertEqual((self.keeps, self.seeks), ([], []))

    def test_context_menu_reports_the_time_under_the_pointer(self):
        requests = []
        self.timeline.menuRequested.connect(lambda t, pos: requests.append((t, pos)))
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(self.x(6), 60), QPoint(300, 400))
        QApplication.sendEvent(self.timeline, event)
        self.assertEqual(len(requests), 1)
        self.assertAlmostEqual(requests[0][0], 6.0, delta=0.05)
        self.assertEqual(requests[0][1], QPoint(300, 400))

    def test_no_context_menu_without_a_video(self):
        requests = []
        self.timeline.menuRequested.connect(lambda t, pos: requests.append(t))
        self.timeline.clear()
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(100, 60), QPoint(100, 60))
        QApplication.sendEvent(self.timeline, event)
        self.assertEqual(requests, [])

    def test_clear_forgets_the_cuts(self):
        self.timeline.clear()
        self.assertEqual(self.timeline.keep, [])


class PlayerTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.player import Player
        self.player = Player()
        self.player.set_segments([(1.0, 3.0), (5.0, 8.0)])

    def test_inside_a_kept_part_playback_carries_on(self):
        self.assertEqual(self.player.next_play_position(2.0), 2.0)
        self.assertEqual(self.player.next_play_position(6.5), 6.5)

    def test_before_the_first_part_it_jumps_to_its_start(self):
        self.assertEqual(self.player.next_play_position(0.2), 1.0)
        self.assertEqual(self.player.next_play_position(0.0), 1.0)

    def test_a_hair_before_a_part_counts_as_inside_it(self):
        self.assertEqual(self.player.next_play_position(0.97), 1.0)

    def test_inside_a_cut_it_jumps_to_the_next_part(self):
        self.assertEqual(self.player.next_play_position(3.5), 5.0)
        self.assertEqual(self.player.next_play_position(4.9), 5.0)

    def test_at_the_end_of_a_part_it_moves_on_to_the_next_one(self):
        self.assertEqual(self.player.next_play_position(2.98), 5.0)

    def test_after_the_last_part_it_stops(self):
        self.assertIsNone(self.player.next_play_position(7.98))
        self.assertIsNone(self.player.next_play_position(9.0))

    def test_without_segments_playback_is_unrestricted(self):
        self.player.set_segments([])
        for t in (0.0, 4.2, 100.0):
            self.assertEqual(self.player.next_play_position(t), t)

    def test_an_open_ended_part_never_stops(self):
        self.player.set_segments([(2.0, None)])
        self.assertEqual(self.player.next_play_position(500.0), 500.0)
        self.assertEqual(self.player.next_play_position(0.5), 2.0)

    def test_segments_are_copied(self):
        segments = [(1.0, 2.0)]
        self.player.set_segments(segments)
        segments.append((4.0, 5.0))
        self.assertEqual(self.player.segments, [(1.0, 2.0)])

    def test_playback_rate_follows_the_speed(self):
        self.player.set_rate(2.0)
        self.assertEqual(self.player.media.playbackRate(), 2.0)
        self.player.set_rate(0.5)
        self.assertEqual(self.player.media.playbackRate(), 0.5)


class OrientationTests(unittest.TestCase):
    def image(self):
        # 2 x 1: red on the left, blue on the right
        img = QImage(2, 1, QImage.Format.Format_RGB32)
        img.setPixelColor(0, 0, QColor("red"))
        img.setPixelColor(1, 0, QColor("blue"))
        return img

    def test_rotate_clockwise_then_mirror(self):
        from video_converter.ui.player import orient_image
        turned = orient_image(self.image(), 90, False)
        self.assertEqual((turned.width(), turned.height()), (1, 2))
        self.assertEqual(turned.pixelColor(0, 0), QColor("red"))     # left edge goes to the top
        upside_down = orient_image(self.image(), 180, False)
        self.assertEqual(upside_down.pixelColor(0, 0), QColor("blue"))
        mirrored = orient_image(self.image(), 0, True)
        self.assertEqual(mirrored.pixelColor(0, 0), QColor("blue"))
        self.assertEqual(orient_image(self.image(), 360, False).pixelColor(0, 0), QColor("red"))


class VideoViewTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.player import VideoView
        self.view = VideoView()
        self.view.resize(416, 250)
        image = QImage(1280, 720, QImage.Format.Format_RGB32)
        image.fill(QColor("gray"))
        self.view.set_frame(image)
        # 9:16 crop of a 1280x720 frame, centered
        self.view.set_crop((1280, 720, 404, 720, 438, 0), 1.0)
        self.view.show()
        self.moves, self.zooms = [], []
        self.view.cropMoved.connect(lambda x, y: self.moves.append((x, y)))
        self.view.zoomChanged.connect(self.zooms.append)

    def tearDown(self):
        self.view.close()
        shiboken6.delete(self.view)     # free it now: every widget left alive slows each later theme change

    def test_dragging_the_box_moves_the_crop(self):
        center = self.view.crop_rect().center().toPoint()
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        QTest.mouseMove(self.view, center + QPoint(60, 0))
        QTest.mouseRelease(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           center + QPoint(60, 0))
        self.assertTrue(self.moves)
        self.assertGreater(self.moves[-1][0], 0.5)

    def test_dragging_far_left_clamps_to_the_edge(self):
        center = self.view.crop_rect().center().toPoint()
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        QTest.mouseMove(self.view, center - QPoint(1000, 0))
        self.assertEqual(self.moves[-1][0], 0.0)

    def test_clicking_outside_the_box_moves_it_there(self):
        frame = self.view.frame_rect()
        QTest.mouseClick(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(int(frame.right()) - 3, int(frame.center().y())))
        self.assertEqual(self.moves[-1][0], 1.0)

    def test_dragging_a_corner_inward_zooms_in(self):
        corner = self.view.crop_rect().topRight().toPoint()
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, corner)
        QTest.mouseMove(self.view, corner - QPoint(25, 0))
        self.assertTrue(self.zooms)
        self.assertGreater(self.zooms[-1], 1.0)

    def test_no_crop_interaction_in_after_mode(self):
        self.view.set_after(None, True)
        center = self.view.crop_rect().center().toPoint()
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        QTest.mouseMove(self.view, center + QPoint(60, 0))
        self.assertEqual(self.moves, [])


class InspectorTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.inspector import Inspector
        self.inspector = Inspector()
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.edits = []
        self.inspector.settingsEdited.connect(lambda s, label, final: self.edits.append((s, label, final)))

    def tearDown(self):
        self.inspector.close()
        shiboken6.delete(self.inspector)     # free it now: every widget left alive slows each later theme change

    def test_showing_settings_does_not_report_edits(self):
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.assertEqual(self.edits, [])

    def test_choosing_a_shape(self):
        self.inspector.shape_group.button(2).click()          # 1:1
        settings = self.edits[-1][0]
        self.assertEqual(settings.edit.aspect, (1, 1))

    def test_switching_to_mp3_hides_video_only_sections(self):
        self.inspector.output_group.button(1).click()
        settings = self.edits[-1][0]
        self.assertEqual(settings.output, OUTPUT_AUDIO)
        self.inspector.set_settings(settings, 10.0)
        self.assertTrue(self.inspector.frame_section.isHidden())
        self.assertTrue(self.inspector.quality_section.isHidden())
        self.assertFalse(self.inspector.mp3_box.isHidden())

    def type_trim(self, widget, text):
        widget.setText(text)
        widget.editingFinished.emit()

    def test_typed_trim_times(self):
        self.type_trim(self.inspector.start_edit, "0:02.5")
        self.assertEqual(self.edits[-1][0].edit.start, 2.5)

    def test_leaving_the_trim_fields_untouched_keeps_the_exact_times(self):
        settings = ClipSettings()
        settings.edit.start, settings.edit.end = 4.234, 8.766       # the fields show 0:04.2 and 0:08.8
        self.inspector.set_settings(settings, 10.0)
        for widget in (self.inspector.start_edit, self.inspector.end_edit):
            widget.editingFinished.emit()
        self.assertEqual(self.edits, [])

    def test_bad_trim_times_are_rejected_with_a_message(self):
        for text in ("abc", "0:20"):                          # not a time; past the 10 s end
            self.type_trim(self.inspector.start_edit, text)
            self.assertEqual(self.edits, [])
            self.assertFalse(self.inspector.trim_error.isHidden())
        settings = ClipSettings()
        settings.edit.start = 5.0
        self.inspector.set_settings(settings, 10.0)
        self.type_trim(self.inspector.end_edit, "4")          # end before start
        self.assertEqual(self.edits, [])

    def test_builtin_preset(self):
        index = self.inspector.preset_box.findText("Square post (1:1)")
        self.inspector.preset_box.setCurrentIndex(index)
        settings, label, _ = self.edits[-1]
        self.assertEqual(settings.edit.aspect, (1, 1))
        self.assertIn("Square", label)
        self.assertEqual(self.inspector.preset_box.currentIndex(), 0)   # ready for the next pick

    def test_zoom_slider_drag_reports_one_final_edit(self):
        slider = self.inspector.zoom_slider
        slider.setSliderDown(True)
        slider.setValue(150)
        slider.setValue(180)
        slider.setSliderDown(False)
        slider.sliderReleased.emit()
        finals = [final for _, _, final in self.edits]
        self.assertEqual(finals[-1], True)
        self.assertNotIn(True, finals[:-1])
        self.assertAlmostEqual(self.edits[-1][0].edit.zoom, 1.8)

    def test_speed_box_sets_the_speed(self):
        self.inspector.speed_box.setValue(2.0)
        settings, label, final = self.edits[-1]
        self.assertEqual((settings.edit.speed, label, final), (2.0, "Speed", True))
        self.inspector.speed_box.setValue(1.37)
        self.assertEqual(self.edits[-1][0].edit.speed, 1.37)

    def test_speed_box_stays_within_the_supported_range(self):
        self.inspector.speed_box.setValue(0.1)
        self.assertEqual(self.edits[-1][0].edit.speed, 0.25)
        self.inspector.speed_box.setValue(9.0)
        self.assertEqual(self.edits[-1][0].edit.speed, 4.0)

    def test_speed_chips(self):
        chips = {b.text(): b for b in self.inspector.speed_section.findChildren(QPushButton)}
        self.assertEqual(set(chips), {"0.5×", "1×", "2×"})
        chips["2×"].click()
        self.assertEqual(self.edits[-1][0].edit.speed, 2.0)
        self.assertEqual(self.inspector.speed_box.value(), 2.0)
        chips["0.5×"].click()
        self.assertEqual(self.edits[-1][0].edit.speed, 0.5)
        chips["1×"].click()
        self.assertEqual(self.edits[-1][0].edit.speed, 1.0)
        self.assertEqual(len(self.edits), 3)

    def test_showing_a_speed_does_not_report_an_edit(self):
        settings = ClipSettings(edit=VideoEdit(speed=3.0, reverse=True, fade_in=1.5, audio_fade_out=2.0))
        self.inspector.set_settings(settings, 10.0)
        self.assertEqual(self.edits, [])
        self.assertEqual(self.inspector.speed_box.value(), 3.0)
        self.assertTrue(self.inspector.reverse_box.isChecked())
        self.assertEqual(self.inspector.fade_boxes["fade_in"].value(), 1.5)
        self.assertEqual(self.inspector.fade_boxes["audio_fade_out"].value(), 2.0)
        self.assertEqual(self.inspector.fade_boxes["fade_out"].value(), 0.0)

    def test_reverse_box(self):
        self.inspector.reverse_box.click()
        settings, label, _ = self.edits[-1]
        self.assertTrue(settings.edit.reverse)
        self.assertEqual(label, "Reverse")
        self.inspector.reverse_box.click()
        self.assertFalse(self.edits[-1][0].edit.reverse)

    def test_each_fade_box_sets_only_its_own_field(self):
        names = ("fade_in", "fade_out", "audio_fade_in", "audio_fade_out")
        self.assertEqual(set(self.inspector.fade_boxes), set(names))
        for name in names:
            with self.subTest(field=name):
                self.inspector.set_settings(ClipSettings(), 10.0)
                self.inspector.fade_boxes[name].setValue(1.5)
                edit = self.edits[-1][0].edit
                self.assertEqual(getattr(edit, name), 1.5)
                for other in names:
                    if other != name:
                        self.assertEqual(getattr(edit, other), 0.0)

    def test_a_fade_box_at_zero_reads_off(self):
        box = self.inspector.fade_boxes["fade_in"]
        self.assertEqual(box.specialValueText(), "Off")
        box.setValue(2.0)
        box.setValue(0.0)
        self.assertEqual(self.edits[-1][0].edit.fade_in, 0.0)

    def test_fade_length_is_limited(self):
        self.inspector.fade_boxes["fade_out"].setValue(100.0)
        self.assertEqual(self.edits[-1][0].edit.fade_out, 30.0)

    def test_the_picture_fade_row_is_hidden_for_mp3_output(self):
        label, holder = self.inspector.fade_rows["Picture"]
        self.assertFalse(label.isHidden() or holder.isHidden())
        self.inspector.set_settings(ClipSettings(output=OUTPUT_AUDIO), 10.0)
        self.assertTrue(label.isHidden() and holder.isHidden())
        sound_label, sound_holder = self.inspector.fade_rows["Sound"]
        self.assertFalse(sound_label.isHidden() or sound_holder.isHidden())
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.assertFalse(label.isHidden() or holder.isHidden())

    def test_split_and_remove_buttons_ask_for_a_cut_at_the_playhead(self):
        requests = []
        self.inspector.cutRequested.connect(requests.append)
        self.inspector.split_button.click()
        self.inspector.remove_part_button.click()
        self.inspector.split_button.click()
        self.assertEqual(requests, ["split", "remove", "split"])
        self.assertEqual(self.edits, [])                      # the window decides what the cut does

    def test_restore_button_is_off_without_cuts_and_clears_them(self):
        self.assertFalse(self.inspector.restore_button.isEnabled())
        settings = ClipSettings(edit=VideoEdit(keep=[(0.0, 3.0), (6.0, 10.0)]))
        self.inspector.set_settings(settings, 10.0)
        self.assertTrue(self.inspector.restore_button.isEnabled())
        self.inspector.restore_button.click()
        new, label, _ = self.edits[-1]
        self.assertEqual((new.edit.keep, label), ([], "Restore cuts"))

    def test_restore_button_also_puts_back_plain_splits(self):
        self.inspector.set_settings(ClipSettings(edit=VideoEdit(keep=[(0.0, 3.0), (3.0, 10.0)])), 10.0)
        self.assertTrue(self.inspector.restore_button.isEnabled())

    def test_trim_info_shows_the_length_of_the_result(self):
        self.assertEqual(self.inspector.trim_info.text(), "Result 0:10.0 of 0:10.0")
        cut = ClipSettings(edit=VideoEdit(keep=[(0.0, 3.0), (6.0, 10.0)]))
        self.inspector.set_settings(cut, 10.0)
        self.assertEqual(self.inspector.trim_info.text(), "Result 0:07.0 of 0:10.0")
        cut.edit.speed = 2.0
        self.inspector.set_settings(cut, 10.0)
        self.assertEqual(self.inspector.trim_info.text(), "Result 0:03.5 of 0:10.0")

    def test_trim_info_counts_the_trim_and_the_cuts_together(self):
        settings = ClipSettings(edit=VideoEdit(keep=[(0.0, 3.0), (6.0, 10.0)], start=2.0, end=8.0))
        self.inspector.set_settings(settings, 10.0)
        self.assertEqual(self.inspector.trim_info.text(), "Result 0:03.0 of 0:10.0")       # 2..3 and 6..8

    def test_trim_info_is_empty_for_an_unknown_length(self):
        self.inspector.set_settings(ClipSettings(), None)
        self.assertEqual(self.inspector.trim_info.text(), "")

    def test_cut_info_explains_what_to_do_then_lists_the_cuts(self):
        self.assertIn("Split", self.inspector.cut_info.text())
        self.inspector.set_settings(ClipSettings(edit=VideoEdit(keep=[(0.0, 3.0), (3.0, 10.0)])), 10.0)
        self.assertIn("Remove part", self.inspector.cut_info.text())
        self.inspector.set_settings(ClipSettings(edit=VideoEdit(keep=[(0.0, 3.0), (6.0, 10.0)])), 10.0)
        self.assertEqual(self.inspector.cut_info.text(), "Cut out: 0:03.0–0:06.0")

    def test_cut_info_lists_the_leading_and_trailing_cuts_and_shortens_long_lists(self):
        self.inspector.set_settings(ClipSettings(edit=VideoEdit(keep=[(2.0, 8.0)])), 10.0)
        self.assertEqual(self.inspector.cut_info.text(), "Cut out: 0:00.0–0:02.0, 0:08.0–0:10.0")
        keep = [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0), (7.0, 8.0), (9.0, 10.0), (11.0, 12.0)]
        self.inspector.set_settings(ClipSettings(edit=VideoEdit(keep=keep)), 14.0)
        text = self.inspector.cut_info.text()
        self.assertTrue(text.startswith("Cut out: 0:00.0–0:01.0, 0:02.0–0:03.0"), text)
        self.assertTrue(text.endswith("and 3 more"), text)



class ReverseWarningInspectorTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.inspector import Inspector
        self.inspector = Inspector()
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.addCleanup(shiboken6.delete, self.inspector)     # runs last: free it, so widgets don't pile up
        self.addCleanup(self.inspector.close)

    def test_the_warning_sits_with_the_reverse_control_and_hides_again(self):
        self.assertTrue(self.inspector.reverse_warning.isHidden())
        self.inspector.set_reverse_warning("Reversing this needs about 6.4 GB of memory.")
        self.assertFalse(self.inspector.reverse_warning.isHidden())
        self.assertEqual(self.inspector.reverse_warning.text(), "Reversing this needs about 6.4 GB of memory.")
        self.assertIs(self.inspector.reverse_warning.parentWidget(), self.inspector.reverse_box.parentWidget())
        self.inspector.set_reverse_warning("")
        self.assertTrue(self.inspector.reverse_warning.isHidden())

    def test_the_size_menu_explains_same_as_source(self):
        tip = self.inspector.size_box.toolTip()
        self.assertIn("capped at 1080", tip)
        self.assertIn("1080", tip)


class FrameRateInspectorTests(unittest.TestCase):
    PHONE = "Play on phone (keeps the whole picture)"

    def setUp(self):
        from video_converter.ui.inspector import Inspector
        self.inspector = Inspector()
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.edits = []
        self.inspector.settingsEdited.connect(lambda s, label, final: self.edits.append((s, label, final)))
        self.addCleanup(shiboken6.delete, self.inspector)     # runs last: free it, so widgets don't pile up
        self.addCleanup(self.inspector.close)

    def test_the_combo_offers_the_three_choices_under_size(self):
        box = self.inspector.rate_box
        self.assertEqual([box.itemText(i) for i in range(box.count())],
                         ["Same as source", "Steady 60 fps", "Steady 30 fps"])
        self.assertEqual(box.currentData(), None)
        self.assertIs(box.parentWidget(), self.inspector.size_box.parentWidget())

    def test_choosing_a_rate_edits_the_settings(self):
        self.inspector.rate_box.setCurrentIndex(1)
        settings, label, final = self.edits[-1]
        self.assertEqual((settings.edit.frame_rate, label, final), (60, "Frame rate", True))
        self.inspector.rate_box.setCurrentIndex(2)
        self.assertEqual(self.edits[-1][0].edit.frame_rate, 30)
        self.inspector.rate_box.setCurrentIndex(0)
        self.assertIsNone(self.edits[-1][0].edit.frame_rate)

    def test_showing_settings_selects_the_rate_without_reporting(self):
        settings = ClipSettings()
        settings.edit.frame_rate = 30
        self.inspector.set_settings(settings, 10.0)
        self.assertEqual(self.inspector.rate_box.currentData(), 30)
        self.assertEqual(self.edits, [])

    def test_the_hint_shows_and_hides(self):
        self.assertTrue(self.inspector.rate_hint.isHidden())
        self.inspector.set_frame_rate_hint("Choose Steady 60 fps.")
        self.assertFalse(self.inspector.rate_hint.isHidden())
        self.assertEqual(self.inspector.rate_hint.text(), "Choose Steady 60 fps.")
        self.assertIs(self.inspector.rate_hint.parentWidget(), self.inspector.rate_box.parentWidget())
        self.inspector.set_frame_rate_hint("")
        self.assertTrue(self.inspector.rate_hint.isHidden())

    def test_up_to_1080p_is_a_size_choice(self):
        box = self.inspector.size_box
        self.assertEqual([box.itemText(i) for i in range(box.count())][:2], ["Same as source", "Up to 1080p"])
        box.setCurrentIndex(1)
        self.assertEqual(self.edits[-1][0].edit.resolution, UP_TO_1080)

    def test_the_play_on_phone_preset_is_first_and_sets_everything(self):
        box = self.inspector.preset_box
        self.assertEqual(box.itemText(1), self.PHONE)
        settings = ClipSettings(edit=VideoEdit(aspect=(9, 16), mute=True, crf=28))
        self.inspector.set_settings(settings, 10.0)
        box.setCurrentIndex(1)
        new, label, _ = self.edits[-1]
        self.assertEqual(label, f"Preset: {self.PHONE}")
        edit = new.edit
        self.assertEqual((new.output, edit.aspect, edit.resolution, edit.frame_rate, edit.crf, edit.mute),
                         ("video", None, UP_TO_1080, 60, 18, False))
        self.assertEqual(self.inspector.preset_box.currentIndex(), 0)


class AdjustSectionTests(unittest.TestCase):
    def setUp(self):
        from video_converter.ui.inspector import Inspector
        self.inspector = Inspector()
        self.inspector.set_settings(ClipSettings(), 10.0)
        self.edits = []
        self.inspector.settingsEdited.connect(lambda s, label, final: self.edits.append((s, label, final)))

    def tearDown(self):
        self.inspector.close()
        shiboken6.delete(self.inspector)     # free it now: every widget left alive slows each later theme change

    def test_a_slider_drag_reports_one_final_edit(self):
        slider, value_label = self.inspector.adjust_sliders["contrast"]
        slider.setSliderDown(True)
        slider.setValue(120)
        slider.setValue(150)
        slider.setSliderDown(False)
        slider.sliderReleased.emit()
        self.assertEqual([final for _, _, final in self.edits][-1], True)
        self.assertNotIn(True, [final for _, _, final in self.edits][:-1])
        self.assertAlmostEqual(self.edits[-1][0].edit.contrast, 1.5)
        self.inspector.set_settings(self.edits[-1][0], 10.0)
        self.assertEqual(value_label.text(), "1.50")

    def test_every_slider_sets_its_own_field(self):
        from video_converter.video import ADJUSTMENTS
        for name, (low, high, neutral) in ADJUSTMENTS.items():
            with self.subTest(field=name):
                self.inspector.set_settings(ClipSettings(), 10.0)
                slider, _ = self.inspector.adjust_sliders[name]
                self.assertEqual((slider.minimum(), slider.maximum()), (round(low * 100), round(high * 100)))
                self.assertEqual(slider.value(), round(neutral * 100))
                slider.setValue(slider.maximum())
                edit = self.edits[-1][0].edit
                self.assertAlmostEqual(getattr(edit, name), high)
                others = {n: getattr(edit, n) for n in ADJUSTMENTS if n != name}
                self.assertEqual(others, {n: v[2] for n, v in ADJUSTMENTS.items() if n != name})

    def test_look_chips(self):
        from video_converter.video import LOOKS
        keys = list(LOOKS)
        self.inspector.look_group.button(keys.index("bw")).click()
        self.assertEqual(self.edits[-1][0].edit.look, "bw")
        self.assertEqual(self.inspector.look_group.button(keys.index("bw")).toolTip(), "Black and white")
        settings = ClipSettings()
        settings.edit.look = "vintage"
        self.inspector.set_settings(settings, 10.0)
        self.assertTrue(self.inspector.look_group.button(keys.index("vintage")).isChecked())

    def test_reset_puts_the_picture_back_but_keeps_stabilizing(self):
        settings = ClipSettings()
        settings.edit.look, settings.edit.gamma, settings.edit.denoise = "warm", 1.5, 3.0
        settings.edit.stabilize = "strong"
        self.inspector.set_settings(settings, 10.0)
        reset = next(b for b in self.inspector.adjust_section.findChildren(QPushButton)
                     if b.text() == "Reset the picture")
        reset.click()
        edit = self.edits[-1][0].edit
        self.assertEqual((edit.look, edit.gamma, edit.denoise, edit.stabilize), ("none", 1.0, 0.0, "strong"))

    def test_stabilize_choice(self):
        box = self.inspector.stabilize_box
        box.setCurrentIndex(box.findData("light"))
        self.assertEqual(self.edits[-1][0].edit.stabilize, "light")

    def test_the_section_is_hidden_for_mp3(self):
        self.assertFalse(self.inspector.adjust_section.isHidden())
        self.inspector.set_settings(ClipSettings(output=OUTPUT_AUDIO), 10.0)
        self.assertTrue(self.inspector.adjust_section.isHidden())


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.landscape = cls.tmp / "landscape.mp4"
        cls.portrait = cls.tmp / "portrait.mp4"
        make_video(cls.landscape)
        make_video(cls.portrait, rotation=-90)
        (cls.tmp / "more").mkdir()
        cls.third = cls.tmp / "more" / "third.mp4"          # 2 s, in a folder of its own
        make_video(cls.third, seconds=2)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        from video_converter.ui.main_window import MainWindow
        self.ini = self.tmp / f"settings-{self.id().split('.')[-1]}.ini"
        self.window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        self.window.resize(1200, 800)
        self.window.show()

    def tearDown(self):
        from video_converter.ui.main_window import MainWindow
        tmpdir = self.window.tmpdir
        self.window.close()
        self.window.loader.shutdown(wait=True)      # a load cut short by closing clears up after itself
        self.assertFalse(tmpdir.exists(), "the window left its temp folder behind")
        for earlier in QApplication.topLevelWidgets():      # the first window of a test that "restarts" the app
            if isinstance(earlier, MainWindow) and earlier is not self.window:
                earlier.close()
                earlier.loader.shutdown(wait=True)
                earlier.deleteLater()
        # unittest keeps every test case alive until the run ends, so free the window (and its
        # media player) now instead of letting closed windows pile up and slow later tests.
        self.window.deleteLater()
        self.window = None
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump(0.1)

    def load(self, *paths):
        self.window.add_paths(list(paths))
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)), "files never loaded")

    def test_adding_files_and_folders_skips_duplicates(self):
        self.load(self.landscape)
        self.assertEqual(self.window.add_paths([self.landscape]), 0)
        self.window.add_paths([self.tmp])                     # the folder holds both videos
        self.assertEqual(len(self.window.items), 2)
        item = self.window.items[0]
        self.assertEqual((item.info.width, item.info.height), (320, 180))
        self.assertTrue(item.thumbs)
        self.assertTrue(item.waveform)

    def test_rotated_phone_video_is_previewed_upright(self):
        self.load(self.portrait)
        self.assertTrue(pump(TIMEOUT, lambda: self.window.view.raw is not None), "no frame decoded")
        image = self.window.view.oriented_image()
        self.assertLess(image.width(), image.height())

    def test_edits_can_be_undone_and_redone(self):
        self.load(self.landscape)
        item = self.window.current
        self.window.inspector.shape_group.button(2).click()   # 1:1
        self.assertEqual(item.settings.edit.aspect, (1, 1))
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.aspect, (9, 16))
        self.assertTrue(self.window.inspector.shape_group.button(1).isChecked())  # the panel follows
        self.window.undo_stack.redo()
        self.assertEqual(item.settings.edit.aspect, (1, 1))

    def test_an_adjustment_drag_is_one_undo_step_and_reaches_the_preview(self):
        self.load(self.landscape)
        item = self.window.current
        slider, _ = self.window.inspector.adjust_sliders["brightness"]
        slider.setSliderDown(True)
        for value in (10, 20, 30):
            slider.setValue(value)
        slider.setSliderDown(False)
        slider.sliderReleased.emit()
        self.assertAlmostEqual(item.settings.edit.brightness, 0.3)
        self.assertEqual(self.window.undo_stack.count(), 1)
        self.assertIn("adjusted", item.list_item.text())
        self.window.compare_button.click()                     # the "after" view renders the adjustment
        self.assertTrue(pump(TIMEOUT, lambda: self.window.view.after_image is not None), "no preview")
        self.window.compare_button.click()
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.brightness, 0.0)
        self.assertEqual(slider.value(), 0)

    def test_a_crop_drag_is_one_undo_step(self):
        self.load(self.landscape)
        item = self.window.current
        for x in (0.4, 0.3, 0.2, 0.1):
            self.window._crop_moved(x, 0.5)
        self.window._edit_finished()
        self.window._crop_moved(0.9, 0.5)                      # a second, separate drag
        self.window._edit_finished()
        self.assertEqual(self.window.undo_stack.count(), 2)
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.pos_x, 0.1)
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.pos_x, 0.5)

    def test_apply_to_all_keeps_each_files_framing(self):
        self.load(self.landscape, self.portrait)
        first, second = self.window.items
        second.settings.edit.pos_x, second.settings.edit.start = 0.9, 1.0
        self.window.media_list.setCurrentRow(0)
        self.window.inspector.shape_group.button(2).click()   # 1:1 on the first file
        self.window.apply_to_all()
        self.assertEqual(second.settings.edit.aspect, (1, 1))
        self.assertEqual((second.settings.edit.pos_x, second.settings.edit.start), (0.9, 1.0))
        self.window.undo_stack.undo()                          # one step undoes the whole batch
        self.assertEqual(second.settings.edit.aspect, (9, 16))

    def test_trim_at_playhead_refuses_an_end_before_the_start(self):
        self.load(self.landscape)
        item = self.window.current
        item.settings.edit.start = 2.0
        self.window.player.seek(1.0)
        pump(2, lambda: abs(self.window.player.position - 1.0) < 0.05)
        self.window.trim_at_playhead("end")
        self.assertIsNone(item.settings.edit.end)

    def test_export_video_and_mp3(self):
        self.load(self.landscape)
        out_dir = self.tmp / "exports"
        out_dir.mkdir(exist_ok=True)
        self.window._set_output_folder(out_dir)
        self.window.export_current()
        audio = self.window.current.settings.copy()
        audio.output = OUTPUT_AUDIO
        self.window.push_settings(audio, "Output")
        self.window.export_current()
        self.assertTrue(pump(60, lambda: not self.window.queue.busy), "exports never finished")
        outputs = sorted(p.name for p in out_dir.iterdir())
        self.assertEqual(outputs, ["landscape (phone).mp4", "landscape.mp3"])
        info = probe_media(FFMPEG, out_dir / "landscape (phone).mp4")
        self.assertLess(info.width, info.height)

    def test_preferences_survive_a_restart(self):
        from video_converter.ui.main_window import MainWindow
        self.load(self.landscape)
        self.window._set_output_folder(self.tmp)
        self.window.user_presets["Mine"] = ClipSettings().to_dict()
        first_tmpdir = self.window.tmpdir
        self.window.close()
        self.window.loader.shutdown(wait=True)
        self.assertFalse(first_tmpdir.exists())
        pump(0.1)
        self.window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        self.assertEqual(self.window.output_dir, self.tmp)
        self.assertIn("Mine", self.window.user_presets)
        self.assertGreaterEqual(self.window.inspector.preset_box.findText("★ Mine"), 0)

    def test_corrupt_saved_presets_do_not_break_startup(self):
        from video_converter.ui.main_window import MainWindow
        store = QSettings(str(self.ini), QSettings.Format.IniFormat)
        store.setValue("presets", "{not json")
        store.sync()
        self.window.close()
        self.window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        self.assertEqual(self.window.user_presets, {})

    def test_junk_in_the_saved_settings_does_not_stop_startup(self):
        from video_converter.ui.main_window import MainWindow
        self.window.close()
        ini = self.tmp / "junk.ini"
        ini.write_text("[General]\ngeometry=not a window layout\nsplitTop=3, 4\nsplitMain=@Invalid()\n"
                       "outputDir=C:/a, C:/b\nlastDir=x, y\ntheme=neon\npresets=[1, 2]\n", encoding="utf-8")
        self.window = MainWindow(FFMPEG, QSettings(str(ini), QSettings.Format.IniFormat))
        self.assertIsNone(self.window.output_dir)
        self.assertEqual(self.window.user_presets, {})
        self.assertIn(self.window.theme_mode, ("dark", "light"))
        with mock.patch("video_converter.ui.main_window.QFileDialog.getOpenFileNames",
                        return_value=([], "")) as dialog:
            self.window.choose_files()
        self.assertEqual(dialog.call_args.args[2], "")           # the bad "last folder" was ignored
        self.window.close()                                      # and the window can save over the junk
        self.window = MainWindow(FFMPEG, QSettings(str(ini), QSettings.Format.IniFormat))
        self.assertEqual(self.window.user_presets, {})

    def test_a_saved_layout_is_still_restored(self):
        from PySide6.QtCore import QByteArray
        from PySide6.QtWidgets import QSplitter
        from video_converter.ui.main_window import MainWindow
        self.window.close()
        self.window.loader.shutdown(wait=True)
        saved = QSettings(str(self.ini), QSettings.Format.IniFormat)
        geometry = saved.value("geometry")
        self.assertIsInstance(geometry, QByteArray)
        with mock.patch.object(MainWindow, "restoreGeometry", return_value=True) as window_layout:
            with mock.patch.object(QSplitter, "restoreState", return_value=True) as split_layouts:
                self.window = MainWindow(FFMPEG, QSettings(str(self.ini), QSettings.Format.IniFormat))
        self.assertEqual(window_layout.call_args.args[0], geometry)
        self.assertEqual(split_layouts.call_count, 2)

    def test_closing_removes_the_temp_folder(self):
        folder = self.window.tmpdir
        self.assertTrue(folder.is_dir())
        self.window.close()
        self.assertFalse(folder.exists())

    def test_closing_while_files_are_still_loading_leaves_no_folder(self):
        # The background loaders make their thumbnail folders inside the temp folder, possibly after
        # the window has already removed it.
        self.window.add_paths([self.landscape, self.portrait, self.third])
        folder = self.window.tmpdir
        self.window.close()
        self.window.loader.shutdown(wait=True)
        self.assertFalse(folder.exists())

    def test_audio_files_are_added_alone_and_from_folders(self):
        folder = self.tmp / "mixed"
        folder.mkdir(exist_ok=True)
        (folder / "video.mp4").write_bytes(self.third.read_bytes())
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=duration=1", str(folder / "song.mp3")], check=True)
        for name in ("notes.txt", "photo.jpg", "cover.png"):
            (folder / name).write_text("not media")
        self.assertEqual(self.window.add_paths([folder]), 2)
        self.assertEqual(sorted(item.path.name for item in self.window.items), ["song.mp3", "video.mp4"])
        self.assertTrue(pump(TIMEOUT, lambda: all(not i.loading for i in self.window.items)), "files never loaded")
        song = next(i for i in self.window.items if i.path.name == "song.mp3")
        self.assertFalse(song.info.has_video)
        self.assertEqual(song.settings.output, OUTPUT_AUDIO)

    def test_every_common_audio_type_is_in_the_file_dialog_filter(self):
        from video_converter.ui.main_window import FILE_FILTER
        head = FILE_FILTER.split(";;")[0]
        self.assertTrue(head.startswith("Videos and audio ("))
        for ext in (".mp4", ".mkv", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"):
            self.assertIn(f"*{ext}", head)
        with mock.patch("video_converter.ui.main_window.QFileDialog.getOpenFileNames",
                        return_value=([], "")) as dialog:
            self.window.choose_files()
        self.assertEqual(dialog.call_args.args[3], FILE_FILTER)

    def reverse_everything(self):
        """Make the open file a long one (about 50 GB to play backwards) and turn reverse on."""
        item = self.window.current
        item.info = MediaInfo(duration=600.0, width=1920, height=1080, has_audio=True, fps=30.0)
        new = item.settings.copy()
        new.edit.reverse = True
        self.window.push_settings(new, "Reverse")
        return item

    def test_a_reversed_file_that_needs_over_2_gb_shows_a_warning_next_to_the_control(self):
        self.load(self.landscape)
        label = self.window.inspector.reverse_warning
        self.assertTrue(label.isHidden())
        self.reverse_everything()
        self.assertFalse(label.isHidden())
        self.assertRegex(label.text(), r"^Reversing this needs about \d+\.\d GB of memory\. Shorten the clip")
        new = self.window.current.settings.copy()
        new.edit.reverse = False
        self.window.push_settings(new, "Reverse")
        self.assertTrue(label.isHidden())

    def test_a_small_reversed_clip_shows_no_warning(self):
        self.load(self.landscape)
        new = self.window.current.settings.copy()
        new.edit.reverse = True
        self.window.push_settings(new, "Reverse")
        self.assertTrue(self.window.inspector.reverse_warning.isHidden())

    def test_exporting_a_huge_reverse_asks_first_and_no_means_no(self):
        self.load(self.landscape)
        self.reverse_everything()
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as ask:
            self.window.export_current()
        ask.assert_called_once()
        self.assertIn("GB of memory", ask.call_args.args[2])
        self.assertEqual(self.window.queue.jobs, [])

    def test_exporting_a_huge_reverse_goes_ahead_on_yes(self):
        self.load(self.landscape)
        self.reverse_everything()
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as ask:
            self.window.export_current()
        ask.assert_called_once()
        self.assertEqual(len(self.window.queue.jobs), 1)
        self.stop_exports()

    def test_the_question_can_be_switched_off_and_is_not_asked_for_small_exports(self):
        self.load(self.landscape)
        with mock.patch.object(QMessageBox, "question") as ask:
            self.window.export_current()                                # nothing reversed
            self.reverse_everything()
            self.window.confirm_heavy_exports = False
            self.window.export_current()
        ask.assert_not_called()
        self.assertEqual(len(self.window.queue.jobs), 2)
        self.stop_exports()

    def test_export_all_asks_once_and_a_no_only_leaves_out_the_big_ones(self):
        self.load(self.landscape, self.third)
        self.reverse_everything()                                        # the first file only
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as ask:
            self.window.export_all()
        ask.assert_called_once()
        self.assertEqual([job.source.name for job in self.window.queue.jobs], ["third.mp4"])
        self.stop_exports()

    def test_joining_asks_about_a_huge_reversed_clip(self):
        self.load(self.landscape, self.third)
        self.reverse_everything()
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as ask:
            self.window.join_clips()
        ask.assert_called_once()
        self.assertEqual(self.window.queue.jobs, [])

    def test_keyboard_shortcuts_skip_text_fields(self):
        self.load(self.landscape)
        self.window.activateWindow()
        item = self.window.current
        field = self.window.inspector.start_edit
        field.setFocus()
        QTest.keyClick(field, Qt.Key.Key_I)                    # typing an "i" must not set the trim
        self.assertIsNone(item.settings.edit.start)
        self.assertEqual(field.text(), "i")

        # ...but the same key anywhere else marks the trim start at the playhead.
        self.window.player.seek(1.0)
        pump(2, lambda: abs(self.window.player.position - 1.0) < 0.05)
        self.window.media_list.setFocus()
        QTest.keyClick(self.window.media_list, Qt.Key.Key_I)
        self.assertAlmostEqual(item.settings.edit.start or 0.0, 1.0, delta=0.1)

    def stop_exports(self):
        # Closing the window asks a question while an export is running, so let them stop first.
        self.window.queue.cancel_all()
        self.assertTrue(pump(30, lambda: not self.window.queue.busy), "exports never stopped")

    def seek(self, seconds):
        self.window.player.seek(seconds)
        self.assertTrue(pump(5, lambda: abs(self.window.player.position - seconds) < 0.05), "seek never landed")

    def test_split_then_remove_at_the_playhead_are_one_undo_step_each(self):
        self.load(self.landscape)
        item, duration = self.window.current, self.window.current.info.duration
        self.seek(1.0)
        self.window.cut_at("split")
        keep = item.settings.edit.keep
        self.assertEqual(len(keep), 2)
        self.assertAlmostEqual(keep[0][1], 1.0, delta=0.05)
        self.assertEqual(keep[0][1], keep[1][0])                              # they touch: a split
        self.assertEqual((keep[0][0], keep[1][1]), (0.0, duration))
        self.assertEqual(self.window.undo_stack.count(), 1)
        self.assertEqual(self.window.timeline.keep, keep)
        self.assertTrue(self.window.inspector.restore_button.isEnabled())

        self.seek(2.0)
        self.window.cut_at("remove")
        self.assertEqual(item.settings.edit.keep, [keep[0]])
        self.assertEqual(self.window.undo_stack.count(), 2)
        self.assertEqual(self.window.player.segments, [keep[0]])               # playback skips the removed part
        self.assertIn("1 cut", item.list_item.text())                          # the list entry says so too
        self.assertIn("1 cut", item.list_item.toolTip())                       # in full, even when cut short

        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.keep, keep)
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.keep, [])
        self.assertFalse(self.window.inspector.restore_button.isEnabled())
        self.assertEqual(self.window.timeline.keep, [])
        self.window.undo_stack.redo()
        self.assertEqual(item.settings.edit.keep, keep)

    def test_cut_at_refuses_inside_a_cut_and_says_why(self):
        self.load(self.landscape)
        item, duration = self.window.current, self.window.current.info.duration
        self.window.push_settings(ClipSettings(edit=VideoEdit(keep=[(0.0, 1.0), (2.0, duration)])), "Cut")
        steps = self.window.undo_stack.count()
        self.window.cut_at("split", 1.5)
        self.assertEqual(item.settings.edit.keep, [(0.0, 1.0), (2.0, duration)])
        self.assertIn("cut out", self.window.statusBar().currentMessage())
        self.window.statusBar().clearMessage()
        self.window.cut_at("remove", 1.5)
        self.assertIn("already cut out", self.window.statusBar().currentMessage())
        self.assertEqual(self.window.undo_stack.count(), steps)

    def test_cut_at_refuses_to_split_at_the_edge_of_a_part(self):
        self.load(self.landscape)
        self.window.cut_at("split", 0.02)
        self.assertEqual(self.window.current.settings.edit.keep, [])
        self.assertIn("Too close", self.window.statusBar().currentMessage())
        self.assertEqual(self.window.undo_stack.count(), 0)

    def test_cut_at_refuses_to_remove_the_only_part(self):
        self.load(self.landscape)
        self.window.cut_at("remove", 1.0)
        self.assertEqual(self.window.current.settings.edit.keep, [])
        self.assertIn("Split the video first", self.window.statusBar().currentMessage())
        self.assertEqual(self.window.undo_stack.count(), 0)

    def test_cut_parts_can_be_put_back_one_by_one_or_all_at_once(self):
        self.load(self.landscape)
        item, duration = self.window.current, self.window.current.info.duration
        self.window.push_settings(ClipSettings(edit=VideoEdit(keep=[(0.0, 0.5), (1.0, 1.5), (2.0, duration)])), "Cut")
        self.window.cut_at("restore", 0.75)
        self.assertEqual(item.settings.edit.keep, [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (2.0, duration)])
        self.window.cut_at("restore_all")
        self.assertEqual(item.settings.edit.keep, [])

    def test_a_cut_drag_on_the_timeline_is_one_undo_step(self):
        self.load(self.landscape)
        item, duration = self.window.current, self.window.current.info.duration
        self.window.push_settings(ClipSettings(edit=VideoEdit(keep=[(0.5, 1.5), (2.0, duration)])), "Cut")
        steps = self.window.undo_stack.count()
        for end in (1.6, 1.7, 1.8):
            self.window._timeline_keep([(0.5, end), (2.0, duration)])
        self.window.timeline.keepEditFinished.emit()
        self.assertEqual(self.window.undo_stack.count(), steps + 1)
        self.assertEqual(item.settings.edit.keep, [(0.5, 1.8), (2.0, duration)])
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.keep, [(0.5, 1.5), (2.0, duration)])

    def test_the_player_follows_the_speed_and_the_cuts(self):
        self.load(self.landscape)
        duration = self.window.current.info.duration
        edit = VideoEdit(keep=[(0.0, 1.0), (2.0, duration)], speed=2.0, start=0.5)
        self.window.push_settings(ClipSettings(edit=edit), "Cut")
        self.assertEqual(self.window.player.segments, [(0.5, 1.0), (2.0, duration)])
        self.assertEqual(self.window.player.media.playbackRate(), 2.0)

    def test_s_and_x_keys_split_and_remove_at_the_playhead(self):
        self.load(self.landscape)
        self.window.activateWindow()
        item = self.window.current
        self.seek(1.0)
        self.window.media_list.setFocus()
        QTest.keyClick(self.window.media_list, Qt.Key.Key_S)
        self.assertEqual(len(item.settings.edit.keep), 2)
        self.assertAlmostEqual(item.settings.edit.keep[0][1], 1.0, delta=0.1)
        self.seek(2.0)
        QTest.keyClick(self.window.media_list, Qt.Key.Key_X)
        self.assertEqual(len(item.settings.edit.keep), 1)
        self.assertAlmostEqual(item.settings.edit.keep[0][1], 1.0, delta=0.1)

    def test_s_and_x_are_ordinary_keys_in_text_fields(self):
        self.load(self.landscape)
        self.window.activateWindow()
        field = self.window.inspector.start_edit
        field.setFocus()
        QTest.keyClick(field, Qt.Key.Key_S)
        QTest.keyClick(field, Qt.Key.Key_X)
        self.assertEqual(field.text(), "sx")
        self.assertEqual(self.window.current.settings.edit.keep, [])

    def test_the_split_and_cut_buttons_work_like_the_keys(self):
        self.load(self.landscape)
        self.seek(1.0)
        self.window.split_button.click()
        self.assertEqual(len(self.window.current.settings.edit.keep), 2)
        self.seek(2.0)
        self.window.inspector.remove_part_button.click()
        self.assertEqual(len(self.window.current.settings.edit.keep), 1)

    def names(self):
        return [item.path.name for item in self.window.items]

    def listed(self):
        return [self.window.media_list.item(row).text().split("\n")[0] for row in range(self.window.media_list.count())]

    def test_dragging_a_file_in_the_list_reorders_the_items(self):
        self.load(self.landscape, self.portrait, self.third)
        self.assertEqual(self.names(), ["landscape.mp4", "portrait.mp4", "third.mp4"])
        self.window.media_list.model().moveRow(QModelIndex(), 0, QModelIndex(), 3)      # first file to the end
        self.assertEqual(self.listed(), ["portrait.mp4", "third.mp4", "landscape.mp4"])
        self.assertEqual(self.names(), self.listed())
        self.window.media_list.model().moveRow(QModelIndex(), 2, QModelIndex(), 1)      # and back to the middle
        self.assertEqual(self.listed(), ["portrait.mp4", "landscape.mp4", "third.mp4"])
        self.assertEqual(self.names(), self.listed())

    def test_selection_still_maps_to_the_right_file_after_a_reorder(self):
        self.load(self.landscape, self.portrait, self.third)
        self.window.media_list.model().moveRow(QModelIndex(), 0, QModelIndex(), 3)
        for row, name in enumerate(self.listed()):
            self.window.media_list.setCurrentRow(row)
            self.assertEqual(self.window.current.path.name, name)
            self.assertIs(self.window.current, self.window.items[row])
        self.window.media_list.setCurrentRow(2)                 # landscape, now last
        self.window.remove_selected()
        self.assertEqual(self.names(), ["portrait.mp4", "third.mp4"])
        self.assertEqual(self.listed(), self.names())

    def test_reordering_keeps_each_files_settings(self):
        self.load(self.landscape, self.third)
        first, second = self.window.items
        fast = first.settings.copy()
        fast.edit.speed = 2.0
        self.window.apply_settings(first, fast)
        self.window.media_list.model().moveRow(QModelIndex(), 0, QModelIndex(), 2)
        self.assertEqual(self.window.items, [second, first])
        self.window.media_list.setCurrentRow(0)
        self.assertIs(self.window.current, second)
        self.assertEqual(self.window.inspector.speed_box.value(), 1.0)
        self.window.media_list.setCurrentRow(1)
        self.assertIs(self.window.current, first)
        self.assertEqual(self.window.inspector.speed_box.value(), 2.0)

    def test_joining_two_files_exports_one_file_as_long_as_both(self):
        self.load(self.landscape, self.portrait)
        out_dir = self.tmp / "joined"
        out_dir.mkdir(exist_ok=True)
        self.window._set_output_folder(out_dir)
        self.window.join_clips()
        self.assertTrue(pump(90, lambda: not self.window.queue.busy), "the join never finished")
        job = self.window.queue.jobs[-1]
        self.assertEqual(job.state.value, "Done", job.error)
        self.assertEqual([p.name for p in out_dir.iterdir()], ["landscape (joined).mp4"])
        info = probe_media(FFMPEG, out_dir / "landscape (joined).mp4")
        self.assertAlmostEqual(info.duration, 6.0, delta=0.3)
        # The first file's phone shape: 9:16 from the 320x180 source exports at 180x320.
        self.assertEqual((info.width, info.height), (180, 320))
        self.assertTrue(info.has_audio)
        self.assertEqual(job.title, "landscape.mp4 + 1 more")

    def test_joining_follows_the_list_order(self):
        self.load(self.landscape, self.portrait, self.third)
        self.window.media_list.model().moveRow(QModelIndex(), 2, QModelIndex(), 0)       # third to the top
        self.window.join_clips()
        job = self.window.queue.jobs[-1]
        self.assertEqual([c.source.name for c in job.clips], ["third.mp4", "landscape.mp4", "portrait.mp4"])
        self.assertEqual(job.source.name, "third.mp4")
        self.stop_exports()

    def test_joining_only_the_selected_files(self):
        self.load(self.landscape, self.portrait, self.third)
        self.window.media_list.clearSelection()
        self.window.media_list.item(0).setSelected(True)
        self.window.media_list.item(2).setSelected(True)
        self.window.join_clips()
        job = self.window.queue.jobs[-1]
        self.assertEqual([c.source.name for c in job.clips], ["landscape.mp4", "third.mp4"])
        self.stop_exports()

    def test_joining_uses_each_files_own_cuts_and_the_first_files_output(self):
        self.load(self.landscape, self.third)
        first, second = self.window.items
        first.settings.edit.speed = 2.0
        second.settings.edit.keep = [(0.0, 0.5), (1.0, 2.0)]
        self.window.join_clips()
        job = self.window.queue.jobs[-1]
        self.assertEqual([c.settings.edit.speed for c in job.clips], [2.0, 1.0])
        self.assertEqual(job.clips[1].settings.edit.keep, [(0.0, 0.5), (1.0, 2.0)])
        self.assertEqual(job.settings.edit.speed, 2.0)
        self.stop_exports()

    def test_joining_a_single_file_is_refused_with_a_message(self):
        self.load(self.landscape)
        self.window.join_clips()
        self.assertEqual(self.window.queue.jobs, [])
        self.assertIn("two or more", self.window.statusBar().currentMessage())

    def test_join_button_is_on_only_while_there_are_two_files(self):
        self.assertFalse(self.window.join_button.isEnabled())
        self.load(self.landscape, self.third)
        self.assertTrue(self.window.join_button.isEnabled())
        self.window.remove_selected()
        self.assertFalse(self.window.join_button.isEnabled())

    def test_join_button_follows_the_number_of_files(self):
        self.load(self.landscape)
        self.load(self.third)                  # added while the first file stays open
        self.assertTrue(self.window.join_button.isEnabled())
        self.window.media_list.setCurrentRow(1)
        self.window.remove_selected()
        self.assertFalse(self.window.join_button.isEnabled())


    def game_recording(self):
        """Make the open file look like a variable-rate game recording to the editor."""
        item = self.window.current
        item.info = MediaInfo(duration=3.0, width=320, height=180, has_audio=True, fps=57.97, max_fps=120.0)
        self.window._show_settings(item)
        return item

    def test_the_frame_rate_combo_edits_the_file_and_can_be_undone(self):
        self.load(self.landscape)
        item = self.window.current
        self.window.inspector.rate_box.setCurrentIndex(1)
        self.assertEqual(item.settings.edit.frame_rate, 60)
        self.window.undo_stack.undo()
        self.assertIsNone(item.settings.edit.frame_rate)
        self.assertEqual(self.window.inspector.rate_box.currentData(), None)
        self.window.undo_stack.redo()
        self.assertEqual(item.settings.edit.frame_rate, 60)
        self.assertEqual(self.window.inspector.rate_box.currentData(), 60)

    def test_a_variable_rate_video_shows_the_hint_until_a_steady_rate_is_chosen(self):
        self.load(self.landscape)
        hint = self.window.inspector.rate_hint
        self.assertTrue(hint.isHidden())                       # an ordinary 15 fps video
        self.game_recording()
        self.assertFalse(hint.isHidden())
        self.assertEqual(hint.text(), "This video's frame rate changes (up to 120 fps), which some phones can't "
                                      "play. Choose Steady 60 fps.")
        self.window.inspector.rate_box.setCurrentIndex(1)
        self.assertTrue(hint.isHidden())
        self.window.undo_stack.undo()
        self.assertFalse(hint.isHidden())
        self.window.inspector.rate_box.setCurrentIndex(2)       # Steady 30 fps is a steady rate too
        self.assertTrue(hint.isHidden())

    def test_the_hint_follows_the_selected_file(self):
        self.load(self.landscape)
        self.game_recording()
        self.load(self.third)
        self.window.media_list.setCurrentRow(1)
        self.assertTrue(self.window.inspector.rate_hint.isHidden())
        self.window.media_list.setCurrentRow(0)
        self.assertFalse(self.window.inspector.rate_hint.isHidden())

    def test_the_play_on_phone_preset_sets_the_file_up(self):
        self.load(self.landscape)
        self.game_recording()
        item = self.window.current
        self.window.inspector.preset_box.setCurrentIndex(1)
        edit = item.settings.edit
        self.assertEqual((edit.aspect, edit.resolution, edit.frame_rate, edit.crf, edit.mute),
                         (None, UP_TO_1080, 60, 18, False))
        self.assertTrue(self.window.inspector.rate_hint.isHidden())
        self.assertEqual(self.window.inspector.rate_box.currentData(), 60)
        self.assertEqual(self.window.inspector.size_box.currentData(), UP_TO_1080)
        self.window.undo_stack.undo()
        self.assertEqual(item.settings.edit.aspect, (9, 16))
        self.assertIsNone(item.settings.edit.frame_rate)


if __name__ == "__main__":
    unittest.main()
