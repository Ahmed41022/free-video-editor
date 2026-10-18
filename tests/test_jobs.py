import subprocess
import sys
import tempfile
import threading
import time
import unittest
from array import array
from pathlib import Path

from video_converter.core import ConversionCancelled, ConversionError, MediaInfo, find_ffmpeg, probe_media
from video_converter.jobs import Clip, Job, JobQueue, JobState
from video_converter.project import OUTPUT_AUDIO, ClipSettings, describe, describe_join
from video_converter.video import VideoEdit

FFMPEG = find_ffmpeg()
TIMEOUT = 10.0


def make_job(name="clip.mp4", **kwargs) -> Job:
    return Job(Path(name), ClipSettings(), **kwargs)


class Recorder:
    """A runner and update callback that record what the queue does, thread-safely."""

    def __init__(self, fail=(), crash=(), os_fail=(), block=()):
        self.lock = threading.Lock()
        self.order = []                 # job ids, in the order they started running
        self.updates = []               # (job id, state, progress) at each on_update call
        self.active = 0
        self.max_active = 0
        self.calls = {}                 # job id -> times the runner was called
        self.fail, self.crash, self.os_fail = set(fail), set(crash), set(os_fail)
        self.block = {job_id: threading.Event() for job_id in block}      # released by the test
        self.started = {}               # job id -> Event set when its runner begins

    def started_event(self, job_id):
        with self.lock:
            return self.started.setdefault(job_id, threading.Event())

    def on_update(self, job):
        with self.lock:
            self.updates.append((job.id, job.state, job.progress))

    def states_of(self, job_id):
        with self.lock:
            return [state for i, state, _ in self.updates if i == job_id]

    def runner(self, ffmpeg, job, progress):
        with self.lock:
            self.order.append(job.id)
            self.calls[job.id] = self.calls.get(job.id, 0) + 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.started_event(job.id).set()
        try:
            progress(0.5)
            gate = self.block.get(job.id)
            if gate is not None:
                # Wait to be released, or for a cancel request.
                while not gate.is_set():
                    if job.cancel_event.wait(0.005):
                        raise ConversionCancelled()
            if job.id in self.fail:
                raise ConversionError("boom")
            if job.id in self.os_fail:
                raise OSError("disk full")
            if job.id in self.crash:
                raise RuntimeError("kaboom")
            return Path(f"out-{job.id}.mp4")
        finally:
            with self.lock:
                self.active -= 1


class QueueTestCase(unittest.TestCase):
    def make_queue(self, **recorder_kwargs):
        self.rec = Recorder(**recorder_kwargs)
        self.queue = JobQueue("fake-ffmpeg", on_update=self.rec.on_update, runner=self.rec.runner)
        self.addCleanup(self.cleanup_queue)
        return self.queue

    def cleanup_queue(self):
        for gate in self.rec.block.values():
            gate.set()
        self.queue.shutdown(timeout=TIMEOUT)

    def wait_started(self, job):
        self.assertTrue(self.rec.started_event(job.id).wait(TIMEOUT), "job never started")

    def wait_all(self):
        self.assertTrue(self.queue.wait(TIMEOUT), "queue did not finish in time")


class JobBasicsTests(unittest.TestCase):
    def test_new_jobs_start_queued_with_unique_ids(self):
        jobs = [make_job() for _ in range(5)]
        self.assertTrue(all(j.state is JobState.QUEUED for j in jobs))
        self.assertEqual(len({j.id for j in jobs}), 5)
        self.assertEqual([j.id for j in jobs], sorted(j.id for j in jobs))
        self.assertTrue(all(j.progress == 0.0 and j.output is None and j.error == "" for j in jobs))

    def test_finished_states(self):
        self.assertFalse(JobState.QUEUED.finished)
        self.assertFalse(JobState.RUNNING.finished)
        for state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
            self.assertTrue(state.finished)

    def test_a_single_files_title_and_summary(self):
        settings = ClipSettings(edit=VideoEdit(speed=2.0))
        job = Job(Path("folder/clip.mp4"), settings)
        self.assertEqual(job.title, "clip.mp4")
        self.assertEqual(job.summary, describe(settings))
        self.assertIn("2× speed", job.summary)

    def test_a_single_files_summary_counts_cuts_that_reach_the_end(self):
        settings = ClipSettings(edit=VideoEdit(keep=[(0.0, 1.0)]))
        self.assertNotIn("cut", Job(Path("clip.mp4"), settings).summary)
        job = Job(Path("clip.mp4"), settings, info=MediaInfo(duration=4.0, width=640, height=360))
        self.assertIn("1 cut", job.summary)

    def test_a_joined_jobs_title_and_summary(self):
        settings = ClipSettings(edit=VideoEdit(aspect=(1, 1)))
        clips = [Clip(Path(f"folder/{name}.mp4"), settings) for name in ("first", "second", "third")]
        job = Job(clips[0].source, settings, clips=clips)
        self.assertEqual(job.title, "first.mp4 + 2 more")
        self.assertEqual(job.summary, describe_join(settings, 3))
        self.assertEqual(job.summary, "MP4 · 1:1 · 3 clips joined")

    def test_a_joined_mp3_summary(self):
        settings = ClipSettings(output=OUTPUT_AUDIO, audio_quality="320 kbps")
        clips = [Clip(Path("a.mp4"), settings), Clip(Path("b.mp4"), settings)]
        job = Job(clips[0].source, settings, clips=clips)
        self.assertEqual(job.title, "a.mp4 + 1 more")
        self.assertEqual(job.summary, "MP3 · 320 kbps · 2 clips joined")

    def test_a_job_without_other_clips_is_titled_by_its_file(self):
        job = Job(Path("a.mp4"), ClipSettings(), clips=[])
        self.assertEqual(job.title, "a.mp4")

    def test_each_job_has_its_own_clip_list(self):
        a, b = make_job(), make_job()
        a.clips.append(Clip(Path("x.mp4"), ClipSettings()))
        self.assertEqual(b.clips, [])

    def test_each_job_has_its_own_cancel_event(self):
        a, b = make_job(), make_job()
        a.cancel_event.set()
        self.assertFalse(b.cancel_event.is_set())

    def test_remaining_seconds(self):
        job = make_job()
        job.state, job.started_at, job.progress = JobState.RUNNING, 100.0, 0.5
        self.assertAlmostEqual(job.remaining_seconds(now=110.0), 10.0)
        job.progress = 0.25
        self.assertAlmostEqual(job.remaining_seconds(now=110.0), 30.0)
        job.progress = 0.9
        self.assertAlmostEqual(job.remaining_seconds(now=190.0), 10.0)

    def test_remaining_seconds_needs_two_percent_progress(self):
        job = make_job()
        job.state, job.started_at = JobState.RUNNING, 100.0
        for progress in (0.0, 0.01, 0.0199):
            job.progress = progress
            self.assertIsNone(job.remaining_seconds(now=150.0))
        job.progress = 0.02
        self.assertIsNotNone(job.remaining_seconds(now=150.0))

    def test_remaining_seconds_only_while_running(self):
        job = make_job()
        job.started_at, job.progress = 100.0, 0.5
        for state in (JobState.QUEUED, JobState.DONE, JobState.FAILED, JobState.CANCELLED):
            job.state = state
            self.assertIsNone(job.remaining_seconds(now=110.0))
        job.state, job.started_at = JobState.RUNNING, None
        self.assertIsNone(job.remaining_seconds(now=110.0))

    def test_remaining_seconds_is_never_negative(self):
        job = make_job()
        job.state, job.started_at, job.progress = JobState.RUNNING, 100.0, 1.0
        self.assertEqual(job.remaining_seconds(now=130.0), 0.0)
        job.progress = 0.5
        self.assertEqual(job.remaining_seconds(now=90.0), 0.0)     # clock earlier than the start

    def test_remaining_seconds_defaults_to_the_real_clock(self):
        job = make_job()
        job.state, job.progress = JobState.RUNNING, 0.5
        job.started_at = time.monotonic() - 4.0
        self.assertAlmostEqual(job.remaining_seconds(), 4.0, delta=1.0)


class RunningJobsTests(QueueTestCase):
    def test_runs_jobs_one_at_a_time_in_insertion_order(self):
        queue = self.make_queue()
        jobs = [queue.add(make_job(f"clip{i}.mp4")) for i in range(8)]
        self.wait_all()
        self.assertEqual(self.rec.order, [j.id for j in jobs])
        self.assertEqual(self.rec.max_active, 1)
        self.assertEqual(queue.jobs, jobs)
        self.assertTrue(all(j.state is JobState.DONE for j in jobs))

    def test_add_returns_the_same_job(self):
        queue = self.make_queue()
        job = make_job()
        self.assertIs(queue.add(job), job)

    def test_runner_gets_the_ffmpeg_path_and_job(self):
        seen = []

        def runner(ffmpeg, job, progress):
            seen.append((ffmpeg, job))
            return Path("x.mp4")

        queue = JobQueue("the-ffmpeg", runner=runner)
        self.addCleanup(queue.shutdown)
        job = queue.add(make_job())
        self.assertTrue(queue.wait(TIMEOUT))
        self.assertEqual(seen, [("the-ffmpeg", job)])

    def test_success_sets_output_progress_and_times(self):
        queue = self.make_queue()
        job = queue.add(make_job())
        self.wait_all()
        self.assertIs(job.state, JobState.DONE)
        self.assertEqual(job.output, Path(f"out-{job.id}.mp4"))
        self.assertEqual(job.progress, 1.0)
        self.assertEqual(job.error, "")
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)
        self.assertGreaterEqual(job.finished_at, job.started_at)

    def test_progress_is_one_when_done_even_if_never_reported(self):
        queue = JobQueue("f", runner=lambda ffmpeg, job, progress: Path("x.mp3"))
        self.addCleanup(queue.shutdown)
        job = queue.add(make_job())
        self.assertTrue(queue.wait(TIMEOUT))
        self.assertEqual(job.progress, 1.0)

    def test_progress_updates_reach_the_job_and_on_update(self):
        queue = self.make_queue()
        job = queue.add(make_job())
        self.wait_all()
        with self.rec.lock:
            progress_seen = [p for i, s, p in self.rec.updates if i == job.id and s is JobState.RUNNING]
        self.assertIn(0.5, progress_seen)

    def test_conversion_error_fails_the_job_with_its_message(self):
        queue = self.make_queue()
        job = make_job()
        self.rec.fail.add(job.id)
        queue.add(job)
        self.wait_all()
        self.assertIs(job.state, JobState.FAILED)
        self.assertEqual(job.error, "boom")
        self.assertIsNone(job.output)
        self.assertLess(job.progress, 1.0)
        self.assertIsNotNone(job.finished_at)

    def test_os_error_fails_the_job(self):
        queue = self.make_queue()
        job = make_job()
        self.rec.os_fail.add(job.id)
        queue.add(job)
        self.wait_all()
        self.assertIs(job.state, JobState.FAILED)
        self.assertIn("disk full", job.error)

    def test_unexpected_exception_fails_the_job_and_keeps_the_queue_alive(self):
        queue = self.make_queue()
        bad = make_job("bad.mp4")
        self.rec.crash.add(bad.id)
        queue.add(bad)
        later = queue.add(make_job("later.mp4"))
        self.wait_all()
        self.assertIs(bad.state, JobState.FAILED)
        self.assertIn("kaboom", bad.error)
        self.assertTrue(bad.error)
        self.assertIs(later.state, JobState.DONE)

    def test_a_failing_job_does_not_stop_later_jobs(self):
        queue = self.make_queue()
        jobs = [make_job(f"c{i}.mp4") for i in range(5)]
        self.rec.fail.update({jobs[0].id, jobs[2].id})
        self.rec.crash.add(jobs[3].id)
        for job in jobs:
            queue.add(job)
        self.wait_all()
        self.assertEqual([j.state for j in jobs],
                         [JobState.FAILED, JobState.DONE, JobState.FAILED, JobState.FAILED, JobState.DONE])
        self.assertEqual(self.rec.order, [j.id for j in jobs])

    def test_runner_raising_cancelled_marks_cancelled(self):
        def runner(ffmpeg, job, progress):
            raise ConversionCancelled()

        queue = JobQueue("f", runner=runner)
        self.addCleanup(queue.shutdown)
        job = queue.add(make_job())
        self.assertTrue(queue.wait(TIMEOUT))
        self.assertIs(job.state, JobState.CANCELLED)
        self.assertEqual(job.error, "")
        self.assertLess(job.progress, 1.0)

    def test_state_transitions_are_reported_in_order(self):
        queue = self.make_queue()
        job = queue.add(make_job())
        self.wait_all()
        states = self.rec.states_of(job.id)
        self.assertIn(JobState.RUNNING, states)
        self.assertEqual(states[-1], JobState.DONE)
        self.assertLess(states.index(JobState.RUNNING), len(states) - 1)
        # Never goes backwards: QUEUED* RUNNING+ DONE
        order = [list(JobState).index(s) for s in states]
        self.assertEqual(order, sorted(order))

    def test_on_update_called_for_every_state_change(self):
        queue = self.make_queue()
        ok, bad, cancelled = make_job("ok.mp4"), make_job("bad.mp4"), make_job("cancelled.mp4")
        self.rec.fail.add(bad.id)
        self.rec.block[ok.id] = threading.Event()
        queue.add(ok)
        self.wait_started(ok)
        queue.add(bad)
        queue.add(cancelled)
        queue.cancel(cancelled.id)                 # queued -> cancelled while `ok` is running
        self.rec.block[ok.id].set()
        self.wait_all()
        self.assertEqual(self.rec.states_of(bad.id)[-1], JobState.FAILED)
        self.assertIn(JobState.RUNNING, self.rec.states_of(bad.id))
        self.assertEqual(self.rec.states_of(ok.id)[-1], JobState.DONE)
        self.assertIn(JobState.CANCELLED, self.rec.states_of(cancelled.id))
        self.assertNotIn(JobState.RUNNING, self.rec.states_of(cancelled.id))

    def test_each_job_runs_exactly_once(self):
        queue = self.make_queue()
        jobs = [queue.add(make_job()) for _ in range(10)]
        self.wait_all()
        self.assertEqual(self.rec.calls, {j.id: 1 for j in jobs})


class CancelTests(QueueTestCase):
    def test_cancelling_a_queued_job_never_runs_it(self):
        queue = self.make_queue()
        first = make_job("first.mp4")
        self.rec.block[first.id] = threading.Event()
        second, third = make_job("second.mp4"), make_job("third.mp4")
        queue.add(first)
        self.wait_started(first)
        queue.add(second)
        queue.add(third)

        queue.cancel(second.id)
        self.assertIs(second.state, JobState.CANCELLED)
        self.assertIsNotNone(second.finished_at)
        self.assertTrue(second.cancel_event.is_set())

        self.rec.block[first.id].set()
        self.wait_all()
        self.assertEqual(self.rec.order, [first.id, third.id])
        self.assertIs(first.state, JobState.DONE)
        self.assertIs(second.state, JobState.CANCELLED)
        self.assertIs(third.state, JobState.DONE)
        self.assertNotIn(second.id, self.rec.calls)

    def test_cancelling_the_running_job_stops_it_and_the_next_one_runs(self):
        queue = self.make_queue()
        running, after = make_job("running.mp4"), make_job("after.mp4")
        self.rec.block[running.id] = threading.Event()
        queue.add(running)
        queue.add(after)
        self.wait_started(running)

        queue.cancel(running.id)
        self.wait_all()
        self.assertIs(running.state, JobState.CANCELLED)
        self.assertIsNone(running.output)
        self.assertLess(running.progress, 1.0)
        self.assertIs(after.state, JobState.DONE)
        self.assertEqual(self.rec.order, [running.id, after.id])

    def test_a_running_job_stays_running_until_the_runner_stops(self):
        queue = self.make_queue()
        job = make_job()
        gate = self.rec.block[job.id] = threading.Event()
        queue.add(job)
        self.wait_started(job)
        queue.cancel(job.id)
        self.assertTrue(job.cancel_event.is_set())
        # A runner that ignores the request still finishes normally; the queue doesn't invent a state.
        gate.set()
        self.wait_all()
        self.assertIn(job.state, (JobState.CANCELLED, JobState.DONE))

    def test_cancelling_finished_jobs_is_a_no_op(self):
        queue = self.make_queue()
        done, failed = make_job("done.mp4"), make_job("failed.mp4")
        self.rec.fail.add(failed.id)
        queue.add(done)
        queue.add(failed)
        self.wait_all()
        for job, state in ((done, JobState.DONE), (failed, JobState.FAILED)):
            updates_before = len(self.rec.updates)
            finished_at = job.finished_at
            queue.cancel(job.id)
            self.assertIs(job.state, state)
            self.assertFalse(job.cancel_event.is_set())
            self.assertEqual(job.finished_at, finished_at)
            self.assertEqual(len(self.rec.updates), updates_before)

    def test_cancelling_twice_or_an_unknown_id_is_harmless(self):
        queue = self.make_queue()
        first, second = make_job(), make_job()
        self.rec.block[first.id] = threading.Event()
        queue.add(first)
        self.wait_started(first)
        queue.add(second)
        queue.cancel(second.id)
        queue.cancel(second.id)
        queue.cancel(-1)
        queue.cancel(10 ** 9)
        self.assertEqual(self.rec.states_of(second.id).count(JobState.CANCELLED), 1)
        self.rec.block[first.id].set()
        self.wait_all()

    def test_cancel_all(self):
        queue = self.make_queue()
        running = make_job("running.mp4")
        self.rec.block[running.id] = threading.Event()
        queue.add(running)
        self.wait_started(running)
        waiting = [queue.add(make_job(f"w{i}.mp4")) for i in range(3)]
        queue.cancel_all()
        self.wait_all()
        self.assertTrue(all(j.state is JobState.CANCELLED for j in [running, *waiting]))
        self.assertEqual(self.rec.order, [running.id])
        self.assertFalse(queue.busy)

    def test_cancelling_a_queued_job_wakes_waiters(self):
        queue = self.make_queue()
        running, waiting = make_job(), make_job()
        gate = self.rec.block[running.id] = threading.Event()
        queue.add(running)
        self.wait_started(running)
        queue.add(waiting)
        queue.cancel(waiting.id)
        gate.set()
        self.wait_all()


class WaitAndBusyTests(QueueTestCase):
    def test_empty_queue_is_idle(self):
        queue = self.make_queue()
        self.assertFalse(queue.busy)
        self.assertTrue(queue.wait(0.01))
        self.assertTrue(queue.wait(0))

    def test_wait_times_out_while_work_is_pending(self):
        queue = self.make_queue()
        job = make_job()
        gate = self.rec.block[job.id] = threading.Event()
        queue.add(job)
        self.wait_started(job)
        self.assertTrue(queue.busy)
        self.assertFalse(queue.wait(0.05))
        self.assertFalse(queue.wait(0))
        gate.set()
        self.wait_all()
        self.assertFalse(queue.busy)
        self.assertTrue(queue.wait(0.01))

    def test_busy_counts_queued_jobs_too(self):
        queue = self.make_queue()
        first, second = make_job(), make_job()
        gate = self.rec.block[first.id] = threading.Event()
        queue.add(first)
        self.wait_started(first)
        queue.add(second)
        self.assertTrue(queue.busy)
        queue.cancel(first.id)
        self.wait_all()
        self.assertFalse(queue.busy)
        gate.set()

    def test_wait_without_timeout_returns_once_done(self):
        queue = self.make_queue()
        queue.add(make_job())
        result = []
        waiter = threading.Thread(target=lambda: result.append(queue.wait()), daemon=True)
        waiter.start()
        waiter.join(TIMEOUT)
        self.assertFalse(waiter.is_alive(), "wait() never returned")
        self.assertEqual(result, [True])

    def test_several_waiters_are_all_released(self):
        queue = self.make_queue()
        job = make_job()
        gate = self.rec.block[job.id] = threading.Event()
        queue.add(job)
        self.wait_started(job)
        results = []
        waiters = [threading.Thread(target=lambda: results.append(queue.wait(TIMEOUT)), daemon=True)
                   for _ in range(4)]
        for w in waiters:
            w.start()
        gate.set()
        for w in waiters:
            w.join(TIMEOUT)
            self.assertFalse(w.is_alive())
        self.assertEqual(results, [True] * 4)

    def test_clear_finished_removes_only_finished_jobs(self):
        queue = self.make_queue()
        done, failed, cancelled = make_job("done.mp4"), make_job("failed.mp4"), make_job("cancelled.mp4")
        self.rec.fail.add(failed.id)
        for job in (done, failed):
            queue.add(job)
        self.wait_all()

        running, waiting = make_job("running.mp4"), make_job("waiting.mp4")
        gate = self.rec.block[running.id] = threading.Event()
        queue.add(running)
        self.wait_started(running)
        queue.add(cancelled)
        queue.add(waiting)
        queue.cancel(cancelled.id)

        removed = queue.clear_finished()
        self.assertEqual(removed, [done, failed, cancelled])
        self.assertEqual(queue.jobs, [running, waiting])

        gate.set()
        self.wait_all()
        self.assertEqual(queue.clear_finished(), [running, waiting])
        self.assertEqual(queue.jobs, [])
        self.assertEqual(queue.clear_finished(), [])

    def test_queue_keeps_working_after_clearing(self):
        queue = self.make_queue()
        queue.add(make_job())
        self.wait_all()
        queue.clear_finished()
        again = queue.add(make_job())
        self.wait_all()
        self.assertIs(again.state, JobState.DONE)
        self.assertEqual(queue.jobs, [again])


class RestartTests(QueueTestCase):
    def test_jobs_added_after_the_queue_went_idle_still_run(self):
        queue = self.make_queue()
        first = queue.add(make_job())
        self.wait_all()
        second = queue.add(make_job())
        self.wait_all()
        third = queue.add(make_job())
        self.wait_all()
        self.assertEqual([j.state for j in (first, second, third)], [JobState.DONE] * 3)

    def test_worker_thread_exits_when_idle(self):
        queue = self.make_queue()
        queue.add(make_job())
        self.wait_all()
        worker = queue._worker
        worker.join(TIMEOUT)
        self.assertFalse(worker.is_alive())

    def test_repeated_idle_then_add_never_loses_a_job(self):
        queue = self.make_queue()
        for i in range(30):
            job = queue.add(make_job(f"again{i}.mp4"))
            self.assertTrue(queue.wait(TIMEOUT), f"iteration {i}: job never finished")
            self.assertIs(job.state, JobState.DONE, f"iteration {i}")

    def test_add_right_as_the_worker_goes_idle(self):
        # Adds a job from the on_update callback of the previous job's final update, i.e. at
        # the moment the worker is about to look for more work and exit.
        done = threading.Event()
        jobs = []
        lock = threading.Lock()

        def on_update(job):
            if job.state is JobState.DONE and len(jobs) < 40:
                with lock:
                    if len(jobs) < 40 and job is jobs[-1]:
                        jobs.append(queue.add(make_job()))
                        if len(jobs) == 40:
                            done.set()

        queue = JobQueue("f", on_update=on_update, runner=lambda f, j, p: Path("x"))
        self.addCleanup(queue.shutdown)
        with lock:
            jobs.append(make_job())
        queue.add(jobs[0])
        self.assertTrue(done.wait(TIMEOUT))
        self.assertTrue(queue.wait(TIMEOUT))
        self.assertEqual(len(jobs), 40)
        self.assertTrue(all(j.state is JobState.DONE for j in jobs))

    def test_stress_with_a_tiny_thread_switch_interval(self):
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, old)
        queue = self.make_queue()
        for i in range(60):
            job = queue.add(make_job())
            # Alternate between waiting for idle and adding straight away.
            if i % 2:
                self.assertTrue(queue.wait(TIMEOUT), f"iteration {i}")
                self.assertIs(job.state, JobState.DONE)
        self.wait_all()
        self.assertTrue(all(j.state is JobState.DONE for j in queue.jobs))
        self.assertEqual(self.rec.max_active, 1)

    def test_concurrent_adders(self):
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-5)
        self.addCleanup(sys.setswitchinterval, old)
        queue = self.make_queue()
        per_thread, threads = 25, 4
        added = [[] for _ in range(threads)]

        def adder(index):
            for n in range(per_thread):
                added[index].append(queue.add(make_job(f"t{index}-{n}.mp4")))
                if n % 5 == 4:
                    queue.wait(TIMEOUT)

        workers = [threading.Thread(target=adder, args=(i,), daemon=True) for i in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(TIMEOUT * 3)
            self.assertFalse(w.is_alive())
        self.wait_all()

        every_job = [j for batch in added for j in batch]
        self.assertEqual(len(queue.jobs), per_thread * threads)
        self.assertTrue(all(j.state is JobState.DONE for j in every_job))
        self.assertEqual(self.rec.calls, {j.id: 1 for j in every_job})
        self.assertEqual(self.rec.max_active, 1)
        # Each thread's own jobs ran in the order that thread added them.
        position = {job_id: n for n, job_id in enumerate(self.rec.order)}
        for batch in added:
            self.assertEqual([position[j.id] for j in batch], sorted(position[j.id] for j in batch))


class ShutdownTests(QueueTestCase):
    def test_shutdown_cancels_running_and_pending_work(self):
        queue = self.make_queue()
        running = make_job("running.mp4")
        self.rec.block[running.id] = threading.Event()
        queue.add(running)
        self.wait_started(running)
        pending = [queue.add(make_job(f"p{i}.mp4")) for i in range(3)]

        queue.shutdown(timeout=TIMEOUT)
        self.assertIs(running.state, JobState.CANCELLED)
        self.assertTrue(all(j.state is JobState.CANCELLED for j in pending))
        self.assertEqual(self.rec.order, [running.id])
        self.assertFalse(queue._worker.is_alive())
        self.assertFalse(queue.busy)

    def test_add_after_shutdown_raises(self):
        queue = self.make_queue()
        queue.shutdown()
        with self.assertRaises(RuntimeError):
            queue.add(make_job())
        self.assertEqual(queue.jobs, [])

    def test_add_after_shutdown_raises_even_after_work_was_done(self):
        queue = self.make_queue()
        queue.add(make_job())
        self.wait_all()
        queue.shutdown()
        with self.assertRaises(RuntimeError):
            queue.add(make_job())

    def test_shutdown_is_idempotent_and_fine_on_an_idle_queue(self):
        queue = self.make_queue()
        queue.shutdown()
        queue.shutdown()
        queue = self.make_queue()
        queue.add(make_job())
        self.wait_all()
        queue.shutdown()
        queue.shutdown()

    def test_shutdown_does_not_hang_on_a_runner_that_ignores_cancel(self):
        release = threading.Event()
        started = threading.Event()

        def stubborn(ffmpeg, job, progress):
            started.set()
            release.wait(TIMEOUT)
            return Path("x.mp4")

        queue = JobQueue("f", runner=stubborn)
        self.addCleanup(release.set)
        job = queue.add(make_job())
        self.assertTrue(started.wait(TIMEOUT))
        queue.shutdown(timeout=0.1)          # returns after the join timeout
        release.set()
        queue._worker.join(TIMEOUT)
        self.assertFalse(queue._worker.is_alive())
        self.assertTrue(job.state.finished)

    def test_a_job_finished_before_shutdown_keeps_its_state(self):
        queue = self.make_queue()
        done = queue.add(make_job())
        self.wait_all()
        queue.shutdown()
        self.assertIs(done.state, JobState.DONE)
        self.assertFalse(done.cancel_event.is_set())


class UpdateCallbackRobustnessTests(unittest.TestCase):
    def test_a_raising_on_update_does_not_wedge_the_queue(self):
        # Suspected bug: jobs.py JobQueue._work calls `self.on_update(job)` outside its try block
        # (both right after the job is marked RUNNING and after the final state is set). If the
        # callback raises, the worker thread dies with `_worker_running` still True, so add()
        # never starts another worker; the job stays RUNNING forever and wait() never returns True.
        # Expected: the queue keeps processing jobs (e.g. later ones reach DONE).
        calls = []

        def on_update(job):
            calls.append(job.state)
            if job.state is JobState.RUNNING and len(calls) < 50 and job.source.name == "explode.mp4":
                raise ValueError("UI callback failed")

        queue = JobQueue("f", on_update=on_update, runner=lambda f, j, p: Path("x"))
        old_hook = threading.excepthook
        threading.excepthook = lambda args: None        # keep the expected traceback out of the test output
        try:
            queue.add(make_job("explode.mp4"))
            later = queue.add(make_job("later.mp4"))
            finished = queue.wait(2.0)
            self.assertTrue(finished)
            self.assertIs(later.state, JobState.DONE)
        finally:
            threading.excepthook = old_hook
            queue.shutdown(timeout=0.1)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.source = cls.tmp / "clip.mp4"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=duration=2:size=320x180:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.source)],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.updates = []
        self.queue = JobQueue(FFMPEG, on_update=lambda job: self.updates.append((job.id, job.state)))
        self.addCleanup(self.queue.shutdown)

    def test_mp3_and_phone_mp4_exports(self):
        out = self.tmp / "exports"
        mp3 = self.queue.add(Job(self.source, ClipSettings(output=OUTPUT_AUDIO), output_dir=out))
        mp4 = self.queue.add(Job(self.source, ClipSettings(), output_dir=out))
        self.assertTrue(self.queue.wait(120), "exports did not finish")

        self.assertIs(mp3.state, JobState.DONE, mp3.error)
        self.assertIs(mp4.state, JobState.DONE, mp4.error)
        self.assertEqual(mp3.output, out / "clip.mp3")
        self.assertEqual(mp4.output, out / "clip (phone).mp4")
        self.assertTrue(mp3.output.is_file() and mp3.output.stat().st_size > 0)
        self.assertTrue(mp4.output.is_file() and mp4.output.stat().st_size > 0)
        self.assertEqual((mp3.progress, mp4.progress), (1.0, 1.0))

        mp3_info = probe_media(FFMPEG, mp3.output)
        self.assertFalse(mp3_info.has_video)
        self.assertAlmostEqual(mp3_info.duration, 2.0, delta=0.3)

        info = probe_media(FFMPEG, mp4.output)
        self.assertEqual((info.width, info.height), (180, 320))      # 9:16
        self.assertTrue(info.has_audio)
        self.assertAlmostEqual(info.duration, 2.0, delta=0.3)
        self.assertTrue(self.source.is_file())

    def test_joined_job_makes_one_file_as_long_as_its_clips(self):
        out = self.tmp / "joined"
        other = self.tmp / "other.mp4"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=duration=1:size=160x120:rate=10", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", str(other)], check=True)
        settings = ClipSettings(edit=VideoEdit(aspect=None))
        clips = [Clip(self.source, settings.copy()), Clip(other, settings.copy())]
        job = self.queue.add(Job(self.source, settings.copy(), output_dir=out, clips=clips))
        self.assertTrue(self.queue.wait(120))
        self.assertIs(job.state, JobState.DONE, job.error)
        self.assertEqual(job.output, out / "clip (joined).mp4")
        self.assertEqual([p.name for p in out.iterdir()], ["clip (joined).mp4"])
        info = probe_media(FFMPEG, job.output)
        self.assertAlmostEqual(info.duration, 3.0, delta=0.3)
        self.assertEqual((info.width, info.height), (320, 180))        # the first clip's size
        self.assertTrue(info.has_audio)                                # the second clip is silent

    def test_joined_mp3_job(self):
        out = self.tmp / "joined-mp3"
        settings = ClipSettings(output=OUTPUT_AUDIO)
        clips = [Clip(self.source, settings.copy()), Clip(self.source, settings.copy())]
        job = self.queue.add(Job(self.source, settings.copy(), output_dir=out, clips=clips))
        self.assertTrue(self.queue.wait(120))
        self.assertIs(job.state, JobState.DONE, job.error)
        self.assertEqual(job.output, out / "clip (joined).mp3")
        self.assertAlmostEqual(probe_media(FFMPEG, job.output).duration, 4.0, delta=0.3)

    def test_two_joined_jobs_of_the_same_files_do_not_collide(self):
        out = self.tmp / "joined-twice"
        settings = ClipSettings(output=OUTPUT_AUDIO)
        jobs = [self.queue.add(Job(self.source, settings.copy(), output_dir=out,
                                   clips=[Clip(self.source, settings.copy()), Clip(self.source, settings.copy())]))
                for _ in range(2)]
        self.assertTrue(self.queue.wait(120))
        self.assertEqual([j.state for j in jobs], [JobState.DONE, JobState.DONE])
        self.assertEqual([j.output for j in jobs], [out / "clip (joined).mp3", out / "clip (joined) (1).mp3"])

    def test_a_joined_job_with_a_broken_clip_fails_cleanly(self):
        out = self.tmp / "joined-broken"
        settings = ClipSettings(edit=VideoEdit(aspect=None))
        clips = [Clip(self.source, settings.copy()), Clip(self.tmp / "missing.mp4", settings.copy())]
        job = self.queue.add(Job(self.source, settings.copy(), output_dir=out, clips=clips))
        self.assertTrue(self.queue.wait(120))
        self.assertIs(job.state, JobState.FAILED)
        self.assertIn("not found", job.error.lower())
        self.assertFalse(out.exists() and any(out.iterdir()))

    def test_mp3_job_passes_cuts_and_speed_through(self):
        out = self.tmp / "mp3-edit"
        edit = VideoEdit(keep=[(0.0, 1.0), (1.5, 2.0)], speed=0.5)     # 1.5 s kept, at half speed
        job = self.queue.add(Job(self.source, ClipSettings(output=OUTPUT_AUDIO, edit=edit), output_dir=out))
        self.assertTrue(self.queue.wait(120))
        self.assertIs(job.state, JobState.DONE, job.error)
        self.assertAlmostEqual(probe_media(FFMPEG, job.output).duration, 3.0, delta=0.25)

    def test_mp3_job_uses_the_sound_fades_not_the_picture_fades(self):
        out = self.tmp / "mp3-fade"
        faded = ClipSettings(output=OUTPUT_AUDIO, edit=VideoEdit(audio_fade_in=1.0))
        picture_only = ClipSettings(output=OUTPUT_AUDIO, edit=VideoEdit(fade_in=1.0))
        a = self.queue.add(Job(self.source, faded, output_dir=out))
        b = self.queue.add(Job(self.source, picture_only, output_dir=out))
        self.assertTrue(self.queue.wait(120))

        def first_and_middle_peaks(path):
            data = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                                   "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
            samples = array("h")
            samples.frombytes(data)
            return max(abs(s) for s in samples[:800]), max(abs(s) for s in samples[6000:10000])

        start, middle = first_and_middle_peaks(a.output)
        self.assertLess(start, middle / 4)
        start, middle = first_and_middle_peaks(b.output)
        self.assertGreater(start, middle / 2)

    def test_mp4_job_passes_cuts_speed_and_reverse_through(self):
        out = self.tmp / "mp4-edit"
        edit = VideoEdit(aspect=None, keep=[(0.0, 1.0), (1.5, 2.0)], speed=2.0, reverse=True)
        job = self.queue.add(Job(self.source, ClipSettings(edit=edit), output_dir=out))
        self.assertTrue(self.queue.wait(120))
        self.assertIs(job.state, JobState.DONE, job.error)
        self.assertAlmostEqual(probe_media(FFMPEG, job.output).duration, 0.75, delta=0.15)

    def test_default_output_is_next_to_the_source(self):
        copy_dir = self.tmp / "next-to"
        copy_dir.mkdir()
        source = copy_dir / "clip.mp4"
        source.write_bytes(self.source.read_bytes())
        job = self.queue.add(Job(source, ClipSettings(edit=VideoEdit(aspect=(1, 1)))))
        self.assertTrue(self.queue.wait(120))
        self.assertIs(job.state, JobState.DONE, job.error)
        self.assertEqual(job.output, copy_dir / "clip (square).mp4")
        info = probe_media(FFMPEG, job.output)
        self.assertEqual((info.width, info.height), (180, 180))

    def test_queued_exports_of_the_same_file_do_not_collide(self):
        out = self.tmp / "same-name"
        jobs = [self.queue.add(Job(self.source, ClipSettings(output=OUTPUT_AUDIO), output_dir=out))
                for _ in range(2)]
        self.assertTrue(self.queue.wait(120))
        self.assertEqual([j.state for j in jobs], [JobState.DONE, JobState.DONE])
        self.assertEqual([j.output for j in jobs], [out / "clip.mp3", out / "clip (1).mp3"])

    def test_missing_source_fails_and_the_next_job_still_runs(self):
        out = self.tmp / "after-failure"
        bad = self.queue.add(Job(self.tmp / "missing.mp4", ClipSettings(), output_dir=out))
        good = self.queue.add(Job(self.source, ClipSettings(output=OUTPUT_AUDIO), output_dir=out))
        self.assertTrue(self.queue.wait(120))
        self.assertIs(bad.state, JobState.FAILED)
        self.assertIn("not found", bad.error.lower())
        self.assertIs(good.state, JobState.DONE, good.error)
        self.assertTrue(good.output.is_file())

    def test_audio_export_of_a_video_without_sound_fails_cleanly(self):
        silent = self.tmp / "no-audio.mp4"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=duration=1:size=160x120:rate=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)],
            check=True,
        )
        out = self.tmp / "no-audio-out"
        job = self.queue.add(Job(silent, ClipSettings(output=OUTPUT_AUDIO), output_dir=out))
        self.assertTrue(self.queue.wait(60))
        self.assertIs(job.state, JobState.FAILED)
        self.assertIn("audio", job.error.lower())
        self.assertFalse((out / "no-audio.mp3").exists())


if __name__ == "__main__":
    unittest.main()
