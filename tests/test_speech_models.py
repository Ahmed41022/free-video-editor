"""The speech models for automatic captions: the pinned manifest, checking a model on disk, and the
download manager (against a fake server, never the network)."""

import errno
import hashlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from video_converter import speech_models as sm
from video_converter.core import ConversionCancelled, ConversionError
from video_converter.locks import FileLock


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# A small stand-in for a model: three files, one big enough to arrive in several chunks.
FILES = {"config.json": b'{"x": 1}', "model.bin": bytes(range(256)) * 1000, "vocabulary.txt": b"a\nb\nc\n"}


def tiny_model(files=None, key="fast") -> sm.SpeechModel:
    files = FILES if files is None else files
    return sm.SpeechModel(key, "Fast", "note", "tiny", "Systran/faster-whisper-tiny", "abc123",
                          tuple(sm.ModelFile(name, len(data), sha(data)) for name, data in files.items()))


class Answer:
    def __init__(self, data: bytes, status=200, content_range=None, fail_after=None):
        self.data, self.status, self.pos, self.fail_after = data, status, 0, fail_after
        self.headers = {"Content-Range": content_range} if content_range else {}
        self.closed = False

    def read(self, n=-1):
        if self.fail_after is not None and self.pos >= self.fail_after:
            raise ConnectionResetError("connection reset")
        if n < 0:
            n = len(self.data) - self.pos
        if self.fail_after is not None:
            n = min(n, self.fail_after - self.pos)
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class FakeServer:
    """Stands in for `open_url`: serves `files` by the file name at the end of the URL and understands Range."""

    def __init__(self, files=None, honor_range=True, fail_after=None, tamper=None):
        self.files = dict(FILES if files is None else files)
        self.honor_range, self.fail_after, self.tamper = honor_range, fail_after, tamper or {}
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        name = url.rsplit("/", 1)[1]
        if name not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        data = self.tamper.get(name, self.files[name])
        range_ = headers.get("Range")
        if range_ and self.honor_range:
            start = int(re.match(r"bytes=(\d+)-", range_).group(1))
            return Answer(data[start:], 206, f"bytes {start}-{len(data) - 1}/{len(data)}", self.fail_after)
        return Answer(data, 200, None, self.fail_after)


class ManifestTests(unittest.TestCase):
    def test_every_model_pins_every_file_by_size_and_sha256(self):
        self.assertEqual(set(sm.MODELS), {"fast", "balanced", "accurate"})
        for model in sm.MODELS.values():
            names = [f.name for f in model.files]
            self.assertEqual(sorted(names), ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"])
            for file in model.files:
                self.assertRegex(file.sha256, r"\A[0-9a-f]{64}\Z", f"{model.key}/{file.name}")
                self.assertGreater(file.size, 0)
            self.assertRegex(model.revision, r"\A[0-9a-f]{40}\Z")
            self.assertTrue(model.repo.startswith("Systran/faster-whisper-"))

    def test_the_downloads_are_about_75_141_and_464_megabytes(self):
        sizes = {key: round(model.total_bytes / 1_048_576) for key, model in sm.MODELS.items()}
        self.assertEqual(sizes, {"fast": 75, "balanced": 141, "accurate": 464})
        self.assertEqual(sm.MODELS["fast"].size_text, "75 MB")

    def test_the_urls_are_https_on_huggingface_at_a_pinned_revision(self):
        for model in sm.MODELS.values():
            for file in model.files:
                url = model.url(file)
                self.assertTrue(sm.allowed_url(url), url)
                self.assertIn(f"/{model.revision}/", url)

    def test_the_default_is_one_of_the_models(self):
        self.assertIn(sm.DEFAULT_MODEL, sm.MODELS)


class AddressTests(unittest.TestCase):
    def test_only_https_on_huggingface_or_its_cdn_is_allowed(self):
        for url in ("https://huggingface.co/a/b", "https://cdn-lfs.huggingface.co/x", "https://us.aws.cdn.hf.co/x/y?z=1",
                    "https://hf.co/x"):
            self.assertTrue(sm.allowed_url(url), url)
        for url in ("http://huggingface.co/a", "https://example.com/huggingface.co", "https://evilhuggingface.co/a",
                    "https://huggingface.co.evil.com/a", "ftp://huggingface.co/a", "file:///c:/x", "https://evil-hf.co/x",
                    "huggingface.co/a", ""):
            self.assertFalse(sm.allowed_url(url), url)

    def test_open_url_refuses_other_addresses_without_connecting(self):
        with mock.patch.object(urllib.request.OpenerDirector, "open") as opened:
            with self.assertRaises(ConversionError):
                sm.open_url("http://huggingface.co/x")
            with self.assertRaises(ConversionError):
                sm.open_url("https://example.com/x")
        opened.assert_not_called()

    def test_a_redirect_to_somewhere_else_is_refused_but_one_to_the_cdn_is_followed(self):
        handler = sm._OnlyHuggingFace()
        request = urllib.request.Request("https://huggingface.co/Systran/m/resolve/r/model.bin")
        for bad in ("http://us.aws.cdn.hf.co/x", "https://example.com/x", "https://huggingface.co.evil.com/x"):
            with self.assertRaises(urllib.error.URLError, msg=bad):
                handler.redirect_request(request, None, 302, "Found", {}, bad)
        for good in ("https://us.aws.cdn.hf.co/x?sig=1", "/api/resolve-cache/models/x"):
            followed = handler.redirect_request(request, None, 302, "Found", {}, good)
            self.assertTrue(sm.allowed_url(followed.full_url), followed.full_url)


class OnDiskTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.model = tiny_model()

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, files=None):
        folder = sm.model_path(self.model, self.root)
        folder.mkdir(parents=True)
        for name, data in (FILES if files is None else files).items():
            (folder / name).write_bytes(data)
        return folder

    def test_models_live_under_a_folder_named_for_the_model(self):
        self.assertEqual(sm.model_path(self.model, self.root), self.root / "tiny")
        self.assertEqual(sm.models_folder(self.root), self.root)

    def test_the_default_folder_is_under_local_app_data(self):
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}):
            self.assertEqual(sm.models_folder(), Path(r"C:\Users\x\AppData\Local") / "Free Video Editor" / "models")

    def test_nothing_is_downloaded_in_an_empty_folder(self):
        self.assertFalse(sm.is_downloaded(self.model, self.root))
        with self.assertRaises(ConversionError):
            sm.verify_model(self.model, self.root)

    def test_an_intact_model_is_downloaded_and_verifies(self):
        folder = self.put()
        self.assertTrue(sm.is_downloaded(self.model, self.root))
        self.assertEqual(sm.verify_model(self.model, self.root), folder)

    def test_a_missing_file_is_not_downloaded(self):
        folder = self.put()
        (folder / "vocabulary.txt").unlink()
        self.assertFalse(sm.is_downloaded(self.model, self.root))
        with self.assertRaisesRegex(ConversionError, "vocabulary.txt"):
            sm.verify_model(self.model, self.root)

    def test_a_truncated_file_is_rejected(self):
        folder = self.put()
        data = (folder / "model.bin").read_bytes()
        (folder / "model.bin").write_bytes(data[:-10])
        self.assertFalse(sm.is_downloaded(self.model, self.root))
        with self.assertRaisesRegex(ConversionError, "damaged"):
            sm.verify_model(self.model, self.root)

    def test_a_file_of_the_right_size_but_other_content_is_rejected(self):
        folder = self.put()
        data = bytearray((folder / "model.bin").read_bytes())
        data[100] ^= 0xFF
        (folder / "model.bin").write_bytes(bytes(data))
        self.assertTrue(sm.is_downloaded(self.model, self.root))        # the quick check only looks at sizes...
        with self.assertRaisesRegex(ConversionError, "damaged"):        # ...the real one reads the files
            sm.verify_model(self.model, self.root)

    def test_verifying_can_be_cancelled(self):
        self.put()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ConversionCancelled):
            sm.verify_model(self.model, self.root, cancel)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.model = tiny_model()
        self.staging = self.root / "tiny.downloading"
        self.patch_chunk = mock.patch.object(sm, "CHUNK", 1000)       # several chunks even for these small files
        self.patch_chunk.start()

    def tearDown(self):
        self.patch_chunk.stop()
        self._tmp.cleanup()

    def test_a_download_puts_an_intact_model_in_place_and_reports_progress(self):
        server, seen = FakeServer(), []
        folder = sm.download_model(self.model, self.root, lambda f, done, total: seen.append((f, done, total)),
                                   open_=server)
        self.assertEqual(folder, self.root / "tiny")
        self.assertEqual(sm.verify_model(self.model, self.root), folder)
        self.assertFalse(self.staging.exists())
        fractions = [f for f, _, _ in seen]
        self.assertEqual(fractions, sorted(fractions))
        self.assertEqual(seen[-1], (1.0, self.model.total_bytes, self.model.total_bytes))
        self.assertGreater(len(seen), 5)
        self.assertTrue(all(total == self.model.total_bytes for _, _, total in seen))
        self.assertEqual({url.rsplit("/", 1)[1] for url, _ in server.requests}, set(FILES))
        self.assertTrue(all(url.startswith("https://huggingface.co/Systran/faster-whisper-tiny/resolve/abc123/")
                            for url, _ in server.requests))

    def hold_lock_in_another_process(self, seconds):
        """A separate process (another copy of the app) that holds the download lock for `seconds`."""
        code = ("import sys, time; from pathlib import Path; from video_converter import speech_models as sm\n"
                "from video_converter.locks import FileLock\n"
                "with FileLock(Path(sys.argv[1]) / sm.LOCK_NAME, timeout=10):\n"
                "    print('held', flush=True); time.sleep(float(sys.argv[2]))\n")
        proc = subprocess.Popen([sys.executable, "-c", code, str(self.root), str(seconds)], stdout=subprocess.PIPE,
                                text=True, cwd=Path(__file__).resolve().parents[1])
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=30)))
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def test_a_download_waits_for_another_copy_that_is_downloading(self):
        server, waited = FakeServer(), []
        other = self.hold_lock_in_another_process(1.5)
        started = time.monotonic()
        folder = sm.download_model(self.model, self.root, open_=server, on_wait=lambda: waited.append(1))
        self.assertEqual(waited, [1])
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        self.assertEqual(sm.verify_model(self.model, self.root), folder)
        other.wait(timeout=30)

    def test_a_download_that_waited_uses_the_model_the_other_copy_finished(self):
        server = FakeServer()
        other = self.hold_lock_in_another_process(1.0)
        # While this one waits, the other copy "finishes" the model (put in place from here, as it would have).
        for name, data in FILES.items():
            (self.root / "tiny").mkdir(exist_ok=True)
            (self.root / "tiny" / name).write_bytes(data)
        sm.download_model(self.model, self.root, open_=server, recheck=False, on_wait=lambda: None)
        self.assertEqual(server.requests, [], "downloaded again what the other copy had just fetched")
        other.wait(timeout=30)

    def test_the_wait_for_another_copy_can_be_cancelled(self):
        cancel, waited = threading.Event(), []
        other = self.hold_lock_in_another_process(30)
        started = time.monotonic()

        def on_wait():
            waited.append(1)
            threading.Timer(0.3, cancel.set).start()

        with self.assertRaises(ConversionCancelled):
            sm.download_model(self.model, self.root, open_=FakeServer(), cancel_event=cancel, on_wait=on_wait)
        self.assertEqual(waited, [1])
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(self.staging.exists())
        other.kill()
        other.wait(timeout=30)

    def test_a_wait_that_runs_out_says_another_copy_is_downloading(self):
        with FileLock(self.root / sm.LOCK_NAME, timeout=5), mock.patch.object(sm, "LOCK_WAIT", 0.3):
            with self.assertRaisesRegex(ConversionError, "Another copy of the app"):
                sm.download_model(self.model, self.root, open_=FakeServer())

    def test_the_lock_is_let_go_after_a_download_and_after_a_failure(self):
        sm.download_model(self.model, self.root, open_=FakeServer())
        with FileLock(self.root / sm.LOCK_NAME, timeout=0):
            pass
        with mock.patch.object(sm, "_download_model", side_effect=ConversionError("boom")):
            with self.assertRaises(ConversionError):
                sm.download_model(self.model, self.root, open_=FakeServer())
        with FileLock(self.root / sm.LOCK_NAME, timeout=0):
            pass

    def test_a_model_already_there_is_not_downloaded_again(self):
        server = FakeServer()
        sm.download_model(self.model, self.root, open_=server)
        server.requests.clear()
        sm.download_model(self.model, self.root, open_=server)
        self.assertEqual(server.requests, [])

    def test_a_damaged_model_on_disk_is_downloaded_again(self):
        server = FakeServer()
        folder = sm.download_model(self.model, self.root, open_=server)
        data = bytearray((folder / "model.bin").read_bytes())
        data[5] ^= 0x55
        (folder / "model.bin").write_bytes(bytes(data))
        server.requests.clear()
        sm.download_model(self.model, self.root, open_=server)
        self.assertTrue(server.requests)
        self.assertEqual(sm.verify_model(self.model, self.root), folder)

    def test_a_download_that_arrives_corrupted_is_rejected_and_not_kept(self):
        data = bytearray(FILES["model.bin"])
        data[7] ^= 0xFF
        server = FakeServer(tamper={"model.bin": bytes(data)})
        with self.assertRaisesRegex(ConversionError, "damaged"):
            sm.download_model(self.model, self.root, open_=server)
        self.assertFalse((self.root / "tiny").exists())
        self.assertFalse((self.staging / "model.bin.part").exists())
        self.assertFalse((self.staging / "model.bin").exists())

    def test_a_file_of_the_wrong_length_is_rejected(self):
        for wrong in (FILES["model.bin"][:-1], FILES["model.bin"] + b"x"):
            with self.subTest(length=len(wrong)):
                server = FakeServer(tamper={"model.bin": wrong})
                with self.assertRaisesRegex(ConversionError, "wrong size"):
                    sm.download_model(self.model, self.root, open_=server)
                self.assertFalse((self.root / "tiny").exists())

    def test_a_cancel_stops_the_download_and_keeps_the_part_for_later(self):
        cancel = threading.Event()
        seen = []

        def progress(fraction, done, total):
            seen.append(done)
            if done > 5000:
                cancel.set()

        with self.assertRaises(ConversionCancelled):
            sm.download_model(self.model, self.root, progress, cancel, FakeServer())
        self.assertFalse((self.root / "tiny").exists())
        self.assertTrue(any(self.staging.glob("*.part")))
        self.assertLess(max(seen), self.model.total_bytes)

    def test_a_download_that_was_cut_off_carries_on_where_it_stopped(self):
        cut = FakeServer(fail_after=3500)
        with self.assertRaisesRegex(ConversionError, "internet"):
            sm.download_model(self.model, self.root, open_=cut)
        part = self.staging / "model.bin.part"
        self.assertTrue(part.is_file())
        have = part.stat().st_size
        self.assertGreater(have, 0)
        server = FakeServer()
        folder = sm.download_model(self.model, self.root, open_=server)
        self.assertEqual(sm.verify_model(self.model, self.root), folder)
        ranged = [h["Range"] for url, h in server.requests if url.endswith("model.bin")]
        self.assertEqual(ranged, [f"bytes={have}-"])
        sent = sum(len(FILES[url.rsplit("/", 1)[1]]) for url, _ in server.requests)
        self.assertLess(sent, self.model.total_bytes)         # the part that was already there was not sent again

    def test_a_finished_file_from_an_earlier_try_is_not_fetched_again(self):
        self.staging.mkdir()
        (self.staging / "config.json").write_bytes(FILES["config.json"])
        server = FakeServer()
        sm.download_model(self.model, self.root, open_=server)
        self.assertNotIn("config.json", [url.rsplit("/", 1)[1] for url, _ in server.requests])

    def test_a_resume_is_checked_against_the_pinned_hash_so_a_bad_part_cannot_slip_in(self):
        self.staging.mkdir()
        (self.staging / "model.bin.part").write_bytes(b"\xff" * 4000)         # not the start of the real file
        with self.assertRaisesRegex(ConversionError, "damaged"):
            sm.download_model(self.model, self.root, open_=FakeServer())
        self.assertFalse((self.root / "tiny").exists())
        # The bad part was thrown away, so trying again works.
        folder = sm.download_model(self.model, self.root, open_=FakeServer())
        self.assertEqual(sm.verify_model(self.model, self.root), folder)

    def test_a_server_that_ignores_range_makes_the_file_start_again(self):
        self.staging.mkdir()
        (self.staging / "model.bin.part").write_bytes(FILES["model.bin"][:2000])
        server = FakeServer(honor_range=False)
        folder = sm.download_model(self.model, self.root, open_=server)
        self.assertEqual(sm.verify_model(self.model, self.root), folder)

    def test_a_part_longer_than_the_file_is_thrown_away(self):
        self.staging.mkdir()
        (self.staging / "model.bin.part").write_bytes(b"z" * (len(FILES["model.bin"]) + 50))
        folder = sm.download_model(self.model, self.root, open_=FakeServer())
        self.assertEqual(sm.verify_model(self.model, self.root), folder)

    def test_no_internet_gives_a_plain_message(self):
        def offline(url, headers):
            raise urllib.error.URLError("getaddrinfo failed")

        with self.assertRaises(ConversionError) as caught:
            sm.download_model(self.model, self.root, open_=offline)
        self.assertIn("internet", str(caught.exception))
        self.assertFalse((self.root / "tiny").exists())

    def test_a_timeout_gives_the_same_plain_message(self):
        def slow(url, headers):
            raise TimeoutError("timed out")

        with self.assertRaisesRegex(ConversionError, "internet"):
            sm.download_model(self.model, self.root, open_=slow)

    def test_a_server_error_names_the_code(self):
        server = FakeServer(files={"other": b"x"})          # everything asked for is a 404
        with self.assertRaisesRegex(ConversionError, "404"):
            sm.download_model(self.model, self.root, open_=server)

    def test_a_full_disk_while_writing_is_reported_as_such(self):
        class Full(Answer):
            def read(self, n=-1):
                raise OSError(errno.ENOSPC, "No space left on device")

        with self.assertRaisesRegex(ConversionError, "free disk space"):
            sm.download_model(self.model, self.root, open_=lambda url, headers: Full(b""))

    def test_not_enough_free_space_is_noticed_before_downloading(self):
        server = FakeServer()
        with mock.patch.object(sm, "_free_space", return_value=1000):
            with self.assertRaisesRegex(ConversionError, "free disk space"):
                sm.download_model(self.model, self.root, open_=server)
        self.assertEqual(server.requests, [])

    def test_the_answers_are_closed(self):
        answers = []

        def serve(url, headers):
            answers.append(FakeServer()(url, headers))
            return answers[-1]

        sm.download_model(self.model, self.root, open_=serve)
        self.assertTrue(answers and all(a.closed for a in answers))

    def test_models_status_lists_every_model(self):
        with tempfile.TemporaryDirectory() as empty:
            status = list(sm.models_status(Path(empty)))
        self.assertEqual([m.key for m, _ in status], ["fast", "balanced", "accurate"])
        self.assertFalse(any(ready for _, ready in status))



class SlowAnswer(Answer):
    """An answer whose bytes arrive only when the test lets them: the network is slow."""

    def __init__(self, data, entered, release):
        super().__init__(data)
        self.entered, self.release = entered, release

    def read(self, n=-1):
        self.entered.set()
        self.release.wait(30)
        return super().read(n)


class CancelledRunTests(unittest.TestCase):
    """A download that was cancelled may still be waiting on the network when the next one starts: whatever
    it receives late must go nowhere."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.model = tiny_model()
        self.staging = self.root / "tiny.downloading"
        patcher = mock.patch.object(sm, "CHUNK", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bytes_that_arrive_after_the_cancel_are_not_written(self):
        entered, release = threading.Event(), threading.Event()
        cancel = threading.Event()
        outcome = []

        def open_(url, headers):
            return SlowAnswer(FILES[url.rsplit("/", 1)[1]], entered, release)

        def run():
            try:
                sm.download_model(self.model, self.root, cancel_event=cancel, open_=open_)
                outcome.append("finished")
            except ConversionCancelled:
                outcome.append("cancelled")

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(10))
        cancel.set()                                  # the window is closed while the first bytes are on their way
        part = self.staging / "config.json.part"
        before = part.stat().st_size if part.exists() else 0
        release.set()                                 # ...and now they arrive
        thread.join(10)
        self.assertEqual(outcome, ["cancelled"])
        after = part.stat().st_size if part.exists() else 0
        self.assertEqual(after, before)
        self.assertFalse((self.root / "tiny").exists())

    def test_a_cancelled_run_does_not_publish_a_finished_file_or_the_folder(self):
        cancel = threading.Event()

        def progress(fraction, done, total):
            if done >= self.model.total_bytes:
                cancel.set()                         # cancelled just as the last byte is written

        with self.assertRaises(ConversionCancelled):
            sm.download_model(self.model, self.root, progress, cancel, FakeServer())
        self.assertFalse((self.root / "tiny").exists())

    def test_a_cancel_before_a_file_is_opened_leaves_the_part_alone(self):
        self.staging.mkdir()
        part = self.staging / "config.json.part"
        part.write_bytes(b"{")
        cancel = threading.Event()
        cancel.set()
        server = FakeServer()
        with self.assertRaises(ConversionCancelled):
            sm.download_model(self.model, self.root, cancel_event=cancel, open_=server)
        self.assertEqual(part.read_bytes(), b"{")
        self.assertEqual(server.requests, [])

    def test_repairing_a_damaged_model_keeps_the_files_that_are_fine(self):
        server = FakeServer()
        folder = sm.download_model(self.model, self.root, open_=server)
        data = bytearray((folder / "model.bin").read_bytes())
        data[9] ^= 0x33
        (folder / "model.bin").write_bytes(bytes(data))
        server.requests.clear()
        sm.download_model(self.model, self.root, open_=server)
        self.assertEqual({url.rsplit("/", 1)[1] for url, _ in server.requests}, {"model.bin"})
        self.assertEqual(sm.verify_model(self.model, self.root), folder)


if __name__ == "__main__":
    unittest.main()
