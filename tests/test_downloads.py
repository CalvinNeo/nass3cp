import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nass3cp import client
from nass3cp.downloads import DownloadError, DownloadManager, safe_filename
from nass3cp.errors import DownloadCancelled
from test_client import PipelineDownloadApi, fake_data_request


class BrowserDownloadApi(PipelineDownloadApi):
    def __init__(self, contents):
        super().__init__(b"")
        self.contents = contents
        self.transfer_id = secrets.token_hex(16)

    def path_info(self, path):
        value = self.contents[path]
        return {"exists": True, "type": value if isinstance(value, str) else "file",
                "size": None if isinstance(value, str) else len(value), "mtime_ns": 0}

    def create_download(self, path, inflight):
        self.remote_content = self.contents[path]
        return super().create_download(path, inflight)

    def urls(self, *args):
        values = super().urls(*args)
        for value in values:
            value["fake_api"] = self
        return values


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.contents = {"/share/报告.txt": "测试内容\n".encode("utf-8"),
                         "/share/second.bin": b"abcdefghijklmnop", "/share/empty.txt": b"",
                         "/share/folder": "directory", "/share/link": "other"}
        self.apis, self.managers, self.targets = [], [], []
        self.before_chunk = None
        self.corrupt = False

        def data_request(method, item, data=None, expected=None, attempts=4, progress=None, cancel_event=None):
            if self.before_chunk:
                self.before_chunk(cancel_event, progress)
            client._check_download_cancelled(cancel_event)
            value = fake_data_request(item["fake_api"])(method, item, data, expected, attempts, progress)
            return b"x" * len(value) if self.corrupt else value

        def record_download(*args, **kwargs):
            self.targets.append(Path(args[2]))
            return client.download(*args, **kwargs)

        self.data_patch = patch("nass3cp.client._data_request", side_effect=data_request)
        self.download_patch = patch("nass3cp.downloads.download", side_effect=record_download)
        self.data_patch.start()
        self.download_patch.start()

    def tearDown(self):
        for manager in self.managers:
            manager.stop()
        self.data_patch.stop()
        self.download_patch.stop()
        for target in self.targets:
            self.assertFalse(target.parent.exists(), "temporary download directory was not cleaned")

    def factory(self):
        api = BrowserDownloadApi(self.contents)
        self.apis.append(api)
        return api

    def manager(self, **kwargs):
        manager = DownloadManager(self.factory, **kwargs)
        self.managers.append(manager)
        return manager

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail("download condition timed out")

    def item_with_status(self, manager, index, status):
        return self.wait_for(lambda: (manager.state()["items"][index]
                                     if manager.state()["items"][index]["status"] == status else None))

    def save(self, manager, item):
        record, handle = manager.open_file(item["id"])
        with handle:
            value = handle.read()
            manager.sent_bytes(record, len(value))
        manager.finish_sending(record, True)
        return value

    def test_serial_queue_uses_verified_pipeline_and_holds_slot_until_browser_receives_file(self):
        manager = self.manager()
        manager.enqueue({"paths": ["/share/报告.txt", "/share/second.bin"]})
        first = self.item_with_status(manager, 0, "ready")
        self.assertEqual(first["received"], len(self.contents[first["path"]]))
        self.assertEqual(manager.state()["concurrency"], 1)
        self.assertEqual(manager.state()["items"][1]["status"], "queued")
        self.assertEqual(len(self.apis), 1)
        self.assertTrue(self.apis[0].acknowledged)
        self.assertLessEqual(self.apis[0].max_objects, 3)
        self.assertFalse(self.apis[0].objects)
        self.assertEqual(self.targets[0].suffix, ".nass3cp-part")
        self.assertEqual(self.save(manager, first), self.contents[first["path"]])
        second = self.item_with_status(manager, 1, "ready")
        self.assertEqual(manager.state()["items"][0]["status"], "complete")
        self.assertEqual(self.save(manager, second), self.contents[second["path"]])

    def test_two_files_can_be_active_while_third_waits(self):
        manager = self.manager(concurrency=2)
        manager.enqueue({"paths": list(self.contents)[:3]})
        self.item_with_status(manager, 0, "ready")
        self.item_with_status(manager, 1, "ready")
        self.assertEqual(len(self.apis), 2)
        self.assertEqual(manager.state()["items"][2]["status"], "queued")
        manager.cancel(manager.state()["items"][0]["id"])
        self.item_with_status(manager, 0, "cancelled")
        empty = self.item_with_status(manager, 2, "ready")
        self.assertEqual(self.save(manager, empty), b"")

    def test_cancel_queued_file_never_starts_a_nas_transfer(self):
        manager = self.manager()
        manager.enqueue({"paths": list(self.contents)[:2]})
        first = self.item_with_status(manager, 0, "ready")
        second = manager.state()["items"][1]
        manager.cancel(second["id"])
        self.save(manager, first)
        self.assertEqual(manager.state()["items"][1]["status"], "cancelled")
        self.assertEqual(len(self.apis), 1)
        self.assertFalse(manager._queue)

    def test_progress_is_per_file_and_active_cancellation_aborts_and_cleans_partial_file(self):
        entered = threading.Event()

        def blocked(event, progress):
            progress(2)
            entered.set()
            if not event.wait(5):
                raise AssertionError("cancel did not interrupt chunk download")

        self.before_chunk = blocked
        manager = self.manager(jobs=1)
        manager.enqueue({"paths": list(self.contents)[:2]})
        self.assertTrue(entered.wait(3))
        first, second = manager.state()["items"]
        self.assertEqual(first["status"], "downloading")
        self.assertGreater(first["received"], 0)
        self.assertLess(first["received"], first["size"])
        self.assertEqual(second["received"], 0)
        self.assertEqual(second["status"], "queued")
        manager.cancel(second["id"])
        manager.cancel(first["id"])
        self.item_with_status(manager, 0, "cancelled")
        self.assertTrue(self.apis[0].aborted)
        self.wait_for(lambda: not self.targets[0].parent.exists())

    def test_bad_checksum_cannot_be_saved_and_later_files_can_continue(self):
        self.corrupt = True
        manager = self.manager()
        manager.enqueue({"paths": ["/share/报告.txt"]})
        failed = self.item_with_status(manager, 0, "failed")
        self.assertIn("SHA-256 mismatch", failed["error"])
        self.assertTrue(self.apis[0].aborted)
        with self.assertRaises(DownloadError) as caught:
            manager.open_file(failed["id"])
        self.assertEqual(caught.exception.status, 409)
        self.corrupt = False
        manager.enqueue({"paths": ["/share/second.bin"]})
        ready = self.item_with_status(manager, 1, "ready")
        self.assertEqual(self.save(manager, ready), self.contents[ready["path"]])

    def test_folders_and_nonregular_files_are_rejected_without_a_transfer(self):
        manager = self.manager()
        manager.enqueue({"paths": ["/share/folder", "/share/link"]})
        self.item_with_status(manager, 0, "failed")
        self.item_with_status(manager, 1, "failed")
        self.assertTrue(all(not api.current for api in self.apis))
        self.assertFalse(self.targets)

    def test_interrupted_browser_stream_keeps_verified_cache_for_manual_retry(self):
        manager = self.manager()
        manager.enqueue({"paths": ["/share/报告.txt"]})
        ready = self.item_with_status(manager, 0, "ready")
        item, handle = manager.open_file(ready["id"])
        with handle:
            with self.assertRaises(DownloadError):
                manager.open_file(ready["id"])
            manager.sent_bytes(item, len(handle.read(2)))
        manager.finish_sending(item, False)
        retry = self.item_with_status(manager, 0, "ready")
        self.assertIn("interrupted", retry["error"])
        self.assertEqual(retry["sent"], 2)
        self.assertEqual(self.save(manager, retry), self.contents[ready["path"]])
        self.assertEqual(len(self.apis), 1)

    def test_ready_timeout_removes_temporary_file_and_frees_queue_slot(self):
        manager = self.manager(ready_timeout=0.01)
        manager.enqueue({"paths": list(self.contents)[:2]})
        self.item_with_status(manager, 0, "expired")
        self.item_with_status(manager, 1, "expired")
        self.wait_for(lambda: all(not target.parent.exists() for target in self.targets))

    def test_queue_validates_paths_deduplicates_active_files_and_enforces_capacity(self):
        manager = self.manager()
        for paths in ([], "file", [None], [""], ["/share/"], ["a\x00b"], ["a" * 8193]):
            with self.subTest(paths=paths), self.assertRaises(DownloadError):
                manager.enqueue({"paths": paths})
        with patch("nass3cp.downloads.MAX_FILES", 2):
            manager.enqueue({"paths": ["/share/报告.txt"] * 2})
            manager.enqueue({"paths": ["/share/报告.txt", "/share/second.bin"]})
            self.assertEqual(len(manager.state()["items"]), 2)
            with self.assertRaises(DownloadError) as caught:
                manager.enqueue({"paths": ["/share/empty.txt"]})
            self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(DownloadError) as caught:
            manager.open_file("f" * 32)
        self.assertEqual(caught.exception.status, 404)

    def test_insufficient_local_space_does_not_start_transfer(self):
        manager = self.manager()
        with patch("nass3cp.downloads.shutil.disk_usage") as usage:
            usage.return_value.free = 0
            manager.enqueue({"paths": ["/share/报告.txt"]})
            failed = self.item_with_status(manager, 0, "failed")
        self.assertIn("disk space", failed["error"])
        self.assertFalse(self.apis[0].current)

    def test_filename_keeps_unicode_but_cannot_be_a_path_or_header(self):
        self.assertEqual(safe_filename("/share/中文 & #.txt"), "中文 & #.txt")
        self.assertEqual(safe_filename('/share/bad\\name"\r\n.txt'), "bad_name___.txt")
        self.assertEqual(safe_filename("/share/CON.txt"), "_CON.txt")
        self.assertEqual(safe_filename("/share/.."), "download")
        self.assertEqual(safe_filename("/share/\udcff.txt"), "_.txt")
        self.assertLessEqual(len(safe_filename("/share/" + "中" * 200 + ".txt").encode("utf-8")), 240)

    def test_callback_can_cancel_before_first_chunk_without_leaking_temp_handle(self):
        api = BrowserDownloadApi(self.contents)

        def cancelled(*args):
            raise DownloadCancelled("cancelled")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "saved.txt"
            with self.assertRaises(DownloadCancelled):
                client.download(api, "/share/报告.txt", str(target), False, 1, 5, True,
                                progress_callback=cancelled)
            self.assertFalse(list(Path(directory).iterdir()))
        self.assertTrue(api.aborted)

    def test_native_data_request_obeys_cancellation_without_retrying(self):
        event = threading.Event()
        event.set()
        # Call the real function, not the fixture's fake S3 adapter.
        self.data_patch.stop()
        with patch("nass3cp.client._data_opener") as opener, self.assertRaises(DownloadCancelled):
            client._data_request("GET", {"url": "https://s3.example.test/file", "headers": {}}, cancel_event=event)
        opener.assert_not_called()
        self.data_patch.start()

    def test_api_clone_preserves_tls_and_credentials_with_a_separate_opener(self):
        api = client.ApiClient("https://nas.example.test", "fixture-password", insecure=True, timeout=8)
        cloned = api.clone()
        self.assertIs(cloned.context, api.context)
        self.assertIsNot(cloned.opener, api.opener)
        self.assertEqual((cloned.base_url, cloned.password, cloned.timeout), (api.base_url, api.password, 8))


if __name__ == "__main__":
    unittest.main()
