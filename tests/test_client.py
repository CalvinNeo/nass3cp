import hashlib
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from nass3cp import client
from nass3cp.errors import ProtocolError


class FakeApi:
    def __init__(self, remote_content=b""):
        self.transfer_id = "a" * 32
        self.remote_content = remote_content
        self.objects = {}
        self.current = {}
        self.acknowledged = False
        self.aborted = False
        self.max_objects = 0

    def create_upload(
        self,
        path,
        size,
        mtime_ns,
        overwrite,
        inflight=None,
        resume=False,
        resume_id=None,
    ):
        if (
            resume
            and resume_id == self.transfer_id
            and self.current.get("direction") == "upload"
        ):
            self.current["resumed"] = True
            return dict(self.current)
        self.current = {
            "id": self.transfer_id,
            "direction": "upload",
            "status": "awaiting_upload",
            "size": size,
            "chunks": (size + 3) // 4,
            "chunk_size": 4,
            "bytes_transferred": 0,
            "resumable": resume,
            "resumed": False,
        }
        return dict(self.current)

    def create_download(self, path, inflight=None, resume=False, resume_id=None):
        if (
            resume
            and resume_id == self.transfer_id
            and self.current.get("direction") == "download"
        ):
            self.current["resumed"] = True
            return dict(self.current)
        size = len(self.remote_content)
        self.current = {
            "id": self.transfer_id,
            "direction": "download",
            "status": "ready",
            "size": size,
            "chunks": (size + 3) // 4,
            "chunk_size": 4,
            "bytes_transferred": size,
            "sha256": hashlib.sha256(self.remote_content).hexdigest(),
            "mtime_ns": 1000000000,
            "resumable": resume,
            "resumed": False,
        }
        for index in range(self.current["chunks"]):
            self.objects[index] = self.remote_content[index * 4 : index * 4 + 4]
        return dict(self.current)

    def urls(self, transfer_id, start, count):
        result = []
        for index in range(start, start + count):
            headers = {}
            if self.current["direction"] == "upload":
                length = min(4, self.current["size"] - index * 4)
                headers["content-length"] = str(length)
            item = {
                "index": index,
                "url": "https://s3.example.test/%d" % index,
                "headers": headers,
            }
            if self.current.get("pipeline") and self.current["direction"] == "download":
                item["sha256"] = hashlib.sha256(self.objects[index]).hexdigest()
            result.append(item)
        return result

    def commit(self, transfer_id, digest):
        combined = b"".join(self.objects[index] for index in sorted(self.objects))
        if hashlib.sha256(combined).hexdigest() != digest:
            raise AssertionError("client sent the wrong digest")
        self.current.update(
            {"status": "complete", "bytes_transferred": len(combined), "sha256": digest}
        )
        return dict(self.current)

    def state(self, transfer_id):
        return dict(self.current)

    def acknowledge(self, transfer_id):
        self.acknowledged = True
        return dict(self.current)

    def abort(self, transfer_id):
        self.aborted = True


def fake_data_request(api):
    def request(method, item, data=None, expected=None, attempts=4, progress=None):
        index = int(item["url"].rsplit("/", 1)[1])
        if method == "PUT":
            if progress is not None:
                progress(len(data) // 2)
            api.objects[index] = data
            api.max_objects = max(api.max_objects, len(api.objects))
            if progress is not None:
                progress(len(data))
            return b""
        value = api.objects[index]
        if progress is not None:
            progress(len(value) // 2)
        if expected is not None and len(value) != expected:
            raise ProtocolError("wrong fake chunk size")
        if progress is not None:
            progress(len(value))
        return value

    return request


class PipelineUploadApi(FakeApi):
    def __init__(self):
        super().__init__()
        self.received = {}

    def create_upload(self, path, size, mtime_ns, overwrite, inflight=None):
        state = super().create_upload(path, size, mtime_ns, overwrite, inflight)
        state.update(
            {
                "status": "receiving",
                "pipeline": True,
                "inflight": inflight,
                "chunks_staged": 0,
                "chunks_consumed": 0,
                "producer_complete": False,
            }
        )
        self.current = state
        return dict(state)

    def chunk_ready(self, transfer_id, index, digest):
        data = self.objects.pop(index)
        if hashlib.sha256(data).hexdigest() != digest:
            raise AssertionError("client announced the wrong chunk digest")
        self.received[index] = data
        self.current["chunks_staged"] += 1
        self.current["chunks_consumed"] += 1
        self.current["bytes_transferred"] += len(data)
        return dict(self.current)

    def commit(self, transfer_id, digest):
        combined = b"".join(self.received[index] for index in sorted(self.received))
        if hashlib.sha256(combined).hexdigest() != digest:
            raise AssertionError("client sent the wrong digest")
        self.current.update(
            {
                "status": "complete",
                "sha256": digest,
                "producer_complete": True,
            }
        )
        return dict(self.current)


class PipelineDownloadApi(FakeApi):
    def __init__(self, remote_content):
        super().__init__(remote_content)
        self.inflight = 0

    def _fill(self):
        while (
            self.current["chunks_staged"] < self.current["chunks"]
            and self.current["chunks_staged"] - self.current["chunks_consumed"]
            < self.inflight
        ):
            index = self.current["chunks_staged"]
            self.objects[index] = self.remote_content[index * 4 : index * 4 + 4]
            self.current["chunks_staged"] += 1
            self.current["bytes_transferred"] += len(self.objects[index])
            self.max_objects = max(self.max_objects, len(self.objects))
        if self.current["chunks_staged"] == self.current["chunks"]:
            self.current.update(
                {
                    "status": "ready",
                    "producer_complete": True,
                    "sha256": hashlib.sha256(self.remote_content).hexdigest(),
                }
            )

    def create_download(self, path, inflight=None):
        size = len(self.remote_content)
        self.inflight = inflight
        self.current = {
            "id": self.transfer_id,
            "direction": "download",
            "status": "preparing",
            "size": size,
            "chunks": (size + 3) // 4,
            "chunk_size": 4,
            "bytes_transferred": 0,
            "mtime_ns": 1000000000,
            "pipeline": True,
            "inflight": inflight,
            "chunks_staged": 0,
            "chunks_consumed": 0,
            "producer_complete": False,
        }
        self._fill()
        return dict(self.current)

    def acknowledge_chunk(self, transfer_id, index, digest):
        if index != self.current["chunks_consumed"]:
            raise AssertionError("client acknowledged chunks out of order")
        data = self.objects.pop(index)
        if hashlib.sha256(data).hexdigest() != digest:
            raise AssertionError("client acknowledged the wrong chunk digest")
        self.current["chunks_consumed"] += 1
        self._fill()
        return dict(self.current)


class IncrementalPipelineDownloadApi(PipelineDownloadApi):
    def _fill(self):
        if (
            self.current["chunks_staged"] < self.current["chunks"]
            and self.current["chunks_staged"] - self.current["chunks_consumed"]
            < self.inflight
        ):
            index = self.current["chunks_staged"]
            self.objects[index] = self.remote_content[index * 4 : index * 4 + 4]
            self.current["chunks_staged"] += 1
            self.current["bytes_transferred"] += len(self.objects[index])
            self.max_objects = max(self.max_objects, len(self.objects))
        if self.current["chunks_staged"] == self.current["chunks"]:
            self.current.update(
                {
                    "status": "ready",
                    "producer_complete": True,
                    "sha256": hashlib.sha256(self.remote_content).hexdigest(),
                }
            )

    def state(self, transfer_id):
        self._fill()
        return dict(self.current)


class ListApi:
    def __init__(self):
        self.calls = []
        self.entries = [
            {"name": "alpha", "type": "directory", "size": None, "mtime_ns": 1},
            {"name": "beta.bin", "type": "file", "size": 4, "mtime_ns": 2},
            {"name": "current", "type": "symlink", "size": None, "mtime_ns": 3},
        ]

    def list_directory(self, path, cursor=0, limit=500):
        self.calls.append((path, cursor, limit))
        page = self.entries[cursor : cursor + limit]
        next_cursor = cursor + len(page)
        return {
            "entries": page,
            "next_cursor": next_cursor if next_cursor < len(self.entries) else None,
        }


class ClientTransferTests(unittest.TestCase):
    def test_control_client_allows_explicit_http(self):
        api = client.ApiClient("http://127.0.0.1:9443", "password")
        self.assertEqual(api.base_url, "http://127.0.0.1:9443")

    def test_control_client_rejects_tls_options_with_http(self):
        with self.assertRaises(ProtocolError):
            client.ApiClient("http://127.0.0.1:9443", "password", insecure=True)

    def test_list_remote_collects_validated_pages(self):
        api = ListApi()

        entries = client.list_remote(api, ".", page_size=2)

        self.assertEqual([entry["name"] for entry in entries], ["alpha", "beta.bin", "current"])
        self.assertEqual(api.calls, [(".", 0, 2), (".", 2, 2)])

    def test_list_remote_rejects_a_cursor_that_does_not_advance_by_the_page(self):
        class BadListApi:
            def list_directory(self, path, cursor=0, limit=500):
                return {"entries": [], "next_cursor": cursor + 1}

        with self.assertRaises(ProtocolError):
            client.list_remote(BadListApi(), ".")

    def test_upload_reads_hashes_and_sends_all_chunks(self):
        content = b"abcdefghij"
        api = FakeApi()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(content)
            with patch("nass3cp.client._data_request", side_effect=fake_data_request(api)):
                client.upload(api, str(source), "dest.bin", False, 2, 60, True)
        self.assertEqual(b"".join(api.objects[index] for index in sorted(api.objects)), content)
        self.assertFalse(api.aborted)

    def test_pipeline_upload_never_exceeds_inflight_window(self):
        content = b"abcdefghijklmnopqrstuvwxyz0123456789"
        api = PipelineUploadApi()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(content)
            with patch("nass3cp.client._data_request", side_effect=fake_data_request(api)):
                client.upload(api, str(source), "dest.bin", False, 16, 60, True, 3)
        self.assertEqual(
            b"".join(api.received[index] for index in sorted(api.received)),
            content,
        )
        self.assertLessEqual(api.max_objects, 3)
        self.assertFalse(api.objects)

    def test_resumable_upload_records_each_completed_block_and_only_retries_missing(self):
        content = b"abcdefghijkl"
        api = FakeApi()
        attempts = {}
        request = fake_data_request(api)

        def fail_first_block_once(method, item, data=None, expected=None, attempts_count=4, progress=None):
            index = int(item["url"].rsplit("/", 1)[1])
            attempts[index] = attempts.get(index, 0) + 1
            if method == "PUT" and index == 0 and attempts[index] == 1:
                raise ProtocolError("simulated interruption")
            return request(method, item, data, expected, attempts_count, progress)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(content)
            checkpoint = client._resume_path(source, "upload")
            with patch(
                "nass3cp.client._data_request", side_effect=fail_first_block_once
            ), self.assertRaises(ProtocolError):
                client.upload(
                    api,
                    str(source),
                    "dest.bin",
                    False,
                    2,
                    60,
                    True,
                    resume=True,
                )

            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(set(saved["completed"]), {"1"})

            with patch(
                "nass3cp.client._data_request", side_effect=fail_first_block_once
            ):
                client.upload(
                    api,
                    str(source),
                    "dest.bin",
                    False,
                    2,
                    60,
                    True,
                    resume=True,
                )

            self.assertFalse(checkpoint.exists())
        self.assertEqual(attempts, {0: 2, 1: 1, 2: 1})
        self.assertEqual(b"".join(api.objects[index] for index in sorted(api.objects)), content)
        self.assertFalse(api.aborted)

    def test_parallel_upload_refills_workers_without_exceeding_jobs(self):
        content = b"abcdefghijkl"
        api = FakeApi()
        second_started = threading.Event()
        third_started = threading.Event()
        concurrency_lock = threading.Lock()
        active_requests = 0
        max_active_requests = 0
        request = fake_data_request(api)

        def delayed_request(method, item, data=None, expected=None, attempts=4, progress=None):
            nonlocal active_requests, max_active_requests
            index = int(item["url"].rsplit("/", 1)[1])
            with concurrency_lock:
                active_requests += 1
                max_active_requests = max(max_active_requests, active_requests)
            try:
                if index == 1:
                    second_started.set()
                    if not third_started.wait(timeout=2.0):
                        raise ProtocolError("parallel worker pool did not refill")
                elif index == 0:
                    if not second_started.wait(timeout=1.0):
                        raise ProtocolError("second parallel request did not start")
                elif index == 2:
                    third_started.set()
                return request(method, item, data, expected, attempts, progress)
            finally:
                with concurrency_lock:
                    active_requests -= 1

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(content)
            with patch("nass3cp.client._data_request", side_effect=delayed_request):
                client.upload(
                    api,
                    str(source),
                    "dest.bin",
                    False,
                    2,
                    60,
                    True,
                    resume=True,
                )

        self.assertTrue(third_started.is_set())
        self.assertEqual(max_active_requests, 2)

    def test_download_verifies_and_atomically_writes_file(self):
        content = b"0123456789"
        api = FakeApi(content)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "destination.bin"
            with patch("nass3cp.client._data_request", side_effect=fake_data_request(api)):
                client.download(api, "source.bin", str(destination), False, 2, 60, True)
            self.assertEqual(destination.read_bytes(), content)
            self.assertFalse(list(Path(directory).glob("*.nass3cp-part")))
        self.assertTrue(api.acknowledged)

    def test_pipeline_download_deletes_each_window_before_refilling(self):
        content = b"abcdefghijklmnopqrstuvwxyz0123456789"
        api = PipelineDownloadApi(content)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "destination.bin"
            with patch("nass3cp.client._data_request", side_effect=fake_data_request(api)):
                client.download(api, "source.bin", str(destination), False, 16, 60, True, 3)
            self.assertEqual(destination.read_bytes(), content)
        self.assertTrue(api.acknowledged)
        self.assertLessEqual(api.max_objects, 3)
        self.assertFalse(api.objects)

    def test_resumable_download_keeps_out_of_order_blocks_and_only_requests_missing(self):
        content = b"abcdefghijkl"
        api = FakeApi(content)
        attempts = {}
        request = fake_data_request(api)

        def fail_first_block_once(method, item, data=None, expected=None, attempts_count=4, progress=None):
            index = int(item["url"].rsplit("/", 1)[1])
            attempts[index] = attempts.get(index, 0) + 1
            if method == "GET" and index == 0 and attempts[index] == 1:
                raise ProtocolError("simulated interruption")
            return request(method, item, data, expected, attempts_count, progress)

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "destination.bin"
            checkpoint = client._resume_path(destination, "download")
            with patch(
                "nass3cp.client._data_request", side_effect=fail_first_block_once
            ), self.assertRaises(ProtocolError):
                client.download(
                    api,
                    "source.bin",
                    str(destination),
                    False,
                    2,
                    60,
                    True,
                    resume=True,
                )

            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(set(saved["completed"]), {"1"})

            with patch(
                "nass3cp.client._data_request", side_effect=fail_first_block_once
            ):
                client.download(
                    api,
                    "source.bin",
                    str(destination),
                    False,
                    2,
                    60,
                    True,
                    resume=True,
                )

            self.assertEqual(destination.read_bytes(), content)
            self.assertFalse(checkpoint.exists())
            self.assertFalse(list(Path(directory).glob("*.nass3cp-part")))
        self.assertEqual(attempts, {0: 2, 1: 1, 2: 1})
        self.assertTrue(api.acknowledged)

    def test_pipeline_download_refills_jobs_while_requests_are_active(self):
        content = b"abcdefgh"
        api = IncrementalPipelineDownloadApi(content)
        second_started = threading.Event()
        first_saw_second = []
        concurrency_lock = threading.Lock()
        active_requests = 0
        max_active_requests = 0
        request = fake_data_request(api)

        def delayed_request(method, item, data=None, expected=None, attempts=4, progress=None):
            nonlocal active_requests, max_active_requests
            index = int(item["url"].rsplit("/", 1)[1])
            with concurrency_lock:
                active_requests += 1
                max_active_requests = max(max_active_requests, active_requests)
            try:
                if index == 1:
                    second_started.set()
                elif index == 0:
                    first_saw_second.append(second_started.wait(timeout=1.0))
                return request(method, item, data, expected, attempts, progress)
            finally:
                with concurrency_lock:
                    active_requests -= 1

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "destination.bin"
            with patch("nass3cp.client._data_request", side_effect=delayed_request):
                client.download(api, "source.bin", str(destination), False, 2, 60, True, 2)
            self.assertEqual(destination.read_bytes(), content)

        self.assertEqual(first_saw_second, [True])
        self.assertEqual(max_active_requests, 2)

    def test_data_plane_rejects_plain_http_before_connecting(self):
        with self.assertRaises(ProtocolError):
            client._data_request("GET", {"url": "http://example.test/a", "headers": {}})

    def test_upload_prints_both_progress_stages(self):
        api = FakeApi()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(b"abcdefghij")
            output = io.StringIO()
            with patch(
                "nass3cp.client._data_request", side_effect=fake_data_request(api)
            ), redirect_stderr(output):
                client.upload(api, str(source), "dest.bin", False, 2, 60, False)
        rendered = output.getvalue()
        self.assertIn("upload to S3:", rendered)
        self.assertIn("copy from S3 to NAS:", rendered)
        self.assertGreaterEqual(rendered.count("100.0%"), 2)

    def test_download_prints_both_progress_stages(self):
        api = FakeApi(b"0123456789")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "destination.bin"
            output = io.StringIO()
            with patch(
                "nass3cp.client._data_request", side_effect=fake_data_request(api)
            ), redirect_stderr(output):
                client.download(api, "source.bin", str(destination), False, 2, 60, False)
        rendered = output.getvalue()
        self.assertIn("copy from NAS to S3:", rendered)
        self.assertIn("download from S3:", rendered)
        self.assertGreaterEqual(rendered.count("100.0%"), 2)

    def test_quiet_suppresses_progress(self):
        api = FakeApi()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(b"abcdefghij")
            output = io.StringIO()
            with patch(
                "nass3cp.client._data_request", side_effect=fake_data_request(api)
            ), redirect_stderr(output):
                client.upload(api, str(source), "dest.bin", False, 2, 60, True)
        self.assertEqual(output.getvalue(), "")

    def test_progress_reader_reports_bytes_while_body_is_read(self):
        reported = []
        reader = client._ProgressReader(b"abcdef", reported.append)
        self.assertEqual(reader.read(2), b"ab")
        self.assertEqual(reported, [])
        self.assertEqual(reader.read(), b"cdef")
        self.assertEqual(reported, [6])


if __name__ == "__main__":
    unittest.main()
