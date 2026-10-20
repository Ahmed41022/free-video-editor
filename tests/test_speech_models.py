"""The speech models for automatic captions: the pinned manifest, checking a model on disk, and the
download manager (against a fake server, never the network)."""

import errno
import hashlib
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from video_converter import speech_models as sm
from video_converter.core import ConversionCancelled, ConversionError


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


if __name__ == "__main__":
    unittest.main()
