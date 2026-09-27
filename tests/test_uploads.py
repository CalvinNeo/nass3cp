import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nass3cp import client
from nass3cp.errors import ProtocolError, UploadCancelled
from nass3cp.uploads import UploadError, UploadManager
from test_client import PipelineUploadApi, fake_data_request


class BrowserUploadApi(PipelineUploadApi):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.path = None
        self.mtime_ns = None

    def path_info(self, path):
        if path == "/share":
            return {"exists": True, "type": "directory"}
        return {"exists": path in self.owner.saved, "type": "file"}

    def create_upload(self, path, size, mtime_ns, overwrite, inflight=None):
        if path in self.owner.saved:
            raise ProtocolError("destination exists")
        if overwrite:
            raise AssertionError("browser uploads must preserve existing files")
        self.path, self.mtime_ns = path, mtime_ns
        value = super().create_upload(path, size, mtime_ns, overwrite, inflight)
        if self.owner.on_create:
            self.owner.on_create()
        return value

    def urls(self, *args):
        items = super().urls(*args)
        for item in items:
            item["fake_api"] = self
        return items

    def commit(self, *args):
        if self.owner.fail_commit:
            raise ProtocolError("SHA-256 mismatch on NAS")
        value = super().commit(*args)
        self.owner.saved[self.path] = b"".join(self.received[index] for index in sorted(self.received))
        return value

    def abort(self, identifier):
        super().abort(identifier)
        self.objects.clear()


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.saved, self.apis, self.managers = {}, [], []
        self.before_chunk = self.on_create = None
        self.fail_commit = False
        self.staging = tempfile.TemporaryDirectory()
        self.addCleanup(self.staging.cleanup)
        temporary_directory = tempfile.TemporaryDirectory
        self.temp_patch = patch("nass3cp.uploads.tempfile.TemporaryDirectory",
                                side_effect=lambda **kwargs: temporary_directory(dir=self.staging.name, **kwargs))

        def data_request(method, item, data=None, expected=None, attempts=4, progress=None, cancel_event=None):
            if progress:
                progress(1)
            if self.before_chunk:
                self.before_chunk(cancel_event)
            client._check_upload_cancelled(cancel_event)
            return fake_data_request(item["fake_api"])(method, item, data, expected, attempts, progress)

        self.data_patch = patch("nass3cp.client._data_request", side_effect=data_request)
        self.temp_patch.start()
        self.data_patch.start()

    def tearDown(self):
        for manager in self.managers:
            manager.stop()
        self.data_patch.stop()
        self.temp_patch.stop()
        self.assertFalse(list(Path(self.staging.name).iterdir()), "upload staging was not cleaned")

    def factory(self):
        api = BrowserUploadApi(self)
        self.apis.append(api)
        return api

    def manager(self):
        manager = UploadManager(self.factory, jobs=1, inflight=2, transfer_timeout=5)
        self.managers.append(manager)
        return manager

    def enqueue(self, manager, name="报告 & #.txt", size=12):
        return manager.enqueue({"path": "/share", "files": [
            {"name": name, "size": size, "mtime_ms": 1_700_000_000_123},
        ]})["enqueued_ids"][0]

    def wait_for(self, manager, identifier, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = next(item for item in manager.state()["items"] if item["id"] == identifier)
            if item["status"] == status:
                return item
            time.sleep(0.01)
        self.fail("upload did not reach %s: %r" % (status, manager.state()))

    def test_multiple_files_use_one_staging_slot_with_progress_and_verified_pipeline(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(cancel_event):
            entered.set()
            self.assertTrue(release.wait(4))

        self.before_chunk = blocked
        manager = self.manager()
        first = self.enqueue(manager)
        empty = self.enqueue(manager, "empty.bin", 0)
        manager.receive(first, io.BytesIO(b"abcdefghijkl"), 12)
        try:
            self.assertTrue(entered.wait(3))
            snapshot = manager.state()
            self.assertTrue(snapshot["busy"])
            self.assertEqual(snapshot["items"][0]["status"], "uploading")
            self.assertEqual(snapshot["items"][0]["sent"], 1)
            self.assertEqual(snapshot["items"][1]["status"], "waiting")
            with self.assertRaises(UploadError) as caught:
                manager.receive(empty, io.BytesIO(b""), 0)
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(len(list(Path(self.staging.name).iterdir())), 1)
        finally:
            release.set()
        self.wait_for(manager, first, "complete")
        self.before_chunk = None
        manager.receive(empty, io.BytesIO(b""), 0)
        self.wait_for(manager, empty, "complete")
        self.assertEqual(self.saved, {"/share/报告 & #.txt": b"abcdefghijkl", "/share/empty.bin": b""})
        transfers = [api for api in self.apis if api.path]
        self.assertEqual(len(transfers), 2)
        self.assertTrue(all(api.mtime_ns == 1_700_000_000_123_000_000 for api in transfers))
        self.assertTrue(all(api.max_objects <= 2 and not api.objects for api in transfers))

    def test_invalid_metadata_cannot_escape_a_folder_or_add_partial_batches(self):
        manager = self.manager()
        invalid = [
            {"name": name, "size": 1} for name in ("../x", "..", "/tmp/file", "a/b", "a\\b", "", "x\x00", "x\n", "\udcff")
        ] + [{"name": "x", "size": value} for value in (-1, True, "1", 2**53)]
        invalid += [{"name": "x", "size": 1, "mtime_ms": value} for value in (-1, True, 2**53)]
        for record in invalid:
            with self.subTest(record=record), self.assertRaises(UploadError):
                manager.enqueue({"path": "/share", "files": [{"name": "valid", "size": 1}, record]})
        for body in ({"path": "", "files": []}, {"path": "/share", "files": "x"},
                     {"path": "/share", "files": [{"name": "x", "size": 1}], "overwrite": True}):
            with self.assertRaises(UploadError):
                manager.enqueue(body)
        self.assertEqual(manager.state()["items"], [])
        self.assertEqual(self.apis, [])

    def test_reselection_reuses_waiting_metadata_and_queue_capacity_is_bounded(self):
        manager = self.manager()
        with patch("nass3cp.uploads.MAX_FILES", 2):
            first = self.enqueue(manager)
            self.assertEqual(self.enqueue(manager), first)
            self.enqueue(manager, "second", 1)
            with self.assertRaises(UploadError):
                self.enqueue(manager, "third", 1)
            manager.cancel(first)
            self.enqueue(manager, "third", 1)
            self.assertEqual(len(manager.state()["items"]), 2)
        with self.assertRaises(UploadError) as caught:
            manager.cancel("f" * 32)
        self.assertEqual(caught.exception.status, 404)

    def test_wrong_length_and_interrupted_body_never_start_a_nas_transfer(self):
        manager = self.manager()
        identifier = self.enqueue(manager)
        with self.assertRaises(UploadError):
            manager.receive(identifier, io.BytesIO(b"short"), 5)
        self.assertEqual(manager.state()["items"][0]["status"], "waiting")
        with self.assertRaises(UploadError):
            manager.receive(identifier, io.BytesIO(b"short"), 12)
        item = self.wait_for(manager, identifier, "failed")
        self.assertIn("interrupted", item["error"])
        self.assertFalse(any(api.path for api in self.apis))
        self.assertFalse(manager.state()["busy"])

    def test_existing_destination_and_missing_folder_fail_before_reading_local_bytes(self):
        manager = self.manager()
        self.saved["/share/exists"] = b"original"
        identifier = self.enqueue(manager, "exists", 1)
        stream = io.BytesIO(b"x")
        with self.assertRaises(UploadError) as caught:
            manager.receive(identifier, stream, 1)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(stream.tell(), 0)
        self.assertEqual(self.saved["/share/exists"], b"original")
        missing = manager.enqueue({"path": "/missing", "files": [{"name": "x", "size": 1}]})["enqueued_ids"][0]
        with self.assertRaises(UploadError):
            manager.receive(missing, stream, 1)
        self.assertFalse(any(api.path for api in self.apis))

    def test_insufficient_temporary_space_fails_without_copying(self):
        manager = self.manager()
        identifier = self.enqueue(manager)
        with patch("nass3cp.uploads.shutil.disk_usage") as usage:
            usage.return_value.free = 0
            with self.assertRaises(UploadError) as caught:
                manager.receive(identifier, io.BytesIO(b"abcdefghijkl"), 12)
        self.assertEqual(caught.exception.status, 507)
        self.assertIn("disk space", self.wait_for(manager, identifier, "failed")["error"])

    def test_cancellation_during_local_copy_cleans_staging_and_releases_slot(self):
        manager = self.manager()
        identifier = self.enqueue(manager)

        class CancellingStream(io.BytesIO):
            def read1(self, size):
                manager.cancel(identifier)
                return super().read1(size)

        with self.assertRaises(UploadError):
            manager.receive(identifier, CancellingStream(b"abcdefghijkl"), 12)
        self.wait_for(manager, identifier, "cancelled")
        self.assertFalse(manager.state()["busy"])
        self.assertFalse(any(api.path for api in self.apis))

    def test_active_cancellation_and_shutdown_abort_nas_and_clean_local_files(self):
        for shutdown in (False, True):
            entered = threading.Event()

            def blocked(event):
                entered.set()
                self.assertTrue(event.wait(4))

            self.before_chunk = blocked
            manager = self.manager()
            identifier = self.enqueue(manager)
            waiting = self.enqueue(manager, "waiting", 1)
            manager.receive(identifier, io.BytesIO(b"abcdefghijkl"), 12)
            self.assertTrue(entered.wait(3))
            if shutdown:
                manager.stop()
            else:
                manager.cancel(waiting)
                manager.cancel(identifier)
            self.wait_for(manager, identifier, "cancelled")
            self.wait_for(manager, waiting, "cancelled")
            self.assertFalse(self.saved)
            self.assertTrue(any(api.aborted for api in self.apis))

    def test_cancel_racing_with_transfer_creation_still_aborts_created_transfer(self):
        manager = self.manager()
        identifier = self.enqueue(manager)
        self.on_create = lambda: manager.cancel(identifier)
        manager.receive(identifier, io.BytesIO(b"abcdefghijkl"), 12)
        self.wait_for(manager, identifier, "cancelled")
        self.assertTrue(any(api.path and api.aborted for api in self.apis))
        self.assertFalse(self.saved)

    def test_nas_checksum_failure_is_not_completion_and_next_file_can_proceed(self):
        manager = self.manager()
        first = self.enqueue(manager)
        self.fail_commit = True
        manager.receive(first, io.BytesIO(b"abcdefghijkl"), 12)
        failed = self.wait_for(manager, first, "failed")
        self.assertIn("SHA-256", failed["error"])
        self.assertFalse(self.saved)
        self.fail_commit = False
        second = self.enqueue(manager, "second", 1)
        manager.receive(second, io.BytesIO(b"x"), 1)
        self.wait_for(manager, second, "complete")
        self.assertEqual(self.saved["/share/second"], b"x")

    def test_progress_callback_cancellation_before_commit_cannot_save_on_nas(self):
        api = self.factory()
        event = threading.Event()

        def progress(label, completed, total):
            if label == "verify upload":
                event.set()

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.write_bytes(b"data")
            with self.assertRaises(UploadCancelled):
                client.upload(api, str(source), "/share/file", False, 1, 5, True,
                              progress_callback=progress, cancel_event=event)
        self.assertTrue(api.aborted)
        self.assertFalse(self.saved)


if __name__ == "__main__":
    unittest.main()
