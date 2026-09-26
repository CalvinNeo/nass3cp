import tempfile
import time
import unittest
from pathlib import Path

from nass3cp.config import SearchConfig
from nass3cp.search import ACTIVE_STATUSES, SearchError, SearchManager, _Pacer


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.manager = SearchManager(SearchConfig(entries_per_second=500), self.resolve)

    def tearDown(self):
        self.manager.stop()
        self.temporary.cleanup()

    def resolve(self, value):
        path = (self.root / value).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise SearchError(403, "path_not_allowed", "outside allowed roots")
        if not path.is_dir():
            raise SearchError(404, "not_found", "folder missing")
        return path

    def wait_for_end(self, identifier, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.manager.state(identifier)
            if value["status"] not in ACTIVE_STATUSES:
                return value
            time.sleep(0.02)
        self.fail("search did not finish")

    def start(self, pattern, **options):
        return self.manager.start(dict(path=".", pattern=pattern, **options))["id"]

    def test_unicode_regex_recurses_and_returns_file_metadata(self):
        (self.root / "子目录").mkdir()
        (self.root / "报告2026.PDF").write_bytes(b"abc")
        (self.root / "子目录" / "报告2025.pdf").write_bytes(b"12345")
        (self.root / "子目录" / "notes.txt").write_text("not a filename match")
        value = self.wait_for_end(self.start(r"报告\d{4}\.pdf$", regex=True))
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["estimated_percent"], 100)
        self.assertEqual(value["scanned_entries"], 4)
        self.assertEqual(value["scanned_files"], 3)
        self.assertEqual(value["directories_completed"], 2)
        self.assertEqual({item["relative_path"] for item in value["results"]}, {"报告2026.PDF", "子目录/报告2025.pdf"})
        self.assertEqual({item["size"] for item in value["results"]}, {3, 5})
        for item in value["results"]:
            self.assertEqual(item["type"], "file")
            self.assertIn("birthtime_ns", item)
            self.assertEqual(Path(item["path"]).parent.as_posix(), item["parent_path"])

    def test_literal_metacharacters_and_case_sensitive_option(self):
        (self.root / "A[1].TXT").touch()
        (self.root / "A1.txt").touch()
        value = self.wait_for_end(self.start("a[1]"))
        self.assertEqual([entry["name"] for entry in value["results"]], ["A[1].TXT"])
        value = self.wait_for_end(self.start("a[1]", case_sensitive=True))
        self.assertEqual(value["results"], [])

    def test_invalid_pattern_fails_before_scanning(self):
        value = self.wait_for_end(self.start("[", regex=True))
        self.assertEqual(value["status"], "failed")
        self.assertIn("Invalid regular expression", value["error"])
        self.assertEqual(value["scanned_entries"], 0)

    def test_catastrophic_regex_is_terminated_and_next_search_can_run(self):
        (self.root / ("a" * 120 + "!" )).touch()
        value = self.wait_for_end(self.start("(a+)+$", regex=True), timeout=5)
        self.assertEqual(value["status"], "failed")
        self.assertIn("100 ms time limit", value["error"])
        value = self.wait_for_end(self.start("!"))
        self.assertEqual(value["results_count"], 1)

    def test_global_concurrency_limit_and_cancel(self):
        self.manager.settings = SearchConfig(entries_per_second=1)
        for index in range(10):
            (self.root / ("file%d" % index)).touch()
        identifier = self.start("file")
        with self.assertRaises(SearchError) as error:
            self.start("another")
        self.assertEqual(error.exception.status, 409)
        self.manager.cancel(identifier)
        result = self.wait_for_end(identifier)
        self.assertEqual(result["status"], "cancelled")
        self.assertNotEqual(result["estimated_percent"], 100)

    def test_browser_abandonment_stops_the_scan(self):
        self.manager.idle_timeout = 0.25
        self.manager.settings = SearchConfig(entries_per_second=1)
        (self.root / "file").touch()
        identifier = self.start("file")
        time.sleep(0.6)
        result = self.wait_for_end(identifier)
        self.assertEqual(result["status"], "cancelled")
        self.assertIn("no browser", result["error"])

    def test_progress_is_live_and_result_pages_do_not_restart_scan(self):
        self.manager.settings = SearchConfig(entries_per_second=20)
        (self.root / "empty").mkdir()
        for index in range(20):
            (self.root / ("file%d.txt" % index)).touch()
        identifier = self.start("file")
        samples = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = self.manager.state(identifier)
            samples.append(value["scanned_entries"])
            if value["status"] not in ACTIVE_STATUSES:
                break
            self.assertNotEqual(value["estimated_percent"], 100)
            if value["estimated_percent"] is not None:
                # A completed empty folder must not make the open, larger folder
                # appear nearly finished while its total size is still unknown.
                self.assertLess(value["estimated_percent"], 90)
            time.sleep(0.05)
        self.assertEqual(value["status"], "completed")
        self.assertGreater(len(set(samples)), 3)
        self.assertEqual(samples, sorted(samples))
        first = self.manager.state(identifier, 0, 7)
        second = self.manager.state(identifier, first["next_cursor"], 7)
        self.assertEqual(len(first["results"]), 7)
        self.assertEqual(len(second["results"]), 7)
        self.assertNotEqual(first["results"][0]["path"], second["results"][0]["path"])
        self.assertEqual(second["scanned_entries"], 21)

    def test_result_limit_bounds_memory_and_reports_partial_results(self):
        self.manager.settings = SearchConfig(entries_per_second=500, max_results=2)
        for index in range(5):
            (self.root / ("file%d" % index)).touch()
        value = self.wait_for_end(self.start("file"))
        self.assertEqual(value["status"], "limited")
        self.assertEqual(value["results_count"], 2)
        self.assertNotEqual(value["estimated_percent"], 100)
        self.assertIn("limit", value["error"])

    def test_symlinks_are_skipped_including_links_outside_the_search_root(self):
        (self.root / "inside").mkdir()
        (self.root / "inside" / "match.txt").touch()
        try:
            (self.root / "outside").symlink_to(self.root.parent, target_is_directory=True)
            (self.root / "loop").symlink_to(self.root, target_is_directory=True)
            (self.root / "linked.txt").symlink_to(self.root / "inside" / "match.txt")
        except OSError as error:
            self.skipTest(str(error))
        value = self.wait_for_end(self.start("txt"))
        self.assertEqual([item["relative_path"] for item in value["results"]], ["inside/match.txt"])
        self.assertEqual(value["skipped_entries"], 3)

    def test_outside_paths_invalid_parameters_and_expired_jobs_are_rejected(self):
        with self.assertRaises(SearchError) as error:
            self.manager.start({"path": "..", "pattern": "file"})
        self.assertEqual(error.exception.status, 403)
        for body in ({"path": ".", "pattern": ""}, {"path": ".", "pattern": "x" * 513},
                     {"path": ".", "pattern": "x", "regex": "true"}):
            with self.assertRaises(SearchError):
                self.manager.start(body)
        identifier = self.start("missing")
        self.wait_for_end(identifier)
        self.manager.retention = 0
        self.manager._jobs[identifier].finished_at -= 1
        with self.assertRaises(SearchError) as error:
            self.manager.state(identifier)
        self.assertEqual(error.exception.status, 404)

    def test_pacing_prevents_catchup_bursts_after_slow_io(self):
        clock = [0.0]
        sleeps = []

        def sleep(delay):
            sleeps.append(delay)
            clock[0] += delay

        pacer = _Pacer(50, clock=lambda: clock[0], sleep=sleep)
        for _ in range(51):
            pacer.wait()
        self.assertAlmostEqual(clock[0], 1.0)
        clock[0] += 5  # A slow operation does not earn a burst of future operations.
        pacer.wait()
        pacer.wait()
        self.assertAlmostEqual(sleeps[-1], 0.02)


if __name__ == "__main__":
    unittest.main()
