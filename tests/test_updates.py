"""Telling the user about a newer version: reading versions, the once-a-day check, and the request itself.
Nothing here uses the network: the request is always replaced."""

import io
import json
import unittest
import urllib.error
from unittest import mock

from video_converter import REPO_URL, __version__, updates
from video_converter.updates import MemoryStore, Release


class NoNetworkTestCase(unittest.TestCase):
    """A test that forgets to replace the request fails, instead of calling GitHub."""

    def setUp(self):
        patch = mock.patch("urllib.request.urlopen", side_effect=AssertionError("the network was used"))
        self.urlopen = patch.start()
        self.addCleanup(patch.stop)


class VersionTests(unittest.TestCase):
    def test_tags_are_read_with_or_without_the_v(self):
        self.assertEqual(updates.parse_version("v2.9.0"), ((2, 9), True))
        self.assertEqual(updates.parse_version("2.9.0"), ((2, 9), True))
        self.assertEqual(updates.parse_version("V10.0.1"), ((10, 0, 1), True))
        self.assertEqual(updates.parse_version(" v2.10 "), ((2, 10), True))

    def test_a_tag_with_something_after_the_numbers_is_not_a_release_yet(self):
        self.assertEqual(updates.parse_version("v2.9.1-rc1"), ((2, 9, 1), False))
        self.assertEqual(updates.parse_version("2.9.1beta"), ((2, 9, 1), False))

    def test_things_that_are_not_versions(self):
        for text in ("", "latest", "nightly-2024", "vX.Y", None):
            with self.subTest(text=text):
                self.assertIsNone(updates.parse_version(text))

    def test_newer_older_and_the_same(self):
        self.assertTrue(updates.is_newer("v2.10.0", "2.9.0"))        # numbers, not text: 10 is more than 9
        self.assertTrue(updates.is_newer("2.9.1", "2.9.0"))
        self.assertTrue(updates.is_newer("3.0.0", "2.9.9"))
        self.assertTrue(updates.is_newer("2.9.0.1", "2.9.0"))
        self.assertFalse(updates.is_newer("2.9.0", "2.9.0"))
        self.assertFalse(updates.is_newer("v2.9.0", "2.9.0"))
        self.assertFalse(updates.is_newer("2.9", "2.9.0"))            # the same version, spelled shorter
        self.assertFalse(updates.is_newer("2.8.9", "2.9.0"))
        self.assertFalse(updates.is_newer("1.99.99", "2.0.0"))
        self.assertFalse(updates.is_newer("2.9.0", "2.10.0"))

    def test_a_pre_release_is_older_than_its_release_but_newer_than_the_one_before(self):
        self.assertFalse(updates.is_newer("2.9.0-rc1", "2.9.0"))
        self.assertTrue(updates.is_newer("2.9.1-rc1", "2.9.0"))
        self.assertTrue(updates.is_newer("2.9.0", "2.9.0-rc1"))

    def test_garbage_is_never_newer(self):
        for text in ("", "latest", "banana"):
            self.assertFalse(updates.is_newer(text, "2.9.0"))
        self.assertFalse(updates.is_newer("3.0.0", "unknown"))

    def test_it_compares_with_this_apps_own_version_by_default(self):
        major, *rest = __version__.split(".")
        self.assertTrue(updates.is_newer(f"{int(major) + 1}.0.0"))
        self.assertFalse(updates.is_newer(__version__))
        self.assertFalse(updates.is_newer("0.0.1"))


class ReleaseTests(unittest.TestCase):
    def test_a_release_from_githubs_answer(self):
        release = updates.release_from({"tag_name": "v2.9.1", "html_url": f"{REPO_URL}/releases/tag/v2.9.1",
                                        "name": "Something", "body": "Lots of text"})
        self.assertEqual(release, Release("2.9.1", f"{REPO_URL}/releases/tag/v2.9.1"))

    def test_the_page_to_open_is_always_this_projects(self):
        for url in ("https://evil.example/download", "http://github.com/someone/else/releases", None, 7,
                    REPO_URL + "-fake/releases"):
            with self.subTest(url=url):
                release = updates.release_from({"tag_name": "v2.9.1", "html_url": url})
                self.assertEqual(release.url, f"{REPO_URL}/releases/latest")

    def test_answers_that_are_not_releases(self):
        for data in (None, [], "v2.9.1", {}, {"tag_name": 5}, {"tag_name": ""}, {"tag_name": "nightly"}):
            with self.subTest(data=data):
                self.assertIsNone(updates.release_from(data))


class RequestTests(NoNetworkTestCase):
    def answer(self, data, status=200):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = lambda size=-1: json.dumps(data).encode()[:size if size > 0 else None]
        self.urlopen.side_effect = None
        self.urlopen.return_value = response
        return response

    def test_it_is_a_plain_get_of_the_latest_release_with_the_apps_name_and_version(self):
        self.answer({"tag_name": "v2.9.1", "html_url": f"{REPO_URL}/releases/tag/v2.9.1"})
        release = updates.fetch_latest()
        self.assertEqual(release.version, "2.9.1")
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/Ahmed41022/free-video-editor/releases/latest")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers["user-agent"], f"Free-Video-Editor/{__version__}")
        self.assertEqual(set(headers), {"user-agent", "accept"})        # nothing else is sent: no cookies, no token
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], 5.0)

    def test_the_address_is_this_projects_repository(self):
        self.assertEqual(updates.API_URL, f"https://api.github.com/repos/Ahmed41022/free-video-editor/releases/latest")
        self.assertTrue(REPO_URL.endswith("Ahmed41022/free-video-editor"))

    def test_an_answer_that_is_not_json_or_not_a_release_is_an_error(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"<html>Rate limited</html>"
        self.urlopen.side_effect = None
        self.urlopen.return_value = response
        with self.assertRaises(ValueError):
            updates.fetch_latest()
        self.answer({"message": "Not Found"})
        with self.assertRaises(ValueError):
            updates.fetch_latest()

    def test_a_huge_answer_is_read_only_so_far(self):
        response = self.answer({"tag_name": "v2.9.1", "body": "x" * 10})
        updates.fetch_latest()
        response.read.assert_called_once_with(updates.MAX_BYTES)


class CheckTests(NoNetworkTestCase):
    NOW = 1_700_000_000.0

    def setUp(self):
        super().setUp()
        self.store = MemoryStore()
        self.calls = 0

    def fetch(self, version="99.0.0", error=None):
        def run():
            self.calls += 1
            if error:
                raise error
            return Release(version, f"{REPO_URL}/releases/tag/v{version}")
        return run

    def check(self, **kwargs):
        return updates.check_for_update(self.store, kwargs.pop("fetch", None) or self.fetch(), now=self.NOW, **kwargs)

    def test_a_newer_version_is_reported(self):
        release = self.check()
        self.assertEqual(release.version, "99.0.0")
        self.assertEqual(self.calls, 1)

    def test_the_same_or_an_older_version_is_not(self):
        self.assertIsNone(self.check(fetch=self.fetch(__version__)))
        self.store.set(updates.LAST_CHECK_KEY, 0.0)
        self.assertIsNone(self.check(fetch=self.fetch("1.0.0")))
        self.assertEqual(self.calls, 2)

    def test_a_failed_check_is_silent_and_logged(self):
        for error in (urllib.error.URLError("no network"), TimeoutError("slow"), OSError("reset"),
                      ValueError("not json"), urllib.error.HTTPError("u", 403, "rate limited", {}, io.BytesIO())):
            with self.subTest(error=type(error).__name__):
                self.store.set(updates.LAST_CHECK_KEY, 0.0)
                with self.assertLogs("video_converter.updates", level="INFO") as logged:
                    self.assertIsNone(self.check(fetch=self.fetch(error=error)))
                self.assertIn("Couldn't check for updates", logged.output[-1])

    def test_a_damaged_setting_is_not_a_problem_either(self):
        class Broken:
            def get(self, key, default):
                raise RuntimeError("the settings are damaged")

            def set(self, key, value):
                raise RuntimeError("and read-only")
        self.assertIsNone(updates.check_for_update(Broken(), self.fetch()))

    def test_it_asks_at_most_once_a_day(self):
        self.assertIsNotNone(self.check())
        self.assertEqual(self.store.get(updates.LAST_CHECK_KEY, None), self.NOW)
        later = lambda seconds: updates.check_for_update(self.store, self.fetch(), now=self.NOW + seconds)
        self.assertIsNone(later(60))
        self.assertIsNone(later(23 * 3600))
        self.assertEqual(self.calls, 1)                                   # neither of those asked
        self.assertIsNotNone(later(24 * 3600 + 1))
        self.assertEqual(self.calls, 2)

    def test_a_check_that_was_skipped_says_so_in_the_log(self):
        self.store.set(updates.LAST_CHECK_KEY, self.NOW - 10)
        with self.assertLogs("video_converter.updates", level="INFO") as logged:
            self.assertIsNone(self.check())
        self.assertIn("less than a day ago", logged.output[0])

    def test_a_failed_attempt_counts_as_the_days_check(self):
        self.assertIsNone(self.check(fetch=self.fetch(error=OSError("offline"))))
        self.assertEqual(self.store.get(updates.LAST_CHECK_KEY, None), self.NOW)
        self.assertIsNone(self.check())
        self.assertEqual(self.calls, 1)

    def test_a_clock_that_went_backwards_does_not_block_checks_forever(self):
        self.store.set(updates.LAST_CHECK_KEY, self.NOW + 10 * 24 * 3600)
        self.assertIsNotNone(self.check())

    def test_a_first_run_has_no_last_check(self):
        self.assertIsNone(self.store.get(updates.LAST_CHECK_KEY, None))
        self.assertIsNotNone(self.check())

    def test_nothing_is_asked_when_the_setting_is_off(self):
        self.store.set(updates.ENABLED_KEY, False)
        self.assertIsNone(self.check())
        self.assertEqual(self.calls, 0)
        self.assertIsNone(self.store.get(updates.LAST_CHECK_KEY, None))

    def test_it_is_on_unless_the_user_turned_it_off(self):
        self.assertTrue(MemoryStore().get(updates.ENABLED_KEY, True))
        self.assertIsNotNone(self.check())

    def test_a_skipped_version_is_not_reported_again_but_a_newer_one_is(self):
        self.store.set(updates.SKIPPED_KEY, "99.0.0")
        self.assertIsNone(self.check())
        self.store.set(updates.LAST_CHECK_KEY, 0.0)
        self.assertEqual(self.check(fetch=self.fetch("99.0.1")).version, "99.0.1")

    def test_the_log_says_what_was_found(self):
        with self.assertLogs("video_converter.updates", level="INFO") as logged:
            self.check()
        self.assertIn("99.0.0 is out", logged.output[0])
        self.assertIn(__version__, logged.output[0])
        self.store.set(updates.LAST_CHECK_KEY, 0.0)
        with self.assertLogs("video_converter.updates", level="INFO") as logged:
            self.check(fetch=self.fetch(__version__))
        self.assertIn("is the latest", logged.output[0])
        self.store.set(updates.LAST_CHECK_KEY, 0.0)
        self.store.set(updates.SKIPPED_KEY, "99.0.0")
        with self.assertLogs("video_converter.updates", level="INFO") as logged:
            self.check()
        self.assertIn("skipped", logged.output[0])

    def test_the_default_request_is_the_real_one_and_goes_through_the_network_function(self):
        with mock.patch.object(updates, "fetch_latest", return_value=Release("99.0.0", REPO_URL)) as fetch:
            self.assertEqual(updates.check_for_update(self.store, now=self.NOW).version, "99.0.0")
        fetch.assert_called_once_with()


class MemoryStoreTests(unittest.TestCase):
    def test_it_keeps_a_copy(self):
        original = {"a": 1}
        store = MemoryStore(original)
        store.set("a", 2)
        store.set("b", 3)
        self.assertEqual(original, {"a": 1})
        self.assertEqual((store.get("a", 0), store.get("b", 0), store.get("c", 7)), (2, 3, 7))


if __name__ == "__main__":
    unittest.main()
